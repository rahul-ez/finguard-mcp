"""
FinGuard MCP - Interactive Host UI & Approval Dashboard
Provides:
  1. Consequence & Trajectory Dashboard (Velocity, Alerts, Savings Erosion)
  2. The 4 Demo Scenarios (Price Hike, Stacked Outflows, Velocity Overrun, Missing Refund)
  3. Interactive "Can I Buy This?" What-If Simulator
  4. Unified Transaction Lifecycle & Refund SLA Tracker
  5. Two-Phase Guarded Human Approval Gate (Preview -> Approval Token -> Commit)
  6. Mistake Recovery (Undo Last Commit)
"""

import sys
import json
import sqlite3
from pathlib import Path
import streamlit as st

# Setup python path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.models import (
    NormalizedEvent,
    paise_to_rupees,
    rupees_to_paise,
)
from engine.reconciler import ReconciliationEngine
from engine.consequences import ConsequenceEngine
from servers.finance_sources_mcp import extract_and_normalize_events, draft_action_package

DB_PATH = PROJECT_ROOT / "db" / "finguard.db"
reconciler = ReconciliationEngine(str(DB_PATH))
consequence_engine = ConsequenceEngine(str(DB_PATH))

# Page configuration
st.set_page_config(
    page_title="FinGuard MCP - Financial Consequence & Reconciliation Agent",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar Context
st.sidebar.title("🛡️ FinGuard MCP")
st.sidebar.markdown("**Make MCP Matter: Less Magic, More Utility**")
st.sidebar.markdown("---")
st.sidebar.markdown("### 👤 User Persona Profile")
st.sidebar.write("**Take-Home:** ₹60,000 / month")
st.sidebar.write("**Target Savings:** ₹12,000 / month")
st.sidebar.write("**Fixed Commitments:** ₹16,000 / month")
st.sidebar.write("**Variable Budget:** ₹30,000 / month")
st.sidebar.write("**Emergency Buffer:** ₹2,000 / month")
st.sidebar.markdown("---")

ref_date = st.sidebar.date_input("Simulation Reference Date", value=None)
ref_date_str = str(ref_date) if ref_date else "2026-11-18"
month_key = ref_date_str[:7] if ref_date_str else "2026-11"

st.sidebar.info(f"**Billing Cycle:** {month_key}\n\n**Days Elapsed:** 18 | **Days Left:** 12")

# Main Header (Theme-adaptive)
st.title("🛡️ FinGuard Financial Consequence Agent")
st.markdown(
    "Expense trackers tell you what already happened. "
    "**FinGuard tells you what it will do to your savings and bills *before* you run out of cash.**"
)
st.write("")

# Run deterministic consequence engine
report = consequence_engine.run_consequence_engine(month_key, ref_date_str)
budget_ctx = consequence_engine.get_budget_context(month_key)
profile = budget_ctx["profile"]

# Top KPI Metric Row (Native Streamlit Metrics: 100% Theme-Adaptive)
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
with kpi1:
    st.metric(
        label="Monthly Take-Home",
        value=paise_to_rupees(profile["monthly_income_paise"]),
    )
with kpi2:
    st.metric(
        label="Safe-to-Spend Today",
        value=f"{paise_to_rupees(report.safe_to_spend_today_paise)}/d",
        help="Deterministic daily discretionary allowance after accounting for upcoming fixed bills, pending orders, and savings target.",
    )
with kpi3:
    savings_delta = report.projected_savings_paise - profile["savings_target_paise"]
    st.metric(
        label="Projected Savings",
        value=paise_to_rupees(report.projected_savings_paise),
        delta=f"{paise_to_rupees(savings_delta)} vs Goal" if savings_delta != 0 else "On Target",
        delta_color="normal" if savings_delta >= 0 else "inverse",
        help=f"Target savings goal is {paise_to_rupees(profile['savings_target_paise'])}.",
    )
with kpi4:
    st.metric(
        label="Liquid Cash Available",
        value=paise_to_rupees(report.remaining_liquid_cash_paise),
        help="Current liquid balance in bank account before payday.",
    )
with kpi5:
    st.metric(
        label="Active Consequence Alerts",
        value=f"{len(report.alerts)} active",
        delta="Action Required" if report.alerts else "All Clear",
        delta_color="inverse" if report.alerts else "normal",
    )

st.write("")
st.markdown("---")

# Navigation Tabs
tab_alerts, tab_simulate, tab_lifecycles, tab_gate = st.tabs([
    "🚨 1. Consequence Radar & Demo Scenarios",
    "🤔 2. 'Can I Buy This?' (What-If Simulator)",
    "🔄 3. Unified Transaction Lifecycles",
    "🛡️ 4. Guarded Ingestion & Approval Gate",
])

# =============================================================================
# TAB 1: Consequence Radar & Showcase Scenarios
# =============================================================================
with tab_alerts:
    st.subheader("Deterministic Consequence Warnings (Ranked by Financial Impact)")
    st.caption("Calculated purely via closed-form equations in Python — zero LLM hallucinations or arithmetic.")

    for alert in report.alerts:
        if alert.kind == "STACKED_OUTFLOWS":
            st.error(
                f"### ⚠️ Scenario B: Stacked Outflows & Rent Collision (Shortfall: {paise_to_rupees(alert.impact_paise)})\n\n"
                f"{alert.message}",
                icon="⚠️",
            )
        elif alert.kind == "REFUND_OVERDUE":
            st.error(
                f"### ⏳ Scenario D: Missing Refund SLA Breach (Locked Cash: {paise_to_rupees(alert.impact_paise)})\n\n"
                f"{alert.message}",
                icon="⏳",
            )
            col_draft1, col_draft2 = st.columns([1, 4])
            with col_draft1:
                if st.button("Generate Dispute Draft", key=f"btn_draft_{alert.lifecycle_id}"):
                    draft = draft_action_package("refund_escalation", alert.lifecycle_id or "lc_amz_2499_01", "token")
                    st.text_area("Dispute Escalation Email Draft (Ready to Send)", draft.draft_text, height=240)
        elif alert.kind == "VELOCITY":
            st.warning(
                f"### 📈 Scenario C: Category Velocity Overrun & Savings Erosion (Impact: {paise_to_rupees(alert.impact_paise)})\n\n"
                f"{alert.message}",
                icon="📈",
            )
        elif alert.kind == "PRICE_HIKE":
            st.info(
                f"### 💳 Scenario A: Subscription Price Hike (Annual Impact: {paise_to_rupees(alert.impact_paise)})\n\n"
                f"{alert.message}",
                icon="💳",
            )
            if st.button("Draft Cancellation Notice", key="btn_sub_cancel"):
                sub_draft = draft_action_package("subscription_cancel", "StreamFlix", "token")
                st.text_area("Subscription Cancellation Notice Draft", sub_draft.draft_text, height=200)

# =============================================================================
# TAB 2: "Can I Buy This?" What-If Simulator
# =============================================================================
with tab_simulate:
    st.subheader("Purchase Decision Simulator: Test Financial Consequences")
    st.caption("Check how an impulsive purchase impacts daily safe-to-spend allowance and upcoming fixed bills.")

    sim_col1, sim_col2, sim_col3 = st.columns(3)
    with sim_col1:
        purchase_amt_rupees = st.number_input("Purchase Amount (₹)", min_value=100.0, max_value=50000.0, value=3500.0, step=250.0)
    with sim_col2:
        purchase_cat = st.selectbox("Category", ["shopping", "entertainment", "food_groceries", "transport"])
    with sim_col3:
        purchase_merchant = st.text_input("Merchant", value="Zara India")

    purchase_paise = rupees_to_paise(purchase_amt_rupees)
    sim_result = consequence_engine.simulate_purchase_decision(
        amount_paise=purchase_paise,
        category=purchase_cat,
        merchant=purchase_merchant,
        reference_date=ref_date_str,
    )

    st.markdown("---")
    res1, res2, res3 = st.columns(3)
    with res1:
        st.metric(
            label="Daily Safe-to-Spend",
            value=f"{paise_to_rupees(sim_result.safe_to_spend_after_paise)}/day",
            delta=f"-{paise_to_rupees(sim_result.safe_to_spend_before_paise - sim_result.safe_to_spend_after_paise)}",
            delta_color="inverse",
        )
    with res2:
        st.metric(
            label=f"'{purchase_cat.title()}' Budget Overrun",
            value=paise_to_rupees(sim_result.category_overrun_paise),
            delta="Overrun" if sim_result.category_overrun_paise > 0 else "Within Budget",
            delta_color="inverse" if sim_result.category_overrun_paise > 0 else "normal",
        )
    with res3:
        if sim_result.collides_with_fixed_bill:
            st.error(f"🚨 Rent Collision: Yes! Collides with {sim_result.colliding_bill_name}")
        else:
            st.success("✅ Fixed Bill Collision: No rent collision detected.")

    st.write("#### Simulation Takeaways:")
    for note in sim_result.notes:
        st.info(note)

# =============================================================================
# TAB 3: Transaction Lifecycles
# =============================================================================
with tab_lifecycles:
    st.subheader("Unified Transaction Lifecycles (Multi-Source Reconciliation)")
    st.caption("Reconciles fragmented signals into full lifecycles: ORDERED -> DEBITED -> CANCELLED -> REFUND_PENDING -> REFUND_OVERDUE.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transaction_lifecycles ORDER BY updated_at DESC")
    lcs = [dict(r) for r in cursor.fetchall()]
    conn.close()

    formatted_lcs = []
    for lc in lcs:
        st_val = lc["lifecycle_status"]
        formatted_lcs.append({
            "Lifecycle ID": lc["lifecycle_id"],
            "Merchant": lc["merchant_normalized"],
            "Category": lc["category"].title(),
            "Expected Amount": paise_to_rupees(lc["expected_amount_paise"]),
            "Settled Net": paise_to_rupees(lc["settled_net_paise"]),
            "Lifecycle Status": st_val,
            "Order Reference": lc["order_ref"] or "-",
            "Bank UPI UTR": lc["upi_utr"] or "-",
            "Refund SLA Date": lc["expected_refund_by"] or "-",
        })

    st.dataframe(formatted_lcs, width="stretch")

# =============================================================================
# TAB 4: Human-in-the-Loop Guarded Ingestion & Approval Gate
# =============================================================================
with tab_gate:
    st.subheader("Two-Phase Guarded Ingestion Gate (Preview -> Approval Token -> Commit)")
    st.caption("No tool or LLM writes to the ledger without cryptographic diff approval.")

    gate_col1, gate_col2 = st.columns([1, 1])
    with gate_col1:
        st.write("#### 1. Ingest & Dry-Run Propose")
        if st.button("Run Dry-Run Proposal"):
            raw_events = extract_and_normalize_events("all")
            preview = reconciler.propose_reconciliation_batch(raw_events, ref_date_str)
            st.session_state["current_preview"] = preview
            st.session_state["raw_events"] = raw_events
            st.success(f"Generated Dry-Run Proposal! Approval Token: `{preview.approval_token[:16]}...`")

    with gate_col2:
        st.write("#### Mistake Recovery")
        if st.button("Undo Last Commit (Rollback)"):
            rollback = reconciler.undo_last_commit()
            if rollback.reverted:
                st.success(rollback.message)
            else:
                st.warning(rollback.message)

    if "current_preview" in st.session_state:
        prev = st.session_state["current_preview"]
        st.markdown("---")
        st.write("#### 2. Proposed Mutation Diff")
        st.write(f"**Cryptographic Approval Token:** `{prev.approval_token}`")
        st.write(f"**Invariant Passed:** {'✅ True' if prev.invariant_check_passed else '❌ False'}")

        p_col1, p_col2 = st.columns(2)
        with p_col1:
            st.write(f"**Proposed Inserts ({len(prev.proposed_inserts)})**")
            st.json(prev.proposed_inserts[:3] if prev.proposed_inserts else [])
        with p_col2:
            st.write(f"**Proposed Updates ({len(prev.proposed_updates)})**")
            st.json(prev.proposed_updates[:3] if prev.proposed_updates else [])

        # Ambiguous items
        user_overrides = {}
        if prev.ambiguous_items:
            st.warning(f"Found {len(prev.ambiguous_items)} ambiguous item(s) needing user classification:")
            for amb in prev.ambiguous_items:
                st.write(f"**Payee:** `{amb.raw_snippet}` | **Amount:** {paise_to_rupees(amb.amount_paise)}")
                chosen_cat = st.selectbox(f"Classify category for {amb.raw_snippet}:", ["food_groceries", "entertainment", "shopping", "transport"], key=f"ov_{amb.external_ref_hash}")
                user_overrides[amb.external_ref_hash] = {"category": chosen_cat, "merchant": "Friend Transfer"}

        # Human Approval Checkbox
        st.markdown("---")
        st.write("#### 3. Human Approval Gate")
        user_approved = st.checkbox("I have inspected the diff and approve committing these mutations to finguard.db")

        if st.button("Execute Guarded Commit", disabled=not user_approved):
            commit_res = reconciler.commit_reconciliation_batch(
                approval_token=prev.approval_token,
                user_approved=user_approved,
                proposed_preview=prev,
                user_overrides=user_overrides,
            )
            if commit_res.committed:
                st.success(f"Transaction `{commit_res.tx_id}` successfully committed to ledger!")
                del st.session_state["current_preview"]
            else:
                st.error(f"Commit Failed: {commit_res.reason}")
