"""Fresh synthetic wire fixtures only: no provider, credentials or old suites."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integration.contest_evidence import (ARTIFACT_KEYS, ContestEvidence,
    ContestEvidenceError, MODEL_KEYS, verify_bundle)


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def protect(value):
    # Explicitly synthetic and reversible. This is NOT production encryption.
    return b'SYNTHETIC_PROTECTED:' + base64.b64encode(value)


def recover(value):
    if not value.startswith(b'SYNTHETIC_PROTECTED:'):
        raise ValueError()
    return base64.b64decode(value[len(b'SYNTHETIC_PROTECTED:'):], validate=True)


def identity(n):
    return '00000000-0000-4000-8000-' + f'{n:012d}'


SECRET = 'synthetic-session-secret-23456789'


class ContestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'evidence'
        self.socket_guard = patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.binding = {key: identity(n) for n, key in enumerate(
            ('run_id', 'workspace_id', 'workflow_id', 'agent_id', 'version_id',
             'simulation_id', 'persona_id', 'iteration_id', 'call_id'), 1)}
        b = self.binding
        self.variables = {'customer_id': 'SYNTHETIC_CUSTOMER', 'session_token': SECRET}
        self.models = {key: {'provider': 'experimental-5' if pos < 3 else 'google',
                            'model': 'experimental-5' if pos < 3 else 'gemini-3-flash-preview'}
                       for pos, key in enumerate(MODEL_KEYS)}
        self.graph = {'nodes': {'hello': {'node_type': 'conversation'}}, 'initial_node': 'hello'}
        self.artifacts = {'brief': b'SYNTHETIC brief', 'tool_api': b'{"paths":{}}',
            'provider_export': raw({'workflow_id': b['workflow_id'], 'agent_id': b['agent_id'],
                                    'workflow': self.graph}),
            'source_manifest': raw({'synthetic-source.py': '1' * 64})}
        b.update(version_number=1, case_id='PUB-01', models=self.models,
                 customer_sha256=sha(self.variables['customer_id'].encode()),
                 export_sha256=sha(self.artifacts['provider_export']),
                 brief_sha256=sha(self.artifacts['brief']), tool_api_sha256=sha(self.artifacts['tool_api']),
                 source_manifest_sha256=sha(self.artifacts['source_manifest']))
        call = {'id': b['call_id'], 'workspaceId': b['workspace_id'],
                'assistantWorkflowId': b['workflow_id'], 'simulationId': b['simulation_id'],
                'entityVersionId': b['version_id'], 'entityVersionNumber': 1,
                'startedAt': '2026-10-11T00:00:00Z', 'endedAt': '2026-10-11T00:01:00Z',
                'messages': [{'role': 'user', 'message': 'SYNTHETIC_PRIVATE_TRANSCRIPT'}]}
        simulation = {'agentId': b['agent_id'], 'agentVersionId': b['version_id'],
            'selectedPersonaIds': [b['persona_id']], 'customerVariablesMap': {b['persona_id']: self.variables},
            'runsPerPersona': 1, 'maxConcurrentCalls': 1, 'maxRetries': 0, **self.models}
        self.fixtures = {
            'session': ['POST', '/v1/practice/sessions', {'case': 'PUB-01'}, {},
                        {'variables': self.variables}],
            'simulation': ['POST', '/api/v2/simulations', simulation, {},
                           {'simulation': {'id': b['simulation_id'], 'settings': {'maxRetries': 0}}}],
            'iteration': ['GET', f"/api/v2/simulations/{b['simulation_id']}/iterations/{b['iteration_id']}",
                None, {'limit': 100, 'offset': 0}, {'iteration': {'id': b['iteration_id'],
                    'agentVersionId': b['version_id'], 'agentVersionNumber': 1, 'params': self.models},
                    'runs': [{'id': 'synthetic-run', 'callId': b['call_id'],
                              'personaId': b['persona_id'], 'outcome': 'pass'}]}],
            'call': ['GET', '/api/v2/call/' + b['call_id'], None, {}, {'call': call}],
            'ledger': ['GET', '/v1/practice/ledger/' + self.variables['customer_id'], None, {},
                       {'ok': True, 'calls': [{'tool': 'close_call', 'args': {'session_token': SECRET}}]}],
            'readback': ['GET', '/api/v2/workflow/' + b['workflow_id'], None,
                         {'workspaceId': b['workspace_id']}, {'workflow': {'id': b['workflow_id'],
                            'agentId': b['agent_id'], 'workflow': self.graph}}],
            'versions': ['GET', '/api/v2/versions/workflow/' + b['workflow_id'], None,
                         {'workspaceId': b['workspace_id']}, {'versions': [{'id': b['version_id'],
                            'versionNumber': 1, 'isPublished': True}]}]}

    def record(self, *, skip_artifact=None, override_status=None, response_override=None):
        sink = ContestEvidence(self.root, secrets=(SECRET,), protector=protect)
        roles = {}
        for role, (method, path, body, params, value) in self.fixtures.items():
            ticket = sink.begin_exchange(method=method, path=path,
                                        body=raw(body) if body is not None else None, params=params)
            encoded = raw(value if role == 'session' else {'success': True, 'data': {'result': value}})
            if role == 'ledger':
                encoded = raw(value)
            if response_override and role in response_override:
                encoded = response_override[role]
            sink.complete_exchange(ticket, http_status=(override_status or {}).get(role, 200),
                headers={'Content-Type': 'application/json', 'x-request-id': SECRET}, body=encoded)
            roles[role] = ticket
        for role in ARTIFACT_KEYS:
            if role != skip_artifact:
                sink.pin_artifact(role, self.artifacts[role])
        return sink, roles

    def seal(self, **kwargs):
        sink, roles = self.record(**kwargs)
        return sink, sink.seal(binding=self.binding, roles=roles)

    def mutate_seal(self, change):
        _, receipt = self.seal()
        path = self.root / 'FINAL.json'
        value = json.loads(path.read_bytes())
        change(value)
        path.write_bytes(raw(value))
        return sha(path.read_bytes())

    def test_full_join_recomputed_and_private_bytes_not_public(self):
        _, receipt = self.seal()
        result = verify_bundle(self.root, receipt['sha256'], decoder=recover)
        self.assertTrue(result['wire_join_verified'])
        self.assertFalse(result['official_policy_pass_verified'])
        self.assertFalse(result['may_write_bank'])
        self.assertFalse(result['may_retry'])
        for path in self.root.rglob('*.json'):
            self.assertNotIn(SECRET.encode(), path.read_bytes())
            self.assertNotIn(b'SYNTHETIC_PRIVATE_TRANSCRIPT', path.read_bytes())
        self.assertEqual(result['iteration_outcome'], 'pass')

    def test_more_than_prefix_limit_preserves_exact_full_response(self):
        self.fixtures['call'][4]['call']['messages'][0]['message'] = 'SYNTHETIC-' * 9000
        _, receipt = self.seal()
        self.assertTrue(verify_bundle(self.root, receipt['sha256'], decoder=recover)['wire_join_verified'])
        value = json.loads((self.root / 'FINAL.json').read_bytes())
        response = value['roles']['call']['response']
        self.assertGreater(response['plaintext_bytes'], 65536)
        stored = recover((self.root / 'wire' / response['file']).read_bytes())
        self.assertEqual(sha(stored), response['plaintext_sha256'])

    def test_unresolved_dispatch_cannot_be_retried_or_sealed(self):
        sink = ContestEvidence(self.root, protector=protect)
        ticket = sink.begin_exchange(method='POST', path='/api/v2/simulations', body=b'{}')
        self.assertTrue((self.root / (ticket + '.dispatch.json')).is_file())
        with self.assertRaisesRegex(ContestEvidenceError, 'UNRESOLVED_EXCHANGE_NO_RETRY'):
            sink.begin_exchange(method='POST', path='/api/v2/simulations', body=b'{}')
        with self.assertRaisesRegex(ContestEvidenceError, 'UNRESOLVED_EXCHANGE_NO_SEAL'):
            sink.seal(binding=self.binding, roles={})

    def test_other_ticket_cannot_supply_a_response(self):
        sink = ContestEvidence(self.root, protector=protect)
        sink.begin_exchange(method='GET', path='/api/v2/workspaces')
        with self.assertRaisesRegex(ContestEvidenceError, 'RESPONSE_WITHOUT_OWN_REQUEST'):
            sink.complete_exchange(identity(20), http_status=200, headers={}, body=b'{}')

    def test_capture_failure_latches_and_forbids_next_request(self):
        sink = ContestEvidence(self.root, protector=protect)
        sink.recorder.protector = lambda value: value
        with self.assertRaisesRegex(ContestEvidenceError, 'REQUEST_CAPTURE_FAILED'):
            sink.begin_exchange(method='GET', path='/api/v2/workspaces')
        with self.assertRaisesRegex(ContestEvidenceError, 'EVIDENCE_LATCHED'):
            sink.begin_exchange(method='GET', path='/api/v2/workspaces')

    def test_truncation_latches_without_further_request(self):
        sink = ContestEvidence(self.root, protector=protect)
        ticket = sink.begin_exchange(method='GET', path='/api/v2/workspaces')
        with self.assertRaisesRegex(ContestEvidenceError, 'RESPONSE_CAPTURE_FAILED'):
            sink.complete_exchange(ticket, http_status=200, headers={}, body=b'{}', complete=False)
        self.assertTrue(sink.failed)

    def test_tampered_ciphertext_fails_before_recovery(self):
        _, receipt = self.seal()
        path = next((self.root / 'wire').glob('*.dpapi'))
        path.write_bytes(path.read_bytes() + b'TAMPER')
        with self.assertRaisesRegex(ContestEvidenceError, 'MANIFEST_CONTENT_MISMATCH'):
            verify_bundle(self.root, receipt['sha256'], decoder=lambda _: self.fail('decoded tampered file'))

    def test_model_override_mismatch_cannot_seal(self):
        value = copy.deepcopy(self.fixtures['iteration'][4])
        value['iteration']['params']['assistantLlmOverride']['model'] = 'wrong-model'
        sink, roles = self.record(response_override={'iteration': raw({'success': True, 'data': {'result': value}})})
        with self.assertRaisesRegex(ContestEvidenceError, 'ITERATION_MODEL_VERSION_MISMATCH'):
            sink.seal(binding=self.binding, roles=roles)

    def test_call_from_other_simulation_cannot_seal(self):
        self.fixtures['call'][4]['call']['simulationId'] = identity(20)
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'CALL_SCOPE_MISMATCH'):
            sink.seal(binding=self.binding, roles=roles)

    def test_customer_variables_must_equal_fresh_session(self):
        request = copy.deepcopy(self.fixtures['simulation'][2])
        request['customerVariablesMap'][self.binding['persona_id']]['customer_id'] = 'FOREIGN'
        self.fixtures['simulation'][2] = request
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'SIMULATION_REQUEST_SCOPE_MISMATCH'):
            sink.seal(binding=self.binding, roles=roles)

    def test_explicit_integer_zero_retries_required_in_request(self):
        for value in (None, 1, 3, False, True, '0'):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as temporary:
                    self.root = Path(temporary) / 'evidence'
                    request = copy.deepcopy(self.fixtures['simulation'][2])
                    if value is None:
                        request.pop('maxRetries', None)
                    else:
                        request['maxRetries'] = value
                    self.fixtures['simulation'][2] = request
                    sink, roles = self.record()
                    with self.assertRaisesRegex(ContestEvidenceError, 'SIMULATION_REQUEST_SCOPE_MISMATCH'):
                        sink.seal(binding=self.binding, roles=roles)

    def test_actual_retry_setting_required_in_response(self):
        for value in (None, 1, 3, False, True, '0'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / 'evidence'
                response = copy.deepcopy(self.fixtures['simulation'][4])
                response['simulation']['settings'] = {} if value is None else {'maxRetries': value}
                sink, roles = self.record(response_override={'simulation': raw({'success': True, 'data': {'result': response}})})
                with self.assertRaisesRegex(ContestEvidenceError, 'SIMULATION_RETRY_SETTING_MISMATCH'):
                    sink.seal(binding=self.binding, roles=roles)

    def test_ledger_for_foreign_customer_cannot_seal(self):
        self.fixtures['ledger'][1] = '/v1/practice/ledger/FOREIGN'
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'RESPONSE_REQUEST_JOIN_MISMATCH'):
            sink.seal(binding=self.binding, roles=roles)

    def test_missing_raw_ledger_cannot_be_replaced_by_hash_summary(self):
        self.fixtures['ledger'][4] = {'ledger_sha256': '1' * 64, 'passed': True}
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'LEDGER_MISSING'):
            sink.seal(binding=self.binding, roles=roles)

    def test_missing_input_artifact_cannot_seal(self):
        sink, roles = self.record(skip_artifact='brief')
        with self.assertRaisesRegex(ContestEvidenceError, 'REQUIRED_EVIDENCE_MISSING'):
            sink.seal(binding=self.binding, roles=roles)

    def test_export_graph_readback_mismatch_cannot_seal(self):
        self.artifacts['provider_export'] = raw({'workflow_id': self.binding['workflow_id'],
            'agent_id': self.binding['agent_id'], 'workflow': {'nodes': {'different': {}}}})
        self.binding['export_sha256'] = sha(self.artifacts['provider_export'])
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'EXPORT_READBACK_MISMATCH'):
            sink.seal(binding=self.binding, roles=roles)

    def test_http_failure_cannot_seal_as_success(self):
        sink, roles = self.record(override_status={'call': 503})
        with self.assertRaisesRegex(ContestEvidenceError, 'HTTP_RESULT_NOT_SUCCESS'):
            sink.seal(binding=self.binding, roles=roles)

    def test_external_seal_hash_catches_rewritten_summary(self):
        _, receipt = self.seal()
        path = self.root / 'FINAL.json'
        path.write_bytes(path.read_bytes().replace(b'"wire_join_verified":true', b'"wire_join_verified":false'))
        with self.assertRaisesRegex(ContestEvidenceError, 'SEAL_HASH_MISMATCH'):
            verify_bundle(self.root, receipt['sha256'], decoder=recover)

    def test_manifest_escape_is_rejected(self):
        pin = self.mutate_seal(lambda value: value['manifest'][0].update(path='../outside'))
        with self.assertRaisesRegex(ContestEvidenceError, 'MANIFEST_PATH_INVALID'):
            verify_bundle(self.root, pin, decoder=recover)

    def test_manifest_duplicate_is_rejected(self):
        pin = self.mutate_seal(lambda value: value['manifest'].append(value['manifest'][0]))
        with self.assertRaisesRegex(ContestEvidenceError, 'MANIFEST_ROW_INVALID'):
            verify_bundle(self.root, pin, decoder=recover)

    def test_decoded_plaintext_hash_mismatch_is_rejected(self):
        _, receipt = self.seal()
        with self.assertRaisesRegex(ContestEvidenceError, 'PROTECTED_PLAINTEXT_MISMATCH'):
            verify_bundle(self.root, receipt['sha256'], decoder=lambda value: recover(value) + b'TAMPER')

    def test_non_json_call_cannot_be_claimed_success(self):
        sink, roles = self.record(response_override={'call': b'NOT JSON'})
        with self.assertRaisesRegex(ContestEvidenceError, 'EVIDENCE_JSON_INVALID'):
            sink.seal(binding=self.binding, roles=roles)

    def test_directory_reopen_cannot_authorize_resume(self):
        ContestEvidence(self.root, protector=protect)
        with self.assertRaisesRegex(ContestEvidenceError, 'EVIDENCE_DIRECTORY_ALREADY_EXISTS'):
            ContestEvidence(self.root, protector=protect)

    def test_unknown_response_latches_and_preserves_dispatch(self):
        sink = ContestEvidence(self.root, protector=protect)
        ticket = sink.begin_exchange(method='POST', path='/api/v2/simulations', body=b'{}')
        sink.mark_unknown(ticket)
        self.assertTrue((self.root / (ticket + '.dispatch.json')).is_file())
        self.assertTrue((self.root / (ticket + '.unknown.json')).is_file())
        with self.assertRaisesRegex(ContestEvidenceError, 'EVIDENCE_LATCHED'):
            sink.begin_exchange(method='GET', path='/api/v2/workspaces')

    def test_other_ticket_cannot_mark_original_unknown(self):
        sink = ContestEvidence(self.root, protector=protect)
        sink.begin_exchange(method='GET', path='/api/v2/workspaces')
        with self.assertRaisesRegex(ContestEvidenceError, 'UNKNOWN_WITHOUT_OWN_REQUEST'):
            sink.mark_unknown(identity(20))

    def test_foreign_customer_in_ledger_body_cannot_seal(self):
        self.fixtures['ledger'][4]['calls'][0]['args']['customer_id'] = 'FOREIGN'
        sink, roles = self.record()
        with self.assertRaisesRegex(ContestEvidenceError, 'LEDGER_FOREIGN_CUSTOMER'):
            sink.seal(binding=self.binding, roles=roles)

    def test_caller_params_mutation_does_not_change_captured_request(self):
        sink = ContestEvidence(self.root, protector=protect)
        params = {'workspaceId': self.binding['workspace_id'], 'limit': 100}
        ticket = sink.begin_exchange(method='GET', path='/api/v2/workspaces', params=params)
        params['limit'] = 99
        params['workspaceId'] = identity(20)
        sink.complete_exchange(ticket, http_status=200, headers={}, body=b'{}')
        captured = sink.cache[ticket][0]['params']
        self.assertEqual(captured, {'workspaceId': self.binding['workspace_id'], 'limit': 100})
        request = sink.exchanges[ticket]['request']
        stored = json.loads(recover((self.root / 'wire' / request['file']).read_bytes()))
        self.assertEqual(captured, stored['params'])

    def test_returned_receipt_mutation_cannot_change_internal_binding(self):
        sink = ContestEvidence(self.root, protector=protect)
        ticket = sink.begin_exchange(method='GET', path='/api/v2/workspaces')
        pair = sink.complete_exchange(ticket, http_status=200, headers={}, body=b'{}')
        original = copy.deepcopy(sink.exchanges[ticket])
        pair['request']['plaintext_sha256'] = '0' * 64
        pair['response']['file'] = 'FOREIGN.dpapi'
        pair['status'] = 'BOGUS'
        self.assertEqual(sink.exchanges[ticket], original)


if __name__ == '__main__':
    unittest.main()
