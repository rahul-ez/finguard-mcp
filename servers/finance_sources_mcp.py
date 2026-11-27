"""
FinGuard MCP Server 1: finance_sources_mcp
Role: Stateless ingestion and action adapters.
Reads authorized local/sandboxed fixtures (emails.json, bank_statements.csv, calendar_bills.json).
Extracts structured events with confidence scores.
Prepares human-approved follow-up drafts (e.g. overdue-refund escalation email).
NEVER sends anything externally.
"""

import os
import sys
import json
import csv
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP
from engine.models import (
    NormalizedEvent,
    DraftPackage,
    paise_to_rupees,
)

mcp = FastMCP("finance_sources")

FIXTURES_DIR = PROJECT_ROOT / "fixtures"
DB_PATH = Path(os.environ.get("FINGUARD_DB_PATH", PROJECT_ROOT / "db" / "finguard.db")).resolve()


def sha256_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# =============================================================================
# RESOURCES (Read-Only)
# =============================================================================

@mcp.resource("sources://emails/unread")
def get_unread_emails() -> str:
    """Returns unread e-commerce orders, cancellations and subscription renewal notices."""
    emails_file = FIXTURES_DIR / "emails.json"
    if not emails_file.exists():
        return "[]"
    with open(emails_file, "r", encoding="utf-8") as f:
        return f.read()


@mcp.resource("sources://statements/latest")
def get_latest_statement() -> str:
    """Returns raw bank statement rows including UPI references and debit/credits."""
    stmt_file = FIXTURES_DIR / "bank_statements.csv"
    if not stmt_file.exists():
        return ""
    with open(stmt_file, "r", encoding="utf-8") as f:
        return f.read()


@mcp.resource("sources://calendar/upcoming_bills")
def get_upcoming_bills() -> str:
    """Returns scheduled fixed commitments, rent, and EMIs for the next 30 days."""
    cal_file = FIXTURES_DIR / "calendar_bills.json"
    if not cal_file.exists():
        return "[]"
    with open(cal_file, "r", encoding="utf-8") as f:
        return f.read()


# =============================================================================
# TOOLS
# =============================================================================

