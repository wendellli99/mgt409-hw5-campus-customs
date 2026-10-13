"""FastAPI desk. Runs are background tasks; human approval is a separate MCP action."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

# Importing agents loads the private .env before the MCP transport reads its URL.
from agents import AgentTeam, usage_dict
from audit import append_event, now, read_events
from mcp_client import MCPError, call_shop_tool
from models import Approval, ApproveRequest, Cash, DeskState, RunRecord, Ticket

app = FastAPI(title='Campus Customs Agent Desk', version='1.0.0')
ALLOWED_ORIGINS = {'http://localhost:5173', 'http://127.0.0.1:5173',
                   'http://localhost:8000', 'http://127.0.0.1:8000'}
app.add_middleware(CORSMiddleware, allow_origins=list(ALLOWED_ORIGINS),
                   allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])
_runs: dict[str, RunRecord] = {}
_tasks: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()
_team: AgentTeam | None = None


def active_runs() -> list[RunRecord]:
    return [run for run in _runs.values() if run.status == 'running']


def get_team() -> AgentTeam:
    global _team
    if _team is None:
        _team = AgentTeam()
    return _team


def normalized_ticket(raw: dict[str, Any]) -> Ticket:
    record = dict(raw)
    ident = int(record.get('ticket_id', record.get('id')))
    summary = next((run.summary for run in reversed(list(_runs.values())) if run.ticket_id == ident and run.summary), '')
    return Ticket.model_validate({**record, 'ticket_id': ident, 'id': ident,
                                  'title': record.get('title', record.get('subject', f'Ticket {ident}')),
                                  'description': record.get('description', record.get('details', record.get('notes', ''))),
                                  'status': record.get('status', 'open'), 'agent_summary': summary})


def normalized_approval(raw: dict[str, Any]) -> Approval:
    ident = str(raw.get('approval_id', raw.get('id')))
    return Approval.model_validate({**raw, 'id': ident, 'approval_id': ident,
                                    'payee': raw.get('payee') or raw.get('vendor_name') or raw.get('landlord') or '',
                                    'description': raw.get('description', raw.get('kind', 'Payment request')),
                                    'created_at': raw.get('created_at', '')})


def normalized_cash(snapshot: dict[str, Any]) -> Cash:
    checking = snapshot.get('checking', {})
    balance = float(checking.get('balance', 0))
    # Outflow is based on actual recorded payments, never prepared approvals.
    payments = snapshot.get('payments', [])
    outflow = round(sum(float(payment.get('amount', 0)) for payment in payments), 2)
    baseline = float(snapshot.get('starting_balance', checking.get('starting_balance', balance + outflow)))
    return Cash.model_validate({**checking, 'balance': balance, 'starting_balance': baseline,
                                'total_outflow': outflow,
                                'account_name': checking.get('name', 'Checking'), 'currency': 'USD'})


async def shop(name: str, args: dict[str, Any] | None = None, *, human: bool = False) -> dict[str, Any]:
    try:
        return await call_shop_tool(name, args, human_action=human)
    except MCPError as exc:
        status = 409 if exc.code not in {'configuration', 'mcp_error'} else 503
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except (TimeoutError, OSError) as exc:
        raise HTTPException(status_code=503, detail='The local MCP shop service is unavailable.') from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail='The local MCP shop service returned an unexpected error.') from exc


def current_events() -> list[dict[str, Any]]:
    """Only show the active experiment on the board; the full audit remains retained."""
    events = read_events(limit=100000)
    reset_positions = [index for index, event in enumerate(events)
                       if event.get('event_type') == 'database_reset']
    if reset_positions:
        events = events[reset_positions[-1] + 1:]
    return events


def harvest_partial_report(record: RunRecord) -> None:
    events = read_events(run_id=record.run_id, limit=10000)
    record.contributions = {event['agent']: event['message'] for event in events
                            if event.get('event_type') == 'agent_completed'}
    latest_usage = next((event.get('tokens') for event in reversed(events) if event.get('tokens')), None)
    if latest_usage:
        record.usage = latest_usage
    if not record.summary and record.contributions:
        record.summary = next(reversed(record.contributions.values()))


async def recover_pending_approval(record: RunRecord) -> bool:
    try:
        context = await call_shop_tool('get_ticket', {'ticket_id': record.ticket_id})
    except Exception:
        return False
    pending = [item for item in context.get('approvals', []) if item.get('status') == 'pending']
    if not pending:
        return False
    record.status = 'awaiting_approval'
    total = sum(float(item.get('amount', 0)) for item in pending)
    record.summary = (f'A verified payment request totaling ${total:,.2f} is awaiting human approval. '
                      'No money was paid by the agents. ' + record.summary)
    return True


@app.on_event('startup')
async def restore_runs() -> None:
    # Server restarts must not make already-completed reports disappear.
    for event in current_events():
        if event.get('event_type') not in {'run_started', 'run_finished'}:
            continue
        output = event.get('output')
        if isinstance(output, dict) and 'run_id' in output:
            _runs[output['run_id']] = RunRecord.model_validate(output)
    for record in _runs.values():
        if record.status == 'running':
            record.status = 'failed'
            record.finished_at = now()
            record.error = 'The backend restarted during this run. Prior evidence was retained; retry the ticket.'
            append_event(run_id=record.run_id, ticket_id=record.ticket_id, agent='Boss',
                         event_type='run_finished', message=record.error, output=record.model_dump())
    latest_by_ticket = {record.ticket_id: record for record in _runs.values()}
    for record in latest_by_ticket.values():
        if record.status == 'failed':
            harvest_partial_report(record)
            if await recover_pending_approval(record):
                record.error = 'The original agent loop stopped after saving this payment request. Review the request before approving.'
                append_event(run_id=record.run_id, ticket_id=record.ticket_id, agent='Boss',
                             event_type='run_finished', message=record.summary, output=record.model_dump(),
                             tokens=record.usage, recovery='Existing pending request recovered after restart.')


def require_local_human(request: Request) -> None:
    origin = request.headers.get('origin')
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail='Use the local Campus Customs desk for human actions.')
    if request.client and request.client.host not in {'127.0.0.1', '::1', 'localhost', 'testclient'}:
        raise HTTPException(status_code=403, detail='Human actions are restricted to the local desk.')


async def execute_run(record: RunRecord) -> None:
    try:
        output, usage = await get_team().run_ticket(record.ticket_id, record.run_id)
        ticket_result = await call_shop_tool('get_ticket', {'ticket_id': record.ticket_id})
        pending = [item for item in ticket_result.get('approvals', []) if item.get('status') == 'pending']
        status = ticket_result['ticket'].get('status', 'open')
        record.summary = output.summary
        record.usage = usage_dict(usage)
        events = read_events(run_id=record.run_id, limit=10000)
        record.contributions = {event['agent']: event['message'] for event in events
                                if event.get('event_type') == 'agent_completed'}
        if pending:
            record.status = 'awaiting_approval'
        elif status == 'resolved':
            record.status = 'completed'
        else:
            record.status = 'failed'
            record.error = 'The team stopped with the ticket still open. Review its report before retrying.'
    except asyncio.CancelledError:
        record.status = 'failed'
        record.error = 'The backend stopped during this run; all prior evidence was retained.'
    except Exception as exc:
        record.status = 'failed'
        # Avoid recording provider response headers or raw credential-containing exception text.
        record.error = f'The run stopped ({type(exc).__name__}). Review the recorded steps and retry when ready.'
        harvest_partial_report(record)
        recovered = await recover_pending_approval(record)
        if recovered:
            record.error = (f'The agent loop stopped ({type(exc).__name__}) after saving a payment request. '
                            'The verified request is ready for human review; approve only if you agree.')
        append_event(run_id=record.run_id, ticket_id=record.ticket_id, agent='Boss',
                     event_type='run_error', message=record.error, error=type(exc).__name__,
                     tokens=record.usage, awaiting_approval=recovered)
    finally:
        record.finished_at = now()
        append_event(run_id=record.run_id, ticket_id=record.ticket_id, agent='Boss',
                     event_type='run_finished', message=record.summary or record.error or 'Run finished.',
                     output=record.model_dump(), tokens=record.usage)


async def start_run(ticket_id: int) -> RunRecord:
    # Called under _lock: one run at a time makes shared obligations easy to inspect.
    if active_runs():
        raise HTTPException(status_code=409, detail='A ticket is already running. Wait for its report before starting another.')
    ticket = await shop('get_ticket', {'ticket_id': ticket_id})
    if ticket['ticket'].get('status') == 'resolved':
        raise HTTPException(status_code=409, detail='This ticket is resolved. Reset the shop to start a fresh experiment.')
    try:
        get_team()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    record = RunRecord(run_id=uuid.uuid4().hex, ticket_id=ticket_id, started_at=now())
    _runs[record.run_id] = record
    append_event(run_id=record.run_id, ticket_id=ticket_id, agent='Boss',
                 event_type='run_started', message=f'Boss started ticket {ticket_id}.', output=record.model_dump())
    _tasks[record.run_id] = asyncio.create_task(execute_run(record))
    return record


@app.get('/api/health')
async def health() -> dict[str, str]:
    return {'status': 'ok', 'model': 'gpt-6-luna'}


@app.get('/api/state', response_model=DeskState)
async def get_state() -> DeskState:
    snapshot = await shop('get_shop_snapshot')
    return DeskState(tickets=[normalized_ticket(row) for row in snapshot.get('tickets', [])],
                     cash=normalized_cash(snapshot),
                     approvals=[normalized_approval(row) for row in snapshot.get('approvals', [])],
                     events=current_events()[-300:], runs=list(_runs.values())[-30:],
                     desk_date=str(snapshot.get('desk_date', '')))


@app.get('/api/tickets', response_model=list[Ticket])
async def get_tickets() -> list[Ticket]:
    result = await shop('list_tickets')
    return [normalized_ticket(row) for row in result['tickets']]


@app.get('/api/events')
async def get_events(ticket_id: int | None = None, run_id: str | None = None,
                     limit: int = Query(default=300, ge=1, le=3000)) -> list[dict[str, Any]]:
    return read_events(ticket_id=ticket_id, run_id=run_id, limit=limit)


@app.get('/api/runs', response_model=list[RunRecord])
async def get_runs() -> list[RunRecord]:
    return list(_runs.values())[-30:]


@app.get('/api/cash', response_model=Cash)
async def get_cash() -> Cash:
    return normalized_cash(await shop('get_shop_snapshot'))


@app.get('/api/approvals', response_model=list[Approval])
async def get_approvals(ticket_id: int | None = None) -> list[Approval]:
    result = await shop('list_approvals', {'ticket_id': ticket_id})
    return [normalized_approval(row) for row in result['approvals']]


@app.post('/api/tickets/{ticket_id}/run', response_model=RunRecord, status_code=202)
async def run_ticket(ticket_id: int) -> RunRecord:
    async with _lock:
        return await start_run(ticket_id)


@app.post('/api/approvals/{approval_id}/approve')
async def approve(approval_id: str, body: ApproveRequest, request: Request) -> dict[str, Any]:
    require_local_human(request)
    async with _lock:
        if active_runs():
            raise HTTPException(status_code=409, detail='Wait for the running agents to finish before approving cash.')
        result = await shop('approve_payment', {'approval_id': approval_id, 'approved_by': body.approved_by}, human=True)
        ticket_id = int(result['approval']['ticket_id'])
        append_event(run_id=f'human-{uuid.uuid4().hex}', ticket_id=ticket_id, agent='Human',
                     event_type='human_approval', message=f'{body.approved_by} approved {result["approval"]["description"]}.',
                     input={'approval_id': approval_id, 'approved_by': body.approved_by}, output=result, model=None)
        if result.get('idempotent'):
            return result
        # Resume the team only after MCP committed the human-approved payment.
        record = await start_run(ticket_id)
        return {**result, 'resumed_run': record.model_dump()}


@app.post('/api/reset')
async def reset(request: Request) -> dict[str, Any]:
    require_local_human(request)
    async with _lock:
        if active_runs():
            raise HTTPException(status_code=409, detail='Wait for the active run to finish before resetting.')
        result = await shop('reset_database', human=True)
        _runs.clear()
        append_event(run_id=f'reset-{uuid.uuid4().hex}', ticket_id=None, agent='Human',
                     event_type='database_reset', message='The human desk reset the working database. Audit evidence was retained.',
                     output={'reset': True}, model=None)
        return {'status': 'reset', **result}


@app.on_event('shutdown')
async def shutdown() -> None:
    for task in _tasks.values():
        if not task.done():
            task.cancel()
    await asyncio.gather(*_tasks.values(), return_exceptions=True)
