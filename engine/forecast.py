"""Read-only, dated inputs shared by consequence reports and purchase simulations."""

import csv
import json
import sqlite3
from calendar import monthrange
from contextlib import closing
from datetime import date
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def lifecycle_snapshot(conn, reference_date):
    """Replay linked events through the selected day without mutating the ledger.

    Seed rows without event history use their creation date. Their final state is
    only used when its update date has been reached; otherwise use the known debit
    or order. This is a forecast from recorded evidence, not a database rollback.
    """
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM transaction_lifecycles WHERE substr(created_at, 1, 10) <= ? ORDER BY created_at, lifecycle_id",
        (reference_date,),
    )]
    histories = {}
    for event in conn.execute(
        "SELECT * FROM raw_events WHERE substr(event_timestamp, 1, 10) <= ? ORDER BY event_timestamp, event_id",
        (reference_date,),
    ):
        histories.setdefault(event["lifecycle_id"], []).append(dict(event))
    for row in rows:
        events = histories.get(row["lifecycle_id"], [])
        if events:
            status, settled = "ORDERED", 0
            for event in events:
                kind = event["event_type"]
                if kind == "BANK_DEBIT" and status != "REFUNDED":
                    settled = event["amount_paise"]
                    if status not in ("CANCELLED", "REFUND_PENDING", "REFUND_OVERDUE", "REFUNDED"):
                        status = "DEBITED"
                    elif status == "CANCELLED":
                        status = "REFUND_PENDING"
                elif kind == "ORDER_CANCELLED":
                    status = "REFUND_PENDING" if settled else "CANCELLED"
                elif kind == "BANK_REFUND":
                    status, settled = "REFUNDED", 0
            row["lifecycle_status"], row["settled_net_paise"] = status, settled
        elif row["updated_at"][:10] > reference_date:
            row["lifecycle_status"] = "DEBITED" if row["settled_net_paise"] else "ORDERED"
        if (row["lifecycle_status"] == "REFUND_PENDING" and row["expected_refund_by"]
                and row["expected_refund_by"] < reference_date):
            row["lifecycle_status"] = "REFUND_OVERDUE"
    return rows


def subscriptions_snapshot(conn, reference_date):
    subscriptions = [dict(row) for row in conn.execute("SELECT * FROM subscriptions ORDER BY next_billing_date")]
    notices = [dict(row) for row in conn.execute("SELECT * FROM raw_events WHERE event_type = 'SUBSCRIPTION_RENEWAL'")]
    for subscription in subscriptions:
        matching = [notice for notice in notices
                    if subscription["merchant_normalized"].casefold() in notice["raw_snippet"].casefold()]
        if matching and not any(notice["event_timestamp"][:10] <= reference_date for notice in matching):
            subscription["current_amount_paise"] = subscription["previous_amount_paise"]
            subscription["status"] = "active"
    return subscriptions


def dated_balance(engine, month_key, reference_date, profile, monthly_rows, rows):
    """Statement balance already includes debits: never subtract those debits again."""
    if engine.statement_path.exists():
        with engine.statement_path.open(encoding="utf-8-sig", newline="") as file:
            statement_rows = [row for row in csv.DictReader(file)
                    if row["date"][:7] == month_key and row["date"] <= reference_date]
        if statement_rows:
            # Preserve CSV ordering among multiple transactions on the same day.
            latest = sorted(statement_rows, key=lambda row: row["date"])[-1]
            with closing(engine._get_connection()) as conn:
                prior = lifecycle_snapshot(conn, latest["date"])
            # Roll forward only activity AFTER the statement cutoff. This also
            # captures a later refund of an older debit, and never counts orders.
            net_activity = sum(row["settled_net_paise"] for row in rows) - sum(row["settled_net_paise"] for row in prior)
            basis = "statement_plus_ledger" if net_activity else "bank_statement"
            return int(latest["balance_paise"]) - net_activity, latest["date"], basis
    # A different cycle without a statement has only a budget estimate available.
    paid = sum(row["settled_net_paise"] for row in monthly_rows)
    return profile["monthly_income_paise"] - paid, reference_date, "budget_estimate"


