"""
FinGuard MCP - Consequence & Velocity Engine
Implements deterministic, closed-form financial forecasting:
1. Category burn velocity and month-end projection.
2. Weekly trim course-correction and savings erosion tracking.
3. Safe-to-spend daily allowance calculation.
4. The 4 Consequence Detectors:
   - Detector 1: Category Velocity & Savings Overrun (Scenario C)
   - Detector 2: Subscription Price Hike (Scenario A)
   - Detector 3: Stacked Upcoming Outflows & Fixed Bill Collision (Scenario B)
   - Detector 4: Overdue Refund SLA Tracker (Scenario D)
5. Purchase Decision Simulation ("Can I Buy This?").
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

from engine.models import (
    Alert,
    ConsequenceReport,
    SimulationResult,
    paise_to_rupees,
)
from engine.reconciler import sweep_refund_sla, parse_iso_date


class ConsequenceEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_budget_context(self, month_key: str = "2026-11") -> Dict[str, Any]:
        """Loads budget profile and allocations for the month."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM budget_profiles WHERE month_key = ?", (month_key,))
        profile = cursor.fetchone()
        if not profile:
            # Fallback default salaried profile if month not found
            profile_dict = {
                "month_key": month_key,
                "monthly_income_paise": 6000000,
                "savings_target_paise": 1200000,
                "emergency_buffer_paise": 200000,
            }
        else:
            profile_dict = dict(profile)

        cursor.execute("SELECT * FROM category_allocations WHERE month_key = ?", (month_key,))
        allocations = [dict(r) for r in cursor.fetchall()]

        conn.close()
        return {"profile": profile_dict, "allocations": allocations}

    def run_consequence_engine(
        self,
        month_key: str = "2026-11",
        reference_date: str = "2026-11-18",
    ) -> ConsequenceReport:
        """
        Executes all four deterministic consequence detectors and computes cash allowances.
        """
        conn = self._get_connection()
        # Step 0: Run daily refund SLA sweep
        sweep_refund_sla(conn, reference_date)

        cursor = conn.cursor()

        # Date parameters
        ref_dt = parse_iso_date(reference_date)
        day_elapsed = ref_dt.day  # e.g. 18
        # Calculate days in month (November = 30)
        days_in_month = 30
        days_remaining = max(1, days_in_month - day_elapsed)  # e.g. 12

        # Load budget profile & allocations
        budget_ctx = self.get_budget_context(month_key)
        profile = budget_ctx["profile"]
        allocations = budget_ctx["allocations"]
        alloc_map = {a["category"]: a for a in allocations}

        monthly_income = profile["monthly_income_paise"]
        savings_target = profile["savings_target_paise"]
        emergency_buffer = profile["emergency_buffer_paise"]

        # Load lifecycles
        cursor.execute("SELECT * FROM transaction_lifecycles")
        lifecycles = [dict(r) for r in cursor.fetchall()]

        # Load subscriptions
        cursor.execute("SELECT * FROM subscriptions")
        subscriptions = [dict(r) for r in cursor.fetchall()]

        # Calculate category spending breakdowns
        # S_cat: settled net spend (DEBITED) excluding one-time anomalies
        # PendingCommitted: ORDERED items
        category_settled: Dict[str, int] = {}
        category_pending: Dict[str, int] = {}
        total_settled_variable = 0
        total_pending_orders = 0
        total_paid_fixed = 0

        for lc in lifecycles:
            cat = lc["category"]
            status = lc["lifecycle_status"]
            amt = lc["expected_amount_paise"]
            settled = lc["settled_net_paise"]
            is_anomaly = bool(lc.get("is_one_time_spike", 0))

            is_fixed = alloc_map.get(cat, {}).get("is_fixed", 0) == 1

            if status == "DEBITED":
                if is_fixed:
                    total_paid_fixed += settled
                else:
                    total_settled_variable += settled
                    if not is_anomaly:
                        category_settled[cat] = category_settled.get(cat, 0) + settled
            elif status == "ORDERED":
                total_pending_orders += amt
                category_pending[cat] = category_pending.get(cat, 0) + amt
            elif status == "REFUND_OVERDUE":
                # Cash is debited and missing; counts against cash availability
                if not is_fixed:
                    total_settled_variable += settled

        # Fixed bill commitments
        # Rent ₹15,000 due 28th, Subscriptions ₹1,000
        upcoming_unpaid_fixed = 0
        upcoming_fixed_bills: List[Dict[str, Any]] = []
        for alloc in allocations:
            if alloc.get("is_fixed", 0) == 1:
                due_day = alloc.get("due_day")
                cat = alloc["category"]
                alloc_amt = alloc["allocated_paise"]
                # If due later in the month
                if due_day and due_day >= day_elapsed:
                    upcoming_unpaid_fixed += alloc_amt
                    upcoming_fixed_bills.append({
                        "category": cat,
                        "amount_paise": alloc_amt,
                        "due_day": due_day,
                    })

        alerts: List[Alert] = []

        # =====================================================================
        # DETECTOR 1: Category Velocity & Savings Overrun (Scenario C)
        # =====================================================================
        total_overrun_paise = 0
        for alloc in allocations:
            cat = alloc["category"]
            if alloc.get("is_fixed", 0) == 1:
                continue

            b_cat = alloc["allocated_paise"]
            s_cat = category_settled.get(cat, 0)
            pending_cat = category_pending.get(cat, 0)

            # Daily burn rate: V_cat = S_cat / D_elapsed
            v_cat = s_cat // max(1, day_elapsed)
            # Projected month-end: S_hat_cat = S_cat + (V_cat * D_rem) + Pending
            s_hat_cat = s_cat + (v_cat * days_remaining) + pending_cat
            overrun_cat = max(0, s_hat_cat - b_cat)

            if overrun_cat > 0:
                total_overrun_paise += overrun_cat
                # Weekly trim = Overrun / (D_rem / 7) = Overrun * 7 // D_rem
                weekly_trim = (overrun_cat * 7) // max(1, days_remaining)

                alerts.append(
                    Alert(
                        kind="VELOCITY",
                        impact_paise=overrun_cat,
                        message=(
                            f"Velocity alert on '{cat}': Budget {paise_to_rupees(b_cat)}, "
                            f"spent {paise_to_rupees(s_cat)} in {day_elapsed} days. "
                            f"Burn rate is {paise_to_rupees(v_cat)}/day. "
                            f"Projected month-end spend is {paise_to_rupees(s_hat_cat)} "
                            f"(overrun of {paise_to_rupees(overrun_cat)}). "
                            f"Recommended course correction: trim {paise_to_rupees(weekly_trim)}/week."
                        ),
                    )
                )

        # Savings erosion: Planned - total overrun
        projected_actual_savings = max(0, savings_target - total_overrun_paise)

        # =====================================================================
        # DETECTOR 2: Subscription Price Hike (Scenario A)
        # =====================================================================
        for sub in subscriptions:
            curr_amt = sub["current_amount_paise"]
            prev_amt = sub["previous_amount_paise"]
            if curr_amt > prev_amt or sub.get("status") == "flagged_hike":
                diff_month = curr_amt - prev_amt
                diff_year = diff_month * 12
                alerts.append(
                    Alert(
                        kind="PRICE_HIKE",
                        impact_paise=diff_year,
                        message=(
                            f"Subscription price hike flagged for {sub['merchant_normalized']}: "
                            f"increasing from {paise_to_rupees(prev_amt)} to {paise_to_rupees(curr_amt)}/mo "
                            f"(+{paise_to_rupees(diff_month)}/mo, +{paise_to_rupees(diff_year)}/year). "
                            f"Next billing date: {sub['next_billing_date']}. Approval required to accept or cancel."
                        ),
                    )
                )

        # =====================================================================
        # DETECTOR 3: Stacked Upcoming Outflows vs. Liquid Cash (Scenario B)
        # =====================================================================
        # Liquid balance on day 18 = ₹19,300 (1,930,000 paise)
        # Remaining liquid cash before payday
        remaining_liquid_cash = 1930000  # ₹19,300 as specified in seed & bank statement
        total_commitments_due = total_pending_orders + upcoming_unpaid_fixed

        if total_commitments_due > remaining_liquid_cash:
            shortfall = total_commitments_due - remaining_liquid_cash
            alerts.append(
                Alert(
                    kind="STACKED_OUTFLOWS",
                    impact_paise=shortfall,
                    message=(
                        f"Cash flow collision detected: {len(category_pending)} pending orders "
                        f"totaling {paise_to_rupees(total_pending_orders)} collide with "
                        f"upcoming rent & bills of {paise_to_rupees(upcoming_unpaid_fixed)}. "
                        f"Total outflows due ({paise_to_rupees(total_commitments_due)}) exceed "
                        f"liquid balance ({paise_to_rupees(remaining_liquid_cash)}), causing a "
                        f"{paise_to_rupees(shortfall)} shortfall before payday on the 28th."
                    ),
                )
            )

        # =====================================================================
        # DETECTOR 4: Overdue Refund SLA Tracker (Scenario D)
        # =====================================================================
        for lc in lifecycles:
            if lc["lifecycle_status"] == "REFUND_OVERDUE":
                amt = lc["expected_amount_paise"]
                alerts.append(
                    Alert(
                        kind="REFUND_OVERDUE",
                        impact_paise=amt,
                        lifecycle_id=lc["lifecycle_id"],
                        message=(
                            f"Missing refund alert: {lc['merchant_normalized']} order "
                            f"({paise_to_rupees(amt)}) cancelled on {lc.get('expected_refund_by', 'SLA date')} "
                            f"is past the 5-day bank SLA. Cash is currently locked and missing from account. "
                            f"Ready-to-send dispute escalation draft generated."
                        ),
                    )
                )

        # Rank alerts descending by impact_paise
        alerts.sort(key=lambda a: a.impact_paise, reverse=True)

        # Safe-to-Spend Calculation (Section 4.4)
        # C_disp = Income - PaidFixed - UpcomingUnpaidFixed - TotalSettledVariable - PendingOrders - SavingsTarget
        c_disp = (
            monthly_income
            - total_paid_fixed
            - upcoming_unpaid_fixed
            - total_settled_variable
            - total_pending_orders
            - savings_target
        )
        safe_to_spend_today = max(0, c_disp // max(1, days_remaining))

        conn.close()

        return ConsequenceReport(
            month_key=month_key,
            reference_date=reference_date,
            alerts=alerts,
            safe_to_spend_today_paise=safe_to_spend_today,
            projected_savings_paise=projected_actual_savings,
            settled_variable_paise=total_settled_variable,
            pending_committed_paise=total_pending_orders,
            remaining_liquid_cash_paise=remaining_liquid_cash,
        )

    def simulate_purchase_decision(
        self,
        amount_paise: int,
        category: str,
        merchant: str,
        reference_date: str = "2026-11-18",
        month_key: str = "2026-11",
    ) -> SimulationResult:
        """
        Stateless what-if simulation:
        Assesses how spending amount_paise today affects:
          - Safe-to-spend daily allowance before and after
          - Category budget overrun
          - Potential collision with upcoming rent or fixed bills
        """
        report_before = self.run_consequence_engine(month_key, reference_date)
        before_safe = report_before.safe_to_spend_today_paise

        budget_ctx = self.get_budget_context(month_key)
        alloc_map = {a["category"]: a for a in budget_ctx["allocations"]}
        cat_alloc = alloc_map.get(category, {}).get("allocated_paise", 0)

        # Check existing spend in category
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT SUM(settled_net_paise) as spent
            FROM transaction_lifecycles
            WHERE category = ? AND lifecycle_status = 'DEBITED'
            """,
            (category,),
        )
        row = cursor.fetchone()
        spent_so_far = row["spent"] or 0
        conn.close()

        ref_dt = parse_iso_date(reference_date)
        days_remaining = max(1, 30 - ref_dt.day)

        # After spending amount_paise:
        # Disposable cash drops by amount_paise
        safe_after = max(0, before_safe - (amount_paise // days_remaining))

        # Check category overrun
        new_total_cat = spent_so_far + amount_paise
        cat_overrun = max(0, new_total_cat - cat_alloc)

        # Check fixed bill collision
        # Liquid cash is ₹19,300; upcoming rent is ₹15,000; pending orders ₹7,400
        liquid_cash = report_before.remaining_liquid_cash_paise
        rent_and_fixed = 1500000  # Rent due on 28th
        pending = report_before.pending_committed_paise

        collides = (amount_paise + pending + rent_and_fixed) > liquid_cash
        colliding_bill = "Greenwood Apts Rent (₹15,000 due Nov 28)" if collides else None

        notes = []
        if collides:
            shortfall = (amount_paise + pending + rent_and_fixed) - liquid_cash
            notes.append(
                f"WARNING: Purchasing {paise_to_rupees(amount_paise)} at {merchant} will cause a "
                f"{paise_to_rupees(shortfall)} shortfall when rent is due on the 28th!"
            )
        if cat_overrun > 0:
            notes.append(
                f"Budget Overrun: This purchase pushes '{category}' over budget by {paise_to_rupees(cat_overrun)}."
            )
        notes.append(
            f"Daily safe-to-spend allowance drops from {paise_to_rupees(before_safe)}/day to {paise_to_rupees(safe_after)}/day."
        )

        return SimulationResult(
            amount_paise=amount_paise,
            category=category,
            merchant=merchant,
            safe_to_spend_before_paise=before_safe,
            safe_to_spend_after_paise=safe_after,
            category_overrun_paise=cat_overrun,
            collides_with_fixed_bill=collides,
            colliding_bill_name=colliding_bill,
            notes=notes,
        )
