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
from typing import List, Dict, Any, Optional
from pathlib import Path

from engine.models import (
    Alert,
    ConsequenceReport,
    SimulationResult,
    paise_to_rupees,
)
from engine.forecast import FIXTURES, forecast_context


class ConsequenceEngine:
    def __init__(self, db_path: str, statement_path=None, calendar_path=None):
        self.db_path = db_path
        self.statement_path = Path(statement_path) if statement_path is not None else FIXTURES / "bank_statements.csv"
        self.calendar_path = Path(calendar_path) if calendar_path is not None else FIXTURES / "calendar_bills.json"

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
        Executes read-only dated consequence detectors and computes cash allowances.
        """
        # Streamlit may reload the contracts before this module. Resolve the
        # current constructor here instead of retaining an older model class.
        from engine.models import ConsequenceReport

        context = forecast_context(self, month_key, reference_date)
        profile = context["profile"]
        allocations = context["allocations"]
        alloc_map = {allocation["category"]: allocation for allocation in allocations}
        day_elapsed = int(reference_date[8:10])
        days_remaining = context["days_remaining"]
        savings_target = profile["savings_target_paise"]
        lifecycles = context["lifecycles"]
        subscriptions = context["subscriptions"]
        category_settled = {item["category"]: item["recurring_paise"] for item in context["categories"]}
        category_pending = {item["category"]: item["pending_paise"] for item in context["categories"]}
        total_settled_variable = sum(row["settled_net_paise"] for row in context["monthly_lifecycles"]
                                     if not alloc_map.get(row["category"], {}).get("is_fixed", 0))
        total_pending_orders = context["pending_paise"]
        upcoming_unpaid_fixed = context["fixed_paise"]

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
        remaining_liquid_cash = context["balance_paise"]
        total_commitments_due = total_pending_orders + upcoming_unpaid_fixed

        if total_commitments_due > remaining_liquid_cash:
            shortfall = total_commitments_due - remaining_liquid_cash
            alerts.append(
                Alert(
                    kind="STACKED_OUTFLOWS",
                    impact_paise=shortfall,
                    message=(
                        f"Cash flow collision detected: {sum(row['lifecycle_status'] == 'ORDERED' for row in lifecycles)} pending orders "
                        f"totaling {paise_to_rupees(total_pending_orders)} collide with "
                        f"upcoming rent & bills of {paise_to_rupees(upcoming_unpaid_fixed)}. "
                        f"Total outflows due ({paise_to_rupees(total_commitments_due)}) exceed "
                        f"liquid balance ({paise_to_rupees(remaining_liquid_cash)}), causing a "
                        f"{paise_to_rupees(shortfall)} shortfall against the recorded balance as of {context['balance_as_of']}."
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

        # The statement balance already reflects paid expenses. Reserve only
        # outstanding commitments, savings, and the emergency buffer from here.
        c_disp = context["discretionary_paise"]
        safe_to_spend_today = max(0, c_disp // max(1, days_remaining))

        return ConsequenceReport(
            month_key=month_key,
            reference_date=reference_date,
            alerts=alerts,
            safe_to_spend_today_paise=safe_to_spend_today,
            projected_savings_paise=projected_actual_savings,
            settled_variable_paise=total_settled_variable,
            pending_committed_paise=total_pending_orders,
            remaining_liquid_cash_paise=remaining_liquid_cash,
            discretionary_cash_paise=c_disp,
            upcoming_fixed_paise=upcoming_unpaid_fixed,
            cash_flow_shortfall_paise=context["bill_shortfall_paise"],
            balance_as_of=context["balance_as_of"],
            balance_basis=context["balance_basis"],
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
        from engine.models import SimulationResult

        if type(amount_paise) is not int or amount_paise < 0:
            raise ValueError("Purchase amount must be a nonnegative integer in paise.")
        context = forecast_context(self, month_key, reference_date)
        category_context = next((item for item in context["categories"] if item["category"] == category), None)
        if category_context is None:
            raise ValueError("Choose a discretionary category with a budget allocation.")
        cat_alloc = category_context["budget_paise"]
        spent_so_far = category_context["actual_paise"]
        category_pending = category_context["pending_paise"]
        before_disposable = context["discretionary_paise"]
        after_disposable = before_disposable - amount_paise
        before_safe = max(0, before_disposable // context["days_remaining"])
        safe_after = max(0, after_disposable // context["days_remaining"])
        cat_overrun_before = max(0, spent_so_far + category_pending - cat_alloc)
        cat_overrun = max(0, spent_so_far + category_pending + amount_paise - cat_alloc)
        liquid_cash = context["balance_paise"]
        after_cash = liquid_cash - amount_paise
        before_shortfall = context["bill_shortfall_paise"]
        after_shortfall = max(0, context["fixed_paise"] + context["pending_paise"] - after_cash)
        added_shortfall = after_shortfall - before_shortfall
        collides = after_shortfall > 0
        # Identify the first scheduled bill that cannot be fully covered once orders are reserved.
        available = after_cash - context["pending_paise"]
        colliding_bill = None
        for bill in context["commitments"]:
            available -= bill["amount_paise"]
            if available < 0:
                colliding_bill = f"{bill['name']} (due {bill['due_date']})"
                break
        notes = []
        if before_shortfall:
            notes.append(f"Bills and pending orders already exceed cash by {paise_to_rupees(before_shortfall)}. "
                         f"This purchase adds {paise_to_rupees(added_shortfall)}, bringing the shortfall to {paise_to_rupees(after_shortfall)}.")
        elif after_shortfall:
            notes.append(f"This purchase creates a {paise_to_rupees(after_shortfall)} shortfall for existing bills and pending orders.")
        elif after_disposable < 0:
            notes.append("Bills and pending orders remain covered, but the purchase does not leave enough cash to protect the full savings goal and emergency buffer.")
        else:
            notes.append("Recorded cash covers this purchase, outstanding bills, pending orders, the savings goal, and the emergency buffer.")
        if cat_overrun_before:
            notes.append(f"This category is already {paise_to_rupees(cat_overrun_before)} over budget, including one-time expenses and pending orders. "
                         f"The purchase adds {paise_to_rupees(cat_overrun - cat_overrun_before)} to that overrun.")
        elif cat_overrun:
            notes.append(f"This purchase adds {paise_to_rupees(cat_overrun)} of category budget overrun.")
        if before_safe == 0 and safe_after == 0:
            notes.append("No daily allowance is available before or after the purchase because protected reserves already exceed cash. See the signed cash remainder below.")
        else:
            notes.append(f"Daily allowance changes from {paise_to_rupees(before_safe)} to {paise_to_rupees(safe_after)}.")

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
            discretionary_before_paise=before_disposable,
            discretionary_after_paise=after_disposable,
            bill_shortfall_before_paise=before_shortfall,
            bill_shortfall_after_paise=after_shortfall,
            additional_bill_shortfall_paise=added_shortfall,
            category_overrun_before_paise=cat_overrun_before,
            additional_category_overrun_paise=cat_overrun - cat_overrun_before,
            liquid_cash_before_paise=liquid_cash,
            liquid_cash_after_paise=after_cash,
            upcoming_fixed_paise=context["fixed_paise"],
            pending_committed_paise=context["pending_paise"],
            savings_reserved_paise=context["profile"]["savings_target_paise"],
            emergency_reserved_paise=context["profile"]["emergency_buffer_paise"],
            category_spent_paise=spent_so_far,
            category_pending_paise=category_pending,
            category_budget_paise=cat_alloc,
            balance_as_of=context["balance_as_of"],
            balance_basis=context["balance_basis"],
        )
