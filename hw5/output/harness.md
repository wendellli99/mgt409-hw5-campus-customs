# Campus Customs operational harness

## Supplied database

Original database SHA256: `23686a90d698f7fa6f901a3f0a9774db9cbb31e13151c9e5717275f3321f360a`. MCP alone reads and changes `data/campus_customs_new.db`; the original is used only for reset. The database is fictional course data.

| Table | Fields (SQLite type; constraints) | Why it matters |
|---|---|---|
| desk | `date_today` TEXT; required<br>`notes` TEXT | Defines the shop calendar. August 31, 2026 governs overdue invoices and lead-time estimates, regardless of the host date. |
| tickets | `id` INTEGER; primary key<br>`type` TEXT; required<br>`requester` TEXT; required<br>`subject` TEXT; required<br>`sku` TEXT<br>`size` TEXT<br>`qty` INTEGER<br>`lease_id` INTEGER<br>`invoice_id` INTEGER<br>`status` TEXT; required<br>`notes` TEXT<br>`created_at` TEXT; required | The three work requests link customer stock, invoice501 and lease1 to their operational decisions; notes preserve the original request. |
| inventory | `sku` TEXT; required, primary key<br>`name` TEXT; required<br>`size` TEXT; required, primary key<br>`qty` INTEGER; required<br>`location` TEXT; required | Stock at the exact SKU and size prevents substituting 14 large hoodies for a shortage of medium hoodies or claiming a small tee is available. |
| pricing | `sku` TEXT; primary key<br>`unit_cost` REAL; required<br>`list_price` REAL; required | Stored unit cost and list price support margin calculations and purchase estimates; it is not an authoritative discount policy. |
| vendors | `id` INTEGER; primary key<br>`name` TEXT; required<br>`specialty` TEXT; required<br>`lead_days` INTEGER; required | Specialty and stored lead days determine a proposed replenishment route and timing. Invoices can block shipping. |
| leases | `id` INTEGER; primary key<br>`space_name` TEXT; required<br>`landlord` TEXT; required<br>`monthly_rent` REAL; required<br>`next_due` TEXT; required<br>`notes` TEXT | Rent amount and next_due connect ticket102 to a real lease and govern the subsequent due date after payment. |
| cash_accounts | `name` TEXT; primary key<br>`balance` REAL; required<br>`date` TEXT; required | The $3,400 checking balance constrains every approved outgoing payment; no revenue is modeled. |
| payments | `id` INTEGER; primary key<br>`kind` TEXT; required<br>`ref_id` INTEGER<br>`amount` REAL; required<br>`account` TEXT; required<br>`paid_at` TEXT; required<br>`approved_by` TEXT; required | The payment ledger must identify the obligation, exact amount, account, timestamp and human approver; empty in original. |
| invoices | `id` INTEGER; primary key<br>`vendor_id` INTEGER; required<br>`amount` REAL; required<br>`due_date` TEXT; required<br>`status` TEXT; required<br>`description` TEXT | Invoice501 is an $840 unpaid vendor1 bill due August28, blocking replenishment until human-approved payment. |

## Initial three MCP tools

| Tool | Tables | Ticket and reason |
|---|---|---|
| `get_ticket(ticket_id)` | tickets | All three: obtains the stored request and its exact SKU/size, invoice or lease reference before choosing the relevant specialist. |
| `get_inventory(sku, size)` | inventory, pricing | 101: confirms zero small tees; 103: compares 20 requested medium hoodies with eight in stock, then uses $22 cost and $58 list price rather than guessing a discount margin. |
| `get_financial_context(ticket_id)` | desk, tickets, cash_accounts, invoices, vendors, leases, payments | 101: finds invoice501 overdue and its $840 blocker. 102: confirms $2,400 rent due September2 and available cash. 103: exposes cash remaining before proposing a $264 replenishment. |

## Team and shop rules

- Boss chooses relevant specialists, gathers their evidence and makes the final operational decision. A resolved ticket may mean a declined order or completed draft; it must not imply unmodeled physical delivery.
- Inventory checks exact SKU/size quantities, computes shortfalls, consults the stored vendor specialty and lead time, and asks Accounting about blockers and affordability. It never invents receipts.
- Accounting computes obligations, cash feasibility and margins. It prepares immutable payment proposals; payment execution is reserved for the human route.
- Facilities checks lease1 and due dates; it asks Accounting to prepare the recorded obligation.
- Customer Service creates drafts from verified facts. The application never sends email or contacts vendors.

