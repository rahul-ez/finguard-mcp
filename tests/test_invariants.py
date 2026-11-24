"""
FinGuard Invariant & Deterministic Math Test Suite
Locks in mathematical correctness and accounting invariants:
1. Paise conservation & rollback on violation
2. SHA-256 deduplication
3. Refund SLA breach transition
4. Guarded commit enforcement (user_approved=True + token match)
5. Scenario C velocity formula sanity check
6. Undo last commit capability
7. Integer paise validation (no floats)
"""

import os
import sqlite3
import pytest
import hashlib
from pathlib import Path

from engine.models import (
    NormalizedEvent,
    paise_to_rupees,
    rupees_to_paise,
)
from engine.reconciler import (
    ReconciliationEngine,
    sweep_refund_sla,
    compute_approval_token,
)
from engine.consequences import ConsequenceEngine
from db.seed_demo import seed_database


TEST_DB_PATH = Path(__file__).resolve().parent / "test_finguard.db"


@pytest.fixture(autouse=True)
def setup_test_db():
    """Initializes and seeds a dedicated clean test SQLite database."""
    seed_database(TEST_DB_PATH)
    yield
    if TEST_DB_PATH.exists():
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass


def test_integer_paise_representation():
    """Rule 2: Money must be integer paise. Floats are rejected by Pydantic."""
    # Valid int paise
    evt = NormalizedEvent(
        external_ref_hash="hash_1",
        source_type="gmail_receipt",
        event_type="ORDER_PLACED",
        amount_paise=249950,
        event_timestamp="2026-11-03T10:00:00",
        raw_snippet="Test order",
        confidence=1.0,
    )
    assert evt.amount_paise == 249950
    assert paise_to_rupees(249950) == "₹2,499.50"
    assert rupees_to_paise(2499.50) == 249950

    # Float amount should fail validation
    with pytest.raises(Exception):
        NormalizedEvent(
            external_ref_hash="hash_2",
            source_type="gmail_receipt",
            event_type="ORDER_PLACED",
            amount_paise=2499.50,  # Invalid float!
            event_timestamp="2026-11-03T10:00:00",
            raw_snippet="Invalid float order",
            confidence=1.0,
        )


def test_conservation_of_funds_invariant():
    """Rule 4.1: MonthlyIncome == sum(Fixed) + sum(Variable) + SavingsTarget + Buffer."""
    reconciler = ReconciliationEngine(str(TEST_DB_PATH))
    conn = reconciler._get_connection()
    assert reconciler.check_funds_invariant(conn, "2026-11") is True

    # Artificially violate invariant by changing an allocation
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE category_allocations SET allocated_paise = allocated_paise + 500000 WHERE category = 'entertainment'"
    )
    conn.commit()

    # Invariant should now fail
    assert reconciler.check_funds_invariant(conn, "2026-11") is False
    conn.close()


def test_idempotent_deduplication():
    """Section 6.4: Re-ingesting the exact same event payload is a no-op."""
    reconciler = ReconciliationEngine(str(TEST_DB_PATH))
    raw_payload = "email_order_payload_unique_123"
    payload_hash = hashlib.sha256(raw_payload.encode()).hexdigest()

    event = NormalizedEvent(
        external_ref_hash=payload_hash,
        source_type="gmail_receipt",
        event_type="ORDER_PLACED",
        merchant_normalized="TestMerchant",
        category="shopping",
        amount_paise=150000,
        event_timestamp="2026-11-18T12:00:00",
        order_ref="TEST-REF-001",
        raw_snippet=raw_payload,
        confidence=1.0,
    )

    # First propose: should propose 2 inserts (lifecycle + raw_event)
    preview1 = reconciler.propose_reconciliation_batch([event])
    assert len(preview1.proposed_inserts) == 2

    # Commit first run
    res = reconciler.commit_reconciliation_batch(
        approval_token=preview1.approval_token,
        user_approved=True,
        proposed_preview=preview1,
    )
    assert res.committed is True

    # Second propose with same event: should be 0 inserts (idempotent skip)
    preview2 = reconciler.propose_reconciliation_batch([event])
    assert len(preview2.proposed_inserts) == 0


