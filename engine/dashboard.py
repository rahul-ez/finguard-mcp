"""Read models for the dashboard. All financial arithmetic stays in the engine."""

import sqlite3
from calendar import monthrange
from datetime import date
from engine.forecast import forecast_context


CATEGORY_LABELS = {
    "food_groceries": "Food & groceries",
    "transport": "Transport",
    "entertainment": "Entertainment",
    "shopping": "Shopping",
    "rent_utilities": "Rent & utilities",
    "subscriptions": "Subscriptions",
}


def load_workspace(db_path):
    """Read the available cycles without creating or reseeding a database."""
    with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
        return [row[0] for row in conn.execute(
            "SELECT month_key FROM budget_profiles ORDER BY month_key DESC"
        )]


def dashboard_snapshot(engine, month_key, reference_date):
    """Aggregate dated forecast inputs without changing the underlying ledger."""
    report = engine.run_consequence_engine(month_key, reference_date)
    context = forecast_context(engine, month_key, reference_date)
    with sqlite3.connect(engine.db_path) as conn:
        conn.row_factory = sqlite3.Row
        audit = [dict(row) for row in conn.execute(
            "SELECT tx_id, action_type, reverted, timestamp FROM audit_log ORDER BY timestamp DESC"
        )]
    ref = date.fromisoformat(reference_date)
    days_in_month = monthrange(ref.year, ref.month)[1]
    days_left = days_in_month - ref.day
    lifecycles = sorted(context["lifecycles"], key=lambda row: row["updated_at"], reverse=True)
    categories = [{**item, "label": CATEGORY_LABELS.get(item["category"], item["category"])}
                  for item in context["categories"]]
    goal = context["profile"]["savings_target_paise"]
    return {
        "report": report, **context, "lifecycles": lifecycles,
        "subscriptions": context["subscriptions"], "audit": audit, "categories": categories,
        "commitments": context["commitments"],
        "days_left": days_left, "days_in_month": days_in_month,
        "cycle_percent": ref.day * 100 // days_in_month,
        "savings_percent": min(100, report.projected_savings_paise * 100 // max(1, goal)),
        "savings_gap_paise": max(0, goal - report.projected_savings_paise),
        "fixed_paise": sum(a["allocated_paise"] for a in context["allocations"] if a["is_fixed"]),
        "variable_paise": sum(a["allocated_paise"] for a in context["allocations"] if not a["is_fixed"]),
        "pending_count": sum(row["lifecycle_status"] == "ORDERED" for row in lifecycles),
        "refund_paise": sum(row["settled_net_paise"] for row in lifecycles
                            if row["lifecycle_status"] in ("REFUND_PENDING", "REFUND_OVERDUE")),
    }