Each agent has a prompt file and can delegate to any of the other four roles: all 20 directed role pairs are available, including a specialist delegating back to Boss. Direct self-delegation is blocked; repeated roles and cycles are bounded by delegation depth and the shared usage limits. All five shop agents use exactly `gpt-6-luna` through Portkey. The backend never has an alternate SQL shop layer.

## Safety and limits

Database transactions recheck the obligation, approval identity, available cash and idempotency immediately before changing cash. A private capability gates approve/reset MCP tools; agents cannot see or call these tools. Payment execution updates the relevant invoice, lease, or purchase record and appends the payment ledger. No tool adds sales revenue.

A real business would additionally require authenticated human roles, approver separation, bank reconciliation, signed payment-provider callbacks, least-privilege tool credentials, immutable external audit storage, privacy controls and reviewed outbound communication. This local coursework app records fictional ledger payments, not bank transfers.

Model-request, delegation, tool-call, token and elapsed-time limits bound a run. The audit remains append-only across database resets and failed runs. Later sections document the exact implemented settings, final MCP inventory, API routes, dashboard, and full-run evidence.

## Final MCP inventory

| Tool | Tables used | Action |
|---|---|---|
| get_ticket | tickets, message_drafts, ticket_outcomes, restock_followups, approval_requests | Read a request and its persisted decisions. |
| get_inventory | inventory, pricing | Exact SKU/size stock with recorded costs and prices. |
| get_financial_context | tickets, desk, cash_accounts, invoices, vendors, leases, payments | Cash, obligations, deadlines, and vendor blocks for a ticket. |
| list_tickets | tickets | List all statuses. |
| get_shop_snapshot | All supplied and supplemental tables | Current dashboard and evidence state. |
| get_vendor_status | vendors, invoices | Read lead days and unpaid-invoice shipping block. |
| prepare_payment | tickets, desk, invoices, leases, vendors, inventory, pricing, cash_accounts, purchase_orders, approval_requests; writes approval_requests and shop_actions | Immutable proposed payment; cash remains unchanged. |
| list_approvals | approval_requests, tickets, vendors, leases | Read pending/approved requests and their database-grounded payees. |
| approve_payment | approval_requests and current obligation, cash, vendor, inventory, pricing and order facts; writes cash_accounts, payments, approval_requests, shop_actions plus invoices/leases/purchase_orders as appropriate | Human backend only. Signature, freshness, cash and idempotency enforced inside one transaction. |
| schedule_restock | tickets, invoices, vendors, desk, restock_followups; writes restock_followups and shop_actions | Schedule follow-up on the paid reprint invoice using its vendor lead time. No stock is added, and confirmed quantity remains unknown. |
| draft_message | tickets; writes message_drafts and shop_actions | Local response draft, never transmitted. |
| resolve_ticket | tickets, approval_requests, message_drafts, invoices, payments, inventory, restock_followups, ticket_outcomes; writes tickets, ticket_outcomes and shop_actions | Mark an operational decision handled only after required payments/drafts; truthful shortage/fulfillment wording is required. |
| reset_database | Read-only protected original baseline; restores nine original tables and clears supplemental state | Human backend only; preserves both external audit files. |

All mutations also append to `mcp_server/audit_log.jsonl`. Payment preparation reserves unpaid invoices and rent due within seven shop days before permitting a discretionary purchase. This is an implemented prudence policy derived from the recorded obligations, not an extra fact present in the source schema. The apparel/gifts supplier mapping uses the recorded vendor specialties and product categories; no supplier shipping terms are invented.

## Supplemental workflow tables

These are application state added only to the working database; they do not alter the source pack. Business data still comes from the supplied records.

