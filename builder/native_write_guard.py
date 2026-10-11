"""Documented-hook prototype for one fresh, controller-exclusive native call.

No native durable storage or provider idempotency is claimed. Publication still
needs real hook-order/abort validation and the separate native lifecycle gates.
The host registry refuses reused customer scopes, including process restarts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from .models import FoundationError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def reserve_single_call(directory, *, workspace_id, customer_id, operation_id=None):
    """HOST-only, irreversible exclusive reservation; never an engine call ID.

All controllers for this bank scope must share this trusted registry. Another
registry/host could evade the protection; this is not global provider dedup.
No automatic release or restart/new-call admission follows an unknown result.
"""
    if any(not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 512
           for value in (workspace_id, customer_id)):
        raise FoundationError('EXPLICIT_BANK_SCOPE_REQUIRED')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise FoundationError('TRUSTED_REAL_HOST_REGISTRY_REQUIRED')
    scope = _json({'workspace_id': workspace_id, 'customer_id': customer_id})
    target = directory / (hashlib.sha256(scope.encode()).hexdigest() + '.json')
    operation_id = operation_id or str(uuid.uuid4())
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise FoundationError('CANONICAL_LOCAL_OPERATION_ID_REQUIRED') from None
    context = {'version': 1, 'customer_id': customer_id, 'workspace_id': workspace_id,
        'reservation_id': operation_id, 'restart_policy': 'never_automatically_resume_or_reinitialize',
        'authority': 'host_exclusive_customer_reservation', 'provider_idempotency': False}
    try:
        with target.open('xb') as stream:
            record = {'version': 1, 'scope_sha256': hashlib.sha256(scope.encode()).hexdigest(),
                      'operation_id': operation_id, 'state': 'reserved_never_automatically_release'}
            stream.write((_json(record) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise FoundationError('CUSTOMER_SCOPE_ALREADY_RESERVED_NO_NEW_CALL') from None
    # This local identifier correlates the admission only; never call.call_id.
    state = {'version': 1, 'reservation_id': context['reservation_id'], 'phase': 'ready',
             'attempts': 0, 'call_id': None, 'payload_json': None, 'proven_not_recorded': False}
    return {'_native_write_context': _json(context), '_native_write_state': _json(state)}


_PRE = r'''
import json, re, datetime
vars['_native_write_route'] = 'blocked'
vars['_native_write_outcome'] = 'unknown'
vars['_native_transport_outcome'] = 'unknown'
_allowed = False
try:
    _context = json.loads(vars.get('_native_write_context', ''))
    _state = json.loads(vars.get('_native_write_state', ''))
    _call_id = vars.get('call.call_id')
    _body = config.get('body')
    _authorization = json.loads(vars.get('_native_write_authorization', ''))
    _cid = _body.get('customer_id') if isinstance(_body, dict) else None
    if not isinstance(_call_id, str) or not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', _call_id) or _call_id == '00000000-0000-0000-0000-000000000000':
        raise ValueError('Missing real platform identity')
    if _context.get('authority') != 'host_exclusive_customer_reservation' or _context.get('provider_idempotency') is not False or not _context.get('reservation_id') or _state.get('reservation_id') != _context.get('reservation_id') or _cid != _context.get('customer_id'):
        raise ValueError('Missing exclusive controller context')
    if _context.get('restart_policy') != 'never_automatically_resume_or_reinitialize':
        raise ValueError('Unsupported restart policy')
    if config.get('url') != EXPECTED_URL or config.get('method') != 'POST' or not isinstance(_body, dict) or set(_body) != set(['customer_id', 'product_id', 'amount', 'date']):
        raise ValueError('Wrong operation or body shape')
    if not isinstance(_body['product_id'], str) or not _body['product_id'] or type(_body['amount']) not in (int, float) or _body['amount'] <= 0 or not isinstance(_body['date'], str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', _body['date']):
        raise ValueError('Invalid guarded body')
    if _authorization.get('source_binding') != SOURCE_BINDING or _authorization.get('call_id') != _call_id or _authorization.get('customer_id') != _cid or _authorization.get('payload') != _body or _authorization.get('identity_verified') is not True or _authorization.get('explicit_consent') is not True:
        raise ValueError('Unbound source or unconfirmed payment authorization')
    _token = _authorization.get('readback_token')
    if not isinstance(_token, str) or not _token or _authorization.get('consent_token') != _token:
        raise ValueError('Consent is not current')
    def _cents(_number):
        if type(_number) not in (int, float) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', str(_number)):
            raise ValueError('Invalid monetary precision')
        _parts = str(_number).split('.')
        return int(_parts[0]) * 100 + int((_parts[1] if len(_parts) == 2 else '').ljust(2, '0'))
    if not _cents(_authorization.get('account_floor')) <= _cents(_body['amount']) <= _cents(_authorization.get('account_past_due')):
        raise ValueError('Payment violates confirmed account bounds')
    _date = datetime.date.fromisoformat(_body['date'])
    _earliest = datetime.date.fromisoformat(_authorization['earliest_date'])
    _deadline = datetime.date.fromisoformat(_authorization['deadline'])
    if not _earliest <= _date <= _deadline or _date.weekday() not in BUSINESS_WEEKDAYS or _body['date'] in HOLIDAYS:
        raise ValueError('Payment violates source calendar or confirmed window')
    _payload = json.dumps(_body, sort_keys=True, separators=(',', ':'), allow_nan=False)
    _phase, _attempts = _state.get('phase'), _state.get('attempts')
    if type(_attempts) is not int:
        raise ValueError('Invalid attempt counter')
    if _phase == 'ready':
        if _attempts != 0 or _state.get('call_id') is not None or _state.get('payload_json') is not None:
            raise ValueError('Invalid initial state')
    elif _phase == 'retry_allowed':
        if _attempts != 1 or _state.get('proven_not_recorded') is not True or _state.get('call_id') != _call_id or _state.get('payload_json') != _payload:
            raise ValueError('No definitive same-call safe retry')
    else:
        raise ValueError('Dispatched, resolved or uncertain request is not repeatable')
    _state['call_id'] = _call_id
    _state['payload_json'] = _payload
    _state['attempts'] = _attempts + 1
    _state['phase'] = 'dispatched'
    _state['proven_not_recorded'] = False
    vars['_native_write_state'] = json.dumps(_state, sort_keys=True, separators=(',', ':'), allow_nan=False)
    vars['_native_write_route'] = 'dispatched'
    _allowed = True
except Exception:
    pass
if not _allowed:
    # No undocumented hook exception/cancel return is used. URL validation
    # must be tested in the real engine before this prototype is admitted.
    config['url'] = ''
    config['body'] = dict()
    config['headers'] = dict()
'''

_POST = r'''
_outcome = 'unknown'
try:
    _status = result.get('status_code') if isinstance(result, dict) else None
    _data = result.get('data') if isinstance(result, dict) else None
    _headers = result.get('header') if isinstance(result, dict) else None
    _content_type = None
    if isinstance(_headers, dict):
        for _name, _value in _headers.items():
            if isinstance(_name, str) and _name.lower() == 'content-type':
                _content_type = _value
    _known_transport = isinstance(result, dict) and result.get('status') == 'success' and result.get('message') == 'Successfully executed POST request' and isinstance(_content_type, str) and _content_type.lower().split(';')[0].strip() == 'application/json'
    if _known_transport and type(_status) is int and _status == 200 and isinstance(_data, dict) and _data.get('ok') is True:
        _outcome = 'confirmed'
    elif _known_transport and type(_status) is int and _status == 200 and isinstance(_data, dict) and _data.get('ok') is False:
        _outcome = 'not_recorded'
    # A bare/wrapper 500 is not proof of the bank's documented HTTP500.
    # The kit does not document an error-envelope discriminator proving origin.
    # It stays unknown; no invented provider_verified bit is accepted here.
except Exception:
    pass
if isinstance(result, dict):
    if not isinstance(result.get('data'), dict):
        result['data'] = dict()
    result['data']['_native_transport_outcome'] = _outcome
'''

_RESULT_GUARD = r'''
import json
_was_dispatched = vars.get('_native_write_route') == 'dispatched'
vars['_native_write_outcome'] = 'unknown'
vars['_native_write_route'] = 'blocked'
try:
    _state = json.loads(vars.get('_native_write_state', ''))
    _context = json.loads(vars.get('_native_write_context', ''))
    if not _was_dispatched or _state.get('phase') != 'dispatched' or _state.get('call_id') != vars.get('call.call_id') or _state.get('reservation_id') != _context.get('reservation_id'):
        raise ValueError('Unbound or replayed result')
    _transport = vars.get('_native_transport_outcome', 'unknown')
    _outcome = 'confirmed' if _transport == 'confirmed' else 'unknown'
    _not_recorded = _transport == 'not_recorded'
    if _not_recorded:
        _outcome = 'retry_allowed' if _state.get('attempts') == 1 else 'failed'
    _state['phase'] = _outcome
    _state['proven_not_recorded'] = _not_recorded
    vars['_native_write_state'] = json.dumps(_state, sort_keys=True, separators=(',', ':'), allow_nan=False)
    vars['_native_write_outcome'] = _outcome
    vars['_native_write_route'] = _outcome
except Exception:
    # No missing state/context is initialized or repaired in the result node.
    pass
vars['_native_transport_outcome'] = 'unknown'
'''


def create_promise_hook_factory(contract, *, source_binding, business_weekdays, holidays, native_runtime_mode="host_exclusive"):
    """Produce inspectable hooks/tool, not a live-banking admission decision."""
    if not isinstance(source_binding, str) or not source_binding or not isinstance(business_weekdays, (tuple, list)) or not business_weekdays or any(type(day) is not int or not 0 <= day <= 6 for day in business_weekdays) or not isinstance(holidays, (tuple, list)):
        raise FoundationError('EXPLICIT_SOURCE_CALENDAR_BINDING_REQUIRED')
    operation = contract.operations.get('create_promise')
    if operation is None or operation.method != 'POST' or set(operation.request.get('properties', {})) != {
            'customer_id', 'product_id', 'amount', 'date'}:
        raise FoundationError('UNSUPPORTED_PROMISE_CONTRACT')
    note500 = operation.response_descriptions.get('500', '')
    proven500 = 'Nothing was recorded' in note500 and 'may be repeated' in note500
    validate_runtime_mode(native_runtime_mode)
    pre_source, result_guard = (_PRE, _RESULT_GUARD) if native_runtime_mode == 'host_exclusive' else _session_promise_profile(source_binding)
    pre = pre_source.replace('EXPECTED_URL', repr(operation.url)).replace('SOURCE_BINDING', repr(source_binding))
    pre = pre.replace('BUSINESS_WEEKDAYS', repr(list(business_weekdays))).replace('HOLIDAYS', repr(list(holidays)))
    post = _POST.replace('DOCUMENTED_500_NOT_RECORDED', repr(proven500))
    if any(marker in pre + post + result_guard for marker in ('{{', '}}')):
        raise FoundationError('UNSUPPORTED_HOOK_TEMPLATE_MARKER')
    body = {name: dict(prop, default='{{_native_arg_' + name + '}}')
            for name, prop in operation.request['properties'].items()}
    tool = {'type': 'function', 'config': {'maxRetries': 0, 'timeoutMs': 10000},
        'function': {'name': 'create_promise', 'handler': '__function__:api_call',
            'description': 'Record only the authorized, explicitly confirmed payment promise.',
            'parameters': {'type': 'object', 'properties': {
                'url': {'type': 'string', 'default': operation.url},
                'method': {'type': 'string', 'default': 'POST'},
                'connect_retries': {'type': 'number', 'default': 0},
                'headers': {'type': 'object', 'properties': {'Content-Type': {'type': 'string', 'default': 'application/json'}}},
                'body': {'type': 'object', 'properties': body, 'required': list(operation.request['required'])}}}},
        'pre_process_code': pre, 'post_process_code': post,
        'response_mapping': [{'path': 'data._native_transport_outcome', 'variable': '_native_transport_outcome'}]}
    return {'decision': 'IMPLEMENTABLE', 'tool': tool, 'result_guard_code': result_guard,
        'native_execution_verified': False, 'bank_writes_enabled': False,
        'requires_live_empty_url_abort_and_hook_order_proof': True,
        'requires_controller_exclusive_customer_reservation': native_runtime_mode == 'host_exclusive',
        'requires_real_platform_call_id': native_runtime_mode == 'host_exclusive',
        'business_authorization_required_upstream_and_rechecked': True,
        'native_durable_state_claim': False, 'provider_idempotency_claim': False,
        'automatic_restart_supported': False, 'maximum_provider_attempts': 2,
        'retry_only_definitive_not_recorded': True,
        'retry500_no_effect_documented_by_contract': proven500,
        'retry500_provider_origin_proven': False,
        'retry500_admitted_by_this_hook': False}


def make_write_hooks(contract, *, source_binding, business_weekdays, holidays, native_runtime_mode="host_exclusive"):
    return create_promise_hook_factory(contract, source_binding=source_binding,
        business_weekdays=business_weekdays, holidays=holidays, native_runtime_mode=native_runtime_mode)


NATIVE_RUNTIME_MODES = ('host_exclusive', 'official_bank_session')


def validate_runtime_mode(mode):
    if mode not in NATIVE_RUNTIME_MODES:
        raise FoundationError('NATIVE_RUNTIME_MODE_UNSUPPORTED')
    return mode


_SESSION_BOOT = r'''
if any(vars.get(k) for k in ('_native_state_json', '_native_write_context', '_native_write_state', '_native_write_journal')):
    raise ValueError('Retained session state cannot be resumed or reinitialized')
for k in ('customer_id', 'now'):
    if not isinstance(vars.get(k), str) or not vars[k].strip() or len(vars[k]) > 512:
        raise ValueError('Official bank session variables required')
datetime.datetime.fromisoformat(vars['now'])
native_id = vars.get('call.call_id') or None
if native_id is not None and (not isinstance(native_id, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', native_id) or native_id == '00000000-0000-0000-0000-000000000000'):
    raise ValueError('Invalid supplied native call identity')
scope = json.dumps(dict(source_binding=SOURCE_BINDING, customer_id=vars['customer_id'], now=vars['now'], native_call_id=native_id), sort_keys=True, separators=(',', ':'))
vars['_native_write_context'] = json.dumps(dict(version=1, authority='bank_session_in_call', customer_id=vars['customer_id'], scope_json=scope, restart_policy='never_automatically_resume_or_reinitialize', provider_idempotency=False))
vars['_native_write_state'] = json.dumps(dict(version=1, scope_json=scope, phase='ready', attempts=0, call_id=None, payload_json=None, proven_not_recorded=False))
'''


def bank_session_boot(source_binding):
    return _SESSION_BOOT.replace('SOURCE_BINDING', repr(source_binding))


def native_scope_code(code, mode, *, source_binding=None):
    """Generation-only authority profile. Default source code is untouched.

    A bank-session secret is not a native call ID. This inline journal protects
    an uninterrupted call, not total state loss or provider-level deduplication.
    """
    validate_runtime_mode(mode)
    if mode == 'host_exclusive':
        return code
    code = code.replace("(vars.get('call.call_id') or None)", '__OPTIONAL_NATIVE_ID__')
    code = code.replace("vars.get('call.call_id') or None", '__OPTIONAL_NATIVE_ID__')
    code = code.replace("vars.get('call.call_id')", "(vars.get('call.call_id') or None)")
    code = code.replace('__OPTIONAL_NATIVE_ID__', "(vars.get('call.call_id') or None)")
    code = code.replace('reservation_id', 'scope_json')
    code = code.replace('host_exclusive_customer_reservation', 'bank_session_in_call')
    code = code.replace("if not S.get('call_id') or S['call_id'] !=", "if S['call_id'] !=")
    # UUID shape is checked only if a genuine native ID is supplied.
    lines = code.splitlines(keepends=True)
    for i, line in enumerate(lines):
        for variable in ('provider_id', '_call_id', 'cid'):
            needle = 'if not isinstance(' + variable + ', str) or not re.fullmatch('
            if needle in line and line.rstrip().endswith(':'):
                prefix, condition = line.split('if ', 1)
                lines[i] = prefix + 'if ' + variable + ' is not None and (' + condition.rstrip()[:-1] + '):\n'
    return ''.join(lines)


def _session_promise_profile(binding):
    pre = native_scope_code(_PRE, 'official_bank_session')
    old = """    if _context.get('authority') != 'bank_session_in_call' or _context.get('provider_idempotency') is not False or not _context.get('scope_json') or _state.get('scope_json') != _context.get('scope_json') or _cid != _context.get('customer_id'):
        raise ValueError('Missing exclusive controller context')
    if _context.get('restart_policy') != 'never_automatically_resume_or_reinitialize':
        raise ValueError('Unsupported restart policy')"""
    new = """    _scope = json.dumps(dict(source_binding=SOURCE_BINDING, customer_id=vars.get('customer_id'), now=vars.get('now'), native_call_id=_call_id), sort_keys=True, separators=(',', ':'))
    if _context.get('authority') != 'bank_session_in_call' or _context.get('provider_idempotency') is not False or _context.get('scope_json') != _scope or _state.get('scope_json') != _scope or _cid != _context.get('customer_id') or _context.get('restart_policy') != 'never_automatically_resume_or_reinitialize':
        raise ValueError('Current inline bank session scope mismatch')"""
    if pre.count(old) != 1:
        raise FoundationError('EXACT_SESSION_ADMISSION_ANCHOR_REQUIRED')
    pre = pre.replace(old, new)
    result = native_scope_code(_RESULT_GUARD, 'official_bank_session')
    result = result.replace("_state.get('scope_json') != _context.get('scope_json')", "_state.get('scope_json') != _context.get('scope_json') or _context.get('scope_json') != json.dumps(dict(source_binding=" + repr(binding) + ", customer_id=vars.get('customer_id'), now=vars.get('now'), native_call_id=vars.get('call.call_id') or None), sort_keys=True, separators=(',', ':')) or _context.get('authority') != 'bank_session_in_call'")
    return pre, result