def fixed_commitments(calendar_path, month_key, reference_date, allocations, rows, subscriptions):
    """Use dated calendar bills; fall back to allocations when no schedule exists."""
    scheduled = []
    if calendar_path.exists():
        with calendar_path.open(encoding="utf-8") as file:
            scheduled = [bill for bill in json.load(file)
                         if bill["due_date"][:7] == month_key and bill.get("is_fixed")]
    commitments = []
    for bill in scheduled:
        # A paid fixture is evidence of payment from its due date onward.
        if bill.get("status") == "paid" and bill["due_date"] <= reference_date:
            continue
        payee = bill.get("payee", "").casefold()
        amount = bill["amount_paise"]
        for subscription in subscriptions:
            if subscription["merchant_normalized"].casefold() == payee:
                amount = subscription["current_amount_paise"]
        paid = any(row["lifecycle_status"] == "DEBITED" and row["settled_net_paise"] == amount
                   and row["category"] == bill["category"] and row["merchant_normalized"]
                   and (row["merchant_normalized"].casefold() in payee
                        or row["merchant_normalized"].casefold() in bill["name"].casefold()) for row in rows)
        if not paid:
            commitments.append({"name": bill["name"], "category": bill["category"],
                                "due_date": bill["due_date"], "amount_paise": amount,
                                "overdue": bill["due_date"] < reference_date})
    covered = {bill["category"] for bill in scheduled}
    ref = date.fromisoformat(reference_date)
    for allocation in allocations:
        if not allocation["is_fixed"] or allocation["category"] in covered:
            continue
        paid = sum(row["settled_net_paise"] for row in rows
                   if row["category"] == allocation["category"] and row["lifecycle_status"] == "DEBITED")
        remaining = max(0, allocation["allocated_paise"] - paid)
        due_day = min(allocation["due_day"] or monthrange(ref.year, ref.month)[1], monthrange(ref.year, ref.month)[1])
        due = date(ref.year, ref.month, due_day).isoformat()
        if remaining:
            commitments.append({"name": allocation["category"].replace("_", " ").title(),
                                "category": allocation["category"], "due_date": due,
                                "amount_paise": remaining, "overdue": due < reference_date})
    return sorted(commitments, key=lambda bill: bill["due_date"])


def forecast_context(engine, month_key, reference_date):
    ref = date.fromisoformat(reference_date[:10])
    reference_date = ref.isoformat()
    if ref.strftime("%Y-%m") != month_key:
        raise ValueError("The reference date must belong to the selected budget cycle.")
    context = engine.get_budget_context(month_key)
    with closing(engine._get_connection()) as conn:
        rows = lifecycle_snapshot(conn, reference_date)
        subscriptions = subscriptions_snapshot(conn, reference_date)
    monthly = [row for row in rows if row["created_at"][:7] == month_key]
    days_remaining = max(1, monthrange(ref.year, ref.month)[1] - ref.day)
    pending_rows = [row for row in rows if row["lifecycle_status"] == "ORDERED"]
    categories = []
    for allocation in context["allocations"]:
        if allocation["is_fixed"]:
            continue
        category = allocation["category"]
        debited = [row for row in monthly if row["category"] == category and row["lifecycle_status"] == "DEBITED"]
        actual = sum(row["settled_net_paise"] for row in debited)
        recurring = sum(row["settled_net_paise"] for row in debited if not row["is_one_time_spike"])
        pending = sum(row["expected_amount_paise"] for row in pending_rows if row["category"] == category)
        # Recurring budget pace excludes tagged spikes. Actual cash still includes them.
        projected = recurring + recurring // ref.day * days_remaining + pending
        budget = allocation["allocated_paise"]
        categories.append({"category": category, "settled_paise": recurring, "actual_paise": actual,
                           "one_time_paise": actual - recurring,
                           "recurring_paise": recurring, "pending_paise": pending,
                           "projected_paise": projected, "budget_paise": budget,
                           "overrun_paise": max(0, projected - budget),
                           "spent_percent": min(100, recurring * 100 // max(1, budget)),
                           "projected_percent": min(100, projected * 100 // max(1, budget))})
    balance, balance_date, basis = dated_balance(engine, month_key, reference_date, context["profile"], monthly, rows)
    bills = fixed_commitments(engine.calendar_path, month_key, reference_date, context["allocations"], monthly, subscriptions)
    pending = sum(row["expected_amount_paise"] for row in pending_rows)
    fixed = sum(bill["amount_paise"] for bill in bills)
    profile = context["profile"]
    disposable = balance - fixed - pending - profile["savings_target_paise"] - profile["emergency_buffer_paise"]
    return {**context, "lifecycles": rows, "monthly_lifecycles": monthly,
            "subscriptions": subscriptions, "categories": categories, "commitments": bills,
            "days_remaining": days_remaining, "balance_paise": balance, "balance_as_of": balance_date,
            "balance_basis": basis, "pending_paise": pending, "fixed_paise": fixed,
            "discretionary_paise": disposable,
            "bill_shortfall_paise": max(0, fixed + pending - balance)}