| Table | Fields | Purpose |
|---|---|---|
| approval_requests | `id` TEXT, `ticket_id` INTEGER, `kind` TEXT, `ref_id` INTEGER, `amount` REAL, `sku` TEXT, `size` TEXT, `qty` INTEGER, `vendor_id` INTEGER, `status` TEXT, `created_at` TEXT, `approved_by` TEXT, `approved_at` TEXT, `payment_id` INTEGER, `description` TEXT, `payload_json` TEXT, `signature` TEXT | Signed immutable proposal plus pending/approved/superseded state, human approver and payment reference. Private capability and signatures are not returned as model tool data. |
| message_drafts | `id` INTEGER, `ticket_id` INTEGER, `message` TEXT, `created_at` TEXT | Saved text and timestamp; no external sending. |
| ticket_outcomes | `ticket_id` INTEGER, `outcome` TEXT, `resolved_at` TEXT | Operational decision and resolved timestamp, with physical fulfillment limits explicit. |
| shop_actions | `id` INTEGER, `ticket_id` INTEGER, `action` TEXT, `details_json` TEXT, `created_at` TEXT | Persisted local action descriptions and reset marker. |
| purchase_orders | `id` INTEGER, `ticket_id` INTEGER, `approval_id` TEXT, `vendor_id` INTEGER, `sku` TEXT, `size` TEXT, `qty` INTEGER, `amount` REAL, `ordered_date` TEXT, `estimated_arrival` TEXT, `status` TEXT | Approved outgoing purchase recorded as pending delivery, without adding inventory. |
| restock_followups | `id` INTEGER, `ticket_id` INTEGER, `sku` TEXT, `size` TEXT, `vendor_id` INTEGER, `source_invoice_id` INTEGER, `requested_qty` INTEGER, `confirmed_qty` INTEGER, `scheduled_date` TEXT, `estimated_arrival` TEXT, `status` TEXT, `note` TEXT | Paid-invoice restock follow-up: requested quantity from ticket, confirmed quantity unknown, estimated arrival from vendor lead time. |

## API routes

| Method / URL | Purpose |
|---|---|
| GET /api/health | Local backend health and fixed model identifier. |
| GET /api/state | Combined tickets, current cash, approvals, generation-scoped events and run reports for the board. |
| GET /api/tickets | The three database statuses. |
| POST /api/tickets/{ticket_id}/run | Start a background team run; return 202 with its run ID. Reject concurrent or already-resolved runs. |
| GET /api/events | Retained audit events, optional ticket/run filters, bounded result count. |
| GET /api/runs | Recent run reports and contributions. |
| GET /api/cash | Checking balance and approved ledger outflow through MCP. |
| GET /api/approvals | Prepared/approved requests, optional ticket filter. |
| POST /api/approvals/{approval_id}/approve | A human local-desk action executes the signed proposal through MCP, records approval, then resumes the ticket. Agents cannot call this route. |
| POST /api/reset | Restore the original shop and start a new activity generation, while preserving audit history. |

Start with `uvicorn main:app --reload --port 8000` from backend. MCP runs separately at port8001; Vite at5173. Allowed local origins are explicitly enumerated; the human route is restricted to local callers and known origins. This is a local teaching app, not a deployed multi-user authorization system.

## Dashboard and runtime limits

The board polls real API state each second. It lists/selects all three tickets, starts the team, displays all five roles with actual contributions and tool names, shows full input/result evidence in disclosures, marks only stored resolved statuses complete, and presents explicit human approvals. Checking displays starting/current cash and approved outflow. A reset changes the activity generation while keeping older audit evidence. See design.md for the visual rationale.

Each initial or resumed team run has a shared budget of 32 model requests, 75,000 input/output tokens and 60 tool calls, including all delegated agents in that run. Maximum delegation depth is three edges; direct self-delegation is refused. Ancestor targets are allowed, so all 20 directed role pairs are available while repeated-role paths remain bounded by depth and the shared budget. A ticket run times out after600 seconds, a model request after60, and an MCP call after20. Model outputs are bounded to2200 tokens per request. The initial live experiment reached its earlier18-request limit before finishing the report, after safely preparing an invoice; that failed evidence is retained. Pending approval, partial contributions, and latest measured usage survive a bounded-run stop and backend restart. No run may spend more merely because it hit a limit.

The audit records actual UTC operation timestamps separately from the shop's fixed business date. Business overdue/order/payment calculations use August31,2026. The final run snapshot, ticket outcomes, ledger reconciliation and real board screenshots have been exported after the human-approved run. Expected plans remain distinct from the recorded Actual sections.

## Verification and delivery status

The local shop/backend verification suite passed 22 tests, and the React production build passed. Desktop and 390×844 phone views were inspected, including live activity, tool disclosures, pending human approval, run failure and responsive layout. A fresh Codex CLI session successfully used native MCP calls for all three initial tools. `mcp_smoke.json` records the exact executed request, successful native events and current working-DB outputs; the original pre-run protocol smoke is preserved inside it. Project `.mcp.json`, Codex project configuration and the global MCP entry are saved. The already-open desktop chat does not hot-load new tools, but the fresh coding-assistant session and backend both demonstrate working MCP integration.

