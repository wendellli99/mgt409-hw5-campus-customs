# Campus Customs MCP server

This FastMCP 3.4.8 server is the single shop-data boundary used by the five agents and the human dashboard backend. It reads and changes `hw5/data/campus_customs_new.db`; it never writes the supplied `hw5/data/campus_customs.db`. The original SHA-256 is checked before starting and resetting. The desk's business date is **2026-08-31**, independently of the computer's current date.

From `hw5/`, with the homework environment activated, run:

```sh
python mcp_server/server.py
```

The streamable HTTP endpoint is `http://127.0.0.1:8001/mcp`. A private local `.env` must contain the same random `HUMAN_APPROVAL_TOKEN` (at least 24 characters) used by the backend. Keep that value out of source control, tool outputs, agent prompts and browser payloads.

## Three initial tools

| Tool | Tables | Ticket and purpose |
|---|---|---|
| `get_ticket(ticket_id)` | `tickets`; later saved drafts, approval and outcome tables | 101 links the S tee request to invoice 501; 102 identifies lease 1; 103 identifies the 20 M hoodie request. This prevents guessing which item or obligation the team is handling. |
| `get_inventory(sku, size=None)` | `inventory`, `pricing` | 101 finds zero S tees; 103 finds eight M hoodies and a $22 unit cost, so the shortfall is 12 and its cost is $264. Stock, cost and list price are distinct facts. |
| `get_financial_context(ticket_id)` | `tickets`, `desk`, `cash_accounts`, `invoices`, `vendors`, `leases`, `payments` | 101 finds the overdue $840 invoice and blocked vendor; 102 finds $2,400 rent due September 2; 103 sees only $160 after protecting those obligations. |

`output/mcp_smoke.json` records genuine HTTP MCP responses from those three tools. Its prompts are prepared test requests, not a claimed human conversation transcript.

## Tools added for the full team and desk

| Tool | Purpose |
|---|---|
| `list_tickets()` | Read every ticket and its status. |
| `get_shop_snapshot()` | Read persisted board state, balance, payments, approvals, drafts, outcomes and actions. |
| `get_vendor_status(vendor_id)` | Read the vendor, invoices, lead time and purchase block. |
| `prepare_payment(ticket_id, kind, ref_id=None, amount=None, sku=None, size=None, qty=None)` | Create or reuse a signed, immutable approval request without changing cash. `kind` is `invoice`, `rent` or `purchase`; `ref_id` means invoice ID, lease ID or purchase vendor ID. Invoice and rent amounts are taken from the database. Purchase amount is quantity × unit cost; an optional supplied amount must match. |
| `list_approvals(ticket_id=None)` | Read pending, approved and superseded requests. |
| `schedule_restock(ticket_id)` | Record an internal follow-up on a paid reprint invoice. For ticket 101, the five-day vendor lead gives a September 5 estimate. Confirmed quantity and physical arrival remain pending; stock stays unchanged. |
| `draft_message(ticket_id, message)` | Save a local response draft; it does not send a message. |
| `resolve_ticket(ticket_id, outcome)` | Save a factual outcome after required approval, draft and restock follow-up steps. A handled ticket can be resolved while physical restock remains explicitly pending. |
| `approve_payment(approval_id, approved_by, approval_token)` | **Human backend only.** Execute an approved request transactionally and idempotently; agent toolsets exclude it. |
| `reset_database(approval_token)` | **Human backend only.** Restore working tables from the protected original and preserve `mcp_server/audit_log.jsonl` from prior runs. |

Every tool returns `{"ok": true, ...}` or `{"ok": false, "error": {"code": ..., "message": ...}}`. Approval outputs include the database-grounded `payee`: the invoice/purchase vendor's name or the lease landlord. The prepared payee is signed inside the immutable request payload. The approval secret and signatures are never returned. The backend owns the human approval route and supplies the secret only after the human action.

## Money and fulfillment safeguards

All payment preparation, execution and reset mutations use SQLite `BEGIN IMMEDIATE`. Dollar values are validated and calculated as integer cents. Duplicate approve clicks produce one payment and one debit. Invoice payment changes the invoice to paid and inserts a payment row in the same transaction; rent inserts a payment and advances the due date by a calendar month. Signed requests reject forged or changed invoice/lease/pricing/stock/date facts. Preparing a replacement supersedes its obsolete request.

A purchase requires an invoice-free vendor and enough cash after reserving all unpaid invoices and rent due within seven desk days. It creates a pending purchase order and debits checking only after human approval. It never fabricates delivered stock, sales revenue or cash receipts. The full 12-hoodie shortfall is unaffordable after invoice and rent payment; a deferred/declined request or an honest smaller offer is appropriate.

Resolution requires a saved response draft, no pending approval, and the relevant invoice/rent payment. Out-of-stock outcomes must explicitly preserve pending fulfillment or state a feasible defer/decline/smaller-offer decision. No messaging service is invoked.

Run focused money-boundary tests from `WH 5/`:

```sh
python -m pytest tests/test_shop.py -q
```

Tests operate on temporary database copies and do not execute payments in the homework's working database.
