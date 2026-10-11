"""Real chat runner + FreyaClient + protected joins, fake HTTP, no sockets.

Every identity and fixture here is synthetic. The reversible TEST_ONLY
protector is NOT DPAPI or verified production encryption. No real credentials,
bank, model, account profile, existing run or private customer data is read.
"""
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from orchestration.contest_build import Budget, FreyaClient
from orchestration.contest_chat import ROOT, run_chat
from orchestration.contest_chat_evidence import (
    ChatEvidence, ContestEvidenceError, EVENT_MODEL, RUNTIME_FILES,
    raw, sha, verify_chat_bundle,
)


def identity(number):
    return '00000000-0000-4000-8000-' + f'{number:012d}'


def protect(content):
    return b'TEST_ONLY_NOT_ENCRYPTION:' + base64.b64encode(content)


def recover(content):
    prefix = b'TEST_ONLY_NOT_ENCRYPTION:'
    if not content.startswith(prefix):
        raise ValueError('SYNTHETIC_BYTES_REQUIRED')
    return base64.b64decode(content[len(prefix):], validate=True)


class Response:
    status = 200
    headers = {'Content-Type': 'application/json'}

    def __init__(self, value):
        self.content = raw({'success': True, 'data': {'result': value}})

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, limit):
        return self.content[:limit]