The final-reset board run is complete. Tickets 101, 102 and 103 all have matching stored resolved statuses and real outcomes. Wendell authorized both fictional course payments and applied them through the board: invoice 501 paid $840 to Bulldog Print Co, then lease 1 rent paid $2,400 to Elm City Properties. Checking reconciles exactly: $3,400 − $840 − $2,400 = $160. Ticket 103 produced no cash movement. Payment approvals are recorded as human events and linked to actual ledger rows.

`export_evidence.py` created `run_snapshot.json`, `resolved_tickets.json`, the preserved Expected plus completed Actual/Cash/Reflection tabs, and `resolved_board.html` with existing screenshots for 101, 102 and 103. Both review pages embed CSS for double-click portability. The exporter reads API/MCP facts, aggregates all audit records after the latest reset, combines completed reports from initial/resumed runs, and verifies the ledger with read-only database QA. It takes one final available shared usage snapshot per distinct run; cumulative counters repeated on loop events are not double-counted.

The final generation contains 310 events, five runs, 10 outbound specialist delegations and 42 actual MCP calls. Distinct-run usage totals are 67 model requests, 130,253 input tokens, and 6,213 output tokens. The 83 earlier audit events remain retained outside this generation. All five individual roles participated somewhere in the final run, while each ticket used only the relevant subset.

The review preserves agent wording and separately flags the specific ticket-102 mistake: the paid lease's next due date of October 2 correctly follows the original September 2 obligation, so the stale ticket note is not a conflict. The ticket-103 decision preceded the human payments: its $3,400 checking was reduced to $160 discretionary availability by obligation reservations, and its $264 replenishment cost created a $104 gap. Paying the invoice later removes the vendor block but leaves the same cash gap after rent. Drafts remain unsent, restock timing remains an estimate, and no physical receipt or new revenue is claimed.

The strict submission validator passes all 48 artifact and connection checks. The user approved public publication, and the repository was created using the Personal Chrome profile: [https://github.com/wendellli99/mgt409-hw5-campus-customs](https://github.com/wendellli99/mgt409-hw5-campus-customs). Reviewed local commit `36841287206d9bdc357d30dcc584a4d924744e2b` was published as remote commit `514c2dfc24143247a0f971034bdeab1e676b4c0b`; their complete file trees match (`09356ebaf0bdf0540c1b5112e4bb37e174387c9e`). An anonymous clone also verified both database files and SQLite integrity. `github_url.txt` contains the verified URL. Publication status and URL records are included in the subsequent documentation update. Canvas submission has not been authorized or performed. All five final exported HTML tabs and all three resolved-board screenshot sections were rendered and inspected. The review text is readable and the Cash tab matches $160. Local implementation, public availability and Canvas submission remain separately evidenced states.

## Final connectivity compatibility check

The recorded ticket runs, cash reconciliation, and preserved audit evidence precede the final compatibility update. The frontend API default now matches the required `http://localhost:8000` literally. Every agent may delegate to any other role, including an earlier teammate in the path; direct self-delegation remains blocked, and depth three plus the unchanged shared 32-request/75,000-token/60-tool limits bound cycles. An offline test verifies all 20 directed role pairs and an explicit Inventory-to-ancestor-Boss handoff without calling any model or shop tool. Existing self/depth checks remain in place. This update did not rerun tickets, approve money, reset the database, or modify the earlier evidence.

## Process evidence and final review

The final source recheck covered live p1–p11, both panels on p1–p10, and the scenario. Seven genuinely read-only MCP tools advertise readOnlyHint=true, destructiveHint=false and openWorldHint=false. Six mutators are not marked read-only; payment execution and reset still require the private human capability and remain excluded from agent tools. Native smoke query results match the resolved working database without changing it.

The validator checks artifacts, current database values and exact native MCP evidence. Its passing result does not establish personal prompt history. Problem 1 asks for per-problem student-typed prompts; the user confirmed no additional history and explicitly retained the 44 prepared prompts, labeled honestly. The Reflection supplies all requested analysis for student review and adoption. Canvas submission remains pending.
