# FinGuard MCP: Financial Consequence & Reconciliation Agent

> **Hackathon Theme:** Make MCP Matter — *Less Magic, More Utility*  
> **Target Persona:** Young salaried professionals and everyday digital-payment (UPI / card / e-commerce) users  
> **One-Line Pitch:** Expense trackers tell you what already happened. **FinGuard tells you what it will *do to your savings and bills*, before you run out of cash.**

---

## 1. Executive Summary & Problem Statement

Every month, millions of digital-payment users face unexpected cash crunches:
- E-commerce orders placed with BNPL or COD collide with month-end rent.
- Cancelled orders leave funds missing for weeks past the 5-day SLA without notice.
- Stealth subscription price hikes quietly erode annual savings.
- Discretionary burn velocity drains bank accounts before fixed bills are paid.

Traditional personal finance apps simply report history: *"You spent ₹3,600 on entertainment."*  
**FinGuard MCP changes the paradigm**: it unifies fragmented signals (email order receipts, bank/UPI statements, calendar bills), tracks multi-event transaction lifecycles, and projects the **downstream consequences** on upcoming fixed bills and planned savings targets.

---

## 2. Core Engineering Principles (Non-Negotiable)

1. **Zero LLM Arithmetic:** The LLM host agent never adds, multiplies, or estimates currency. Every number originates from a deterministic Python function inside the consequence and reconciliation engines.
2. **Integer Paise Representation:** All monetary values in SQLite, Python models, and MCP payloads are stored as integer paise (`₹1 = 100 paise`, e.g., `₹2,499.50` = `249950`). Float currency is strictly forbidden.
3. **Transaction Lifecycle Over Isolated Rows:** Order emails are not settled spends. Every purchase maps into a reconciled lifecycle:
   $$\text{ORDERED} \longrightarrow \text{DEBITED} \longrightarrow \text{CANCELLED} \longrightarrow \text{REFUND\_PENDING} \longrightarrow \text{REFUNDED / REFUND\_OVERDUE}$$
4. **Two-Phase Guarded Mutations (`Preview -> approval_token -> Commit`):** No MCP tool may mutate the ledger without returning a dry-run diff and cryptographic SHA-256 token, requiring explicit human approval (`user_approved=True`).
5. **Local-First & Privacy-Preserving:** Raw statements and fixtures stay on localhost in SQLite (`finguard.db`). No financial PII leaves the user's environment.

---

## 3. System Architecture

```text
+-----------------------------------------------------------------------------+
|                        EXTERNAL / LOCAL DATA FIXTURES                       |
|   [Email Orders & Hikes]       [Bank / UPI Statements]   [Calendar Rent/Bills] |
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
|  |   (Resources: emails,             |   |   - commit_reconciliation_    |  |
|  |    statements, calendar)          |   |       batch                   |  |
|  |                                   |   |   - run_consequence_engine    |  |
|  |                                   |   |   - simulate_purchase_decision|  |
|  |                                   |   |   - undo_last_commit          |  |
|  +-----------------------------------+   +-------------------------------+  |
+------------------------------------------------------+----------------------+
                                                       |
                                                       v
+-----------------------------------------------------------------------------+
|                     DETERMINISTIC CORE & STATE (Python + SQLite)            |
|   [Reconciliation Engine]      [Consequence Engine]       [finguard.db]     |
|   - Priority Matching          - Daily Burn Rate          - 5 Core Tables   |
|   - Refund SLA Sweep           - Safe-to-Spend Today      - Audit Log & Undo|
+------------------------------------------------------+----------------------+
                                                       |
                                                       v
+-----------------------------------------------------------------------------+
|                         HOST CLIENT & HUMAN APPROVAL UI                     |
|   - Consequence Radar (Scenarios A-D)    - "Can I Buy This?" What-If Sim    |
|   - Interactive Approval Gate Modal      - Active Lifecycles Table          |
+-----------------------------------------------------------------------------+
```

---

## 4. Mathematical Formulas & Invariants

All calculations live strictly in `engine/consequences.py` and `engine/reconciler.py`:

### 4.1 Conservation of Funds Invariant
Checked before every SQLite commit. If violated, the transaction rolls back:
$$\text{MonthlyIncome} = \sum \text{FixedCommitments} + \sum \text{VariableAllocations} + \text{SavingsTarget} + \text{EmergencyBuffer}$$

