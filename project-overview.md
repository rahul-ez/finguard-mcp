# FinGuard MCP: Financial Consequence & Reconciliation Agent

> **Hackathon theme:** Make MCP Matter, *Less Magic, More Utility*
> **Target persona:** Young salaried professionals and everyday digital-payment (UPI / card / e-commerce) users
> **One-line pitch:** Expense trackers tell you what already happened. FinGuard tells you what it will *do to your savings and bills*, before you run out of cash.

---

## Table of Contents

0. [Quick Context for AI Agents](#0-quick-context-for-ai-agents)
1. [Core Idea](#1-core-idea)
2. [Engineering Principles (Non-Negotiable)](#2-engineering-principles-non-negotiable)
3. [System Architecture](#3-system-architecture)
4. [Formulas and Invariants](#4-formulas-and-invariants)
5. [Database Schema](#5-database-schema-finguarddb--sqlite)
6. [Reconciliation State Machine](#6-transaction-reconciliation-state-machine)
7. [MCP Server Specifications](#7-mcp-server-specifications)
8. [Data Contracts (Pydantic Models)](#8-data-contracts-pydantic-models)
9. [Standard Agent Workflows](#9-standard-agent-workflows)
10. [Seeded Demo Scenarios](#10-seeded-demo-scenarios)
11. [Repository Structure](#11-repository-structure)
12. [Quickstart](#12-quickstart)
13. [Suggested Build Order](#13-suggested-build-order)
14. [Judge Q&A Cheat Sheet](#14-judge-qa-cheat-sheet)
15. [Glossary](#15-glossary)

---

## 0. Quick Context for AI Agents

If you are an AI agent (coding assistant, or the host LLM using these MCP servers), read this section first.

**What this project is:** Two MCP servers plus a deterministic Python engine and a SQLite ledger. They merge financial signals from emails, bank/UPI statements and calendar bills into one ledger of transaction lifecycles, then forecast the consequences of current spending.

**Your role depends on what you are doing:**

| If you are... | You must... |
| :--- | :--- |
| **Using the MCP tools** (host agent) | Call tools, explain their outputs in plain English, ask the user about ambiguities, and request approval before any write. **Never** compute or estimate numbers yourself. |
| **Extending the codebase** (coding agent) | Follow all five rules in Section 2. Put all math in `engine/`. Never add `float` money fields. Never add a write tool that skips the preview/approve/commit flow. |

**Hard rules, short form:**
1. No LLM arithmetic. Every number comes from a deterministic tool output.
2. Money is integer paise. `₹1 = 100 paise`. No floats.
3. An order email is not a settled spend. Use lifecycle states.
4. Every write follows `Preview -> approval_token -> user_approved=True -> Commit`.
5. Data stays local. Only minimal parsed context goes to external APIs.

**When unsure about a number:** call `run_consequence_engine` or `simulate_purchase_decision`. Do not guess.

---

## 1. Core Idea

Traditional expense trackers report history ("You spent ₹8,000 on shopping"). **FinGuard MCP** does more:

- **Connects fragmented signals:** email receipts, bank/UPI statements and calendar commitments.
- **Builds a deterministic financial event ledger** that reconciles multi-step lifecycles (`Order -> Debit -> Cancel -> Refund`), so cancelled orders and pending refunds never distort your totals.
- **Forecasts spending velocity** and projects month-end totals per category.
- **Warns of downstream consequences** on savings and fixed bills (rent, EMIs) before cash runs out.
- **Requires explicit human approval** for every ledger mutation or external action.

---

## 2. Engineering Principles (Non-Negotiable)

Humans and AI agents extending this repository must obey all five.

1. **Zero LLM arithmetic.** The LLM must never compute totals, remaining budgets, daily allowances, taxes/fees or end-of-month projections. Every number shown to the user must originate from a deterministic Python function inside the MCP servers.
2. **Integer paise representation.** All monetary values in SQLite, Python models and MCP payloads are integer paise (`₹2,499.50` = `249950`). Format to `₹X,XXX.XX` only at the UI/presentation boundary. `float` is forbidden for currency.
3. **Transaction lifecycle over isolated rows.** An order confirmation email is not proof of settled spend. Every event maps into a unified `TransactionLifecycle`: `ORDERED -> DEBITED -> CANCELLED -> REFUND_PENDING -> REFUNDED / REFUND_OVERDUE`.
4. **Two-phase guarded mutations (`Preview -> Approve -> Commit`).** No MCP tool may write to the ledger, change budget allocations or dispatch a follow-up draft without first producing a deterministic diff and a cryptographic `approval_token`, then receiving `user_approved=True`.
5. **Local-first and privacy-preserving.** Raw bank statements, UPI alerts and email fixtures stay on localhost in SQLite (`finguard.db`). External APIs receive only the minimal parsed context needed for explanation.

---

## 3. System Architecture

```text
+-----------------------------------------------------------------------------+
|                        EXTERNAL / LOCAL DATA SOURCES                        |
|  [Email Receipts & Alerts]   [Bank / UPI Statements]   [Calendar / Bills]   |
+--------------+--------------------------+------------------------+----------+
               |                          |                        |
               v                          v                        v
+-----------------------------------------------------------------------------+
|                        MCP INTEGRATION LAYER (FastMCP)                      |
|                                                                             |
|  +-----------------------------------+   +-------------------------------+  |
|  |   1. finance_sources_mcp          |   |   2. finguard_ledger_mcp      |  |
|  |   - extract_and_normalize_events  |   |   - propose_reconciliation_   |  |
|  |   - draft_action_package          |   |       batch                   |  |
|  |   (+ resources: emails,           |   |   - commit_reconciliation_    |  |
|  |      statements, calendar)        |   |       batch                   |  |
|  |                                   |   |   - run_consequence_engine    |  |
|  |                                   |   |   - simulate_purchase_decision|  |
|  |                                   |   |   - undo_last_commit          |  |
|  +-----------------------------------+   +-------------------------------+  |
+------------------------------------------------------+----------------------+
                                                       |
                                                       v
+-----------------------------------------------------------------------------+
|                     DETERMINISTIC CORE & STATE (Python + SQLite)            |
|  [Reconciliation Engine]  [Velocity & Consequence Engine]  [finguard.db]    |
+------------------------------------------------------+----------------------+
                                                       |
                                                       v
+-----------------------------------------------------------------------------+
|                         HOST CLIENT & HUMAN APPROVAL UI                     |
|   - Consequence & Trajectory Dashboard   - Lifecycle & Refund Tracker       |
|   - Human-in-the-Loop Approval Gate      - Interactive "Can I Buy This?"    |
+-----------------------------------------------------------------------------+
```

### Separation of Responsibilities

| Component | Role | State |
| :--- | :--- | :--- |
| **`finance_sources_mcp`** | Stateless ingestion and action adapters. Reads authorized local/sandboxed fixtures (`emails.json`, `bank_statements.csv`, `calendar_bills.json`). Extracts structured events with confidence scores. Prepares human-approved follow-up drafts (e.g. overdue-refund emails). **Never sends anything externally.** | Stateless |
| **`finguard_ledger_mcp`** | Owns `finguard.db`. Runs entity resolution across sources, enforces accounting invariants, computes burn velocity and savings impact. Exposes read-only `ledger://...` resources and guarded write tools. | Stateful |
| **Host LLM agent** | Orchestrates tool calls, surfaces ambiguities (e.g. unmapped personal UPI handles), turns deterministic outputs into plain-English explanations, and asks for confirmation before any mutation. **Does no math.** | None |

---

## 4. Formulas and Invariants

All values are integer paise unless stated. All of this logic lives in `engine/consequences.py` and `engine/reconciler.py`, never in the LLM.

### 4.1 Conservation of Funds Invariant

Checked atomically inside `commit_reconciliation_batch()` before every SQLite `COMMIT`. If violated, the transaction rolls back immediately.

```text
MonthlyIncome == sum(FixedCommitments) + sum(VariableAllocations) + SavingsTarget + EmergencyBuffer
```

### 4.2 Effective Net Spend (Lifecycle-Aware)

A transaction's contribution to category spend depends on its reconciled state:

| Lifecycle state | `effective_spend` | Tracked as |
| :--- | :--- | :--- |
| `ORDERED` (no bank debit yet) | `0` | `pending_outflow` |
| `DEBITED` (settled, active) | `amount_paise` | settled spend |
| `CANCELLED` + `REFUND_PENDING` (within SLA, e.g. <= 5 business days) | `0` | `receivable_refund` |
| `CANCELLED` + `REFUND_OVERDUE` (past expected refund date) | `amount_paise` (cash is still missing) | Triggers **Scenario D alert** |
| `REFUNDED` (credit matched on statement) | `0` (`net_delta = debit - credit = 0`) | none |

### 4.3 Category Spending Velocity and End-of-Month Projection

Definitions:

| Symbol | Meaning |
| :--- | :--- |
| `D_total` | Total days in billing month (e.g. 30) |
| `D_elapsed` | Days elapsed so far (e.g. 18) |
| `D_rem` | `D_total - D_elapsed` (e.g. 12) |
| `B_cat` | Monthly category budget allocation |
| `S_cat` | Settled variable spend in category so far (excluding one-time anomalies if tagged, otherwise total settled spend) |

```text
1. Daily burn rate:          V_cat      = S_cat / D_elapsed
2. Projected month-end:      S_hat_cat  = S_cat + (V_cat * D_rem) + PendingCommittedOutflows_cat
3. Projected overrun:        Overrun    = max(0, S_hat_cat - B_cat)
4. Weekly course correction: WeeklyTrim = Overrun / max(1, D_rem / 7)
```

### 4.4 Safe-to-Spend and Savings Impact

```text
Remaining disposable cash:
  C_disp = Income - PaidFixed - UpcomingUnpaidFixed - TotalSettledVariable
           - PendingOrders - SavingsTarget

Safe-to-spend today:
  S_today = max(0, C_disp / D_rem)

Savings erosion (when total projected variable spend > total variable budget):
  ProjectedActualSavings = max(0, SavingsTarget - sum(Overrun across categories))
```

### 4.5 Rounding Rule

Integer division in paise must be deterministic. Use floor division (`//`) unless a function documents otherwise, and unit-test it. Example: a weekly trim of `₹1,166.67` is stored as `116666` paise and displayed as `₹1,166.66` or rounded for display only, consistently everywhere.

---

## 5. Database Schema (`finguard.db`, SQLite)

```sql
-- 1. Monthly budget profile (adaptive allocations, integer paise)
CREATE TABLE IF NOT EXISTS budget_profiles (
    month_key TEXT PRIMARY KEY,              -- e.g. '2026-11'
    monthly_income_paise INTEGER NOT NULL,
    savings_target_paise INTEGER NOT NULL,
    emergency_buffer_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_allocations (
    allocation_id TEXT PRIMARY KEY,
    month_key TEXT NOT NULL REFERENCES budget_profiles(month_key),
    category TEXT NOT NULL,                  -- 'rent_utilities' | 'food_groceries' | 'transport' | 'entertainment' | 'shopping' | 'subscriptions'
    allocated_paise INTEGER NOT NULL,
    is_fixed INTEGER NOT NULL DEFAULT 0,     -- 1 = fixed commitment, 0 = discretionary
    due_day INTEGER                          -- day of month if fixed commitment
);

-- 2. Unified transaction lifecycles (reconciles orders, debits, cancels, refunds)
CREATE TABLE IF NOT EXISTS transaction_lifecycles (
    lifecycle_id TEXT PRIMARY KEY,           -- e.g. 'lc_amz_2499_01'
    merchant_normalized TEXT NOT NULL,       -- e.g. 'Amazon India', 'Swiggy', 'Netflix'
    category TEXT NOT NULL,
    expected_amount_paise INTEGER NOT NULL,
    settled_net_paise INTEGER NOT NULL DEFAULT 0,
    lifecycle_status TEXT NOT NULL,          -- 'ORDERED' | 'DEBITED' | 'CANCELLED' | 'REFUND_PENDING' | 'REFUNDED' | 'REFUND_OVERDUE'
    order_ref TEXT,
    upi_utr TEXT,
    expected_refund_by TEXT,                 -- ISO date 'YYYY-MM-DD' if cancelled
    is_one_time_spike INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 3. Raw atomic events linked to a lifecycle (auditability and deduplication)
CREATE TABLE IF NOT EXISTS raw_events (
    event_id TEXT PRIMARY KEY,
    lifecycle_id TEXT REFERENCES transaction_lifecycles(lifecycle_id),
    source_type TEXT NOT NULL,               -- 'gmail_receipt' | 'bank_statement' | 'upi_alert' | 'calendar'
    external_ref_hash TEXT UNIQUE NOT NULL,  -- SHA-256 of raw payload (idempotency)
    event_type TEXT NOT NULL,                -- 'ORDER_PLACED' | 'BANK_DEBIT' | 'ORDER_CANCELLED' | 'BANK_REFUND' | 'SUBSCRIPTION_RENEWAL'
    amount_paise INTEGER NOT NULL,
    event_timestamp TEXT NOT NULL,
    raw_snippet TEXT NOT NULL,
    confidence REAL NOT NULL                 -- parser confidence 0.0-1.0 (not money, so REAL is allowed)
);

-- 4. Recurring subscriptions and price-change watchlist
CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id TEXT PRIMARY KEY,
    merchant_normalized TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    previous_amount_paise INTEGER NOT NULL,
    current_amount_paise INTEGER NOT NULL,
    billing_cycle TEXT NOT NULL,             -- 'monthly' | 'annual'
    next_billing_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'    -- 'active' | 'flagged_hike' | 'cancelled'
);

-- 5. Guarded commit and rollback audit log
CREATE TABLE IF NOT EXISTS audit_log (
    tx_id TEXT PRIMARY KEY,
    approval_token TEXT UNIQUE NOT NULL,
    action_type TEXT NOT NULL,
    before_state_json TEXT NOT NULL,
    after_state_json TEXT NOT NULL,
    approved_by_user INTEGER NOT NULL,
    reverted INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL
);
```

---

## 6. Transaction Reconciliation State Machine

The deterministic matcher links separate events from email and bank/UPI sources into one `transaction_lifecycles` record.

### 6.1 Matching Key Priority

1. **Exact match:** `order_ref` or `upi_utr` matches an open lifecycle.
2. **Fuzzy window match:** same `merchant_normalized` AND `event.amount_paise == lifecycle.expected_amount_paise` within **±5 days**.
3. **No match:** create a new lifecycle, or flag the event for user classification (e.g. unmapped personal UPI handle).

### 6.2 State Transitions

| Incoming event | Source | Transition | Effect |
| :--- | :--- | :--- | :--- |
| `ORDER_PLACED` | Email | creates lifecycle in `ORDERED` | `settled_net_paise = 0` |
| `BANK_DEBIT` | Statement / UPI | `ORDERED` -> `DEBITED` (or creates a new lifecycle) | `settled_net_paise = amount_paise` |
| `ORDER_CANCELLED` | Email | `DEBITED` -> `REFUND_PENDING` | `expected_refund_by = cancel_date + 5 days` |
| `BANK_REFUND` | Statement credit | `REFUND_PENDING` or `REFUND_OVERDUE` -> `REFUNDED` | `settled_net_paise = 0` |

### 6.3 Daily Sweep

Any `REFUND_PENDING` lifecycle where `current_date > expected_refund_by` automatically transitions to `REFUND_OVERDUE` and emits a **Consequence Alert**.

### 6.4 Idempotency

Every raw event is hashed (`SHA-256` of the raw payload) into `external_ref_hash` (UNIQUE). Re-ingesting the same email or statement row is a no-op.

---

## 7. MCP Server Specifications

Both servers use `fastmcp` (`from fastmcp import FastMCP`) with strict Pydantic v2 input/output schemas.

### 7.1 Server 1: `finance_sources_mcp.py` (read-only adapters and draft generator)

**Resources**

| URI | Returns |
| :--- | :--- |
| `sources://emails/unread` | Parsed e-commerce orders, cancellations and subscription renewal notices |
| `sources://statements/latest` | Bank debit/credit rows and UPI alerts with UTR references |
| `sources://calendar/upcoming_bills` | Upcoming EMIs, rent and scheduled payments for the next 30 days |

**Tools**

| Tool | Behavior |
| :--- | :--- |
| `extract_and_normalize_events(source_filter: str = "all") -> list[NormalizedEvent]` | Parses raw emails/CSV rows into typed events with SHA-256 `external_ref_hash` and a `confidence` score. Flags personal UPI handles (`@okicici`, `@okaxis`, `@ybl`) that have no known merchant mapping as `requires_user_classification = True`. |
| `draft_action_package(action_kind: str, lifecycle_id: str, approval_token: str) -> DraftPackage` | Generates a ready-to-send refund escalation email (citing exact `order_ref`, `upi_utr`, debit date and overdue days) or a subscription cancellation checklist. **Never sends externally.** Returns draft text for user approval. |

### 7.2 Server 2: `finguard_ledger_mcp.py` (ledger, forecasting and consequence engine)

**Resources (read-only)**

| URI | Returns |
| :--- | :--- |
| `ledger://budget/summary/{month_key}` | Budget profile, category allocations, settled spend, pending outflows, safe-to-spend today |
| `ledger://lifecycles/active` | All lifecycles grouped by status (`DEBITED`, `ORDERED`, `REFUND_PENDING`, `REFUND_OVERDUE`) |
| `ledger://consequences/alerts` | Active deterministic warnings ranked by financial impact (in paise) |

**Tools**

| Tool | Type | Behavior |
| :--- | :--- | :--- |
| `propose_reconciliation_batch(events: list[NormalizedEvent]) -> CommitPreview` | Dry run | Runs the matcher and returns exact proposed DB inserts/updates, ambiguous UPI items needing user input, and a SHA-256 `approval_token`. Writes nothing. |
| `commit_reconciliation_batch(approval_token: str, user_approved: bool, user_overrides: dict \| None = None) -> CommitResult` | **Guarded write** | Fails immediately if `user_approved is not True` or the token mismatches. Applies `user_overrides` (e.g. classifying a personal UPI payment), verifies the Conservation-of-Funds invariant, writes to `audit_log`, commits atomically. |
| `run_consequence_engine(month_key: str, reference_date: str) -> ConsequenceReport` | Read-only | Runs all four detectors (below). |
| `simulate_purchase_decision(amount_paise: int, category: str, merchant: str, reference_date: str) -> SimulationResult` | Read-only | Stateless what-if: impact on `safe_to_spend_today`, category overrun probability, and whether it collides with an upcoming fixed bill. |
| `undo_last_commit() -> RollbackResult` | Guarded write | Reads the latest un-reverted `audit_log` entry, restores `before_state_json`, marks `reverted = 1`. |

**The four consequence detectors in `run_consequence_engine`:**

1. **Velocity and Savings Erosion:** projects month-end category totals and the exact ₹ drop in planned savings.
2. **Subscription Price Hike:** flags `current_amount_paise > previous_amount_paise`, computes annualized extra cost (`delta * 12`), and checks discretionary headroom.
3. **Stacked Upcoming Outflows:** sums `ORDERED` pending payments plus upcoming fixed bills (rent, EMIs) against remaining liquid balance to detect cash shortfalls before payday.
4. **Overdue Refund Tracker:** flags `REFUND_OVERDUE` lifecycles and calculates locked cash.

---

## 8. Data Contracts (Pydantic Models)

Defined in `engine/models.py`. These are the reference shapes. Money fields are always `int` paise.

```python
from pydantic import BaseModel, Field
from typing import Literal, Optional

LifecycleStatus = Literal[
    "ORDERED", "DEBITED", "CANCELLED",
    "REFUND_PENDING", "REFUNDED", "REFUND_OVERDUE",
]
EventType = Literal[
    "ORDER_PLACED", "BANK_DEBIT", "ORDER_CANCELLED",
    "BANK_REFUND", "SUBSCRIPTION_RENEWAL",
]
SourceType = Literal["gmail_receipt", "bank_statement", "upi_alert", "calendar"]

class NormalizedEvent(BaseModel):
    external_ref_hash: str                    # SHA-256 of raw payload
    source_type: SourceType
    event_type: EventType
    merchant_normalized: Optional[str] = None
    category: Optional[str] = None
    amount_paise: int                         # integer paise only
    event_timestamp: str                      # ISO 8601
    order_ref: Optional[str] = None
    upi_utr: Optional[str] = None
    raw_snippet: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_user_classification: bool = False

class CommitPreview(BaseModel):
    approval_token: str                       # SHA-256 over the proposed diff
    proposed_inserts: list[dict]
    proposed_updates: list[dict]
    ambiguous_items: list[NormalizedEvent]    # need user input
    invariant_check_passed: bool

class CommitResult(BaseModel):
    tx_id: str
    committed: bool
    reason: Optional[str] = None              # populated on failure/rollback

class Alert(BaseModel):
    kind: Literal["VELOCITY", "PRICE_HIKE", "STACKED_OUTFLOWS", "REFUND_OVERDUE"]
    impact_paise: int
    message: str                              # generated by deterministic code
    lifecycle_id: Optional[str] = None

class ConsequenceReport(BaseModel):
    month_key: str
    reference_date: str
    alerts: list[Alert]                       # ranked by impact_paise desc
    safe_to_spend_today_paise: int
    projected_savings_paise: int

class SimulationResult(BaseModel):
    safe_to_spend_before_paise: int
    safe_to_spend_after_paise: int
    category_overrun_paise: int
    collides_with_fixed_bill: bool
    notes: list[str]

class DraftPackage(BaseModel):
    action_kind: str                          # 'refund_escalation' | 'subscription_cancel'
    lifecycle_id: str
    draft_text: str
    sent: Literal[False] = False              # drafts are never sent by the system
```

> The exact field sets may evolve, but the **types (int paise, never float money)** and the **preview -> token -> commit** flow must not change.

---

## 9. Standard Agent Workflows

### 9.1 Ingest and reconcile

```text
1. extract_and_normalize_events("all")
2. If any event has requires_user_classification=True -> ask the user what it is.
3. propose_reconciliation_batch(events)           # dry run, returns approval_token
4. Show the user the proposed diff in plain English.
5. Wait for explicit approval.
6. commit_reconciliation_batch(token, user_approved=True, user_overrides={...})
```

### 9.2 "How am I doing this month?"

```text
1. run_consequence_engine(month_key, reference_date)
2. Explain each Alert in plain language. Quote the numbers verbatim from the report.
```

### 9.3 "Can I buy this?"

```text
1. simulate_purchase_decision(amount_paise, category, merchant, reference_date)
2. Report the before/after safe-to-spend and any bill collision. No own math.
```

### 9.4 Overdue refund follow-up

```text
1. run_consequence_engine -> finds REFUND_OVERDUE lifecycle
2. draft_action_package("refund_escalation", lifecycle_id, approval_token)
3. Show the draft. The user sends it themselves. FinGuard never sends it.
```

### 9.5 Mistake recovery

```text
undo_last_commit()   # restores the previous state from audit_log
```

### Agent do / don't

| Do | Don't |
| :--- | :--- |
| Quote numbers exactly as returned by tools | Add, subtract, average or "round" amounts yourself |
| Ask about ambiguous UPI payees | Guess a category for an unknown VPA |
| Show the diff before asking approval | Pass `user_approved=True` without an explicit user "yes" |
| Treat order emails as *pending*, not spent | Count `ORDERED` or cancelled-and-refunded items as spend |

---

## 10. Seeded Demo Scenarios

Seed `finguard.db` and `fixtures/` with a realistic salaried profile: **₹60,000/month take-home**, reference date **18th of the month**, **12 days remaining**. The seed covers four showcase scenarios.

| Scenario | Fixture inputs | Deterministic system output |
| :--- | :--- | :--- |
| **A. Subscription price hike** | Renewal email: **StreamFlix** goes from `₹499` (`49900`) to `₹699` (`69900`). | Detects `+₹200/mo` (`+₹2,400/yr`), flags a `subscriptions` overrun, offers a downgrade/cancel workflow on approval. |
| **B. Stacked upcoming purchases** | 3 open orders (`ORDERED` / BNPL due on 25th): laptop bag `₹2,200`, earbuds `₹1,800`, sneakers `₹3,400`. Rent `₹15,000` due on 28th. | `₹7,400` pending outflows collide with `₹15,000` rent, giving a `₹3,100` shortfall unless discretionary spend pauses.* |
| **C. Category velocity overrun** | Entertainment budget `₹4,000`; `₹3,600` (90%) spent by day 18 at a steady pace (not a single spike). | Projects `₹6,000` month-end spend (`+₹2,000` overrun), cutting planned `₹12,000` savings to `₹10,000`. Recommends trimming about `₹1,166/week`. |
| **D. Missing refund lifecycle** | Amazon order `₹2,499` (`lc_amz_2499`): ordered day 3, bank debit day 3, cancelled day 5, no refund by day 18 (past the 5-day SLA). | Links all events into one lifecycle (`REFUND_OVERDUE`), keeps `₹2,499` visible as missing cash, drafts an evidence-backed refund claim email with the UTR. |

\* The `₹3,100` figure implies the seed has about `₹19,300` of liquid balance available before payday (`7,400 + 15,000 - 3,100`). Keep the seed data consistent with this.

**Sanity check for Scenario C (verify in tests):**
`V = 360000 / 18 = 20000 paise/day` -> `S_hat = 360000 + 20000 * 12 = 600000` -> `Overrun = 600000 - 400000 = 200000` -> `WeeklyTrim = 200000 / (12/7) ≈ 116666 paise`.

---

## 11. Repository Structure

```text
finguard-mcp/
├── README.md                      # This specification
├── pyproject.toml                 # Dependencies: fastmcp, pydantic, streamlit (or fastapi)
├── db/
│   ├── schema.sql                 # SQLite DDL from Section 5
│   ├── seed_demo.py               # Populates finguard.db and fixtures
│   └── finguard.db                # Local SQLite database (auto-generated)
├── fixtures/
│   ├── emails.json                # Orders, cancellations, subscription hike notices
│   ├── bank_statements.csv        # Bank debits, credits, UPI UTR rows
│   └── calendar_bills.json        # Upcoming rent, EMIs, fixed due dates
├── servers/
│   ├── finance_sources_mcp.py     # MCP Server 1: parsers and approval-gated drafts
│   └── finguard_ledger_mcp.py     # MCP Server 2: reconciliation, invariants, consequences
├── engine/
│   ├── models.py                  # Shared Pydantic schemas (integer paise enforced)
│   ├── reconciler.py              # Deterministic lifecycle state machine
│   └── consequences.py            # Velocity, savings erosion, hike and shortfall math
├── app/
│   └── dashboard.py               # Host UI: trajectory gauge, lifecycle table, approval modal
└── tests/
    └── test_invariants.py         # Paise conservation, dedup, refund SLA
```

---

## 12. Quickstart

```bash
# 1. Install dependencies
uv add fastmcp pydantic streamlit

# 2. Initialize SQLite database and seed the 4 demo scenarios
python db/seed_demo.py

# 3. Verify deterministic math and accounting invariants
pytest tests/test_invariants.py

# 4. Inspect MCP tools and resources standalone via MCP Inspector
npx @modelcontextprotocol/inspector python servers/finguard_ledger_mcp.py

# 5. Launch the interactive host UI and approval dashboard
streamlit run app/dashboard.py
```

### Connecting an MCP client (example)

Register both servers in your MCP-capable client (path and command names may vary by client):

```json
{
  "mcpServers": {
    "finance_sources": { "command": "python", "args": ["servers/finance_sources_mcp.py"] },
    "finguard_ledger": { "command": "python", "args": ["servers/finguard_ledger_mcp.py"] }
  }
}
```

---

## 13. Suggested Build Order

For a 24-hour hackathon, in this order:

1. `engine/models.py`: Pydantic schemas with int paise.
2. `db/schema.sql` and `db/seed_demo.py`: database plus the four scenarios.
3. `engine/reconciler.py`: lifecycle state machine and hashing/dedup.
4. `engine/consequences.py`: the four detectors and formulas from Section 4.
5. `tests/test_invariants.py`: lock the math before building on top of it.
6. `servers/finance_sources_mcp.py`, then `servers/finguard_ledger_mcp.py`.
7. `app/dashboard.py`: UI and approval modal.
8. Rehearse the 90-second pitch using Scenarios A-D.

**Minimum tests to write:**
- Paise conservation (Conservation-of-Funds invariant holds and rolls back on violation).
- Dedup (same payload ingested twice creates one event).
- Refund SLA (`REFUND_PENDING` becomes `REFUND_OVERDUE` after the window).
- Commit refuses `user_approved=False` and mismatched tokens.
- Scenario C numbers match the sanity check in Section 10.

---

## 14. Judge Q&A Cheat Sheet

**Q: Why MCP instead of a single monolithic script?**
A: Financial data lives across untrusted, heterogeneous sources (emails, bank CSVs, calendars) with different permission boundaries. MCP separates **read-only source adapters** (`finance_sources_mcp`) from the **stateful accounting ledger** (`finguard_ledger_mcp`), and enforces a typed `preview -> approval_token -> commit` boundary so the LLM can never silently mutate balances or double-count transactions.

**Q: What happens if the LLM hallucinates a number?**
A: It cannot alter ledger state. All amounts are parsed into integer paise by deterministic adapters, verified against a Conservation-of-Funds invariant on every SQLite commit, and projected using closed-form velocity formulas. The LLM only coordinates tools and explains the verified outputs.

**Q: How do you prevent double-counting when both an order email and a bank debit arrive?**
A: Every raw event is hashed (SHA-256) for idempotency and passed through a lifecycle state machine (`ORDERED -> DEBITED -> CANCELLED -> REFUND_PENDING -> REFUNDED`) keyed by `order_ref`, `upi_utr`, and `(merchant, exact_paise, ±5-day window)`.

---

## 15. Glossary

| Term | Meaning |
| :--- | :--- |
| **Paise** | 1/100 of a rupee. All money is stored as integer paise. |
| **UPI** | Unified Payments Interface, India's instant payment system. |
| **VPA** | Virtual Payment Address (UPI handle such as `name@okicici`). |
| **UTR** | Unique Transaction Reference number on a bank/UPI transaction. |
| **BNPL** | Buy Now, Pay Later. |
| **EMI** | Equated Monthly Instalment, a fixed recurring loan payment. |
| **Lifecycle** | The reconciled chain of events (order, debit, cancel, refund) for one purchase. |
| **Approval token** | SHA-256 hash of a proposed diff. A commit must present the matching token. |
| **Pending outflow** | Money committed to leave the account (e.g. `ORDERED` item) but not yet debited. |
| **Receivable refund** | Money owed back to the user after a cancellation, still within the refund SLA. |
| **Savings erosion** | The ₹ amount by which projected overruns reduce the planned savings target. |
| **Safe-to-spend** | Deterministic daily allowance after fixed bills, pending orders and savings target. |
