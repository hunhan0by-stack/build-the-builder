"""Bounded, offline checks of generated native graphs against independent fixtures.

This is a fixture runner for a deliberately small graph subset, not a Freya
emulator, speech-extraction test, universal Python sandbox or policy oracle.
Generated actions run in a separate, deadline-bound Python process with closed
builtins and module facades. No HTTP request is ever dispatched.
"""
from __future__ import annotations

import ast
import copy
import datetime
import hashlib
import functools
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

MAX_BYTES = 2_000_000
MAX_CASES = 120
MAX_STEPS = 120
MAX_ACTION_LINES = 100_000
KIND = 'generated-behavior-fixture-1'
REPORT_KIND = 'generated-behavior-report-1'
_MISSING = object()


class BehaviorError(Exception):
    """Only fixed diagnostic codes cross the worker boundary."""


def _raw(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise BehaviorError('BEHAVIOR_JSON_VALUE_INVALID') from None


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(encoded):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BehaviorError('BEHAVIOR_JSON_DUPLICATE_KEY')
            result[key] = value
        return result
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise BehaviorError('BEHAVIOR_JSON_NONFINITE')
        return number
    try:
        return json.loads(encoded, object_pairs_hook=pairs,
                          parse_float=finite_float,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              BehaviorError('BEHAVIOR_JSON_NONFINITE')))
    except (ValueError, TypeError, RecursionError):
        raise BehaviorError('BEHAVIOR_JSON_INVALID') from None


def _keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise BehaviorError('BEHAVIOR_FIXTURE_SHAPE_INVALID')


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _requests(value):
    if type(value) is not list or len(value) > MAX_STEPS:
        raise BehaviorError('BEHAVIOR_FIXTURE_REQUESTS_INVALID')
    for row in value:
        _keys(row, ('operation', 'body'))
        if type(row['operation']) is not str or not row['operation'] or type(row['body']) is not dict:
            raise BehaviorError('BEHAVIOR_FIXTURE_REQUESTS_INVALID')


def _validate_fixture(fixture):
    _keys(fixture, ('kind', 'brief_sha256', 'tool_api_sha256', 'cases'))
    if fixture['kind'] != KIND or not _hash(fixture['brief_sha256']) or not _hash(fixture['tool_api_sha256']):
        raise BehaviorError('BEHAVIOR_FIXTURE_SOURCE_INVALID')
    if type(fixture['cases']) is not list or not 1 <= len(fixture['cases']) <= MAX_CASES:
        raise BehaviorError('BEHAVIOR_FIXTURE_CASES_INVALID')
    names = set()
    for case in fixture['cases']:
        _keys(case, ('name', 'call_variables', 'responses', 'steps'))
        if type(case['name']) is not str or not case['name'] or len(case['name']) > 120 or case['name'] in names:
            raise BehaviorError('BEHAVIOR_FIXTURE_CASE_NAME_INVALID')
        names.add(case['name'])
        if type(case['call_variables']) is not dict or any(type(k) is not str or not k or k.startswith('_') for k in case['call_variables']):
            raise BehaviorError('BEHAVIOR_FIXTURE_VARIABLES_INVALID')
        if type(case['responses']) is not list or len(case['responses']) > MAX_STEPS:
            raise BehaviorError('BEHAVIOR_FIXTURE_RESPONSES_INVALID')
        for response in case['responses']:
            _keys(response, ('operation', 'result'))
            if type(response['operation']) is not str or not response['operation']:
                raise BehaviorError('BEHAVIOR_FIXTURE_RESPONSES_INVALID')
        if type(case['steps']) is not list or not 1 <= len(case['steps']) <= MAX_STEPS:
            raise BehaviorError('BEHAVIOR_FIXTURE_STEPS_INVALID')
        for step in case['steps']:
            _keys(step, ('extractions', 'expect'))
            if type(step['extractions']) is not dict or any(type(k) is not str or not k or k.startswith('_') for k in step['extractions']):
                raise BehaviorError('BEHAVIOR_FIXTURE_EXTRACTIONS_INVALID')
            _keys(step['expect'], ('requests',), ('speech_contains', 'ended'))
            _requests(step['expect']['requests'])
            speech = step['expect'].get('speech_contains', [])
            if type(speech) is not list or len(speech) > MAX_STEPS or any(type(s) is not str or len(s) > 2000 for s in speech):
                raise BehaviorError('BEHAVIOR_FIXTURE_SPEECH_INVALID')
            if 'ended' in step['expect'] and type(step['expect']['ended']) is not bool:
                raise BehaviorError('BEHAVIOR_FIXTURE_ENDED_INVALID')
        if case['steps'][0]['extractions'] != {}:
            raise BehaviorError('BEHAVIOR_FIXTURE_INITIAL_STEP_REQUIRED')
    if len(_raw(fixture)) > MAX_BYTES:
        raise BehaviorError('BEHAVIOR_FIXTURE_TOO_LARGE')
    return fixture


