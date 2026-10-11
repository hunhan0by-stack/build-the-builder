"""Synthetic offline fidelity checks; never executes a generated candidate."""
import copy
import time
import unittest

from orchestration.generated_behavior import _Graph
from tests_orchestration import test_generated_behavior as helpers


class ConversationPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.harness = helpers.GeneratedBehaviorTests(
            'test_real_code_and_hooks_prepare_exact_changed_request_only_after_consent')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.workflow = copy.deepcopy(self.harness.workflow)
        self.close_request = {'operation': 'close_call', 'body': {
            'customer_id': 'offline-fixture', 'outcome': 'promise'}}
        close = copy.deepcopy(self.harness.contract.freya_templates()['tools']['close_call'])
        close['pre_process_code'] = (
            "config['body']={'customer_id':vars['customer_id'],'outcome':'promise'}\n")
        self.workflow['nodes']['write']['transitions'] = [helpers.always('farewell')]
        self.workflow['nodes']['farewell'] = {
            'node_type': 'conversation', 'passthrough': True,
            'on_enter_first_entry_only': True,
            'on_enter': {'type': 'tts', 'text': 'Fresh farewell for {{amount}} on {{payment_date}}.'},
            'passthrough_default_transition': 'close_call',
            'transitions': [helpers.always('close_call')]}
        self.workflow['nodes']['close_call'] = {
            'node_type': 'api_request', 'passthrough': True,
            'pre_actions': [close], 'transitions': [helpers.always('end')]}
        terminal = self.workflow['nodes']['end']['terminal']['function']['parameters']['properties']
        terminal['message_mode']['default'] = 'none'
        terminal.pop('spoken_message')
        self.content = self.harness.content()
        self.case = self.content['cases'][0]
        self.case['responses'].append({'operation': 'close_call', 'result': {
            'status': 'success', 'status_code': 200, 'data': {'closed': True}, 'header': {}}})
        self.case['steps'][1]['expect'] = {
            'requests': [self.harness.request, self.close_request],
            'speech_contains': ['Fresh farewell for 123.75 on 2026-11-14.'],
            'ended': True}

    def test_farewell_tts_closes_and_ends_in_same_user_turn_in_isolated_worker(self):
        report = self.harness.verify(self.workflow, self.harness.fixture(self.content))
        self.assertTrue(report['passed'], report)
        result = report['cases'][0]
        self.assertEqual(result['steps_completed'], 2)
        self.assertEqual(result['requests'], [self.harness.request, self.close_request])
        self.assertTrue(result['ended'])
        self.assertEqual(result['speech'], [
            'Please confirm 123.75 on 2026-11-14',
            'Fresh farewell for 123.75 on 2026-11-14.'])
        self.assertFalse(report['live_success_verified'])
        self.assertFalse(report['semantic_correctness_verified'])

    def test_entry_speech_precedes_close_request_and_end_follows_it(self):
        graph = _Graph(self.workflow, self.harness.contract, self.case, time.monotonic() + 20)
        events = []

        class RecordingSpeech(list):
            def append(self, value):
                events.append(('speech', value))
                super().append(value)

        graph.speech = RecordingSpeech()
        action = graph.action

        def observe_action(value):
            name = value['function']['name']
            if name in ('create_promise', 'close_call'):
                events.append(('request', name))
                self.assertFalse(graph.ended, 'API must execute before terminal end')
            action(value)

        graph.action = observe_action
        graph.advance()
        graph.check(self.case['steps'][0]['expect'])
        graph.turn({'decision': True})
        self.assertTrue(graph.ended, 'No additional customer turn is needed after farewell')
        events.append(('end', graph.node))
        graph.check(self.case['steps'][1]['expect'])
        self.assertEqual(events, [
            ('speech', 'Please confirm 123.75 on 2026-11-14'),
            ('request', 'create_promise'),
            ('speech', 'Fresh farewell for 123.75 on 2026-11-14.'),
            ('request', 'close_call'),
            ('end', 'end')])

    def test_non_passthrough_conversation_keeps_strict_no_extra_turn_expectation_failing(self):
        for passthrough in (False, None):
            with self.subTest(passthrough=passthrough):
                workflow = copy.deepcopy(self.workflow)
                if passthrough is None:
                    workflow['nodes']['farewell'].pop('passthrough')
                else:
                    workflow['nodes']['farewell']['passthrough'] = passthrough
                # Keep the exact positive fixture unchanged: an ordinary
                # conversation must pause, so its missing close request fails.
                report = self.harness.verify(workflow, self.harness.fixture(self.content))
                self.assertFalse(report['passed'])
                result = report['cases'][0]
                self.assertEqual(result['diagnostic'], 'BEHAVIOR_REQUEST_EXPECTATION_MISMATCH')
                self.assertEqual(result['requests'], [self.harness.request])
                self.assertFalse(result['ended'])
                self.assertIn('Fresh farewell for 123.75 on 2026-11-14.', result['speech'])


if __name__ == '__main__':
    unittest.main()
