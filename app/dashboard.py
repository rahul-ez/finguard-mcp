"""FinGuard's local financial workspace and guarded reconciliation dashboard."""

import csv
import io
import os
import sqlite3
import sys
from calendar import monthrange
from datetime import date
from html import escape
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.consequences import ConsequenceEngine
from engine.dashboard import CATEGORY_LABELS, dashboard_snapshot, load_workspace
from engine.models import paise_to_rupees
from engine.reconciler import ReconciliationEngine
from servers.finance_sources_mcp import draft_action_package, extract_and_normalize_events

DB_PATH = Path(os.environ.get("FINGUARD_DB_PATH", PROJECT_ROOT / "db" / "finguard.db")).resolve()
VIEWS = ["Overview", "Purchase simulator", "Transactions", "Review & approve"]
STATUS_LABELS = {
    "ORDERED": "Ordered", "DEBITED": "Debited", "CANCELLED": "Cancelled",
    "REFUND_PENDING": "Refund pending", "REFUND_OVERDUE": "Refund overdue", "REFUNDED": "Refunded",
}
ALERT_LABELS = {
    "STACKED_OUTFLOWS": ("Cash flow", "Upcoming payments exceed your cash", "danger", "Shortfall"),
    "REFUND_OVERDUE": ("Refund follow-up", "A refund has missed its deadline", "danger", "Locked cash"),
    "VELOCITY": ("Spending pace", "Your spending is putting savings at risk", "warning", "Projected overrun"),
    "PRICE_HIKE": ("Subscription", "A subscription price has increased", "warning", "Annual increase"),
}


def html(content):
    st.markdown(content, unsafe_allow_html=True)


def money(amount):
    return paise_to_rupees(amount)


def balance_description(basis, as_of):
    if basis == "budget_estimate":
        return f"Budget / ledger estimate as of {as_of}; no statement for this cycle"
    if basis == "statement_plus_ledger":
        return f"Statement from {as_of}, adjusted for later posted ledger activity"
    return f"Statement balance as of {as_of}"


def go_to(view):
    st.session_state["workspace_view"] = view


def clear_approval():
    st.session_state["approve_changes"] = False


def clear_preview():
    st.session_state.pop("current_preview", None)
    st.session_state.pop("simulation", None)
    clear_approval()


def call_source(tool, *args):
    # FastMCP 2 wraps decorated functions; 3+ preserves the original callable.
    return (tool if callable(tool) else tool.fn)(*args)


def save_draft_edit(key):
    # Widget keys are removed when their view disappears; keep the edited copy separately.
    st.session_state[f"draft_content_{key}"] = st.session_state[f"draft_text_{key}"]


def metric_card(label, value, detail, tone="neutral", symbol="↗"):
    html(f'<article class="metric-card {tone}"><div class="metric-label">{escape(label)}'
         f'<span class="metric-symbol" aria-hidden="true">{symbol}</span></div>'
         f'<div class="metric-value">{escape(value)}</div>'
         f'<div class="metric-detail">{escape(detail)}</div></article>')


def section_heading(title, caption=None):
    st.subheader(title)
    if caption:
        st.caption(caption)


def draft_panel(action, record_id, key):
    if st.button("Prepare refund follow-up" if action == "refund_escalation" else "Prepare cancellation draft",
                 key=f"prepare_{key}"):
        try:
            st.session_state[f"draft_{key}"] = call_source(draft_action_package, action, record_id, "draft-only")
        except (sqlite3.Error, OSError, ValueError) as exc:
            st.error(f"Could not prepare the draft: {exc}")
    draft = st.session_state.get(f"draft_{key}")
    if draft:
        st.caption("Draft only. Verify the recipient and transaction details before using it.")
        st.text_area("Review draft", value=st.session_state.get(f"draft_content_{key}", draft.draft_text),
                     height=240, key=f"draft_text_{key}", on_change=save_draft_edit, args=(key,))
        st.download_button("Download draft", st.session_state[f"draft_text_{key}"],
                           file_name=f"finguard-{action}-{record_id}.txt", mime="text/plain", key=f"download_{key}")