### 4.2 Velocity and Month-End Spend Projection
- **Daily Burn Rate:** $V_{\text{cat}} = \lfloor S_{\text{cat}} / D_{\text{elapsed}} \rfloor$
- **Projected Month-End Spend:** $\hat{S}_{\text{cat}} = S_{\text{cat}} + (V_{\text{cat}} \times D_{\text{rem}}) + \text{PendingOutflows}_{\text{cat}}$
- **Projected Overrun:** $\text{Overrun}_{\text{cat}} = \max(0, \hat{S}_{\text{cat}} - B_{\text{cat}})$
- **Weekly Course Correction:** $\text{WeeklyTrim} = \lfloor (\text{Overrun}_{\text{cat}} \times 7) / D_{\text{rem}} \rfloor$

### 4.3 Safe-to-Spend Allowance
$$C_{\text{disp}} = \text{DatedCashBalance} - \text{UnpaidBills} - \text{PendingOrders} - \text{SavingsTarget} - \text{EmergencyBuffer}$$
$$S_{\text{today}} = \max\left(0, \left\lfloor \frac{C_{\text{disp}}}{D_{\text{rem}}} \right\rfloor\right)$$

The cash balance uses the latest statement on or before the reference date, adjusted for later
posted ledger activity through that date. Paid expenses are already in this balance and are
not deducted again. Unpaid calendar bills remain reserved after their due dates. If no statement
exists for the cycle, the dashboard explicitly labels its budget/ledger balance as an estimate.

### 4.4 Savings Erosion
$$\text{ProjectedSavings} = \max\left(0, \text{SavingsTarget} - \sum \text{Overrun}\right)$$

---

## 5. The 4 Showcase Demo Scenarios

The seeded profile features a salaried professional earning **₹60,000/month take-home** on **Day 18** (12 days remaining):

| Scenario | Input Event / Fixture | Deterministic Output |
| :--- | :--- | :--- |
| **A. Subscription Price Hike** | StreamFlix renewal notice: ₹499 $\rightarrow$ ₹699/mo | Detects **+₹200/mo (+₹2,400/yr)**. Flags subscription budget overrun and drafts formal cancellation notice. |
| **B. Stacked Upcoming Outflows** | 3 pending orders (₹2,200 + ₹1,800 + ₹3,400 = ₹7,400) + ₹15,000 rent due on 28th | Outflows (₹22,400) collide with liquid cash (₹19,300), detecting an imminent **₹3,100 cash shortfall** before payday. |
| **C. Category Velocity Overrun** | Entertainment budget ₹4,000; ₹3,600 spent by day 18 | Projects **₹6,000** month-end spend (+₹2,000 overrun), cutting planned ₹12,000 savings to **₹10,000**. Suggests trimming **₹1,166.66/week**. |
| **D. Missing Refund Lifecycle** | Amazon order ₹2,499 cancelled on day 5, no credit on day 18 | Status: `REFUND_OVERDUE` (past 5-day SLA). Tracks ₹2,499 missing cash and drafts escalation email citing bank UTR `431829103941`. |

---

## 6. Repository Layout

```text
FinGuardMCP/
├── pyproject.toml              # Dependencies: fastmcp, pydantic, streamlit, pytest
├── requirements.txt            # Pip dependencies
├── README.md                   # Complete architectural and execution guide
├── Hackathon context.md        # Original specification and hackathon context
├── db/
│   ├── schema.sql              # SQLite DDL: 5 tables (integer paise strictly enforced)
│   ├── seed_demo.py            # Reproducible seed script for the ₹60k persona
│   └── finguard.db             # Local SQLite database
├── fixtures/
│   ├── emails.json             # Orders, cancellations, subscription hike notice
│   ├── bank_statements.csv     # Bank debits, credits, UPI UTR rows, personal VPA
│   └── calendar_bills.json     # Upcoming rent (₹15,000), utility due dates
├── engine/
│   ├── models.py               # Strict Pydantic v2 data contracts
│   ├── reconciler.py           # Priority matcher, refund SLA sweep, 2-phase guarded commit
│   └── consequences.py         # Deterministic velocity, safe-to-spend, 4 detectors
├── servers/
│   ├── finance_sources_mcp.py  # Server 1: Stateless ingestion adapters & draft generator
│   └── finguard_ledger_mcp.py  # Server 2: Stateful ledger, guarded reconciliation, what-if sim
├── app/
│   └── dashboard.py            # Streamlit UI: Visual gauges, radar alerts, what-if simulator
└── tests/
    └── test_invariants.py      # Automated invariant tests (100% passing)
```

