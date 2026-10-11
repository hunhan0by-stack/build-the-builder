from pathlib import Path
from types import SimpleNamespace
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.models import FoundationError
from orchestration.contest_build import Budget, KIT
from orchestration.contest_practice import run_practice


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return json.dumps(self.body).encode()


class PracticeProofTests(unittest.TestCase):
    def test_reservation_requires_matching_terminal_and_resets_per_repeat(self):
        from orchestration.workspace_lock import SimulationReservation
        with tempfile.TemporaryDirectory() as directory:
            guard = SimulationReservation(directory, 'synthetic-workspace', require_protected=False)
            guard.reserve({'synthetic': True})
            restarted = SimulationReservation(directory, 'synthetic-workspace')
            with self.assertRaisesRegex(FoundationError, 'PENDING_NO_REPLAY'):
                restarted.require_available()
            guard.created({'id': 'first'})
            guard.observe_terminal({'id': 'foreign', 'status': 'completed'})
            with self.assertRaisesRegex(FoundationError, 'TERMINAL_UNMEASURED'):
                guard.release_after_local_terminal()
            guard.observe_terminal({'id': 'first', 'status': 'completed'})
            guard.release_after_local_terminal()
            restarted.require_available()
            guard.reserve({'synthetic': 'second'})
            guard.created({'id': 'second'})
            with self.assertRaisesRegex(FoundationError, 'TERMINAL_UNMEASURED'):
                guard.release_after_local_terminal()
            guard.observe_terminal({'id': 'second', 'status': 'completed'})
            value = json.loads(guard.path.read_text(encoding='utf-8'))
            value['owner_sha256'] = 'foreign'
            guard.path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(FoundationError, 'OWNERSHIP_CHANGED'):
                guard.release_after_local_terminal()

    def test_protected_reservation_refuses_plaintext_release_and_untyped_terminal(self):
        from orchestration.workspace_lock import SimulationReservation
        with tempfile.TemporaryDirectory() as directory:
            guard = SimulationReservation(directory, 'synthetic-workspace')
            guard.reserve({'synthetic': True})
            guard.created({'id': 'first'})
            guard.observe_terminal({'id': 'first', 'status': True})
            self.assertFalse(guard.terminal)
            guard.observe_terminal({'id': 'first', 'status': 'completed'})
            with self.assertRaisesRegex(FoundationError, 'PROTECTED_CHAIN_REQUIRED'):
                guard.release_after_local_terminal()
            self.assertTrue(guard.path.exists())
            guard.observe_terminal({'id': 'first', 'status': 'running'})
            self.assertFalse(guard.terminal)

    def test_real_client_without_protected_capture_is_rejected_before_any_dispatch(self):
        from orchestration.contest_build import FreyaClient
        client = FreyaClient.__new__(FreyaClient)
        client.evidence = None
        client.api = lambda *a, **k: self.fail('UNSEALED_PROVIDER_DISPATCH_FORBIDDEN')
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(FoundationError, 'PROTECTED_CHAIN_REQUIRED_NO_DISPATCH'):
            run_practice(client=client, publication={}, case_id='PUB-01', repetitions=1,
                contract=BankContract.from_path(KIT / 'tools/openapi.json'),
                directory=directory, budget=Budget(60))

    def test_each_repeat_uses_fresh_session_and_same_published_version(self):
        case = next(row for row in json.loads((KIT / 'public-test-set.json').read_text()) if row['id'] == 'PUB-01')
        bank_requests, simulations = [], []
        next_customer = [0]
        ledger = {key: copy.deepcopy(case['expected'][key]) for key in ('outcome', 'promises', 'notes', 'callbacks', 'transfers')}
        ledger['reason'] = case['expected'].get('reason')
        ledger['calls'] = [{'tool': tool, 'result': {'ok': True}} for tool in (
            'verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call')]

        def bank(request, **kwargs):
            bank_requests.append(request.full_url)
            if request.method == 'POST':
                next_customer[0] += 1
                return Response({'variables': {**case['variables'], 'customer_id': 'fresh-' + str(next_customer[0])}})
            return Response(ledger)

        def api(method, path, body=None, **params):
            if path == '/api/v2/simulations' and method == 'GET':
                return {'simulations': [], 'total': 0}
            if path == '/api/v2/personas':
                return {'personas': [{'id': 'persona-local'}]}
            if path == '/api/v2/simulations':
                simulations.append(body)
                return {'simulation': {'id': 'sim-' + str(len(simulations)), 'settings': {'maxRetries': 0}}}
            if path.endswith('/iterations'):
                return {'iterations': [{'id': 'iteration-local'}]}
            if '/iterations/' in path:
                return {'iteration': {'agentVersionId': 'version-frozen', 'agentVersionNumber': 1,
                    'params': simulations[-1]}, 'runs': [{'personaId': 'persona-local', 'callId': 'call-local', 'outcome': 'pass'}]}
            if path.startswith('/api/v2/call/'):
                return {'call': {'messages': [{'role': 'assistant', 'message': 'Recorded.'}]}}
            return {'simulation': {'id': path.rsplit('/', 1)[-1], 'status': 'completed'}}

        client = SimpleNamespace(kit=SimpleNamespace(workspace_id=lambda: 'practice-unit-local'),
                                 api=api, opener=SimpleNamespace(open=bank))
        publication = {'agent_id': 'agent-local', 'version_id': 'version-frozen', 'version_number': 1}
        with tempfile.TemporaryDirectory() as directory, patch('orchestration.contest_build.ROOT', Path(directory)), patch.dict('os.environ', {'FREYA_BANK_TEAM_KEY': 'synthetic-local-test-value'}), patch('orchestration.contest_practice.time.sleep'):
            proof = run_practice(client=client, publication=publication, case_id='PUB-01',
                repetitions=3, contract=BankContract.from_path(KIT / 'tools/openapi.json'),
                directory=directory, budget=Budget(60))
        self.assertTrue(proof['passed'])
        self.assertEqual(len(simulations), 3)
        self.assertTrue(all(type(body.get('maxRetries')) is int and body['maxRetries'] == 0 for body in simulations))
        customer_ids = [body['customerVariablesMap']['persona-local']['customer_id'] for body in simulations]
        self.assertEqual(len(set(customer_ids)), 3)
        self.assertTrue(all(body['agentVersionId'] == 'version-frozen' for body in simulations))
        self.assertEqual(len([url for url in bank_requests if '/ledger/' in url]), 3)

    def test_invalid_inventory_is_rejected_before_persona_or_bank(self):
        bad = ({}, {'simulations': None}, {'simulations': [] ,'items': [{}]},
               {'simulations': [None]}, {'simulations': [{'status': 'running'}]},
               {'simulations': [{'status': True}]}, {'simulations': []},
               {'simulations': [], 'total': True}, {'simulations': [], 'total': -1},
               {'simulations': [], 'total': 1},
               {'simulations': [{'status': 'completed', 'workspaceId': 'foreign'}], 'total': 1})
        for inventory in bad:
            with self.subTest(inventory=inventory), tempfile.TemporaryDirectory() as directory:
                calls = []
                def api(method, path, body=None, **params):
                    calls.append((method, path))
                    if method != 'GET' or path != '/api/v2/simulations':
                        self.fail('MUTATION_BEFORE_VALID_INVENTORY')
                    return inventory
                client = SimpleNamespace(kit=SimpleNamespace(workspace_id=lambda: 'inventory-unit-local'),
                    api=api, opener=SimpleNamespace(open=lambda *a, **k: self.fail('BANK_FORBIDDEN')))
                with patch('orchestration.contest_build.ROOT', Path(directory)), patch.dict('os.environ', {'FREYA_BANK_TEAM_KEY': 'synthetic-local-test-value'}), self.assertRaises(FoundationError):
                    run_practice(client=client, publication={}, case_id='PUB-01', repetitions=1,
                        contract=BankContract.from_path(KIT / 'tools/openapi.json'),
                        directory=directory, budget=Budget(60))
                self.assertEqual(calls, [('GET', '/api/v2/simulations')])

    def test_public_cases_cannot_be_applied_to_a_hidden_bank(self):
        contract = BankContract.from_path(KIT / 'tools/openapi.json')
        contract.server = 'https://different-bank.invalid/v1'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FoundationError, 'DIFFERENT_BANK'):
                run_practice(client=object(), publication={}, case_id='PUB-01', repetitions=1,
                             contract=contract, directory=directory, budget=Budget(60))

    def test_case_without_publish_is_rejected_before_any_model_or_api_call(self):
        from orchestration.contest_build import OUTPUT, run
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            trace = run(brief_path='missing', tool_api_path='missing', directory=Path(temporary) / 'run',
                        practice_case='PUB-01', publish_live=False)
            self.assertEqual(trace['diagnostic'], 'CONTEST_PRACTICE_REQUIRES_PUBLISHED_WORKFLOW')
            self.assertEqual(trace['provider_actions'], [])


if __name__ == '__main__':
    unittest.main()
