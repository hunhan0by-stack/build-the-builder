"""Source-bound ISO-date counteroffers, never implicit payment authorization.

Supports only Brief A rules20/23's definite-date negotiation. Vague dates,
relative weekday parsing, floor/refusal negotiation and full case coverage
remain explicitly unsupported. Native playback and platform PASS are unproven.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import asdict
import hashlib
import json

from .models import FoundationError
from .native_freya import _RUNTIME, measure_code_exec_budget


RULE20 = '20. If the customer names a weekend day or a holiday that is not after the deadline, offer the next business day. If the named day is after the deadline, rule 23 applies.'
RULE23 = '23. If the customer asks for a date after the deadline, say the latest date you can record is the deadline and offer it. If the customer refuses the deadline, treat it as a flat refusal (rule 30).'


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _reference(document, quote):
    if _sha(document.text.encode()) != document.sha256:
        raise FoundationError('PAYMENT_DATE_SOURCE_HASH_CHANGED')
    start = document.text.find(quote)
    if start < 0 or document.text.find(quote, start + 1) >= 0:
        raise FoundationError('PAYMENT_DATE_EXACT_RULE_REQUIRED')
    return dict(asdict(document.evidence(start, start + len(quote))), quote=quote,
                path=document.path, sha256=document.sha256)


def _specialize_turn(tree):
    actions = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'ACTION' for t in n.targets)]
    steps = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'step']
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'step']
    if (len(actions) != 1 or ast.literal_eval(actions[0].value) != 'turn' or len(steps) != 1
            or len(steps[0].body) != 1 or len(calls) != 1 or ast.unparse(calls[0]) != 'step(ACTION)'):
        raise FoundationError('PAYMENT_DATE_LITERAL_TURN_DRIVER_REQUIRED')
    branch = steps[0].body[0]
    while isinstance(branch, ast.If) and ast.unparse(branch.test) != "action == 'turn'":
        branch = branch.orelse[0] if len(branch.orelse) == 1 else None
    if not isinstance(branch, ast.If):
        raise FoundationError('PAYMENT_DATE_TURN_BODY_REQUIRED')
    unchanged = ast.dump(ast.Module(body=copy.deepcopy(branch.body), type_ignores=[]), include_attributes=False)
    steps[0].body = copy.deepcopy(branch.body)
    after = ast.dump(ast.Module(body=steps[0].body, type_ignores=[]), include_attributes=False)
    if unchanged != after:
        raise FoundationError('PAYMENT_DATE_SPECIALIZATION_CHANGED_TURN')
    return tree


def extend_payment_dates(candidate, documents, *,
        publication_name='Native-A-Dates 00000000-0000-0000-0000-000000000000'):
    """Return a candidate with only native_turn's propose/date behavior changed.

    Amount bounds, API payload validation, identity/current-call gates, bank
    hooks, confirmation and closure remain identical. A counteroffer never
    creates a draft/readback token or banking request. Publication is separate.
    """
    if candidate.get('payment_date_extension') or candidate.get('config', {}).get('facts', {}).get('product_limit') != 1:
        raise FoundationError('PAYMENT_DATE_FRESH_SINGLE_PRODUCT_SCOPE_REQUIRED')
    refs = [_reference(documents['brief'], rule) for rule in (RULE20, RULE23)]
    out = copy.deepcopy(candidate)
    code_slot = out['workflow']['nodes']['native_turn']['pre_actions'][0]['function']['parameters']['properties']['code']
    before = code_slot['default']
    tree = _specialize_turn(ast.parse(before))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'propose']
    original = next(n for n in ast.parse(_RUNTIME).body if isinstance(n, ast.FunctionDef) and n.name == 'propose')
    if len(functions) != 1 or ast.dump(functions[0], include_attributes=False) != ast.dump(original, include_attributes=False):
        raise FoundationError('PAYMENT_DATE_EXACT_PROPOSAL_GUARDS_REQUIRED')
    proposal = functions[0]
    calendar_guard = next((n for n in proposal.body if isinstance(n, ast.If)
        and ast.unparse(n.test) == "not business(target) or not day(S['now'][:10]) <= target <= day(p['deadline'])"), None)
    payload = next((n for n in proposal.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'payload' for t in n.targets)), None)
    if calendar_guard is None or payload is None:
        raise FoundationError('PAYMENT_DATE_EXACT_CALENDAR_AND_PAYLOAD_SLOTS_REQUIRED')
    # Determine an offer using source facts and all compiled date restrictions.
    # The later, untouched constraint loop still validates the amount ceiling.
    date_logic = ast.parse('''
_requested = target
_first, _last = day(S['now'][:10]), day(p['deadline'])
for _rule in CFG['constraints']:
    if 'create_promise' not in _rule['operations']:
        continue
    if _rule['target'] == 'payment.date_on_or_before':
        _last = min(_last, day(_rule['value']))
    elif _rule['target'] == 'payment.date_on_or_after':
        _first = max(_first, day(_rule['value']))
if target < _first or _last < _first:
    raise ValueError('Earlier-than-source date negotiation unsupported')
_late = target > _last
if _late:
    target = _last
    for _ in range(370):
        if business(target):
            break
        target -= datetime.timedelta(days=1)
else:
    for _ in range(370):
        if business(target):
            break
        target += datetime.timedelta(days=1)
if not _first <= target <= _last or not business(target):
    raise ValueError('No source-permitted date counteroffer')
''').body
    index = proposal.body.index(calendar_guard)
    proposal.body[index:index + 1] = date_logic
    counter = ast.parse('''
if target != _requested:
    _date = target.isoformat()
    checked(dict(customer_id=S['customer_id'], product_id=event['product_id'], amount=event['amount'], date=_date), CFG['operations']['create_promise']['request'], True)
    p['suggested_amount'], p['suggested_date'] = event['amount'], _date
    CFG['wording']['date_counteroffer'] = LATE_WORDING if _late else CALENDAR_WORDING
    emit('date_counteroffer', context())
    S['date_counteroffer'] = dict(product_id=event['product_id'], amount=event['amount'], date=_date)
    return
'''.replace('LATE_WORDING', repr({'template': 'The latest date I can record is {date}. Can you make this payment on {date}?', 'evidence': [{k: v for k, v in refs[1].items() if k != 'path'}], 'origin': 'derived_from_rule_23'}))
       .replace('CALENDAR_WORDING', repr({'template': 'That date is not a business day. Can you make this payment on {date}?', 'evidence': [{k: v for k, v in refs[0].items() if k != 'path'}], 'origin': 'derived_from_rule_20'}))).body
    # After the source constraint loop, before any original draft is constructed.
    index = proposal.body.index(payload)
    proposal.body[index:index] = counter
    lowered = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    compile(lowered, '<native-date-counteroffer>', 'exec')
    code_slot['default'] = lowered
    budget = measure_code_exec_budget(out['workflow'], {}, reserve_variable_bytes=16384)
    wire = json.dumps({'name': publication_name, 'workflow': out['workflow']}, allow_nan=False).encode()
    if not budget['passed'] or len(wire) >= 131072:
        raise FoundationError('PAYMENT_DATE_FULL_PUBLICATION_BYTE_LIMIT')
    out['workflow_sha256'] = _sha(json.dumps(out['workflow'], sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(',', ':')).encode())
    out['budget'] = dict(budget, whole_workflow_compact_bytes=len(json.dumps(out['workflow'],
        sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()),
        whole_workflow_limit_bytes=131072, full_publication_body_bytes=len(wire),
        publication_name_is_size_fixture=True, full_publication_limit=131072,
        exact_full_api_request_measured=False)
    out['payment_date_extension'] = {
        'decision': 'IMPLEMENTABLE_LOCAL_ONLY', 'scope': 'Exact ISO late and nonbusiness-date counteroffers only',
        'source_references': refs, 'base_workflow_sha256': candidate['workflow_sha256'],
        'original_turn_sha256': _sha(before.encode()), 'extended_turn_sha256': _sha(lowered.encode()),
        'literal_turn_AST_specialization_before_proposal_delta': True,
        'bank_or_confirmation_guards_changed': False, 'other_workflow_nodes_changed': False,
        'date_counteroffer_authorizes_payment': False, 'platform_PASS': False,
        'official_full_PUB04_PASS': False, 'official_full_PUB07_PASS': False,
        'unsupported': ['vague date clarification', 'relative weekday language', 'refused deadline persuasion',
                        'below-floor negotiation', 'hardship or AI/scam interjections', 'multi-product', 'arbitrary restart'],
        'network_calls': 0}
    return out


def payment_date_evidence(candidate, fixture_cases):
    """Replay definite ISO-date subflows from archived official case inputs.

    Does not simulate each complete persona conversation. Expectations are
    frozen from the official fixture before the emitted graph executes.
    """
    import tempfile
    from .native_kit_verification import OfficialNativeHarness, local_http, local_variables
    requests = {'PUB-04': '2026-11-06', 'PUB-07': '2026-10-26'}
    cases = {row['id']: copy.deepcopy(row) for row in fixture_cases}
    if (len(fixture_cases) != 2 or set(cases) != set(requests)
            or not candidate.get('payment_date_extension')):
        raise FoundationError('PAYMENT_DATE_ARCHIVED_TWO_FIXTURES_REQUIRED')
    checks, traces = [], {}
    for case_id in requests:
        case = cases[case_id]
        expected = copy.deepcopy(case['expected']['promises'][0])
        with tempfile.TemporaryDirectory(prefix='native-date-evidence-') as registry:
            variables = local_variables(registry)
            variables.update(case['variables'])
            h = OfficialNativeHarness(candidate, variables)
            h.start()
            if h.state['phase'] == 'person_confirm':
                h.receive({'kind': 'person_confirm', 'explicit_yes': True})
            for field in candidate['config']['facts']['identity_order']:
                h.receive({'kind': 'identity_answer', 'field': field, 'answer': case['identity'][field]})
                h.dispatch('verify_identity')
                h.resume('verify_identity', local_http({'ok': True, 'verified': True}))
            h.dispatch('get_account')
            h.resume('get_account', local_http({'ok': True, 'products': [dict(case['products'][0], product_id='p1')]}))
            h.receive({'kind': 'proposal', 'product_id': 'p1', 'amount': expected['amount'], 'date': requests[case_id]})
            offer = {'phase': h.state['phase'], 'draft': copy.deepcopy(h.state['draft']),
                     'consent': h.state['consent'], 'readback_token': h.state['readback_token'],
                     'tts': copy.deepcopy(h.tts_events[-1]), 'bank_count': len(h.requests)}
            try:
                h.dispatch('create_promise')
                blocked_before_acceptance = False
            except FoundationError:
                blocked_before_acceptance = True
            h.receive({'kind': 'proposal', **expected})
            before_confirmation = len(h.requests)
            h.receive({'kind': 'confirm', 'readback_token': h.state['readback_token'], 'explicit_yes': True})
            bank = h.dispatch('create_promise')
            h.resume('create_promise', local_http({'ok': True}))
            h.complete_terminal_fixture()
            close = h.dispatch_lifecycle_fixture()
            wording = ('The latest date I can record is {date}. Can you make this payment on {date}?'
                       if case_id == 'PUB-04' else 'That date is not a business day. Can you make this payment on {date}?')
            values = {
                'no_automatic_authorization': offer['phase'] == 'proposal' and offer['draft'] == {}
                    and not offer['consent'] and not offer['readback_token'] and blocked_before_acceptance,
                'source_date_actually_spoken': offer['tts']['node'] == 'native_speak'
                    and offer['tts']['text'] == wording.format(date=expected['date']),
                'fresh_readback_and_explicit_consent': before_confirmation == offer['bank_count']
                    and bank['body'] == dict(customer_id=h.variables['customer_id'], **expected),
                'one_promise_and_last_close': [r['operation'] for r in h.requests]
                    == ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']
                    and close['body'] == {'customer_id': h.variables['customer_id'], 'outcome': 'promise'}
                    and not h.dispatch_lifecycle_fixture().get('url'),
            }
            checks.extend({'id': case_id + '_ISO_' + key, 'passed': bool(value)} for key, value in values.items())
            traces[case_id] = {'events': h.state['events'], 'tts_events': h.tts_events,
                              'requests': h.requests, 'traversal': h.traversal,
                              'terminal_fixture': h.terminal_fixture, 'code_measurements': h.code_measurements}
    return {'passed': all(row['passed'] for row in checks), 'checks': checks, 'traces': traces,
            'fixture_scope': 'PUB-04 and PUB-07 definite ISO-date subflows only; synthetic bank and completion fixtures, not complete personas or official evaluation',
            'platform_PASS': False, 'network_calls': 0}