def read_fixture(path, brief_sha256, tool_api_sha256):
    """Read a closed-schema fixture pinned to this exact source pair.

    The reserved fingerprints make mutation after this read detectable. The
    caller should also archive the original fixture bytes in its run manifest.
    """
    path = Path(path)
    if not _hash(brief_sha256) or not _hash(tool_api_sha256):
        raise BehaviorError('BEHAVIOR_EXPECTED_SOURCE_INVALID')
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise BehaviorError('BEHAVIOR_FIXTURE_FILE_INVALID')
    encoded = path.read_bytes()
    fixture = _validate_fixture(_json(encoded))
    if fixture['brief_sha256'] != brief_sha256 or fixture['tool_api_sha256'] != tool_api_sha256:
        raise BehaviorError('BEHAVIOR_FIXTURE_SOURCE_MISMATCH')
    return dict(fixture, _fixture_sha256=_sha(encoded),
                _fixture_content_sha256=_sha(_raw(fixture)))


def _bound_fixture(fixture, contract):
    if type(fixture) is not dict or not _hash(fixture.get('_fixture_sha256')) or not _hash(fixture.get('_fixture_content_sha256')):
        raise BehaviorError('BEHAVIOR_FIXTURE_READ_REQUIRED')
    content = {k: v for k, v in fixture.items() if k not in ('_fixture_sha256', '_fixture_content_sha256')}
    _validate_fixture(content)
    if _sha(_raw(content)) != fixture['_fixture_content_sha256']:
        raise BehaviorError('BEHAVIOR_FIXTURE_CHANGED_AFTER_READ')
    if content['tool_api_sha256'] != contract.source_hash:
        raise BehaviorError('BEHAVIOR_FIXTURE_SOURCE_MISMATCH')
    return content


def _base_report(workflow, fixture):
    return {'kind': REPORT_KIND, 'status': 'LOCAL_BEHAVIOR_FAILED', 'passed': False,
            'cases': [], 'fixture_sha256': fixture.get('_fixture_sha256') if type(fixture) is dict else None,
            'workflow_sha256': _sha(_raw(workflow)), 'semantic_correctness_verified': False,
            'live_success_verified': False,
            'scope': 'Independent fixture checks of supported local graph actions only; no speech extraction, HTTP, platform delivery or universal sandbox guarantee.'}


