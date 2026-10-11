"""Offline runner for the inspected native graph subset; never dispatch HTTP.

Only typed extraction fixtures are supplied. This is not an LLM, Freya emulator,
or security sandbox for arbitrary adversarial Python. AST restrictions, a small
builtin/import allowlist and per-action instruction bounds protect this review.
"""
import ast
import copy
import datetime
import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'artifacts/final-builder/model-current-medium-changed-v13-20261011/agent.workflow.json'


def candidate_path(fallback=ORIGINAL):
    """Explicit override, optional packaged fixture, then historical local path.

    Missing data is an honest skip, never a measured behavior success. A supplied
    but missing override is an error so a bad path cannot silently select history.
    """
    explicit = os.environ.get('GENERATED_DIALOGUE_CANDIDATE')
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError('Explicit generated dialogue candidate is unavailable')
        return path
    packaged = ROOT / 'tests_orchestration/inputs/candidate-regression/agent.workflow.json'
    for path in (packaged, fallback):
        if path.is_file():
            return path
    raise unittest.SkipTest('NOT_MEASURED: supply GENERATED_DIALOGUE_CANDIDATE or package an explicit regression graph')


class StepBudgetExceeded(BaseException):
    pass


def execute(code, variables, **extra):
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(n.name not in ('json', 're', 'datetime') for n in node.names):
                raise ValueError('Offline import denied')
        if isinstance(node, ast.ImportFrom):
            raise ValueError('Offline from-import denied')
        if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            raise ValueError('Offline private attribute denied')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('Offline private name denied')
    modules = {'json': json, 're': re, 'datetime': datetime}
    def importer(name, *args, **kwargs):
        if name not in modules:
            raise ValueError('Offline import denied')
        return modules[name]
    safe = dict(__import__=importer, dict=dict, list=list, tuple=tuple, set=set,
                str=str, int=int, float=float, bool=bool, type=type, len=len,
                min=min, max=max, all=all, any=any, next=next, range=range,
                enumerate=enumerate, zip=zip, sorted=sorted, sum=sum, abs=abs,
                Exception=Exception, ValueError=ValueError, TypeError=TypeError)
    namespace = dict(__builtins__=safe, vars=variables, **extra)
    compiled = compile(tree, '<offline-generated>', 'exec')
    remaining = 100000
    def trace(frame, event, arg):
        nonlocal remaining
        if frame.f_code.co_filename == '<offline-generated>' and event == 'line':
            remaining -= 1
            if remaining < 0:
                raise StepBudgetExceeded('Generated action exceeded local instruction budget')
        return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        exec(compiled, namespace)
    finally:
        sys.settrace(previous)


def ok(**data):
    return dict(status='success', status_code=200, data=dict(ok=True, **data), header={})


def products(count=3):
    rows = [
        dict(product_id='CARD', type='credit_card', arrears_balance=400,
             required_minimum=80, days_late=10, card_last4='1234'),
        dict(product_id='LOAN', type='personal_loan', arrears_balance=350,
             scheduled_due=120, days_late=20),
        dict(product_id='CAR', type='car_loan', arrears_balance=500,
             scheduled_due=150, days_late=40),
        dict(product_id='FOURTH', type='credit_card', arrears_balance=300,
             required_minimum=60, days_late=4),
    ]
    return copy.deepcopy(rows[:count])


def event(intent='proposal', scope='current', **fields):
    return dict(intent=intent, scope=scope, **fields)