def test_refund_sla_transition():
    """Section 6.3: Any REFUND_PENDING past expected_refund_by becomes REFUND_OVERDUE."""
    conn = sqlite3.connect(TEST_DB_PATH)
    cursor = conn.cursor()

    # Insert a REFUND_PENDING lifecycle with refund date 2026-11-10
    cursor.execute(
        """
        INSERT INTO transaction_lifecycles (
            lifecycle_id, merchant_normalized, category, expected_amount_paise,
            settled_net_paise, lifecycle_status, order_ref, upi_utr,
            expected_refund_by, is_one_time_spike, created_at, updated_at
        ) VALUES ('lc_test_refund', 'Flipkart', 'shopping', 199900, 199900,
                  'REFUND_PENDING', 'FK-991', '991823', '2026-11-10', 0,
                  '2026-11-05T10:00:00', '2026-11-05T10:00:00')
        """
    )
    conn.commit()

    # Sweep on reference date 2026-11-18 (8 days past SLA)
    transitioned = sweep_refund_sla(conn, reference_date="2026-11-18")
    assert transitioned >= 1

    cursor.execute("SELECT lifecycle_status FROM transaction_lifecycles WHERE lifecycle_id = 'lc_test_refund'")
    status = cursor.fetchone()[0]
    assert status == "REFUND_OVERDUE"
    conn.close()


def test_guarded_commit_requires_user_approval_and_token():
    """Section 7.2: Commit refuses user_approved=False and token mismatch."""
    reconciler = ReconciliationEngine(str(TEST_DB_PATH))

    event = NormalizedEvent(
        external_ref_hash="hash_guard_test",
        source_type="bank_statement",
        event_type="BANK_DEBIT",
        merchant_normalized="Coffee House",
        category="food_groceries",
        amount_paise=35000,
        event_timestamp="2026-11-18T14:00:00",
        raw_snippet="Coffee debit",
        confidence=1.0,
    )

    preview = reconciler.propose_reconciliation_batch([event])

    # Case 1: user_approved is False -> Rejected
    res1 = reconciler.commit_reconciliation_batch(
        approval_token=preview.approval_token,
        user_approved=False,
        proposed_preview=preview,
    )
    assert res1.committed is False
    assert "user_approved must be True" in (res1.reason or "")

    # Case 2: approval_token mismatch -> Rejected
    res2 = reconciler.commit_reconciliation_batch(
        approval_token="tampered_fake_token_12345",
        user_approved=True,
        proposed_preview=preview,
    )
    assert res2.committed is False
    assert "approval_token does not match" in (res2.reason or "")

    # Case 3: valid token + user_approved=True -> Succeeded
    res3 = reconciler.commit_reconciliation_batch(
        approval_token=preview.approval_token,
        user_approved=True,
        proposed_preview=preview,
    )
    assert res3.committed is True


def test_scenario_c_velocity_sanity_check():
    """
    Section 10 Sanity check for Scenario C:
    Budget = 400,000 paise (₹4,000)
    Spent day 18 = 360,000 paise (₹3,600)
    V = 360000 // 18 = 20000 paise/day
    S_hat = 360000 + 20000 * 12 = 600000 paise (₹6,000)
    Overrun = 600000 - 400000 = 200000 paise (₹2,000)
    WeeklyTrim = 200000 * 7 // 12 = 116666 paise (₹1,166.66)
    Planned Savings = 1,200,000 paise (₹12,000)
    Projected Savings = 1,200,000 - 200,000 = 1,000,000 paise (₹10,000)
    """
    engine = ConsequenceEngine(str(TEST_DB_PATH))
    report = engine.run_consequence_engine(month_key="2026-11", reference_date="2026-11-18")

    # Locate velocity alert for entertainment
    ent_alerts = [a for a in report.alerts if a.kind == "VELOCITY" and "entertainment" in a.message]
    assert len(ent_alerts) == 1
    ent_alert = ent_alerts[0]

    # Verify exact math from Section 10
    assert ent_alert.impact_paise == 200000  # ₹2,000 overrun
    assert "6,000.00" in ent_alert.message
    assert "1,166.66" in ent_alert.message
    assert report.projected_savings_paise == 1000000  # ₹10,000 remaining savings


def test_undo_last_commit():
    """Section 7.2: undo_last_commit restores previous state from audit log."""
    reconciler = ReconciliationEngine(str(TEST_DB_PATH))

    event = NormalizedEvent(
        external_ref_hash="hash_undo_test",
        source_type="bank_statement",
        event_type="BANK_DEBIT",
        merchant_normalized="Test Shop",
        category="shopping",
        amount_paise=50000,
        event_timestamp="2026-11-18T15:00:00",
        raw_snippet="Test Shop Debit",
        confidence=1.0,
    )

    preview = reconciler.propose_reconciliation_batch([event])
    reconciler.commit_reconciliation_batch(
        approval_token=preview.approval_token,
        user_approved=True,
        proposed_preview=preview,
    )

    # Verify new row exists
    conn = reconciler._get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transaction_lifecycles WHERE merchant_normalized = 'Test Shop'")
    assert cursor.fetchone()[0] == 1
    conn.close()

    # Perform undo
    undo_res = reconciler.undo_last_commit()
    assert undo_res.reverted is True

    # Verify restored state
    conn = reconciler._get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transaction_lifecycles WHERE merchant_normalized = 'Test Shop'")
    assert cursor.fetchone()[0] == 0
    conn.close()