def render_alerts(snapshot):
    report = snapshot["report"]
    section_heading("Needs your attention", "Ranked by financial impact. Open an alert to see the calculation.")
    if not report.alerts:
        st.success("No active consequences detected for this reference date.")
    for index, alert in enumerate(report.alerts):
        label, title, tone, impact_label = ALERT_LABELS[alert.kind]
        with st.container(border=True, key=f"alert_{index}"):
            html(f'<div class="alert-top"><span class="status-label {tone}">{label}</span>'
                 f'<span class="impact">{escape(money(alert.impact_paise))}'
                 f'<small>{impact_label}</small></span></div>'
                 f'<h3 class="alert-title">{title}</h3>')
            with st.expander("Details & calculation"):
                st.write(alert.message)
                if alert.kind == "REFUND_OVERDUE" and alert.lifecycle_id:
                    draft_panel("refund_escalation", alert.lifecycle_id, alert.lifecycle_id)
                elif alert.kind == "PRICE_HIKE":
                    for sub in snapshot["subscriptions"]:
                        if sub["merchant_normalized"] in alert.message:
                            draft_panel("subscription_cancel", sub["sub_id"], sub["sub_id"])


def render_budgets(snapshot):
    with st.container(border=True, key="budget_panel"):
        section_heading("Spending pace", "Actual spending against your monthly allocation")
        html('<div class="chart-legend"><span><i class="legend-actual"></i>Spent</span>'
             '<span><i class="legend-projected"></i>Month-end projection</span></div>')
        if not snapshot["categories"]:
            st.info("No category budgets have been set for this cycle.")
        for category in snapshot["categories"]:
            tone = "warning" if category["overrun_paise"] else "neutral"
            html(f'<div class="budget-row"><div class="budget-label"><strong>{escape(category["label"])}</strong>'
                 f'<span>{money(category["settled_paise"])} <small>/ {money(category["budget_paise"])}</small></span></div>'
                 f'<div class="budget-track" role="img" aria-label="{escape(category["label"])}: '
                 f'{money(category["settled_paise"])} spent, {money(category["projected_paise"])} projected">'
                 f'<div class="budget-projected {tone}" style="width:{category["projected_percent"]}%"></div>'
                 f'<div class="budget-actual" style="width:{category["spent_percent"]}%"></div></div>'
                 f'<div class="budget-foot {tone}">Projected {money(category["projected_paise"])}'
                 f'{" · Over budget" if category["overrun_paise"] else " · Within budget"}</div></div>')
        st.caption("Recurring budget projection includes pending orders. Tagged one-time expenses are excluded from this pace chart but remain reflected in the cash balance.")


def render_commitments(snapshot):
    with st.container(border=True, key="commitments_panel"):
        section_heading("Bills still to cover", "Unpaid calendar bills; budget allocations are used when no schedule exists")
        if not snapshot["commitments"]:
            st.caption("No more scheduled fixed allocations this month.")
        for item in snapshot["commitments"]:
            due = date.fromisoformat(item["due_date"])
            html(f'<div class="commitment"><div class="date-tile"><strong>{due.day:02}</strong>'
                 f'<span>{due:%b}</span></div><div class="commitment-name">{escape(item["name"])}'
                 f'<small>{"Overdue" if item["overdue"] else "Scheduled bill"}</small></div><strong>{money(item["amount_paise"])}</strong></div>')
        st.caption(f'{snapshot["pending_count"]} pending orders · {money(snapshot["report"].pending_committed_paise)} committed separately')


