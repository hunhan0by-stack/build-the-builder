"""Source-bound single-product native banking pilot, not final competition coverage.

The old read-only prefix stays immutable. This emitter adds the documented
terminal/end_of_call closure and a fresh-call write guard. Publication is a
separate controller decision; local harnesses do not confer native authority.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import tokenize
from pathlib import Path
import textwrap

from .ingest import read_source
from .models import FoundationError
from .native_freya import build_native_workflow, _code_node, _edge, measure_code_exec_budget
from .native_official_closure import compile_official_closure
from .native_write_guard import make_write_hooks, native_scope_code, validate_runtime_mode


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _compress_generated_python(code):
    """Remove only comments and blank lines outside strings; require exact AST."""
    before = ast.dump(ast.parse(code), include_attributes=False)
    lines = code.splitlines(keepends=True)
    tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    protected = set()
    for token in tokens:
        if token.type == tokenize.STRING and token.start[0] != token.end[0]:
            protected.update(range(token.start[0], token.end[0] + 1))
    for token in reversed(tokens):
        if token.type == tokenize.COMMENT:
            index = token.start[0] - 1
            lines[index] = lines[index][:token.start[1]] + lines[index][token.end[1]:]
    after = ''.join(line for number, line in enumerate(lines, 1) if line.strip() or number in protected)
    if ast.dump(ast.parse(after), include_attributes=False) != before:
        raise FoundationError('GENERATED_COMPRESSION_CHANGED_PYTHON_AST')
    return after


def _cfg(candidate):
    code = candidate['workflow']['nodes']['native_boot']['pre_actions'][0]['function']['parameters']['properties']['code']['default']
    assignments = [item for item in ast.parse(code).body if isinstance(item, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'CFG' for target in item.targets)]
    if len(assignments) != 1:
        raise FoundationError('EXACT_NATIVE_PREFIX_CAPSULE_REQUIRED')
    expression = assignments[0].value
    if not isinstance(expression, ast.Call) or ast.unparse(expression.func) != 'json.loads' or len(expression.args) != 1:
        raise FoundationError('EXACT_NATIVE_PREFIX_JSON_CAPSULE_REQUIRED')
    return json.loads(ast.literal_eval(expression.args[0]))


def _bind_driver_state_in_place(code):
    """Keep one shared state object in module or enclosing sandbox scope.

    The retained live boot failed with 'event_counter'. Its exact snippet
    reproduces that failure inside a function wrapper: global rebinding in
    step() leaves emit() reading the enclosing empty S. The documented vars
    contract does not guarantee module execution. Change only that rebinding.
    """
    tree = ast.parse(code)
    steps = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'step']
    if len(steps) != 1:
        raise FoundationError('EXACT_NATIVE_DRIVER_STEP_REQUIRED')
    step = steps[0]
    declarations = [node for node in ast.walk(step) if isinstance(node, ast.Global)]
    assignments = [node for node in ast.walk(step) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'S' for target in node.targets)]
    if (len(declarations) != 1 or declarations[0].names != ['S'] or len(assignments) != 1
            or len(assignments[0].targets) != 1 or not isinstance(assignments[0].value, ast.Call)
            or not isinstance(assignments[0].value.func, ast.Name)
            or assignments[0].value.func.id != 'dict'):
        raise FoundationError('EXACT_NATIVE_DRIVER_STATE_REBIND_REQUIRED')
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    for node, replacement in ((declarations[0], ''),
            (assignments[0], 'S.update(' + ast.get_source_segment(code, assignments[0].value) + ')')):
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        replacements.append((start, end, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        code = code[:start] + replacement + code[end:]
    compile(code, 'scope_safe_native_driver', 'exec')
    return code


def _bind_scalar_read_result(code):
    """Lower unconfirmed object mapping to documented scalar/dotted vars."""
    old = "observed = vars.pop('_native_http_response', None)"
    if code.count(old) != 1 or code.count("vars.pop('_native_http_response', None)") != 2:
        raise FoundationError('EXACT_NATIVE_READ_RESULT_BOUNDARY_REQUIRED')
    code = code.replace("vars.pop('_native_http_response', None)", "vars['_native_http_json'] = ''", 1)
    # Share the existing strict parser; do not duplicate it three times in the
    # bounded publication payload or change duplicate/nonfinite semantics.
    parser = next(node for node in ast.parse(code).body if isinstance(node, ast.FunctionDef) and node.name == 'parse_turn')
    pairs = next(node for node in parser.body if isinstance(node, ast.FunctionDef) and node.name == 'pairs')
    load = next(node for node in parser.body if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == 'json.loads')
    pair_source = ast.get_source_segment(code, pairs)
    load_source = ast.get_source_segment(code, load)
    expression = ast.get_source_segment(code, load.value).replace('object_pairs_hook=pairs', 'object_pairs_hook=read_pairs')
    shared = textwrap.dedent('    ' + pair_source).replace('def pairs(', 'def read_pairs(', 1)
    shared += '\ndef read_json(raw):\n    return ' + expression + '\n'
    code = code.replace('    ' + pair_source + '\n', '', 1).replace(load_source, 'value = read_json(raw)', 1)
    code = code.replace('def parse_turn(raw):', shared + 'def parse_turn(raw):', 1)
    return code.replace(old, """key = 'bank_' + pending.get('operation', '') + '_response.data._native_http_json'
        raw = vars.get('_native_http_json') or vars.get(key, '')
        vars['_native_http_json'] = vars[key] = ''
        observed = read_json(raw) if isinstance(raw, str) and len(raw) <= 8192 else None""")


def _compact_edge_descriptions(graph):
    """Shorten only redundant display text; every executable field stays exact."""
    count = 0
    for node in graph['nodes'].values():
        for edge in node.get('transitions', []):
            if edge.get('description') == 'Deterministic native controller route':
                edge['description'] = 'Native route'
                count += 1
    return count


_READ_POST = '''import json
try:
    body = result.get('data')
    valid = result.get('status') == 'success' and type(result.get('status_code')) is int and result['status_code'] == 200
    if len(json.dumps(body, allow_nan=False)) > 4096:
        body, valid = dict(), False
    raw = json.dumps(dict(transport_ok=valid, body=body), allow_nan=False)
except Exception:
    raw = json.dumps(dict(transport_ok=False, body=dict()))
result.clear()
result.update(status='success', status_code=200, data=dict(_native_http_json=raw))
'''


_METADATA = '''
_product = S.get('product', dict())
_verified = S.get('verified', [])
_order = CFG['facts']['identity_order']
vars['_native_identity_field'] = _order[len(_verified)] if len(_verified) < len(_order) else ''
vars['_native_proposal_product_id'] = _product.get('product_id', '')
vars['_native_suggested_amount'] = _product.get('suggested_amount', '')
vars['_native_suggested_date'] = _product.get('suggested_date', '')
'''


_AUTHORIZE = '''
import json
vars['_native_route'] = 'native_blocked'
vars['_native_write_authorization'] = ''
try:
    S = json.loads(vars.get('_native_state_json', ''))
    if S.get('binding') != BINDING or S.get('phase') != 'confirmed' or S.get('verified') != IDENTITY_ORDER:
        raise ValueError('Source, identity or confirmation missing')
    if S.get('call_id') != vars.get('call.call_id') or S.get('customer_id') != vars.get('customer_id') or S.get('now') != vars.get('now'):
        raise ValueError('Native call binding mismatch')
    if not S.get('readback_token') or S.get('consent') != S.get('readback_token'):
        raise ValueError('Explicit current consent missing')
    _context = json.loads(vars.get('_native_write_context', ''))
    _write = json.loads(vars.get('_native_write_state', ''))
    if _context.get('customer_id') != S['customer_id'] or _write.get('reservation_id') != _context.get('reservation_id') or _write.get('phase') != 'ready':
        raise ValueError('Fresh host reservation missing')
    _draft = S['draft']
    _product = S['product']
    _payload = dict(customer_id=S['customer_id'], product_id=_draft['product_id'], amount=_draft['amount'], date=_draft['date'])
    if _payload['product_id'] != _product['product_id']:
        raise ValueError('Confirmed product mismatch')
    _auth = dict(source_binding=BINDING, customer_id=S['customer_id'], call_id=S['call_id'], payload=_payload,
        identity_verified=True, explicit_consent=True, consent_token=S['consent'], readback_token=S['readback_token'],
        account_snapshot=_product, account_floor=_product['floor'], account_past_due=_product['past_due'], earliest_date=S['now'][:10], deadline=_product['deadline'])
    vars['_native_write_authorization'] = json.dumps(_auth, allow_nan=False)
    for _key, _value in _payload.items():
        vars['_native_arg_' + _key] = _value
    S['phase'] = 'write_pending'
    vars['_native_state_json'] = json.dumps(S, allow_nan=False)
    vars['_native_route'] = 'bank_create_promise'
except Exception:
    vars['_native_write_outcome'] = 'unknown'
'''


_RESULT = '''
vars['_native_route'] = 'native_blocked'
vars['_native_speech'] = ''
try:
    S = json.loads(vars.get('_native_state_json', ''))
    if S.get('binding') != BINDING or S.get('call_id') != vars.get('call.call_id') or S.get('customer_id') != vars.get('customer_id') or S.get('phase') != 'write_pending':
        raise ValueError('Unbound result')
    A = json.loads(vars.get('_native_write_authorization', ''))
    _draft_payload = dict(customer_id=S['customer_id'], product_id=S['draft']['product_id'], amount=S['draft']['amount'], date=S['draft']['date'])
    if S.get('product') != A.get('account_snapshot') or _draft_payload != A.get('payload') or S.get('consent') != S.get('readback_token') or S.get('readback_token') != A.get('readback_token'):
        raise ValueError('Account, draft or current confirmation changed during dispatch')
    _outcome = vars.get('_native_write_outcome')
    if _outcome == 'retry_allowed':
        vars['_native_route'] = 'bank_create_promise'
    elif _outcome == 'confirmed':
        _product = S['product']
        _label = _product['type'].replace('_', ' ')
        _recipe = PRODUCT_LABEL
        if _recipe and _product['type'] == _recipe['suffix_type']:
            _label = _recipe['suffix_template'].replace('{' + _recipe['suffix_field'] + '}', _product[_recipe['suffix_field']])
        vars['_native_speech'] = SUCCESS_TEMPLATE.replace('{product_label}', _label)
        S['phase'] = 'promise_confirmed'
        S['events'].append(dict(kind='promise_success', text=vars['_native_speech'], bank_ok=True))
        vars['_native_state_json'] = json.dumps(S, allow_nan=False)
        vars['_native_route'] = 'native_success'
    elif _outcome == 'failed':
        # Both attempts definitively returned ok:false: Rule37, not uncertainty.
        vars['_native_speech'] = 'I could not record the promise.'
        S['phase'] = 'promise_failed'
        S['consent'] = ''
        S['events'].append(dict(kind='promise_failure', text=vars['_native_speech'], bank_ok=False))
        vars['_native_state_json'] = json.dumps(S, allow_nan=False)
        vars['_native_route'] = 'native_success'
    else:
        # No success statement and no automatic resend for uncertain responses.
        S['phase'] = 'blocked'
        S['consent'] = ''
        S['diagnostic'] = 'Bank result is ' + str(_outcome) + '; no success or replay authorized'
        vars['_native_state_json'] = json.dumps(S, allow_nan=False)
except Exception:
    vars['_native_route'] = 'native_blocked'
'''


_TERMINAL_ARM = '''
import json
vars['_native_terminal_path'] = ''
vars['_native_close_url'] = ''
vars['_native_close_customer'] = ''
vars['_native_terminal_target'] = 'native_blocked'
try:
    S = json.loads(vars.get('_native_state_json', ''))
    _state = json.loads(vars.get('_native_write_state', ''))
    _context = json.loads(vars.get('_native_write_context', ''))
    A = json.loads(vars.get('_native_write_authorization', ''))
    if S.get('binding') != BINDING or S.get('verified') != IDENTITY_ORDER or S.get('now') != vars.get('now'):
        raise ValueError('Source, identity or date changed')
    if not S.get('call_id') or S['call_id'] != vars.get('call.call_id') or S.get('customer_id') != vars.get('customer_id'):
        raise ValueError('Native call binding changed')
    if _context.get('customer_id') != S['customer_id'] or not _context.get('reservation_id') or _state.get('reservation_id') != _context['reservation_id'] or _state.get('call_id') != S['call_id']:
        raise ValueError('No bound bank result')
    _payload = dict(customer_id=S['customer_id'], product_id=S['draft']['product_id'], amount=S['draft']['amount'], date=S['draft']['date'])
    if A.get('source_binding') != BINDING or A.get('call_id') != S['call_id'] or A.get('customer_id') != S['customer_id'] or A.get('payload') != _payload or A.get('account_snapshot') != S.get('product'):
        raise ValueError('Account or authorized request changed')
    if not S.get('readback_token') or S['readback_token'] != A.get('readback_token') or A.get('consent_token') != S['readback_token']:
        raise ValueError('Current confirmation missing')
    _confirmed = _state.get('phase') == 'confirmed' and S.get('phase') == 'promise_confirmed'
    _failed = _state.get('phase') == 'failed' and _state.get('attempts') == 2 and _state.get('proven_not_recorded') is True and S.get('phase') == 'promise_failed'
    if not (_confirmed or _failed) or (_confirmed and S.get('consent') != S['readback_token']) or (_failed and S.get('consent') != '') or vars.get('_native_close_armed'):
        raise ValueError('Uncertain, unconfirmed or repeated terminal entry')
    _witness = dict(binding=S['binding'], call_id=S['call_id'], customer_id=S['customer_id'], reservation_id=_context['reservation_id'], phase=S['phase'])
    vars['_native_terminal_path'] = json.dumps(_witness)
    vars['_native_close_armed'] = json.dumps(_witness)
    vars['_native_close_url'] = CLOSE_URL
    vars['_native_close_customer'] = S['customer_id']
    vars['_native_terminal_target'] = 'official_goodbye' if _confirmed else 'official_failure_goodbye'
except Exception:
    pass
'''


def build_official_native_workflow(brief_paths, tool_api_path, kit_path, *, initial_variables=None,
                                  identity_failure_branch=False, payment_date_negotiation=False, native_runtime_mode="host_exclusive"):
    if type(identity_failure_branch) is not bool or type(payment_date_negotiation) is not bool:
        raise FoundationError('OFFICIAL_NATIVE_BRANCH_OPTION_MUST_BE_BOOLEAN')
    validate_runtime_mode(native_runtime_mode)
    from orchestration.product_build import source_bundle, compile_bundle
    docs, api, contract = source_bundle(brief_paths, tool_api_path)
    compilation = compile_bundle(brief_paths, tool_api_path, docs, contract)
    candidate = build_native_workflow(compilation['compiled'], compilation['documents'], compilation['wording_bundle'],
        wording_authorization=compilation.get('wording_authorization'), initial_variables=initial_variables, native_runtime_mode=native_runtime_mode)
    cfg = _cfg(candidate)
    # This first complete graph is intentionally the supported single-product
    # promise branch, not the full brief's callbacks, hardship or multi-product.
    if cfg['facts']['product_limit'] != 1:
        raise FoundationError('OFFICIAL_NATIVE_PILOT_SINGLE_PRODUCT_REQUIRED')
    success = cfg['wording']['promise_success']['template']
    if set(__import__('re').findall(r'\{([^{}]+)\}', success)) not in (set(), {'product_label'}):
        raise FoundationError('OFFICIAL_NATIVE_SUCCESS_TEMPLATE_UNSUPPORTED')
    kit = Path(kit_path)
    generic = compilation.get('scope') in ('bounded-brief-requirements-1', 'bounded_explicit_typed_source')
    if generic and (identity_failure_branch or payment_date_negotiation):
        raise FoundationError('SOURCE_POLICY_NATIVE_PRACTICE_EXTENSIONS_UNSUPPORTED')
    source_names = {'workflow_format': 'docs/workflow-format.md',
        'tools_and_code': 'docs/tools-and-code.md', 'base_agent': 'examples/base-agent.json'}
    if not generic:
        source_names = {'brief': 'briefs/brief-a.md', **source_names}
    official = {key: read_source(kit / path) for key, path in source_names.items()}
    rule37 = '37. Say the promise is recorded only after `create_promise` returns `ok: true`. If it fails, try once more. If it fails again, say you could not record it and close as `no_promise` / `service_error`.'
    if (not generic and rule37 not in official['brief'].text) or cfg['facts']['promise_max_attempts'] != 2:
        raise FoundationError('OFFICIAL_NATIVE_RULE37_CONTRACT_REQUIRED')
    closure_authority = dict(source_policy=compilation['compiled'], policy_documents=docs,
        farewell_authority=compilation['wording_bundle'], source_scope=compilation['scope']) if generic else {}
    contract.validate_request('close_call', {'customer_id': 'local-schema-fixture', 'outcome': 'no_promise', 'reason': 'service_error'})
    closure = compile_official_closure(official, contract, outcome='promise', customer_id_variable='customer_id',
        farewell=cfg['wording']['goodbye']['template'], **closure_authority)
    # Conversation tools are retained; end_call nodes silently lose tools.
    # This documented placement plus technical ending requires the separate
    # native conversation-close probe before publication/banking admission.
    node_quote = "A lifecycle tool in a node's `tools` runs for `start_of_call` only on the `initial_node`, and for `end_of_call` only on the node in which the call ended."
    placement_quote = '`tools` is kept only on `conversation` nodes.'
    if node_quote not in official['tools_and_code'].text or placement_quote not in official['workflow_format'].text:
        raise FoundationError('OFFICIAL_CONVERSATION_LIFECYCLE_CONTRACT_REQUIRED')
    failure_closure = compile_official_closure(official, contract, outcome='no_promise', reason='service_error',
        customer_id_variable='customer_id', farewell=cfg['wording']['goodbye']['template'], **closure_authority)
    write = make_write_hooks(contract, source_binding=cfg['binding'],
        business_weekdays=cfg['facts']['business_weekdays'], holidays=cfg['facts']['holidays'], native_runtime_mode=native_runtime_mode)
    graph = copy.deepcopy(candidate['workflow'])
    nodes = graph['nodes']
    for name in ('native_boot', 'native_turn', 'native_observe'):
        properties = nodes[name]['pre_actions'][0]['function']['parameters']['properties']
        properties['code']['default'] = _bind_scalar_read_result(_bind_driver_state_in_place(properties['code']['default'])) + _METADATA
    nodes['native_boot']['pre_actions'][0]['function']['parameters']['properties']['code']['default'] += "\nvars['_native_close_url'] = ''\nvars['_native_close_customer'] = ''\nvars['_native_terminal_target'] = 'native_blocked'\n"
    for name in ('verify_identity', 'get_account'):
        tool = nodes['bank_' + name]['pre_actions'][0]
        # Documented config rewrite, never an assumed exception-based abort.
        clear = "vars['_native_http_json'] = ''\nvars['bank_" + name + "_response.data._native_http_json'] = ''\n"
        tool['pre_process_code'] = clear + 'try:\n' + textwrap.indent(tool['pre_process_code'], '    ') + "\nexcept Exception:\n    config['url'] = ''\n    config['body'] = dict()\n    config['headers'] = dict()\n"
        tool['post_process_code'] = _READ_POST
        nodes['bank_' + name]['response_mapping'] = [{'path': 'data._native_http_json', 'variable': '_native_http_json'}]
    authorize = _AUTHORIZE.replace('BINDING', repr(cfg['binding'])).replace('IDENTITY_ORDER', repr(cfg['facts']['identity_order']))
    authorize = native_scope_code(authorize, native_runtime_mode)
    nodes['native_write_gate'] = _code_node('native_write_gate', authorize,
        [_edge('bank_create_promise', '_native_route', 'bank_create_promise'), _edge('native_blocked')])
    tool = copy.deepcopy(write['tool'])
    mapping = tool.pop('response_mapping')
    current_authority = '''import json
_current = False
try:
    S = json.loads(vars.get('_native_state_json', ''))
    A = json.loads(vars.get('_native_write_authorization', ''))
    _draft_payload = dict(customer_id=S['customer_id'], product_id=S['draft']['product_id'], amount=S['draft']['amount'], date=S['draft']['date'])
    _current = S.get('binding') == BINDING and S.get('phase') == 'write_pending' and S.get('call_id') == vars.get('call.call_id') and S.get('customer_id') == vars.get('customer_id') and S.get('now') == vars.get('now') and S.get('verified') == IDENTITY_ORDER and bool(S.get('readback_token')) and S.get('consent') == S.get('readback_token') == A.get('readback_token') and A.get('payload') == _draft_payload and S.get('product') == A.get('account_snapshot')
except Exception:
    pass
if _current:
'''.replace('BINDING', repr(cfg['binding'])).replace('IDENTITY_ORDER', repr(cfg['facts']['identity_order']))
    current_authority = native_scope_code(current_authority, native_runtime_mode)
    tool['pre_process_code'] = current_authority + textwrap.indent(tool['pre_process_code'], '    ') + '''
else:
    vars['_native_write_route'] = 'blocked'
    vars['_native_write_outcome'] = 'unknown'
    vars['_native_transport_outcome'] = 'unknown'
    config['url'] = ''
    config['body'] = dict()
    config['headers'] = dict()
'''
    nodes['bank_create_promise'] = {'name': 'bank_create_promise', 'node_type': 'api_request',
        'on_enter': {'type': 'silent'}, 'passthrough': True, 'pre_actions': [tool], 'response_mapping': mapping,
        'passthrough_default_transition': 'native_write_result', 'transitions': [_edge('native_write_result')]}
    result = write['result_guard_code'] + native_scope_code(_RESULT, native_runtime_mode).replace('BINDING', repr(cfg['binding'])).replace('SUCCESS_TEMPLATE', repr(success)).replace(
        'PRODUCT_LABEL', repr(cfg['flow'].get('context_recipe', {}).get('product_label')))
    nodes['native_write_result'] = _code_node('native_write_result', result, [
        _edge('bank_create_promise', '_native_route', 'bank_create_promise'),
        _edge('native_success', '_native_route', 'native_success'), _edge('native_blocked')])
    nodes['native_success'] = {'name': 'native_success', 'node_type': 'conversation', 'on_enter': {'type': 'tts', 'text': '{{_native_speech}}'},
        'task_message': 'Speak only the supplied exact source wording. No tools or added wording.', 'passthrough': True,
        'passthrough_default_transition': 'native_terminal_arm', 'transitions': [_edge('native_terminal_arm')]}
    arm = native_scope_code(_TERMINAL_ARM, native_runtime_mode).replace('BINDING', repr(cfg['binding'])).replace('IDENTITY_ORDER', repr(cfg['facts']['identity_order'])).replace('CLOSE_URL', repr(contract.operations['close_call'].url))
    nodes['native_terminal_arm'] = _code_node('native_terminal_arm', arm, [
        _edge('official_goodbye', '_native_terminal_target', 'official_goodbye'),
        _edge('official_failure_goodbye', '_native_terminal_target', 'official_failure_goodbye'), _edge('native_blocked')])
    graph['tools'] = []
    for name, components in [('official_goodbye', closure), ('official_failure_goodbye', failure_closure)]:
        # Same technical handler/configuration shape as the isolated native
        # conversation-close diagnostic; static outcome avoids unsupported
        # global optional-leaf omission and whole-object variable coercion.
        end = {'type': 'function', 'config': {'maxRetries': 0, 'timeoutMs': 5000},
            'function': copy.deepcopy(components['terminal_node']['terminal']['function'])}
        end['function']['description'] = 'Invoke official_farewell_end silently; the handler supplies the farewell.'
        close = copy.deepcopy(components['lifecycle_tool_template'])
        properties = close['function']['parameters']['properties']
        properties['url']['default'] = '{{_native_close_url}}'
        properties['body']['properties']['customer_id']['default'] = '{{_native_close_customer}}'
        close.pop('pre_process_code', None)
        close.pop('post_process_code', None)
        graph['nodes'][name] = {'name': name, 'node_type': 'conversation', 'on_enter': {'type': 'llm'},
            'task_message': 'Invoke official_farewell_end silently. The handler supplies the farewell. Call no other tool.',
            'disable_inactivity_monitor': True, 'tools': [end, close], 'transitions': []}
    abort = copy.deepcopy(nodes['official_goodbye'])
    abort['name'] = 'native_blocked'
    abort['tools'] = abort['tools'][:1]
    nodes['native_blocked'] = abort
    _compact_edge_descriptions(graph)
    # Drop only redundant EMPTY optional fields, preserving code, source policy,
    # transitions, parameter schemas, static outcomes and all real guards.
    for node in graph['nodes'].values():
        for slot in ('tools', 'pre_actions'):
            if node.get(slot) == []:
                node.pop(slot)
            for action in node.get(slot, []):
                for field in ('pre_process_code', 'post_process_code'):
                    if action.get(field) == '':
                        action.pop(field)
                # Actual HTTP201 fills omitted descriptions with empty strings.
                # Emit that default explicitly; exact action readback stays strict.
                action['function'].setdefault('description', '')
    # Extraction receives current controlled context, never silently invents a
    # payment amount/date. Model extraction reliability is a live-test question.
    listening = graph['nodes']['native_listen']
    # The official variable schema owns this field, not the node. A measured
    # dryRun HTTP400 rejected the previous node-level placement.
    listening['extraction_schema'][0]['extraction_prompt'] = '''{{extractor.type_preamble}}
Extract the latest customer's answer, not old answers: {{extractor.speech}}
Conversation context: {{extractor.conversation.last-10}}
Return only the requested variables: {{extractor.variables_list}}
Current phase: {{_native_phase}}; identity field: {{_native_identity_field}}.
Current proposal product: {{_native_proposal_product_id}}, offered amount: {{_native_suggested_amount}}, offered date: {{_native_suggested_date}}.
Current readback token: {{_native_readback_token}}.
Return _native_turn_json as a JSON string of exactly one of these objects:
person_confirm: kind and explicit_yes boolean for the right-person question.
identity_answer: kind, field=current identity field, answer=current spoken answer. Convert a fully stated date of birth to YYYY-MM-DD, preserve four telephone digits.
proposal: kind, product_id=current proposal product, amount=numeric and date=YYYY-MM-DD. Explicit acceptance of the full payment offered for today binds to the current offered amount/date. Never accept silence or invent another offer.
confirm: kind, readback_token=current token, explicit_yes=true only for an explicit confirmation of the readback.
Ambiguous, unsupported or contradictory answers: kind=unsupported.
Do not speak, select tools or make policy decisions.'''
    listening['extraction_schema'][0]['description'] = 'JSON string of the current structured answer, following the extraction prompt. Never reuse a previous answer.'
    # Empty is the controller's consumed-input sentinel, not a customer answer.
    # The live schema rejected that clear with min1; stale input must not survive.
    # parse_turn still rejects empty input before any policy or bank action.
    listening['extraction_schema'][0]['validation_pattern'] = '^[\\s\\S]{0,2048}$'
    candidate['workflow'] = graph
    candidate['config'] = cfg
    candidate['closure'] = closure
    candidate['terminal_contract'] = {'scope': 'closing conversation technical end then same-node end_of_call; live probe pending',
        'source': official['tools_and_code'].inventory(), 'quote': node_quote,
        'placement_source': official['workflow_format'].inventory(), 'placement_quote': placement_quote,
        'global_pre_hook_required': False, 'device_playback_ack_required': False,
        'external_mid_farewell_hangup_verified': False, 'arbitrary_restart_supported': False}
    if generic:
        candidate['definitive_failure_binding'] = {
            'source': [doc.inventory() for doc in docs],
            'source_references': [ref for ref in closure['source_references'] if ref['role'].startswith('original_policy')],
            'source_derived_parameters': {'promise_max_attempts': cfg['facts']['promise_max_attempts'],
                'failure_outcome': 'no_promise', 'failure_reason': 'service_error'},
            'speech': 'I could not record the promise.', 'literal_official_sentence_claim': False,
            'required_result': 'two documented HTTP200 ok:false responses; never unknown',
            'practice_brief_or_public_fixture_used': False}
    else:
        candidate['definitive_failure_binding'] = {'source': official['brief'].inventory(), 'quote': rule37,
            'speech': 'I could not record the promise.', 'literal_official_sentence_claim': False,
            'required_result': 'two documented HTTP200 ok:false responses; never unknown'}
    candidate['write_plan'] = {key: value for key, value in write.items() if key not in ('tool', 'result_guard_code')}
    candidate['readiness'] = {'scope': 'fresh exclusive single-product promise call; full brief and restart not supported',
        'decision': 'IMPLEMENTABLE', 'bank_writes_enabled': False, 'publish_allowed': False,
        'actual_native_closure_verified': False, 'actual_conversation_technical_end_verified': False, 'actual_hook_abort_verified': False,
        'platform_schema_validated': False, 'native_atomic_persistence_claim': False,
        'provider_idempotency_claim': False, 'automatic_restart_allowed': False,
        'required_live_authority': ['matching engine lifecycle probe', 'matching URL validation probe',
            'fresh host-exclusive customer reservation', 'real native call ID on this graph', 'version and fixed model readback'],
        'unsupported': ['arbitrary native restart with reset variables', 'multi_product', 'callbacks', 'transfers', 'all brief branches']}
    if native_runtime_mode == 'official_bank_session':
        from .native_freya import specialize_native_action
        spec = graph['nodes']['native_boot']['pre_actions'][0]['function']['parameters']['properties']['code']
        spec['default'] = specialize_native_action(spec['default'], 'boot')
        if identity_failure_branch:
            from .native_branch_extensions import extend_identity_failure
            candidate = extend_identity_failure(candidate, official, contract)
        if payment_date_negotiation:
            from .native_payment_negotiation import extend_payment_dates
            candidate = extend_payment_dates(candidate, official)
        graph = candidate['workflow']
        from .native_freya import specialize_native_action
        for name, action in [('native_boot', 'boot'), ('native_turn', 'turn'), ('native_observe', 'observe')]:
            spec = graph['nodes'][name]['pre_actions'][0]['function']['parameters']['properties']['code']
            spec['default'] = specialize_native_action(spec['default'], action)
    compression = []
    for node_name, node in graph['nodes'].items():
        for slot in ('pre_actions', 'tools'):
            for action in node.get(slot, []):
                function = action['function']
                fields = [(function['parameters']['properties']['code'], 'default', 'code')] if function['handler'] == '__function__:code_exec' else [(action, field, field) for field in ('pre_process_code', 'post_process_code') if field in action]
                for container, key, label in fields:
                    code = container[key]
                    if '{{' in code or '}}' in code:
                        raise FoundationError('NATIVE_CODE_TEMPLATE_MARKER_FORBIDDEN')
                    reduced = _compress_generated_python(code)
                    if reduced != code:
                        compression.append({'node': node_name, 'slot': slot, 'field': label,
                            'before_bytes': len(code.encode()), 'after_bytes': len(reduced.encode()),
                            'AST_equivalent': True, 'before_sha256': hashlib.sha256(code.encode()).hexdigest(),
                            'after_sha256': hashlib.sha256(reduced.encode()).hexdigest()})
                    container[key] = reduced
                    compile(reduced, '<generated-native>', 'exec')
    candidate['generated_python_compression'] = {'policy': 'comment tokens and blank-only lines outside strings',
        'source_policy_and_wording_changed': False, 'snippets': compression}
    budget = measure_code_exec_budget(graph, initial_variables or {}, reserve_variable_bytes=16384)
    wire = _json(graph).encode()
    if not budget['passed'] or len(wire) >= 131072:
        raise FoundationError('OFFICIAL_NATIVE_WORKFLOW_BYTE_BUDGET_EXCEEDED')
    candidate['budget'] = dict(budget, whole_workflow_compact_bytes=len(wire), whole_workflow_limit_bytes=131072,
        exact_full_api_request_measured=False)
    candidate['workflow_sha256'] = hashlib.sha256(wire).hexdigest()
    candidate['input_inventory'] = [doc.inventory() for doc in docs] + [api.inventory()]
    if identity_failure_branch and native_runtime_mode == 'host_exclusive':
        from .native_branch_extensions import extend_identity_failure
        candidate = extend_identity_failure(candidate, official, contract)
    if payment_date_negotiation and native_runtime_mode == 'host_exclusive':
        from .native_payment_negotiation import extend_payment_dates
        candidate = extend_payment_dates(candidate, official)
    if native_runtime_mode == 'official_bank_session':
        # Extensions also execute under the selected nullable-native-ID profile.
        for node in candidate['workflow']['nodes'].values():
            for action in node.get('pre_actions', []) + node.get('tools', []):
                fn = action['function']
                if fn.get('handler') == '__function__:code_exec':
                    spec = fn['parameters']['properties']['code']
                    spec['default'] = native_scope_code(spec['default'], native_runtime_mode)
                for field in ('pre_process_code', 'post_process_code'):
                    if field in action:
                        action[field] = native_scope_code(action[field], native_runtime_mode)
        candidate['native_runtime_mode'] = native_runtime_mode
        candidate['readiness']['scope'] = 'One uninterrupted fresh official bank session; inline replay protection only; no cold-state-loss recovery'
        candidate['readiness']['required_live_authority'] = ['matching engine lifecycle probe', 'matching URL validation probe', 'fresh official bank session customer_id and now', 'fixed model and version readback']
        candidate['write_plan']['requires_real_platform_call_id'] = False
        candidate['write_plan']['requires_controller_exclusive_customer_reservation'] = False
        candidate['budget'] = measure_code_exec_budget(candidate['workflow'], initial_variables or {}, reserve_variable_bytes=16384)
        candidate['workflow_sha256'] = hashlib.sha256(_json(candidate['workflow']).encode()).hexdigest()
    return candidate
