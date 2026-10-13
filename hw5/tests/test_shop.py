"""Meaningful money-boundary tests use disposable DB copies, never the desk DB."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_server"))
from shop import ORIGINAL_PATH, ORIGINAL_SHA256, Shop, ShopError, safe_call

TOKEN = "test-only-human-approval-secret-123456789"


@pytest.fixture
def shop(tmp_path, monkeypatch):
    monkeypatch.setenv("HUMAN_APPROVAL_TOKEN", TOKEN)
    original = tmp_path / "original.db"
    working = tmp_path / "working.db"
    shutil.copy2(ORIGINAL_PATH, original)
    shutil.copy2(original, working)
    return Shop(working, original, tmp_path / "audit.jsonl")


def approve(shop, request):
    return shop.approve_payment(request["approval"]["id"], "Test human", TOKEN)


def test_initial_database_facts_and_no_prepare_cash_change(shop):
    snapshot = shop.get_shop_snapshot()
    assert snapshot["desk_date"] == "2026-08-31"
    assert snapshot["checking"]["balance"] == 3400
    assert snapshot["available_for_purchase"] == 160
    assert shop.get_inventory("CC-HOOD-NAVY", "M")["items"][0]["qty"] == 8
    request = shop.prepare_payment(101, "invoice", 501)
    assert request["approval"]["amount"] == 840
    assert shop.get_shop_snapshot()["checking"]["balance"] == 3400
    assert shop.get_shop_snapshot()["payments"] == []


def test_forged_unknown_and_wrong_token_fail_without_money_mutation(shop):
    request = shop.prepare_payment(101, "invoice", 501)
    result = safe_call(shop.approve_payment, request["approval"]["id"], "Test", "wrong")
    assert result["error"]["code"] == "human_approval_required"
    assert safe_call(shop.approve_payment, "invented", "Test", TOKEN)["error"]["code"] == "not_found"
    with shop.connect() as conn:
        conn.execute("DROP TRIGGER immutable_approval_request")
        conn.execute("UPDATE approval_requests SET signature='forged'")
    assert safe_call(shop.approve_payment, request["approval"]["id"], "Test", TOKEN)["error"]["code"] == "forged_approval"
    assert shop.get_shop_snapshot()["checking"]["balance"] == 3400
    assert shop.get_shop_snapshot()["payments"] == []


def test_immutable_and_stale_approval(shop):
    request = shop.prepare_payment(101, "invoice", 501)
    with pytest.raises(sqlite3.IntegrityError), shop.connect() as conn:
        conn.execute("UPDATE approval_requests SET amount=1")
    with shop.connect() as conn:
        conn.execute("UPDATE invoices SET amount=900 WHERE id=501")
    result = safe_call(shop.approve_payment, request["approval"]["id"], "Test", TOKEN)
    assert result["error"]["code"] == "stale_approval"
    assert shop.get_shop_snapshot()["checking"]["balance"] == 3400
    replacement = shop.prepare_payment(101, "invoice", 501)
    assert replacement["approval"]["id"] != request["approval"]["id"]
    assert safe_call(shop.approve_payment, request["approval"]["id"], "Test", TOKEN)["error"]["code"] == "stale_approval"
    assert approve(shop, replacement)["checking"]["balance"] == 2500


def test_tampered_displayed_fields_cannot_bypass_signed_payload(shop):
    request = shop.prepare_payment(101, "invoice", 501)
    with shop.connect() as conn:
        conn.execute("DROP TRIGGER immutable_approval_request")
        conn.execute("UPDATE approval_requests SET amount=1")
    result = safe_call(shop.approve_payment, request["approval"]["id"], "Test", TOKEN)
    assert result["error"]["code"] == "forged_approval"
    assert shop.get_shop_snapshot()["checking"]["balance"] == 3400


def test_invoice_rent_reconciliation_and_calendar_rollforward(shop):
    invoice = approve(shop, shop.prepare_payment(101, "invoice", 501))
    assert invoice["checking"]["balance"] == 2560
    assert shop.get_vendor_status(1)["blocked"] is False
    rent = approve(shop, shop.prepare_payment(102, "rent", 1))
    snapshot = shop.get_shop_snapshot()
    assert rent["checking"]["balance"] == 160
    assert snapshot["leases"][0]["next_due"] == "2026-10-02"
    assert [p["amount"] for p in snapshot["payments"]] == [840, 2400]
    assert snapshot["invoices"][0]["status"] == "paid"
    result = safe_call(shop.prepare_payment, 103, "purchase", 1, None, "CC-HOOD-NAVY", "M", 12)
    assert result["error"]["code"] == "insufficient_purchase_budget"
    assert snapshot["checking"]["balance"] == 3400 - sum(p["amount"] for p in snapshot["payments"])


def test_duplicate_clicks_are_transactionally_idempotent(shop):
    request = shop.prepare_payment(101, "invoice", 501)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: approve(shop, request), range(6)))
    assert all(result["ok"] for result in results)
    assert sum(not r["idempotent"] for r in results) == 1
    snapshot = shop.get_shop_snapshot()
    assert snapshot["checking"]["balance"] == 2560
    assert len(snapshot["payments"]) == 1


def test_blocked_purchase_and_insufficient_cash_leave_no_partial_updates(shop):
    result = safe_call(shop.prepare_payment, 103, "purchase", 1, None, "CC-HOOD-NAVY", "M", 12)
    assert result["error"]["code"] == "vendor_blocked"
    request = shop.prepare_payment(102, "rent", 1)
    with shop.connect() as conn:
        conn.execute("UPDATE cash_accounts SET balance=2000")
    assert safe_call(shop.approve_payment, request["approval"]["id"], "Test", TOKEN)["error"]["code"] == "insufficient_cash"
    snapshot = shop.get_shop_snapshot()
    assert snapshot["checking"]["balance"] == 2000
    assert snapshot["payments"] == []
    assert snapshot["leases"][0]["next_due"] == "2026-09-02"


def test_purchase_pending_delivery_does_not_add_stock(shop):
    approve(shop, shop.prepare_payment(101, "invoice", 501))
    approve(shop, shop.prepare_payment(102, "rent", 1))
    # A small affordable purchase in a disposable fixture exercises the generic
    # purchase path; it is not the homework's 12-hoodie recommendation.
    request = shop.prepare_payment(103, "purchase", 1, 154, "CC-HOOD-NAVY", "M", 7)
    result = approve(shop, request)
    assert result["checking"]["balance"] == 6
    assert result["purchase_order"]["status"] == "pending_delivery"
    assert result["purchase_order"]["estimated_arrival"] == "2026-09-05"
    assert shop.get_inventory("CC-HOOD-NAVY", "M")["items"][0]["qty"] == 8
    assert safe_call(shop.prepare_payment, 103, "purchase", 1, -1, "CC-HOOD-NAVY", "M", 1)["error"]["code"] == "invalid_money"
    assert safe_call(shop.prepare_payment, 103, "purchase", 1, None, "CC-HOOD-NAVY", "M", 7)["error"]["code"] == "excess_quantity"


def test_resolution_requires_payment_draft_and_honest_pending_restock(shop):
    shop.draft_message(101, "The small tee is unavailable; vendor follow-up is pending.")
    assert safe_call(shop.resolve_ticket, 101, "Restock pending.")["error"]["code"] == "unpaid_invoice"
    request = shop.prepare_payment(101, "invoice", 501)
    assert safe_call(shop.resolve_ticket, 101, "Restock pending.")["error"]["code"] == "pending_approval"
    approve(shop, request)
    assert safe_call(shop.resolve_ticket, 101, "Order delivered.")["error"]["code"] == "unsupported_fulfillment"
    assert safe_call(shop.resolve_ticket, 101, "Restock pending.")["error"]["code"] == "missing_restock_followup"
    followup = shop.schedule_restock(101)["restock"]
    assert followup["confirmed_qty"] is None
    assert followup["estimated_arrival"] == "2026-09-05"
    assert shop.get_inventory("CC-TEE-WHITE", "S")["items"][0]["qty"] == 0
    assert shop.resolve_ticket(101, "Paid invoice; restock follow-up scheduled, delivery pending. Draft saved.")["ticket"]["status"] == "resolved"


def test_reset_restores_supplied_values_preserves_external_audit_and_original(shop):
    before_sha = hashlib.sha256(shop.original_path.read_bytes()).hexdigest()
    request = shop.prepare_payment(101, "invoice", 501)
    approve(shop, request)
    old_audit = shop.audit_path.read_text()
    assert safe_call(shop.reset_database, "wrong")["error"]["code"] == "human_approval_required"
    result = shop.reset_database(TOKEN)
    assert result["checking"]["balance"] == 3400
    assert result["payments"] == [] and result["approvals"] == []
    assert all(t["status"] == "open" for t in result["tickets"])
    assert result["leases"][0]["next_due"] == "2026-09-02"
    assert shop.audit_path.read_text().startswith(old_audit)
    assert json.loads(shop.audit_path.read_text().splitlines()[-1])["action"] == "database_reset"
    assert hashlib.sha256(shop.original_path.read_bytes()).hexdigest() == before_sha == ORIGINAL_SHA256
