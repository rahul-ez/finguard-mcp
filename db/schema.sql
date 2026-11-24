-- FinGuard MCP Database Schema (finguard.db, SQLite)
-- All monetary amounts stored as INTEGER PAISE (₹1 = 100 paise). Float money is strictly forbidden.

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
    confidence REAL NOT NULL                 -- parser confidence 0.0-1.0
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