class SyntheticHTTP:
    """Checks pending disk evidence at the actual HTTP dispatch boundary."""
    def __init__(self, test):
        self.test = test
        self.requests, self.transcript = [], []
        self.turn_count = 0
        self.mode = None
        self.agent_end_at = 2
        self.call_ended = True

    def open(self, request, *, timeout):
        test = self.test
        method, path = request.get_method(), urlsplit(request.full_url).path
        test.assertEqual(urlsplit(request.full_url).hostname, 'synthetic-provider.invalid')
        sink = test.client.evidence
        test.assertIsNotNone(sink.pending)
        ticket = sink.pending[0]
        test.assertTrue((sink.directory / (ticket + '.dispatch.json')).is_file())
        sequence = sink.directory / ('chat-sequence-' + str(len(self.requests) + 1) + '.json')
        test.assertTrue(sequence.is_file())
        test.assertEqual(json.loads(sequence.read_bytes())['exchange_id'], ticket)
        body = json.loads(request.data) if request.data is not None else None
        self.requests.append((method, path, body))
        if method == 'GET' and path == '/api/v2/workflow/' + test.publication['workflow_id']:
            graph = copy.deepcopy(test.graph)
            if self.mode == 'wrong_model':
                graph['agent_config']['llmConfig']['model'] = 'synthetic-wrong'
            if self.mode == 'changed_after' and self.turn_count:
                graph['system_prompt'] = 'SYNTHETIC_CHANGED_AFTER_CHAT'
            value = {'workflow': {'id': test.publication['workflow_id'],
                                 'agentId': test.publication['agent_id'], 'workflow': graph}}
        elif method == 'GET' and path == '/api/v2/versions/workflow/' + test.publication['workflow_id']:
            value = {'versions': [{'id': test.publication['version_id'],
                                  'versionNumber': 1, 'isPublished': self.mode != 'unpublished'}]}
        elif method == 'POST' and path == '/api/v2/chat/message':
            self.turn_count += 1
            if self.mode == 'timeout':
                raise TimeoutError('TEST_ONLY_PRIVATE_TIMEOUT_TEXT')
            if self.mode == 'http500':
                error = raw({'success': False, 'data': {'message': 'TEST_ONLY_PRIVATE_HTTP_ERROR'}})
                raise HTTPError(request.full_url, 500, 'synthetic error',
                                {'Content-Type': 'application/json'}, io.BytesIO(error))
            call_id = identity(999) if self.mode == 'foreign_call' and self.turn_count == 2 else identity(6)
            message_id = 'synthetic-reply-1' if self.mode == 'duplicate_reply' else 'synthetic-reply-' + str(self.turn_count)
            self.transcript.extend([{'role': 'user', 'message': body['message'],
                                     'messageId': 'synthetic-user-' + str(self.turn_count)},
                                    {'role': 'assistant', 'message': 'TEST_ONLY_PRIVATE_REPLY',
                                     'messageId': message_id}])
            value = {'callId': call_id, 'messageId': message_id, 'response': 'TEST_ONLY_PRIVATE_REPLY',
                     'warning': 'TEST_ONLY_PRIVATE_WARNING',
                     'endedByAgent': self.turn_count == self.agent_end_at}
        elif method == 'GET' and path == '/api/v2/call/' + identity(6):
            value = {'call': {'id': identity(6), 'workspaceId': test.workspace,
                              'assistantWorkflowId': test.publication['workflow_id'],
                              'entityVersionId': test.publication['version_id'],
                              'entityVersionNumber': 2 if self.mode == 'wrong_call_version' else 1,
                              'startedAt': '2026-10-11T00:00:00Z',
                              'endedAt': '2026-10-11T00:01:00Z' if self.call_ended else None,
                              'endedReason': 'SYNTHETIC_PRIVATE_REASON',
                              'messages': copy.deepcopy(self.transcript)}}
            if self.mode == 'missing_reply':
                value['call']['messages'] = [row for row in self.transcript if row['role'] == 'user']
        else:
            raise AssertionError('NO_OTHER_HTTP_ROUTE_AUTHORIZED')
        return Response(value)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-chat-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output, self.private = self.base / 'public', self.base / 'protected'
        self.workspace = identity(1)
        self.graph = {'initial_node': 'start', 'nodes': {
            'start': {'name': 'start', 'node_type': 'conversation'}},
            'agent_config': {'llmConfig': copy.deepcopy(EVENT_MODEL)}}
        self.publication = {'workflow_id': identity(2), 'agent_id': identity(3),
                            'version_id': identity(4), 'version_number': 1}
        self.fixture = {'customerVariables': {'customer_id': 'TEST_ONLY_PRIVATE_CUSTOMER',
                                             'first_name': 'SYNTHETIC_PERSON'},
                        'messages': ['TEST_ONLY_PRIVATE_USER_ONE', 'TEST_ONLY_PRIVATE_USER_TWO']}
        self.files = {}
        values = {'brief': b'SYNTHETIC BRIEF; not a policy fixture.\n',
                  'tool_api': raw({'openapi': '3.0.0', 'paths': {}}),
                  'source_manifest': raw({name: sha((ROOT / name).read_bytes()) for name in RUNTIME_FILES}),
                  'provider_export': raw({'workflow_id': identity(2), 'agent_id': identity(3),
                                          'workflow': self.graph}),
                  'fixture': raw(self.fixture)}
        for name, content in values.items():
            self.files[name] = self.base / (name + '.source')
            self.files[name].write_bytes(content)
        self.publication['export_sha256'] = sha(values['provider_export'])
        self.fixture_sha = sha(values['fixture'])
        self.generation_pins = {name: sha(values[name]) for name in ('brief', 'tool_api', 'source_manifest')}
        self.budget = Budget(90)
        with patch.dict('os.environ', {'FREYA_API_KEY': 'TEST_ONLY_FAKE_KEY_NEVER_SENT',
                                     'FREYA_API_URL': 'https://synthetic-provider.invalid'}):
            self.client = FreyaClient(self.budget, [])
        self.http = SyntheticHTTP(self)
        self.client.opener = self.http
        self.sinks = []

    def factory(self, directory, **kwargs):
        sink = ChatEvidence(directory, protector=protect, **kwargs)
        self.sinks.append(sink)
        return sink

    def run_capture(self, **changed):
        arguments = dict(client=self.client, publication=self.publication, workspace_id=self.workspace,
                         brief_path=self.files['brief'], tool_api_path=self.files['tool_api'],
                         source_manifest_path=self.files['source_manifest'],
                         provider_export_path=self.files['provider_export'], fixture_path=self.files['fixture'],
                         official_fixture_sha256=self.fixture_sha, directory=self.output,
                         generation_run_id=identity(10), generation_input_sha256=self.generation_pins,
                         private_directory=self.private, budget=self.budget, live_authorized=True,
                         evidence_factory=self.factory)
        arguments.update(changed)
        with patch('socket.socket', side_effect=AssertionError('NO_NETWORK_IN_SYNTHETIC_TEST')):
            return run_chat(**arguments)

    def assert_no_forced_end_or_bank(self):
        self.assertTrue(all(path != '/api/v2/chat/end' and not path.startswith('/v1/')
                            and 'simulation' not in path and 'persona' not in path
                            for _, path, _ in self.http.requests))

    def test_capture_recomputes_exact_wire_join_and_public_privacy(self):
        result = self.run_capture()
        self.assertEqual(result['status'], 'CHAT_CONVERSATION_CAPTURED')
        self.assertEqual(result['closing_observation'], 'AGENT_END_CONFIRMED')
        self.assertEqual(len(self.http.requests), 7)
        checked = verify_chat_bundle(self.private, result['protected_evidence_sha256'], decoder=recover)
        self.assertTrue(checked['wire_join_verified'])
        self.assertFalse(checked['production_encryption_verified'])
        self.assertEqual(checked['protection'], 'injected_protector_not_verified')
        for key in ('ledger_verdict', 'policy_verdict', 'runtime_model_observation'):
            self.assertEqual(checked[key], 'NOT_MEASURED')
        self.assertFalse(checked['live_success_verified'])
        self.assertFalse(checked['final_submission_ready'])
        content = (self.output / 'FINAL.json').read_bytes()
        for secret in ('TEST_ONLY_PRIVATE', 'TEST_ONLY_FAKE_KEY', 'SYNTHETIC_PERSON', 'synthetic-reply-'):
            self.assertNotIn(secret.encode(), content)
        posts = [body for method, _, body in self.http.requests if method == 'POST']
        self.assertEqual(posts[0]['customerVariables'], self.fixture['customerVariables'])
        self.assertEqual(posts[0]['versionId'], self.publication['version_id'])
        self.assertEqual(posts[1], {'callId': identity(6), 'message': self.fixture['messages'][1]})
        self.assert_no_forced_end_or_bank()

    def test_no_authorization_dispatches_nothing(self):
        with self.assertRaisesRegex(ContestEvidenceError, 'NEW_HUMAN_AUTHORIZATION'):
            self.run_capture(live_authorized=False)
        self.assertEqual(self.http.requests, [])
        self.assertFalse(self.output.exists())

    def test_fixture_hash_mismatch_dispatches_nothing(self):
        result = self.run_capture(official_fixture_sha256='0' * 64)
        self.assertEqual(result['diagnostic'], 'CHAT_OFFICIAL_FIXTURE_HASH_MISMATCH')
        self.assertEqual(self.http.requests, [])

    def test_missing_customer_fixture_never_invents_one(self):
        self.files['fixture'].write_bytes(raw({'customerVariables': {}, 'messages': ['Hello']}))
        result = self.run_capture(official_fixture_sha256=sha(self.files['fixture'].read_bytes()))
        self.assertEqual(result['diagnostic'], 'CHAT_OFFICIAL_CUSTOMER_VARIABLES_REQUIRED')
        self.assertEqual(self.http.requests, [])

    def test_changed_generation_source_manifest_dispatches_nothing(self):
        self.files['source_manifest'].write_bytes(raw({}))
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_GENERATION_INPUT_HASH_MISMATCH')
        self.assertEqual(self.http.requests, [])

    def test_protector_preflight_failure_prevents_http(self):
        def broken_factory(directory, **kwargs):
            return ChatEvidence(directory, protector=lambda content: content, **kwargs)
        result = self.run_capture(evidence_factory=broken_factory)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.http.requests, [])

    def test_model_gate_prevents_first_chat_mutation(self):
        self.graph['agent_config']['llmConfig']['model'] = 'synthetic-wrong'
        self.files['provider_export'].write_bytes(raw({'workflow_id': identity(2), 'agent_id': identity(3),
                                                      'workflow': self.graph}))
        self.publication['export_sha256'] = sha(self.files['provider_export'].read_bytes())
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_EVENT_MODEL_CONFIG_REQUIRED')
        self.assertEqual([method for method, _, _ in self.http.requests], ['GET', 'GET'])

    def test_unpublished_version_prevents_first_chat_mutation(self):
        self.http.mode = 'unpublished'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_SELECTED_PUBLISHED_VERSION_REQUIRED')
        self.assertEqual([method for method, _, _ in self.http.requests], ['GET', 'GET'])

    def test_unknown_turn_keeps_pending_and_never_retries_or_cleans_up(self):
        self.http.mode = 'timeout'
        result = self.run_capture()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertTrue(result['pending_exchange_retained'])
        self.assertEqual(len(self.http.requests), 3)
        self.assertEqual(self.http.turn_count, 1)
        self.assertFalse((self.private / 'FINAL.json').exists())
        self.assertEqual(len(list(self.private.glob('*.unknown.json'))), 1)
        with self.assertRaises(ContestEvidenceError):
            self.client.api('POST', '/api/v2/chat/message', {'message': 'never send'})
        self.assertEqual(len(self.http.requests), 3)
        self.assert_no_forced_end_or_bank()

    def test_http500_retains_full_error_without_retry_or_public_text(self):
        self.http.mode = 'http500'
        result = self.run_capture()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['pending_exchange_retained'])
        self.assertEqual(self.http.turn_count, 1)
        self.assertEqual(len(self.http.requests), 3)
        ticket = self.sinks[0].sequence[-1]
        self.assertEqual(self.sinks[0].cache[ticket][1]['http_status'], 500)
        self.assertIn(b'TEST_ONLY_PRIVATE_HTTP_ERROR', self.sinks[0].cache[ticket][1]['body'])
        self.assertNotIn(b'TEST_ONLY_PRIVATE', (self.output / 'FINAL.json').read_bytes())
        self.assert_no_forced_end_or_bank()

    def test_foreign_call_stops_before_any_more_request(self):
        self.http.mode = 'foreign_call'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_RESPONSE_FOREIGN_CALL')
        self.assertEqual(len(self.http.requests), 4)
        self.assertFalse((self.private / 'FINAL.json').exists())

    def test_reused_message_id_stops_before_any_more_request(self):
        self.http.mode = 'duplicate_reply'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_RESPONSE_MESSAGE_INVALID_OR_REUSED')
        self.assertEqual(len(self.http.requests), 4)

    def test_call_version_mismatch_cannot_be_sealed(self):
        self.http.mode = 'wrong_call_version'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_CALL_VERSION_OR_SCOPE_MISMATCH')
        self.assertFalse((self.private / 'FINAL.json').exists())

    def test_transcript_missing_reply_ids_cannot_be_sealed(self):
        self.http.mode = 'missing_reply'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_CALL_MESSAGE_IDS_MISSING_OR_DUPLICATE')
        self.assertFalse((self.private / 'FINAL.json').exists())

    def test_changed_export_readback_cannot_be_sealed(self):
        self.http.mode = 'changed_after'
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_EXPORT_READBACK_MISMATCH')
        self.assertFalse((self.private / 'FINAL.json').exists())

    def test_agent_end_stops_unused_script_and_open_call_is_not_forced_closed(self):
        self.http.agent_end_at = 1
        self.http.call_ended = False
        result = self.run_capture()
        self.assertEqual(result['status'], 'CHAT_CONVERSATION_CAPTURED')
        self.assertEqual(self.http.turn_count, 1)
        self.assertEqual(result['closing_observation'], 'AGENT_END_CLAIM_UNCONFIRMED')
        self.assertFalse(result['lifecycle_ended'])
        self.assertFalse(result['controller_end_dispatched'])
        self.assert_no_forced_end_or_bank()

    def test_capture_reports_open_conversation_without_agent_flag(self):
        self.http.agent_end_at = 100
        self.http.call_ended = False
        result = self.run_capture()
        self.assertEqual(result['status'], 'CHAT_CONVERSATION_CAPTURED')
        self.assertEqual(result['closing_observation'], 'CALL_REMAINS_OPEN')
        self.assert_no_forced_end_or_bank()

    def test_ciphertext_tamper_is_detected_by_offline_recomputation(self):
        result = self.run_capture()
        path = next((self.private / 'wire').glob('*.dpapi'))
        path.write_bytes(path.read_bytes() + b'TAMPER')
        with self.assertRaisesRegex(ContestEvidenceError, 'MANIFEST_CONTENT_MISMATCH'):
            verify_chat_bundle(self.private, result['protected_evidence_sha256'], decoder=recover)

    def test_separate_profile_cannot_dispatch_chat_end_or_bank_session(self):
        sink = ChatEvidence(self.private, protector=protect)
        for path in ('/api/v2/chat/end', '/v1/practice/sessions', '/api/v2/simulations'):
            with self.assertRaisesRegex(ContestEvidenceError, 'OUTSIDE_CAPTURE_SCOPE'):
                sink.begin_exchange(method='POST', path=path, body=b'{}')
        self.assertIsNone(sink.pending)

    def test_fresh_budget_cannot_reset_per_brief_deadline(self):
        with self.assertRaisesRegex(ContestEvidenceError, 'SAME_PER_BRIEF_BUDGET'):
            self.run_capture(budget=Budget(90))
        self.assertEqual(self.http.requests, [])

    def test_branch_or_model_override_field_rejected_before_dispatch(self):
        sink = ChatEvidence(self.private, protector=protect)
        for field, value in (('fromMessageId', 'synthetic-anchor'),
                             ('assistantLlmOverride', EVENT_MODEL)):
            with self.assertRaisesRegex(ContestEvidenceError, 'BRANCH_OVERRIDE_OR_OTHER_FIELD'):
                sink.begin_exchange(method='POST', path='/api/v2/chat/message',
                                    body=raw({'callId': identity(6), 'message': 'synthetic', field: value}))
        self.assertIsNone(sink.pending)

    def test_sequence_write_failure_retains_pending_but_prevents_http(self):
        from orchestration import contest_chat_evidence as evidence
        original = evidence.write_new

        def fail_sequence(path, value):
            if Path(path).name.startswith('chat-sequence-'):
                raise OSError('SYNTHETIC_STORAGE_FAILURE')
            return original(path, value)

        with patch.object(evidence, 'write_new', side_effect=fail_sequence):
            result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_SEQUENCE_PERSISTENCE_FAILED_NO_DISPATCH')
        self.assertTrue(result['pending_exchange_retained'])
        self.assertEqual(self.http.requests, [])

    def test_fresh_directory_cannot_be_used_to_overwrite_previous_capture(self):
        self.run_capture()
        fresh_client = copy.copy(self.client)
        fresh_client.trace, fresh_client.evidence = [], None
        before = len(self.http.requests)
        with self.assertRaisesRegex(ContestEvidenceError, 'FRESH_OUTPUT_REQUIRED'):
            self.run_capture(client=fresh_client)
        self.assertEqual(len(self.http.requests), before)

    def test_wrong_generation_brief_cannot_be_relabelled_as_export_source(self):
        self.files['brief'].write_bytes(b'SYNTHETIC_DIFFERENT_SOURCE')
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_GENERATION_INPUT_HASH_MISMATCH')
        self.assertEqual(self.http.requests, [])

    def test_generation_snapshot_and_chat_runtime_are_kept_separate(self):
        self.files['source_manifest'].write_bytes(raw({'orchestration/contest_build.py': '0' * 64}))
        self.generation_pins['source_manifest'] = sha(self.files['source_manifest'].read_bytes())
        result = self.run_capture()
        self.assertEqual(result['status'], 'CHAT_CONVERSATION_CAPTURED')
        seal = json.loads((self.private / 'FINAL.json').read_bytes())
        self.assertEqual(seal['binding']['generation_input_sha256'], self.generation_pins)
        self.assertEqual(seal['binding']['generation_run_id'], identity(10))
        self.assertNotEqual(seal['binding']['artifact_sha256']['source_manifest'],
                            seal['binding']['artifact_sha256']['chat_source_manifest'])
        self.assertNotEqual(seal['binding']['runtime_sha256']['orchestration/contest_build.py'], '0' * 64)
        verify_chat_bundle(self.private, result['protected_evidence_sha256'], decoder=recover)

    def test_expired_shared_budget_dispatches_nothing(self):
        clock = [0]
        self.budget = Budget(5, clock=lambda: clock[0])
        self.client.budget = self.budget
        clock[0] = 6
        result = self.run_capture()
        self.assertEqual(result['diagnostic'], 'CHAT_PER_BRIEF_DEADLINE_EXCEEDED')
        self.assertEqual(self.http.requests, [])
        self.assertEqual(result['shared_budget_elapsed_seconds'], 6)


if __name__ == '__main__':
    unittest.main()
