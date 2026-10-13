# Campus Customs backend

From this folder, with the assignment environment activated, start:

```sh
uvicorn main:app --reload --port 8000
```

The separate MCP service must be listening at `http://127.0.0.1:8001/mcp`. The browser desk uses `http://localhost:8000`; both local Vite origins on port 5173 are allowed.

All five roles in `agents.py` use PydanticAI and `gpt-6-luna` through the course Portkey Responses endpoint. Each role has a prompt in `prompts/` and can delegate to any other teammate. Limits are shared by the entire ticket, including nested delegation: 32 model requests, 75,000 total tokens, 60 tool calls, maximum three delegation edges, no circular delegation. MCP calls time out after 20 seconds; model requests after 60 seconds; complete ticket runs after 600 seconds. A stopped run preserves its evidence and remains open for review/retry.

Private `.env` configuration is read from the assignment root, with the coursework workspace `.env` as a local convenience fallback. Required keys: `PORTKEY_API_KEY` and `HUMAN_APPROVAL_TOKEN`; optional `MCP_URL`. The approval credential never enters an agent tool schema or model prompt. Shop facts and actions use MCP exclusively; this backend has no database connection.

Ticket runs start in the background and return a run ID immediately. The board polls `/api/state`; `/api/events` exposes complete retained evidence with optional ticket/run filters. Human approval calls the MCP administrative tool and then resumes the ticket; the agents themselves can only prepare requests. Reset clears the working shop, shows a new experiment on the board, and preserves the append-only audit. Reports are restored from the audit after a backend restart.

Run focused tests from the assignment root:

```sh
python -m unittest discover -s backend/tests -v
```

Tests use isolated files and fake shop actions. They do not approve payments in the working database.
