"""Independent expectations exercise actual generated code in an offline child."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from builder.api_contract import BankContract
from orchestration.contest_build import Budget
from orchestration.generated_behavior import BehaviorError, read_fixture, verify_behavior

API = Path(__file__).parent / 'inputs/openapi.json'
BRIEF_HASH = hashlib.sha256(b'Independent changed amount and date policy').hexdigest()


def code_action(code):
    return {'function': {'name': 'code_exec', 'handler': '__function__:code_exec',
            'parameters': {'type': 'object', 'properties': {'code': {'type': 'string', 'default': code}}}}}


def always(target):
    return {'condition': {'operator': 'always'}, 'target_node': target}


class GeneratedBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.contract = BankContract.from_path(API)
        self.request = {'operation': 'create_promise', 'body': {
            'customer_id': 'offline-fixture', 'product_id': 'ONE', 'amount': 123.75, 'date': '2026-11-14'}}
        # This API may intentionally differ from the generated Cedar candidate.
        fields = self.contract.operations['create_promise'].request['properties']
        self.amount_field = 'amount' if 'amount' in fields else 'promised_total'
        self.date_field = 'date' if 'date' in fields else 'due_on'
        self.request['body'] = {'customer_id': 'offline-fixture', 'product_id': 'ONE',
                               self.amount_field: 123.75, self.date_field: '2026-11-14'}
        write = copy.deepcopy(self.contract.freya_templates()['tools']['create_promise'])
        write['pre_process_code'] = (
            "config['body'] = {'customer_id': vars['customer_id'], 'product_id': 'ONE', "
            + repr(self.amount_field) + ": vars['amount'], " + repr(self.date_field) + ": vars['payment_date']}\n")
        self.workflow = {'initial_node': 'prepare', 'global_nodes': {}, 'nodes': {
            'prepare': {'node_type': 'code', 'pre_actions': [code_action(
                "import json\nimport re\nimport datetime\n"
                "vars['amount'] = vars['proposed_amount'] + 0.25\n"
                "vars['payment_date'] = datetime.date.fromisoformat(vars['proposed_date']).isoformat()\n"
                "vars['say'] = 'Please confirm ' + str(vars['amount']) + ' on ' + vars['payment_date']\n")],
                'transitions': [always('listen')]},
            'listen': {'node_type': 'conversation', 'on_enter': {'type': 'tts', 'text': '{{say}}'},
                'extraction_schema': [{'name': 'decision', 'type': 'boolean'}], 'transitions': [always('reconcile')]},
            'reconcile': {'node_type': 'code', 'pre_actions': [code_action("vars['send'] = vars.get('decision') is True\n")],
                'transitions': [{'condition': {'operator': 'equals', 'variable': 'send', 'operand': True},
                                 'target_node': 'write'}, always('end')]},
            'write': {'node_type': 'api_request', 'pre_actions': [write], 'transitions': [always('end')]},
            'end': {'node_type': 'end_call', 'on_enter': {'type': 'silent'}, 'terminal': {
                'function': {'handler': '__function__:end_call', 'parameters': {
                    'type': 'object', 'properties': {
                        'message_mode': {'type': 'string', 'default': 'custom'},
                        'spoken_message': {'type': 'string', 'default': 'Goodbye.'}}}}}}}}
        for node in self.workflow['nodes'].values():
            if node['node_type'] in ('code', 'api_request'):
                node['passthrough'] = True

    def content(self):
        return {'kind': 'generated-behavior-fixture-1', 'brief_sha256': BRIEF_HASH,
                'tool_api_sha256': self.contract.source_hash, 'cases': [{
            'name': 'Changed request fields and explicit fresh confirmation',
            'call_variables': {'customer_id': 'offline-fixture', 'proposed_amount': 123.5,
                               'proposed_date': '2026-11-14'},
            'responses': [{'operation': 'create_promise', 'result': {'status': 'success',
                'status_code': 200, 'data': {'ok': True}, 'header': {}}}],
            'steps': [{'extractions': {}, 'expect': {'requests': [], 'speech_contains': ['123.75 on 2026-11-14'], 'ended': False}},
                      {'extractions': {'decision': True}, 'expect': {'requests': [self.request], 'speech_contains': ['Goodbye.'], 'ended': True}}]}]}

    def fixture(self, content=None):
        path = self.directory / 'fixture.json'
        path.write_text(json.dumps(content or self.content()), encoding='utf-8')
        return read_fixture(path, BRIEF_HASH, self.contract.source_hash)

    def verify(self, workflow=None, fixture=None):
        return verify_behavior(workflow or self.workflow, self.contract, fixture or self.fixture(),
                               self.directory / 'run', Budget(20))

    def test_real_code_and_hooks_prepare_exact_changed_request_only_after_consent(self):
        report = self.verify()
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['status'], 'LOCAL_BEHAVIOR_PASSED')
        self.assertEqual(report['cases'][0]['requests'], [self.request])
        self.assertFalse(report['semantic_correctness_verified'])
        self.assertFalse(report['live_success_verified'])
        self.assertEqual(report['fixture_sha256'], hashlib.sha256((self.directory/'fixture.json').read_bytes()).hexdigest())

    def test_policy_error_in_real_code_fails_observed_request(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['prepare']['pre_actions'][0] = code_action(
            "vars['amount'] = vars['proposed_amount'] + 1\nvars['payment_date'] = vars['proposed_date']\nvars['say']='confirm'\n")
        content = self.content()
        content['cases'][0]['steps'][0]['expect'].pop('speech_contains')
        report = self.verify(changed, self.fixture(content))
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_REQUEST_EXPECTATION_MISMATCH')
        self.assertEqual(report['cases'][0]['requests'][0]['body'][self.amount_field], 124.5)

    def test_missing_and_invalid_body_fail_even_if_internal_guard_claims_authorized(self):
        for code in ("vars['hook_guard']='authorized'\nconfig['body']={}\n",
                     "vars['hook_guard']='authorized'\nconfig['body']={'customer_id':'offline-fixture','product_id':'ONE'," + repr(self.amount_field) + ":'123.75'," + repr(self.date_field) + ":'2026-11-14'}\n"):
            with self.subTest(code=code):
                changed = copy.deepcopy(self.workflow)
                changed['nodes']['write']['pre_actions'][0]['pre_process_code'] = code
                report = self.verify(changed)
                self.assertFalse(report['passed'])
                self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_REQUEST_BODY_INVALID')
                self.assertEqual(len(report['cases'][0]['requests']), 1)

    def test_post_hook_mutation_is_seen_by_mapping_and_later_code(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['write']['pre_actions'][0]['post_process_code'] = (
            "result['data']['mapped']='after hook'\nvars['hook_observation']='call variable written'\n")
        changed['nodes']['write']['response_mapping'] = [{'path': 'data.mapped', 'variable': 'mapped'}]
        changed['nodes']['write']['transitions'] = [always('summary')]
        changed['nodes']['summary'] = {'node_type': 'code', 'passthrough': True, 'pre_actions': [code_action(
            "vars['summary'] = vars['mapped'] + ' ' + vars['hook_observation']\n")], 'transitions': [always('end')]}
        changed['nodes']['end']['terminal']['function']['parameters']['properties']['spoken_message']['default'] = '{{summary}}'
        content = self.content()
        content['cases'][0]['steps'][1]['expect']['speech_contains'] = ['after hook call variable written']
        report = self.verify(changed, self.fixture(content))
        self.assertTrue(report['passed'], report)

    def test_omitted_extraction_keeps_previous_value_and_exposes_stale_consent(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['reconcile']['transitions'][1] = always('listen')
        content = self.content()
        case = content['cases'][0]
        changed['nodes']['reconcile']['pre_actions'] = [code_action(
            "if vars.get('first_turn') is None:\n    vars['first_turn']=True\n    vars['send']=False\nelse:\n    vars['send']=vars.get('decision') is True\n")]
        case['steps'][1] = {'extractions': {'decision': True}, 'expect': {'requests': [], 'ended': False}}
        case['steps'].append({'extractions': {}, 'expect': {'requests': [], 'ended': False}})
        report = self.verify(changed, self.fixture(content))
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_REQUEST_EXPECTATION_MISMATCH')
        self.assertEqual(report['cases'][0]['requests'], [self.request])

    def test_equals_uses_documented_string_conversion(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['reconcile']['transitions'][0]['condition']['operand'] = 'True'
        self.assertTrue(self.verify(changed)['passed'])

    def test_previous_readback_cannot_satisfy_a_fresh_step_speech_expectation(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['listen']['on_enter_first_entry_only'] = True
        changed['nodes']['reconcile']['transitions'][1] = always('listen')
        content = self.content()
        case = content['cases'][0]
        case['responses'] = []
        case['steps'][1] = {'extractions': {'decision': False}, 'expect': {
            'requests': [], 'speech_contains': ['123.75 on 2026-11-14'], 'ended': False}}
        report = self.verify(changed, self.fixture(content))
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_SPEECH_EXPECTATION_MISMATCH')
        self.assertEqual(report['cases'][0]['steps_completed'], 1)
        self.assertIn('123.75 on 2026-11-14', report['cases'][0]['speech'][0])
        self.assertEqual(len(report['cases'][0]['speech']), 1)
        # Actually speaking it again in the later step satisfies the fixture.
        changed['nodes']['listen']['on_enter_first_entry_only'] = False
        self.assertTrue(self.verify(changed, self.fixture(content))['passed'])

    def test_missing_request_placeholder_drops_field_and_speech_preserves_literal(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['write']['pre_actions'][0].pop('pre_process_code')
        # Defaults reference unset input variables. The request must have only
        # the existing customer id, rather than fabricated null values.
        report = self.verify(changed)
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['requests'][0]['body'], {'customer_id': 'offline-fixture'})
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['listen']['on_enter']['text'] = '{{missing}}'
        content = self.content()
        content['cases'][0]['steps'][0]['expect']['speech_contains'] = ['{{missing}}']
        self.assertTrue(self.verify(changed, self.fixture(content))['passed'])

    def test_infinite_action_is_stopped_by_instruction_budget(self):
        changed = copy.deepcopy(self.workflow)
        changed['nodes']['prepare']['pre_actions'] = [code_action('while True:\n    pass\n')]
        report = self.verify(changed)
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_ACTION_BUDGET_EXCEEDED')

    def test_unsupported_graph_is_never_pass(self):
        changed = copy.deepcopy(self.workflow)
        changed['global_nodes'] = {'interrupt': {'node_type': 'conversation'}}
        report = self.verify(changed)
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_GRAPH_UNSUPPORTED')

    def test_non_passthrough_action_and_missing_terminal_are_never_automatic_pass(self):
        for node, field, value, diagnostic in (
            ('write', 'passthrough', False, 'BEHAVIOR_PASSTHROUGH_REQUIRED'),
            ('end', 'terminal', None, 'BEHAVIOR_TERMINAL_UNSUPPORTED')):
            with self.subTest(node=node):
                changed = copy.deepcopy(self.workflow)
                changed['nodes'][node][field] = value
                report = self.verify(changed)
                self.assertFalse(report['passed'])
                self.assertEqual(report['cases'][0]['diagnostic'], diagnostic)

    def test_module_facade_cannot_expose_process_or_private_attributes(self):
        for code in ('import re\nvars["leak"]=re.sys.version\n',
                     'import json\nvars["leak"]=json.loads.__globals__\n',
                     'import os\nvars["leak"]=os.environ\n'):
            with self.subTest(code=code):
                changed = copy.deepcopy(self.workflow)
                changed['nodes']['prepare']['pre_actions'] = [code_action(code)]
                report = self.verify(changed)
                self.assertFalse(report['passed'])
                self.assertNotIn('leak', json.dumps(report))

    def test_fixture_source_and_after_read_mutation_are_rejected(self):
        content = self.content()
        content['brief_sha256'] = '0'*64
        with self.assertRaisesRegex(BehaviorError, 'SOURCE_MISMATCH'):
            self.fixture(content)
        fixture = self.fixture()
        fixture['cases'][0]['steps'][1]['expect']['requests'][0]['body'][self.amount_field] = 99
        report = self.verify(fixture=fixture)
        self.assertFalse(report['passed'])
        self.assertEqual(report['diagnostic'], 'BEHAVIOR_FIXTURE_CHANGED_AFTER_READ')

    def test_closed_fixture_rejects_duplicate_keys_and_extra_fields(self):
        path = self.directory/'fixture.json'
        path.write_text('{"kind":"a","kind":"b"}')
        with self.assertRaisesRegex(BehaviorError, 'DUPLICATE_KEY'):
            read_fixture(path, BRIEF_HASH, self.contract.source_hash)
        content = self.content()
        content['cases'][0]['model_coverage'] = 'not an independent expectation'
        with self.assertRaisesRegex(BehaviorError, 'SHAPE_INVALID'):
            self.fixture(content)

    def test_overflowing_json_float_and_nonserializable_values_use_fixed_diagnostics(self):
        path = self.directory/'fixture.json'
        path.write_text(json.dumps(self.content()).replace('123.5', '1e999'))
        with self.assertRaisesRegex(BehaviorError, 'JSON_NONFINITE'):
            read_fixture(path, BRIEF_HASH, self.contract.source_hash)
        fixture = self.fixture()
        fixture['cases'][0]['call_variables']['unsupported'] = object()
        report = self.verify(fixture=fixture)
        self.assertFalse(report['passed'])
        self.assertEqual(report['diagnostic'], 'BEHAVIOR_JSON_VALUE_INVALID')

    def test_child_timeout_and_expired_budget_are_failures(self):
        import subprocess
        with patch('orchestration.generated_behavior.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 1)):
            report = self.verify()
        self.assertFalse(report['passed'])
        self.assertEqual(report['diagnostic'], 'BEHAVIOR_WORKER_TIMEOUT')
        budget = Budget(1, clock=iter([0, 2]).__next__)
        report = verify_behavior(self.workflow, self.contract, self.fixture(), self.directory/'run', budget)
        self.assertFalse(report['passed'])


class BehaviorObservationTests(unittest.TestCase):
    def test_summary_emits_counts_without_fixture_or_actual_content(self):
        from orchestration.generated_behavior import summarize_behavior_observations
        rows = [{'passed': False, 'id': 'private-case', 'steps_completed': 2,
            'requests': [{'customer_id': 'private-customer', 'amount': 998}],
            'speech': ['private utterance'], 'ended': False,
            'expect': {'secret': 'gold'}, 'extraction': {'decision': 'yes'}}] * 2
        summary = summarize_behavior_observations({'cases': rows})
        self.assertEqual(summary['failure_groups'], [{'completed_steps': 2,
            'observed_request_count': 1, 'observed_speech_count': 1,
            'ended': False, 'occurrences': 2}])
        serialized = json.dumps(summary)
        for secret in ('private-case', 'private-customer', 'private utterance',
                'gold', 'extraction', '998', 'decision'):
            self.assertNotIn(secret, serialized)

    def test_summary_does_not_coerce_invalid_values(self):
        from orchestration.generated_behavior import summarize_behavior_observations
        summary = summarize_behavior_observations({'cases': [{'passed': False,
            'steps_completed': True, 'requests': {}, 'speech': 'text', 'ended': 1}]})
        group = summary['failure_groups'][0]
        self.assertTrue(all(group[key] is None for key in (
            'completed_steps', 'observed_request_count', 'observed_speech_count', 'ended')))
        self.assertEqual(summarize_behavior_observations({'cases': None})['failed_case_count'], 0)

if __name__ == '__main__':
    unittest.main()