class NativeGraphHarness:
    def __init__(self, path=ORIGINAL, rows=None, responses=None):
        self.workflow = json.loads(Path(path).read_text(encoding='utf-8'))
        self.vars = dict(customer_id='offline-synthetic', current_date='2026-11-10')
        self.rows = products() if rows is None else copy.deepcopy(rows)
        self.responses = copy.deepcopy(responses or {})
        self.calls, self.speech, self.trace = [], [], []
        self.node = self.workflow['initial_node']
        self.entered = set()
        self.ended = False

    @property
    def state(self):
        return self.vars.get('cedar_state', {})

    def resolve(self, value):
        if isinstance(value, str):
            full = re.fullmatch(r'\{\{([\w.:-]+)\}\}', value)
            if full:
                return copy.deepcopy(self.vars.get(full[1]))
            return re.sub(r'\{\{([\w.:-]+)\}\}', lambda m: str(self.vars.get(m[1], '')), value)
        return copy.deepcopy(value)

    def condition(self, c):
        if 'conditions' in c:
            values = [self.condition(x) for x in c['conditions']]
            return all(values) if c['logic'] == 'and' else any(values)
        if c['operator'] == 'always':
            return True
        if c['operator'] == 'equals':
            return self.vars.get(c['variable']) == c['operand']
        raise ValueError('Unimplemented local transition ' + c['operator'])

    def schema_defaults(self, schema):
        if 'default' in schema:
            return self.resolve(schema['default'])
        if schema.get('type') == 'object':
            return {k: self.schema_defaults(v) for k, v in schema.get('properties', {}).items()}
        return None

    def action(self, action):
        fn = action['function']
        params = self.schema_defaults(fn['parameters'])
        if fn['handler'] == '__function__:code_exec':
            execute(params['code'], self.vars)
            return
        if fn['handler'] != '__function__:api_call':
            raise ValueError('Unknown local handler')
        operation = fn['name']
        if params['url'] != 'https://synthetic-bank.example.invalid/v2/' + operation:
            raise AssertionError('Only inspected synthetic endpoint configurations allowed')
        execute(action['pre_process_code'], self.vars, config=params)
        if self.vars.get('hook_guard') != 'authorized':
            raise AssertionError('Generated pre-hook denied bank action: ' + operation)
        self.calls.append(dict(operation=operation, body=copy.deepcopy(params['body'])))
        queued = self.responses.get(operation, [])
        if queued:
            response = queued.pop(0)
        elif operation == 'verify_identity':
            response = ok(verified=True, all_verified=params['body']['field'] == 'due_on_of_birth')
        elif operation == 'get_account':
            response = ok(products=self.rows)
        else:
            response = ok()
        execute(action['post_process_code'], self.vars, result=copy.deepcopy(response))
        for mapping in self.workflow['nodes'][self.node].get('response_mapping', []):
            current = response
            for part in mapping['path'].split('.'):
                current = current.get(part) if isinstance(current, dict) else None
            self.vars[mapping['variable']] = current

    def advance(self, limit=100):
        for unused in range(limit):
            node = self.workflow['nodes'][self.node]
            self.trace.append(self.node)
            entry = node.get('on_enter', {})
            if entry.get('type') == 'tts' and (not node.get('on_enter_first_entry_only') or self.node not in self.entered):
                self.speech.append(self.resolve(entry['text']))
            self.entered.add(self.node)
            for action in node.get('pre_actions', []):
                self.action(action)
            if node['node_type'] == 'end_call':
                self.ended = True
                return self
            if node['node_type'] == 'conversation':
                return self
            self.node = next(t['target_node'] for t in node['transitions'] if self.condition(t['condition']))
        raise AssertionError('Native graph exceeded offline transition budget')

    def turn(self, extraction=None, **fields):
        return self.turn_raw(json.dumps(extraction if extraction is not None else fields, allow_nan=False))

    def turn_raw(self, raw):
        if self.ended or self.workflow['nodes'][self.node]['node_type'] != 'conversation':
            raise AssertionError('No listening conversation')
        self.vars['speech_event'] = raw
        self.node = self.workflow['nodes'][self.node]['transitions'][0]['target_node']
        return self.advance()

    def ready(self):
        self.advance()
        self.turn(identity_answer='1234')
        self.turn(identity_answer='1988-03-21')
        return self

    def promise_calls(self):
        return [x['body'] for x in self.calls if x['operation'] == 'create_promise']

    def propose_all(self):
        self.turn(events=[event(scope=p['product_id'], amount=180+i*10, date='2026-11-13') for i, p in enumerate(self.rows[:3]) if p['arrears_balance'] != 0])
        return self
