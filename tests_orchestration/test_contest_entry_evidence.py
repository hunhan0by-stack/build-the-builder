"""Two entry-path checks with real capture/join code and synthetic wire only.

No transport, model, credential, profile, DPAPI or existing run data is used.
The reversible TEST_ONLY protector below is not production encryption.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import orchestration.contest_build as entry
from orchestration.contest_preflight import EVIDENCE_FILES
from integration.contest_evidence import ContestEvidence, ROLE_KEYS, verify_bundle
from orchestration.workspace_lock import SimulationReservation
from builder.models import FoundationError


SOURCE_ROOT = Path(__file__).resolve().parents[1]
KIT = SOURCE_ROOT / 'data/freya-hackathon/kit-v3/build-the-builder'
RECEIPTS = []
ARTIFACT_DIRECTORY = None  # Explicit test runner may retain fresh synthetic copies.


def sha(content):
    return hashlib.sha256(content).hexdigest()


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def identity(number):
    return '00000000-0000-4000-8000-' + f'{number:012d}'


def protect(content):
    return b'TEST_ONLY_REVERSIBLE:' + base64.b64encode(content)


def recover(content):
    prefix = b'TEST_ONLY_REVERSIBLE:'
    if not content.startswith(prefix):
        raise ValueError('NOT_SYNTHETIC_EVIDENCE')
    return base64.b64decode(content[len(prefix):], validate=True)


class FixtureModel:
    """Deterministic fixture generation: no model engine or process."""
    def generate(self, *, tool_templates, **unused):
        graph = {'initial_node': 'collect', 'nodes': {
            'collect': {'name': 'collect', 'node_type': 'conversation',
                'tools': list(tool_templates['tools'].values()),
                'transitions': [{'target_node': 'end', 'condition': {'operator': 'always'}}]},
            'end': {'name': 'end', 'node_type': 'end_call'}}}
        return graph, {'synthetic_fixture': True}


class WireResponse:
    status = 200
    headers = {'Content-Type': 'application/json', 'x-request-id': identity(50)}

    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, limit):
        return self.content[:limit]


class SyntheticHTTP:
    """Fake HTTP boundary; all callers/SDK/capture hooks above it are real."""
    def __init__(self):
        self.calls = []
        self.case = next(row for row in json.loads((KIT / 'public-test-set.json').read_bytes())
                         if row['id'] == 'PUB-01')
        self.ids = {key: identity(number) for number, key in enumerate(
            ('workspace', 'workflow', 'agent', 'version', 'simulation', 'persona', 'iteration', 'call'), 1)}
        self.graph = None
        self.simulation_body = None
        self.credit_observations = 0
        self.variables = {**self.case['variables'], 'customer_id': 'TEST_ONLY_CUSTOMER',
                          'session_token': 'TEST_ONLY_SESSION_1234567890123456'}

    def open(self, request, *, timeout):
        parsed = urlsplit(request.full_url)
        method, path = request.get_method(), parsed.path
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = json.loads(request.data) if request.data is not None else None
        ids = self.ids
        self.calls.append({'method': method, 'path': path, 'params': params,
                           'request_bytes': request.data})
        if parsed.hostname == 'freya-bank-mock.freyavoice.workers.dev':
            if method == 'POST' and path == '/v1/practice/sessions':
                assert body == {'case': 'PUB-01'}
                value = {'variables': self.variables}
            elif method == 'GET' and path == '/v1/practice/ledger/' + self.variables['customer_id']:
                value = {**copy.deepcopy(self.case['expected']), 'ok': True,
                    'customer_id': self.variables['customer_id'], 'calls': [
                    {'tool': 'verify_identity', 'result': {'ok': True}},
                    {'tool': 'verify_identity', 'result': {'ok': True}},
                    {'tool': 'get_account', 'result': {'ok': True}},
                    {'tool': 'create_promise', 'result': {'ok': True}},
                    {'tool': 'close_call', 'result': {'ok': True}}]}
            else:
                raise AssertionError('UNEXPECTED_BANK_OR_WRITE_REQUEST:' + method + path)
            content = raw(value)
        else:
            assert parsed.hostname == 'synthetic-provider.invalid'
            if path == '/api/v2/workspaces' and method == 'GET':
                value = {'workspaces': [{'id': ids['workspace']}]}
            elif path == '/api/v2/workspaces/' + ids['workspace'] + '/credits' and method == 'GET':
                self.credit_observations += 1
                assert self.credit_observations <= 2
                value = {'creditsLeft': 499.99 if self.credit_observations == 1 else 499.97,
                         'creditUsdRate': .01}
            elif path == '/api/v2/workflow' and method == 'POST':
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
                    assert method == 'GET'
                    value = {'workflow': {'id': ids['workflow'], 'agentId': ids['agent'],
                                          'workflow': copy.deepcopy(self.graph)}}
            elif path == '/api/v2/versions/workflow/' + ids['workflow'] and method == 'GET':
                value = {'versions': [{'id': ids['version'], 'versionNumber': 1, 'isPublished': True}]}
            elif path == '/api/v2/simulations' and method == 'GET':
                value = {'simulations': [], 'total': 0}
            elif path == '/api/v2/personas' and method == 'POST':
                value = {'personas': [{'id': ids['persona']}]}
            elif path == '/api/v2/simulations' and method == 'POST':
                self.simulation_body = copy.deepcopy(body)
                value = {'simulation': {'id': ids['simulation'], 'settings': {'maxRetries': 0}}}
            elif path == '/api/v2/simulations/' + ids['simulation']:
                value = {'simulation': {'id': ids['simulation'], 'status': 'completed'}}
            elif path == '/api/v2/simulations/' + ids['simulation'] + '/iterations':
                value = {'iterations': [{'id': ids['iteration']}]}
            elif path == '/api/v2/simulations/' + ids['simulation'] + '/iterations/' + ids['iteration']:
                models = {key: pair for key, pair in self.simulation_body.items() if key.endswith('LlmOverride')}
                value = {'iteration': {'id': ids['iteration'], 'agentVersionId': ids['version'],
                            'agentVersionNumber': 1, 'params': models},
                    'runs': [{'id': 'TEST_ONLY_RUN', 'callId': ids['call'],
                              'personaId': ids['persona'], 'outcome': 'pass'}]}
            elif path == '/api/v2/call/' + ids['call']:
                value = {'call': {'id': ids['call'], 'workspaceId': ids['workspace'],
                    'assistantWorkflowId': ids['workflow'], 'simulationId': ids['simulation'],
                    'entityVersionId': ids['version'], 'entityVersionNumber': 1,
                    'startedAt': '2026-10-11T00:00:00Z', 'endedAt': '2026-10-11T00:01:00Z',
                    'messages': [{'role': 'user', 'message': 'TEST_ONLY_PRIVATE_TRANSCRIPT'}]}}
            else:
                raise AssertionError('UNEXPECTED_PROVIDER_REQUEST:' + method + path)
            content = raw({'success': True, 'data': {'result': value}})
        self.calls[-1]['response_bytes'] = content
        return WireResponse(content)


class ContestEntryEvidenceTests(unittest.TestCase):
    def test_release_requires_current_owned_complete_protected_chain(self):
        trace = self.invoke(self.factory)
        self.assertEqual(trace['status'], 'PRACTICE_CASE_PASSED', trace.get('diagnostic'))
        sink = self.sinks[0]
        original_receipt = json.loads((self.output / 'practice-evidence-seal.json').read_bytes())
        final = sink.directory / 'FINAL.json'
        original_final = final.read_bytes()
        original_value = json.loads(original_final)
        binding = original_value['binding']
        wire = sink.directory / original_value['manifest'][0]['path']
        original_wire = wire.read_bytes()
        cases = ('missing-seal', 'unsealed', 'pending', 'failed', 'recorder-failed',
                 'foreign-simulation', 'foreign-workspace', 'stale-current-binding',
                 'wrong-hash', 'wrong-path', 'missing-final', 'edited-final',
                 'edited-protected-wire', 'wire-false', 'wire-int', 'foreign-terminal',
                 'duplicate-role', 'different-reserved-request')
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                sink.sealed, sink.pending, sink.failed, sink.recorder.failed = True, None, False, False
                final.write_bytes(original_final)
                wire.write_bytes(original_wire)
                receipt, current_binding = copy.deepcopy(original_receipt), copy.deepcopy(binding)
                value = copy.deepcopy(original_value)
                guard = SimulationReservation(self.root / ('guard-' + str(index)), binding['workspace_id'])
                reserved_body = copy.deepcopy(self.http.simulation_body)
                if case == 'different-reserved-request': reserved_body['maxRetries'] = 1
                guard.reserve(reserved_body)
                guard.created({'id': binding['simulation_id']})
                guard.observe_terminal({'id': binding['simulation_id'], 'status': 'completed'})
                if case == 'missing-seal': receipt = {}
                elif case == 'unsealed': sink.sealed = False
                elif case == 'pending': sink.pending = ('TEST_ONLY_PENDING',)
                elif case == 'failed': sink.failed = True
                elif case == 'recorder-failed': sink.recorder.failed = True
                elif case in ('foreign-simulation', 'foreign-workspace'):
                    field = 'simulation_id' if case == 'foreign-simulation' else 'workspace_id'
                    current_binding[field] = identity(90)
                    value['binding'] = current_binding
                elif case == 'stale-current-binding': current_binding['iteration_id'] = identity(91)
                elif case == 'wrong-hash': receipt['sha256'] = '0' * 64
                elif case == 'wrong-path': receipt['path'] = str(self.root / 'foreign' / 'FINAL.json')
                elif case == 'missing-final': final.unlink()
                elif case == 'edited-final': final.write_bytes(original_final + b' ')
                elif case == 'edited-protected-wire': wire.write_bytes(original_wire + b'changed')
                elif case in ('wire-false', 'wire-int'):
                    value['result']['wire_join_verified'] = False if case == 'wire-false' else 1
                    receipt['result'] = copy.deepcopy(value['result'])
                elif case == 'foreign-terminal': guard.observe_terminal({'id': identity(92), 'status': 'completed'})
                elif case == 'duplicate-role': value['roles']['ledger'] = copy.deepcopy(value['roles']['session'])
                if case in ('foreign-simulation', 'foreign-workspace', 'wire-false', 'wire-int', 'duplicate-role'):
                    final.write_bytes(raw(value))
                    receipt['sha256'] = sha(final.read_bytes())
                with self.assertRaises(FoundationError):
                    guard.release_after_verified_chain(evidence=sink, seal=receipt, binding=current_binding)
                self.assertTrue(guard.path.exists(), 'UNKNOWN_OR_UNVERIFIED_RESERVATION_RELEASED')
        sink.sealed, sink.pending, sink.failed, sink.recorder.failed = True, None, False, False
        final.write_bytes(original_final)
        wire.write_bytes(original_wire)
        guard = SimulationReservation(self.root / 'genuine-guard', binding['workspace_id'])
        guard.reserve(self.http.simulation_body)
        guard.created({'id': binding['simulation_id']})
        guard.observe_terminal({'id': binding['simulation_id'], 'status': 'completed'})
        guard.release_after_verified_chain(evidence=sink, seal=original_receipt, binding=binding)
        self.assertFalse(guard.path.exists())

    def test_actual_entry_tampered_seal_keeps_workspace_reserved(self):
        actual_seal = ContestEvidence.seal
        def corrupted_receipt(sink, **kwargs):
            receipt = actual_seal(sink, **kwargs)
            receipt['sha256'] = '0' * 64
            return receipt
        with patch.object(ContestEvidence, 'seal', corrupted_receipt):
            trace = self.invoke(self.factory)
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(trace['diagnostic'], 'CONTEST_SIMULATION_PROTECTED_CHAIN_UNVERIFIED_NO_RELEASE')
        marker = SimulationReservation(self.root / 'integration/state', self.http.ids['workspace']).path
        self.assertTrue(marker.exists())
        self.output = self.root / 'artifacts/final-builder/after-tampered-seal'
        second = self.invoke(self.factory)
        self.assertEqual(second['diagnostic'], 'CONTEST_WORKSPACE_SIMULATION_PENDING_NO_REPLAY')
        self.assertEqual(sum(row['method'] == 'POST' and row['path'] == '/api/v2/simulations'
                             for row in self.http.calls), 1)

    def test_source_change_during_practice_invalidates_final_success(self):
        original = self.http.open
        source = self.root / 'orchestration/contest_practice.py'
        before = sha(source.read_bytes())
        def network(request, **kwargs):
            response = original(request, **kwargs)
            if urlsplit(request.full_url).path == '/api/v2/call/' + self.http.ids['call']:
                source.write_bytes(source.read_bytes() + b'\n# isolated source epoch change\n')
            return response
        self.http.open = network
        trace = self.invoke(self.factory)
        self.assertNotEqual(sha(source.read_bytes()), before)
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(trace['diagnostic'], 'CONTEST_RUNTIME_SOURCE_CHANGED')
        self.assertFalse(trace['live_success_verified'])
        self.assertFalse(trace['local_behavior_verified'])
        self.assertEqual(trace['protected_evidence']['status'], 'CAPTURE_FAILED_OR_JOIN_INCOMPLETE')

    def test_unknown_simulation_blocks_new_output_until_reconciled(self):
        original = self.http.open
        posts = []
        def network(request, **kwargs):
            if request.get_method() == 'POST' and urlsplit(request.full_url).path == '/api/v2/simulations':
                posts.append(request)
                original(request, **kwargs)
                raise TimeoutError('SYNTHETIC_ACCEPTED_REQUEST_RESPONSE_LOST')
            return original(request, **kwargs)
        self.http.open = network
        first = self.invoke(self.factory)
        self.assertEqual(first['status'], 'BLOCKED')
        self.assertEqual(len(posts), 1)
        self.output = self.root / 'artifacts/final-builder/fresh-run'
        second = self.invoke(self.factory)
        self.assertEqual(second['status'], 'BLOCKED')
        self.assertEqual(second['diagnostic'], 'CONTEST_WORKSPACE_SIMULATION_PENDING_NO_REPLAY')
        self.assertEqual(len(posts), 1)
        self.assertFalse(second['live_success_verified'])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'isolated-root'
        self.output = self.root / 'artifacts/final-builder/entry-test'
        self.root.mkdir()
        # Public source copies only. No real integration/runs or state enumeration.
        for package in ('builder', 'compiler', 'orchestration'):
            for source in (SOURCE_ROOT / package).glob('*.py'):
                target = self.root / source.relative_to(SOURCE_ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        for name in EVIDENCE_FILES:
            target = self.root / 'integration' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE_ROOT / 'integration' / name).read_bytes())
        self.http = SyntheticHTTP()
        self.sinks = []
        for guard in (
            patch('socket.socket', side_effect=AssertionError('SOCKET_FORBIDDEN')),
            patch('socket.getaddrinfo', side_effect=AssertionError('DNS_FORBIDDEN')),
            patch('subprocess.Popen', side_effect=AssertionError('PROCESS_FORBIDDEN')),
            patch('integration.private_state.protect_bytes', side_effect=AssertionError('DPAPI_FORBIDDEN')),
            patch('integration.private_state.unprotect_bytes', side_effect=AssertionError('RECOVERY_FORBIDDEN')),
            patch('os.environ', {'FREYA_API_KEY': 'TEST_ONLY_PROVIDER_1234567890123456',
                                 'FREYA_BANK_TEAM_KEY': 'TEST_ONLY_BANK_1234567890123456',
                                 'FREYA_API_URL': 'https://synthetic-provider.invalid'}),
            patch.object(entry, 'ROOT', self.root),
            patch.object(entry, 'OUTPUT', self.root / 'artifacts/final-builder'),
            patch.object(entry, 'build_opener', return_value=self.http),
            patch('orchestration.contest_practice.time.sleep', return_value=None),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def factory(self, directory, *, secrets):
        self.assertTrue(Path(directory).resolve().is_relative_to(self.root.resolve()))
        self.assertFalse(Path(directory).exists())
        sink = ContestEvidence(directory, secrets=secrets, protector=protect)
        self.sinks.append(sink)
        return sink

    def invoke(self, factory, *, record_credits=False):
        return entry.run(brief_path=KIT / 'briefs/brief-a.md',
            tool_api_path=KIT / 'tools/openapi.json', directory=self.output,
            provider='codex', model_provider=FixtureModel(), publish_live=True,
            practice_case='PUB-01', repetitions=1, evidence_factory=factory,
            record_credits=record_credits)

    def retain(self, name, receipt):
        RECEIPTS.append({'test': name, **receipt})
        if ARTIFACT_DIRECTORY is not None:
            destination = Path(ARTIFACT_DIRECTORY) / name
            destination.mkdir(parents=True, exist_ok=False)
            shutil.copytree(self.output, destination / 'public-output')
            for index, sink in enumerate(self.sinks, 1):
                shutil.copytree(sink.directory, destination / ('synthetic-evidence-' + str(index)))
            (destination / 'receipt.json').write_bytes(raw(receipt) + b'\n')

    def test_full_entry_publishes_runs_and_seals_real_capture_join(self):
        trace = self.invoke(self.factory)
        self.assertEqual(trace['status'], 'PRACTICE_CASE_PASSED', trace.get('diagnostic'))
        self.assertEqual(str(uuid.UUID(trace['run_id'])), trace['run_id'])
        self.assertEqual(len(self.sinks), 1)
        self.assertTrue(self.sinks[0].sealed)
        marker = SimulationReservation(self.root / 'integration/state', self.http.ids['workspace']).path
        self.assertFalse(marker.exists(), 'VERIFIED_CURRENT_CHAIN_DID_NOT_RELEASE_RESERVATION')
        receipt = json.loads((self.output / 'practice-evidence-seal.json').read_bytes())
        seal = json.loads(Path(receipt['path']).read_bytes())
        checked = verify_bundle(self.sinks[0].directory, receipt['sha256'], decoder=recover)
        self.assertTrue(checked['wire_join_verified'])
        self.assertFalse(checked['official_policy_pass_verified'])
        self.assertFalse(checked['may_write_bank'])
        self.assertFalse(checked['may_retry'])
        self.assertEqual(set(seal['roles']), set(ROLE_KEYS))
        self.assertEqual(seal['binding']['run_id'], trace['run_id'])
        physical = {'brief': 'input-brief.md', 'tool_api': 'input-api.json',
                    'provider_export': 'provider-export.json', 'source_manifest': 'runtime-source-manifest.json'}
        for role, filename in physical.items():
            artifact = seal['artifacts'][role]
            recovered = recover((self.sinks[0].directory / 'wire' / artifact['file']).read_bytes())
            self.assertEqual(recovered, (self.output / filename).read_bytes())
            self.assertEqual(sha(recovered), artifact['plaintext_sha256'])
        self.assertEqual(seal['binding']['export_sha256'], sha((self.output / 'provider-export.json').read_bytes()))
        sources = json.loads((self.output / 'runtime-source-manifest.json').read_bytes())
        self.assertEqual(sources, trace['runtime_sha256'])
        self.assertIn('orchestration/contest_build.py', sources)
        self.assertIn('integration/contest_evidence.py', sources)
        for relative, fingerprint in sources.items():
            self.assertEqual(sha((self.root / relative).read_bytes()), fingerprint)
            self.assertEqual(sha((SOURCE_ROOT / relative).read_bytes()), fingerprint,
                             'SOURCE_CHANGED_DURING_ENTRY_TEST:' + relative)
        # Actual seven request/response pairs match exact bytes seen at fake HTTP.
        for pair in seal['roles'].values():
            request = json.loads(recover((self.sinks[0].directory / 'wire' / pair['request']['file']).read_bytes()))
            response = recover((self.sinks[0].directory / 'wire' / pair['response']['file']).read_bytes())
            request_body = base64.b64decode(request['body_base64']) if request['body_base64'] else None
            matching = [row for row in self.http.calls if row['method'] == request['method']
                and row['path'] == request['path'] and row['request_bytes'] == request_body
                and row['response_bytes'] == response]
            self.assertTrue(matching, 'CAPTURE_NOT_FROM_ACTUAL_HTTP_BOUNDARY')
        for path in self.sinks[0].directory.rglob('*.json'):
            self.assertNotIn(b'TEST_ONLY_PRIVATE_TRANSCRIPT', path.read_bytes())
            self.assertNotIn(self.http.variables['session_token'].encode(), path.read_bytes())
        self.assertEqual(sum(row['method'] == 'POST' and row['path'] == '/api/v2/simulations'
                             for row in self.http.calls), 1)
        self.retain('positive', {'scope': 'SYNTHETIC_ONLY_NOT_LIVE_FREYA_PASS',
            'real_FreyaClient_api_capture': True, 'real_run_practice': True,
            'role_join_count': len(seal['roles']), 'wire_exchanges': len(self.http.calls),
            'source_hashes': sources, 'protected_seal_sha256': receipt['sha256'],
            'physical_export_sha256': seal['binding']['export_sha256'],
            'source_manifest_sha256': seal['binding']['source_manifest_sha256'],
            'run_id': trace['run_id'], 'result': checked, 'network_calls': 0,
            'real_model_calls': 0, 'credential_reads': 0, 'dpapi_calls': 0})

    def test_capture_precondition_failure_prevents_any_dispatch(self):
        def failed_factory(directory, *, secrets):
            # The actual recorder preflight fails: no protocol spy is involved.
            return ContestEvidence(directory, secrets=secrets, protector=lambda content: content)
        trace = self.invoke(failed_factory)
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(trace.get('diagnostic'), 'CONTEST_PROTECTED_EVIDENCE_HOST_UNAVAILABLE')
        self.assertEqual(self.http.calls, [])
        self.assertNotIn('publication', trace)
        self.assertFalse((self.output / 'provider-export.json').exists())
        self.assertFalse((self.output / 'practice-results.json').exists())
        self.assertEqual(trace['tests'], [])
        self.assertFalse(trace['live_success_verified'])
        self.retain('capture-precondition-failure', {'scope': 'SYNTHETIC_ONLY',
            'diagnostic': trace['diagnostic'], 'publication_started': False,
            'session_started': False, 'simulation_started': False, 'http_dispatches': 0,
            'network_calls': 0, 'real_model_calls': 0, 'credential_reads': 0, 'dpapi_calls': 0})

    def test_normal_entry_credit_pair_is_captured_before_seal_not_isolated_price(self):
        trace = self.invoke(self.factory, record_credits=True)
        self.assertEqual(trace['status'], 'PRACTICE_CASE_PASSED', trace.get('diagnostic'))
        self.assertEqual(len(self.sinks), 1)
        sink = self.sinks[0]
        self.assertTrue(sink.sealed)
        row = json.loads((self.output / 'practice-results.json').read_bytes())[0]
        credit = row['credit_observation']
        self.assertEqual(credit, trace['tests'][0]['runs'][0]['credit_observation'])
        self.assertEqual(credit['status'], 'OBSERVED_WORKSPACE_DELTA')
        self.assertAlmostEqual(credit['observed_workspace_credit_decrease'], .02)
        self.assertFalse(credit['isolated_test_price_verified'])
        self.assertFalse(credit['official_test_pass_verified'])
        self.assertEqual(credit['workspace_id'], self.http.ids['workspace'])
        self.assertLessEqual(credit['before']['response_received_at_utc'],
                             credit['after']['request_started_at_utc'])
        endpoint = '/api/v2/workspaces/' + self.http.ids['workspace'] + '/credits'
        credit_indices = [index for index, wire in enumerate(self.http.calls)
                          if wire['path'] == endpoint]
        self.assertEqual(len(credit_indices), 2)
        self.assertEqual(self.http.credit_observations, 2)
        session_index = next(index for index, wire in enumerate(self.http.calls)
                             if wire['path'] == '/v1/practice/sessions')
        call_index = next(index for index, wire in enumerate(self.http.calls)
                          if wire['path'] == '/api/v2/call/' + self.http.ids['call'])
        self.assertLess(credit_indices[0], session_index)
        self.assertGreater(credit_indices[1], call_index)
        receipt = json.loads((self.output / 'practice-evidence-seal.json').read_bytes())
        seal = json.loads(Path(receipt['path']).read_bytes())
        tickets = []
        for name, index in zip(('before', 'after'), credit_indices):
            snapshot = credit[name]
            ticket = snapshot['capture_exchange_id']
            tickets.append(ticket)
            pair = sink.exchanges[ticket]
            self.assertEqual(pair['status'], 'CAPTURED')
            self.assertEqual(pair['http_status'], 200)
            request = json.loads(recover((sink.directory / 'wire' / pair['request']['file']).read_bytes()))
            response = recover((sink.directory / 'wire' / pair['response']['file']).read_bytes())
            self.assertEqual(request['method'], 'GET')
            self.assertEqual(request['path'], endpoint)
            self.assertIsNone(request['body_base64'])
            self.assertEqual(request['params'], {})
            self.assertEqual(response, self.http.calls[index]['response_bytes'])
            fields = json.loads(response)['data']['result']
            self.assertEqual(fields['creditsLeft'], snapshot['creditsLeft'])
            self.assertEqual(fields['creditUsdRate'], snapshot['creditUsdRate'])
            self.assertEqual(sha(response), pair['response']['plaintext_sha256'])
            self.assertTrue(any(item['path'] == ticket + '.exchange.json' for item in seal['manifest']))
            self.assertTrue(any(item['path'] == 'wire/' + pair['response']['file'] for item in seal['manifest']))
        self.assertNotEqual(*tickets)
        checked = verify_bundle(sink.directory, receipt['sha256'], decoder=recover)
        self.assertTrue(checked['wire_join_verified'])
        self.assertFalse(checked['official_policy_pass_verified'])
        self.assertEqual(set(seal['roles']), set(ROLE_KEYS))
        sources = json.loads((self.output / 'runtime-source-manifest.json').read_bytes())
        helper_hash = sha((SOURCE_ROOT / 'orchestration/credit_observation.py').read_bytes())
        self.assertEqual(sources['orchestration/credit_observation.py'], helper_hash)
        self.assertEqual(sha((self.root / 'orchestration/credit_observation.py').read_bytes()), helper_hash)
        self.assertEqual(sha((self.output / 'provider-export.json').read_bytes()), seal['binding']['export_sha256'])
        self.retain('entry-credit', {'scope': 'SYNTHETIC_ONLY_NOT_LIVE_FREYA_PASS',
            'actual_entry_credit_helper': True, 'real_FreyaClient_api_capture': True,
            'credit_request_count': len(credit_indices), 'wire_exchanges': len(self.http.calls),
            'credit_observation': credit, 'credit_capture_tickets': tickets,
            'seven_role_join': checked, 'protected_seal_sha256': receipt['sha256'],
            'credit_helper_sha256': helper_hash, 'physical_export_sha256': seal['binding']['export_sha256'],
            'network_calls': 0, 'real_model_calls': 0, 'credential_reads': 0, 'dpapi_calls': 0})


if __name__ == '__main__':
    unittest.main()