def summarize_behavior_observations(report, workflow=None):
    """Emit counts and bounded actual graph/input semantics, never expectations."""
    cases = report.get('cases', []) if type(report) is dict else []
    cases = cases if type(cases) is list else []
    failures = [row for row in cases if type(row) is dict and row.get('passed') is False]
    groups = {}
    for row in failures:
        steps = row.get('steps_completed')
        steps = steps if type(steps) is int and 0 <= steps <= MAX_STEPS else None
        requests, speech, ended = row.get('requests'), row.get('speech'), row.get('ended')
        observed = (steps, len(requests) if type(requests) is list else None,
            len(speech) if type(speech) is list else None,
            ended if type(ended) is bool else None)
        groups[observed] = groups.get(observed, 0) + 1
    from orchestration.repair_observations import summarize_contexts
    # Node ordinals have meaning only within the exact graph executed by the
    # worker. An absent/mismatched report pin cannot authorize source pointers.
    pinned = False
    if type(report) is dict and type(workflow) is dict and _hash(report.get('workflow_sha256')):
        try:
            pinned = report['workflow_sha256'] == _sha(_raw(workflow))
        except BehaviorError:
            pass
    contexts = summarize_contexts(failures, workflow) if pinned else []
    return {'kind': 'observed-behavior-summary-1', 'failed_case_count': len(failures),
        **({'failure_contexts': contexts, 'workflow_sha256': report['workflow_sha256']}
           if contexts else {}),
        'failure_groups': [dict(zip(('completed_steps', 'observed_request_count',
            'observed_speech_count', 'ended'), values), occurrences=count)
            for values, count in sorted(groups.items(), key=lambda item: _raw(list(item[0])))]}


def verify_behavior(workflow, contract, fixture, directory, budget):
    """Return pass/fail observations; never publish or dispatch bank requests."""
    try:
        report = _base_report(workflow, fixture)
    except BehaviorError as error:
        return {'kind': REPORT_KIND, 'status': 'LOCAL_BEHAVIOR_FAILED', 'passed': False,
                'cases': [], 'fixture_sha256': None, 'workflow_sha256': None,
                'diagnostic': str(error), 'semantic_correctness_verified': False,
                'live_success_verified': False}
    try:
        content = _bound_fixture(fixture, contract)
        timeout = min(60.0, float(budget.remaining()))
        if timeout < 0.01:
            raise BehaviorError('BEHAVIOR_DEADLINE_EXCEEDED')
        payload = _raw({'workflow': workflow, 'contract': contract.serializable(),
                        'fixture': content, 'deadline_seconds': timeout})
        if len(payload) > MAX_BYTES:
            raise BehaviorError('BEHAVIOR_INPUT_TOO_LARGE')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='behavior-worker-', dir=directory) as temporary:
            input_path, output_path = Path(temporary) / 'input.json', Path(temporary) / 'output.json'
            input_path.write_bytes(payload)
            try:
                completed = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                    '--worker', str(input_path), str(output_path)], cwd=temporary,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout, check=False, env={})
            except subprocess.TimeoutExpired:
                raise BehaviorError('BEHAVIOR_WORKER_TIMEOUT') from None
            budget.remaining()
            if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size > MAX_BYTES:
                raise BehaviorError('BEHAVIOR_WORKER_RESULT_INVALID')
            result = _json(output_path.read_bytes())
            _keys(result, ('passed', 'cases'), ('diagnostic',))
            if type(result['passed']) is not bool or type(result['cases']) is not list:
                raise BehaviorError('BEHAVIOR_WORKER_RESULT_INVALID')
            report.update(result)
            report['status'] = 'LOCAL_BEHAVIOR_PASSED' if report['passed'] else 'LOCAL_BEHAVIOR_FAILED'
    except BehaviorError as error:
        report['diagnostic'] = str(error)
    except Exception:
        report['diagnostic'] = 'BEHAVIOR_LOCAL_OPERATION_FAILED'
    return report


class _InstructionBudget(BaseException):
    pass


def _modules():
    return {
        'json': SimpleNamespace(loads=json.loads, dumps=json.dumps, JSONDecodeError=json.JSONDecodeError),
        're': SimpleNamespace(fullmatch=re.fullmatch, match=re.match, search=re.search,
                              sub=re.sub, findall=re.findall, split=re.split, escape=re.escape,
                              IGNORECASE=re.IGNORECASE, MULTILINE=re.MULTILINE, DOTALL=re.DOTALL),
        'datetime': SimpleNamespace(date=datetime.date, datetime=datetime.datetime,
                                    timedelta=datetime.timedelta, timezone=datetime.timezone)}


