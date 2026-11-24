"""
FinGuard MCP - Reconciliation Engine
Implements:
1. Deterministic lifecycle state machine (ORDERED -> DEBITED -> CANCELLED -> REFUND_PENDING -> REFUNDED / REFUND_OVERDUE).
2. Priority matching: Exact (order_ref/upi_utr) -> Fuzzy window (merchant + amount + ±5 days) -> New lifecycle.
3. Daily sweep for refund SLA breach (current_date > expected_refund_by).
4. Idempotent SHA-256 event deduplication.
5. Two-phase guarded commits (propose -> approval_token -> commit / rollback).
"""

import json
import sqlite3
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

from engine.models import (
    NormalizedEvent,
    CommitPreview,
    CommitResult,
    RollbackResult,
    TransactionLifecycle,
    LifecycleStatus,
)


def compute_approval_token(inserts: List[Dict[str, Any]], updates: List[Dict[str, Any]]) -> str:
    """Computes a deterministic SHA-256 token over the proposed diff."""
    payload = json.dumps({"inserts": inserts, "updates": updates}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_iso_date(dt_str: str) -> datetime:
    """Parses standard ISO date or datetime strings."""
    if "T" in dt_str:
        return datetime.fromisoformat(dt_str)
    return datetime.strptime(dt_str[:10], "%Y-%m-%d")


def sweep_refund_sla(conn: sqlite3.Connection, reference_date: str) -> int:
    """
    Daily sweep: Transitions any REFUND_PENDING lifecycle past expected_refund_by to REFUND_OVERDUE.
    Returns number of lifecycles transitioned.
    """
    cursor = conn.cursor()
    ref_dt = parse_iso_date(reference_date).strftime("%Y-%m-%d")
    cursor.execute(
        """
        UPDATE transaction_lifecycles
        SET lifecycle_status = 'REFUND_OVERDUE', updated_at = ?
        WHERE lifecycle_status = 'REFUND_PENDING'
          AND expected_refund_by IS NOT NULL
          AND expected_refund_by < ?
        """,
        (reference_date, ref_dt),
    )
    return cursor.rowcount


class ReconciliationEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def check_funds_invariant(self, conn: sqlite3.Connection, month_key: str = "2026-11") -> bool:
        """
        Enforces 4.1 Conservation of Funds Invariant:
        MonthlyIncome == sum(FixedCommitments) + sum(VariableAllocations) + SavingsTarget + EmergencyBuffer
        """
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT monthly_income_paise, savings_target_paise, emergency_buffer_paise
            FROM budget_profiles
            WHERE month_key = ?
            """,
            (month_key,),
        )
        profile = cursor.fetchone()
        if not profile:
            return True  # No profile for month yet

        income = profile["monthly_income_paise"]
        savings = profile["savings_target_paise"]
        buffer = profile["emergency_buffer_paise"]

        cursor.execute(
            """
            SELECT SUM(allocated_paise) as total_alloc
            FROM category_allocations
            WHERE month_key = ?
            """,
            (month_key,),
        )
        row = cursor.fetchone()
        total_alloc = row["total_alloc"] or 0

        return income == (total_alloc + savings + buffer)

    def propose_reconciliation_batch(
        self,
        events: List[NormalizedEvent],
        reference_date: str = "2026-11-18",
    ) -> CommitPreview:
        """
        Dry-run matcher that inspects incoming normalized events, checks existing DB records,
        and constructs proposed inserts and updates with an approval token. Does NOT write to DB.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Check for existing event hashes (Idempotency)
        cursor.execute("SELECT external_ref_hash FROM raw_events")
        existing_hashes = {row["external_ref_hash"] for row in cursor.fetchall()}

        # Load active lifecycles
        cursor.execute(
            """
            SELECT lifecycle_id, merchant_normalized, category, expected_amount_paise,
                   settled_net_paise, lifecycle_status, order_ref, upi_utr,
                   expected_refund_by, created_at, updated_at
            FROM transaction_lifecycles
            """
        )
        existing_lifecycles = [dict(row) for row in cursor.fetchall()]

        proposed_inserts: List[Dict[str, Any]] = []
        proposed_updates: List[Dict[str, Any]] = []
        ambiguous_items: List[NormalizedEvent] = []

        ref_dt = parse_iso_date(reference_date)

        for event in events:
            # 1. Skip if event already ingested (idempotency)
            if event.external_ref_hash in existing_hashes:
                continue

            # Flag ambiguous events (e.g. unmapped personal UPI handle)
            if event.requires_user_classification:
                ambiguous_items.append(event)
                continue

            # Try matching with existing lifecycles
            matched_lc = None
            event_dt = parse_iso_date(event.event_timestamp)

            # Match priority 1: Exact match on order_ref or upi_utr
            if event.order_ref:
                for lc in existing_lifecycles:
                    if lc.get("order_ref") == event.order_ref:
                        matched_lc = lc
                        break

            if not matched_lc and event.upi_utr:
                for lc in existing_lifecycles:
                    if lc.get("upi_utr") == event.upi_utr:
                        matched_lc = lc
                        break

            # Match priority 2: Fuzzy window match (merchant + exact amount + ±5 days)
            if not matched_lc and event.merchant_normalized:
                for lc in existing_lifecycles:
                    if (
                        lc.get("merchant_normalized", "").lower() == event.merchant_normalized.lower()
                        and lc.get("expected_amount_paise") == event.amount_paise
                    ):
                        lc_dt = parse_iso_date(lc["created_at"])
                        if abs((event_dt - lc_dt).days) <= 5:
                            matched_lc = lc
                            break

            # Apply state transition logic
            if matched_lc:
                lc_id = matched_lc["lifecycle_id"]
                curr_status = matched_lc["lifecycle_status"]

                if event.event_type == "BANK_DEBIT" and curr_status == "ORDERED":
                    # ORDERED -> DEBITED
                    proposed_updates.append({
                        "table": "transaction_lifecycles",
                        "lifecycle_id": lc_id,
                        "lifecycle_status": "DEBITED",
                        "settled_net_paise": event.amount_paise,
                        "upi_utr": event.upi_utr or matched_lc.get("upi_utr"),
                        "updated_at": event.event_timestamp,
                    })
                elif event.event_type == "ORDER_CANCELLED" and curr_status in ("DEBITED", "ORDERED"):
                    # DEBITED / ORDERED -> REFUND_PENDING
                    expected_refund = (event_dt + timedelta(days=5)).strftime("%Y-%m-%d")
                    # If refund is already overdue relative to reference_date
                    status = "REFUND_OVERDUE" if expected_refund < ref_dt.strftime("%Y-%m-%d") else "REFUND_PENDING"
                    proposed_updates.append({
                        "table": "transaction_lifecycles",
                        "lifecycle_id": lc_id,
                        "lifecycle_status": status,
                        "expected_refund_by": expected_refund,
                        "updated_at": event.event_timestamp,
                    })
                elif event.event_type == "BANK_REFUND" and curr_status in ("REFUND_PENDING", "REFUND_OVERDUE"):
                    # REFUND_PENDING / REFUND_OVERDUE -> REFUNDED
                    proposed_updates.append({
                        "table": "transaction_lifecycles",
                        "lifecycle_id": lc_id,
                        "lifecycle_status": "REFUNDED",
                        "settled_net_paise": 0,
                        "updated_at": event.event_timestamp,
                    })

                # Insert raw event linked to matched lifecycle
                proposed_inserts.append({
                    "table": "raw_events",
                    "event_id": f"rev_{uuid.uuid4().hex[:8]}",
                    "lifecycle_id": lc_id,
                    "source_type": event.source_type,
                    "external_ref_hash": event.external_ref_hash,
                    "event_type": event.event_type,
                    "amount_paise": event.amount_paise,
                    "event_timestamp": event.event_timestamp,
                    "raw_snippet": event.raw_snippet,
                    "confidence": event.confidence,
                })
            else:
                # Priority 3: No match -> Create new lifecycle
                new_lc_id = f"lc_{uuid.uuid4().hex[:8]}"
                initial_status: LifecycleStatus = "ORDERED" if event.event_type == "ORDER_PLACED" else "DEBITED"
                settled_net = event.amount_paise if initial_status == "DEBITED" else 0

                new_lc = {
                    "table": "transaction_lifecycles",
                    "lifecycle_id": new_lc_id,
                    "merchant_normalized": event.merchant_normalized or "Unknown Merchant",
                    "category": event.category or "shopping",
                    "expected_amount_paise": event.amount_paise,
                    "settled_net_paise": settled_net,
                    "lifecycle_status": initial_status,
                    "order_ref": event.order_ref,
                    "upi_utr": event.upi_utr,
                    "expected_refund_by": None,
                    "is_one_time_spike": 0,
                    "created_at": event.event_timestamp,
                    "updated_at": event.event_timestamp,
                }
                proposed_inserts.append(new_lc)
                existing_lifecycles.append(new_lc)

                proposed_inserts.append({
                    "table": "raw_events",
                    "event_id": f"rev_{uuid.uuid4().hex[:8]}",
                    "lifecycle_id": new_lc_id,
                    "source_type": event.source_type,
                    "external_ref_hash": event.external_ref_hash,
                    "event_type": event.event_type,
                    "amount_paise": event.amount_paise,
                    "event_timestamp": event.event_timestamp,
                    "raw_snippet": event.raw_snippet,
                    "confidence": event.confidence,
                })

        invariant_ok = self.check_funds_invariant(conn)
        conn.close()

        token = compute_approval_token(proposed_inserts, proposed_updates)
        summary = (
            f"Proposed batch: {len(proposed_inserts)} inserts, "
            f"{len(proposed_updates)} updates, "
            f"{len(ambiguous_items)} ambiguous items needing user clarification."
        )

        return CommitPreview(
            approval_token=token,
            proposed_inserts=proposed_inserts,
            proposed_updates=proposed_updates,
            ambiguous_items=ambiguous_items,
            invariant_check_passed=invariant_ok,
            summary=summary,
        )

    def commit_reconciliation_batch(
        self,
        approval_token: str,
        user_approved: bool,
        proposed_preview: CommitPreview,
        user_overrides: Optional[Dict[str, Any]] = None,
    ) -> CommitResult:
        """
        Two-phase guarded write:
        1. Validates user_approved is True.
        2. Validates approval_token matches the proposed diff.
        3. Applies any user overrides (e.g. classification of personal UPI handles).
        4. Verifies Conservation of Funds invariant.
        5. Commits atomically with audit_log recording for rollback capability.
        """
        if not user_approved:
            return CommitResult(
                tx_id="",
                committed=False,
                reason="Guarded commit rejected: user_approved must be True.",
            )

        expected_token = compute_approval_token(
            proposed_preview.proposed_inserts,
            proposed_preview.proposed_updates,
        )
        if approval_token != expected_token:
            return CommitResult(
                tx_id="",
                committed=False,
                reason="Security violation: approval_token does not match proposed diff payload.",
            )

        conn = self._get_connection()
        cursor = conn.cursor()
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"

        try:
            conn.execute("BEGIN TRANSACTION")

            # Capture snapshot before mutation for undo
            before_lifecycles = [dict(r) for r in cursor.execute("SELECT * FROM transaction_lifecycles").fetchall()]
            before_events = [dict(r) for r in cursor.execute("SELECT * FROM raw_events").fetchall()]
            before_state = {"lifecycles": before_lifecycles, "events": before_events}

            # Apply updates
            for update in proposed_preview.proposed_updates:
                if update.get("table") == "transaction_lifecycles":
                    lc_id = update["lifecycle_id"]
                    fields = []
                    vals = []
                    for k, v in update.items():
                        if k not in ("table", "lifecycle_id"):
                            fields.append(f"{k} = ?")
                            vals.append(v)
                    vals.append(lc_id)
                    cursor.execute(
                        f"UPDATE transaction_lifecycles SET {', '.join(fields)} WHERE lifecycle_id = ?",
                        vals,
                    )

            # Apply inserts
            for ins in proposed_preview.proposed_inserts:
                if ins.get("table") == "transaction_lifecycles":
                    cursor.execute(
                        """
                        INSERT INTO transaction_lifecycles (
                            lifecycle_id, merchant_normalized, category, expected_amount_paise,
                            settled_net_paise, lifecycle_status, order_ref, upi_utr,
                            expected_refund_by, is_one_time_spike, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ins["lifecycle_id"],
                            ins["merchant_normalized"],
                            ins["category"],
                            ins["expected_amount_paise"],
                            ins["settled_net_paise"],
                            ins["lifecycle_status"],
                            ins.get("order_ref"),
                            ins.get("upi_utr"),
                            ins.get("expected_refund_by"),
                            ins.get("is_one_time_spike", 0),
                            ins["created_at"],
                            ins["updated_at"],
                        ),
                    )
                elif ins.get("table") == "raw_events":
                    cursor.execute(
                        """
                        INSERT INTO raw_events (
                            event_id, lifecycle_id, source_type, external_ref_hash,
                            event_type, amount_paise, event_timestamp, raw_snippet, confidence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ins["event_id"],
                            ins["lifecycle_id"],
                            ins["source_type"],
                            ins["external_ref_hash"],
                            ins["event_type"],
                            ins["amount_paise"],
                            ins["event_timestamp"],
                            ins["raw_snippet"],
                            ins["confidence"],
                        ),
                    )

            # Handle user overrides for ambiguous items
            if user_overrides and proposed_preview.ambiguous_items:
                for amb in proposed_preview.ambiguous_items:
                    override = user_overrides.get(amb.external_ref_hash)
                    if override:
                        category = override.get("category", "shopping")
                        merchant = override.get("merchant", amb.merchant_normalized or "Personal UPI")
                        new_lc_id = f"lc_{uuid.uuid4().hex[:8]}"
                        cursor.execute(
                            """
                            INSERT INTO transaction_lifecycles (
                                lifecycle_id, merchant_normalized, category, expected_amount_paise,
                                settled_net_paise, lifecycle_status, order_ref, upi_utr,
                                expected_refund_by, is_one_time_spike, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                new_lc_id,
                                merchant,
                                category,
                                amb.amount_paise,
                                amb.amount_paise,
                                "DEBITED",
                                amb.order_ref,
                                amb.upi_utr,
                                None,
                                0,
                                amb.event_timestamp,
                                amb.event_timestamp,
                            ),
                        )
                        cursor.execute(
                            """
                            INSERT INTO raw_events (
                                event_id, lifecycle_id, source_type, external_ref_hash,
                                event_type, amount_paise, event_timestamp, raw_snippet, confidence
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"rev_{uuid.uuid4().hex[:8]}",
                                new_lc_id,
                                amb.source_type,
                                amb.external_ref_hash,
                                amb.event_type,
                                amb.amount_paise,
                                amb.event_timestamp,
                                amb.raw_snippet,
                                1.0,
                            ),
                        )

            # Check invariant before commit
            if not self.check_funds_invariant(conn):
                conn.rollback()
                return CommitResult(
                    tx_id=tx_id,
                    committed=False,
                    reason="Conservation of Funds invariant violated. Transaction rolled back.",
                )

            # Record audit log
            after_lifecycles = [dict(r) for r in cursor.execute("SELECT * FROM transaction_lifecycles").fetchall()]
            after_events = [dict(r) for r in cursor.execute("SELECT * FROM raw_events").fetchall()]
            after_state = {"lifecycles": after_lifecycles, "events": after_events}

            cursor.execute(
                """
                INSERT INTO audit_log (
                    tx_id, approval_token, action_type, before_state_json,
                    after_state_json, approved_by_user, reverted, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    approval_token,
                    "reconciliation_batch",
                    json.dumps(before_state),
                    json.dumps(after_state),
                    1,
                    0,
                    datetime.now().isoformat(),
                ),
            )

            conn.commit()
            return CommitResult(tx_id=tx_id, committed=True)

        except Exception as e:
            conn.rollback()
            return CommitResult(tx_id=tx_id, committed=False, reason=f"Database error: {str(e)}")
        finally:
            conn.close()

    def undo_last_commit(self) -> RollbackResult:
        """
        Mistake recovery: Restores the previous database state from the latest un-reverted audit_log entry.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT tx_id, before_state_json
            FROM audit_log
            WHERE reverted = 0
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return RollbackResult(
                tx_id="",
                reverted=False,
                message="No un-reverted transactions found in audit log.",
            )

        tx_id = row["tx_id"]
        before_state = json.loads(row["before_state_json"])

        try:
            conn.execute("BEGIN TRANSACTION")

            # Wipe and restore lifecycles
            cursor.execute("DELETE FROM transaction_lifecycles")
            for lc in before_state.get("lifecycles", []):
                cursor.execute(
                    """
                    INSERT INTO transaction_lifecycles (
                        lifecycle_id, merchant_normalized, category, expected_amount_paise,
                        settled_net_paise, lifecycle_status, order_ref, upi_utr,
                        expected_refund_by, is_one_time_spike, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lc["lifecycle_id"],
                        lc["merchant_normalized"],
                        lc["category"],
                        lc["expected_amount_paise"],
                        lc["settled_net_paise"],
                        lc["lifecycle_status"],
                        lc["order_ref"],
                        lc["upi_utr"],
                        lc["expected_refund_by"],
                        lc["is_one_time_spike"],
                        lc["created_at"],
                        lc["updated_at"],
                    ),
                )

            # Wipe and restore raw_events
            cursor.execute("DELETE FROM raw_events")
            for rev in before_state.get("events", []):
                cursor.execute(
                    """
                    INSERT INTO raw_events (
                        event_id, lifecycle_id, source_type, external_ref_hash,
                        event_type, amount_paise, event_timestamp, raw_snippet, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev["event_id"],
                        rev["lifecycle_id"],
                        rev["source_type"],
                        rev["external_ref_hash"],
                        rev["event_type"],
                        rev["amount_paise"],
                        rev["event_timestamp"],
                        rev["raw_snippet"],
                        rev["confidence"],
                    ),
                )

            cursor.execute("UPDATE audit_log SET reverted = 1 WHERE tx_id = ?", (tx_id,))
            conn.commit()

            return RollbackResult(
                tx_id=tx_id,
                reverted=True,
                restored_state={"lifecycles_count": len(before_state.get("lifecycles", []))},
                message=f"Successfully rolled back transaction {tx_id}.",
            )
        except Exception as e:
            conn.rollback()
            return RollbackResult(
                tx_id=tx_id,
                reverted=False,
                message=f"Rollback failed: {str(e)}",
            )
        finally:
            conn.close()