---

## 7. Quickstart & Execution Guide

### Prerequisites
- Python 3.10+
- Installed packages: `fastmcp`, `pydantic`, `streamlit`, `pytest`

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Seed the Database
Initializes `db/finguard.db` and populates the 4 demo scenarios:
```bash
python db/seed_demo.py
```

### Step 3: Run the Invariant Test Suite
Verifies paise conservation rollback, idempotency, refund SLA breach, guarded tokens, and Scenario C arithmetic:
```bash
python -m pytest tests/test_invariants.py -v
```

### Step 4: Run MCP Servers Standalone
Connect with any MCP client (e.g., Claude Desktop, Antigravity, or MCP Inspector):
```bash
# Server 1: Finance Sources Adapter
python servers/finance_sources_mcp.py

# Server 2: FinGuard Ledger & Consequence Engine
python servers/finguard_ledger_mcp.py
```

#### MCP Client Configuration
Add to your MCP settings configuration:
```json
{
  "mcpServers": {
    "finance_sources": {
      "command": "python",
      "args": ["C:/FinGaurdMCP/servers/finance_sources_mcp.py"]
    },
    "finguard_ledger": {
      "command": "python",
      "args": ["C:/FinGaurdMCP/servers/finguard_ledger_mcp.py"]
    }
  }
}
```

### Step 5: Launch the Interactive Dashboard
Launch the visual Streamlit dashboard:
```bash
streamlit run app/dashboard.py
```

The dashboard includes a financial overview with savings progress, ranked consequence alerts,
category spending projections, and scheduled fixed allocations. Use the sidebar to open the
purchase simulator, search and export transaction lifecycles, or preview and approve source
reconciliation. Refund and cancellation drafts can be reviewed, edited, and downloaded locally.

The workspace is explicitly a demo snapshot. Its reference date defaults to November 18, 2026;
changing it uses balances and transaction events through that date, excluding later spending.
Linked events reconstruct lifecycle status; seeded records without event history use their
recorded transaction dates. The simulator shares the overview's cash and bill calculations,
separates existing deficits from additional purchase impact, and shows the full cash breakdown.
To use an isolated ledger, set `FINGUARD_DB_PATH` to its absolute path before starting Streamlit.

Run the engine and dashboard workflow checks together with `python -m pytest tests -q`.

---

## 8. Judge Q&A Cheat Sheet

**Q: Why use MCP instead of a single script or LLM prompt?**  
*A: Financial data lives across disparate, sensitive silos (emails, bank statements, calendars). MCP provides clear security boundaries: stateless, read-only ingestion adapters (`finance_sources_mcp`) are separated from the stateful accounting ledger (`finguard_ledger_mcp`). Furthermore, MCP enforces a typed `preview -> approval_token -> commit` protocol so an LLM cannot silently alter financial records.*

**Q: What happens if the LLM hallucinates numbers or balances?**  
*A: It has no effect on the ledger. All amounts are parsed into integer paise, checked against the Conservation-of-Funds invariant before any SQLite commit, and projected using closed-form velocity equations in Python. The LLM acts solely as a conversational coordinator.*

**Q: How do you prevent double-counting when both an order confirmation and bank debit arrive?**  
*A: Every raw event is hashed with SHA-256 for idempotency. Events pass through a priority matcher (`order_ref` $\rightarrow$ `upi_utr` $\rightarrow$ `merchant + amount + ±5-day window`) to advance a single unified `TransactionLifecycle` (`ORDERED -> DEBITED -> CANCELLED -> REFUND_PENDING -> REFUNDED`).*

**Q: Can a user recover from an accidental commit?**  
*A: Yes. Every commit atomically snapshots before/after states in `audit_log`. The `undo_last_commit()` tool restores the previous database state with zero data loss.*
