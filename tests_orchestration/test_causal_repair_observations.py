"""Synthetic only: no model, live service, or actual generated candidate runs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from builder.models import FoundationError
from builder.model_workflow import prompt_for
from orchestration import contest_build as cb
from orchestration.generated_behavior import (_Graph, _base_report, _run_suite, read_fixture,
    summarize_behavior_observations, verify_behavior)
from orchestration.repair_observations import (delimiter_locations, remember_behavior,
    safe_input, summarize_contexts, summarize_input)

PRIVATE = 'PRIVATE_SYNTHETIC_MARKER'


def synthetic_payload():
    always = lambda target: {'target_node': target, 'condition': {'operator': 'always'}}
    workflow = {'initial_node': 'opening', 'nodes': {
        'opening': {'node_type': 'conversation',
            'on_enter': {'type': 'tts', 'text': PRIVATE},
            'extraction_schema': [{'name': 'speech_event', 'type': 'string'}],
            'transitions': [always('controller')]},
        'controller': {'node_type': 'code', 'passthrough': True, 'transitions': [
            {'target_node': 'end', 'condition': {'operator': 'equals',
                'variable': 'unrelated_route', 'operand': 'ready'}}, always('opening')]},
        'end': {'node_type': 'end_call', 'terminal': {}}}}
    extractions = {'speech_event': json.dumps({'decision': 'yes', 'identity_answer': PRIVATE,
        'amount': 987654321, 'date': '2097-12-29', 'source_intent': PRIVATE,
        'source_values': {PRIVATE: PRIVATE}, 'events': [{'scope': PRIVATE}]})}
    fixture = {'kind': 'generated-behavior-fixture-1', 'brief_sha256': 'a'*64,
        'tool_api_sha256': 'b'*64, 'cases': [{'name': PRIVATE, 'call_variables': {},
        'responses': [], 'steps': [{'extractions': {}, 'expect': {'requests': []}},
            {'extractions': extractions, 'expect': {'requests': [
                {'operation': PRIVATE, 'body': {PRIVATE: PRIVATE}}]}}]}]}
    return {'workflow': workflow, 'fixture': fixture, 'deadline_seconds': 5,
        'contract': {'source_sha256': 'b'*64, 'operations': {}}}


class CausalRepairTests(unittest.TestCase):
    def actual_summary(self):
        payload = synthetic_payload()
        with patch('orchestration.generated_behavior._execute', side_effect=AssertionError('No code execution')):
            report = dict(_base_report(payload['workflow'], {}), **_run_suite(payload))
        return payload, report, summarize_behavior_observations(report, payload['workflow'])

    def test_actual_opening_loop_has_decision_question_and_evaluated_path(self):
        payload, report, summary = self.actual_summary()
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][0]['diagnostic'], 'BEHAVIOR_REQUEST_EXPECTATION_MISMATCH')
        context = summary['failure_contexts'][0]
        self.assertEqual(context['input']['explicit_decision'], 'yes')
        self.assertEqual(context['question_node']['node_id'], 'opening')
        self.assertEqual(context['stopped_node']['node_id'], 'opening')
        routes = [r for r in context['trace'] if r['event'] == 'transition']
        self.assertEqual([(r['node']['node_id'], r['transition_index'], r['matched']) for r in routes],
            [('opening', 0, True), ('controller', 0, False), ('controller', 1, True)])
        self.assertEqual(context['controller_state'], 'not_observed')
        encoded = json.dumps(summary)
        for secret in (PRIVATE, '987654321', '2097-12-29', 'unrelated_route', 'ready'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(payload['fixture'], synthetic_payload()['fixture'])

    def test_real_isolated_worker_node_pointer_matches_unsorted_source(self):
        payload = synthetic_payload()
        self.assertEqual(list(payload['workflow']['nodes']), ['opening', 'controller', 'end'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_path = root / 'fixture.json'
            fixture_path.write_text(json.dumps(payload['fixture']))
            fixture = read_fixture(fixture_path, 'a'*64, 'b'*64)
            contract = SimpleNamespace(source_hash='b'*64, serializable=lambda: payload['contract'])
            report = verify_behavior(payload['workflow'], contract, fixture, root/'actual-worker', cb.Budget(5))
            self.assertFalse(report['passed'])
            context = summarize_behavior_observations(report, payload['workflow'])['failure_contexts'][0]
            self.assertEqual(context['question_node']['node_id'], 'opening')
            self.assertEqual(context['stopped_node']['node_id'], 'opening')
            transitions = [row for row in context['trace'] if row['event'] == 'transition']
            self.assertEqual([(row['node']['node_id'], row['target']['node_id'], row['matched'])
                for row in transitions], [('opening','controller',True),
                ('controller','end',False), ('controller','opening',True)])

    def test_malformed_unknown_and_host_values_never_stringify(self):
        class Forbidden:
            def __repr__(self):
                raise AssertionError('repr called')
            def __str__(self):
                raise AssertionError('str called')
        for value, state in ((Forbidden(), 'invalid_type'), ('{"decision":"yes","decision":"no"}', 'invalid_json'),
                ('{"amount":NaN}', 'invalid_json'), ('{"amount":1e999}', 'invalid_json'),
                ('[]', 'non_object'), ('{broken '+PRIVATE, 'invalid_json')):
            self.assertEqual(summarize_input({'speech_event': value})['speech_event'], state)
        summary = summarize_input({'speech_event': json.dumps({PRIVATE: PRIVATE,
            'decision': [PRIVATE], 'amount': True, 'events': [PRIVATE, {PRIVATE: PRIVATE}]})})
        self.assertEqual(summary['additional_key_count'], 1)
        self.assertEqual(summary['fields']['amount'], 'boolean')
        self.assertNotIn('explicit_decision', summary)
        self.assertNotIn(PRIVATE, json.dumps(summary))
        self.assertEqual(safe_input({'speech_event': Forbidden(), 'fields': {'decision': Forbidden()}}),
            {'speech_event': 'unavailable'})
        self.assertEqual(summarize_input({123: Forbidden(), 'speech_date': Forbidden()})['speech_date_type'], 'invalid')

    def test_context_rebuilt_from_fixed_schema_and_workflow_metadata(self):
        payload, report, _ = self.actual_summary()
        observed = report['cases'][0]['causal_observation']
        observed.update(controller_state=PRIVATE, node_id=PRIVATE, extra=PRIVATE)
        observed['input']['fields']['evil'] = PRIVATE
        observed['trace'].append({'event': 'transition', 'node': True, 'matched': True, 'transition_index': 0})
        self.assertNotIn(PRIVATE, json.dumps(summarize_contexts(report['cases'], payload['workflow'])))
        self.assertEqual(summarize_contexts(report['cases'], None), [])

    def test_worker_boundary_preserves_context_and_trace_is_bounded(self):
        payload, report, summary = self.actual_summary()
        graph = _Graph(payload['workflow'], SimpleNamespace(), payload['fixture']['cases'][0], 0)
        graph.begin_observation(1, {})
        for _ in range(100):
            graph.observe('enter')
        self.assertEqual(len(graph.causal_observation['trace']), 24)
        self.assertTrue(graph.causal_observation['trace_truncated'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_path = root/'fixture.json'
            fixture_path.write_text(json.dumps(payload['fixture']))
            fixture = read_fixture(fixture_path, 'a'*64, 'b'*64)
            contract = SimpleNamespace(source_hash='b'*64, serializable=lambda: payload['contract'])
            def worker(command, **kwargs):
                self.assertEqual(command[1:3], ['-I', '-B'])
                self.assertGreater(kwargs['timeout'], 0)
                self.assertEqual(kwargs['env'], {})
                Path(command[-1]).write_text(json.dumps({key: report[key] for key in ('passed', 'cases')}))
                return SimpleNamespace(returncode=0)
            with patch('orchestration.generated_behavior.subprocess.run', side_effect=worker):
                transferred = verify_behavior(payload['workflow'], contract, fixture, root/'run', cb.Budget(5))
            self.assertEqual(summarize_behavior_observations(transferred, payload['workflow']), summary)

    def test_history_bounded_and_versioned_delimiter_metadata_has_no_snippets(self):
        _, _, summary = self.actual_summary()
        history = []
        for text in (b'one', b'two', b'three'):
            history = remember_behavior(history, text, summary)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]['response_sha256'], hashlib.sha256(b'three').hexdigest())
        self.assertTrue(all(row['status'] == 'not_retested' for row in history))
        result = delimiter_locations({'code_blocks': [{'id': 'controller', 'code': PRIVATE+'\nx={"a":{}}'}]})
        self.assertEqual(result, [{'code_block_index': 0, 'code_block_id': 'controller',
            'line': 2, 'column': 9, 'token': '}}'}])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def run_retry(self, with_fixture):
        payload, report, summary = self.actual_summary()
        calls = []
        first_bytes = json.dumps({'workflow_json': '{}', 'code_blocks': [],
            'unresolved': [], 'coverage': [], 'limitations': []}).encode()
        class Model:
            def generate(self, **kwargs):
                calls.append(copy.deepcopy(kwargs['feedback']))
                index = len(calls)
                response = first_bytes if index != 2 else json.dumps({'code_blocks': [
                    {'id': 'controller', 'code': 'x={"a":{}}'}]}).encode()
                (kwargs['directory']/'model-response.json').write_bytes(response)
                if index == 2:
                    raise FoundationError('BUILDER_MODEL_CODE_TEMPLATE_DELIMITERS_REFUSED')
                if not with_fixture and index == 1:
                    raise FoundationError('BUILDER_MODEL_PLAN_FIELDS_INVALID')
                return payload['workflow'], {'calls': 1}
        contract = SimpleNamespace(freya_templates=lambda: {'tools': {}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief, api, fixture = root/'brief.md', root/'api.json', root/'fixture.json'
            brief.write_text('Synthetic unrelated policy')
            api.write_text('{}')
            content = payload['fixture']
            content['brief_sha256'] = hashlib.sha256(brief.read_bytes()).hexdigest()
            content['tool_api_sha256'] = hashlib.sha256(api.read_bytes()).hexdigest()
            fixture.write_text(json.dumps(content))
            kit = root/'kit'
            for name in ('freya.py', 'docs/workflow-format.md', 'docs/tools-and-code.md', 'tools/score.py'):
                path = kit/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic platform source')
            reports = [report, {'passed': True, 'cases': []}]
            with patch.object(cb, 'OUTPUT', root), patch.object(cb, 'KIT', kit), \
                    patch.object(cb, 'BankContract', return_value=contract), \
                    patch.object(cb, 'validate_workflow', return_value={}), \
                    patch('orchestration.generated_behavior.verify_behavior', side_effect=reports) as verify:
                result = cb.run(brief_path=brief, tool_api_path=api, directory=root/'run',
                    provider='codex', model_provider=Model(),
                    behavior_fixture=fixture if with_fixture else None, deadline_seconds=20)
            self.assertEqual(verify.call_count, 2 if with_fixture else 0)
            self.assertEqual(result['status'], 'LOCAL_BEHAVIOR_PASSED' if with_fixture else 'LOCAL_WORKFLOW_GENERATED')
        return calls, first_bytes, summary

    def test_actual_contest_retry_payload_retains_source_versioned_behavior_after_static_failure(self):
        calls, first_bytes, summary = self.run_retry(True)
        self.assertIsNone(calls[0])
        self.assertEqual(calls[1]['behavior_observations'], summary)
        third = calls[2]
        self.assertNotIn('behavior_observations', third)
        self.assertNotIn('behavior_diagnostics', third)
        self.assertEqual(third['behavior_history'], [{'response_sha256': hashlib.sha256(first_bytes).hexdigest(),
            'status': 'not_retested', 'observations': summary}])
        self.assertEqual(third['code_delimiter_locations'][0]['code_block_id'], 'controller')
        self.assertNotIn(PRIVATE, json.dumps(third))

    def test_no_fixture_does_not_add_behavior_or_oracle(self):
        calls, _, _ = self.run_retry(False)
        for feedback in calls[1:]:
            self.assertFalse(any(key.startswith('behavior_') for key in feedback))

    def test_context_requires_exact_report_workflow_fingerprint(self):
        payload, report, summary = self.actual_summary()
        self.assertIn('failure_contexts', summary)
        different = copy.deepcopy(payload['workflow'])
        different['nodes']['controller']['transitions'].reverse()
        for candidate, evidence in ((different, report), (payload['workflow'], dict(report, workflow_sha256='c'*64)),
                (payload['workflow'], {key: value for key, value in report.items() if key != 'workflow_sha256'})):
            safe = summarize_behavior_observations(evidence, candidate)
            self.assertNotIn('failure_contexts', safe)
            self.assertNotIn('workflow_sha256', safe)
            self.assertEqual(safe['failure_groups'], summary['failure_groups'])

    def test_general_prompt_preserves_partial_values_and_reconciles_scopes(self):
        prompt = prompt_for('Synthetic policy', {}, {})
        for phrase in ('Before clearing or replacing active/pending state',
                'amount while asking for date and date while asking for amount',
                'their explicit scopes before merging either', 'never silently narrow collective scope'):
            self.assertIn(phrase, prompt)


if __name__ == '__main__':
    unittest.main()