@functools.lru_cache(maxsize=8)
def _checked_code(code):
    if type(code) is not str or len(code.encode('utf-8')) > 150_000:
        raise BehaviorError('BEHAVIOR_CODE_INVALID')
    try:
        tree = ast.parse(code)
    except SyntaxError:
        raise BehaviorError('BEHAVIOR_CODE_SYNTAX_INVALID') from None
    modules = _modules()
    denied = {'eval', 'exec', 'compile', 'open', 'input', 'getattr', 'setattr', 'delattr',
              'globals', 'locals', 'vars', 'breakpoint', 'help', 'memoryview', 'super'}
    for element in ast.walk(tree):
        if isinstance(element, ast.Import) and any(alias.name not in modules for alias in element.names):
            raise BehaviorError('BEHAVIOR_CODE_IMPORT_REFUSED')
        if isinstance(element, ast.ImportFrom):
            if element.level or element.module not in modules or any(alias.name.startswith('_') or alias.name not in vars(modules[element.module]) for alias in element.names):
                raise BehaviorError('BEHAVIOR_CODE_IMPORT_REFUSED')
        if isinstance(element, ast.Attribute) and element.attr.startswith('_'):
            raise BehaviorError('BEHAVIOR_CODE_PRIVATE_ACCESS_REFUSED')
        if isinstance(element, ast.Name) and (element.id.startswith('__') or element.id in denied):
            # vars is the documented workflow input, not the builtin vars().
            if element.id != 'vars':
                raise BehaviorError('BEHAVIOR_CODE_HOST_ACCESS_REFUSED')
        if isinstance(element, ast.Call) and isinstance(element.func, ast.Name) and element.func.id in denied | {'__import__'}:
            raise BehaviorError('BEHAVIOR_CODE_HOST_ACCESS_REFUSED')
    try:
        return compile(tree, '<generated-behavior>', 'exec')
    except BaseException:
        raise BehaviorError('BEHAVIOR_ACTION_EXECUTION_FAILED') from None


