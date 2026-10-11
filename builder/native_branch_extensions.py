"""Narrow source-bound native branches; local execution is not platform proof.

This extension completes Brief A rules 12/14 (PUB-03) without broadening
payment, transfer, callback, restart or Brief B multi-product admission.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import asdict
import hashlib
import json

from .models import FoundationError
from .native_freya import _edge, measure_code_exec_budget
from .native_official_closure import compile_official_closure


RULE12 = '12. A wrong answer may be asked again. Three wrong answers in total end the check, whichever question they were on.'
RULE14 = '14. After three wrong answers, do not state the debt. Tell the customer to use the mobile app or a branch. Close as `not_verified` / `failed`.'
TARGET = 'official_identity_failed_goodbye'


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _quote(document, quote):
    if _sha(document.text.encode('utf-8')) != document.sha256:
        raise FoundationError('IDENTITY_BRANCH_SOURCE_HASH_CHANGED')
    start = document.text.find(quote)
    if start < 0 or document.text.find(quote, start + 1) >= 0:
        raise FoundationError('IDENTITY_BRANCH_EXACT_SOURCE_REQUIRED')
    return dict(asdict(document.evidence(start, start + len(quote))),
                quote=quote, path=document.path, sha256=document.sha256)


def _observe_specialization(code):
    """Partial evaluation of the one literal observe driver; no policy edit.

    Keep exactly its observe body and remove two now-unreferenced helpers.
    Any changed call shape is rejected rather than silently specialized.
    """
    tree = ast.parse(code)
    actions = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'ACTION' for t in n.targets)]
    steps = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'step']
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'step']
    if (len(actions) != 1 or ast.literal_eval(actions[0].value) != 'observe'
            or len(steps) != 1 or len(calls) != 1
            or ast.unparse(calls[0]) != 'step(ACTION)' or len(steps[0].body) != 1):
        raise FoundationError('IDENTITY_BRANCH_LITERAL_OBSERVE_DRIVER_REQUIRED')
    branch = steps[0].body[0]
    while isinstance(branch, ast.If) and ast.unparse(branch.test) != "action == 'observe'":
        branch = branch.orelse[0] if len(branch.orelse) == 1 else None
    if not isinstance(branch, ast.If):
        raise FoundationError('IDENTITY_BRANCH_OBSERVE_BODY_REQUIRED')
    original_observe_ast = ast.dump(ast.Module(body=copy.deepcopy(branch.body), type_ignores=[]),
                                    include_attributes=False)
    steps[0].body = copy.deepcopy(branch.body)
    tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef)
                 and n.name in {'parse_turn', 'propose'})]
    if any(isinstance(n, ast.Name) and n.id in {'parse_turn', 'propose'} for n in ast.walk(tree)):
        raise FoundationError('IDENTITY_BRANCH_SPECIALIZATION_HAS_LIVE_REMOVED_REFERENCE')
    specialized = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    new_step = next(n for n in ast.parse(specialized).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'step')
    if ast.dump(ast.Module(body=new_step.body, type_ignores=[]), include_attributes=False) != original_observe_ast:
        raise FoundationError('IDENTITY_BRANCH_SPECIALIZATION_CHANGED_OBSERVE')
    return specialized


def extend_identity_failure(candidate, documents, contract, *,
        publication_name='Native-A-Branches 00000000-0000-0000-0000-000000000000'):
    """Return a fresh candidate; never change the supplied sealed candidate.

    Requires the current emitter's exact driver and official three-attempt
    policy. It declares lifecycle close only after verified failed answers;
    unknown read results, stale calls or a consumed bank reservation abort.
    The API/tool documents remain untouched and no banking is dispatched here.
    """
    out = copy.deepcopy(candidate)
    if candidate.get('branch_extensions') or candidate.get('config', {}).get('facts', {}).get('identity_max_wrong') != 3:
        raise FoundationError('IDENTITY_BRANCH_OFFICIAL_THREE_ATTEMPTS_REQUIRED')
    refs = [_quote(documents['brief'], RULE12), _quote(documents['brief'], RULE14)]
    cfg = out['config']
    if cfg['facts']['product_limit'] != 1 or cfg['facts']['identity_order'] != ['date_of_birth', 'phone_last4']:
        raise FoundationError('IDENTITY_BRANCH_BRIEF_A_SCOPE_REQUIRED')
    closure = compile_official_closure(documents, contract, outcome='not_verified', reason='failed',
        customer_id_variable='customer_id', farewell=cfg['wording']['goodbye']['template'])
    nodes = out['workflow']['nodes']
    if TARGET in nodes or out['workflow'].get('tools'):
        raise FoundationError('IDENTITY_BRANCH_FRESH_NODE_SCOPE_REQUIRED')
    slot = nodes['native_observe']['pre_actions'][0]['function']['parameters']['properties']['code']
    before = slot['default']
    lowered = _observe_specialization(before)
    tree = ast.parse(lowered)
    step = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'step')
    exhausted = [n for n in ast.walk(step) if isinstance(n, ast.Raise)
                 and ast.unparse(n) == "raise ValueError('Identity attempts exhausted')"]
    if len(exhausted) != 1:
        raise FoundationError('IDENTITY_BRANCH_EXACT_EXHAUSTION_GATE_REQUIRED')
    wording = {'template': 'Please use the mobile app or a branch.',
               # Keep locations in the audit record, not executable identity.
               'evidence': [{k: v for k, v in refs[1].items() if k != 'path'}],
               'origin': 'derived_from_rule_14_not_verbatim_source_sentence'}
    replacement = ast.parse('''
_context = json.loads(vars.get('_native_write_context', ''))
_write = json.loads(vars.get('_native_write_state', ''))
if (S['wrong'] != 3 or S['pending'] or S.get('product') or S.get('draft')
    or S.get('consent') or S['verified'] == CFG['facts']['identity_order']
    or _context.get('authority') != 'host_exclusive_customer_reservation'
    or _context.get('provider_idempotency') is not False
    or _context.get('restart_policy') != 'never_automatically_resume_or_reinitialize'
    or _context.get('customer_id') != S['customer_id'] or not _context.get('reservation_id')
    or _write.get('reservation_id') != _context['reservation_id']
    or _write.get('phase') != 'ready' or type(_write.get('attempts')) is not int or _write['attempts'] != 0
    or _write.get('call_id') is not None or _write.get('payload_json') is not None
    or vars.get('_native_write_authorization') or vars.get('_native_close_armed')):
    raise ValueError('Identity failure branch has uncertain or consumed scope')
CFG['wording']['identity_failed'] = WORDING
emit('identity_failed', dict())
S['phase'] = 'identity_failed'
_witness = dict(binding=S['binding'], call_id=S['call_id'], customer_id=S['customer_id'], reservation_id=_context['reservation_id'], phase=S['phase'])
vars['_native_close_armed'] = json.dumps(_witness, allow_nan=False)
vars['_native_close_url'] = CLOSE_URL
vars['_native_close_customer'] = S['customer_id']
vars['_native_route'] = TARGET
return
'''.replace('WORDING', repr(wording)).replace('CLOSE_URL', repr(contract.operations['close_call'].url))
       .replace('TARGET', repr(TARGET))).body

    class Replace(ast.NodeTransformer):
        def visit_Raise(self, node):
            return copy.deepcopy(replacement) if node is exhausted[0] else node

    slot['default'] = ast.unparse(ast.fix_missing_locations(Replace().visit(tree))) + '\n'
    compile(slot['default'], '<native-identity-failed>', 'exec')
    # Reuse the measured conversation placement and technical ending shape;
    # only the source-derived static outcome/reason body differs.
    goodbye = copy.deepcopy(nodes['official_goodbye'])
    if (goodbye.get('node_type') != 'conversation' or len(goodbye.get('tools', [])) != 2
            or goodbye['tools'][0]['function']['handler'] != '__function__:end_call'
            or goodbye['tools'][1]['config'].get('alwaysRunsAt') != ['end_of_call']):
        raise FoundationError('IDENTITY_BRANCH_MEASURED_CONVERSATION_SHAPE_REQUIRED')
    goodbye['name'] = TARGET
    close = copy.deepcopy(closure['lifecycle_tool_template'])
    props = close['function']['parameters']['properties']
    props['url']['default'] = '{{_native_close_url}}'
    props['body']['properties']['customer_id']['default'] = '{{_native_close_customer}}'
    close['function'].setdefault('description', '')
    goodbye['tools'][1] = close
    nodes[TARGET] = goodbye
    dispatch = nodes['native_dispatch']['transitions']
    if not dispatch or any(e.get('target_node') == TARGET for e in dispatch):
        raise FoundationError('IDENTITY_BRANCH_DISPATCH_SCOPE_REQUIRED')
    dispatch.insert(0, _edge(TARGET, '_native_route', TARGET))
    budget = measure_code_exec_budget(out['workflow'], {}, reserve_variable_bytes=16384)
    body = json.dumps({'name': publication_name, 'workflow': out['workflow']}, allow_nan=False).encode()
    if not budget['passed'] or len(body) >= 131072:
        raise FoundationError('IDENTITY_BRANCH_FULL_PUBLICATION_BYTE_LIMIT')
    out['workflow_sha256'] = _sha(json.dumps(out['workflow'], sort_keys=True, ensure_ascii=True,
        allow_nan=False, separators=(',', ':')).encode())
    out['budget'] = dict(budget, whole_workflow_compact_bytes=len(json.dumps(out['workflow'],
        sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()),
        full_publication_body_bytes=len(body), publication_name_is_size_fixture=True,
        full_publication_limit=131072)
    out['branch_extensions'] = {'scope': 'Brief A rules12/14 identity exhaustion only',
        'native_decision': 'IMPLEMENTABLE_UNMEASURED_EXTENSION',
        'practice_case': 'PUB-03', 'official_practice_inventory': {'A': 10, 'B': 3},
        'source_references': refs, 'wording_is_derived': True,
        'observe_specialization': {'literal_action': 'observe', 'unreachable_actions_removed': ['boot', 'turn', 'write_gate', 'close_gate'],
            'original_sha256': _sha(before.encode()), 'specialized_sha256': _sha(lowered.encode()),
            'observe_body_AST_unchanged_before_branch_delta': True},
        'native_extension_live_verified': False, 'official_case_PASS': False,
        'network_calls': 0, 'arbitrary_restart_supported': False,
        'remaining_unsupported': ['other Brief A non-promise branches', 'Brief B multi-product']}
    out['readiness']['scope'] += '; source-bound three-wrong identity closure extension (local only)'
    return out


def identity_failure_evidence(candidate):
    """Execute the extended graph; synthetic replies never grant live authority."""
    import tempfile
    from .native_kit_verification import OfficialNativeHarness, local_http, local_variables
    if candidate.get('branch_extensions', {}).get('practice_case') != 'PUB-03':
        raise FoundationError('IDENTITY_BRANCH_EVIDENCE_REQUIRES_EXPORTED_EXTENSION')
    with tempfile.TemporaryDirectory(prefix='identity-branch-evidence-') as registry:
        h = OfficialNativeHarness(candidate, local_variables(registry))
        h.start()
        h.receive({'kind': 'person_confirm', 'explicit_yes': True})
        premature = False
        for answer in ('1991-07-12', '1992-08-21', '1990-07-21'):
            h.receive({'kind': 'identity_answer', 'field': 'date_of_birth', 'answer': answer})
            h.dispatch('verify_identity')
            h.resume('verify_identity', local_http({'ok': True, 'verified': False}))
            if h.state['wrong'] < 3:
                premature |= bool(h.variables.get('_native_close_armed'))
        h.complete_terminal_fixture()
        closed = h.dispatch_lifecycle_fixture()
        # Semantic intent alone is not a spoken instruction. Require the actual
        # TTS node in the executed graph before the technical closing node.
        spoken = [row for row in h.tts_events if row.get('text') == 'Please use the mobile app or a branch.']
        traversal = [row['node'] for row in h.traversal]
        ordered = traversal[-4:] == ['native_observe', 'native_speak', 'native_dispatch', TARGET]
        text = ' '.join(row['text'] for row in h.tts_events).lower()
        checks = {
            'identity_failure_after_three_confirmed_wrong': h.state['wrong'] == 3 and h.state['phase'] == 'identity_failed' and not premature,
            'identity_failure_no_account_or_promise': [r['operation'] for r in h.requests] == ['verify_identity'] * 3 + ['close_call'],
            'identity_failure_source_app_or_branch': len(spoken) == 1 and spoken[0]['node'] == 'native_speak' and ordered,
            'identity_failure_no_debt_disclosure': not any(v in text for v in ('collections', 'past-due', 'credit card', 'minimum payment')),
            'identity_failure_exact_close': closed['body'] == {'customer_id': h.variables['customer_id'], 'outcome': 'not_verified', 'reason': 'failed'},
            'identity_failure_no_lifecycle_replay': not h.dispatch_lifecycle_fixture().get('url'),
        }
        return {'passed': all(checks.values()),
            'checks': [{'id': name, 'passed': bool(value)} for name, value in checks.items()],
            'trace': {'events': h.state['events'], 'requests': h.requests, 'tts_events': h.tts_events,
                      'traversal': h.traversal, 'terminal_fixture': h.terminal_fixture,
                      'code_measurements': h.code_measurements},
            'fixture_scope': 'Local false identity responses and native lifecycle fixture, not Freya PUB-03 evaluation',
            'speech_evidence_scope': 'Executed local TTS instruction before closing conversation; no device/audio acknowledgement or new platform verification',
            'platform_PASS': False, 'network_calls': 0}