@mcp.tool()
def extract_and_normalize_events(source_filter: str = "all") -> List[NormalizedEvent]:
    """
    Parses raw emails and bank statement rows into typed NormalizedEvent instances with
    deterministic SHA-256 idempotency hashes. Flags personal/unmapped UPI VPAs for user classification.
    """
    normalized: List[NormalizedEvent] = []

    # 1. Process Emails
    if source_filter in ("all", "emails"):
        emails_file = FIXTURES_DIR / "emails.json"
        if emails_file.exists():
            with open(emails_file, "r", encoding="utf-8") as f:
                emails = json.load(f)

            for em in emails:
                subject = em.get("subject", "")
                body = em.get("body", "")
                snippet = f"{subject} | {body}"
                h = sha256_hash(f"email_{em.get('id', '')}_{snippet}")

                if "Order Confirmation" in subject or "Order Placed" in subject or "Order Received" in subject:
                    event_type = "ORDER_PLACED"
                elif "Cancelled" in subject or "Cancellation" in subject:
                    event_type = "ORDER_CANCELLED"
                elif "Price Revision" in subject or "Renewal" in subject:
                    event_type = "SUBSCRIPTION_RENEWAL"
                else:
                    event_type = "ORDER_PLACED"

                amt = em.get("amount_paise") or em.get("current_amount_paise") or 0

                normalized.append(
                    NormalizedEvent(
                        external_ref_hash=h,
                        source_type="gmail_receipt",
                        event_type=event_type,
                        merchant_normalized=em.get("merchant"),
                        category=em.get("category", "shopping"),
                        amount_paise=amt,
                        event_timestamp=em.get("timestamp", datetime.now().isoformat()),
                        order_ref=em.get("order_ref"),
                        raw_snippet=snippet,
                        confidence=0.95,
                        requires_user_classification=False,
                    )
                )

    # 2. Process Bank Statements
    if source_filter in ("all", "statements"):
        stmt_file = FIXTURES_DIR / "bank_statements.csv"
        if stmt_file.exists():
            with open(stmt_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Ignore salary credit
                    if "SALARY" in row["description"].upper():
                        continue

                    h = sha256_hash(f"stmt_{row['transaction_id']}_{row['description']}")
                    desc = row["description"]
                    amt_paise = int(row["amount_paise"])
                    is_credit = row["type"].upper() == "CREDIT"
                    event_type = "BANK_REFUND" if is_credit else "BANK_DEBIT"

                    # Check for unmapped personal UPI handle (@okicici, @okaxis, @ybl, etc.)
                    is_personal_upi = any(
                        vpa in desc.lower()
                        for vpa in ["@okicici", "@okaxis", "@ybl", "@paytm", "@ibl"]
                    ) and "AMAZON" not in desc.upper() and "SWIGGY" not in desc.upper() and "PVR" not in desc.upper()

                    merchant = None
                    category = None
                    if "AMAZON" in desc.upper():
                        merchant = "Amazon India"
                        category = "shopping"
                    elif "SWIGGY" in desc.upper():
                        merchant = "Swiggy"
                        category = "food_groceries"
                    elif "BLINKIT" in desc.upper():
                        merchant = "Blinkit"
                        category = "food_groceries"
                    elif "PVR" in desc.upper():
                        merchant = "PVR Cinemas"
                        category = "entertainment"
                    elif "UBER" in desc.upper():
                        merchant = "Uber"
                        category = "transport"
                    elif is_personal_upi:
                        merchant = desc.split("/")[1] if "/" in desc else desc
                        category = None  # Requires human classification

                    normalized.append(
                        NormalizedEvent(
                            external_ref_hash=h,
                            source_type="bank_statement",
                            event_type=event_type,
                            merchant_normalized=merchant,
                            category=category,
                            amount_paise=amt_paise,
                            event_timestamp=f"{row['date']}T12:00:00",
                            upi_utr=row.get("upi_utr") or None,
                            raw_snippet=desc,
                            confidence=0.85 if is_personal_upi else 0.98,
                            requires_user_classification=is_personal_upi,
                        )
                    )

    return normalized


@mcp.tool()
def draft_action_package(action_kind: str, lifecycle_id: str, approval_token: str) -> DraftPackage:
    """
    Generates a ready-to-send dispute escalation email citing exact order_ref, UPI UTR, debit date,
    and overdue SLA days, or a subscription cancellation notice.
    NEVER sends externally. Returns draft text for human review.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM transaction_lifecycles WHERE lifecycle_id = ?", (lifecycle_id,))
    lc = cursor.fetchone()

    # If not found in lifecycles, check subscriptions
    sub = None
    if not lc:
        cursor.execute(
            "SELECT * FROM subscriptions WHERE sub_id = ? OR merchant_normalized LIKE ?",
            (lifecycle_id, f"%{lifecycle_id}%"),
        )
        sub = cursor.fetchone()

    conn.close()

    if not lc and not sub:
        return DraftPackage(
            action_kind="refund_escalation",
            lifecycle_id=lifecycle_id,
            draft_text=f"Error: Record {lifecycle_id} not found in database.",
            sent=False,
        )

    merchant = lc["merchant_normalized"] if lc else sub["merchant_normalized"]
    amt_str = paise_to_rupees(lc["expected_amount_paise"]) if lc else paise_to_rupees(sub["current_amount_paise"])
    order_ref = (lc["order_ref"] if lc and lc["order_ref"] else "N/A")
    upi_utr = (lc["upi_utr"] if lc and lc["upi_utr"] else "N/A")
    expected_date = (lc["expected_refund_by"] if lc and lc["expected_refund_by"] else "N/A")

    if action_kind == "refund_escalation":
        subject = f"URGENT: Missing Refund Escalation for Order #{order_ref} - {amt_str}"
        body = f"""To: {merchant.lower().replace(' ', '')}-grievance@escalations.in
Subject: {subject}

Dear Grievance Officer,

I am writing to urgently escalate an unresolved refund for cancelled order #{order_ref}.

Transaction & Payment Lifecycle Details:
  - Merchant: {merchant}
  - Order Reference: {order_ref}
  - Amount Debited: {amt_str}
  - Bank UPI Reference / UTR: {upi_utr}
  - Order Cancellation Date: {lc['created_at'][:10]}
  - Promised Refund SLA Due Date: {expected_date}

As per RBI turnaround time (TAT) guidelines and your platform's refund policy, the credit was due by {expected_date}. As of today, the funds have not been credited to my bank account.

Please immediately initiate an electronic credit or provide the Bank Reference Number / NPCI Retrieval Reference Number (RRN) to trace this missing credit.

Sincerely,
Account Holder
(Draft generated by FinGuard MCP - Requires human verification before sending)
"""
        return DraftPackage(
            action_kind="refund_escalation",
            lifecycle_id=lifecycle_id,
            draft_text=body.strip(),
            recipient=f"support@{merchant.lower().replace(' ', '')}.com",
            subject=subject,
            sent=False,
        )

    elif action_kind == "subscription_cancel":
        subject = f"Notice of Subscription Cancellation & Downgrade - {merchant}"
        body = f"""To: support@{merchant.lower().replace(' ', '')}.com
Subject: {subject}

Dear Support Team,

Please treat this communication as formal notice to cancel my recurring subscription for {merchant} effective immediately, prior to the upcoming renewal date.

Reason: Unplanned subscription price revision.

Please confirm cancellation of auto-debit / mandate on my card/UPI handle and confirm no further amounts of {amt_str} will be charged.

Sincerely,
Subscriber
"""
        return DraftPackage(
            action_kind="subscription_cancel",
            lifecycle_id=lifecycle_id,
            draft_text=body.strip(),
            recipient=f"support@{merchant.lower().replace(' ', '')}.com",
            subject=subject,
            sent=False,
        )

    return DraftPackage(
        action_kind="refund_escalation",
        lifecycle_id=lifecycle_id,
        draft_text=f"Unknown action_kind: {action_kind}",
        sent=False,
    )


if __name__ == "__main__":
    mcp.run()
