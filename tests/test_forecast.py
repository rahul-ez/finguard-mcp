"""Regression coverage for date-aware cash forecasting and marginal purchase impact."""

import sqlite3

import pytest

from db.seed_demo import seed_database
from engine.consequences import ConsequenceEngine
from engine.dashboard import dashboard_snapshot
from engine.forecast import forecast_context


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "forecast.db"
    seed_database(db)
    return ConsequenceEngine(str(db))


def test_early_month_purchase_uses_early_balance_and_excludes_future_transactions(engine):
    result = engine.simulate_purchase_decision(50000, "food_groceries", "BigBasket", "2026-11-03")
    assert result.balance_as_of == "2026-11-03"
    assert result.liquid_cash_before_paise == 5350100
    assert result.pending_committed_paise == 0
    assert result.category_spent_paise == 0  # First grocery transaction is November 4.
    assert result.safe_to_spend_before_paise == 88896
    assert result.safe_to_spend_after_paise == 87044
    assert result.discretionary_before_paise - result.discretionary_after_paise == 50000
    assert not result.collides_with_fixed_bill
    snapshot = dashboard_snapshot(engine, "2026-11", "2026-11-03")
    assert not any(row["created_at"][:10] > "2026-11-03" for row in snapshot["lifecycles"])
    assert not any(alert.kind in ("REFUND_OVERDUE", "PRICE_HIKE") for alert in snapshot["report"].alerts)


def test_historical_refund_state_is_replayed_without_writing(engine):
    with open(engine.db_path, "rb") as file:
        original = file.read()
    snapshots = {}
    for ref in ("2026-11-03", "2026-11-06", "2026-11-18", "2026-11-03"):
        context = forecast_context(engine, "2026-11", ref)
        snapshots[ref] = next(row["lifecycle_status"] for row in context["lifecycles"] if row["lifecycle_id"] == "lc_amz_2499_01")
    assert snapshots == {"2026-11-03": "DEBITED", "2026-11-06": "REFUND_PENDING", "2026-11-18": "REFUND_OVERDUE"}
    with open(engine.db_path, "rb") as file:
        assert file.read() == original


def test_existing_shortfall_and_new_purchase_are_separate_and_match_overview(engine):
    report = engine.run_consequence_engine()
    result = engine.simulate_purchase_decision(50000, "food_groceries", "BigBasket")
    assert report.remaining_liquid_cash_paise == 1930000
    assert result.bill_shortfall_before_paise == report.cash_flow_shortfall_paise == 379900
    assert result.bill_shortfall_after_paise == 429900
    assert result.additional_bill_shortfall_paise == 50000
    assert result.safe_to_spend_before_paise == report.safe_to_spend_today_paise == 0
    assert result.discretionary_before_paise == report.discretionary_cash_paise
    assert result.discretionary_after_paise == result.discretionary_before_paise - 50000
    assert result.upcoming_fixed_paise == report.upcoming_fixed_paise == 1569900
    assert "already exceed cash" in result.notes[0]
    assert not any("drops from" in note or "will cause" in note for note in result.notes)


def test_category_overrun_reports_only_increment_and_includes_pending_orders(engine):
    result = engine.simulate_purchase_decision(50000, "shopping", "BigBasket")
    assert result.category_pending_paise == 740000
    assert result.category_spent_paise == 1990100  # One-time expense counts once in actual category usage.
    assert result.category_overrun_before_paise > 0
    assert result.additional_category_overrun_paise == 50000
    assert result.category_overrun_paise == result.category_overrun_before_paise + 50000


def test_bills_do_not_disappear_merely_because_the_due_date_passed(engine):
    context = forecast_context(engine, "2026-11", "2026-11-29")
    rent = next(bill for bill in context["commitments"] if bill["category"] == "rent_utilities")
    assert rent["amount_paise"] == 1500000
    assert rent["overdue"]
    assert context["balance_as_of"] == "2026-11-17"  # No fabricated later balance.


def test_paid_scheduled_bill_is_not_reserved_again(engine):
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("""INSERT INTO transaction_lifecycles VALUES
            ('paid_rent','Landlord - Greenwood Apts','rent_utilities',1500000,1500000,
             'DEBITED',NULL,NULL,NULL,0,'2026-11-28T10:00:00','2026-11-28T10:00:00')""")
    context = forecast_context(engine, "2026-11", "2026-11-29")
    assert not any(bill["category"] == "rent_utilities" for bill in context["commitments"])
    assert context["balance_paise"] == 430000
    assert context["balance_basis"] == "statement_plus_ledger"


def test_post_statement_refund_increases_cash_without_counting_orders_as_spend(engine):
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("""INSERT INTO raw_events VALUES
            ('late_refund','lc_amz_2499_01','bank_statement','late_refund_unique','BANK_REFUND',249900,
             '2026-11-18T10:00:00','Refund credited',1.0)""")
        conn.execute("""UPDATE transaction_lifecycles SET lifecycle_status='REFUNDED',settled_net_paise=0,
                        updated_at='2026-11-18T10:00:00' WHERE lifecycle_id='lc_amz_2499_01'""")
    report = engine.run_consequence_engine()
    assert report.remaining_liquid_cash_paise == 1930000 + 249900
    assert report.pending_committed_paise == 740000  # An order reserves cash; it is not another debit.
    assert not any(alert.kind == "REFUND_OVERDUE" for alert in report.alerts)


def test_daily_allowance_rounds_remaining_cash_once(engine, tmp_path):
    statement = tmp_path / "rounding.csv"
    statement.write_text("date,balance_paise\n2026-11-03,999\n", encoding="utf-8")
    engine.statement_path = statement
    engine.calendar_path = tmp_path / "no_calendar.json"
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("UPDATE budget_profiles SET savings_target_paise = 0, emergency_buffer_paise = 0")
        conn.execute("UPDATE category_allocations SET allocated_paise = 0 WHERE is_fixed = 1")
    result = engine.simulate_purchase_decision(1, "food_groceries", "Test", "2026-11-03")
    assert result.safe_to_spend_before_paise == 999 // 27
    assert result.safe_to_spend_after_paise == 998 // 27
    assert result.safe_to_spend_before_paise != result.safe_to_spend_after_paise


def test_missing_statement_has_explicit_estimate_and_mismatched_cycle_is_rejected(engine, tmp_path):
    engine.statement_path = tmp_path / "missing.csv"
    report = engine.run_consequence_engine(reference_date="2026-11-03")
    assert report.balance_basis == "budget_estimate"
    with pytest.raises(ValueError, match="selected budget cycle"):
        engine.run_consequence_engine("2026-11", "2026-12-03")
    for amount in (-1, 1.5, True):
        with pytest.raises(ValueError, match="integer in paise"):
            engine.simulate_purchase_decision(amount, "shopping", "Test")
