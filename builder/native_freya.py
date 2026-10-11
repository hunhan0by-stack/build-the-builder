"""Bounded, source-backed native Freya candidate; publication is deliberately blocked.

Only documented code_exec variables and bank API_request nodes are emitted. Fresh
call identity/account reads and typed proposal/readback can run. Bank writes and
close are sealed: the kit does not document a trusted restart or speech receipt
admission into native code. A Python harness is evidence about generated snippets,
not evidence of Freya extraction, speech, hook-abort or persistence behaviour.
"""
from __future__ import annotations

import copy
import hashlib
import json
import textwrap
import re

from .freya_export import measure_code_exec_budget, code_exec_request_bytes
from .compiled_policy import lower_compilation
from .models import FoundationError
from .semantic_renderer import validate_wording_bundle


FORMAT_VERSION = "native-source-candidate-1"
OPERATIONS = ("verify_identity", "get_account", "create_promise", "close_call")
HTTP_OPERATIONS = ("verify_identity", "get_account")
_SCHEMA_KEYS = {"type", "properties", "items", "required", "enum", "format",
                "minimum", "maximum", "minLength", "maxLength", "pattern",
                "minItems", "maxItems", "additionalProperties", "description", "title"}


def _schema_admission(schema):
    if not isinstance(schema, dict) or set(schema) - _SCHEMA_KEYS:
        raise FoundationError("Native adapter requires an explicit supported schema subset")
    if schema.get("type") not in {"object", "array", "string", "boolean", "number", "integer"}:
        raise FoundationError("Native adapter rejects unsupported schema types")
    if schema.get("format") not in {None, "date"}:
        raise FoundationError("Native adapter rejects unsupported schema formats")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise FoundationError("Native adapter rejects dynamic additional-property schemas")
    for child in schema.get("properties", {}).values():
        _schema_admission(child)
    if "items" in schema:
        _schema_admission(schema["items"])


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)