def render_overview(snapshot):
    report, profile = snapshot["report"], snapshot["profile"]
    columns = st.columns(4)
    with columns[0]:
        metric_card("Safe to spend / day", money(report.safe_to_spend_today_paise), "After bills, orders & your savings goal", "accent", "↗")
    with columns[1]:
        metric_card("Savings at budget pace", money(report.projected_savings_paise), "Category forecast · excludes one-time spikes", symbol="◷")
    with columns[2]:
        metric_card("Liquid cash", money(report.remaining_liquid_cash_paise),
                    balance_description(snapshot["balance_basis"], snapshot["balance_as_of"]), symbol="≋")
    with columns[3]:
        metric_card("Pending commitments", money(report.pending_committed_paise), f'{snapshot["pending_count"]} orders awaiting settlement', symbol="⇄")
    with st.container(key="savings_strip"):
        left, right = st.columns([1.6, 1])
        with left:
            html('<div class="eyebrow">MONTH-END OUTLOOK</div>')
            if snapshot["savings_gap_paise"]:
                html(f'<div class="outlook-title">Your savings need a little room.</div>'
                     f'<div class="muted">Current spending pace puts you {money(snapshot["savings_gap_paise"])} below your savings goal.</div>')
            else:
                html('<div class="outlook-title">Your savings are on track.</div>'
                     '<div class="muted">Your current spending pace protects your savings goal.</div>')
        with right:
            html(f'<div class="goal-label"><span>Projected savings / goal</span><strong>{snapshot["savings_percent"]}%</strong></div>'
                 f'<div class="goal-track" role="progressbar" aria-label="Projected savings as a percentage of goal" '
                 f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{snapshot["savings_percent"]}">'
                 f'<div style="width:{snapshot["savings_percent"]}%"></div></div>'
                 f'<div class="goal-amount">{money(report.projected_savings_paise)} of {money(profile["savings_target_paise"])}</div>')
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        render_alerts(snapshot)
        st.button("Test a purchase →", on_click=go_to, args=("Purchase simulator",), type="primary")
    with right:
        render_budgets(snapshot)
        render_commitments(snapshot)


