"""
FinGuard MCP Server 2: finguard_ledger_mcp
Role: Stateful accounting ledger, consequence engine, and what-if simulation.
Enforces:
  - Two-phase guarded writes (propose -> approval_token -> commit).
  - Conservation of Funds invariant.
  - Closed-form mathematical projections (no LLM arithmetic).
  - Read-only ledger resources.
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP
from engine.models import (
    NormalizedEvent,
    CommitPreview,
    CommitResult,
    RollbackResult,
    ConsequenceReport,
    SimulationResult,
    paise_to_rupees,
)
from engine.reconciler import ReconciliationEngine
from engine.consequences import ConsequenceEngine

mcp = FastMCP("finguard_ledger")

DB_PATH = PROJECT_ROOT / "db" / "finguard.db"
reconciler = ReconciliationEngine(str(DB_PATH))
consequence_engine = ConsequenceEngine(str(DB_PATH))

# In-memory storage for active proposal preview awaiting commit
_ACTIVE_PROPOSALS: Dict[str, CommitPreview] = {}


# =============================================================================
# RESOURCES (Read-Only)
# =============================================================================

@mcp.resource("ledger://budget/summary/{month_key}")
def get_budget_summary(month_key: str = "2026-11") -> str:
    """Returns budget profile, category allocations, settled net spend, and safe-to-spend today."""
    ctx = consequence_engine.get_budget_context(month_key)
    report = consequence_engine.run_consequence_engine(month_key)
    payload = {
        "budget_profile": ctx["profile"],
        "allocations": ctx["allocations"],
        "safe_to_spend_today_paise": report.safe_to_spend_today_paise,
        "safe_to_spend_today_formatted": paise_to_rupees(report.safe_to_spend_today_paise),
        "projected_savings_paise": report.projected_savings_paise,
        "projected_savings_formatted": paise_to_rupees(report.projected_savings_paise),
        "settled_variable_paise": report.settled_variable_paise,
        "pending_committed_paise": report.pending_committed_paise,
    }
    return json.dumps(payload, indent=2)


@mcp.resource("ledger://lifecycles/active")
def get_active_lifecycles() -> str:
    """Returns all active transaction lifecycles grouped by status."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transaction_lifecycles ORDER BY created_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    grouped: Dict[str, List[Dict[str, Any]]] = {
        "ORDERED": [],
        "DEBITED": [],
        "REFUND_PENDING": [],
        "REFUND_OVERDUE": [],
        "REFUNDED": [],
    }
    for r in rows:
        st = r["lifecycle_status"]
        if st in grouped:
            grouped[st].append(r)

    return json.dumps(grouped, indent=2)


@mcp.resource("ledger://consequences/alerts")
def get_consequence_alerts() -> str:
    """Returns active deterministic financial alerts ranked descending by financial impact."""
    report = consequence_engine.run_consequence_engine("2026-11", "2026-11-18")
    return json.dumps([a.model_dump() for a in report.alerts], indent=2)


# =============================================================================
# TOOLS
# =============================================================================

@mcp.tool()
def propose_reconciliation_batch(
    events: List[NormalizedEvent],
    reference_date: str = "2026-11-18",
) -> CommitPreview:
    """
    Dry run: Matches incoming events against active lifecycles.
    Produces exact proposed DB diff, flags ambiguous items, and generates a cryptographic approval_token.
    Writes NOTHING to the ledger.
    """
    preview = reconciler.propose_reconciliation_batch(events, reference_date)
    _ACTIVE_PROPOSALS[preview.approval_token] = preview
    return preview


@mcp.tool()
def commit_reconciliation_batch(
    approval_token: str,
    user_approved: bool,
    user_overrides: Optional[Dict[str, Any]] = None,
) -> CommitResult:
    """
    Guarded write: Commits the proposed diff to the SQLite ledger.
    Fails immediately if user_approved is False or if approval_token does not match.
    Enforces Conservation-of-Funds invariant and logs audit state for undo capability.
    """
    preview = _ACTIVE_PROPOSALS.get(approval_token)
    if not preview:
        # Reconstruct preview from empty if direct call
        preview = reconciler.propose_reconciliation_batch([])
        if preview.approval_token != approval_token:
            return CommitResult(
                tx_id="",
                committed=False,
                reason="Invalid or expired approval_token. Must run propose_reconciliation_batch first.",
            )

    result = reconciler.commit_reconciliation_batch(
        approval_token=approval_token,
        user_approved=user_approved,
        proposed_preview=preview,
        user_overrides=user_overrides,
    )
    if result.committed and approval_token in _ACTIVE_PROPOSALS:
        del _ACTIVE_PROPOSALS[approval_token]

    return result


@mcp.tool()
def run_consequence_engine(
    month_key: str = "2026-11",
    reference_date: str = "2026-11-18",
) -> ConsequenceReport:
    """
    Deterministic consequence analysis:
    Runs all 4 detectors (Velocity & Savings Overrun, Subscription Hike, Stacked Outflows, Overdue Refund)
    and computes safe-to-spend daily allowance and projected month-end savings.
    """
    return consequence_engine.run_consequence_engine(month_key, reference_date)


@mcp.tool()
def simulate_purchase_decision(
    amount_paise: int,
    category: str,
    merchant: str,
    reference_date: str = "2026-11-18",
    month_key: str = "2026-11",
) -> SimulationResult:
    """
    Stateless what-if decision simulator:
    Evaluates whether spending amount_paise today will cause category budget overruns,
    reduce daily safe-to-spend allowance, or collide with upcoming fixed rent and bills.
    """
    return consequence_engine.simulate_purchase_decision(
        amount_paise=amount_paise,
        category=category,
        merchant=merchant,
        reference_date=reference_date,
        month_key=month_key,
    )


@mcp.tool()
def undo_last_commit() -> RollbackResult:
    """
    Mistake recovery: Reverts the most recent un-reverted ledger mutation recorded in audit_log.
    Restores the exact previous database state atomically.
    """
    return reconciler.undo_last_commit()


if __name__ == "__main__":
    mcp.run()