# Only json/re/datetime are verified sandbox imports in tools-and-code.md.
# Every code node has an outer catch and initializes routing to blocked first.
# No file, socket, module loader, external store or SDK is embedded here.
_RUNTIME = r'''
import json, re, datetime
def checked(value, schema, strict=False):
    kind = schema['type']
    valid = (kind == 'object' and isinstance(value, dict) or
             kind == 'array' and isinstance(value, list) or
             kind == 'string' and isinstance(value, str) or
             kind == 'boolean' and type(value) is bool or
             kind == 'integer' and type(value) is int or
             kind == 'number' and type(value) in (int, float))
    if not valid:
        raise ValueError('Schema type mismatch')
    json.dumps(value, allow_nan=False)
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise ValueError('Schema enum mismatch')
    if isinstance(value, dict):
        props = schema.get('properties', dict())
        if set(schema.get('required', [])) - set(value):
            raise ValueError('Required API field missing')
        for key, item in value.items():
            if key in props:
                checked(item, props[key], strict)
            elif strict or schema.get('additionalProperties') is False:
                raise ValueError('Unknown API field')
    elif isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 256):
            raise ValueError('API list length invalid')
        for item in value:
            checked(item, schema['items'], strict)
    elif isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4096):
            raise ValueError('API string length invalid')
        if schema.get('pattern') and not re.search(schema['pattern'], value):
            raise ValueError('API pattern mismatch')
        if schema.get('format') == 'date':
            day(value)
    elif type(value) in (int, float):
        if 'minimum' in schema and value < schema['minimum'] or 'maximum' in schema and value > schema['maximum']:
            raise ValueError('API numeric bound failed')
def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('Explicit ISO date required')
    return datetime.date.fromisoformat(value)
def cents(value):
    if type(value) not in (int, float) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', str(value)):
        raise ValueError('Finite nonnegative amount with at most two decimals required')
    parts = str(value).split('.')
    return int(parts[0]) * 100 + int((parts[1] if len(parts) == 2 else '').ljust(2, '0'))
def business(value):
    return value.weekday() in CFG['facts']['business_weekdays'] and value.isoformat() not in CFG['facts']['holidays']
def emit(kind, context):
    entry = CFG['wording'][kind]
    if kind == 'identity_question' and CFG['flow']['right_party_confirmation']:
        variants = CFG['flow']['identity_hint_templates'] if context.get('identity_hint') else CFG['flow']['identity_templates']
        entry = variants[context['field']]
    text = entry['template']
    for name in re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', text):
        value = context.get(name)
        if type(value) not in (str, int, float):
            raise ValueError('Unbound source wording context')
        value = str(value)
        # Bounded native context is printable ASCII; unsupported Unicode inputs
        # fail rather than importing an undocumented sandbox module.
        if not value or len(value) > 512 or any(not 32 <= ord(c) <= 126 or c in '{}<>' for c in value):
            raise ValueError('Unsafe wording value')
        text = text.replace('{' + name + '}', value)
    if '{' in text or '}' in text or len(text) > 32000:
        raise ValueError('Unsupported wording expansion')
    S['event_counter'] += 1
    if S['event_counter'] > 64:
        raise ValueError('Native event budget exhausted')
    event = dict(event_id='native-speech-' + str(S['event_counter']), kind=kind, text=text,
                 evidence=entry['evidence'], call_id=S['call_id'], session_id=S['session_id'], generation=1)
    if len(json.dumps(dict(S, events=S['events'] + [event]), ensure_ascii=True).encode('utf-8')) > 9000:
        raise ValueError('Native serialized state budget exceeded')
    S['events'].append(event)
    return event
def queue(operation, payload):
    if operation in ('create_promise', 'close_call'):
        raise ValueError('Native write/completion admission unavailable')
    if S.get('pending') or S['phase'] == 'blocked':
        raise ValueError('Pending or blocked request')
    if operation == 'get_account' and S['verified'] != CFG['facts']['identity_order']:
        raise ValueError('Two-factor identity gate')
    checked(payload, CFG['operations'][operation]['request'], True)
    S['request_counter'] += 1
    if S['request_counter'] > 16:
        raise ValueError('Native request budget exhausted')
    S['pending'] = dict(request_id='native-bank-' + str(S['request_counter']), operation=operation,
                        payload=payload, dispatched=False)
    for key in list(vars):
        if key.startswith('_native_arg_'):
            vars.pop(key)
    for key, value in payload.items():
        vars['_native_arg_' + key] = value
    vars.pop('_native_http_response', None)
    vars['_native_route'] = 'bank_' + operation
def context():
    p = S['product']
    value = dict(p, product_label=p['type'], amount=S.get('draft', dict()).get('amount', p['suggested_amount']),
                 date=S.get('draft', dict()).get('date', p['suggested_date']))
    recipe = CFG['flow'].get('context_recipe', dict())
    for source, name in recipe.get('money_fields', dict()).items():
        if source in value:
            money = cents(value[source])
            value[name] = str(money // 100) + '.' + str(money % 100).zfill(2)
    label = recipe.get('product_label')
    if label:
        if p['type'] == label['suffix_type']:
            suffix = p.get(label['suffix_field'])
            if not isinstance(suffix, str) or not re.fullmatch(r'[0-9]{4}', suffix):
                raise ValueError('Reviewed product label needs the API card suffix')
            value['product_label'] = label['suffix_template'].replace('{' + label['suffix_field'] + '}', suffix)
        else:
            value['product_label'] = p['type'].replace(label['fallback']['old'], label['fallback']['new'])
    return value
def propose(event):
    S['consent'] = ''
    S['readback_token'] = ''
    S['draft'] = dict()
    S['phase'] = 'proposal'
    p = S['product']
    if event['product_id'] != p['product_id']:
        raise ValueError('Proposal product mismatch')
    if not cents(p['floor']) <= cents(event['amount']) <= cents(p['past_due']):
        raise ValueError('Amount outside floor/full interval')
    target = day(event['date'])
    if not business(target) or not day(S['now'][:10]) <= target <= day(p['deadline']):
        raise ValueError('Date outside source calendar')
    for rule in CFG['constraints']:
        if 'create_promise' not in rule['operations']:
            continue
        key, limit = rule['target'], rule['value']
        if key == 'payment.amount' and cents(event['amount']) > cents(limit):
            raise ValueError('Compiled payment ceiling exceeded')
        if key == 'payment.date_on_or_before' and target > day(limit) or key == 'payment.date_on_or_after' and target < day(limit):
            raise ValueError('Compiled payment date boundary exceeded')
    payload = dict(customer_id=S['customer_id'], product_id=event['product_id'], amount=event['amount'], date=event['date'])
    checked(payload, CFG['operations']['create_promise']['request'], True)
    S['draft'] = dict(product_id=event['product_id'], amount=event['amount'], date=event['date'])
    S['readback_token'] = 'native-readback-' + str(S['event_counter'] + 1)
    S['phase'] = 'readback'
    emit('readback', context())
def parse_turn(raw):
    def pairs(items):
        value = dict()
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate turn key')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    expected = dict(person_confirm=('kind', 'explicit_yes'), identity_answer=('kind', 'field', 'answer'), proposal=('kind', 'product_id', 'amount', 'date'),
                    confirm=('kind', 'readback_token', 'explicit_yes'))
    if not isinstance(value, dict) or value.get('kind') not in expected or set(value) != set(expected[value['kind']]):
        raise ValueError('Unsupported structured input')
    return value
def step(action):
    global S
    if action == 'boot':
        if vars.get('_native_state_json'):
            raise ValueError('Trusted resumed-call continuity unavailable')
        for key in ('customer_id', 'now'):
            if not isinstance(vars.get(key), str) or not vars[key].strip():
                raise ValueError('Missing explicit native call binding: ' + key)
        provider_id = vars.get('call.call_id')
        if not isinstance(provider_id, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', provider_id) or provider_id == '00000000-0000-0000-0000-000000000000':
            raise ValueError('Documented platform call.call_id binding unavailable')
        datetime.datetime.fromisoformat(vars['now'])
        S = dict(schema_version='native-call-vars-1', binding=CFG['binding'], phase='identity',
                 customer_id=vars['customer_id'], now=vars['now'], call_id=provider_id, session_id=vars['customer_id'],
                 call_identity_authority='documented_system_variable_binding_unmeasured',
                 session_identity_authority='bank_customer_scope_not_provider_session',
                 verified=[], wrong=0, event_counter=0, request_counter=0, events=[], pending=dict(),
                 product=dict(), draft=dict(), consent='', readback_token='', diagnostic='')
        emit('opening', dict((key, vars[key]) for key in vars if not key.startswith('_')))
        if CFG['flow']['right_party_confirmation']:
            S['phase'] = 'person_confirm'
        else:
            emit('identity_question', dict(field=CFG['facts']['identity_order'][0], identity_field=CFG['facts']['identity_order'][0]))
    elif action == 'turn':
        event = parse_turn(vars.get('_native_turn_json', ''))
        if S['pending'] or S['phase'] == 'blocked':
            raise ValueError('Input while pending or blocked')
        kind = event['kind']
        if kind == 'person_confirm' and S['phase'] == 'person_confirm':
            if event['explicit_yes'] is not True:
                raise ValueError('Explicit right-party confirmation required')
            S['phase'] = 'identity'
            emit('identity_question', dict(field=CFG['facts']['identity_order'][0], identity_field=CFG['facts']['identity_order'][0]))
        elif kind == 'identity_answer' and S['phase'] == 'identity':
            field = CFG['facts']['identity_order'][len(S['verified'])]
            if event['field'] != field:
                raise ValueError('Identity order violated')
            if field == 'date_of_birth':
                day(event['answer'])
            elif not isinstance(event['answer'], str) or not re.fullmatch(r'[0-9]{4}', event['answer']):
                raise ValueError('Phone suffix must be four digits')
            queue('verify_identity', dict(customer_id=S['customer_id'], field=field, answer=event['answer']))
        elif kind == 'proposal' and S['phase'] in ('proposal', 'readback', 'confirmed'):
            propose(event)
        elif kind == 'confirm' and S['phase'] == 'readback':
            S['consent'] = ''
            if event['explicit_yes'] is not True or event['readback_token'] != S['readback_token']:
                raise ValueError('Explicit current readback approval required')
            S['consent'] = S['readback_token']
            S['phase'] = 'confirmed'
            vars['_native_route'] = 'native_write_gate'
        else:
            raise ValueError('Structured input incompatible with phase')
    elif action == 'observe':
        pending = S['pending']
        observed = vars.pop('_native_http_response', None)
        if not pending or pending['dispatched'] is not True or not isinstance(observed, dict):
            raise ValueError('Response without one dispatched request')
        if observed.get('transport_ok') is not True:
            raise ValueError('HTTP outcome unconfirmed')
        result = observed.get('body')
        op = pending['operation']
        if op not in ('verify_identity', 'get_account'):
            raise ValueError('Native write result cannot bypass sealed admission')
        checked(result, CFG['operations'][op]['response'], True)
        if result.get('ok') is not True:
            raise ValueError('Bank business outcome unconfirmed')
        S['pending'] = dict()
        if op == 'verify_identity':
            if type(result.get('verified')) is not bool:
                raise ValueError('Verification fact missing')
            if result['verified']:
                S['verified'].append(pending['payload']['field'])
            elif not result.get('hint'):
                S['wrong'] += 1
            if S['wrong'] >= CFG['facts']['identity_max_wrong']:
                raise ValueError('Identity attempts exhausted')
            if S['verified'] == CFG['facts']['identity_order']:
                S['phase'] = 'account'
                queue('get_account', dict(customer_id=S['customer_id']))
            else:
                S['phase'] = 'identity'
                field = CFG['facts']['identity_order'][len(S['verified'])]
                emit('identity_question', dict(field=field, identity_field=field, identity_hint=bool(result.get('hint'))))
        else:
            rows = result.get('products')
            if not isinstance(rows, list) or len(rows) != 1 or len(rows) > CFG['facts']['product_limit']:
                raise ValueError('Native adapter supports exactly one account')
            p = dict(rows[0])
            if not isinstance(p.get('product_id'), str) or not p['product_id']:
                raise ValueError('Account identifier missing')
            floor_key = CFG['facts']['floor_fields'][p['type']]
            p['floor'] = p[floor_key]
            if cents(p['floor']) > cents(p['past_due']) or type(p['days_late']) is not int:
                raise ValueError('Invalid account bounds')
            bands = [b for b in CFG['facts']['bands'] if b['min_days_late'] <= p['days_late'] <= b['max_days_late']]
            if len(bands) != 1:
                raise ValueError('Missing or ambiguous delinquency band')
            limit = day(S['now'][:10])
            remaining = bands[0]['deadline_business_days']
            for unused in range(4000):
                if not remaining:
                    break
                limit += datetime.timedelta(days=1)
                if business(limit):
                    remaining -= 1
            if remaining:
                raise ValueError('Calendar budget exceeded')
            p['deadline'] = limit.isoformat()
            amount_limit = cents(p['past_due'])
            earliest = day(S['now'][:10])
            for rule in CFG['constraints']:
                if 'create_promise' not in rule['operations']:
                    continue
                if rule['target'] == 'payment.amount':
                    amount_limit = min(amount_limit, cents(rule['value']))
                elif rule['target'] == 'payment.date_on_or_before':
                    limit = min(limit, day(rule['value']))
                elif rule['target'] == 'payment.date_on_or_after':
                    earliest = max(earliest, day(rule['value']))
            for unused in range(370):
                if business(earliest):
                    break
                earliest += datetime.timedelta(days=1)
            if earliest > limit or cents(p['floor']) > amount_limit:
                raise ValueError('No permitted proposal in source bounds')
            p['suggested_amount'] = amount_limit / 100
            p['suggested_date'] = earliest.isoformat()
            S['product'] = p
            S['phase'] = 'proposal'
            emit('account_summary', context())
            emit('proposal_request', context())
    elif action in ('write_gate', 'close_gate'):
        S['consent'] = ''
        raise ValueError('Native write/completion admission unavailable')
    else:
        raise ValueError('Unknown native action')
'''