def _execute(code, variables, deadline, **extra):
    # Only immutable checked bytecode is reused. State and limits are fresh.
    if type(code) is not str:
        raise BehaviorError('BEHAVIOR_CODE_INVALID')
    compiled = _checked_code(code)
    modules = _modules()
    def importer(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in modules:
            raise BehaviorError('BEHAVIOR_CODE_IMPORT_REFUSED')
        return modules[name]
    def bounded_range(*args):
        result = range(*args)
        if len(result) > 10_000:
            raise BehaviorError('BEHAVIOR_CODE_RANGE_TOO_LARGE')
        return result
    def value_type(value):
        # Preserve type(x) checks without exposing type(name,bases,namespace)
        # as a way to synthesize classes with magic methods from string keys.
        return type(value)
    safe = dict(__import__=importer, dict=dict, list=list, tuple=tuple, set=set,
                str=str, int=int, float=float, bool=bool, type=value_type, len=len,
                min=min, max=max, all=all, any=any, next=next, range=bounded_range,
                enumerate=enumerate, zip=zip, sorted=sorted, sum=sum, abs=abs,
                isinstance=isinstance, round=round, Exception=Exception,
                ValueError=ValueError, TypeError=TypeError, KeyError=KeyError)
    namespace = dict(__builtins__=safe, vars=variables, **extra)
    remaining = MAX_ACTION_LINES
    def trace(frame, event, arg):
        nonlocal remaining
        if frame.f_code.co_filename == '<generated-behavior>' and event == 'line':
            remaining -= 1
            if remaining < 0 or time.monotonic() >= deadline:
                raise _InstructionBudget()
        return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        exec(compiled, namespace)
    except _InstructionBudget:
        raise BehaviorError('BEHAVIOR_ACTION_BUDGET_EXCEEDED') from None
    except BehaviorError:
        raise
    except BaseException:
        raise BehaviorError('BEHAVIOR_ACTION_EXECUTION_FAILED') from None
    finally:
        sys.settrace(previous)
    if len(_raw(variables)) > 200_000:
        raise BehaviorError('BEHAVIOR_VARIABLES_TOO_LARGE')


class _Graph:
    def __init__(self, workflow, contract, case, deadline):
        self.workflow, self.contract, self.deadline = workflow, contract, deadline
        self.variables = copy.deepcopy(workflow.get('test_variables', {}))
        if type(self.variables) is not dict:
            raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
        self.variables.update(copy.deepcopy(case['call_variables']))
        self.responses = copy.deepcopy(case['responses'])
        self.requests, self.speech = [], []
        self.checked_speech = 0
        self.entered, self.ended = set(), False
        if type(workflow) is not dict or workflow.get('global_nodes') or workflow.get('tools'):
            raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
        self.nodes = workflow.get('nodes')
        if type(self.nodes) is not dict or not 1 <= len(self.nodes) <= 180:
            raise BehaviorError('BEHAVIOR_GRAPH_INVALID')
        # The isolated worker receives canonical JSON with sorted object keys.
        self.node_indices = {name: index for index, name in enumerate(sorted(self.nodes))}
        self.causal_observation = {}
        self.node = workflow.get('initial_node')
        if self.node not in self.nodes:
            raise BehaviorError('BEHAVIOR_GRAPH_INVALID')
        for node in self.nodes.values():
            if type(node) is not dict or node.get('node_type') not in ('code', 'api_request', 'conversation', 'end_call'):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            if node.get('tools') or node.get('post_actions') or node.get('return_to_previous_on_interrupt') or node.get('max_turn_transition') or node.get('loop_condition'):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            if node.get('on_enter', {}).get('type', 'silent') not in ('silent', 'tts'):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            if node['node_type'] == 'conversation' and node.get('pre_actions'):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            if node['node_type'] in ('code', 'api_request') and node.get('passthrough') is not True:
                raise BehaviorError('BEHAVIOR_PASSTHROUGH_REQUIRED')
            if node['node_type'] == 'api_request' and len(node.get('pre_actions', [])) != 1:
                raise BehaviorError('BEHAVIOR_ACTION_CHAIN_UNSUPPORTED')
            if node['node_type'] == 'code' and node.get('response_mapping'):
                raise BehaviorError('BEHAVIOR_RESPONSE_MAPPING_UNSUPPORTED')
            transitions = node.get('transitions', [])
            if type(transitions) is not list or any(type(t) is not dict or t.get('is_tool_call') for t in transitions):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            targets = [t.get('target_node') for t in transitions]
            if len(set(targets)) != len(targets):
                # The platform merges duplicate targets before routing. We do
                # not silently emulate a different list-priority semantics.
                raise BehaviorError('BEHAVIOR_TRANSITION_MERGE_UNSUPPORTED')
            if node['node_type'] == 'end_call' and (node.get('pre_actions') or transitions):
                raise BehaviorError('BEHAVIOR_GRAPH_UNSUPPORTED')
            if node['node_type'] == 'end_call' and type(node.get('terminal')) is not dict:
                raise BehaviorError('BEHAVIOR_TERMINAL_UNSUPPORTED')

    def begin_observation(self, index, extractions):
        from orchestration.repair_observations import summarize_input
        self.causal_observation = {'step_index': index,
            'question_node': self.node_indices[self.node]
                if self.nodes[self.node]['node_type'] == 'conversation' else None,
            'input': summarize_input(extractions), 'trace': [], 'trace_truncated': False}

    def observe(self, event, **values):
        from orchestration.repair_observations import MAX_TRACE
        trace = self.causal_observation.setdefault('trace', [])
        trace.append(dict(event=event, node=self.node_indices[self.node], **values))
        if len(trace) > MAX_TRACE:
            del trace[0]
            self.causal_observation['trace_truncated'] = True

    def resolve(self, value, request=False):
        if type(value) is str:
            complete = re.fullmatch(r'\{\{([\w.:-]+)\}\}', value)
            if complete:
                if complete[1] not in self.variables:
                    return _MISSING if request else value
                return copy.deepcopy(self.variables[complete[1]])
            return re.sub(r'\{\{([\w.:-]+)\}\}',
                          lambda m: str(self.variables[m[1]]) if m[1] in self.variables else m[0], value)
        return copy.deepcopy(value)

    def defaults(self, schema):
        if type(schema) is not dict:
            raise BehaviorError('BEHAVIOR_ACTION_SCHEMA_INVALID')
        if 'default' in schema:
            return self.resolve(schema['default'], request=True)
        if schema.get('type') == 'object':
            result = {}
            for key, child in schema.get('properties', {}).items():
                value = self.defaults(child)
                if value is not _MISSING:
                    result[key] = value
            return result
        return _MISSING

    def condition(self, condition):
        if type(condition) is not dict:
            raise BehaviorError('BEHAVIOR_CONDITION_UNSUPPORTED')
        if 'conditions' in condition:
            if condition.get('logic') not in ('and', 'or') or type(condition['conditions']) is not list or not condition['conditions']:
                raise BehaviorError('BEHAVIOR_CONDITION_UNSUPPORTED')
            values = [self.condition(c) for c in condition['conditions']]
            return all(values) if condition['logic'] == 'and' else any(values)
        operator = condition.get('operator')
        if operator == 'always':
            return True
        if operator == 'equals' and condition.get('mode', 'exact') == 'exact':
            value = self.variables.get(condition.get('variable'))
            expected = condition.get('operand')
            return str(value) == str(expected)
        raise BehaviorError('BEHAVIOR_CONDITION_UNSUPPORTED')

    def transition(self):
        transitions = self.nodes[self.node].get('transitions', [])
        if type(transitions) is not list:
            raise BehaviorError('BEHAVIOR_GRAPH_INVALID')
        for index, transition in enumerate(transitions):
            matched = self.condition(transition.get('condition'))
            self.observe('transition', transition_index=index, matched=matched)
            if matched:
                target = transition.get('target_node')
                if target not in self.nodes:
                    raise BehaviorError('BEHAVIOR_TRANSITION_UNSUPPORTED')
                self.node = target
                return
        raise BehaviorError('BEHAVIOR_TRANSITION_NOT_FOUND')

    def action(self, action):
        if type(action) is not dict or type(action.get('function')) is not dict:
            raise BehaviorError('BEHAVIOR_ACTION_UNSUPPORTED')
        fn = action['function']
        config = self.defaults(fn.get('parameters'))
        handler = fn.get('handler')
        if handler == '__function__:code_exec':
            if action.get('pre_process_code') or action.get('post_process_code'):
                raise BehaviorError('BEHAVIOR_ACTION_UNSUPPORTED')
            _execute(config.get('code'), self.variables, self.deadline)
            return
        if handler != '__function__:api_call' or action.get('config', {}).get('maxRetries', 0) != 0 or action.get('config', {}).get('interruptible') is False:
            raise BehaviorError('BEHAVIOR_ACTION_UNSUPPORTED')
        if action.get('pre_process_code'):
            _execute(action['pre_process_code'], self.variables, self.deadline, config=config)
        operation = fn.get('name')
        # Observe the body actually prepared by the hook, including invalid
        # bodies. An internal guard variable is never interpreted as HTTP abort.
        self.requests.append({'operation': operation, 'body': copy.deepcopy(config.get('body'))})
        expected = self.contract.operations.get(operation)
        if expected is None or config.get('method') != expected.method or config.get('url') != expected.url:
            raise BehaviorError('BEHAVIOR_REQUEST_ENDPOINT_INVALID')
        try:
            self.contract.validate_request(operation, config.get('body'))
        except Exception:
            raise BehaviorError('BEHAVIOR_REQUEST_BODY_INVALID') from None
        if len(self.requests) > MAX_STEPS or not self.responses or self.responses[0]['operation'] != operation:
            raise BehaviorError('BEHAVIOR_RESPONSE_SEQUENCE_MISMATCH')
        result = self.responses.pop(0)['result']
        if action.get('post_process_code'):
            _execute(action['post_process_code'], self.variables, self.deadline, result=result)
        # Native post hooks mutate result in place before response mapping.
        for mapping in self.nodes[self.node].get('response_mapping', []):
            if type(mapping) is not dict or type(mapping.get('path')) is not str or type(mapping.get('variable')) is not str:
                raise BehaviorError('BEHAVIOR_RESPONSE_MAPPING_UNSUPPORTED')
            current = result
            for part in mapping['path'].split('.'):
                current = current.get(part) if type(current) is dict else None
            self.variables[mapping['variable']] = copy.deepcopy(current)

    def advance(self):
        for unused in range(MAX_STEPS):
            if time.monotonic() >= self.deadline:
                raise BehaviorError('BEHAVIOR_DEADLINE_EXCEEDED')
            node = self.nodes[self.node]
            self.observe('enter')
            entry = node.get('on_enter', {})
            if node['node_type'] != 'end_call' and entry.get('type') == 'tts' and (not node.get('on_enter_first_entry_only') or self.node not in self.entered):
                text = self.resolve(entry.get('text'))
                if type(text) is not str or len(text) > 20_000:
                    raise BehaviorError('BEHAVIOR_SPEECH_INVALID')
                self.speech.append(text)
                if len(self.speech) > MAX_STEPS:
                    raise BehaviorError('BEHAVIOR_SPEECH_TOO_LARGE')
            self.entered.add(self.node)
            for action in node.get('pre_actions', []):
                self.action(action)
            if node['node_type'] == 'end_call':
                terminal = node.get('terminal')
                if terminal:
                    fn = terminal.get('function', {})
                    if fn.get('handler') != '__function__:end_call':
                        raise BehaviorError('BEHAVIOR_TERMINAL_UNSUPPORTED')
                    params = self.defaults(fn.get('parameters'))
                    if params.get('interruptible') or params.get('message_mode') not in ('none', 'custom'):
                        raise BehaviorError('BEHAVIOR_TERMINAL_UNSUPPORTED')
                    if params['message_mode'] == 'custom':
                        text = params.get('spoken_message')
                        if type(text) is not str or len(text) > 20_000:
                            raise BehaviorError('BEHAVIOR_SPEECH_INVALID')
                        self.speech.append(text)
                self.ended = True
                return
            if node['node_type'] == 'conversation' and node.get('passthrough') is not True:
                return
            self.transition()
        raise BehaviorError('BEHAVIOR_GRAPH_STEP_BUDGET_EXCEEDED')

    def turn(self, extractions):
        if self.ended or self.nodes[self.node]['node_type'] != 'conversation':
            raise BehaviorError('BEHAVIOR_NOT_LISTENING')
        schema = self.nodes[self.node].get('extraction_schema', [])
        if type(schema) is not list:
            raise BehaviorError('BEHAVIOR_EXTRACTION_UNSUPPORTED')
        fields = {row.get('name'): row for row in schema if type(row) is dict}
        if len(fields) != len(schema) or None in fields or set(extractions) - fields.keys():
            raise BehaviorError('BEHAVIOR_EXTRACTION_UNSUPPORTED')
        for name, field in fields.items():
            # Call variables persist across nodes and turns. Omitted extraction
            # never clears stale consent; only generated code can consume it.
            if name in extractions:
                value = extractions[name]
                kind = field.get('type')
                checks = {'string': lambda: type(value) is str, 'date': lambda: type(value) is str,
                          'datetime': lambda: type(value) is str, 'name': lambda: type(value) is str,
                          'phone_number': lambda: type(value) is str, 'float': lambda: type(value) in (int, float),
                          'integer': lambda: type(value) is int, 'boolean': lambda: type(value) is bool}
                if kind not in checks or not checks[kind]():
                    raise BehaviorError('BEHAVIOR_EXTRACTION_TYPE_INVALID')
                if field.get('enum_values') and value not in field['enum_values']:
                    raise BehaviorError('BEHAVIOR_EXTRACTION_VALUE_INVALID')
                if field.get('validation_pattern') and not re.search(field['validation_pattern'], str(value)):
                    raise BehaviorError('BEHAVIOR_EXTRACTION_VALUE_INVALID')
                self.variables[name] = copy.deepcopy(value)
        self.transition()
        self.advance()

    def check(self, expect):
        # A fresh readback must be observed in this step. An earlier matching
        # sentence cannot satisfy a later confirmation/correction checkpoint.
        new_speech = self.speech[self.checked_speech:]
        self.checked_speech = len(self.speech)
        # JSON canonical bytes compare numeric/bool types too (True != 1).
        if _raw(self.requests) != _raw(expect['requests']):
            raise BehaviorError('BEHAVIOR_REQUEST_EXPECTATION_MISMATCH')
        if 'ended' in expect and self.ended is not expect['ended']:
            raise BehaviorError('BEHAVIOR_END_EXPECTATION_MISMATCH')
        speech = '\n'.join(new_speech)
        if any(text not in speech for text in expect.get('speech_contains', [])):
            raise BehaviorError('BEHAVIOR_SPEECH_EXPECTATION_MISMATCH')


def _run_suite(payload):
    _checked_code.cache_clear()
    # Import only trusted host contract code, outside the generated namespace.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from builder.api_contract import BankContract, Operation
    contract = object.__new__(BankContract)
    contract.source_hash = payload['contract']['source_sha256']
    contract.operations = {name: Operation(**value) for name, value in payload['contract']['operations'].items()}
    fixture = _validate_fixture(payload['fixture'])
    deadline = time.monotonic() + min(60.0, float(payload['deadline_seconds']))
    cases = []
    for case in fixture['cases']:
        row = {'name': case['name'], 'passed': False, 'steps_completed': 0}
        graph = None
        try:
            graph = _Graph(payload['workflow'], contract, case, deadline)
            for index, step in enumerate(case['steps']):
                graph.begin_observation(index, step['extractions'])
                if index == 0:
                    graph.advance()
                else:
                    graph.turn(step['extractions'])
                graph.check(step['expect'])
                row['steps_completed'] += 1
            if graph.responses:
                raise BehaviorError('BEHAVIOR_UNUSED_RESPONSES')
            row['passed'] = True
        except BehaviorError as error:
            row['diagnostic'] = str(error)
        except BaseException:
            row['diagnostic'] = 'BEHAVIOR_CASE_EXECUTION_FAILED'
        if graph:
            row.update(requests=graph.requests, speech=graph.speech, ended=graph.ended)
            row['causal_observation'] = dict(graph.causal_observation,
                stopped_node=graph.node_indices.get(graph.node))
        cases.append(row)
        if time.monotonic() >= deadline:
            return {'passed': False, 'cases': cases, 'diagnostic': 'BEHAVIOR_DEADLINE_EXCEEDED'}
        if len(_raw(cases)) > MAX_BYTES // 2:
            return {'passed': False, 'cases': [], 'diagnostic': 'BEHAVIOR_OBSERVATIONS_TOO_LARGE'}
    return {'passed': all(row['passed'] for row in cases), 'cases': cases}


def _worker(input_path, output_path):
    try:
        path = Path(input_path)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise BehaviorError('BEHAVIOR_INPUT_TOO_LARGE')
        result = _run_suite(_json(path.read_bytes()))
    except BehaviorError as error:
        result = {'passed': False, 'cases': [], 'diagnostic': str(error)}
    except BaseException:
        result = {'passed': False, 'cases': [], 'diagnostic': 'BEHAVIOR_WORKER_FAILED'}
    encoded = _raw(result)
    if len(encoded) > MAX_BYTES:
        encoded = _raw({'passed': False, 'cases': [], 'diagnostic': 'BEHAVIOR_WORKER_OUTPUT_TOO_LARGE'})
    Path(output_path).write_bytes(encoded)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        _worker(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(2)