def render_simulator(engine, snapshot, ref_date):
    left, right = st.columns([1, 1.4], gap="large")
    with left, st.container(border=True):
        section_heading("Before you buy", "See how a purchase changes your room to spend.")
        with st.form("purchase_form"):
            amount = st.number_input("Purchase amount (₹)", min_value=1, max_value=1000000, value=3500, step=100,
                                     help="Enter whole rupees. The engine calculates in integer paise.")
            category = st.selectbox("Category", list(CATEGORY_LABELS)[:4], format_func=CATEGORY_LABELS.get)
            merchant = st.text_input("Merchant", value="Zara India", max_chars=100)
            submitted = st.form_submit_button("Check financial impact", type="primary", use_container_width=True)
        st.caption("A simulation does not add a purchase to your ledger.")
    if submitted:
        if not merchant.strip():
            st.error("Enter a merchant to run the simulation.")
        else:
            try:
                result = engine.simulate_purchase_decision(amount_paise=amount * 100, category=category,
                                                          merchant=merchant.strip(), reference_date=ref_date.isoformat(),
                                                          month_key=ref_date.strftime("%Y-%m"))
                st.session_state["simulation"] = (ref_date.isoformat(), result)
            except (ValueError, sqlite3.Error, OSError) as exc:
                st.error(f"Could not calculate this purchase: {exc}")
    with right:
        saved = st.session_state.get("simulation")
        if not saved or saved[0] != ref_date.isoformat() or not hasattr(saved[1], "discretionary_before_paise"):
            with st.container(border=True, key="simulation_empty"):
                html('<div class="empty-symbol" aria-hidden="true">↗</div>')
                st.subheader("Make the next purchase an informed one.")
                st.write("Enter an amount and merchant to compare your daily allowance, category budget, and upcoming bills.")
                st.caption(f'Current daily allowance: {money(snapshot["report"].safe_to_spend_today_paise)}')
        else:
            result = saved[1]
            with st.container(border=True):
                section_heading(f"Your purchase at {result.merchant}", f'{money(result.amount_paise)} · {CATEGORY_LABELS[result.category]}')
                if result.bill_shortfall_before_paise:
                    st.error(f'Bills are already underfunded. This purchase adds {money(result.additional_bill_shortfall_paise)} to the existing shortfall.')
                elif result.collides_with_fixed_bill:
                    st.error(f'This purchase creates a {money(result.bill_shortfall_after_paise)} shortfall for bills and pending orders.')
                elif result.discretionary_after_paise < 0:
                    st.warning("Bills remain covered, but your savings goal and emergency buffer would not be fully protected.")
                else:
                    st.success("This purchase fits within the cash available after bills, orders, savings, and your buffer.")
                a, b = st.columns(2)
                if result.discretionary_before_paise < 0:
                    a.metric("Cash remainder before", money(result.discretionary_before_paise))
                    b.metric("Cash remainder after", money(result.discretionary_after_paise))
                    st.caption("Negative amounts show how much cash is missing after reserving bills, orders, your savings goal, and emergency buffer.")
                else:
                    a.metric("Daily allowance before", money(result.safe_to_spend_before_paise))
                    b.metric("Daily allowance after", money(result.safe_to_spend_after_paise))
                st.metric("Added category overrun from this purchase", money(result.additional_category_overrun_paise))
                for note in result.notes:
                    st.write(note)
                with st.expander("How this is calculated", expanded=True):
                    st.dataframe([
                        {"Cash calculation": "Recorded cash", "Before": money(result.liquid_cash_before_paise), "After": money(result.liquid_cash_after_paise)},
                        {"Cash calculation": "Reserve for unpaid bills", "Before": money(result.upcoming_fixed_paise), "After": money(result.upcoming_fixed_paise)},
                        {"Cash calculation": "Reserve for pending orders", "Before": money(result.pending_committed_paise), "After": money(result.pending_committed_paise)},
                        {"Cash calculation": "Reserve for savings goal", "Before": money(result.savings_reserved_paise), "After": money(result.savings_reserved_paise)},
                        {"Cash calculation": "Reserve for emergency buffer", "Before": money(result.emergency_reserved_paise), "After": money(result.emergency_reserved_paise)},
                        {"Cash calculation": "Cash left after reserves", "Before": money(result.discretionary_before_paise), "After": money(result.discretionary_after_paise)},
                        {"Cash calculation": "Bills / orders shortfall only", "Before": money(result.bill_shortfall_before_paise), "After": money(result.bill_shortfall_after_paise)},
                    ], hide_index=True, use_container_width=True)
                    st.caption(f'Category budget {money(result.category_budget_paise)} · Already spent {money(result.category_spent_paise)} · Pending {money(result.category_pending_paise)} · Existing overrun {money(result.category_overrun_before_paise)} · Total overrun after purchase {money(result.category_overrun_paise)}')
                st.caption(f'{balance_description(result.balance_basis, result.balance_as_of)}. Paid expenses are already reflected in this balance and are not deducted again.')


def lifecycle_table(rows):
    return [{"Merchant": row["merchant_normalized"], "Category": CATEGORY_LABELS.get(row["category"], row["category"]),
             "Status": STATUS_LABELS.get(row["lifecycle_status"], row["lifecycle_status"]),
             "Expected amount": money(row["expected_amount_paise"]), "Settled net": money(row["settled_net_paise"]),
             "Refund due": row["expected_refund_by"] or "—", "Order reference": row["order_ref"] or "—",
             "Bank UTR": row["upi_utr"] or "—", "Lifecycle ID": row["lifecycle_id"]} for row in rows]


