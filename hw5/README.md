# Campus Customs · Multi-Agent Operations

A local operations desk for three fictional shop tickets. Five PydanticAI agents read and change shop data through one FastMCP server. React shows their activity, proposed payments, ticket outcomes, and cash. Every shop agent uses **gpt-6-luna through Portkey**.

## Install

Use Python 3.11+ and Node.js 20.19+ or 22.12+. From this `hw5/` folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Put your private `PORTKEY_API_KEY` in `.env`. Generate a private `HUMAN_APPROVAL_TOKEN` (for example with `python -c "import secrets; print(secrets.token_urlsafe(32))"`) and put it in the same file. MCP and backend load this configuration. Never commit `.env` or pass the approval capability to a model. The model is fixed in code; changing a model environment variable cannot select another model.

Both supplied SQLite files are included in `data/`. Keep `campus_customs.db` untouched. Before a new full three-ticket run, restore the working copy using the board’s **Start a fresh desk** button, then confirm **Reset the desk**, or stop the services and copy the original:

```bash
cp data/campus_customs.db data/campus_customs_new.db
```

The reset restores the $3,400 checking balance, three open tickets, unpaid invoice, stock, and lease. It preserves `output/audit_trail.json`; older runs remain visible in the evidence. Stop services before using the file-copy option so no open transaction is replaced.

## Start three services

In a terminal at `hw5/`, activate the virtual environment and start MCP:

```bash
source .venv/bin/activate
python mcp_server/server.py
```

MCP listens at `http://127.0.0.1:8001/mcp`. `.mcp.json` contains a project connection for compatible editors, and `.codex/config.toml` contains the Codex project connection. Open/trust this project and reload the editor’s MCP list if needed. A fresh Codex CLI session successfully called all three initial tools through native MCP during the final review. `output/mcp_smoke.json` preserves the exact executed request, native call events, and database-matching results, plus the earlier protocol smoke. Query tools declare accurate read-only metadata; payment execution and reset remain protected human actions.

In a second terminal at `hw5/`:

```bash
source .venv/bin/activate
cd backend
uvicorn main:app --reload --port 8000
```

In a third terminal at `hw5/`:

```bash
cd frontend
npm ci
npm run dev
```

Open [the operations desk](http://localhost:5173). The backend allows local Vite origins and reads the MCP URL from configuration. If MCP or Portkey is unavailable, the board reports an error instead of displaying a fabricated result.

## Resolve a fresh run

Reset before the full run, then run 101, 102, and 103 in order. When a payment is prepared, inspect its amount and reason and approve it as the human operator. Approval automatically resumes the paused ticket after the payment commits; wait for that continuation rather than manually starting a duplicate run. Agents cannot approve their own requests. Payments affect only the fictional course ledger; there is no bank or email integration.

Ticket 101 has zero small tees and an $840 overdue supplier invoice. Ticket 102 has $2,400 rent due September 2 relative to the shop’s August 31 date. Together these obligations leave $160. Ticket 103 requests 20 medium hoodies while only eight are stocked; the $264 shortfall purchase cannot be funded from that balance. A resolved ticket records an honest decision and draft; it does not invent a shipment, receipt, or customer payment.

## Review the homework

- `AI_prompts.md`: four prepared working prompts per problem, with follow-up purposes.
- `REQUIREMENTS_REVIEW.md`: review of all eleven pages, required artifacts, verification status, and outstanding human steps.
- `output/harness.md`: all supplied table fields, MCP tools, agents, routes, dashboard, safety and limits.
- `output/mcp_smoke.json`: genuine MCP responses for the first three tools.
- `output/desk_tickets.html`: double-clickable Expected/Actual ticket tabs, cash reconciliation, and evidence-based reflection.
- `output/design.md`: visual and interaction decisions.
- `output/audit_trail.json`: retained real loop, tool, delegation, approval and usage evidence.
- `output/resolved_tickets.json` and `output/resolved_board.html`: final ticket outcomes and board screenshots once the human-approved full run is complete.

## Verify

```bash
python -m pytest -q
python validate_submission.py
cd frontend
npm run build
```

Money and reset tests use temporary database copies. The strict validator checks final cash against the payment ledger, original-file integrity, required evidence, ticket outcomes, screenshot references, and model configuration. It will report outstanding final-run or publication evidence instead of pretending the assignment is complete.

## Publication

The homework is published in the **[public GitHub repository](https://github.com/wendellli99/mgt409-hw5-campus-customs)**. Both databases are included, and the verified repository URL is saved in `output/github_url.txt`. An anonymous clone matched the reviewed local file tree. The strict package validator passes all 48 artifact and connection checks. Canvas submission has not been authorized or performed; submit the repository URL there to complete that separate course step.

## Final review limits

The full source review covered all eleven pages and both panels where present. The final shop/backend suite passes 22 tests, and the frontend production build passes. All 20 directed agent pairs are supported, with direct self-delegation blocked and shared limits plus depth three bounding cycles. Recorded ticket runs and their Expected/Actual evidence predate this final compatibility update and remain preserved. No ticket was rerun and no shop data changed during the final review.

Problem 1 literally asks for prompts the student typed per problem. The user confirmed there is no additional history and asked to retain the 44 prepared prompts; they remain labeled honestly. Passing artifact checks does not remove this limitation. The evidence-based Reflection is ready for student review and adoption. Canvas submission remains separate.
