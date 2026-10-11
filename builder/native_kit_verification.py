"""Execute exported native snippets locally; never confer live banking authority.

Response envelopes and terminal completion below are explicit local fixtures.
The harness executes actual pre/post hooks with their documented scopes, then
the graph's response mappings and result code. It neither sends HTTP nor
asserts that Freya played speech or validated an empty-URL abort.
"""
from __future__ import annotations

import copy
import json
import tempfile

from .models import FoundationError
from .native_freya import NativeHarness
from .native_write_guard import reserve_single_call


def local_http(data, *, status_code=200, status='success'):
    return {'status': status, 'status_code': status_code,
            'message': 'Successfully executed POST request',
            'header': {'Content-Type': 'application/json'}, 'data': copy.deepcopy(data)}


class OfficialNativeHarness(NativeHarness):
    """Local graph interpreter with actual write hooks and explicit lifecycle fixture."""
    def __init__(self, candidate, variables):
        super().__init__(candidate, variables)
        self.dispatch_attempts = []
        self.terminal_fixture = None
        self.lifecycle_fixture_dispatched = False

    def dispatch(self, operation):
        if operation != 'create_promise':
            return super().dispatch(operation)
        if self.current_node != 'bank_create_promise':
            raise FoundationError('LOCAL_WRITE_REQUIRES_CURRENT_BANK_NODE')
        tool = self.candidate['workflow']['nodes'][self.current_node]['pre_actions'][0]
        properties = tool['function']['parameters']['properties']
        config = {'url': properties['url']['default'], 'method': properties['method']['default'],
                  'body': {key: copy.deepcopy(self.variables.get('_native_arg_' + key))
                           for key in properties['body']['properties']}}
        exec(tool['pre_process_code'], {'vars': self.variables, 'config': config})
        self.dispatch_attempts.append({'operation': operation, **copy.deepcopy(config)})
        if config.get('url'):
            self.requests.append({'operation': operation, **copy.deepcopy(config)})
        return config

    def resume(self, operation, envelope):
        if operation != 'create_promise':
            return super().resume(operation, envelope)
        if self.current_node != 'bank_create_promise' or not isinstance(envelope, dict):
            raise FoundationError('LOCAL_WRITE_RESULT_REQUIRES_CURRENT_NODE_AND_ENVELOPE')
        node = self.candidate['workflow']['nodes'][self.current_node]
        result = copy.deepcopy(envelope)
        # The documented post-hook scope exposes result, not vars or config.
        exec(node['pre_actions'][0]['post_process_code'], {'result': result})
        for mapping in node['response_mapping']:
            observed = result
            for key in mapping['path'].split('.'):
                observed = observed[key]
            self.variables[mapping['variable']] = observed
        return self.drive(self._choose(node))

    def complete_terminal_fixture(self):
        """LOCAL model of explicit technical ending; never a native receipt."""
        node = self.candidate['workflow']['nodes'][self.current_node]
        if node.get('node_type') != 'conversation' or node.get('transitions') or 'terminal' in node:
            raise FoundationError('LOCAL_FIXTURE_REQUIRES_CLOSING_CONVERSATION')
        tools = node.get('tools', [])
        endings = [tool for tool in tools if tool.get('function', {}).get('handler') == '__function__:end_call'
                   and not tool.get('config', {}).get('alwaysRunsAt')]
        if len(endings) != 1 or node.get('on_enter') != {'type': 'llm'}:
            raise FoundationError('LOCAL_FIXTURE_REQUIRES_UNIQUE_TECHNICAL_END_TOOL')
        terminal = endings[0]['function']
        properties = terminal['parameters']['properties']
        if properties['message_mode']['default'] != 'custom':
            raise FoundationError('LOCAL_FIXTURE_REQUIRES_CUSTOM_TECHNICAL_END')
        self.terminal_fixture = {'node': self.current_node,
            'technical_function': terminal['name'], 'farewell': properties['spoken_message']['default'],
            'sequence': ['technical_end_invocation', 'terminal_farewell', 'engine_hangup_after_speech', 'call_analysis'],
            'evidence_kind': 'local_documented_order_fixture', 'native_playback_verified': False,
            'actual_end_tool_arguments_and_native_completion_verified': False}
        return copy.deepcopy(self.terminal_fixture)

    def dispatch_lifecycle_fixture(self):
        if not self.terminal_fixture or self.terminal_fixture['node'] != self.current_node:
            raise FoundationError('LOCAL_COMPLETION_FIXTURE_REQUIRED')
        # Documented node-local lifecycle selection; other end nodes' tools
        # and tools from nodes not ended are not invoked in this fixture.
        node = self.candidate['workflow']['nodes'][self.current_node]
        tools = [tool for tool in node.get('tools', [])
                 if tool.get('config', {}).get('alwaysRunsAt') == ['end_of_call']]
        if len(tools) != 1:
            raise FoundationError('LOCAL_UNIQUE_END_OF_CALL_TOOL_REQUIRED')
        tool = tools[0]
        properties = tool['function']['parameters']['properties']
        def binding(value):
            if isinstance(value, str) and value.startswith('{{') and value.endswith('}}'):
                return copy.deepcopy(self.variables.get(value[2:-2], ''))
            return value
        body = {}
        for key, spec in properties['body']['properties'].items():
            body[key] = binding(spec['default'])
        config = {'url': binding(properties['url']['default']),
                  'method': binding(properties['method']['default']), 'body': body}
        if self.lifecycle_fixture_dispatched or not self.variables.get('_native_close_armed'):
            config['url'] = ''
        # This is a local model of one engine lifecycle invocation, not a
        # provider idempotency proof or a claim that global tool PRE ran.
        if config.get('url'):
            self.lifecycle_fixture_dispatched = True
        self.dispatch_attempts.append({'operation': 'close_call', **copy.deepcopy(config)})
        if config.get('url'):
            self.requests.append({'operation': 'close_call', **copy.deepcopy(config)})
        return config