def render_transactions(snapshot):
    a, b, c = st.columns([2, 1, 1])
    query = a.text_input("Search transactions", placeholder="Merchant, order reference, bank UTR…")
    statuses = b.multiselect("Status", list(STATUS_LABELS), format_func=STATUS_LABELS.get)
    categories = c.multiselect("Category", sorted({r["category"] for r in snapshot["lifecycles"]}),
                               format_func=lambda value: CATEGORY_LABELS.get(value, value))
    rows = [row for row in snapshot["lifecycles"]
            if (not statuses or row["lifecycle_status"] in statuses)
            and (not categories or row["category"] in categories)
            and (not query.strip() or any(query.strip().casefold() in str(row.get(field) or "").casefold()
                 for field in ("merchant_normalized", "order_ref", "upi_utr", "lifecycle_id")))]
    st.caption(f'{len(rows)} of {len(snapshot["lifecycles"])} lifecycles · Orders and settled payments remain distinct')
    if not rows:
        st.info("No transactions match these filters. Try a different merchant, status, or category.")
        return
    table = lifecycle_table(rows)
    st.dataframe(table, hide_index=True, use_container_width=True)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(table[0]))
    writer.writeheader()
    writer.writerows({key: "'" + value if value.startswith(("=", "+", "-", "@")) else value
                      for key, value in row.items()} for row in table)
    st.download_button("Export filtered transactions", output.getvalue().encode("utf-8-sig"),
                       "finguard-transactions.csv", "text/csv")
    with st.container(border=True):
        section_heading("Inspect a lifecycle", "Trace a purchase across its order, payment, and refund records.")
        selected = st.selectbox("Transaction", [row["lifecycle_id"] for row in rows],
                                format_func=lambda value: next(f'{r["merchant_normalized"]} · {money(r["expected_amount_paise"])} · {value}' for r in rows if r["lifecycle_id"] == value))
        record = next(row for row in rows if row["lifecycle_id"] == selected)
        st.write(f'**Current status:** {STATUS_LABELS.get(record["lifecycle_status"], record["lifecycle_status"])}')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            events = [dict(row) for row in conn.execute(
                "SELECT source_type, event_type, amount_paise, event_timestamp FROM raw_events WHERE lifecycle_id = ? AND substr(event_timestamp, 1, 10) <= ? ORDER BY event_timestamp",
                (selected, snapshot["report"].reference_date))]
        if events:
            st.dataframe([{"Date": event["event_timestamp"], "Source": event["source_type"].replace("_", " "),
                           "Event": event["event_type"].replace("_", " ").title(), "Amount": money(event["amount_paise"])}
                          for event in events], hide_index=True, use_container_width=True)
        else:
            st.caption("This seeded lifecycle has no linked raw events yet. Preview the local sources to reconcile them.")
        if record["lifecycle_status"] in ("REFUND_PENDING", "REFUND_OVERDUE"):
            draft_panel("refund_escalation", selected, f"transaction_{selected}")


