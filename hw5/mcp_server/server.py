"""Run from hw5: python mcp_server/server.py (HTTP :8001/mcp)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP

from shop import Shop, safe_call

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
mcp = FastMCP("Campus Customs Shop", instructions="Business facts come from the supplied SQLite shop database. Agents prepare payments only. Human-only approve_payment and reset_database must never be available to agent model toolsets. Draft messages are saved locally and are not sent.")
shop = Shop()


@mcp.tool
def get_ticket(ticket_id: int) -> dict:
    """Read ticket details, saved drafts/outcome/restock, and its approval requests."""
    return safe_call(shop.get_ticket, ticket_id)


@mcp.tool
def get_inventory(sku: str, size: str | None = None) -> dict:
    """Read actual stock and database costs/list prices for a SKU and optional size."""
    return safe_call(shop.get_inventory, sku, size)


@mcp.tool
def get_financial_context(ticket_id: int) -> dict:
    """Read the desk's business date, checking, invoices, vendor blocks, rent and payments."""
    return safe_call(shop.get_financial_context, ticket_id)


@mcp.tool
def list_tickets() -> dict:
    """List all tickets and their open/resolved statuses."""
    return safe_call(shop.list_tickets)


@mcp.tool
def get_shop_snapshot() -> dict:
    """Read persisted desk state for the human dashboard and audit view."""
    return safe_call(shop.get_shop_snapshot)


@mcp.tool
def get_vendor_status(vendor_id: int) -> dict:
    """Read vendor lead time, invoices, and whether unpaid invoices block purchases."""
    return safe_call(shop.get_vendor_status, vendor_id)


@mcp.tool
def prepare_payment(ticket_id: int, kind: str, ref_id: int | None = None,
                    amount: float | None = None, sku: str | None = None,
                    size: str | None = None, qty: int | None = None) -> dict:
    """Prepare an immutable human approval; never debit cash. Kinds: invoice, rent, purchase. Invoice/rent amount comes from DB. Purchase ref_id is vendor ID; amount is optional but must match qty*unit_cost; purchase reserves near-term rent and unpaid invoices."""
    return safe_call(shop.prepare_payment, ticket_id, kind, ref_id, amount, sku, size, qty)


@mcp.tool
def list_approvals(ticket_id: int | None = None) -> dict:
    """List prepared and approved payment requests; optionally filter by ticket."""
    return safe_call(shop.list_approvals, ticket_id)


@mcp.tool
def approve_payment(approval_id: str, approved_by: str, approval_token: str) -> dict:
    """HUMAN BACKEND ONLY: authenticated, transactional and idempotent payment execution. Never expose this tool to an agent model."""
    return safe_call(shop.approve_payment, approval_id, approved_by, approval_token)


@mcp.tool
def schedule_restock(ticket_id: int) -> dict:
    """Schedule internal follow-up on this ticket's existing paid reprint invoice. Save vendor lead-time estimate without claiming delivery or adding stock; confirmed quantity stays unknown."""
    return safe_call(shop.schedule_restock, ticket_id)


@mcp.tool
def draft_message(ticket_id: int, message: str) -> dict:
    """Save a response draft locally. Does not send customer or landlord messages."""
    return safe_call(shop.draft_message, ticket_id, message)


@mcp.tool
def resolve_ticket(ticket_id: int, outcome: str) -> dict:
    """Save a factual outcome and mark a handled ticket resolved after required payments/drafts/follow-ups. Pending physical fulfillment must remain explicit."""
    return safe_call(shop.resolve_ticket, ticket_id, outcome)


@mcp.tool
def reset_database(approval_token: str) -> dict:
    """HUMAN BACKEND ONLY: reset working tables from the protected original; preserve external audit. Never expose this tool to agent models."""
    return safe_call(shop.reset_database, approval_token)


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=int(os.environ.get("MCP_PORT", "8001")), path="/mcp")
