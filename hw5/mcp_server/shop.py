"""Campus Customs' only shop-data and money boundary.

Business dates come from desk.date_today. Operational audit timestamps are UTC.
Money is calculated in integer cents and written back to the assignment's REAL
columns only at the SQLite boundary. Agents can prepare requests; only a caller
holding the backend's human-approval secret can execute or reset them.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "campus_customs_new.db"
ORIGINAL_PATH = ROOT / "data" / "campus_customs.db"
AUDIT_PATH = Path(__file__).resolve().parent / "audit_log.jsonl"
ORIGINAL_SHA256 = "23686a90d698f7fa6f901a3f0a9774db9cbb31e13151c9e5717275f3321f360a"


class ShopError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cents(value: Any) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if rounded != number:
            raise ValueError
        return int(rounded * 100)
    except (ValueError, TypeError, InvalidOperation):
        raise ShopError("invalid_money", "Amounts must be nonnegative, finite dollar values with at most two decimal places.")


def dollars(value: int) -> float:
    return float(Decimal(value) / 100)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def secret() -> str:
    value = os.environ.get("HUMAN_APPROVAL_TOKEN", "")
    if len(value) < 24:
        raise ShopError("approval_not_configured", "HUMAN_APPROVAL_TOKEN must be configured with at least 24 characters on the MCP server and backend.")
    return value


def authorize(token: str) -> None:
    if not isinstance(token, str) or not hmac.compare_digest(token, secret()):
        raise ShopError("human_approval_required", "This action requires the backend's human-approval credential.")


def signature(payload: str) -> str:
    return hmac.new(secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
 id TEXT PRIMARY KEY, ticket_id INTEGER NOT NULL, kind TEXT NOT NULL,
 ref_id INTEGER, amount REAL NOT NULL CHECK(amount>0), sku TEXT, size TEXT,
 qty INTEGER, vendor_id INTEGER, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','approved','superseded')), created_at TEXT NOT NULL,
 approved_by TEXT, approved_at TEXT, payment_id INTEGER,
 description TEXT NOT NULL, payload_json TEXT NOT NULL, signature TEXT NOT NULL,
 FOREIGN KEY(ticket_id) REFERENCES tickets(id));
CREATE TRIGGER IF NOT EXISTS immutable_approval_request
 BEFORE UPDATE OF ticket_id,kind,ref_id,amount,sku,size,qty,vendor_id,created_at,
 description,payload_json,signature ON approval_requests
 BEGIN SELECT RAISE(ABORT,'Prepared approval fields are immutable'); END;
CREATE TABLE IF NOT EXISTS message_drafts (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, message TEXT NOT NULL,
 created_at TEXT NOT NULL, FOREIGN KEY(ticket_id) REFERENCES tickets(id));
CREATE TABLE IF NOT EXISTS ticket_outcomes (
 ticket_id INTEGER PRIMARY KEY, outcome TEXT NOT NULL, resolved_at TEXT NOT NULL,
 FOREIGN KEY(ticket_id) REFERENCES tickets(id));
CREATE TABLE IF NOT EXISTS shop_actions (
 id INTEGER PRIMARY KEY, ticket_id INTEGER, action TEXT NOT NULL,
 details_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_orders (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, approval_id TEXT NOT NULL UNIQUE,
 vendor_id INTEGER NOT NULL, sku TEXT NOT NULL, size TEXT NOT NULL, qty INTEGER NOT NULL,
 amount REAL NOT NULL, ordered_date TEXT NOT NULL, estimated_arrival TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending_delivery');
CREATE TABLE IF NOT EXISTS restock_followups (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL UNIQUE, sku TEXT NOT NULL,
 size TEXT NOT NULL, vendor_id INTEGER NOT NULL, source_invoice_id INTEGER NOT NULL,
 requested_qty INTEGER NOT NULL, confirmed_qty INTEGER,
 scheduled_date TEXT NOT NULL, estimated_arrival TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'scheduled_follow_up', note TEXT NOT NULL);
"""


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Shop:
    def __init__(self, db_path: Path = DB_PATH, original_path: Path = ORIGINAL_PATH,
                 audit_path: Path = AUDIT_PATH):
        self.db_path, self.original_path, self.audit_path = map(Path, (db_path, original_path, audit_path))
        if self.db_path.resolve() == self.original_path.resolve():
            raise RuntimeError("The working database must differ from the protected original.")
        if not self.db_path.is_file() or not self.original_path.is_file():
            raise RuntimeError("Both supplied assignment databases must exist.")
        if hashlib.sha256(self.original_path.read_bytes()).hexdigest() != ORIGINAL_SHA256:
            raise RuntimeError("The protected original database no longer matches the supplied baseline.")
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, factory=ManagedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def rows(conn, sql, params=()):
        return [dict(row) for row in conn.execute(sql, params)]

    @staticmethod
    def one(conn, sql, params=(), label="record"):
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ShopError("not_found", f"No {label} matches the supplied identifier.")
        return dict(row)

    def ticket(self, conn, ticket_id):
        return self.one(conn, "SELECT * FROM tickets WHERE id=?", (ticket_id,), "ticket")

    def desk_date(self, conn):
        return self.one(conn, "SELECT date_today FROM desk LIMIT 1", label="desk date")["date_today"]

    def checking(self, conn):
        return self.one(conn, "SELECT * FROM cash_accounts WHERE name='checking'", label="checking account")

    def invoices(self, conn):
        today = self.desk_date(conn)
        result = self.rows(conn, "SELECT i.*,v.name AS vendor_name FROM invoices i JOIN vendors v ON v.id=i.vendor_id ORDER BY i.id")
        for row in result:
            row["overdue"] = row["status"] != "paid" and row["due_date"] < today
        return result

    def leases(self, conn):
        today = date.fromisoformat(self.desk_date(conn))
        result = self.rows(conn, "SELECT * FROM leases ORDER BY id")
        for row in result:
            row["due_in_days"] = (date.fromisoformat(row["next_due"]) - today).days
        return result

    def vendors(self, conn):
        result = self.rows(conn, "SELECT * FROM vendors ORDER BY id")
        for row in result:
            invoices = self.rows(conn, "SELECT * FROM invoices WHERE vendor_id=? AND status!='paid'", (row["id"],))
            row["blocked"] = bool(invoices)
            row["open_invoice_total"] = dollars(sum(cents(i["amount"]) for i in invoices))
        return result

    def approvals(self, conn, ticket_id=None):
        sql = "SELECT id,ticket_id,kind,ref_id,amount,sku,size,qty,vendor_id,status,created_at,approved_by,approved_at,payment_id,description,payload_json FROM approval_requests"
        approvals = self.rows(conn, sql + (" WHERE ticket_id=?" if ticket_id is not None else "") + " ORDER BY created_at,id", (() if ticket_id is None else (ticket_id,)))
        for approval in approvals:
            payload = json.loads(approval.pop("payload_json"))
            # The prepared payee is signed with the immutable amount/reference.
            # Legacy requests display a database-grounded name until the next reset.
            payee = payload.get("payee")
            if not payee:
                if approval["kind"] == "rent":
                    payee = self.one(conn, "SELECT landlord FROM leases WHERE id=?", (approval["ref_id"],))["landlord"]
                else:
                    payee = self.one(conn, "SELECT name FROM vendors WHERE id=?", (approval["vendor_id"],))["name"]
            approval["payee"] = payee
        return approvals

    def action(self, conn, ticket_id, name, details):
        stamp = utc_now()
        conn.execute("INSERT INTO shop_actions(ticket_id,action,details_json,created_at) VALUES(?,?,?,?)", (ticket_id, name, canonical(details), stamp))

    def audit(self, action, details):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = canonical({"recorded_at": utc_now(), "action": action, "details": details}) + "\n"
        # O_APPEND retains prior runs across a database reset.
        fd = os.open(self.audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, record.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def get_ticket(self, ticket_id):
        with self.connect() as conn:
            return {"ok": True, "ticket": self.ticket(conn, ticket_id),
                    "drafts": self.rows(conn, "SELECT * FROM message_drafts WHERE ticket_id=? ORDER BY id", (ticket_id,)),
                    "outcome": next(iter(self.rows(conn, "SELECT * FROM ticket_outcomes WHERE ticket_id=?", (ticket_id,))), None),
                    "restock": next(iter(self.rows(conn, "SELECT * FROM restock_followups WHERE ticket_id=?", (ticket_id,))), None),
                    "approvals": self.approvals(conn, ticket_id)}

    def list_tickets(self):
        with self.connect() as conn:
            return {"ok": True, "tickets": self.rows(conn, "SELECT * FROM tickets ORDER BY id")}

    def get_inventory(self, sku, size=None):
        with self.connect() as conn:
            items = self.rows(conn, "SELECT i.*,p.unit_cost,p.list_price FROM inventory i JOIN pricing p ON p.sku=i.sku WHERE i.sku=?" + (" AND i.size=?" if size else "") + " ORDER BY i.size", ((sku, size) if size else (sku,)))
            if not items:
                raise ShopError("not_found", "No inventory item matches that SKU and size.")
            return {"ok": True, "items": items}

    def get_financial_context(self, ticket_id):
        with self.connect() as conn:
            return {"ok": True, "ticket": self.ticket(conn, ticket_id), "desk_date": self.desk_date(conn),
                    "checking": self.checking(conn), "invoices": self.invoices(conn),
                    "vendors": self.vendors(conn), "leases": self.leases(conn),
                    "payments": self.rows(conn, "SELECT * FROM payments ORDER BY id"),
                    "available_for_purchase": dollars(self.available_purchase_cents(conn))}

    def get_shop_snapshot(self):
        with self.connect() as conn:
            actions = self.rows(conn, "SELECT * FROM shop_actions ORDER BY id DESC LIMIT 150")
            for action in actions:
                action["details"] = json.loads(action.pop("details_json"))
            return {"ok": True, "desk_date": self.desk_date(conn), "checking": self.checking(conn),
                    "invoices": self.invoices(conn), "leases": self.leases(conn), "vendors": self.vendors(conn),
                    "payments": self.rows(conn, "SELECT * FROM payments ORDER BY id"),
                    "approvals": self.approvals(conn), "tickets": self.rows(conn, "SELECT * FROM tickets ORDER BY id"),
                    "drafts": self.rows(conn, "SELECT * FROM message_drafts ORDER BY id"),
                    "outcomes": self.rows(conn, "SELECT * FROM ticket_outcomes ORDER BY ticket_id"),
                    "restocks": self.rows(conn, "SELECT * FROM restock_followups ORDER BY id"),
                    "purchase_orders": self.rows(conn, "SELECT * FROM purchase_orders ORDER BY id"),
                    "available_for_purchase": dollars(self.available_purchase_cents(conn)), "actions": actions}

    def get_vendor_status(self, vendor_id):
        with self.connect() as conn:
            vendor = self.one(conn, "SELECT * FROM vendors WHERE id=?", (vendor_id,), "vendor")
            invoices = self.rows(conn, "SELECT * FROM invoices WHERE vendor_id=? ORDER BY id", (vendor_id,))
            return {"ok": True, "vendor": vendor, "blocked": any(i["status"] != "paid" for i in invoices), "invoices": invoices}

    def available_purchase_cents(self, conn):
        """Reserve unpaid invoices and shop rent due within seven desk days."""
        today = date.fromisoformat(self.desk_date(conn))
        reserves = sum(cents(i["amount"]) for i in self.rows(conn, "SELECT amount FROM invoices WHERE status!='paid'"))
        reserves += sum(cents(lease["monthly_rent"]) for lease in self.rows(conn, "SELECT monthly_rent,next_due FROM leases") if date.fromisoformat(lease["next_due"]) <= today + timedelta(days=7))
        return max(0, cents(self.checking(conn)["balance"]) - reserves)

    def request_payload(self, conn, ticket_id, kind, ref_id, amount, sku, size, qty):
        ticket = self.ticket(conn, ticket_id)
        if ticket["status"] != "open":
            raise ShopError("ticket_closed", "Payments can only be prepared for an open ticket.")
        payload = {"ticket_id": ticket_id, "kind": kind, "ref_id": ref_id,
                   "sku": sku, "size": size, "qty": qty, "business_date": self.desk_date(conn)}
        if kind in ("invoice", "rent"):
            if amount is not None or sku is not None or size is not None or qty is not None:
                raise ShopError("invalid_request", "Invoice and rent amounts come from the database; do not supply amount or inventory fields.")
            if kind == "invoice":
                invoice = self.one(conn, "SELECT * FROM invoices WHERE id=?", (ref_id,), "invoice")
                if ticket["invoice_id"] != ref_id:
                    raise ShopError("wrong_reference", "The invoice must be linked to this ticket.")
                if invoice["status"] == "paid":
                    raise ShopError("already_paid", "This invoice is already paid.")
                payload.update(amount=invoice["amount"], vendor_id=invoice["vendor_id"], state=invoice,
                               payee=self.one(conn, "SELECT name FROM vendors WHERE id=?", (invoice["vendor_id"],), "vendor")["name"],
                               description=f"Invoice {ref_id}: {invoice['description'] or 'vendor payment'}")
            else:
                lease = self.one(conn, "SELECT * FROM leases WHERE id=?", (ref_id,), "lease")
                if ticket["lease_id"] != ref_id:
                    raise ShopError("wrong_reference", "The lease must be linked to this ticket.")
                if date.fromisoformat(lease["next_due"]) > date.fromisoformat(self.desk_date(conn)) + timedelta(days=7):
                    raise ShopError("not_due", "This rent period has already been paid or is not due within seven days.")
                payload.update(amount=lease["monthly_rent"], vendor_id=None, state=lease,
                               payee=lease["landlord"],
                               description=f"Rent for {lease['space_name']}, due {lease['next_due']}")
        elif kind == "purchase":
            if ticket["sku"] != sku or ticket["size"] != size:
                raise ShopError("wrong_item", "A purchase must match the requesting ticket's SKU and size.")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                raise ShopError("invalid_quantity", "Purchase quantity must be a positive integer.")
            item = self.one(conn, "SELECT i.*,p.unit_cost,p.list_price FROM inventory i JOIN pricing p ON p.sku=i.sku WHERE i.sku=? AND i.size=?", (sku, size), "inventory item")
            pending_qty = self.one(conn, "SELECT COALESCE(SUM(qty),0) AS qty FROM purchase_orders WHERE ticket_id=? AND status='pending_delivery'", (ticket_id,))["qty"]
            shortage = max(0, ticket["qty"] - item["qty"] - pending_qty)
            if qty > shortage:
                raise ShopError("excess_quantity", f"Purchase quantity cannot exceed the ticket's {shortage}-unit shortfall.")
            expected_vendor = 1 if sku.startswith(("CC-HOOD", "CC-TEE")) else 2
            if ref_id is None:
                ref_id = expected_vendor
                payload["ref_id"] = ref_id
            if ref_id != expected_vendor:
                raise ShopError("wrong_vendor", "The supplied vendor does not match this product category.")
            vendor = self.one(conn, "SELECT * FROM vendors WHERE id=?", (ref_id,), "vendor")
            unpaid = self.rows(conn, "SELECT * FROM invoices WHERE vendor_id=? AND status!='paid'", (ref_id,))
            if unpaid:
                raise ShopError("vendor_blocked", "Pay the vendor's open invoices before preparing a new purchase.")
            amount_cents = cents(item["unit_cost"]) * qty
            if amount is not None and cents(amount) != amount_cents:
                raise ShopError("amount_mismatch", "Purchase amount must equal quantity times the database unit cost.")
            if amount_cents > self.available_purchase_cents(conn):
                raise ShopError("insufficient_purchase_budget", f"This purchase costs ${dollars(amount_cents):.2f}; ${dollars(self.available_purchase_cents(conn)):.2f} remains after reserving unpaid invoices and rent due within seven days.")
            payload.update(amount=dollars(amount_cents), vendor_id=ref_id, state={"item": item, "vendor": vendor, "unpaid_invoices": unpaid},
                           payee=vendor["name"],
                           description=f"Purchase {qty} {sku} size {size}; stock remains pending until delivery")
        else:
            raise ShopError("invalid_kind", "Payment kind must be invoice, rent, or purchase.")
        if cents(payload["amount"]) <= 0:
            raise ShopError("invalid_money", "A payment must be positive.")
        if cents(payload["amount"]) > cents(self.checking(conn)["balance"]):
            raise ShopError("insufficient_cash", "Checking cannot cover this payment.")
        return payload

    def prepare_payment(self, ticket_id, kind, ref_id=None, amount=None, sku=None, size=None, qty=None):
        with self.transaction() as conn:
            payload = self.request_payload(conn, ticket_id, kind, ref_id, amount, sku, size, qty)
            payload_json = canonical(payload)
            existing = conn.execute("SELECT id FROM approval_requests WHERE payload_json=? AND status='pending'", (payload_json,)).fetchone()
            if existing:
                approval_id = existing["id"]
            else:
                approval_id = secrets.token_hex(12)
                conn.execute("UPDATE approval_requests SET status='superseded' WHERE ticket_id=? AND kind=? AND ref_id IS ? AND status='pending'", (ticket_id, kind, payload["ref_id"]))
                conn.execute("INSERT INTO approval_requests(id,ticket_id,kind,ref_id,amount,sku,size,qty,vendor_id,created_at,description,payload_json,signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (approval_id, ticket_id, kind, payload["ref_id"], payload["amount"], sku, size, qty, payload["vendor_id"], utc_now(), payload["description"], payload_json, signature(payload_json)))
                self.action(conn, ticket_id, "payment_prepared", {"approval_id": approval_id, "kind": kind, "amount": payload["amount"]})
            approval = next(a for a in self.approvals(conn, ticket_id) if a["id"] == approval_id)
            result = {"ok": True, "approval": approval, "checking": self.checking(conn), "available_for_purchase": dollars(self.available_purchase_cents(conn))}
        if not existing:
            self.audit("payment_prepared", approval)
        return result

    def list_approvals(self, ticket_id=None):
        with self.connect() as conn:
            if ticket_id is not None:
                self.ticket(conn, ticket_id)
            return {"ok": True, "approvals": self.approvals(conn, ticket_id)}

    def approve_payment(self, approval_id, approved_by, approval_token):
        authorize(approval_token)
        if not isinstance(approved_by, str) or not approved_by.strip() or len(approved_by.strip()) > 120:
            raise ShopError("invalid_approver", "Supply the human approver's name (1–120 characters).")
        with self.transaction() as conn:
            row = self.one(conn, "SELECT * FROM approval_requests WHERE id=?", (approval_id,), "approval")
            if not hmac.compare_digest(row["signature"], signature(row["payload_json"])):
                raise ShopError("forged_approval", "The prepared payment signature is invalid.")
            payload = json.loads(row["payload_json"])
            protected_fields = ("ticket_id", "kind", "ref_id", "amount", "sku", "size", "qty", "vendor_id", "description")
            if any(row[field] != payload.get(field) for field in protected_fields):
                raise ShopError("forged_approval", "The displayed approval fields differ from the signed request.")
            if row["status"] == "approved":
                payment = self.one(conn, "SELECT * FROM payments WHERE id=?", (row["payment_id"],), "payment")
                if any(payment[field] != row[field] for field in ("kind", "ref_id", "amount")) or payment["approved_by"] != row["approved_by"]:
                    raise ShopError("forged_approval", "The recorded payment does not match this approved request.")
                return {"ok": True, "idempotent": True, "approval": next(a for a in self.approvals(conn) if a["id"] == approval_id), "payment": payment, "checking": self.checking(conn)}
            if row["status"] != "pending":
                raise ShopError("stale_approval", "This request was superseded by a newer prepared approval.")
            current = self.request_payload(conn, row["ticket_id"], row["kind"], row["ref_id"], row["amount"] if row["kind"] == "purchase" else None, row["sku"], row["size"], row["qty"])
            if canonical(current) != row["payload_json"]:
                raise ShopError("stale_approval", "The underlying invoice, lease, pricing, stock, or desk date changed. Prepare a new request before approval.")
            before = cents(self.checking(conn)["balance"])
            after = before - cents(row["amount"])
            if after < 0:
                raise ShopError("insufficient_cash", "This approval would make checking negative.")
            conn.execute("UPDATE cash_accounts SET balance=?,date=? WHERE name='checking'", (dollars(after), self.desk_date(conn)))
            cur = conn.execute("INSERT INTO payments(kind,ref_id,amount,account,paid_at,approved_by) VALUES(?,?,?,?,?,?)", (row["kind"], row["ref_id"], row["amount"], "checking", self.desk_date(conn), approved_by.strip()))
            payment_id = cur.lastrowid
            purchase_order = None
            if row["kind"] == "invoice":
                conn.execute("UPDATE invoices SET status='paid' WHERE id=? AND status!='paid'", (row["ref_id"],))
            elif row["kind"] == "rent":
                due = date.fromisoformat(payload["state"]["next_due"])
                year = due.year + (due.month == 12)
                month = 1 if due.month == 12 else due.month + 1
                next_due = date(year, month, min(due.day, calendar.monthrange(year, month)[1])).isoformat()
                conn.execute("UPDATE leases SET next_due=? WHERE id=?", (next_due, row["ref_id"]))
            else:
                vendor = payload["state"]["vendor"]
                estimated = (date.fromisoformat(self.desk_date(conn)) + timedelta(days=vendor["lead_days"])).isoformat()
                order_cur = conn.execute("INSERT INTO purchase_orders(ticket_id,approval_id,vendor_id,sku,size,qty,amount,ordered_date,estimated_arrival) VALUES(?,?,?,?,?,?,?,?,?)", (row["ticket_id"], approval_id, row["vendor_id"], row["sku"], row["size"], row["qty"], row["amount"], self.desk_date(conn), estimated))
                purchase_order = self.one(conn, "SELECT * FROM purchase_orders WHERE id=?", (order_cur.lastrowid,))
            conn.execute("UPDATE approval_requests SET status='approved',approved_by=?,approved_at=?,payment_id=? WHERE id=? AND status='pending'", (approved_by.strip(), utc_now(), payment_id, approval_id))
            payment = self.one(conn, "SELECT * FROM payments WHERE id=?", (payment_id,))
            details = {"approval_id": approval_id, "payment": payment, "checking_before": dollars(before), "checking_after": dollars(after), "purchase_order": purchase_order}
            self.action(conn, row["ticket_id"], "payment_approved", details)
            result = {"ok": True, "idempotent": False, "approval": next(a for a in self.approvals(conn) if a["id"] == approval_id), "payment": payment, "checking": self.checking(conn), "purchase_order": purchase_order}
        self.audit("payment_approved", details)
        return result

    def schedule_restock(self, ticket_id):
        with self.transaction() as conn:
            ticket = self.ticket(conn, ticket_id)
            if not ticket["invoice_id"] or not ticket["sku"]:
                raise ShopError("no_reprint_invoice", "This ticket has no existing reprint invoice to follow up.")
            invoice = self.one(conn, "SELECT * FROM invoices WHERE id=?", (ticket["invoice_id"],), "invoice")
            if invoice["status"] != "paid":
                raise ShopError("unpaid_invoice", "The linked invoice must be paid before scheduling the vendor follow-up.")
            if self.rows(conn, "SELECT id FROM invoices WHERE vendor_id=? AND status!='paid'", (invoice["vendor_id"],)):
                raise ShopError("vendor_blocked", "The vendor still has open invoices.")
            vendor = self.one(conn, "SELECT * FROM vendors WHERE id=?", (invoice["vendor_id"],), "vendor")
            existing = conn.execute("SELECT id FROM restock_followups WHERE ticket_id=?", (ticket_id,)).fetchone()
            if not existing:
                today = self.desk_date(conn)
                estimated = (date.fromisoformat(today) + timedelta(days=vendor["lead_days"])).isoformat()
                note = "Internal follow-up on the paid reprint invoice. Lead-time estimate only; vendor confirmation, quantity, shipment, and physical receipt remain pending. Inventory is unchanged."
                conn.execute("INSERT INTO restock_followups(ticket_id,sku,size,vendor_id,source_invoice_id,requested_qty,scheduled_date,estimated_arrival,note) VALUES(?,?,?,?,?,?,?,?,?)", (ticket_id, ticket["sku"], ticket["size"], vendor["id"], invoice["id"], ticket["qty"], today, estimated, note))
                self.action(conn, ticket_id, "restock_followup_scheduled", {"estimated_arrival": estimated, "requested_qty": ticket["qty"], "confirmed_qty": None})
            result = self.one(conn, "SELECT * FROM restock_followups WHERE ticket_id=?", (ticket_id,))
        if not existing:
            self.audit("restock_followup_scheduled", result)
        return {"ok": True, "restock": result}

    def draft_message(self, ticket_id, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise ShopError("invalid_message", "A nonempty draft of at most 12,000 characters is required.")
        with self.transaction() as conn:
            ticket = self.ticket(conn, ticket_id)
            if ticket["status"] != "open":
                raise ShopError("ticket_closed", "This ticket is already resolved.")
            cur = conn.execute("INSERT INTO message_drafts(ticket_id,message,created_at) VALUES(?,?,?)", (ticket_id, message.strip(), utc_now()))
            draft = self.one(conn, "SELECT * FROM message_drafts WHERE id=?", (cur.lastrowid,))
            self.action(conn, ticket_id, "message_drafted", {"draft_id": draft["id"], "message": draft["message"], "sent": False})
        self.audit("message_drafted", draft)
        return {"ok": True, "draft": draft, "sent": False}

    def resolve_ticket(self, ticket_id, outcome):
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 12000:
            raise ShopError("invalid_outcome", "Supply a nonempty, factual outcome of at most 12,000 characters.")
        with self.transaction() as conn:
            ticket = self.ticket(conn, ticket_id)
            if ticket["status"] == "resolved":
                return {"ok": True, "idempotent": True, "ticket": ticket, "outcome": self.one(conn, "SELECT * FROM ticket_outcomes WHERE ticket_id=?", (ticket_id,))}
            if self.approvals(conn, ticket_id) and any(a["status"] == "pending" for a in self.approvals(conn, ticket_id)):
                raise ShopError("pending_approval", "A pending payment approval must be handled before resolving this ticket.")
            if not self.rows(conn, "SELECT id FROM message_drafts WHERE ticket_id=?", (ticket_id,)):
                raise ShopError("missing_draft", "Save a customer or landlord response draft before resolving this ticket.")
            if ticket["invoice_id"]:
                invoice = self.one(conn, "SELECT * FROM invoices WHERE id=?", (ticket["invoice_id"],))
                if invoice["status"] != "paid":
                    raise ShopError("unpaid_invoice", "The linked invoice is still unpaid.")
            if ticket["lease_id"] and not self.rows(conn, "SELECT id FROM payments WHERE kind='rent' AND ref_id=?", (ticket["lease_id"],)):
                raise ShopError("unpaid_rent", "The required rent payment has not been approved.")
            if ticket["sku"]:
                stock = self.one(conn, "SELECT qty FROM inventory WHERE sku=? AND size=?", (ticket["sku"], ticket["size"]))["qty"]
                if stock < ticket["qty"]:
                    lower = outcome.lower()
                    honest_markers = ("pending", "await", "scheduled", "defer", "declin", "cannot", "can't", "unavailable", "short", "only", "insufficient", "follow-up", "follow up")
                    if not any(marker in lower for marker in honest_markers):
                        raise ShopError("unsupported_fulfillment", "Stock is insufficient. State the pending restock, defer/decline decision, or smaller feasible offer honestly.")
                    if ticket["invoice_id"] and not self.rows(conn, "SELECT id FROM restock_followups WHERE ticket_id=?", (ticket_id,)):
                        raise ShopError("missing_restock_followup", "Schedule the paid-invoice restock follow-up before resolving this out-of-stock request.")
            conn.execute("INSERT INTO ticket_outcomes(ticket_id,outcome,resolved_at) VALUES(?,?,?)", (ticket_id, outcome.strip(), utc_now()))
            conn.execute("UPDATE tickets SET status='resolved' WHERE id=?", (ticket_id,))
            self.action(conn, ticket_id, "ticket_resolved", {"outcome": outcome.strip(), "customer_message_sent": False})
            result = {"ok": True, "idempotent": False, "ticket": self.ticket(conn, ticket_id), "outcome": self.one(conn, "SELECT * FROM ticket_outcomes WHERE ticket_id=?", (ticket_id,))}
        self.audit("ticket_resolved", {"ticket_id": ticket_id, "outcome": outcome.strip()})
        return result

    def reset_database(self, approval_token):
        authorize(approval_token)
        if hashlib.sha256(self.original_path.read_bytes()).hexdigest() != ORIGINAL_SHA256:
            raise ShopError("baseline_changed", "Reset refused: the protected original differs from the supplied baseline.")
        # Copy baseline data through one transaction; never replace a database
        # underneath another SQLite connection or write to the original file.
        with self.transaction() as conn:
            before = {"checking": self.checking(conn), "payments": self.rows(conn, "SELECT * FROM payments ORDER BY id"), "approvals": self.approvals(conn)}
            uri = self.original_path.resolve().as_uri() + "?mode=ro"
            original = sqlite3.connect(uri, uri=True)
            original.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA defer_foreign_keys=ON")
                for table in ("purchase_orders", "restock_followups", "message_drafts", "ticket_outcomes", "approval_requests", "shop_actions", "payments", "tickets", "invoices", "leases", "vendors", "inventory", "pricing", "cash_accounts", "desk"):
                    conn.execute(f'DELETE FROM "{table}"')
                for table in ("desk", "inventory", "pricing", "vendors", "leases", "cash_accounts", "payments", "invoices", "tickets"):
                    rows = original.execute(f'SELECT * FROM "{table}"').fetchall()
                    if rows:
                        columns = list(rows[0].keys())
                        quoted = ",".join(f'"{col}"' for col in columns)
                        placeholders = ",".join("?" for _ in columns)
                        conn.executemany(f'INSERT INTO "{table}"({quoted}) VALUES({placeholders})', [tuple(row) for row in rows])
                self.action(conn, None, "database_reset", {"baseline": "data/campus_customs.db", "external_audit_preserved": True})
            finally:
                original.close()
        self.audit("database_reset", {"before": before, "original_sha256": hashlib.sha256(self.original_path.read_bytes()).hexdigest(), "external_audit_preserved": True})
        return {"reset": True, **self.get_shop_snapshot()}


def safe_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ShopError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except sqlite3.IntegrityError:
        return {"ok": False, "error": {"code": "integrity_error", "message": "The requested change violated a database invariant; no transaction was committed."}}