def render_review(reconciler, snapshot, ref_date):
    with st.container(border=True, key="review_intro"):
        section_heading("Bring your sources together", "Preview local email and bank / UPI fixtures, resolve uncertain matches, then approve the changes.")
        source = st.selectbox("Source to review", ["all", "emails", "statements"],
                              format_func=lambda value: {"all": "All local transaction sources", "emails": "Email receipts", "statements": "Bank / UPI statements"}[value])
        if st.button("Preview reconciliation", type="primary"):
            clear_preview()
            try:
                with st.spinner("Matching events to your ledger…"):
                    events = call_source(extract_and_normalize_events, source)
                    st.session_state["current_preview"] = reconciler.propose_reconciliation_batch(events, ref_date.isoformat())
            except (sqlite3.Error, OSError, ValueError) as exc:
                st.error(f"Could not preview these sources: {exc}")
    preview = st.session_state.get("current_preview")
    if preview:
        with st.container(border=True):
            section_heading("Review the proposed changes", preview.summary)
            a, b, c = st.columns(3)
            a.metric("Inserts", len(preview.proposed_inserts))
            b.metric("Updates", len(preview.proposed_updates))
            c.metric("Need classification", len(preview.ambiguous_items))
            if preview.invariant_check_passed:
                st.success("Budget balance check passed.")
            else:
                st.error("Budget allocations do not balance with income. Commit is blocked.")
            changes = []
            existing = {row["lifecycle_id"]: row for row in snapshot["lifecycles"]}
            for action, mutations in [("Insert", preview.proposed_inserts), ("Update", preview.proposed_updates)]:
                for item in mutations:
                    lifecycle = existing.get(item.get("lifecycle_id"), {})
                    changes.append({
                        "Change": action,
                        "Record": "Source event" if item.get("table") == "raw_events" else "Transaction",
                        "Merchant / reference": item.get("merchant_normalized") or lifecycle.get("merchant_normalized") or item.get("lifecycle_id", "—"),
                        "Amount": money(item.get("amount_paise", item.get("expected_amount_paise", lifecycle.get("expected_amount_paise", 0)))),
                        "Proposed state": item.get("lifecycle_status", item.get("event_type", "—")).replace("_", " ").title(),
                    })
            if changes:
                st.dataframe(changes, hide_index=True, use_container_width=True)
            with st.expander("Inspect the complete change set"):
                st.write("**Inserts**")
                st.json(preview.proposed_inserts)
                st.write("**Updates**")
                st.json(preview.proposed_updates)
            overrides = {}
            for item in preview.ambiguous_items:
                st.warning(f'Classify {money(item.amount_paise)} · {item.raw_snippet}')
                category = st.selectbox("Choose a category", [None, *list(CATEGORY_LABELS)[:4]],
                                       format_func=lambda value: CATEGORY_LABELS.get(value, "Select a category…"),
                                       key=f"classify_{item.external_ref_hash}", on_change=clear_approval)
                merchant = st.text_input("Merchant or payee", value=item.merchant_normalized or "Personal transfer",
                                         key=f"merchant_{item.external_ref_hash}", on_change=clear_approval)
                if category and merchant.strip():
                    overrides[item.external_ref_hash] = {"category": category, "merchant": merchant.strip()}
            with st.expander("Approval fingerprint"):
                st.code(preview.approval_token, language=None)
                st.caption("The fingerprint binds the proposed inserts and updates. Review classifications above before approving.")
            ready = (preview.invariant_check_passed and len(overrides) == len(preview.ambiguous_items)
                     and bool(preview.proposed_inserts or preview.proposed_updates or preview.ambiguous_items))
            approved = st.checkbox("I reviewed the complete changes and classifications and approve this ledger update.",
                                   key="approve_changes", disabled=not ready)
            if not ready:
                st.caption("Resolve all classifications and the budget balance check to proceed. An empty proposal cannot be committed.")
            if st.button("Approve & commit", type="primary", disabled=not (approved and ready)):
                result = reconciler.commit_reconciliation_batch(preview.approval_token, approved, preview, overrides)
                if result.committed:
                    st.session_state.pop("current_preview", None)
                    st.session_state.pop("simulation", None)
                    st.session_state["flash"] = ("success", f"Ledger updated. Transaction {result.tx_id} is recorded in the audit log.")
                    st.rerun()
                else:
                    st.error(f"Commit failed: {result.reason}")
    else:
        st.info("Your ledger stays unchanged until you preview and explicitly approve a proposal.")
    with st.expander("Commit history & recovery"):
        if snapshot["audit"]:
            st.dataframe(snapshot["audit"], hide_index=True, use_container_width=True)
        else:
            st.caption("No commits have been recorded yet.")
        can_undo = any(not item["reverted"] for item in snapshot["audit"])
        confirm = st.checkbox("Restore the ledger to its state before the latest active commit.", disabled=not can_undo)
        if st.button("Undo latest commit", disabled=not (can_undo and confirm)):
            result = reconciler.undo_last_commit()
            if result.reverted:
                st.session_state.pop("current_preview", None)
                st.session_state.pop("simulation", None)
                st.session_state["flash"] = ("success", result.message)
                st.rerun()
            else:
                st.warning(result.message)