def _capsule(config):
    # Avoid Freya template delimiters even inside escaped JSON string literals.
    return "CFG = json.loads(" + repr(_json(config)).replace("{{", "{\\x7b").replace("}}", "}\\x7d") + ")\n"


def _driver(config, action, native_runtime_mode="host_exclusive"):
    tail = """vars['_native_route'] = 'native_blocked'
vars['_native_speech'] = ''
vars['_native_has_speech'] = 'no'
S = dict()
try:
    if len(json.dumps(vars, ensure_ascii=True, allow_nan=False).encode('utf-8')) > 16000:
        raise ValueError('Native vars budget exceeded')
    if ACTION != 'boot':
        S = json.loads(vars.get('_native_state_json', ''))
        if S.get('binding') != CFG['binding'] or S.get('customer_id') != vars.get('customer_id') or S.get('now') != vars.get('now') or S.get('call_id') != vars.get('call.call_id') or S.get('session_id') != vars.get('customer_id'):
            raise ValueError('Native state binding mismatch')
    elif vars.get('_native_state_json'):
        S = json.loads(vars['_native_state_json'])
    cursor = len(S.get('events', []))
    step(ACTION)
    if vars['_native_route'] == 'native_blocked':
        vars['_native_route'] = 'native_listen'
    fresh = S['events'][cursor:]
    vars['_native_speech'] = ' '.join(e['text'] for e in fresh)
    vars['_native_has_speech'] = 'yes' if fresh else 'no'
except Exception as error:
    vars['_native_route'] = 'native_blocked'
    S['phase'] = 'blocked'
    S['consent'] = ''
    S['diagnostic'] = str(error)
finally:
    vars['_native_turn_json'] = ''
    vars['_native_state_json'] = json.dumps(S, allow_nan=False)
    vars['_native_phase'] = S.get('phase', 'blocked')
    vars['_native_readback_token'] = S.get('readback_token', '')
"""
    from .native_write_guard import native_scope_code, bank_session_boot
    if native_runtime_mode == 'official_bank_session' and action == 'boot':
        tail = tail.replace('    cursor = ', textwrap.indent(bank_session_boot(config['binding']), '    ') + '\n    cursor = ', 1)
    code = native_scope_code(_RUNTIME, native_runtime_mode) + _capsule(config) + "ACTION = " + repr(action) + "\n" + native_scope_code(tail, native_runtime_mode)
    if "{{" in code or "}}" in code:
        raise FoundationError("Native code includes forbidden template delimiters")
    compile(code, "native_driver", "exec")
    return code


