"""Execute generated code through the repair/publication wiring without a bank."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from builder.api_contract import BankContract
from orchestration.contest_build import OUTPUT, run, validate_workflow, verify_source_snapshot

INPUT = Path(__file__).parent / 'inputs'
BRIEF = INPUT / 'single-brief.md'
API = INPUT / 'openapi.json'


def request_graph(contract, amount):
    bodies = {
        'verify_identity': {'customer_id': 'offline-unit', 'field': 'phone_last4', 'answer': '4321'},
        'get_account': {'customer_id': 'offline-unit'},
        'create_promise': {'customer_id': 'offline-unit', 'product_id': 'UNIT',
                           'amount': amount, 'date': '2026-11-13'},
        'close_call': {'customer_id': 'offline-unit', 'outcome': 'promise'},
    }
    nodes = {}
    names = list(bodies)
    for index, name in enumerate(names):
        action = copy.deepcopy(contract.freya_templates()['tools'][name])
        action['pre_process_code'] = 'config["body"] = ' + repr(bodies[name])
        nodes[name] = {'name': name, 'node_type': 'api_request', 'passthrough': True, 'pre_actions': [action],
                       'on_enter': {'type': 'silent'}, 'transitions': [
                           {'target_node': names[index + 1] if index + 1 < len(names) else 'end',
                            'condition': {'operator': 'always'}}]}
    nodes['end'] = {'name': 'end', 'node_type': 'end_call', 'on_enter': {'type': 'silent'},
                    'terminal': {'function': {'name': 'end_call', 'handler': '__function__:end_call',
                                             'parameters': {'type': 'object', 'properties': {
                                                 'message_mode': {'type': 'string', 'default': 'none'}}}}}}
    return {'initial_node': names[0], 'nodes': nodes, 'global_nodes': {}}


def fixture(contract):
    bodies = [
        {'customer_id': 'offline-unit', 'field': 'phone_last4', 'answer': '4321'},
        {'customer_id': 'offline-unit'},
        {'customer_id': 'offline-unit', 'product_id': 'UNIT', 'amount': 80, 'date': '2026-11-13'},
        {'customer_id': 'offline-unit', 'outcome': 'promise'},
    ]
    names = ['verify_identity', 'get_account', 'create_promise', 'close_call']
    response = {'status': 'success', 'status_code': 200, 'data': {'ok': True}, 'header': {}}
    return {'kind': 'generated-behavior-fixture-1',
            'brief_sha256': hashlib.sha256(BRIEF.read_bytes()).hexdigest(),
            'tool_api_sha256': hashlib.sha256(API.read_bytes()).hexdigest(),
            'cases': [{'name': 'independent-fixture-marker', 'call_variables': {},
                       'responses': [{'operation': name, 'result': response} for name in names],
                       'steps': [{'extractions': {}, 'expect': {
                           'requests': [{'operation': name, 'body': body} for name, body in zip(names, bodies)],
                           'speech_contains': [], 'ended': True}}]}]}


class RepairModel:
    def __init__(self, contract, amounts):
        self.contract, self.amounts, self.calls = contract, amounts, []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        workflow = request_graph(self.contract, self.amounts[min(len(self.calls) - 1, len(self.amounts) - 1)])
        response = {'workflow_json': json.dumps(workflow), 'unresolved': [],
                    'coverage': [{'quote': 'source', 'nodes': ['create_promise'], 'implementation': 'unit fixture'}],
                    'limitations': ['synthetic wiring test only']}
        (kwargs['directory'] / 'model-response.json').write_text(json.dumps(response), encoding='utf-8')
        return workflow, {'model': 'synthetic-unit-only', 'semantic_correctness_verified': False}


class BehaviorPipelineTests(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.contract = BankContract.from_path(API)

    def test_executed_request_mismatch_repairs_before_publication(self):
        model = RepairModel(self.contract, [81, 80])
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            fixture_path = root / 'fixture.json'
            fixture_path.write_text(json.dumps(fixture(self.contract)), encoding='utf-8')
            def publication(workflow, **kwargs):
                self.assertEqual(len(model.calls), 2)
                (kwargs['directory'] / 'provider-export.json').write_text(json.dumps(workflow), encoding='utf-8')
                return {'workflow_id': 'unit-workflow', 'agent_id': 'unit-agent'}
            with patch('orchestration.contest_build.publish', side_effect=publication) as publish:
                result = run(brief_path=BRIEF, tool_api_path=API, directory=root / 'run',
                             provider='codex', model_provider=model, behavior_fixture=fixture_path,
                             publish_live=True, client_factory=lambda *args: SimpleNamespace(), deadline_seconds=60)
            self.assertEqual(result['status'], 'PROVIDER_EXPORTED_TESTS_REQUIRED', result)
            self.assertTrue(result['local_behavior_verified'])
            self.assertFalse(result['live_success_verified'])
            self.assertEqual(len(result['tests']), 1)
            self.assertEqual(result['compilation']['attempts'][0]['diagnostic'], 'CONTEST_GENERATED_BEHAVIOR_FAILED')
            self.assertTrue((root / 'run/model-attempt-1/behavior-report.json').is_file())
            self.assertTrue((root / 'run/model-attempt-2/behavior-report.json').is_file())
            self.assertIn('behavior-fixture.json', json.loads((root / 'run/manifest.json').read_text()))
            publish.assert_called_once()
        self.assertNotIn('independent-fixture-marker', json.dumps(model.calls[1]['feedback']))
        self.assertIn('behavior_diagnostics', model.calls[1]['feedback'])
        self.assertNotIn('fixture', model.calls[0])

    def test_persistent_behavior_failure_never_publishes(self):
        model = RepairModel(self.contract, [81])
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            fixture_path = root / 'fixture.json'
            fixture_path.write_text(json.dumps(fixture(self.contract)), encoding='utf-8')
            with patch('orchestration.contest_build.publish') as publish:
                result = run(brief_path=BRIEF, tool_api_path=API, directory=root / 'run',
                             provider='codex', model_provider=model, behavior_fixture=fixture_path,
                             publish_live=True, deadline_seconds=60)
            self.assertEqual(result['status'], 'BLOCKED', result)
            self.assertEqual(result['diagnostic'], 'CONTEST_GENERATED_BEHAVIOR_FAILED')
            self.assertEqual(len(model.calls), 3)
            self.assertFalse(result['local_behavior_verified'])
            publish.assert_not_called()

    def test_fixture_for_other_sources_blocks_before_model_call(self):
        model = RepairModel(self.contract, [80])
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            value = fixture(self.contract)
            value['tool_api_sha256'] = '0' * 64
            fixture_path = root / 'fixture.json'
            fixture_path.write_text(json.dumps(value), encoding='utf-8')
            result = run(brief_path=BRIEF, tool_api_path=API, directory=root / 'run',
                         provider='codex', model_provider=model, behavior_fixture=fixture_path, deadline_seconds=60)
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertIn('FIXTURE', result['diagnostic'])
            self.assertEqual(model.calls, [])

    def test_legacy_code_template_delimiters_refused(self):
        workflow = request_graph(self.contract, 80)
        workflow['nodes']['get_account']['pre_actions'][0]['post_process_code'] = 'result["text"] = "{{value}}"'
        with self.assertRaisesRegex(Exception, 'CONTEST_CODE_TEMPLATE_DELIMITERS_REFUSED'):
            validate_workflow(workflow, self.contract)

    def test_changed_runtime_refused_before_behavior_or_publication(self):
        model = RepairModel(self.contract, [80])
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root = Path(temporary)
            fixture_path = root / 'fixture.json'
            fixture_path.write_text(json.dumps(fixture(self.contract)), encoding='utf-8')
            from builder.models import FoundationError
            with patch('orchestration.contest_build.verify_source_snapshot',
                       side_effect=[None, FoundationError('CONTEST_RUNTIME_SOURCE_CHANGED'),
                                    FoundationError('CONTEST_RUNTIME_SOURCE_CHANGED')]), \
                 patch('orchestration.generated_behavior.verify_behavior') as behavior, \
                 patch('orchestration.contest_build.publish') as publish:
                result = run(brief_path=BRIEF, tool_api_path=API, directory=root / 'run',
                             provider='codex', model_provider=model, behavior_fixture=fixture_path,
                             publish_live=True, deadline_seconds=60)
            self.assertEqual(result['diagnostic'], 'CONTEST_RUNTIME_SOURCE_CHANGED')
            self.assertEqual(len(model.calls), 1)
            behavior.assert_not_called()
            publish.assert_not_called()

    def test_source_snapshot_checks_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'runtime.py'
            source.write_bytes(b'first')
            trace = {'runtime_sha256': {'runtime.py': hashlib.sha256(b'first').hexdigest()}}
            with patch('orchestration.contest_build.ROOT', root):
                verify_source_snapshot(trace)
                source.write_bytes(b'second')
                with self.assertRaisesRegex(Exception, 'CONTEST_RUNTIME_SOURCE_CHANGED'):
                    verify_source_snapshot(trace)


if __name__ == '__main__':
    unittest.main()