def main():
    st.set_page_config(page_title="FinGuard · Financial workspace", page_icon="◈", layout="wide")
    html(f'<style>{(Path(__file__).with_name("styles.css")).read_text(encoding="utf-8")}</style>')
    try:
        cycles = load_workspace(DB_PATH)
    except (sqlite3.Error, OSError):
        st.title("Set up your financial workspace")
        st.info("The FinGuard ledger is missing or unavailable. Initialize the demo database, then reload this page.")
        st.code("python db/seed_demo.py", language="bash")
        st.stop()
    if not cycles:
        st.title("Your workspace is ready for a budget")
        st.info("No budget cycles are available. Run python db/seed_demo.py to initialize the demo profile.")
        st.stop()
    with st.sidebar:
        html('<div class="brand"><div class="brand-mark" aria-hidden="true">F<span>▰</span></div>'
             '<div>FinGuard<small>FINANCIAL WORKSPACE</small></div></div>')
        html('<div class="workspace-label">PERSONAL FINANCE <span>LOCAL DEMO</span></div>')
        view = st.radio("Workspace navigation", VIEWS, key="workspace_view", label_visibility="collapsed")
        st.divider()
        cycle = st.selectbox("Budget cycle", cycles, key="budget_cycle", on_change=clear_preview,
                             format_func=lambda value: date.fromisoformat(value + "-01").strftime("%B %Y"))
        start = date.fromisoformat(cycle + "-01")
        end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        ref_date = st.date_input("Forecast reference date", value=date(start.year, start.month, 18),
                                 min_value=start, max_value=end, key=f"reference_{cycle}", on_change=clear_preview)
        st.caption("Forecasts use bank balances and transaction events available through the selected date. Seeded records without event history use their recorded transaction dates.")
    engine = ConsequenceEngine(str(DB_PATH))
    try:
        snapshot = dashboard_snapshot(engine, cycle, ref_date.isoformat())
    except (sqlite3.Error, ValueError, OSError) as exc:
        st.error(f"Could not load your financial workspace: {exc}")
        st.stop()
    with st.sidebar:
        html(f'<div class="cycle-label"><strong>Day {ref_date.day} of {snapshot["days_in_month"]}</strong><span>{snapshot["days_left"]} days left</span></div>'
             f'<div class="cycle-track"><div style="width:{snapshot["cycle_percent"]}%"></div></div>')
        st.divider()
        html('<div class="eyebrow">YOUR MONTHLY PLAN</div>')
        for label, amount in [("Take-home pay", snapshot["profile"]["monthly_income_paise"]),
                              ("Fixed commitments", snapshot["fixed_paise"]), ("Variable budget", snapshot["variable_paise"]),
                              ("Savings goal", snapshot["profile"]["savings_target_paise"]),
                              ("Emergency buffer", snapshot["profile"]["emergency_buffer_paise"])]:
            html(f'<div class="plan-row"><span>{label}</span><strong>{money(amount)}</strong></div>')
        html('<div class="local-note"><span class="status-dot"></span> Local SQLite ledger<small>Fixture data · Human-approved updates</small></div>')
    html(f'<div class="topline"><span>Workspace <b>/</b> {escape(view)}</span>'
         f'<span class="workspace-badge"><span class="status-dot"></span> Demo workspace</span></div>')
    descriptions = {
        "Overview": ("Your financial overview", "Understand what today's spending means for the rest of your month."),
        "Purchase simulator": ("Can I buy this?", "Check the effect on your savings and bills before you commit."),
        "Transactions": ("Every transaction, connected", "Follow orders, payments, and refunds through one reconciled lifecycle."),
        "Review & approve": ("You're in control of your ledger", "Review every proposed change before it becomes part of your financial picture."),
    }
    title, description = descriptions[view]
    left, right = st.columns([3, 1])
    with left:
        st.title(title)
        st.markdown(f'<p class="page-description">{description}</p>', unsafe_allow_html=True)
    with right:
        html(f'<div class="reference-label">FORECAST AS OF<strong>{ref_date:%d %B %Y}</strong>'
             f'<span>{len(snapshot["report"].alerts)} active alerts</span></div>')
    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    if view == "Overview":
        render_overview(snapshot)
    elif view == "Purchase simulator":
        render_simulator(engine, snapshot, ref_date)
    elif view == "Transactions":
        render_transactions(snapshot)
    else:
        render_review(ReconciliationEngine(str(DB_PATH)), snapshot, ref_date)
    html('<footer class="workspace-footer"><strong>FinGuard MCP</strong><span>Calculated in integer paise · Local demo data · Drafts stay under your control</span></footer>')


if __name__ == "__main__":
    main()