def _edge(target, variable=None, operand=None):
    condition = {"operator": "always"} if variable is None else {
        "logic": "or", "conditions": [{"variable": variable, "operator": "equals", "operand": operand}]}
    return {"target_node": target, "description": "Deterministic native controller route", "condition": condition}


def _code_node(name, code, transitions, fallback="native_blocked"):
    return {"name": name, "node_type": "code", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": fallback,
            "pre_actions": [{"type": "function", "function": {"name": "code_exec",
                "handler": "__function__:code_exec", "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "default": code}, "timeout_ms": {"type": "number", "default": 10000}}}}}],
            "transitions": transitions}


def _bank_node(operation, config, native_runtime_mode="host_exclusive"):
    name = operation.name
    body = copy.deepcopy(operation.request)
    for key, spec in body["properties"].items():
        spec["default"] = "{{_native_arg_" + key + "}}"
    # Request hooks need no renderer, proposal evaluator or whole source bundle.
    minimal = {"binding": config["binding"], "operations": {name: config["operations"][name]},
               "facts": {"identity_order": config["facts"]["identity_order"]}}
    guard = _RUNTIME[:_RUNTIME.index("def cents(")] + _capsule(minimal) + "OP = " + repr(name) + "\n" + """S = json.loads(vars.get('_native_state_json', ''))
pending = S.get('pending', dict())
if OP in ('create_promise', 'close_call'):
    raise ValueError('Native write/completion admission unavailable')
if S.get('binding') != CFG['binding'] or S.get('customer_id') != vars.get('customer_id') or S.get('call_id') != vars.get('call.call_id') or S.get('session_id') != vars.get('customer_id') or S.get('now') != vars.get('now'):
    raise ValueError('Native call binding mismatch')
if S.get('phase') == 'blocked' or pending.get('operation') != OP or pending.get('dispatched') is not False:
    raise ValueError('Native request is missing, blocked or already dispatched')
if config.get('url') != CFG['operations'][OP]['url'] or config.get('method') != CFG['operations'][OP]['method'] or config.get('body') != pending.get('payload'):
    raise ValueError('Request differs from admitted bank operation')
checked(config['body'], CFG['operations'][OP]['request'], True)
if OP == 'get_account' and S['verified'] != CFG['facts']['identity_order']:
    raise ValueError('Two-factor identity gate')
pending['dispatched'] = True
vars['_native_state_json'] = json.dumps(S, allow_nan=False)
"""
    post = """import json
try:
    body = result.get('data')
    valid = result.get('status') == 'success' and type(result.get('status_code')) is int and 200 <= result['status_code'] < 300
    if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode('utf-8')) > 4096:
        body = dict()
        valid = False
    result.clear()
    result.update(status='success', status_code=200, message='', data=dict(_native_http=dict(transport_ok=valid, body=body)))
except Exception:
    result.clear()
    result.update(status='success', status_code=200, message='', data=dict(_native_http=dict(transport_ok=False, body=dict())))
"""
    from .native_write_guard import native_scope_code
    guard = native_scope_code(guard, native_runtime_mode)
    tool = {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
        "function": {"name": name, "handler": "__function__:api_call", "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "default": operation.url}, "method": {"type": "string", "default": operation.method},
            "connect_retries": {"type": "number", "default": 0},
            "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}}, "body": body}}},
        "pre_process_code": guard, "post_process_code": post}
    return {"name": "bank_" + name, "node_type": "api_request", "on_enter": {"type": "silent"}, "passthrough": True,
            "passthrough_default_transition": "native_observe", "pre_actions": [tool],
            "response_mapping": [{"path": "data._native_http", "variable": "_native_http_response"}],
            "transitions": [_edge("native_observe")]}


_SEALED_GATE = """import json
vars['_native_route'] = 'native_blocked'
vars['_native_speech'] = ''
vars['_native_has_speech'] = 'no'
try:
    raw = vars.get('_native_state_json', '')
    if len(raw) > 16000:
        raise ValueError('Native state budget exceeded')
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError('Invalid native state')
except Exception:
    state = dict()
state.update(phase='blocked', consent='', diagnostic='Native write/completion admission unavailable')
vars['_native_state_json'] = json.dumps(state, allow_nan=False)
vars['_native_phase'] = 'blocked'
vars['_native_readback_token'] = ''
vars['_native_turn_json'] = ''
"""


def build_native_workflow(compiled, documents, wording_bundle, *, initial_variables=None, deployment=None, wording_authorization=None, source_snapshots=None, native_runtime_mode="host_exclusive"):
    """Return an unpublishable native candidate, without evaluating any external input.

    deployment is reserved, deliberately rejects endpoints/receipt boolean flags.
    Only a future separately reviewed platform binding can alter write admission.
    """
    if deployment is not None:
        raise FoundationError("External deployment/persistence/receipt endpoints are not allowed in this native graph")
    wording = validate_wording_bundle(wording_bundle, documents)
    from compiler.typed_source import source_wording
    flow = {"right_party_confirmation": False}
    if wording_authorization is not None:
        from .native_policy_bindings import verify_reviewed_native_binding
        flow = verify_reviewed_native_binding(compiled, documents, wording_bundle, wording_authorization, source_snapshots=source_snapshots)
    elif wording != source_wording(documents):
        raise FoundationError("Native wording roles differ from source-declared typed kind mapping")
    if compiled.coverage.get("unresolved_count") or compiled.coverage.get("unsupported_decisions") or not compiled.semantics.get("complete_for_supported_decisions"):
        raise FoundationError("Native adapter requires resolved typed lowering")
    # Revalidate source quotes and typed fact/rule agreement. This catches stale
    # documents and mutated lowering values, not incorrect model interpretation.
    by_id = {document.source_id: document for document in documents}
    source_facts = []
    for name, fact in compiled.policy.facts.items():
        refs = []
        for ref in fact.evidence:
            doc = by_id.get(ref.source_id)
            if doc is None or not 0 <= ref.start < ref.end <= len(doc.text):
                raise FoundationError("Native policy source evidence is missing")
            refs.append({"source_id": ref.source_id, "start": ref.start, "end": ref.end, "quote": doc.text[ref.start:ref.end]})
        source_facts.append({"name": name, "value": copy.deepcopy(fact.value), "category": "other", "evidence": refs})
    rebuilt = lower_compilation({"kind": "source_checked_decision_ir", "schema_version": "compiler-envelope-1",
        "contract_sha256": compiled.contract.source_hash, "sources": [doc.inventory() for doc in documents],
        "decisions": {"schema_version": "decision-ir-1", "rules": copy.deepcopy(compiled.semantics["decisions"]),
                      "facts": source_facts, "api_bindings": [{"role": "identity_check", "operation": "verify_identity",
                          "direction": "request", "field_path": "field", "allowed_values": compiled.policy.facts["identity_order"].value,
                          "evidence": next(item["evidence"] for item in source_facts if item["name"] == "identity_order")}],
                      "unresolved": []}}, documents, compiled.contract)
    if rebuilt.policy.serializable() != compiled.policy.serializable() or rebuilt.constraints != compiled.constraints:
        raise FoundationError("Native lowering differs from revalidated source decisions")
    facts = {key: copy.deepcopy(value.value) for key, value in compiled.policy.facts.items()}
    if set(facts["identity_order"]) != {"date_of_birth", "phone_last4"} or len(facts["identity_order"]) != 2:
        raise FoundationError("Native adapter supports exactly the two bank identity factors")
    operations = {}
    for name in OPERATIONS:
        op = compiled.contract.operations[name]
        _schema_admission(op.request)
        _schema_admission(op.response)
        operations[name] = {"url": op.url, "method": op.method, "request": copy.deepcopy(op.request), "response": copy.deepcopy(op.response)}
    runtime_flow = {key: copy.deepcopy(flow[key]) for key in ("right_party_confirmation", "identity_templates", "identity_hint_templates", "context_recipe") if key in flow}
    if "context_recipe" in runtime_flow:
        runtime_flow["context_recipe"]["product_label"].pop("implementation_span", None)
    config = {"facts": facts, "constraints": copy.deepcopy(compiled.constraints), "wording": wording, "operations": operations, "flow": runtime_flow}
    config["binding"] = hashlib.sha256(_json(config).encode()).hexdigest()
    routes = ["native_listen", "native_write_gate", "native_blocked"] + ["bank_" + name for name in HTTP_OPERATIONS]
    drive_routes = [_edge("native_speak", "_native_has_speech", "yes"), _edge("native_dispatch", "_native_has_speech", "no")]
    nodes = {name: _code_node(name, _driver(config, action, native_runtime_mode), drive_routes) for name, action in (
        ("native_boot", "boot"), ("native_turn", "turn"), ("native_observe", "observe"))}
    for name in ("native_write_gate", "native_close_gate"):
        nodes[name] = _code_node(name, _SEALED_GATE, [_edge("native_blocked")])
    nodes["native_dispatch"] = _code_node("native_dispatch", "pass\n", [_edge(name, "_native_route", name) for name in routes])
    nodes["native_speak"] = {"name": "native_speak", "node_type": "conversation", "on_enter": {"type": "tts", "text": "{{_native_speech}}"},
        "task_message": "Speak only the supplied exact source wording. No tool calls or added wording.", "passthrough": True,
        "passthrough_default_transition": "native_dispatch", "transitions": [_edge("native_dispatch")]}
    description = ("Extract ONLY the current customer's structured message as JSON, never prior slots. "
        "Allowed exact shapes: person_confirm {kind,explicit_yes}; identity_answer {kind,field,answer}; proposal {kind,product_id,amount,date}; "
        "confirm {kind,readback_token,explicit_yes}. Amount must be a JSON number, dates explicit YYYY-MM-DD. "
        "For vague dates, text intents, callback, transfer or ambiguity return an unsupported kind. "
        "Never infer explicit_yes from silence or a question. In person_confirm phase accept only an explicit answer to the current right-person question. In readback phase bind yes only to the current readback token "
        "{{_native_readback_token}}. This extraction has not been measured on the platform.")
    nodes["native_listen"] = {"name": "native_listen", "node_type": "conversation", "on_enter": {"type": "silent"},
        "task_message": "Wait for the customer's current answer. Do not add policy decisions or wording. No tool calls.",
        "extraction_schema": [{"name": "_native_turn_json", "type": "string", "description": description,
                               "min_confidence": 0.8, "validation_pattern": "^[\\s\\S]{1,2048}$", "max_retries": 1}],
        "transitions": [{"target_node": "native_turn", "description": "Current structured input is present",
                         "condition": {"logic": "or", "conditions": [{"variable": "_native_turn_json", "operator": "not_equals", "operand": ""}]}}],
        "max_node_turns": 6, "max_turn_transition": "native_blocked"}
    # No end_call handler: receipt absence never fabricates completed goodbye.
    nodes["native_blocked"] = {"name": "native_blocked", "node_type": "conversation", "on_enter": {"type": "silent"},
        "task_message": "The partial candidate is blocked. Do not speak, call tools, or claim completion.",
        "disable_inactivity_monitor": True, "transitions": []}
    for name in HTTP_OPERATIONS:
        nodes["bank_" + name] = _bank_node(compiled.contract.operations[name], config, native_runtime_mode)
    workflow = {"initial_node": "native_boot", "nodes": nodes,
        "system_prompt": "The deterministic controller supplies exact source wording and bank actions. Never invent policy, successful bank results or speech completion."}
    for node in nodes.values():
        for tool in node.get("pre_actions", []):
            fn = tool["function"]
            if fn["handler"] == "__function__:code_exec":
                compile(fn["parameters"]["properties"]["code"]["default"], "native_code", "exec")
            else:
                compile(tool["pre_process_code"], "native_pre", "exec")
                compile(tool["post_process_code"], "native_post", "exec")
    budget = measure_code_exec_budget(workflow, initial_variables or {}, reserve_variable_bytes=16384)
    if not budget["passed"]:
        raise FoundationError("Native serialized code+vars admission exceeds the safe byte budget")
    package_bytes = len(json.dumps(workflow, indent=2, ensure_ascii=True, allow_nan=False).encode("utf-8"))
    if package_bytes >= 131072:
        raise FoundationError("Native whole workflow JSON package exceeds the byte limit")
    budget.update(whole_workflow_json_bytes=package_bytes, whole_workflow_limit_bytes=131072,
                  whole_workflow_package_passed=True,
                  whole_workflow_serialization="json.dumps(workflow, indent=2, ensure_ascii=True, allow_nan=False), UTF-8")
    return {"format_version": FORMAT_VERSION, "workflow": workflow, "budget": budget, "wording": wording,
        "source_inventory": [doc.inventory() for doc in documents], "policy_coverage": copy.deepcopy(compiled.coverage),
        "readiness": {"native_schema_candidate": True, "platform_schema_validated": False, "publish_allowed": False,
            "bank_writes_enabled": False, "speech_completed": False, "persistent_restart_proven": False,
            "native_event_binding_authority": "documented_system_variable_binding_unmeasured", "semantic_truth_verified": False,
            "provider_call_id_variable": "call.call_id", "system_variable_flat_reader_platform_verified": False,
            "provider_session_id_required": False, "session_scope": "bank_customer_id_not_provider_session_id",
            "supported": ["fresh_call_opening", "two_factor_identity_bank_reads", "single_account_bank_read", "typed_proposal", "source_readback", "current_explicit_confirmation"],
            "unsupported": ["native_write_admission", "authenticated_native_speech_receipt_admission", "resumed_call_trusted_continuity", "bank_close", "arbitrary_voice_intent", "multi_product", "callbacks", "transfers", "add_note", "non_ascii_render_context", "native_hook_abort_platform_guarantee", "posthook_flattened_vars_size_platform_guarantee"],
            "scope": "partial native graph; Python harness does not establish platform behaviour"}}


class NativeHarness:
    """Execute the actual emitted code/hook/mapping strings, without any network.

    HTTP envelopes are explicit local fixtures. The harness raising on a pre-hook
    error is a local contract, not a claim about undocumented engine behaviour.
    """
    def __init__(self, candidate, variables):
        self.candidate = copy.deepcopy(candidate)
        self.variables = copy.deepcopy(variables)
        self.requests = []
        self.traversal = []
        self.tts_events = []
        self.current_node = None
        self.code_measurements = []

    def run_code(self, node):
        action = self.candidate["workflow"]["nodes"][node]["pre_actions"][0]
        fn = action["function"]
        if fn["handler"] != "__function__:code_exec":
            raise FoundationError("Harness requires an actual code_exec node")
        code = fn["parameters"]["properties"]["code"]["default"]
        measured = len(code_exec_request_bytes(code, self.variables))
        self.code_measurements.append({"node": node, "serialized_input_bytes": measured,
                                       "platform_private_framing_measured": False})
        if measured > 114688:
            raise FoundationError("Local harness observed code+vars above safe budget")
        exec(code, {"vars": self.variables})
        return self.state

    @property
    def state(self):
        return json.loads(self.variables.get("_native_state_json", "{}"))

    def start(self):
        return self.drive("native_boot")

    def _choose(self, node):
        def matches(condition):
            if condition.get("operator") == "always":
                return True
            if "logic" in condition:
                values = [matches(item) for item in condition["conditions"]]
                return any(values) if condition["logic"] == "or" else all(values)
            observed = self.variables.get(condition["variable"])
            if condition["operator"] == "equals":
                return observed == condition["operand"]
            if condition["operator"] == "not_equals":
                return observed != condition["operand"]
            raise FoundationError("Unsupported harness transition")
        for edge in node["transitions"]:
            if matches(edge["condition"]):
                return edge["target_node"]
        return node.get("passthrough_default_transition")

    def drive(self, node_name):
        """Follow actual graph edges until listening, blocked or pending HTTP."""
        for _ in range(40):
            self.current_node = node_name
            node = self.candidate["workflow"]["nodes"][node_name]
            self.traversal.append({"node": node_name, "node_type": node["node_type"]})
            if node["node_type"] == "code":
                self.run_code(node_name)
            elif node["node_type"] == "conversation" and node.get("passthrough"):
                text = node["on_enter"]["text"]
                if text != "{{_native_speech}}":
                    raise FoundationError("Unsupported TTS binding in local harness")
                self.tts_events.append({"node": node_name, "text": self.variables["_native_speech"],
                                        "speech_completed": False})
            else:
                return self.state
            node_name = self._choose(node)
            if node_name is None:
                raise FoundationError("Native graph has no deterministic route")
        raise FoundationError("Native graph loop bound exceeded")

    def receive(self, event):
        if self.current_node != "native_listen":
            raise FoundationError("Harness accepts current structured input only at native_listen")
        self.variables["_native_turn_json"] = _json(event)
        target = self._choose(self.candidate["workflow"]["nodes"]["native_listen"])
        return self.drive(target)

    def dispatch(self, operation):
        if operation not in HTTP_OPERATIONS:
            raise FoundationError("Native write/completion admission unavailable; HTTP node omitted")
        node = self.candidate["workflow"]["nodes"]["bank_" + operation]
        tool = node["pre_actions"][0]
        props = tool["function"]["parameters"]["properties"]
        body = {}
        for key, spec in props["body"]["properties"].items():
            slot = "_native_arg_" + key
            if slot in self.variables:
                body[key] = copy.deepcopy(self.variables[slot])
        config = {"url": props["url"]["default"], "method": props["method"]["default"], "body": body}
        exec(tool["pre_process_code"], {"vars": self.variables, "config": config})
        self.requests.append({"operation": operation, **copy.deepcopy(config)})
        return config

    def resume(self, operation, envelope):
        if not isinstance(envelope, dict):
            raise FoundationError("Harness expects an explicit local HTTP envelope object")
        if self.current_node != "bank_" + operation or self.state.get("pending", {}).get("operation") != operation:
            raise FoundationError("HTTP result belongs to a different native request node")
        node = self.candidate["workflow"]["nodes"]["bank_" + operation]
        result = copy.deepcopy(envelope)
        exec(node["pre_actions"][0]["post_process_code"], {"result": result})
        for mapping in node["response_mapping"]:
            observed = result
            for part in mapping["path"].split("."):
                observed = observed[part]
            self.variables[mapping["variable"]] = observed
        return self.drive(self._choose(node))



def specialize_native_action(code, action):
    """Keep the selected existing step branch and its helper closure only."""
    import ast
    tree = ast.parse(code)
    step = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'step')
    declared = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ACTION' for t in n.targets))
    if ast.literal_eval(declared.value) != action:
        raise FoundationError('EXACT_NATIVE_LITERAL_ACTION_REQUIRED')
    branches = [n for n in step.body if isinstance(n, ast.If) and ast.unparse(n.test).startswith('action == ')]
    if branches:
        branch = branches[0]
        while isinstance(branch, ast.If) and ast.unparse(branch.test) != 'action == ' + repr(action):
            branch = branch.orelse[0] if len(branch.orelse) == 1 else None
        if not isinstance(branch, ast.If):
            raise FoundationError('EXACT_NATIVE_STATIC_ACTION_REQUIRED')
        step.body = copy.deepcopy(branch.body)
    elif any(isinstance(n, ast.Name) and n.id == 'action' for n in ast.walk(step)):
        raise FoundationError('UNRECOGNIZED_DYNAMIC_NATIVE_ACTION')
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    roots = [n for n in tree.body if not isinstance(n, ast.FunctionDef)]
    required, pending = set(), [n.id for root in roots for n in ast.walk(root) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]
    while pending:
        name = pending.pop()
        if name in required or name not in functions:
            continue
        required.add(name)
        pending.extend(n.id for n in ast.walk(functions[name]) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load))
    cfg = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CFG' for t in n.targets))
    configuration = json.loads(ast.literal_eval(cfg.value.args[0]))
    tree.body = [n for n in tree.body if n is not cfg and (not isinstance(n, ast.FunctionDef) or n.name in required)]
    code = 'import json\n' + _capsule(configuration) + ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    compile(code, '<selected-native-action>', 'exec')
    return code
