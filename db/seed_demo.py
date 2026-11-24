"""
FinGuard MCP - Seed Demo Script
Populates finguard.db with the realistic ₹60,000/month salaried professional profile
and seeds all 4 showcase hackathon scenarios:
  - Scenario A: StreamFlix subscription price hike (+₹200/mo, +₹2,400/yr)
  - Scenario B: Stacked upcoming orders (₹7,400) colliding with ₹15,000 rent (₹3,100 shortfall)
  - Scenario C: Entertainment velocity overrun (₹3,600 spent by day 18 -> ₹6,000 projected, cuts savings)
  - Scenario D: Amazon ₹2,499 cancelled order with missing refund (REFUND_OVERDUE)
"""

import os
import sqlite3
import hashlib
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent
DB_PATH = DB_DIR / "finguard.db"
SCHEMA_PATH = DB_DIR / "schema.sql"


def sha256_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed_database(db_file: Path = DB_PATH):
    print(f"Connecting to database at: {db_file}")
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    # 1. Initialize schema
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    cursor.executescript(schema_sql)

    # Clean existing rows for reproducible demo run
    tables = [
        "budget_profiles",
        "category_allocations",
        "transaction_lifecycles",
        "raw_events",
        "subscriptions",
        "audit_log",
    ]
    for tbl in tables:
        cursor.execute(f"DELETE FROM {tbl}")

    # 2. Insert Budget Profile (Conservation of funds invariant)
    # Income (60k) == Fixed (16k) + Variable (30k) + Savings (12k) + Buffer (2k)
    cursor.execute(
        """
        INSERT INTO budget_profiles (month_key, monthly_income_paise, savings_target_paise, emergency_buffer_paise, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("2026-11", 6000000, 1200000, 200000, "2026-11-01T00:00:00"),
    )

    # 3. Category Allocations
    allocations = [
        ("alloc_rent", "2026-11", "rent_utilities", 1500000, 1, 28),   # Fixed rent due on 28th
        ("alloc_subs", "2026-11", "subscriptions", 100000, 1, 20),     # Fixed subscriptions
        ("alloc_food", "2026-11", "food_groceries", 1400000, 0, None), # Variable
        ("alloc_trans", "2026-11", "transport", 400000, 0, None),       # Variable
        ("alloc_ent", "2026-11", "entertainment", 400000, 0, None),     # Variable (4k budget, 3.6k spent)
        ("alloc_shop", "2026-11", "shopping", 800000, 0, None),         # Variable
    ]
    cursor.executemany(
        """
        INSERT INTO category_allocations (allocation_id, month_key, category, allocated_paise, is_fixed, due_day)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        allocations,
    )

    # 4. Subscriptions (Scenario A)
    subscriptions = [
        ("sub_streamflix", "StreamFlix", "subscriptions", 49900, 69900, "monthly", "2026-11-20", "flagged_hike"),
        ("sub_spotify", "Spotify", "subscriptions", 50000, 50000, "monthly", "2026-12-11", "active"),
    ]
    cursor.executemany(
        """
        INSERT INTO subscriptions (sub_id, merchant_normalized, category, previous_amount_paise, current_amount_paise, billing_cycle, next_billing_date, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        subscriptions,
    )

    # 5. Transaction Lifecycles
    # Reference date is 2026-11-18.
    lifecycles = [
        # Scenario D: Missing Refund Lifecycle (Amazon ₹2,499, ordered day 3, cancelled day 5, refund due day 10, overdue by 8 days)
        (
            "lc_amz_2499_01",
            "Amazon India",
            "shopping",
            249900,
            249900,  # Cash was debited and is currently missing
            "REFUND_OVERDUE",
            "AMZ-IND-8839210-991",
            "431829103941",
            "2026-11-10",
            0,
            "2026-11-03T10:15:00",
            "2026-11-18T00:00:00",
        ),
        # Scenario B: 3 Stacked Pending Orders (COD / BNPL due 25th)
        (
            "lc_ord_bag_01",
            "Urban Tribe",
            "shopping",
            220000,
            0,
            "ORDERED",
            "UT-99182",
            None,
            None,
            0,
            "2026-11-16T18:45:00",
            "2026-11-16T18:45:00",
        ),
        (
            "lc_ord_buds_02",
            "Boat Audio",
            "shopping",
            180000,
            0,
            "ORDERED",
            "BT-55412",
            None,
            None,
            0,
            "2026-11-17T11:20:00",
            "2026-11-17T11:20:00",
        ),
        (
            "lc_ord_snkr_03",
            "Myntra",
            "shopping",
            340000,
            0,
            "ORDERED",
            "MYN-449102",
            None,
            None,
            0,
            "2026-11-17T21:10:00",
            "2026-11-17T21:10:00",
        ),
        # Scenario C: Entertainment Spends totaling ₹3,600 (360,000 paise) across days 1-18
        (
            "lc_ent_pvr_01",
            "PVR Cinemas",
            "entertainment",
            120000,
            120000,
            "DEBITED",
            None,
            "332190812903",
            None,
            0,
            "2026-11-06T19:00:00",
            "2026-11-06T19:00:00",
        ),
        (
            "lc_ent_bms_02",
            "BookMyShow",
            "entertainment",
            80000,
            80000,
            "DEBITED",
            None,
            "332190812906",
            None,
            0,
            "2026-11-09T15:30:00",
            "2026-11-09T15:30:00",
        ),
        (
            "lc_ent_game_03",
            "Gaming Lounge",
            "entertainment",
            160000,
            160000,
            "DEBITED",
            None,
            "332190812909",
            None,
            0,
            "2026-11-13T20:00:00",
            "2026-11-13T20:00:00",
        ),
        # Other settled categories (Food, Transport, Bills)
        (
            "lc_util_elec_01",
            "BESCOM",
            "rent_utilities",
            250000,
            250000,
            "DEBITED",
            None,
            None,
            None,
            0,
            "2026-11-02T10:00:00",
            "2026-11-02T10:00:00",
        ),
        (
            "lc_util_broad_01",
            "Airtel",
            "rent_utilities",
            150000,
            150000,
            "DEBITED",
            None,
            None,
            None,
            0,
            "2026-11-02T10:30:00",
            "2026-11-02T10:30:00",
        ),
        (
            "lc_sub_spot_01",
            "Spotify",
            "subscriptions",
            50000,
            50000,
            "DEBITED",
            None,
            None,
            None,
            0,
            "2026-11-11T09:00:00",
            "2026-11-11T09:00:00",
        ),
        # Food & groceries (₹7,200 spent on ₹14,000 budget, on-track)
        (
            "lc_food_swiggy_01",
            "Swiggy",
            "food_groceries",
            45000,
            45000,
            "DEBITED",
            None,
            "332190812901",
            None,
            0,
            "2026-11-04T13:00:00",
            "2026-11-04T13:00:00",
        ),
        (
            "lc_food_blink_01",
            "Blinkit",
            "food_groceries",
            125000,
            125000,
            "DEBITED",
            None,
            "332190812902",
            None,
            0,
            "2026-11-05T08:30:00",
            "2026-11-05T08:30:00",
        ),
        (
            "lc_food_bb_01",
            "BigBasket",
            "food_groceries",
            250000,
            250000,
            "DEBITED",
            None,
            "332190812905",
            None,
            0,
            "2026-11-08T11:00:00",
            "2026-11-08T11:00:00",
        ),
        (
            "lc_food_im_01",
            "Swiggy Instamart",
            "food_groceries",
            100000,
            100000,
            "DEBITED",
            None,
            "332190812908",
            None,
            0,
            "2026-11-12T16:00:00",
            "2026-11-12T16:00:00",
        ),
        (
            "lc_food_nb_01",
            "Natures Basket",
            "food_groceries",
            200000,
            200000,
            "DEBITED",
            None,
            "332190812911",
            None,
            0,
            "2026-11-15T18:00:00",
            "2026-11-15T18:00:00",
        ),
        # Transport (₹2,000 spent on ₹4,000 budget, on-track)
        (
            "lc_trans_uber_01",
            "Uber",
            "transport",
            60000,
            60000,
            "DEBITED",
            None,
            "332190812904",
            None,
            0,
            "2026-11-07T09:30:00",
            "2026-11-07T09:30:00",
        ),
        (
            "lc_trans_ola_01",
            "Ola",
            "transport",
            80000,
            80000,
            "DEBITED",
            None,
            "332190812910",
            None,
            0,
            "2026-11-14T20:30:00",
            "2026-11-14T20:30:00",
        ),
        (
            "lc_trans_metro_01",
            "Bangalore Metro",
            "transport",
            60000,
            60000,
            "DEBITED",
            None,
            "332190812912",
            None,
            0,
            "2026-11-16T18:30:00",
            "2026-11-16T18:30:00",
        ),
        # One-time medical expense anomaly (is_one_time_spike=1, does not distort recurring velocity)
        (
            "lc_med_apollo_01",
            "Apollo Pharmacy",
            "shopping",
            1990100,
            1990100,
            "DEBITED",
            None,
            "332190812907",
            None,
            1,  # one-time spike
            "2026-11-10T14:00:00",
            "2026-11-10T14:00:00",
        ),
    ]

    cursor.executemany(
        """
        INSERT INTO transaction_lifecycles (
            lifecycle_id, merchant_normalized, category, expected_amount_paise,
            settled_net_paise, lifecycle_status, order_ref, upi_utr,
            expected_refund_by, is_one_time_spike, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        lifecycles,
    )

    # 6. Raw Events (Linked to lifecycles with SHA-256 idempotency hash)
    raw_events = [
        # Amazon Order Placed & Debited & Cancelled
        (
            "rev_amz_ord_01",
            "lc_amz_2499_01",
            "gmail_receipt",
            sha256_hash("email_001_amz_order_249900"),
            "ORDER_PLACED",
            249900,
            "2026-11-03T10:15:00",
            "Order Confirmation #AMZ-IND-8839210-991 INR 2499",
            1.0,
        ),
        (
            "rev_amz_deb_01",
            "lc_amz_2499_01",
            "bank_statement",
            sha256_hash("stmt_amz_debit_431829103941"),
            "BANK_DEBIT",
            249900,
            "2026-11-03T10:16:00",
            "UPI/AMAZON SELLER SERVICES/431829103941 DEBIT INR 2499",
            1.0,
        ),
        (
            "rev_amz_can_01",
            "lc_amz_2499_01",
            "gmail_receipt",
            sha256_hash("email_002_amz_cancel_249900"),
            "ORDER_CANCELLED",
            249900,
            "2026-11-05T14:30:00",
            "Order Cancelled #AMZ-IND-8839210-991 Refund INR 2499 by 10 Nov 2026",
            1.0,
        ),
        # 3 Pending Orders
        (
            "rev_ord_bag_01",
            "lc_ord_bag_01",
            "gmail_receipt",
            sha256_hash("email_004_bag_220000"),
            "ORDER_PLACED",
            220000,
            "2026-11-16T18:45:00",
            "Order #UT-99182 Urban Tribe Bag INR 2200",
            1.0,
        ),
        (
            "rev_ord_buds_02",
            "lc_ord_buds_02",
            "gmail_receipt",
            sha256_hash("email_005_buds_180000"),
            "ORDER_PLACED",
            180000,
            "2026-11-17T11:20:00",
            "Order #BT-55412 boAt Earbuds INR 1800",
            1.0,
        ),
        (
            "rev_ord_snkr_03",
            "lc_ord_snkr_03",
            "gmail_receipt",
            sha256_hash("email_006_snkr_340000"),
            "ORDER_PLACED",
            340000,
            "2026-11-17T21:10:00",
            "Order #MYN-449102 Nike Shoes INR 3400",
            1.0,
        ),
        # Subscription Hike Notice
        (
            "rev_sub_hike_01",
            None,
            "gmail_receipt",
            sha256_hash("email_003_streamflix_hike_69900"),
            "SUBSCRIPTION_RENEWAL",
            69900,
            "2026-11-12T09:00:00",
            "StreamFlix Premium rate increase to INR 699 from 20 Nov 2026",
            1.0,
        ),
    ]

    cursor.executemany(
        """
        INSERT INTO raw_events (
            event_id, lifecycle_id, source_type, external_ref_hash,
            event_type, amount_paise, event_timestamp, raw_snippet, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        raw_events,
    )

    conn.commit()
    conn.close()
    print("Database finguard.db successfully initialized and seeded with 4 showcase scenarios!")


if __name__ == "__main__":
    seed_database()
