"""
FinGuard MCP - Data Contracts (Pydantic Models)
Enforces:
1. Integer paise representation (₹1 = 100 paise) - float currency is forbidden.
2. Strict lifecycle & event enums.
3. Two-phase guarded commit contracts.
"""

from typing import Literal, Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator


LifecycleStatus = Literal[
    "ORDERED",
    "DEBITED",
    "CANCELLED",
    "REFUND_PENDING",
    "REFUNDED",
    "REFUND_OVERDUE",
]

EventType = Literal[
    "ORDER_PLACED",
    "BANK_DEBIT",
    "ORDER_CANCELLED",
    "BANK_REFUND",
    "SUBSCRIPTION_RENEWAL",
]

SourceType = Literal["gmail_receipt", "bank_statement", "upi_alert", "calendar"]


def paise_to_rupees(paise: int) -> str:
    """Formats integer paise into standard Indian Rupee representation (e.g. 249950 -> '₹2,499.50')."""
    sign = "-" if paise < 0 else ""
    abs_paise = abs(paise)
    rupees_part = abs_paise // 100
    paise_part = abs_paise % 100
    return f"{sign}₹{rupees_part:,}.{paise_part:02d}"


def rupees_to_paise(rupees: float) -> int:
    """Converts a floating rupee value to integer paise safely using exact rounding."""
    return int(round(rupees * 100))


class NormalizedEvent(BaseModel):
    """Atomic financial event extracted from email, statement, or calendar."""
    external_ref_hash: str  # SHA-256 of raw payload (idempotency key)
    source_type: SourceType
    event_type: EventType
    merchant_normalized: Optional[str] = None
    category: Optional[str] = None
    amount_paise: int = Field(..., description="Monetary value in integer paise")
    event_timestamp: str  # ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    order_ref: Optional[str] = None
    upi_utr: Optional[str] = None
    raw_snippet: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_user_classification: bool = False

    @field_validator("amount_paise")
    @classmethod
    def validate_amount_paise(cls, v: int) -> int:
        if not isinstance(v, int):
            raise TypeError("amount_paise must be an integer (paise)")
        return v


class CommitPreview(BaseModel):
    """Diff and security token generated during dry-run propose step."""
    approval_token: str  # SHA-256 over proposed inserts/updates
    proposed_inserts: List[Dict[str, Any]]
    proposed_updates: List[Dict[str, Any]]
    ambiguous_items: List[NormalizedEvent]
    invariant_check_passed: bool
    summary: str


class CommitResult(BaseModel):
    """Outcome of guarded commit."""
    tx_id: str
    committed: bool
    reason: Optional[str] = None


class RollbackResult(BaseModel):
    """Outcome of undoing the last commit."""
    tx_id: str
    reverted: bool
    restored_state: Optional[Dict[str, Any]] = None
    message: str


class Alert(BaseModel):
    """Deterministic consequence warning ranked by impact."""
    kind: Literal["VELOCITY", "PRICE_HIKE", "STACKED_OUTFLOWS", "REFUND_OVERDUE"]
    impact_paise: int
    message: str
    lifecycle_id: Optional[str] = None


class ConsequenceReport(BaseModel):
    """Comprehensive financial consequence evaluation report."""
    month_key: str
    reference_date: str
    alerts: List[Alert]  # ranked by impact_paise desc
    safe_to_spend_today_paise: int
    projected_savings_paise: int
    settled_variable_paise: int
    pending_committed_paise: int
    remaining_liquid_cash_paise: int
    discretionary_cash_paise: int = 0
    upcoming_fixed_paise: int = 0
    cash_flow_shortfall_paise: int = 0
    balance_as_of: str = ""
    balance_basis: str = "bank_statement"


class SimulationResult(BaseModel):
    """Result of what-if purchase simulation."""
    amount_paise: int
    category: str
    merchant: str
    safe_to_spend_before_paise: int
    safe_to_spend_after_paise: int
    category_overrun_paise: int
    collides_with_fixed_bill: bool
    colliding_bill_name: Optional[str] = None
    notes: List[str]
    discretionary_before_paise: int = 0
    discretionary_after_paise: int = 0
    bill_shortfall_before_paise: int = 0
    bill_shortfall_after_paise: int = 0
    additional_bill_shortfall_paise: int = 0
    category_overrun_before_paise: int = 0
    additional_category_overrun_paise: int = 0
    liquid_cash_before_paise: int = 0
    liquid_cash_after_paise: int = 0
    upcoming_fixed_paise: int = 0
    pending_committed_paise: int = 0
    savings_reserved_paise: int = 0
    emergency_reserved_paise: int = 0
    category_spent_paise: int = 0
    category_pending_paise: int = 0
    category_budget_paise: int = 0
    balance_as_of: str = ""
    balance_basis: str = "bank_statement"


class DraftPackage(BaseModel):
    """Ready-to-send action draft for human review."""
    action_kind: Literal["refund_escalation", "subscription_cancel"]
    lifecycle_id: str
    draft_text: str
    recipient: Optional[str] = None
    subject: Optional[str] = None
    sent: Literal[False] = False  # FinGuard never dispatches externally


class CategoryAllocation(BaseModel):
    allocation_id: str
    month_key: str
    category: str
    allocated_paise: int
    is_fixed: bool
    due_day: Optional[int] = None


class BudgetProfile(BaseModel):
    month_key: str
    monthly_income_paise: int
    savings_target_paise: int
    emergency_buffer_paise: int
    created_at: str
    allocations: List[CategoryAllocation] = []


class TransactionLifecycle(BaseModel):
    lifecycle_id: str
    merchant_normalized: str
    category: str
    expected_amount_paise: int
    settled_net_paise: int = 0
    lifecycle_status: LifecycleStatus
    order_ref: Optional[str] = None
    upi_utr: Optional[str] = None
    expected_refund_by: Optional[str] = None
    is_one_time_spike: bool = False
    created_at: str
    updated_at: str


class SubscriptionRecord(BaseModel):
    sub_id: str
    merchant_normalized: str
    category: str
    previous_amount_paise: int
    current_amount_paise: int
    billing_cycle: Literal["monthly", "annual"]
    next_billing_date: str
    status: Literal["active", "flagged_hike", "cancelled"] = "active"