def prepare_local_pub01(candidate, variables):
    """Independent fixed PUB-01 input fixture, not extracted model output."""
    h = OfficialNativeHarness(candidate, variables)
    h.start()
    h.receive({'kind': 'person_confirm', 'explicit_yes': True})
    for field, answer in [('date_of_birth', '1988-03-14'), ('phone_last4', '4471')]:
        h.receive({'kind': 'identity_answer', 'field': field, 'answer': answer})
        if not h.dispatch('verify_identity').get('url'):
            raise FoundationError('LOCAL_PUB01_IDENTITY_DISPATCH_BLOCKED')
        h.resume('verify_identity', local_http({'ok': True, 'verified': True}))
    if not h.dispatch('get_account').get('url'):
        raise FoundationError('LOCAL_PUB01_ACCOUNT_DISPATCH_BLOCKED')
    h.resume('get_account', local_http({'ok': True, 'products': [{'product_id': 'p1',
        'type': 'credit_card', 'card_last4': '4821', 'past_due': 420.0,
        'days_late': 6, 'minimum_payment': 126.0}]}))
    h.receive({'kind': 'proposal', 'product_id': 'p1', 'amount': 420.0, 'date': '2026-10-14'})
    return h


def local_variables(registry):
    variables = {'customer_id': 'local-pub01-fixture', 'first_name': 'Daniel',
        'last_name': 'Reyes', 'title': 'Mr', 'now': '2026-10-14T11:00',
        'call.call_id': '11111111-2222-4333-8444-555555555555'}
    variables.update(reserve_single_call(registry, workspace_id='local-fixture-workspace',
        customer_id=variables['customer_id']))
    return variables


def verify_local_candidate(candidate):
    """Execute the exact candidate's happy path and return inspectable local evidence."""
    with tempfile.TemporaryDirectory(prefix='native-kit-local-') as directory:
        h = prepare_local_pub01(candidate, local_variables(directory))
        h.receive({'kind': 'confirm', 'readback_token': h.state['readback_token'], 'explicit_yes': True})
        request = h.dispatch('create_promise')
        if not request.get('url'):
            raise FoundationError('LOCAL_PUB01_WRITE_DISPATCH_BLOCKED')
        h.resume('create_promise', local_http({'ok': True}))
        h.complete_terminal_fixture()
        close = h.dispatch_lifecycle_fixture()
        if not close.get('url'):
            raise FoundationError('LOCAL_PUB01_CLOSE_DISPATCH_BLOCKED')
        expected_body = {'customer_id': h.variables['customer_id'], 'product_id': 'p1',
                         'amount': 420.0, 'date': '2026-10-14'}
        operations = [item['operation'] for item in h.requests]
        checks = {
            'two_factors_before_account': [x['body']['field'] for x in h.requests[:2]] == ['date_of_birth', 'phone_last4'],
            'exact_tool_order': operations == ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call'],
            'exact_promise_payload': request['body'] == expected_body,
            'exact_close_payload': close['body'] == {'customer_id': h.variables['customer_id'], 'outcome': 'promise'},
            'bank_confirmed_before_success': h.state['phase'] == 'promise_confirmed' and any(x['kind'] == 'promise_success' and x['bank_ok'] is True for x in h.state['events']),
            'farewell_then_lifecycle_local_fixture': bool(h.terminal_fixture),
            'single_create_and_close': operations.count('create_promise') == operations.count('close_call') == 1,
            'safe_code_budget': all(x['serialized_input_bytes'] <= 114688 for x in h.code_measurements),
            'no_native_authority_inferred': candidate['readiness']['bank_writes_enabled'] is False and candidate['readiness']['publish_allowed'] is False,
        }
        if not all(checks.values()):
            raise FoundationError('LOCAL_PUB01_CHECKS_FAILED: ' + ','.join(k for k, value in checks.items() if not value))
        return {'verdict': 'LOCAL_EXPORTED_GRAPH_PUB01_FIXTURE_PASS', 'passed': all(checks.values()), 'checks': checks,
            'requests': h.requests, 'tts_events': h.tts_events, 'traversal': h.traversal,
            'semantic_events': h.state['events'], 'terminal_fixture': h.terminal_fixture,
            'code_measurements': h.code_measurements, 'network_calls': 0,
            'native_playback_verified': False, 'official_pub01_pass': False,
            'native_atomic_persistence_claim': False, 'provider_idempotency_claim': False}
