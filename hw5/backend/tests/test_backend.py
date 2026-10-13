"""Focused tests use fakes/disposable evidence; they never approve the working shop."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
import main
from agents import AgentTeam, TeamDeps, LIMITS, SHOP_TOOLS
from audit import append_event, read_events
from models import RunRecord


class AuditTests(unittest.TestCase):
    def test_concurrent_append_retains_evidence_and_redacts_secret_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audit.json'
            def record(index):
                append_event(run_id=str(index), ticket_id=101, agent='Accounting', event_type='test',
                             message='Test event.', input={'approval_token': 'never-store-this'}, audit_path=path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(record, range(24)))
            events = read_events(audit_path=path)
            self.assertEqual(len(events), 24)
            self.assertEqual({event['run_id'] for event in events}, {str(i) for i in range(24)})
            self.assertNotIn('never-store-this', path.read_text())


class TeamGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.team = AgentTeam(model=TestModel())
        self.deps = TeamDeps('test', 101, 'Inventory', ('Boss', 'Inventory'), RunUsage(), self.team)

    def test_all_five_roles_have_delegation_and_admin_tools_are_hidden(self):
        self.assertEqual(len(self.team.agents), 5)
        self.assertNotIn('approve_payment', SHOP_TOOLS)
        self.assertNotIn('reset_database', SHOP_TOOLS)
        for agent in self.team.agents.values():
            self.assertIn('delegate', agent._function_toolset.tools)
        self.assertEqual(LIMITS.request_limit, 32)
        self.assertEqual(LIMITS.total_tokens_limit, 75000)
        self.assertEqual(LIMITS.tool_calls_limit, 60)
        self.assertFalse(self.team.tool_allowed(SimpleNamespace(deps=self.deps), SimpleNamespace(name='resolve_ticket')))

    async def test_circular_and_depth_guards_stop_before_any_model_call(self):
        ctx = SimpleNamespace(deps=self.deps)
        with self.assertRaises(ModelRetry):
            await self.team.delegate(ctx, 'Boss', 'Investigate.')
        self.deps.path = ('Boss', 'Customer Service', 'Inventory', 'Accounting')
        with self.assertRaises(ModelRetry):
            await self.team.delegate(ctx, 'Facilities', 'Investigate.')

    async def test_mutation_cannot_target_another_ticket(self):
        with self.assertRaises(ModelRetry):
            await self.team.process_mcp_call(SimpleNamespace(deps=self.deps), None,
                                             'prepare_payment', {'ticket_id': 102, 'kind': 'rent'})
        with self.assertRaises(ModelRetry):
            await self.team.process_mcp_call(SimpleNamespace(deps=self.deps), None,
                                             'approve_payment', {'approval_id': 'x'})


class ApiTests(unittest.TestCase):
    def setUp(self):
        main._runs.clear()
        main._tasks.clear()
        self.events = []
        self.patches = [patch.object(main, 'append_event', side_effect=lambda **kw: self.events.append(kw)),
                        patch.object(main, 'read_events', return_value=[])]
        for item in self.patches:
            item.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        main._runs.clear()
        main._tasks.clear()

    def test_actual_payment_outflow_and_source_notes_are_normalized(self):
        snapshot = {'checking': {'name': 'checking', 'balance': 160},
                    'payments': [{'amount': 840}, {'amount': 2400}]}
        cash = main.normalized_cash(snapshot)
        self.assertEqual(cash.starting_balance, 3400)
        self.assertEqual(cash.total_outflow, 3240)
        ticket = main.normalized_ticket({'id': 101, 'notes': 'Need one S tee.', 'status': 'open'})
        self.assertEqual(ticket.description, 'Need one S tee.')

    def test_active_run_blocks_duplicate_runs_reset_and_cash_approval(self):
        record = RunRecord(run_id='active', ticket_id=101, started_at='2026-10-13T00:00:00Z')
        main._runs[record.run_id] = record
        for url, body in [('/api/tickets/101/run', None), ('/api/reset', None),
                          ('/api/approvals/test/approve', {'approved_by': 'Test Person'})]:
            response = self.client.post(url, json=body)
            self.assertEqual(response.status_code, 409, response.text)

    def test_cross_origin_cannot_approve_or_reset(self):
        response = self.client.post('/api/approvals/test/approve', json={'approved_by': 'Test Person'},
                                    headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/reset', headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(response.status_code, 403)

    def test_human_approval_uses_admin_mcp_path_and_resumes_after_commit(self):
        calls = []
        async def fake_shop(name, args=None, *, human=False):
            calls.append((name, args, human))
            return {'ok': True, 'approval': {'ticket_id': 101, 'description': 'Invoice payment'},
                    'checking': {'balance': 2560}}
        async def fake_start(ticket_id):
            calls.append(('resume', ticket_id))
            return RunRecord(run_id='resume', ticket_id=ticket_id, started_at='2026-10-13T00:00:00Z')
        with patch.object(main, 'shop', side_effect=fake_shop), patch.object(main, 'start_run', side_effect=fake_start):
            response = self.client.post('/api/approvals/test/approve', json={'approved_by': 'Test Person'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls[0][0], 'approve_payment')
        self.assertTrue(calls[0][2])
        self.assertEqual(calls[1], ('resume', 101))

    def test_current_experiment_hides_old_events_without_deleting_audit(self):
        old = {'event_type': 'run_finished', 'message': 'Old'}
        reset = {'event_type': 'database_reset'}
        new = {'event_type': 'run_started', 'message': 'New'}
        with patch.object(main, 'read_events', return_value=[old, reset, new]):
            self.assertEqual(main.current_events(), [new])

    def test_idempotent_approval_returns_without_restart(self):
        async def fake_shop(name, args=None, *, human=False):
            return {'ok': True, 'idempotent': True,
                    'approval': {'ticket_id': 101, 'description': 'Invoice payment'}}
        with patch.object(main, 'shop', side_effect=fake_shop), patch.object(main, 'start_run') as resume:
            response = self.client.post('/api/approvals/test/approve', json={'approved_by': 'Test Person'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['idempotent'])
        resume.assert_not_called()

    def test_pending_request_recovers_usage_and_partial_agent_reports(self):
        record = RunRecord(run_id='partial', ticket_id=101, started_at='2026-10-13T00:00:00Z', status='failed')
        evidence = [{'event_type': 'agent_completed', 'agent': 'Accounting', 'message': 'Invoice $840 prepared.',
                     'tokens': {'requests': 18, 'input_tokens': 31000, 'output_tokens': 1154}}]
        with patch.object(main, 'read_events', return_value=evidence):
            main.harvest_partial_report(record)
        async def fake_call(name, args):
            return {'ticket': {'status': 'open'}, 'approvals': [{'status': 'pending', 'amount': 840}]}
        with patch.object(main, 'call_shop_tool', side_effect=fake_call):
            recovered = asyncio.run(main.recover_pending_approval(record))
        self.assertTrue(recovered)
        self.assertEqual(record.status, 'awaiting_approval')
        self.assertEqual(record.usage['requests'], 18)
        self.assertIn('Accounting', record.contributions)
        self.assertIn('$840.00', record.summary)


if __name__ == '__main__':
    unittest.main()
