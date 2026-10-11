"""Actual contest main -> fresh FreyaClient -> separate chat proof, no network.

The model and HTTP boundary are deterministic synthetic fixtures. The inherited
TEST_ONLY protector is reversible and is never reported as DPAPI encryption.
No existing private run, credential store, model process or real bank is read.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import orchestration.contest_build as entry
import orchestration.contest_chat as chat
from integration.contest_evidence import ContestEvidence
from orchestration.contest_chat_evidence import ChatEvidence, verify_chat_bundle
from orchestration.contest_preflight import EVIDENCE_FILES, KIT_FILES
from tests_orchestration.test_contest_entry_evidence import (
    FixtureModel, WireResponse, identity, protect, raw, recover, sha,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1]
KIT = SOURCE_ROOT / 'data/freya-hackathon/kit-v3/build-the-builder'
RECEIPTS = []


class CountedModel(FixtureModel):
    def __init__(self):
        self.calls = 0

    def generate(self, **arguments):
        self.calls += 1
        return super().generate(**arguments)


class HTTP:
    """Only publication/readback/chat; no session, persona, sim or bank route."""
    def __init__(self, test):
        self.test = test
        self.requests, self.transcript = [], []
        self.graph = None
        self.mode = None
        self.turns = 0
        self.ids = {name: identity(number) for number, name in enumerate(
            ('workspace', 'workflow', 'agent', 'version', 'call'), 1)}

    def open(self, request, *, timeout):
        test, ids = self.test, self.ids
        parsed = urlsplit(request.full_url)
        test.assertEqual(parsed.hostname, 'synthetic-provider.invalid')
        method, path = request.get_method(), parsed.path
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = json.loads(request.data) if request.data is not None else None
        sink = test.clients[-1].evidence
        test.assertIsNotNone(sink.pending)
        test.assertTrue((sink.directory / (sink.pending[0] + '.dispatch.json')).is_file())
        if isinstance(sink, ChatEvidence):
            test.assertTrue((sink.directory / ('chat-sequence-' + str(len(sink.sequence)) + '.json')).is_file())
        self.requests.append({'method': method, 'path': path, 'body': body,
                              'request_bytes': request.data})
        if method == 'GET' and path == '/api/v2/workspaces':
            value = {'workspaces': [{'id': ids['workspace']}]}
        elif method == 'POST' and path == '/api/v2/workflow':
            if params.get('dryRun') == 'true':
                value = {'valid': True}
            else:
                self.graph = copy.deepcopy(body['workflow'])
                value = {'workflow': {'id': ids['workflow'], 'agentId': ids['agent']}}
        elif path == '/api/v2/workflow/' + ids['workflow']:
            if method == 'PUT':
                self.graph = copy.deepcopy(body['workflow'])
                value = {}
            else:
                test.assertEqual(method, 'GET')
                value = {'workflow': {'id': ids['workflow'], 'agentId': ids['agent'],
                                      'workflow': copy.deepcopy(self.graph)}}
        elif method == 'GET' and path == '/api/v2/versions/workflow/' + ids['workflow']:
            value = {'versions': [{'id': ids['version'], 'versionNumber': 1, 'isPublished': True}]}
        elif method == 'POST' and path == '/api/v2/chat/message':
            self.turns += 1
            test.assertIsInstance(sink, ChatEvidence)
            if self.mode == 'timeout':
                raise TimeoutError('TEST_ONLY_PRIVATE_UNKNOWN_RESPONSE')
            if self.mode == 'http500':
                content = raw({'success': False, 'data': {'message': 'TEST_ONLY_PRIVATE_HTTP500_BODY'}})
                raise HTTPError(request.full_url, 500, 'synthetic',
                                {'Content-Type': 'application/json'}, io.BytesIO(content))
            self.transcript.extend([
                {'role': 'user', 'message': body['message'], 'messageId': 'synthetic-user-' + str(self.turns)},
                {'role': 'assistant', 'message': 'TEST_ONLY_PRIVATE_REPLY',
                 'messageId': 'synthetic-reply-' + str(self.turns)},
            ])
            if self.mode == 'deadline':
                test.clock[0] = test.budgets[0].end + .5
            value = {'callId': ids['call'], 'messageId': 'synthetic-reply-' + str(self.turns),
                     'response': 'TEST_ONLY_PRIVATE_REPLY', 'endedByAgent': self.turns == 2}
        elif method == 'GET' and path == '/api/v2/call/' + ids['call']:
            value = {'call': {'id': ids['call'], 'workspaceId': ids['workspace'],
                              'assistantWorkflowId': ids['workflow'],
                              'entityVersionId': identity(999) if self.mode == 'foreign_version' else ids['version'],
                              'entityVersionNumber': 1,
                              'startedAt': '2026-10-11T00:00:00Z', 'endedAt': '2026-10-11T00:01:00Z',
                              'messages': copy.deepcopy(self.transcript)}}
        else:
            raise AssertionError('NO_OTHER_HTTP_ROUTE_AUTHORIZED')
        content = raw({'success': True, 'data': {'result': value}})
        self.requests[-1]['response_bytes'] = content
        return WireResponse(content)


class ChatEntryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='synthetic-chat-entry-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'isolated-source'
        self.output = self.root / 'artifacts/final-builder/chat-entry'
        self.root.mkdir()
        # An explicit public source closure; never enumerate private runs/state.
        for package in ('builder', 'compiler', 'orchestration'):
            for source in (SOURCE_ROOT / package).glob('*.py'):
                target = self.root / source.relative_to(SOURCE_ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        for name in EVIDENCE_FILES:
            target = self.root / 'integration' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE_ROOT / 'integration' / name).read_bytes())
        for name in KIT_FILES:
            target = self.root / 'data/freya-hackathon/kit-v3/build-the-builder' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((KIT / name).read_bytes())
        self.fixture_path = self.base / 'synthetic-fixture.json'
        self.fixture = {'customerVariables': {'customer_id': 'TEST_ONLY_PRIVATE_CUSTOMER',
                                             'first_name': 'SYNTHETIC_PERSON'},
                        'messages': ['TEST_ONLY_PRIVATE_USER_ONE', 'TEST_ONLY_PRIVATE_USER_TWO']}
        self.fixture_path.write_bytes(raw(self.fixture))
        self.clients, self.budgets, self.publish_sinks, self.chat_sinks = [], [], [], []
        self.clock = [0.0]
        self.http = HTTP(self)
        self.model = CountedModel()
        real_budget = entry.Budget

        def make_budget(seconds):
            value = real_budget(seconds, clock=lambda: self.clock[0])
            self.budgets.append(value)
            return value

        for guard in (
            patch('socket.socket', side_effect=AssertionError('NO_SOCKET_IN_TEST')),
            patch('socket.getaddrinfo', side_effect=AssertionError('NO_DNS_IN_TEST')),
            patch('subprocess.Popen', side_effect=AssertionError('NO_MODEL_PROCESS_IN_TEST')),
            patch('integration.private_state.protect_bytes', side_effect=AssertionError('NO_DPAPI_PROFILE_IN_TEST')),
            patch('integration.private_state.unprotect_bytes', side_effect=AssertionError('NO_PRIVATE_RECOVERY_IN_TEST')),
            patch('os.environ', {'FREYA_API_KEY': 'TEST_ONLY_FAKE_PROVIDER_KEY_1234567890',
                                 'FREYA_API_URL': 'https://synthetic-provider.invalid'}),
            patch.object(entry, 'ROOT', self.root),
            patch.object(entry, 'OUTPUT', self.root / 'artifacts/final-builder'),
            patch.object(chat, 'ROOT', self.root),
            patch.object(entry, 'Budget', side_effect=make_budget),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def client_factory(self, budget, trace):
        client = entry.FreyaClient(budget, trace)
        client.opener = self.http
        self.clients.append(client)
        return client

    def publication_factory(self, directory, **kwargs):
        self.assertTrue(Path(directory).resolve().is_relative_to(self.root.resolve()))
        sink = ContestEvidence(directory, protector=protect, **kwargs)
        self.publish_sinks.append(sink)
        return sink

    def chat_factory(self, directory, **kwargs):
        self.assertTrue(Path(directory).resolve().is_relative_to(self.root.resolve()))
        sink = ChatEvidence(directory, protector=protect, **kwargs)
        self.chat_sinks.append(sink)
        return sink

    def invoke(self, **changed):
        arguments = dict(brief_path=KIT / 'briefs/brief-a.md', tool_api_path=KIT / 'tools/openapi.json',
                         directory=self.output, provider='codex', model_provider=self.model,
                         publish_live=True, client_factory=self.client_factory,
                         evidence_factory=self.publication_factory,
                         chat_fixture=self.fixture_path, chat_fixture_sha256=sha(self.fixture_path.read_bytes()),
                         chat_authorized=True, chat_evidence_factory=self.chat_factory,
                         deadline_seconds=900)
        arguments.update(changed)
        return entry.run(**arguments)

    def assert_no_other_test_or_cleanup(self):
        for row in self.http.requests:
            self.assertNotEqual(row['path'], '/api/v2/chat/end')
            self.assertFalse(row['path'].startswith('/v1/'))
            self.assertNotIn('simulation', row['path'])
            self.assertNotIn('persona', row['path'])

    def test_main_publishes_and_binds_fresh_chat_client_to_same_generation(self):
        trace = self.invoke()
        self.assertEqual(trace['status'], 'CHAT_CONVERSATION_CAPTURED', trace.get('diagnostic'))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.clients), 2)
        self.assertIsNot(self.clients[0], self.clients[1])
        self.assertEqual(len(self.budgets), 1)
        self.assertIs(self.clients[0].budget, self.clients[1].budget)
        self.assertEqual(len(self.chat_sinks), 1)
        sink = self.chat_sinks[0]
        self.assertTrue(sink.sealed)
        seal_raw = (sink.directory / 'FINAL.json').read_bytes()
        seal = json.loads(seal_raw)
        checked = verify_chat_bundle(sink.directory, sha(seal_raw), decoder=recover)
        self.assertTrue(checked['wire_join_verified'])
        self.assertFalse(checked['production_encryption_verified'])
        self.assertEqual(seal['binding']['generation_run_id'], trace['run_id'])
        self.assertEqual(seal['binding']['generation_input_sha256']['brief'], trace['inputs']['brief_sha256'])
        self.assertEqual(seal['binding']['generation_input_sha256']['tool_api'], trace['inputs']['tool_api_sha256'])
        self.assertEqual(seal['binding']['generation_input_sha256']['source_manifest'],
                         sha((self.output / 'runtime-source-manifest.json').read_bytes()))
        self.assertEqual(seal['binding']['artifact_sha256']['provider_export'],
                         sha((self.output / 'provider-export.json').read_bytes()))
        self.assertFalse(trace['live_success_verified'])
        self.assertFalse(trace['final_submission_ready'])
        self.assertEqual(checked['ledger_verdict'], 'NOT_MEASURED')
        self.assertEqual(checked['policy_verdict'], 'NOT_MEASURED')
        self.assertEqual(checked['runtime_model_observation'], 'NOT_MEASURED')
        self.assertEqual(self.http.turns, 2)
        self.assert_no_other_test_or_cleanup()
        RECEIPTS.append({'test': self.id(), 'status': trace['status'],
                         'synthetic_http_requests': len(self.http.requests),
                         'protected_seal_sha256': sha(seal_raw), 'recomputed_result': checked,
                         'generation_source_sha256': seal['binding']['generation_input_sha256'],
                         'runtime_sha256': seal['binding']['runtime_sha256'],
                         'model_calls': self.model.calls, 'budget_instances': len(self.budgets),
                         'new_clients': len(self.clients), 'network_calls': 0,
                         'synthetic_protector_is_encryption': False})

    def test_foreign_call_readback_blocks_without_model_repair_or_new_chat(self):
        self.http.mode = 'foreign_version'
        trace = self.invoke()
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.http.turns, 2)
        self.assertFalse(self.chat_sinks[0].sealed)
        self.assertFalse((self.chat_sinks[0].directory / 'FINAL.json').exists())
        self.assert_no_other_test_or_cleanup()

    def test_unknown_chat_response_is_retained_and_never_repeated(self):
        self.http.mode = 'timeout'
        trace = self.invoke()
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.http.turns, 1)
        sink = self.chat_sinks[0]
        self.assertTrue(sink.failed)
        self.assertIsNotNone(sink.pending)
        self.assertEqual(len(list(sink.directory.glob('*.unknown.json'))), 1)
        self.assertEqual(self.http.requests[-1]['path'], '/api/v2/chat/message')
        self.assert_no_other_test_or_cleanup()

    def test_chat_consumes_original_per_brief_deadline_without_reset(self):
        self.http.mode = 'deadline'
        trace = self.invoke()
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(len(self.budgets), 1)
        self.assertEqual(len(self.clients), 2)
        self.assertIs(self.clients[0].budget, self.clients[1].budget)
        self.assertEqual(self.http.turns, 1)
        self.assertEqual(self.http.requests[-1]['path'], '/api/v2/chat/message')
        self.assertGreater(trace['elapsed_seconds'], trace['deadline_seconds'])
        self.assert_no_other_test_or_cleanup()

    def test_missing_new_chat_authorization_blocks_before_model_or_http(self):
        trace = self.invoke(chat_authorized=False)
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.http.requests, [])

    def test_fixture_hash_mismatch_blocks_before_model_or_http(self):
        trace = self.invoke(chat_fixture_sha256='0' * 64)
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.http.requests, [])

    def test_chat_and_public_practice_cannot_be_combined(self):
        trace = self.invoke(practice_case='PUB-01')
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.http.requests, [])


if __name__ == '__main__':
    unittest.main()
