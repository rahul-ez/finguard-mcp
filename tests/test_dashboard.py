"""Exercise the financial workspace against disposable ledgers, never the user's DB."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db.seed_demo import seed_database
from engine.consequences import ConsequenceEngine
from engine.dashboard import dashboard_snapshot

APP = Path(__file__).resolve().parents[1] / "app" / "dashboard.py"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    db_path = tmp_path / "workspace.db"
    seed_database(db_path)
    monkeypatch.setenv("FINGUARD_DB_PATH", str(db_path))
    # AppTest shares imported modules between script runs.
    import servers.finance_sources_mcp as sources
    monkeypatch.setattr(sources, "DB_PATH", db_path)
    return db_path


def app():
    result = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not result.exception
    return result


def button(at, label):
    return next(item for item in at.button if item.label == label)


def test_overview_and_filtered_lifecycle_inspection(workspace):
    at = app()
    assert at.title[0].value == "Your financial overview"
    at.radio[0].set_value("Transactions").run()
    at.text_input[0].set_value("431829103941").run()
    assert not at.exception
    assert len(at.dataframe[0].value) == 1
    assert at.dataframe[0].value.iloc[0]["Merchant"] == "Amazon India"
    at.multiselect[0].set_value(["ORDERED"]).run()
    assert not at.dataframe
    assert "No transactions match" in at.info[0].value


def test_purchase_simulator_keeps_integer_money_and_does_not_write(workspace):
    original = workspace.read_bytes()
    at = app()
    at.radio[0].set_value("Purchase simulator").run()
    at.number_input[0].set_value(3500)
    at.text_input[0].set_value("Test merchant")
    button(at, "Check financial impact").click().run()
    assert not at.exception
    result = at.session_state["simulation"][1]
    assert result.amount_paise == 350000
    assert isinstance(result.amount_paise, int)
    assert result.merchant == "Test merchant"
    assert len(at.metric) == 3
    assert workspace.read_bytes() == original
    at.date_input[0].set_value(date(2026, 11, 20)).run()
    assert not at.metric  # A result from another reference date must not look current.


def test_refund_draft_survives_reruns_and_navigation(workspace):
    at = app()
    button(at, "Prepare refund follow-up").click().run()
    assert not at.exception
    assert "431829103941" in at.text_area[0].value
    at.text_area[0].set_value("My reviewed draft with reference 431829103941").run()
    at.radio[0].set_value("Transactions").run()
    at.radio[0].set_value("Overview").run()
    assert at.text_area[0].value == "My reviewed draft with reference 431829103941"


def test_preview_classification_approval_commit_and_rollback(workspace):
    with sqlite3.connect(workspace) as conn:
        before_lifecycles = conn.execute("SELECT * FROM transaction_lifecycles ORDER BY lifecycle_id").fetchall()
        before_events = conn.execute("SELECT * FROM raw_events ORDER BY event_id").fetchall()
    at = app()
    at.radio[0].set_value("Review & approve").run()
    button(at, "Preview reconciliation").click().run()
    assert not at.exception
    assert button(at, "Approve & commit").disabled
    assert at.checkbox[0].disabled  # Every ambiguous item must be explicitly classified.
    for selector in at.selectbox:
        if selector.label == "Choose a category":
            selector.set_value("shopping")
    at.run()
    at.checkbox[0].check().run()
    assert not button(at, "Approve & commit").disabled
    # Changing an approved classification invalidates the user's previous checkbox.
    next(s for s in at.selectbox if s.label == "Choose a category").set_value("transport").run()
    assert not at.checkbox[0].value
    assert button(at, "Approve & commit").disabled
    at.checkbox[0].check().run()
    button(at, "Approve & commit").click().run()
    assert not at.exception
    assert "Ledger updated" in at.success[0].value
    assert "current_preview" not in at.session_state
    with sqlite3.connect(workspace) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE reverted = 0").fetchone()[0] == 1
    at.checkbox[0].check().run()
    button(at, "Undo latest commit").click().run()
    assert not at.exception
    with sqlite3.connect(workspace) as conn:
        assert conn.execute("SELECT * FROM transaction_lifecycles ORDER BY lifecycle_id").fetchall() == before_lifecycles
        assert conn.execute("SELECT * FROM raw_events ORDER BY event_id").fetchall() == before_events


def test_reference_change_discards_an_approved_preview(workspace):
    at = app()
    at.radio[0].set_value("Review & approve").run()
    button(at, "Preview reconciliation").click().run()
    assert "current_preview" in at.session_state
    at.date_input[0].set_value(date(2026, 11, 19)).run()
    assert not at.exception
    assert "current_preview" not in at.session_state


def test_snapshot_projection_matches_engine_and_calendar(workspace):
    engine = ConsequenceEngine(str(workspace))
    snapshot = dashboard_snapshot(engine, "2026-11", "2026-11-18")
    entertainment = next(item for item in snapshot["categories"] if item["category"] == "entertainment")
    assert entertainment["projected_paise"] == 600000
    assert snapshot["savings_gap_paise"] == 200000
    assert snapshot["days_left"] == 12
    assert snapshot["fixed_paise"] == 1600000
    # February must have a different projection horizon from November.
    with sqlite3.connect(workspace) as conn:
        conn.execute("UPDATE budget_profiles SET month_key = '2028-02'")
        conn.execute("UPDATE category_allocations SET month_key = '2028-02'")
    feb = dashboard_snapshot(engine, "2028-02", "2028-02-18")
    assert feb["days_in_month"] == 29
    assert feb["days_left"] == 11
    ent = next(item for item in feb["categories"] if item["category"] == "entertainment")
    assert ent["settled_paise"] == 0
    assert ent["overrun_paise"] == 0
    assert not any(item.kind == "VELOCITY" and "entertainment" in item.message for item in feb["report"].alerts)


def test_missing_ledger_has_setup_state(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("FINGUARD_DB_PATH", str(missing))
    at = app()
    assert at.title[0].value == "Set up your financial workspace"
    assert not missing.exists()


def test_simulator_explains_the_selected_date_and_existing_deficit(workspace):
    at = app()
    at.radio[0].set_value("Purchase simulator").run()
    at.date_input[0].set_value(date(2026, 11, 3)).run()
    at.number_input[0].set_value(500)
    at.text_input[0].set_value("BigBasket")
    button(at, "Check financial impact").click().run()
    assert not at.exception
    assert not at.error
    assert at.metric[0].value == "₹888.96"
    assert at.metric[1].value == "₹870.44"
    at.date_input[0].set_value(date(2026, 11, 18)).run()
    button(at, "Check financial impact").click().run()
    assert not at.exception
    assert at.metric[0].label == "Cash remainder before"
    assert at.metric[0].value.startswith("-₹")
    assert "already underfunded" in at.error[0].value
    assert "₹500.00" in at.error[0].value
    assert len(at.dataframe[0].value) == 7


def test_overview_accepts_a_report_from_before_the_balance_metadata_update(workspace, monkeypatch):
    """Reproduce an older report object surviving a Streamlit module reload."""
    from types import SimpleNamespace

    original = ConsequenceEngine.run_consequence_engine

    def legacy_report(engine, *args, **kwargs):
        values = original(engine, *args, **kwargs).model_dump()
        values.pop("balance_basis")
        values.pop("balance_as_of")
        # Keep nested alert objects as they were in the original contract.
        values["alerts"] = original(engine, *args, **kwargs).alerts
        return SimpleNamespace(**values)

    monkeypatch.setattr(ConsequenceEngine, "run_consequence_engine", legacy_report)
    at = app()
    assert at.title[0].value == "Your financial overview"
    assert any("Statement balance as of" in item.value for item in at.markdown)
