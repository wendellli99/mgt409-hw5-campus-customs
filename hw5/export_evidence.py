#!/usr/bin/env python3
"""Export assignment evidence from the live desk; never run or approve agents.

Run from hw5 with the project's Python environment after the board run and the
three screenshots are complete: python export_evidence.py
An incomplete desk produces run_snapshot.json and run_progress.json only.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'output'
TICKETS = (101, 102, 103)
AGENTS = ('Boss', 'Inventory', 'Accounting', 'Facilities', 'Customer Service')


def cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def dollars(value: int) -> float:
    return float(Decimal(value) / 100)


def money(value: Any) -> str:
    amount = Decimal(str(value))
    if amount == 0:
        return '$0.00'
    return f'−${abs(amount):,.2f}' if amount < 0 else f'${amount:,.2f}'


def h(value: Any) -> str:
    return escape(str(value), quote=True)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def get_api(base: str) -> dict[str, Any]:
    with urlopen(base.rstrip('/') + '/api/state', timeout=25) as response:
        return json.load(response)


def unpack_mcp(result: Any) -> dict[str, Any]:
    for name in ('data', 'structured_content', 'structuredContent'):
        value = getattr(result, name, None)
        if isinstance(value, dict):
            return value
    for block in getattr(result, 'content', []):
        if getattr(block, 'type', '') == 'text':
            value = json.loads(block.text)
            if isinstance(value, dict):
                return value
    raise RuntimeError('The MCP snapshot was not a JSON object.')


async def get_shop(url: str) -> dict[str, Any]:
    from fastmcp import Client
    async with Client(url) as client:
        result = unpack_mcp(await client.call_tool('get_shop_snapshot', {}, timeout=20))
    if result.get('ok') is not True:
        raise RuntimeError('The MCP shop snapshot was unsuccessful.')
    return result


def readonly_verification() -> dict[str, Any]:
    """Artifact QA only; the app/agents themselves use MCP for every shop access."""
    records = {}
    for name in ('campus_customs.db', 'campus_customs_new.db'):
        path = ROOT / 'data' / name
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            records[name] = {
                'checking': dict(conn.execute("SELECT * FROM cash_accounts WHERE name='checking'").fetchone()),
                'tickets': [dict(row) for row in conn.execute('SELECT id,status FROM tickets ORDER BY id')],
                'payments': [dict(row) for row in conn.execute('SELECT * FROM payments ORDER BY id')],
                'leases': [dict(row) for row in conn.execute('SELECT * FROM leases ORDER BY id')],
            }
    return records


def generation(audit: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    boundary = max((index for index, event in enumerate(audit)
                    if event.get('event_type') == 'database_reset'), default=-1)
    return (audit[boundary] if boundary >= 0 else None,
            audit[boundary + 1:], audit[:boundary] if boundary >= 0 else [])


def run_records(state: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {event.get('run_id') for event in events
           if event.get('ticket_id') in TICKETS and event.get('agent') in AGENTS}
    result: dict[str, dict[str, Any]] = {}
    for run in state.get('runs', []):
        if run.get('run_id') in ids:
            result[run['run_id']] = dict(run)
    for event in events:
        if event.get('event_type') in {'run_started', 'run_finished'} and isinstance(event.get('output'), dict):
            record = event['output']
            if record.get('run_id') in ids:
                result.setdefault(record['run_id'], {}).update(record)
    for run_id, record in result.items():
        steps = [event for event in events if event.get('run_id') == run_id]
        # These counters are shared cumulative counters. Read ONE final snapshot
        # per run, never add the snapshots attached to every tool/loop event.
        recorded = record.get('usage')
        final_usage_available = bool(recorded)
        if not recorded:
            recorded = next((event['tokens'] for event in reversed(steps)
                             if isinstance(event.get('tokens'), dict) and event['tokens']), {})
        record['usage'] = recorded
        record['usage_source'] = 'final_run_record' if final_usage_available else 'last_recorded_shared_counter'
        record['delegation_count'] = sum(event.get('event_type') == 'delegation' for event in steps)
        record['mcp_call_count'] = sum(event.get('event_type') == 'tool_call' for event in steps)
    return sorted(result.values(), key=lambda record: record.get('started_at', ''))


def summed_usage(runs: list[dict[str, Any]]) -> dict[str, int]:
    keys = ('requests', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'tool_calls')
    totals = {key: sum(int(run.get('usage', {}).get(key, 0) or 0) for run in runs) for key in keys}
    totals['total_tokens'] = totals['input_tokens'] + totals['output_tokens']
    return totals


def reconcile(state: dict[str, Any], shop: dict[str, Any], verified: dict[str, Any],
              events: list[dict[str, Any]]) -> dict[str, Any]:
    original = verified['campus_customs.db']
    working = verified['campus_customs_new.db']
    start = cents(original['checking']['balance'])
    original_payments = {row['id'] for row in original['payments']}
    payments = [row for row in shop.get('payments', []) if row['id'] not in original_payments]
    approvals = {row.get('payment_id'): row for row in shop.get('approvals', [])
                 if row.get('status') == 'approved' and row.get('payment_id') is not None}
    human_events = [event for event in events if event.get('event_type') == 'human_approval']
    human_by_payment = {}
    for event in human_events:
        result = event.get('output') or {}
        if isinstance(result, dict) and isinstance(result.get('payment'), dict):
            human_by_payment[result['payment']['id']] = event
    ledger = []
    running = start
    errors = []
    for payment in sorted(payments, key=lambda row: row['id']):
        approved = approvals.get(payment['id'])
        human = human_by_payment.get(payment['id'])
        ticket = approved.get('ticket_id') if approved else human.get('ticket_id') if human else None
        if ticket not in TICKETS:
            errors.append(f'Payment {payment["id"]} cannot be traced to one of the three ticket approvals.')
        if payment.get('account') != 'checking':
            errors.append(f'Payment {payment["id"]} was not recorded against checking.')
        amount = cents(payment['amount'])
        before = running
        running -= amount
        ledger.append({**payment, 'ticket_id': ticket,
                       'approval_id': approved.get('id', approved.get('approval_id')) if approved else None,
                       'description': approved.get('description', '') if approved else '',
                       'payee': approved.get('payee', '') if approved else '',
                       'checking_before': dollars(before), 'checking_after': dollars(running),
                       'human_event_id': human.get('event_id') if human else None})
        if approved and cents(approved['amount']) != amount:
            errors.append(f'Approval and ledger amount disagree for payment {payment["id"]}.')
        if not human:
            errors.append(f'Payment {payment["id"]} lacks a matching human approval event after reset.')
    balances = {'computed_ending': running,
                'api_ending': cents(state['cash']['balance']),
                'mcp_ending': cents(shop['checking']['balance']),
                'database_ending': cents(working['checking']['balance'])}
    if len(set(balances.values())) != 1:
        errors.append('Computed, API, MCP, and database checking balances do not all match.')
    if cents(state['cash'].get('starting_balance', dollars(start))) != start:
        errors.append('The API starting balance does not match the supplied original database.')
    database_payments = [row for row in working['payments'] if row['id'] not in original_payments]
    if payments != database_payments:
        errors.append('MCP payment ledger does not match the read-only working database verification.')
    changes = {str(ticket): dollars(-sum(cents(row['amount']) for row in ledger if row['ticket_id'] == ticket))
               for ticket in TICKETS}
    return {'starting_checking': dollars(start), 'payment_ledger': ledger,
            'ticket_cash_changes': changes, 'approved_outflow': dollars(start - running),
            **{key: dollars(value) for key, value in balances.items()},
            'verified': not errors, 'errors': errors}


def ticket_evidence(ticket: dict[str, Any], shop: dict[str, Any], events: list[dict[str, Any]],
                    runs: list[dict[str, Any]], cash: dict[str, Any], verified: dict[str, Any]) -> dict[str, Any]:
    ident = int(ticket.get('ticket_id', ticket.get('id')))
    selected = [event for event in events if event.get('ticket_id') == ident]
    contributions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in selected:
        if event.get('event_type') == 'agent_completed' and event.get('agent') in AGENTS:
            contributions[event['agent']].append({
                'run_id': event['run_id'], 'event_id': event['event_id'],
                'summary': event.get('message', ''), 'timestamp': event.get('timestamp'),
                'awaiting_approval': (event.get('output') or {}).get('awaiting_approval', False),
                'evidence': (event.get('output') or {}).get('evidence', [])})
    edges = [{**event.get('delegation', {}), 'run_id': event['run_id'],
              'event_id': event['event_id'], 'timestamp': event.get('timestamp')}
             for event in selected if event.get('event_type') == 'delegation']
    calls = [event for event in selected if event.get('event_type') == 'tool_call']
    first = next((edge for edge in edges if edge.get('from') == 'Boss'), None)
    outcome = next((row for row in shop.get('outcomes', []) if row['ticket_id'] == ident), {})
    actual_agents = [agent for agent in AGENTS if any(event.get('agent') == agent for event in selected)]
    financial_contexts = [event['output'] for event in selected
                          if event.get('event_type') == 'tool_result'
                          and event.get('tool') == 'get_financial_context'
                          and isinstance(event.get('output'), dict)]
    chronology_notes = []
    first_payment_at = min((event.get('timestamp', '') for event in events
                            if event.get('event_type') == 'human_approval'), default='')
    if ident == 103 and first_payment_at and outcome.get('resolved_at', '') < first_payment_at:
        context = financial_contexts[0] if financial_contexts else {}
        chronology_notes.append(f'Decision chronology: ticket 103 was evaluated and resolved before the two authorized '
                                f'payments. Checking then held {money((context.get("checking") or {}).get("balance", 3400))}, '
                                'but invoice 501 was unpaid and Bulldog Print Co was blocked. Reserving its $840 invoice '
                                'and the imminent $2,400 rent left $160 available for a discretionary purchase. '
                                'The 12 missing medium hoodies cost $264, creating a $104 gap. The later invoice payment '
                                'unblocked the vendor, but paying both obligations left $160 actual checking, so the '
                                '$104 cash gap remained. The stored outcome records the decision-time vendor block; '
                                'it is not a claim that the vendor remained blocked after the invoice payment.')
    review_notes = []
    if ident == 102:
        lease_id = ticket.get('lease_id')
        original_lease = next((row for row in verified['campus_customs.db']['leases'] if row['id'] == lease_id), {})
        current_lease = next((row for row in shop.get('leases', []) if row['id'] == lease_id), {})
        rent = next((row for row in cash['payment_ledger'] if row['ticket_id'] == ident and row['kind'] == 'rent'), None)
        if rent and original_lease and current_lease:
            review_notes.append(f'Review correction: the original rent due date was {original_lease["next_due"]}. '
                                f'Payment {rent["id"]} on {rent["paid_at"]} advanced lease {lease_id} by one month '
                                f'to {current_lease["next_due"]}, as expected. The original ticket note about rent '
                                'due in two days describes the pre-payment obligation; it is historical context, '
                                'not a conflict with the updated lease. The final team summary called it a conflict, '
                                'which is a verification/wording mistake. The payment and due-date transition were correct.')
    return {'id': ident, 'ticket_id': ident, 'title': ticket.get('title', ticket.get('subject', '')),
            'final_status': ticket['status'], 'status': ticket['status'],
            'review_notes': review_notes, 'chronology_notes': chronology_notes, 'financial_contexts_at_decision': financial_contexts,
            'outcome': outcome.get('outcome', ''), 'resolved_at': outcome.get('resolved_at'),
            'short_outcome': outcome.get('outcome', '')[:320],
            'actual_agents': actual_agents, 'unused_agents': [agent for agent in AGENTS if agent not in actual_agents],
            'boss_first_call': first.get('to') if first else None,
            'agent_contributions': dict(contributions), 'delegations': edges,
            'tools_used': sorted({event['tool'] for event in calls if event.get('tool')}),
            'tool_calls_by_agent': {agent: dict(Counter(event['tool'] for event in calls
                                    if event.get('agent') == agent and event.get('tool'))) for agent in actual_agents},
            'delegation_count': len(edges), 'mcp_call_count': len(calls),
            'runs': [run for run in runs if run.get('ticket_id') == ident],
            'usage': summed_usage([run for run in runs if run.get('ticket_id') == ident]),
            'human_approvals': [event for event in selected if event.get('event_type') == 'human_approval'],
            'cash_change': cash['ticket_cash_changes'][str(ident)],
            'payments': [row for row in cash['payment_ledger'] if row['ticket_id'] == ident],
            'drafts': [row for row in shop.get('drafts', []) if row['ticket_id'] == ident],
            'restocks': [row for row in shop.get('restocks', []) if row['ticket_id'] == ident],
            'purchase_orders': [row for row in shop.get('purchase_orders', []) if row['ticket_id'] == ident],
            'source_event_ids': [event['event_id'] for event in selected if event.get('event_id')]}


def paragraphs(lines: list[str]) -> str:
    return ''.join(f'<p>{h(line)}</p>' for line in lines)


def actual_html(ticket: dict[str, Any]) -> str:
    first = ticket['boss_first_call'] or 'No specialist delegation was recorded'
    content = paragraphs([
        f'Final stored status: {ticket["final_status"]}. Boss first called {first}. '
        f'This ticket involved {", ".join(ticket["actual_agents"])}; '
        f'{", ".join(ticket["unused_agents"]) or "no agents"} remained unused.',
        'Stored outcome: ' + ticket['outcome'],
    ])
    if ticket.get('chronology_notes'):
        content += '<div class="review-correction"><h4>Decision chronology</h4>' + paragraphs(ticket['chronology_notes']) + '</div>'
    if ticket.get('review_notes'):
        content += '<div class="review-correction"><h4>Review correction</h4>' + paragraphs(ticket['review_notes']) + '</div>'
    content += '<h4>Actual delegations</h4><ol>'
    for edge in ticket['delegations']:
        content += f'<li><strong>{h(edge.get("from"))} → {h(edge.get("to"))}</strong>: {h(edge.get("task", ""))}</li>'
    content += '</ol><h4>What each agent contributed</h4>'
    for agent in AGENTS:
        completed = ticket['agent_contributions'].get(agent, [])
        content += f'<h5>{h(agent)}</h5>'
        if completed:
            content += '<ul>' + ''.join(f'<li>{h(item["summary"])}</li>' for item in completed) + '</ul>'
        elif agent in ticket['actual_agents']:
            content += '<p>This agent appeared in the recorded steps, but no completed report was recorded. Its participation is shown in the audit; no report is invented here.</p>'
        else:
            content += '<p>Not used on this ticket.</p>'
        tools = ticket['tool_calls_by_agent'].get(agent, {})
        if tools:
            content += '<p class="note">MCP calls: ' + ', '.join(f'<code>{h(tool)}</code> × {count}' for tool, count in sorted(tools.items())) + '.</p>'
    content += '<h4>Tools, human approvals, and bounded runs</h4>'
    content += '<p>Actual MCP tools: ' + ', '.join(f'<code>{h(tool)}</code>' for tool in ticket['tools_used']) + '.</p>'
    if ticket['payments']:
        for row in ticket['payments']:
            content += paragraphs([f'{row["approved_by"]} approved {row["description"]} for {money(row["amount"])} '
                                   f'to {row["payee"] or "the ledger payee"}. Payment {row["id"]} changed checking '
                                   f'from {money(row["checking_before"])} to {money(row["checking_after"])}.'])
    else:
        content += '<p>No approved payment or purchase changed checking on this ticket.</p>'
    content += paragraphs([f'This final-reset generation contains {len(ticket["runs"])} run(s), '
                           f'{ticket["delegation_count"]} specialist delegations, and {ticket["mcp_call_count"]} MCP calls. '
                           f'The final shared counters across its distinct runs record {ticket["usage"]["requests"]} model requests, '
                           f'{ticket["usage"]["input_tokens"]:,} input tokens and {ticket["usage"]["output_tokens"]:,} output tokens. '
                           'Counters attached to intermediate steps were not summed.'])
    content += '<details><summary>Evidence references</summary><p>' + h(', '.join(ticket['source_event_ids'])) + '</p></details>'
    return content


def cash_html(cash: dict[str, Any]) -> str:
    content = paragraphs([f'Starting checking after the final reset: {money(cash["starting_checking"])}. '
                           'The starting amount was verified against the supplied original database.'])
    content += '<div class="table-wrap"><table><thead><tr><th>Ticket</th><th>Approved payment / purchase</th><th>Cash change</th><th>Explanation</th></tr></thead><tbody>'
    for ident in TICKETS:
        rows = [row for row in cash['payment_ledger'] if row['ticket_id'] == ident]
        labels = '; '.join(row['description'] or row['kind'] for row in rows) or 'No approved payment or purchase'
        why = '; '.join(f'{row["payee"] or row["kind"]}, payment {row["id"]}, approved by {row["approved_by"]}' for row in rows) or 'No ledger outflow was recorded for this ticket.'
        content += f'<tr><td>{ident}</td><td>{h(labels)}</td><td>{h(money(cash["ticket_cash_changes"][str(ident)]))}</td><td>{h(why)}</td></tr>'
    content += '</tbody></table></div><h3>Ledger reconciliation</h3><ol>'
    for row in cash['payment_ledger']:
        content += f'<li>Payment {row["id"]}, ticket {row["ticket_id"]}: {h(money(row["checking_before"]))} − {h(money(row["amount"]))} = {h(money(row["checking_after"]))}.</li>'
    content += '</ol>'
    content += paragraphs([f'{money(cash["starting_checking"])} − {money(cash["approved_outflow"])} = '
                           f'{money(cash["computed_ending"])} ending checking. '
                           f'This matches the API, MCP snapshot, and cash_accounts in the working database '
                           f'({money(cash["database_ending"])}). Prepared or pending approvals do not count as cash movements.'])
    return content


def reflection_html(tickets: list[dict[str, Any]], cash: dict[str, Any],
                    earlier: list[dict[str, Any]]) -> str:
    by_id = {ticket['id']: ticket for ticket in tickets}
    content = '<p class="note">Prepared evidence-based analysis for Wendell’s review. The observations below use the recorded app run, ticket outcomes, tool calls, drafts, approvals, and cash ledger; they do not invent a personal account of using the app.</p>'
    content += '<h3>How well the agents handled each ticket</h3>'
    anchors = {
        101: 'The important distinction is resolving the request versus physically fulfilling the tee. Invoice payment can remove the supplier block, but a lead-time estimate and internal follow-up do not prove shipment or receipt. The restock and saved customer draft are the evidence for whether the team communicated that limit honestly.',
        102: 'This is the cleanest operational success to evaluate: the linked rent obligation needs an approved payment, a real ledger entry, and the lease due date advanced. A polite landlord draft adds communication, but the financial state is what makes the resolution credible.',
        103: 'This request tests judgment rather than a purchase button. The team must distinguish the gross profit on the whole order from the cash needed to acquire the stock shortfall. A profitable proposed discount can still be unaffordable; a deferred or smaller offer can be a valid ticket resolution without pretending all 20 hoodies are available.',
    }
    for ident in TICKETS:
        ticket = by_id[ident]
        reports = sum(len(items) for items in ticket['agent_contributions'].values())
        content += f'<h4>Ticket {ident}: {h(ticket["title"])}</h4>'
        content += paragraphs([
            'The stored outcome was: ' + ticket['outcome'],
            f'The team used {", ".join(ticket["actual_agents"])} and recorded {reports} completed reports, '
            f'{ticket["delegation_count"]} delegations and {ticket["mcp_call_count"]} MCP calls across '
            f'{len(ticket["runs"])} run(s). Cash changed by {money(ticket["cash_change"])}. '
            + anchors[ident],
            ('A human approval paused this work before money moved; the resumed run then had to verify the changed MCP facts. '
             'That gives the payment a clear human decision and an inspectable ledger trail.'
             if ticket['human_approvals'] else
             'No human payment approval was recorded for this ticket. Its resolution should therefore be assessed as the saved decision and draft, not as an executed cash transaction.'),
            f'The cost of this coordination was {ticket["usage"]["requests"]} model requests and '
            f'{ticket["usage"]["total_tokens"]:,} input-plus-output tokens. The roles made the route understandable, '
            'but repeated reads and report handoffs are overhead worth comparing with a simpler one-agent workflow.',
        ])
        if ticket.get('review_notes'):
            content += '<div class="review-correction">' + paragraphs(ticket['review_notes']) + '</div>'
        failures = [run for run in ticket['runs'] if run.get('status') == 'failed']
        if failures:
            content += paragraphs([f'{len(failures)} run(s) in this generation failed or stopped before resolution. '
                                   'Those failed steps remain in the same audit and are included in the workload counts; the final stored outcome does not make those failures disappear.'])
    content += '<h3>Actual compared with the Expected plans</h3>'
    for ident in TICKETS:
        ticket = by_id[ident]
        expected_first = 'Facilities' if ident == 102 else 'Inventory'
        first = ticket['boss_first_call'] or 'no recorded specialist'
        edges = '; '.join(f'{edge.get("from")} → {edge.get("to")}' for edge in ticket['delegations']) or 'none'
        expected_delta = {101: -840, 102: -2400, 103: 0}[ident]
        match = 'matched' if first == expected_first else 'differed from'
        content += f'<h4>Ticket {ident}</h4>'
        content += paragraphs([
            f'Expected called for Boss → {expected_first} first. Actual started with Boss → {first}, which {match} '
            f'the first-call plan. The full recorded outbound delegation sequence was: {edges}. '
            'The Expected section above remains intact so the route can be compared directly rather than rewritten after the fact.',
            f'Expected cash change was {money(expected_delta)}; actual cash change was {money(ticket["cash_change"])}. '
            f'The agents actually used {", ".join(ticket["tools_used"])}. '
            + (f'Actual used {first} as the first specialist. The recorded drafting role(s) were '
               f'{", ".join(agent for agent, tools in ticket["tool_calls_by_agent"].items() if "draft_message" in tools) or "not recorded"}. '
               'The expected separate Boss → Customer Service drafting handoff did not occur in this generation; '
               'Inventory saved the customer draft itself. The team also persisted schedule_restock, which was not '
               'named in the original expected tool list, to keep the paid-invoice follow-up distinct from receipt.' if ident == 101 else
               'Actual kept the expected Boss → Facilities → Accounting path, and repeated that path after human approval '
               'to verify the posted payment. Boss saved the landlord draft and resolved the ticket; Customer Service '
               'did not participate. The financial plan matched, while the final summary’s stale-note conflict wording '
               'needs the explicit correction above.' if ident == 102 else
               f'Actual began with Boss → {first}, then Customer Service → Inventory → Accounting. '
               'That put the customer-facing response owner before the stock and finance review, unlike the expected '
               'Inventory-first route. The expected feasibility constraint still drove the outcome: the 12-unit '
               'shortfall cost $264 against $160 reserved-available funds. Discounted pricing remained unconfirmed; '
               'no purchase, revenue, or stock movement was invented.'),
        ])
    if by_id[103].get('chronology_notes'):
        content += '<h4>The sequence matters on ticket 103</h4>' + paragraphs(by_id[103]['chronology_notes'])
    content += '<h3>What would have been simpler with one agent and tools?</h3>'
    content += paragraphs([
        'Ticket 102 is a good candidate. One agent could read the lease and checking evidence, prepare the exact rent approval, stop for the human, then verify the ledger and draft the landlord response. Splitting Facilities and Accounting makes ownership visible, but the task has a short, mostly deterministic state transition.',
        'Ticket 101 also repeats some facts when Boss, Inventory and Accounting each inspect the same ticket. A single agent could maintain the stockout, invoice blocker and vendor lead time in one context, prepare the invoice, then save the follow-up and response after approval. Separate specialists are more useful if stock allocation or supplier alternatives become substantially more complex.',
        f'For ticket 103, one agent with get_inventory and get_financial_context could compare stock, '
        f'unit cost, requested quantity and the final {money(cash["database_ending"])} balance, then draft an honest smaller/deferred offer. '
        'The multi-agent route provides an inspectable financial review, but forwarding the same calculations among agents adds requests and makes mistakes harder to attribute. In either architecture, human approval and MCP cash safeguards still belong in deterministic tools.',
    ])
    content += '<h3>Three new problems this team can solve with these tools</h3><p>These are new instances of supported ticket types, assuming the request and referenced invoice, lease, or stock record already exist in the shop database. The current tools do not provide new-ticket intake or external message delivery.</p><ol>'
    content += '<li><strong>Another customer request for a sold-out size tied to an existing unpaid reprint invoice.</strong> Inventory can inspect that SKU and size; Accounting can assess and prepare the existing invoice payment; after a human approves, the team can save an internal restock follow-up and an honest customer draft. This reuses the constrained path exercised on 101 and still does not claim physical fulfillment.</li>'
    content += '<li><strong>A new notice about a lease payment due soon.</strong> Facilities can read the recorded lease, Accounting can check cash and prepare its exact rent payment, and the human can approve. The ledger and next_due transition used on 102 provide an auditable outcome and a locally saved landlord draft.</li>'
    content += '<li><strong>A student organization asks whether a smaller recorded-stock order or a different proposed discount is feasible.</strong> Inventory and Accounting can compare actual stock, prices, total margin and incremental replenishment cash, then Customer Service can save an offer or decline draft. The reasoning used on 103 supports this decision; these tools do not establish a discount policy, collect money, reserve stock, or send the offer.</li></ol>'
    content += '<h3>Three new problems the current tools cannot solve</h3><ol>'
    content += '<li><strong>A supplier shipment is lost and the customer needs a confirmed delivery date.</strong> schedule_restock stores an internal lead-time estimate, not a carrier tracking record or goods receipt. The app would need supplier confirmation, purchase-order shipment updates, carrier tracking, and receipt tools, with a Fulfillment or Supplier Operations agent. Ticket 101 is the concrete reason those distinctions matter.</li>'
    content += '<li><strong>A landlord disputes a payment or the shop needs to renegotiate its lease.</strong> The present ledger can show the internal rent action from 102 but cannot read bank settlement, send evidence to the landlord, or change lease terms. Bank reconciliation, document retrieval, landlord messaging, and a human-reviewed lease amendment workflow would be needed, with a Facilities/Lease Administration specialist and Accounting for settlement verification.</li>'
    content += '<li><strong>The Yale AI Club accepts a 20-hoodie quote and wants to pay a deposit to fund replenishment.</strong> Ticket 103 cannot create cash from a hypothetical sale. The app needs quote acceptance, customer invoicing, payment collection, deposit accounting, inventory reservation, and order fulfillment tools, with Sales/Order Management and Payment/Reconciliation agents plus human oversight. Only those recorded deposits could change the current cash constraint.</li></ol>'
    content += '<h3>Evidence boundaries</h3>'
    older_failures = {event.get('run_id') for event in earlier if event.get('event_type') == 'run_error'}
    content += paragraphs([
        'All three stored statuses are resolved, but the saved outcomes define what that means. '
        'Drafts remain unsent, restock dates remain estimates, and no unrecorded customer revenue is assumed. '
        f'The Cash tab reconciles {money(cash["starting_checking"])} starting checking, '
        f'{money(cash["approved_outflow"])} actual approved outflow and {money(cash["database_ending"])} ending checking.',
        f'{len(older_failures)} earlier failed trial run(s) occurred before the final reset and remain in audit_trail.json. '
        'They are excluded from this final-reset generation’s outcomes, contributions and usage totals. '
        'Within each run, shared cumulative counters are read once from the final available counter snapshot; repeated step snapshots are never added.',
    ])
    return content


def replace_placeholder(document: str, attribute: str, body: str) -> str:
    pattern = re.compile(r'<div\s+' + attribute + r'\s*>')
    matches = list(pattern.finditer(document))
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one {attribute} placeholder; found {len(matches)}. Original HTML was preserved.')
    opening = matches[0]
    depth = 1
    # Match the corresponding closing div, including nested divs inserted by a
    # previous export. Escaped evidence text cannot masquerade as markup.
    for tag in re.finditer(r'<div\b[^>]*>|</div\s*>', document[opening.end():], re.IGNORECASE):
        depth += -1 if tag.group(0).lower().startswith('</div') else 1
        if depth == 0:
            closing_start = opening.end() + tag.start()
            return document[:opening.end()] + body + document[closing_start:]
    raise RuntimeError(f'No closing div found for {attribute}; original HTML was preserved.')


CSS = '''body{margin:0;background:#f5f2ea;color:#172b3d;font:16px/1.65 system-ui}main{max-width:1120px;margin:35px auto;padding:24px}h1{font-family:Georgia,serif;font-size:38px;letter-spacing:-1px;font-weight:400}h2{font-size:25px}h3{font-size:19px;margin-top:28px}h4{font-size:17px;margin-top:24px}h5{font-size:15px;margin:20px 0 6px}nav{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid #d1d7d5;padding-bottom:16px}button{padding:12px 18px;border:1px solid #d1d7d5;background:white;border-radius:8px;font:inherit;cursor:pointer}button[aria-selected=true]{background:#172b3d;color:white}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #90b5a9;outline-offset:4px}section{display:none;background:white;border:1px solid #d1d7d5;border-radius:14px;padding:28px;margin-top:22px}section.active{display:block}.eyebrow{letter-spacing:2px;font-size:12px;text-transform:uppercase;color:#567064}.note{color:#586b77}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd;vertical-align:top}.table-wrap{overflow-x:auto}code{background:#f0f2ef;padding:3px 5px;overflow-wrap:anywhere}a{color:#2a6957}details{margin:14px 0}details p{overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer;color:#567064}.review-correction{background:#faf2df;border:1px solid #e1cf9c;border-radius:8px;padding:14px 18px;margin:16px 0}.review-correction h4{margin:0 0 8px}.review-correction p{margin:6px 0}figure{margin:30px 0;padding:20px;background:white;border:1px solid #d1d7d5;border-radius:12px}figure img{display:block;width:100%;height:auto;border-radius:8px}figcaption{margin-top:15px;color:#586b77;font-size:14px}.board-label{display:inline-block;font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#567064}@media(max-width:600px){main{margin:12px auto;padding:14px}section{padding:18px}h1{font-size:30px}figure{padding:10px}td,th{padding:8px;font-size:13px}}'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--mcp-url', default=os.environ.get('MCP_URL', 'http://127.0.0.1:8001/mcp'))
    parser.add_argument('--snapshot-only', action='store_true', help='Save a snapshot/progress record without changing final artifacts.')
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    state = get_api(args.api_url)
    shop = asyncio.run(get_shop(args.mcp_url))
    audit_path = OUTPUT / 'audit_trail.json'
    audit = json.loads(audit_path.read_text()) if audit_path.exists() else []
    if not isinstance(audit, list):
        raise RuntimeError('The existing audit trail is not a JSON list; it was preserved.')
    reset, events, earlier = generation(audit)
    verified = readonly_verification()
    cash = reconcile(state, shop, verified, events)
    runs = run_records(state, events)
    records = [ticket_evidence(ticket, shop, events, runs, cash, verified)
               for ticket in state.get('tickets', []) if int(ticket.get('ticket_id', ticket.get('id'))) in TICKETS]
    db_status = {row['id']: row['status'] for row in verified['campus_customs_new.db']['tickets']}
    mcp_status = {row['id']: row['status'] for row in shop.get('tickets', [])}
    complete = len(records) == 3 and all(ticket['final_status'] == 'resolved'
                                        and db_status.get(ticket['id']) == 'resolved'
                                        and mcp_status.get(ticket['id']) == 'resolved'
                                        and bool(ticket['outcome']) for ticket in records)
    screenshots = {ident: OUTPUT / f'board-resolved{ident}.jpg' for ident in TICKETS}
    missing = [path.name for path in screenshots.values() if not path.is_file() or path.stat().st_size == 0]
    snapshot = {'captured_at': datetime.now(timezone.utc).isoformat(),
                'complete': complete, 'latest_reset': reset,
                'final_generation_event_count': len(events), 'earlier_event_count': len(earlier),
                'api_state': state, 'shop_snapshot': shop, 'database_verification': verified,
                'cash_reconciliation': cash, 'runs': runs,
                'source_files': ['audit_trail.json', 'data/campus_customs.db', 'data/campus_customs_new.db']}
    write_json(OUTPUT / 'run_snapshot.json', snapshot)
    progress = {'captured_at': snapshot['captured_at'], 'complete': complete,
                'final_artifacts_written': False, 'latest_reset_event_id': reset.get('event_id') if reset else None,
                'ticket_statuses': {str(ticket['id']): ticket['final_status'] for ticket in records},
                'pending_approvals': [row for row in shop.get('approvals', []) if row.get('status') == 'pending'],
                'cash_reconciliation': cash, 'missing_screenshots': missing,
                'reasons': []}
    if reset is None:
        progress['reasons'].append('No recorded database_reset precedes the final test generation.')
    if any(run.get('status') == 'running' for run in runs):
        progress['reasons'].append('A recorded agent run is still running; wait for its final shared usage report.')
    if not complete:
        progress['reasons'].append('All three tickets must have matching resolved API/MCP/database statuses and stored outcomes.')
    if not cash['verified']:
        progress['reasons'].extend(cash['errors'])
    if missing:
        progress['reasons'].append('Existing React-board screenshots are required for each resolved ticket.')
    if args.snapshot_only:
        progress['reasons'].append('Snapshot-only mode was requested.')
    if progress['reasons']:
        write_json(OUTPUT / 'run_progress.json', progress)
        print('Saved truthful snapshot/progress only. ' + ' '.join(progress['reasons']))
        return 0
    if any(row.get('status') == 'pending' for row in shop.get('approvals', [])):
        raise RuntimeError('A pending approval remains; final export refused.')
    document_path = OUTPUT / 'desk_tickets.html'
    document = document_path.read_text()
    for ticket in records:
        document = replace_placeholder(document, f'data-actual="{ticket["id"]}"', actual_html(ticket))
    document = replace_placeholder(document, 'data-cash', cash_html(cash))
    document = replace_placeholder(document, 'data-reflection', reflection_html(records, cash, earlier))
    document = re.sub(r'<style>.*?</style>', lambda _: '<style>' + CSS + '</style>', document, count=1, flags=re.DOTALL)
    document = document.replace('<link rel="stylesheet" href="evidence.css">', '<style>' + CSS + '</style>')
    document = document.replace('Actual results, cash reconciliation, and reflection will be filled from the saved run evidence.',
                                'Actual results, cash reconciliation, and the prepared reflection below come from the saved final-reset run evidence. Review them alongside the preserved Expected plans.')
    board = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Campus Customs · Resolved board evidence</title><style>' + CSS + '</style></head><body><main><div class="eyebrow">Homework 5 · Recorded React dashboard</div><h1>Three tickets. Recorded outcomes.</h1><p class="note">These are the existing screenshots captured from the real React board after each ticket resolved. The ticket outcomes and cash figures below were read from the final persisted state. Screenshots may show earlier cumulative cash before later tickets were completed.</p>'
    for ticket in records:
        board += f'<figure><span class="board-label">Ticket {ticket["id"]} · Resolved</span><h2>{h(ticket["title"])}</h2><img src="{h(screenshots[ticket["id"]].name)}" alt="Campus Customs React dashboard showing resolved ticket {ticket["id"]}"><figcaption>{h(ticket["outcome"])}</figcaption>'
        if ticket.get('review_notes'):
            board += '<div class="review-correction">' + paragraphs(ticket['review_notes']) + '</div>'
        if ticket.get('chronology_notes'):
            board += '<div class="review-correction">' + paragraphs(ticket['chronology_notes']) + '</div>'
        board += '</figure>'
    board += '<p><a href="desk_tickets.html">Review Expected, Actual, Cash, and Reflection</a></p></main></body></html>'
    result = {'captured_at': snapshot['captured_at'], 'source': 'live API, MCP shop snapshot, append-only audit after latest reset, read-only database QA',
              'latest_reset_event_id': reset.get('event_id'), 'tickets': records, 'cash': cash,
              'shared_usage_across_distinct_runs': summed_usage(runs),
              'delegation_count': sum(ticket['delegation_count'] for ticket in records),
              'mcp_call_count': sum(ticket['mcp_call_count'] for ticket in records)}
    # Prepare every artifact first, then replace files without changing the audit.
    write_json(OUTPUT / 'resolved_tickets.json', result)
    (OUTPUT / 'evidence.css').write_text(CSS + '\n')
    (OUTPUT / 'resolved_board.html').write_text(board)
    document_path.write_text(document)
    progress['final_artifacts_written'] = True
    write_json(OUTPUT / 'run_progress.json', progress)
    print(f'Exported all three resolved tickets; verified checking {money(cash["starting_checking"])} − '
          f'{money(cash["approved_outflow"])} = {money(cash["database_ending"])}. Expected plans and the full audit were preserved.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'Export stopped: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
