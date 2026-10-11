"""Single offline source-to-native-product command with immutable bounded proof.

Supported typed sources are interpreted from their contents, never filenames.
Unsupported natural language remains blocked. Local bank fixtures and emitted
code checks do not grant publication, speech completion or model reliability.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import socket
import time
import tempfile
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.compiled_policy import lower_compilation
from builder.ingest import collect_sources, read_source
from builder.models import Document, FoundationError, strict_json
from builder.native_freya import NativeHarness, build_native_workflow
from builder.native_write_guard import validate_runtime_mode, NATIVE_RUNTIME_MODES
from compiler.core import compile_sources, make_request, validate_decisions
from compiler.typed_source import TypedSourceProvider, source_wording
from orchestration.prepare_delivery import scan_public

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/product-build'
RUNTIME_FILES = ('builder/brief_requirements.py', 'builder/native_freya.py', 'builder/compiled_policy.py',
    'builder/api_contract.py', 'builder/native_policy_bindings.py', 'builder/conversation.py',
    'builder/semantics.py', 'builder/semantic_renderer.py', 'builder/extract.py',
    'builder/ingest.py', 'builder/models.py', 'compiler/typed_source.py', 'compiler/core.py',
    'orchestration/product_build.py', 'orchestration/prepare_delivery.py')
KIT = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder'
OFFICIAL_FILES = ('briefs/brief-a.md', 'briefs/brief-b.md', 'docs/workflow-format.md',
                 'docs/tools-and-code.md', 'examples/base-agent.json', 'public-test-set.json')
PLATFORM_FILES = ('docs/workflow-format.md', 'docs/tools-and-code.md', 'examples/base-agent.json')
OFFICIAL_RUNTIME = ('builder/native_kit_emitter.py', 'builder/native_kit_verification.py',
                    'builder/native_write_guard.py', 'builder/native_official_closure.py',
                    'builder/freya_export.py', 'builder/native_branch_extensions.py',
                    'builder/native_payment_negotiation.py',
                    'builder/native_multi_product.py',
                    'orchestration/native_behavioral_repair.py')
NATIVE_MODES = ('prefix', 'official')


def _runtime_pins(mode):
    return {name: sha((ROOT / name).read_bytes()) for name in
            RUNTIME_FILES + (OFFICIAL_RUNTIME if mode == 'official' else ())}


def _publication_bytes(workflow):
    # Same whole-body serializer as the live transport. The UUID/name is a
    # size fixture; the live publisher must measure its actual request again.
    body = {'name': 'Native-A-PUB01 00000000-0000-0000-0000-000000000000',
            'workflow': workflow}
    count = len(json.dumps(body, allow_nan=False).encode())
    if count >= 131072:
        raise FoundationError('PRODUCT_FULL_PUBLICATION_BODY_EXCEEDS_CAP')
    return count


def _export_scope(candidate):
    if candidate.get('format_version') == 'native-multi-product-1':
        return 'structured_multi_product_promise_and_refusal_branches'
    return 'complete_single_product_promise_branch'


def _official_source_names(bound=None):
    if bound is None:
        return OFFICIAL_FILES  # Existing explicit practice-reference reader.
    if bound.get('scope') in ('bounded_explicit_typed_source', 'bounded-brief-requirements-1'):
        # Platform wire/lifecycle references are distinct from practice policy
        # and test fixtures. Hidden briefs supply their own compiled authority.
        return PLATFORM_FILES
    if bound.get('scope') in ('reviewed_single_product_practice_sources',
            'single_account_prefix_of_reviewed_multi_product_practice_sources'):
        return OFFICIAL_FILES
    raise FoundationError('PRODUCT_OFFICIAL_SOURCE_SCOPE_INVALID')


def _official_sources(bound=None):
    return [{'name': name, 'document': asdict(read_source(KIT / name)),
             'base64': base64.b64encode((KIT / name).read_bytes()).decode()}
            for name in _official_source_names(bound)]


def _official_provenance_path(value, name):
    # An archived origin is provenance, never a file access capability. Keep it
    # verbatim so the emitted source binding survives moving a verified archive.
    suffix = PurePosixPath(name).parts
    return isinstance(value, str) and any(path.is_absolute()
        and path.parts[-len(suffix):] == suffix
        for path in (PurePosixPath(value), PureWindowsPath(value)))


def _emit_official(bound, original_docs, api, contract, snapshots, official, native_runtime_mode="host_exclusive"):
    """Supply replayed source-bound compilation, never substitute policy rules.

    Archived inputs permit independent replay without the original input paths.
    Official source documents remain byte-verified and are injected only into
    the emitter's reader; this is not a native runtime/storage capability.
    """
    outcomes = bound['compiled'].policy.facts['outcome_table'].value
    if (not any(row.get('outcome') == 'promise' and row.get('reasons') == [] for row in outcomes)
            or not any(row.get('outcome') == 'no_promise' and 'service_error' in row.get('reasons', [])
                       for row in outcomes)):
        raise FoundationError('PRODUCT_OFFICIAL_PROMISE_FAILURE_OUTCOMES_UNSUPPORTED')
    module = importlib.import_module('builder.native_kit_emitter')
    product_module = importlib.import_module('orchestration.product_build')
    originals = {str(KIT / row['name']): Document(**row['document']) for row in official}
    provenance = {doc.path: doc for doc in [*originals.values(), *original_docs]}
    closure_module = importlib.import_module('builder.native_official_closure')
    def archived_source(path):
        key = str(path)
        if key not in originals and key not in provenance:
            raise FoundationError('PRODUCT_UNDECLARED_OFFICIAL_SOURCE_READ')
        return originals.get(key, provenance.get(key))
    if bound['scope'] == 'single_account_prefix_of_reviewed_multi_product_practice_sources':
        multi = importlib.import_module('builder.native_multi_product')
        with patch.object(product_module, 'source_bundle', return_value=(original_docs, api, contract)), \
             patch.object(product_module, 'compile_bundle', return_value=bound), \
             patch.object(multi, 'read_source', side_effect=archived_source), \
             patch.object(closure_module, 'read_source', side_effect=archived_source):
            candidate = multi.build_multi_product_native_workflow([d.path for d in original_docs], api.path, KIT,
                compilation=bound, source_snapshots=snapshots, native_runtime_mode=native_runtime_mode)
        cases = originals[str(KIT / 'public-test-set.json')]
        candidate['public_fixture_sha256'] = cases.sha256
        candidate['fixture_cases'] = [c for c in strict_json(cases.text) if c['id'] in ('PUB-11', 'PUB-12', 'PUB-13')]
        return candidate
    actual_builder = module.build_native_workflow
    def build(*args, **kwargs):
        kwargs['source_snapshots'] = snapshots
        return actual_builder(*args, **kwargs)
    with patch.object(product_module, 'source_bundle', return_value=(original_docs, api, contract)), \
         patch.object(product_module, 'compile_bundle', return_value=bound), \
         patch.object(module, 'read_source', side_effect=archived_source), \
         patch.object(closure_module, 'read_source', side_effect=archived_source), \
         patch.object(module, 'build_native_workflow', side_effect=build):
        reviewed_a = bound['scope'] == 'reviewed_single_product_practice_sources'
        candidate = module.build_official_native_workflow([d.path for d in original_docs], api.path, KIT,
            identity_failure_branch=reviewed_a, payment_date_negotiation=reviewed_a, native_runtime_mode=native_runtime_mode)
        if candidate.get('payment_date_extension'):
            cases = originals[str(KIT / 'public-test-set.json')]
            candidate['payment_date_extension']['public_fixture_sha256'] = cases.sha256
            candidate['payment_date_extension']['fixture_cases'] = [c for c in strict_json(cases.text)
                if c['id'] in ('PUB-04', 'PUB-07')]
        return candidate


def _official_evidence(candidate, compiled):
    """Execute the full emitted graph with independently source-bounded inputs.

    These are synthetic local HTTP/completion fixtures, not Freya receipts.
    Amount/date vary with the supplied memo rather than using the PUB01 oracle.
    """
    if candidate.get('format_version') == 'native-multi-product-1':
        from builder.native_multi_product import verify_multi_product_candidate
        proof = verify_multi_product_candidate(candidate, fixture_cases=candidate['fixture_cases'])
        return {'passed': proof['passed'],
            'checks': [{'id': 'B_' + key, 'passed': value} for key, value in proof['checks'].items()],
            'trace': {'cases': proof['cases'], 'requests': [r for c in proof['cases'] for r in c['original_graph_trace']['requests']]},
            'scope': 'Local Brief B structured PUB-11/12/13 graph execution; synthetic bank and terminal fixtures, not Freya evaluation',
            'network_calls': 0, 'platform_PASS': False}
    from builder.native_kit_verification import OfficialNativeHarness, local_http, local_variables
    facts = {name: fact.value for name, fact in compiled.policy.facts.items()}
    now = date(2026, 10, 14)
    day = now
    amount = Decimal('420')
    band = next((b for b in facts['bands'] if b['min_days_late'] <= 6 <= b['max_days_late']), None)
    if band is None:
        raise FoundationError('PRODUCT_FULL_NATIVE_FIXTURE_BAND_UNSUPPORTED')
    def business(value):
        return value.weekday() in facts['business_weekdays'] and value.isoformat() not in facts['holidays']
    limit = now
    remaining = band['deadline_business_days']
    for unused in range(4000):
        if not remaining:
            break
        limit += timedelta(days=1)
        if business(limit):
            remaining -= 1
    if remaining:
        raise FoundationError('PRODUCT_FULL_NATIVE_FIXTURE_CALENDAR_INFEASIBLE')
    for constraint in compiled.constraints:
        if 'create_promise' not in constraint['operations']:
            continue
        target, value = constraint['target'], constraint['value']
        if target == 'payment.amount':
            amount = min(amount, Decimal(str(value)))
        elif target == 'payment.date_on_or_after':
            day = max(day, date.fromisoformat(value))
        elif target == 'payment.date_on_or_before':
            limit = min(limit, date.fromisoformat(value))
    for unused in range(370):
        if business(day):
            break
        day += timedelta(days=1)
    if amount <= 0 or day > limit or not business(day):
        raise FoundationError('PRODUCT_FULL_NATIVE_FIXTURE_PROPOSAL_INFEASIBLE')
    product_type = next(kind for kind in compiled.contract.operations['get_account'].response['properties']['products']['items']['properties']['type']['enum']
                        if kind in facts['floor_fields'])
    product = {'product_id': 'p1', 'type': product_type, 'card_last4': '4821',
               'past_due': 420.0, 'days_late': 6,
               facts['floor_fields'][product_type]: float(min(Decimal('126'), amount))}
    with tempfile.TemporaryDirectory(prefix='product-full-native-') as registry:
        variables = local_variables(registry)
        # Required binding comes from source placeholders, not a guessed bank
        # identity. This is explicitly a synthetic local test input; real callers
        # must provide their actual declared context before publication/admission.
        bank_refs = [entry['evidence'] for entry in candidate['config']['wording'].values()
                     if '{bank_name}' in entry['template']]
        fixture_context = {}
        if bank_refs:
            variables['bank_name'] = 'Local Fixture Bank'
            fixture_context['bank_name'] = {'value': variables['bank_name'], 'source_evidence': bank_refs,
                'authority': 'synthetic_local_input_for_source_required_placeholder',
                'source_bank_identity_value_claim': False, 'actual_native_context_verified': False}
        h = OfficialNativeHarness(candidate, variables)
        h.start()
        if h.state['phase'] == 'person_confirm':
            h.receive({'kind': 'person_confirm', 'explicit_yes': True})
        for field in facts['identity_order']:
            h.receive({'kind': 'identity_answer', 'field': field,
                       'answer': '1988-03-14' if field == 'date_of_birth' else '4471'})
            if not h.dispatch('verify_identity').get('url'):
                raise FoundationError('PRODUCT_FULL_NATIVE_IDENTITY_BLOCKED')
            h.resume('verify_identity', local_http({'ok': True, 'verified': True}))
        h.dispatch('get_account')
        h.resume('get_account', local_http({'ok': True, 'products': [product]}))
        h.receive({'kind': 'proposal', 'product_id': 'p1', 'amount': float(amount), 'date': day.isoformat()})
        before = len(h.requests)
        try:
            h.dispatch('create_promise')
        except FoundationError:
            pass
        denied_before_consent = len(h.requests) == before
        h.receive({'kind': 'confirm', 'readback_token': h.state['readback_token'], 'explicit_yes': True})
        request = h.dispatch('create_promise')
        if not request.get('url'):
            raise FoundationError('PRODUCT_FULL_NATIVE_WRITE_FIXTURE_BLOCKED')
        h.resume('create_promise', local_http({'ok': True}))
        h.complete_terminal_fixture()
        close = h.dispatch_lifecycle_fixture()
        operations = [row['operation'] for row in h.requests]
        checks = {
            'source_factor_order_before_account': [r['body']['field'] for r in h.requests[:2]] == facts['identity_order'],
            'explicit_confirmation_before_write': denied_before_consent,
            'source_bounded_exact_promise': request['body'] == dict(customer_id=h.variables['customer_id'], product_id='p1', amount=float(amount), date=day.isoformat()),
            'confirmed_bank_before_success': h.state['phase'] == 'promise_confirmed' and any(e['kind'] == 'promise_success' and e.get('bank_ok') is True for e in h.state['events']),
            'one_promise_one_close_in_order': operations == ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call'],
            'exact_close_without_reason': bool(close.get('url')) and close['body'] == {'customer_id': h.variables['customer_id'], 'outcome': 'promise'},
            'source_farewell_local_fixture': h.terminal_fixture['farewell'] == candidate['config']['wording']['goodbye']['template'],
            'replayed_local_lifecycle_denied': not h.dispatch_lifecycle_fixture().get('url'),
            'runtime_code_budget': all(row['serialized_input_bytes'] <= 114688 for row in h.code_measurements),
            'no_live_or_restart_authority': candidate['readiness']['bank_writes_enabled'] is False and candidate['readiness']['automatic_restart_allowed'] is False}
        result = {'checks': [{'id': k, 'passed': bool(v)} for k, v in checks.items()],
            'passed': all(checks.values()), 'trace': {'events': h.state['events'], 'requests': h.requests,
                'tts_events': h.tts_events, 'traversal': h.traversal, 'terminal_fixture': h.terminal_fixture,
                'code_measurements': h.code_measurements, 'fixture_context': fixture_context},
            'scope': 'Complete single-product promise branch; source-derived local bank and completion fixtures only',
            'account_result_tested': True, 'payment_success_tested': True,
            'real_platform_speech_proven': False, 'model_reliability_proven': False, 'network_calls': 0}
        if candidate.get('branch_extensions'):
            from builder.native_branch_extensions import identity_failure_evidence
            branch = identity_failure_evidence(candidate)
            result['checks'].extend(branch['checks'])
            result['passed'] = result['passed'] and branch['passed']
            result['trace']['identity_failure_branch'] = branch['trace']
            result['branch_fixture_scope'] = branch['fixture_scope']
        if candidate.get('payment_date_extension'):
            from builder.native_payment_negotiation import payment_date_evidence
            dates = payment_date_evidence(candidate, candidate['payment_date_extension']['fixture_cases'])
            result['checks'].extend(dates['checks'])
            result['passed'] = result['passed'] and dates['passed']
            result['trace']['payment_date_subflows'] = dates['traces']
            result['date_fixture_scope'] = dates['fixture_scope']
        return result




def _official_session_fixture_variables(registry):
    # Explicit local model of the official run_cases server variables. No
    # native-ID alias, host reservation or provider-storage claim is supplied.
    return dict(customer_id='local-pub01-fixture', first_name='Daniel',
                last_name='Reyes', title='Mr', now='2026-10-14T11:00')


def official_evidence(candidate, compiled):
    if candidate.get('native_runtime_mode') != 'official_bank_session':
        return _official_evidence(candidate, compiled)
    # Source oracles are unchanged: adapt ONLY the fixture's admission inputs
    # used by existing subflow oracles to the selected runtime profile.
    module = importlib.import_module('builder.native_kit_verification')
    with patch.object(module, 'local_variables', side_effect=_official_session_fixture_variables):
        return _official_evidence(candidate, compiled)


def raw_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode() + b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def source_bundle(brief_paths, tool_api_path):
    docs = collect_sources([Path(path) for path in brief_paths])
    api = read_source(Path(tool_api_path))
    contract = BankContract(strict_json(api.text), api.source_id, api.sha256)
    return docs, api, contract


def compile_bundle(brief_paths, tool_api_path, docs, contract):
    """Reviewed-source integration may return its independently reproducible binding.

    No generic NLP fallback is invented when both interpreters refuse a source.
    """
    try:
        envelope = compile_sources(policy_paths=[Path(p) for p in brief_paths],
            tool_api_path=Path(tool_api_path), provider=TypedSourceProvider())
        compiled = lower_compilation(envelope, docs, contract)
        return {'compiled': compiled, 'documents': docs, 'wording_bundle': source_wording(docs),
                'compilation': envelope, 'scope': 'bounded_explicit_typed_source',
                'unsupported': [], 'publish_allowed': False}
    except FoundationError as typed_error:
        # Content-derived bounded entry over the already bound source documents
        # and actual API contract. It reads no original paths or model defaults.
        try:
            bounded = importlib.import_module('builder.brief_requirements')
            return bounded.compile_requirement_documents(docs, contract)
        except FoundationError as bounded_error:
            # Unknown/ambiguous briefs retain their actual bounded compiler
            # refusal. Only genuine exact reviewed source/API bytes may enter
            # the separate practice-version binder; names/order are not gates.
            from builder.policy_specs import BRIEF_A_HASH, BRIEF_B_HASH, API_HASH
            hashes = [doc.sha256 for doc in docs]
            if (BRIEF_A_HASH not in hashes or len(set(hashes)) != len(hashes)
                    or set(hashes) - {BRIEF_A_HASH, BRIEF_B_HASH}
                    or contract.source_hash != API_HASH):
                raise bounded_error from typed_error
            current = collect_sources([Path(path) for path in brief_paths])
            api = read_source(Path(tool_api_path))
            actual_contract = BankContract(strict_json(api.text), api.source_id, api.sha256)
            if (current != list(docs) or api.sha256 != API_HASH
                    or actual_contract.serializable() != contract.serializable()):
                raise bounded_error from typed_error
        # Only an installed, reviewed local binder is admitted. It must refuse
        # unknown source hashes and keep its unsupported branches explicit.
        try:
            module = importlib.import_module('builder.native_policy_bindings')
        except ModuleNotFoundError:
            raise FoundationError('PRODUCT_UNSUPPORTED_SOURCE_INTERPRETATION') from typed_error
        result = module.bind_reviewed_native_sources(docs, contract)
        if result.get('publish_allowed') is not False:
            raise FoundationError('PRODUCT_SOURCE_PROVIDER_SCOPE_INVALID')
        return result


def critical_evidence(candidate, compiled, *, right_party_required=False):
    """Fresh executions of actual emitted snippets and independent safety probes."""
    variables = {'customer_id': 'local-product-fixture', 'call_id': 'local-product-call',
                 'session_id': 'local-product-session', 'call.call_id': '00000000-0000-0000-0000-000000000123',
                 'now': '2026-10-14T11:00',
                 'first_name': 'Local', 'last_name': 'Fixture', 'title': 'Mx'}
    checks = []
    def add(identifier, passed):
        checks.append({'id': identifier, 'passed': bool(passed)})
    nodes = candidate['workflow']['nodes']
    http_names = {tool['function']['name'] for node in nodes.values()
                  for tool in node.get('pre_actions', [])
                  if tool['function']['handler'] == '__function__:api_call'}
    add('only_read_identity_and_account_http_nodes', http_names == {'verify_identity', 'get_account'})
    h = NativeHarness(candidate, variables)
    inputs, responses = [], []
    def receive(value):
        inputs.append({'phase_before': h.state.get('phase'), 'input': value})
        return h.receive(value)
    def resume(operation, value):
        responses.append({'operation': operation, 'envelope': value})
        return h.resume(operation, value)
    h.start()
    expected_phase = 'person_confirm' if right_party_required else 'identity'
    expected_events = ['opening'] if right_party_required else ['opening', 'identity_question']
    add('source_opening_and_required_entry_phase', h.state.get('phase') == expected_phase
        and [row['kind'] for row in h.state.get('events', [])] == expected_events)
    early = NativeHarness(candidate, variables)
    early.start()
    try:
        early.dispatch('get_account')
    except (FoundationError, ValueError):
        add('premature_account_read_rejected', not early.requests)
    else:
        add('premature_account_read_rejected', False)
    order = compiled.policy.facts['identity_order'].value
    if right_party_required:
        wrong_party = NativeHarness(candidate, variables)
        wrong_party.start()
        wrong_party.receive({'kind': 'identity_answer', 'field': order[0],
                             'answer': '1988-03-14' if order[0] == 'date_of_birth' else '4471'})
        add('right_party_confirmation_precedes_security_question',
            wrong_party.state.get('phase') == 'blocked' and not wrong_party.requests)
        receive({'kind': 'person_confirm', 'explicit_yes': True})
        add('explicit_person_confirmation_opens_identity', h.state.get('phase') == 'identity')
    for field in order:
        answer = '1988-03-14' if field == 'date_of_birth' else '4471'
        receive({'kind': 'identity_answer', 'field': field, 'answer': answer})
        h.dispatch('verify_identity')
        resume('verify_identity', {'status': 'success', 'status_code': 200,
                 'data': {'ok': True, 'verified': True}, 'header': {}})
    h.dispatch('get_account')
    add('source_factor_order_then_account_request',
        [row['body']['field'] for row in h.requests if row['operation'] == 'verify_identity'] == order
        and [row['operation'] for row in h.requests] == ['verify_identity', 'verify_identity', 'get_account'])
    # These bank facts are synthetic. A permitted draft is derived independently
    # from source calendar, delinquency band and lowered constraints.
    facts = {key: fact.value for key, fact in compiled.policy.facts.items()}
    band = facts['bands'][0]
    days_late = max(band['min_days_late'], min(8, band['max_days_late']))
    amount = Decimal('500')
    earliest = date.fromisoformat(variables['now'][:10])
    limit = earliest
    def business(day):
        return day.weekday() in facts['business_weekdays'] and day.isoformat() not in facts['holidays']
    remaining = band['deadline_business_days']
    for unused in range(4000):
        if not remaining:
            break
        limit += timedelta(days=1)
        if business(limit):
            remaining -= 1
    if remaining:
        raise FoundationError('PRODUCT_FIXTURE_CALENDAR_INFEASIBLE')
    for rule in compiled.constraints:
        if 'create_promise' not in rule['operations']:
            continue
        if rule['target'] == 'payment.amount':
            amount = min(amount, Decimal(str(rule['value'])))
        elif rule['target'] == 'payment.date_on_or_before':
            limit = min(limit, date.fromisoformat(rule['value']))
        elif rule['target'] == 'payment.date_on_or_after':
            earliest = max(earliest, date.fromisoformat(rule['value']))
    for unused in range(370):
        if business(earliest):
            break
        earliest += timedelta(days=1)
    if amount <= 0 or earliest > limit or not business(earliest):
        raise FoundationError('PRODUCT_FIXTURE_PROPOSAL_INFEASIBLE')
    product_type = next(kind for kind in compiled.contract.operations['get_account'].response['properties']['products']['items']['properties']['type']['enum']
                        if kind in facts['floor_fields'])
    product = {'product_id': 'local-fixture-product', 'type': product_type,
               'past_due': 500, 'days_late': days_late, 'card_last4': '3456',
               facts['floor_fields'][product_type]: float(min(Decimal('100'), amount))}
    resume('get_account', {'status': 'success', 'status_code': 200,
        'data': {'ok': True, 'products': [product]}, 'header': {}})
    add('account_disclosure_and_offer_follow_verified_read', h.state.get('phase') == 'proposal'
        and [row['kind'] for row in h.state['events']][-2:] == ['account_summary', 'proposal_request'])
    add('independently_source_bounded_suggestion', h.state.get('product', {}).get('suggested_amount') == float(amount)
        and h.state.get('product', {}).get('suggested_date') == earliest.isoformat())
    receive({'kind': 'proposal', 'product_id': product['product_id'], 'amount': float(amount), 'date': earliest.isoformat()})
    add('explicit_draft_creates_current_readback', h.state.get('phase') == 'readback'
        and h.state['events'][-1]['kind'] == 'readback' and not h.state['consent'])
    before = len(h.requests)
    try:
        h.dispatch('create_promise')
    except (FoundationError, ValueError):
        add('withheld_consent_cannot_dispatch_write', len(h.requests) == before)
    else:
        add('withheld_consent_cannot_dispatch_write', False)
    token = h.state['readback_token']
    # Inspect the actual turn snippet before its graph edge immediately reaches
    # the sealed gate, then follow that real edge through native_dispatch.
    confirmation = {'kind': 'confirm', 'readback_token': token, 'explicit_yes': True}
    inputs.append({'phase_before': h.state.get('phase'), 'input': confirmation})
    h.variables['_native_turn_json'] = json.dumps(confirmation)
    h.run_code('native_turn')
    confirmation_checkpoint = h.state
    add('explicit_current_confirmation_binds_only_current_readback',
        h.state.get('phase') == 'confirmed' and h.state['consent'] == token)
    h.drive('native_dispatch')
    add('confirmed_draft_still_cannot_write_or_claim_success', h.state.get('phase') == 'blocked'
        and len(h.requests) == before and not any(row['kind'] == 'promise_success' for row in h.state['events']))
    sealed = NativeHarness(candidate, variables)
    sealed.start()
    sealed.run_code('native_write_gate')
    add('native_write_gate_remains_sealed', sealed.state.get('phase') == 'blocked' and not sealed.requests)
    replay = NativeHarness(candidate, variables)
    replay.start()
    replay.variables['_native_state_json'] = json.dumps(h.state)
    replay.start()
    add('unattested_restart_rejected', replay.state.get('phase') == 'blocked')
    return {'checks': checks, 'passed': all(row['passed'] for row in checks),
            'trace': {'events': h.state.get('events', []), 'requests': h.requests,
                      'inputs': inputs, 'bank_results': responses,
                      'traversal': h.traversal, 'final_state': h.state,
                      'confirmation_checkpoint': confirmation_checkpoint,
                      'code_measurements': h.code_measurements, 'tts_events': h.tts_events},
            'scope': 'Fresh local emitted identity/account/disclosure/proposal/readback/consent prefix; synthetic bank fixtures; write gate sealed',
            'account_result_tested': True, 'payment_success_tested': False,
            'account_result_or_payment_success_tested': True,
            'real_platform_speech_proven': False, 'model_reliability_proven': False,
            'network_calls': 0}


def build_product(*, brief_paths, tool_api_path, output_directory, deadline_seconds=1800,
                  native_mode='prefix', native_runtime_mode='host_exclusive'):
    if native_mode not in NATIVE_MODES:
        raise FoundationError('PRODUCT_NATIVE_MODE_INVALID')
    validate_runtime_mode(native_runtime_mode)
    if native_runtime_mode == 'official_bank_session' and native_mode != 'official':
        raise FoundationError('PRODUCT_BANK_SESSION_REQUIRES_OFFICIAL_EXPORT')
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('PRODUCT_DEADLINE_MUST_BE_1_TO_1800')
    if not brief_paths or len(brief_paths) > 8:
        raise FoundationError('PRODUCT_EXPLICIT_BOUNDED_BRIEF_INPUTS_REQUIRED')
    start = time.monotonic()
    deadline = start + deadline_seconds
    target = Path(output_directory).resolve()
    if target == OUTPUT.resolve() or not target.is_relative_to(OUTPUT.resolve()) or target.exists():
        raise FoundationError('PRODUCT_FRESH_CHILD_OUTPUT_REQUIRED')
    target.mkdir(parents=True)
    def guard():
        if time.monotonic() >= deadline:
            raise FoundationError('PRODUCT_SHARED_DEADLINE_EXCEEDED')
    def save(name, value):
        guard()
        raw = raw_json(value)
        scan_public(raw, '.json')
        with (target / name).open('xb') as stream:
            stream.write(raw)
    report = {'version': 'source-native-product-1', 'status': 'BLOCKED',
        'deadline_seconds': deadline_seconds, 'publish_allowed': False, 'publication_ready': False,
        'final_submission_ready': False, 'network_calls': 0, 'model_calls': 0,
        'live_enabled': False, 'semantic_truth_verified': False, 'native_mode': native_mode, 'native_runtime_mode': native_runtime_mode,
        'verification_portability': 'Raw source bytes are independently parsed and replayed without original input paths; local runtime code pins must match',
        'unsupported': ['Real Freya extraction/TTS/persistence/completion binding',
                        'Unseen arbitrary natural-language policy interpretation',
                        'Native bank writes and close remain sealed']}
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('blocked')
        raise FoundationError('PRODUCT_NETWORK_FORBIDDEN')
    pins = _runtime_pins(native_mode)
    try:
        with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
            docs, api, contract = source_bundle(brief_paths, tool_api_path)
            guard()
            save('source-inventory.json', [asdict(doc) for doc in [*docs, api]])
            raw_sources = []
            for doc in [*docs, api]:
                raw = Path(doc.path).read_bytes()
                if sha(raw) != doc.sha256 or len(raw) != doc.byte_count:
                    raise FoundationError('PRODUCT_INPUT_SOURCE_CHANGED')
                if doc.status != 'processed':
                    raise FoundationError('PRODUCT_UNSUPPORTED_SOURCE_MODALITY')
                scan_public(raw, doc.kind)
                raw_sources.append({'path': doc.path, 'sha256': doc.sha256,
                    'bytes': doc.byte_count, 'base64': base64.b64encode(raw).decode()})
            save('source-bytes.json', raw_sources)
            save('api-document.json', strict_json(api.text))
            bound = compile_bundle(brief_paths, tool_api_path, docs, contract)
            guard()
            arguments = {}
            if bound.get('wording_authorization') is not None:
                arguments['wording_authorization'] = bound['wording_authorization']
                arguments['source_snapshots'] = {row['sha256']: base64.b64decode(row['base64'], validate=True) for row in raw_sources}
            if native_mode == 'official':
                official = _official_sources(bound)
                save('official-sources.json', official)
                snapshots = {row['sha256']: base64.b64decode(row['base64'], validate=True) for row in raw_sources}
                candidate = _emit_official(bound, docs, api, contract, snapshots, official, native_runtime_mode)
                repair_module = importlib.import_module('orchestration.native_behavioral_repair')
                source_reference = _emit_official(bound, docs, api, contract, snapshots, official, native_runtime_mode)
                repair = repair_module.diagnose_and_repair_native(candidate, bound['compiled'],
                    official_evidence, source_reference=source_reference)
                candidate = repair['candidate']
                save('native-repair-evidence.json', repair['receipt'])
                report['native_behavioral_repair'] = {'status': repair['status'],
                    'receipt': 'native-repair-evidence.json',
                    'scope': 'One static farewell source-role binding; policy and banking repair unsupported',
                    'fault_injection_performed': False, 'platform_PASS': False}
                save('candidate.json', candidate)
                publication_bytes = _publication_bytes(candidate['workflow'])
                workflow_raw = raw_json(candidate['workflow'])
            else:
                candidate = build_native_workflow(bound['compiled'], bound['documents'],
                                                  bound['wording_bundle'], **arguments)
                workflow_raw = json.dumps(candidate['workflow'], indent=2, ensure_ascii=True, allow_nan=False).encode() + b'\n'
            if len(workflow_raw) >= 131072:
                raise FoundationError('PRODUCT_ACTUAL_EXPORT_BYTES_EXCEED_CAP')
            scan_public(workflow_raw, '.json')
            with (target / 'agent.workflow.json').open('xb') as stream:
                stream.write(workflow_raw)
            save('binding.json', {'scope': bound['scope'], 'unsupported': bound['unsupported'],
                'compilation': bound.get('compilation'), 'documents': [asdict(doc) for doc in bound['documents']],
                'wording_bundle': bound['wording_bundle'], 'wording_authorization': bound.get('wording_authorization'),
                'required_flow': bound.get('required_flow', {}), 'native_runtime_mode': native_runtime_mode})
            save('lowered-policy.json', bound['compiled'].serializable())
            right_party = bound.get('required_flow', {}).get('right_party_confirmation') is True
            evidence = (official_evidence(candidate, bound['compiled']) if native_mode == 'official' else
                        critical_evidence(candidate, bound['compiled'], right_party_required=right_party))
            guard()
            save('critical-evidence.json', evidence)
            # Re-read serialized export; evidence from a different graph is invalid.
            candidate_from_file = dict(candidate, workflow=strict_json(workflow_raw.decode()))
            independent = (official_evidence(candidate_from_file, bound['compiled']) if native_mode == 'official' else
                           critical_evidence(candidate_from_file, bound['compiled'], right_party_required=right_party))
            save('serialized-export-evidence.json', independent)
            if evidence != independent or not evidence['passed'] or attempts:
                raise FoundationError('PRODUCT_CRITICAL_EXPORT_CHECK_FAILED')
            if any(sha(Path(doc.path).read_bytes()) != doc.sha256 for doc in [*docs, api]):
                raise FoundationError('PRODUCT_INPUT_SOURCE_CHANGED')
            if any(sha((ROOT / name).read_bytes()) != digest for name, digest in pins.items()):
                raise FoundationError('PRODUCT_RUNTIME_SOURCE_CHANGED')
            report.update(status='LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED', provider_scope=bound['scope'],
                source_hashes={doc.path: doc.sha256 for doc in [*docs, api]},
                candidate_sha256=sha(workflow_raw), actual_export_bytes=len(workflow_raw),
                critical_checks=len(evidence['checks']), serialized_export_trace_equal=True,
                source_supported_flow=bound.get('required_flow', {}),
                source_unsupported=bound['unsupported'],
                native_readiness=candidate['readiness'], source_and_runtime_unchanged=True)
            if native_mode == 'official':
                report.update(full_publication_body_bytes=publication_bytes,
                    publication_body_limit_bytes=131072, publication_body_size_fixture=True,
                    native_export_scope=_export_scope(candidate),
                    unsupported=candidate['readiness']['unsupported'],
                    native_playback_verified=False, official_PUB01_PASS=False, provider_final_export=False)
    except (FoundationError, ValueError, OSError, TypeError, KeyError) as error:
        report.update(status='BLOCKED', diagnostic=str(error) if isinstance(error, FoundationError)
                      else 'SOURCE_OR_NATIVE_PRODUCT_VALIDATION_BLOCKED')
    report.update(runtime_sha256=pins, elapsed_seconds=time.monotonic() - start,
                  network_attempts=len(attempts))
    if time.monotonic() >= deadline:
        report.update(status='BLOCKED', diagnostic='PRODUCT_SHARED_DEADLINE_EXCEEDED')
    (target / 'readiness.json').write_bytes(raw_json(report))
    manifest = {'files': [{'path': p.name, 'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())}
                         for p in sorted(target.iterdir()) if p.is_file()]}
    (target / 'manifest.json').write_bytes(raw_json(manifest))
    final = {'status': report['status'], 'deadline_seconds': deadline_seconds,
             'elapsed_seconds': time.monotonic() - start,
             'manifest_sha256': sha((target / 'manifest.json').read_bytes()), 'publish_allowed': False}
    (target / 'FINAL.json').write_bytes(raw_json(final))
    if time.monotonic() >= deadline:
        (target / 'FAILED_FINALIZATION.json').write_bytes(raw_json({'reason': 'PRODUCT_SHARED_DEADLINE_EXCEEDED'}))
        report['status'] = 'BLOCKED'
    return report


def verify_product_build(directory, *, deadline_monotonic=None):
    def guard():
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise FoundationError('PRODUCT_SHARED_DEADLINE_EXCEEDED')
    guard()
    explicit = Path(directory)
    root = explicit.resolve()
    if explicit.is_symlink() or not root.is_dir() or (root / 'FAILED_FINALIZATION.json').exists():
        raise FoundationError('PRODUCT_FINALIZATION_BLOCKED')
    if any(child.is_symlink() or not child.is_file() for child in root.iterdir()):
        raise FoundationError('PRODUCT_EVIDENCE_PATH_INVALID')
    final = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
    if (final.get('status') != 'LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED' or final.get('publish_allowed') is not False
            or type(final.get('deadline_seconds')) is not int or not 1 <= final['deadline_seconds'] <= 1800
            or type(final.get('elapsed_seconds')) not in (int, float)
            or not 0 <= final['elapsed_seconds'] < final['deadline_seconds']):
        raise FoundationError('PRODUCT_FINAL_RECEIPT_INVALID')
    raw = (root / 'manifest.json').read_bytes()
    if sha(raw) != final['manifest_sha256']:
        raise FoundationError('PRODUCT_MANIFEST_CHANGED')
    rows = strict_json(raw.decode())['files']
    names = {row['path'] for row in rows}
    actual = {p.name for p in root.iterdir() if p.is_file()}
    required = {'source-inventory.json', 'source-bytes.json', 'api-document.json', 'agent.workflow.json', 'binding.json',
                'lowered-policy.json', 'critical-evidence.json', 'serialized-export-evidence.json', 'readiness.json'}
    if 'official-sources.json' in names or 'candidate.json' in names:
        required |= {'official-sources.json', 'candidate.json', 'native-repair-evidence.json'}
    if names != required or len(rows) != len(names) or actual != names | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('PRODUCT_EVIDENCE_INVENTORY_INVALID')
    for row in rows:
        if Path(row['path']).name != row['path'] or (root / row['path']).is_symlink():
            raise FoundationError('PRODUCT_EVIDENCE_PATH_INVALID')
        raw = (root / row['path']).read_bytes()
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise FoundationError('PRODUCT_EVIDENCE_BYTES_CHANGED')
        scan_public(raw, '.json')
    report = strict_json((root / 'readiness.json').read_text(encoding='utf-8'))
    mode = report.get('native_mode', 'prefix')
    native_runtime_mode = validate_runtime_mode(report.get('native_runtime_mode', 'host_exclusive'))
    if native_runtime_mode == 'official_bank_session' and mode != 'official':
        raise FoundationError('PRODUCT_BANK_SESSION_REQUIRES_OFFICIAL_EXPORT')
    if mode not in NATIVE_MODES or (mode == 'official') != ('official-sources.json' in names):
        raise FoundationError('PRODUCT_NATIVE_MODE_OR_ARCHIVE_MISMATCH')
    expected_pins = _runtime_pins(mode)
    if report.get('runtime_sha256') != expected_pins:
        raise FoundationError('PRODUCT_RUNTIME_PIN_MISMATCH')
    if report['status'] != final['status'] or any(report.get(key) is not False for key in
            ('publish_allowed', 'publication_ready', 'final_submission_ready', 'live_enabled', 'semantic_truth_verified')):
        raise FoundationError('PRODUCT_READINESS_PROMOTION_REFUSED')
    inventory = [Document(**doc) for doc in strict_json((root / 'source-inventory.json').read_text(encoding='utf-8'))]
    raw_sources = strict_json((root / 'source-bytes.json').read_text(encoding='utf-8'))
    source_snapshots = {}
    if len(raw_sources) != len(inventory):
        raise FoundationError('PRODUCT_RAW_SOURCE_INVENTORY_MISMATCH')
    with tempfile.TemporaryDirectory(prefix='verify-source-', dir=OUTPUT) as temporary:
        for index, (record, doc) in enumerate(zip(raw_sources, inventory)):
            raw = base64.b64decode(record['base64'], validate=True)
            if (record['path'] != doc.path or record['sha256'] != doc.sha256
                    or sha(raw) != doc.sha256 or len(raw) != doc.byte_count
                    or record['bytes'] != len(raw)):
                raise FoundationError('PRODUCT_RAW_SOURCE_HASH_MISMATCH')
            source_snapshots[doc.sha256] = raw
            path = Path(temporary) / ('input-' + str(index) + doc.kind)
            path.write_bytes(raw)
            parsed = read_source(path)
            parsed.path = doc.path
            if asdict(parsed) != asdict(doc):
                raise FoundationError('PRODUCT_RAW_SOURCE_TEXT_REPLAY_MISMATCH')
    bound = strict_json((root / 'binding.json').read_text(encoding='utf-8'))
    if bound.get('native_runtime_mode', 'host_exclusive') != native_runtime_mode:
        raise FoundationError('PRODUCT_NATIVE_RUNTIME_MODE_BINDING_MISMATCH')
    docs = [Document(**doc) for doc in bound['documents']]
    api = inventory[-1]
    api_document = strict_json((root / 'api-document.json').read_text(encoding='utf-8'))
    if api_document != strict_json(api.text):
        raise FoundationError('PRODUCT_API_SNAPSHOT_MISMATCH')
    contract = BankContract(api_document, api.source_id, api.sha256)
    arguments = {}
    if bound['scope'] == 'bounded_explicit_typed_source':
        if [asdict(doc) for doc in docs] != [asdict(doc) for doc in inventory[:-1]]:
            raise FoundationError('PRODUCT_POLICY_SNAPSHOT_MISMATCH')
        request = make_request(docs, contract)
        replayed = validate_decisions(strict_json(TypedSourceProvider().generate(request)), docs, contract)
        if replayed != bound['compilation']['decisions']:
            raise FoundationError('PRODUCT_SOURCE_DECISION_REPLAY_MISMATCH')
        compiled = lower_compilation(bound['compilation'], docs, contract)
        wording = source_wording(docs)
    elif bound['scope'] == 'bounded-brief-requirements-1':
        if [asdict(doc) for doc in docs] != [asdict(doc) for doc in inventory[:-1]]:
            raise FoundationError('PRODUCT_POLICY_SNAPSHOT_MISMATCH')
        module = importlib.import_module('builder.brief_requirements')
        rebuilt = module.compile_requirement_documents(inventory[:-1], contract)
        if (rebuilt.get('publish_allowed') is not False or rebuilt['scope'] != bound['scope']
                or raw_json(rebuilt['compilation']) != raw_json(bound['compilation'])
                or raw_json(rebuilt['wording_bundle']) != raw_json(bound['wording_bundle'])
                or bound.get('wording_authorization') is not None
                or bound.get('required_flow') != {}):
            raise FoundationError('PRODUCT_BOUNDED_REQUIREMENTS_REPLAY_MISMATCH')
        compiled, docs, wording = rebuilt['compiled'], rebuilt['documents'], rebuilt['wording_bundle']
    else:
        module = importlib.import_module('builder.native_policy_bindings')
        rebuilt = module.bind_reviewed_native_sources(inventory[:-1], contract, source_snapshots=source_snapshots)
        if (rebuilt.get('publish_allowed') is not False or rebuilt['scope'] != bound['scope']
                or raw_json([asdict(doc) for doc in rebuilt['documents']]) != raw_json(bound['documents'])
                or raw_json(rebuilt['wording_bundle']) != raw_json(bound['wording_bundle'])
                or raw_json(rebuilt['wording_authorization']) != raw_json(bound['wording_authorization'])
                or raw_json(rebuilt['required_flow']) != raw_json(bound['required_flow'])):
            raise FoundationError('PRODUCT_REVIEWED_SOURCE_REPLAY_MISMATCH')
        compiled, docs, wording = rebuilt['compiled'], rebuilt['documents'], rebuilt['wording_bundle']
        arguments['wording_authorization'] = rebuilt['wording_authorization']
        arguments['source_snapshots'] = source_snapshots
    if raw_json(compiled.serializable()) != raw_json(strict_json((root / 'lowered-policy.json').read_text(encoding='utf-8'))):
        raise FoundationError('PRODUCT_REPLAYED_LOWERING_MISMATCH')
    if mode == 'official':
        official = strict_json((root / 'official-sources.json').read_text(encoding='utf-8'))
        if not isinstance(official, list) or [row.get('name') for row in official] != list(_official_source_names(bound)):
            raise FoundationError('PRODUCT_OFFICIAL_SOURCE_INVENTORY_INVALID')
        for row in official:
            raw = base64.b64decode(row['base64'], validate=True)
            doc = Document(**row['document'])
            if (sha(raw) != doc.sha256 or len(raw) != doc.byte_count
                    or not _official_provenance_path(doc.path, row['name'])):
                raise FoundationError('PRODUCT_OFFICIAL_SOURCE_HASH_MISMATCH')
            with tempfile.TemporaryDirectory(prefix='official-replay-', dir=OUTPUT) as temporary:
                path = Path(temporary) / ('source' + doc.kind)
                path.write_bytes(raw)
                parsed = read_source(path)
                parsed.path = doc.path
                if asdict(parsed) != asdict(doc):
                    raise FoundationError('PRODUCT_OFFICIAL_SOURCE_TEXT_MISMATCH')
        rebuilt_bound = dict(bound, compiled=compiled, documents=docs, wording_bundle=wording)
        candidate = _emit_official(rebuilt_bound, inventory[:-1], api, contract, source_snapshots, official, native_runtime_mode)
        repair_module = importlib.import_module('orchestration.native_behavioral_repair')
        receipt = strict_json((root / 'native-repair-evidence.json').read_text(encoding='utf-8'))
        checked = repair_module.verify_native_graph_receipt(receipt, compiled,
            official_evidence, source_reference=candidate)
        if (checked.get('valid') is not True or receipt['after_candidate'] != candidate
                or report.get('native_behavioral_repair') != {'status': checked['status'],
                    'receipt': 'native-repair-evidence.json',
                    'scope': 'One static farewell source-role binding; policy and banking repair unsupported',
                    'fault_injection_performed': False, 'platform_PASS': False}):
            raise FoundationError('PRODUCT_NATIVE_REPAIR_REPLAY_MISMATCH')
        if candidate != strict_json((root / 'candidate.json').read_text(encoding='utf-8')):
            raise FoundationError('PRODUCT_OFFICIAL_CANDIDATE_REPLAY_MISMATCH')
        if (report.get('full_publication_body_bytes') != _publication_bytes(candidate['workflow'])
                or report.get('publication_body_limit_bytes') != 131072
                or report.get('native_export_scope') != _export_scope(candidate)
                or report.get('native_readiness') != candidate['readiness']
                or report.get('unsupported') != candidate['readiness']['unsupported']
                or any(report.get(key) is not False for key in
                       ('native_playback_verified', 'official_PUB01_PASS', 'provider_final_export'))):
            raise FoundationError('PRODUCT_OFFICIAL_EXPORT_SCOPE_OR_BUDGET_MISMATCH')
    else:
        candidate = build_native_workflow(compiled, docs, wording, **arguments)
    if raw_json(candidate['workflow']) != raw_json(strict_json((root / 'agent.workflow.json').read_text(encoding='utf-8'))):
        raise FoundationError('PRODUCT_SOURCE_EXPORT_MISMATCH')
    evidence = (official_evidence(candidate, compiled) if mode == 'official' else
                critical_evidence(candidate, compiled,
                    right_party_required=bound.get('required_flow', {}).get('right_party_confirmation') is True))
    if (not evidence['passed'] or evidence != strict_json((root / 'critical-evidence.json').read_text(encoding='utf-8'))
            or evidence != strict_json((root / 'serialized-export-evidence.json').read_text(encoding='utf-8'))):
        raise FoundationError('PRODUCT_CRITICAL_REPLAY_MISMATCH')
    guard()
    return {'status': report['status'], 'valid': True, 'publish_allowed': False,
            'candidate_sha256': sha((root / 'agent.workflow.json').read_bytes()),
            'manifest_sha256': final['manifest_sha256'], 'critical_checks': len(evidence['checks'])}


def build_product_batch(*, jobs, output_directory, deadline_seconds=1800, native_mode='prefix', native_runtime_mode='host_exclusive'):
    """Two independent briefs, two fresh exports, a separate budget per brief."""
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('PRODUCT_DEADLINE_MUST_BE_1_TO_1800')
    if native_mode not in NATIVE_MODES:
        raise FoundationError('PRODUCT_NATIVE_MODE_INVALID')
    validate_runtime_mode(native_runtime_mode)
    if native_runtime_mode == 'official_bank_session' and native_mode != 'official':
        raise FoundationError('PRODUCT_BANK_SESSION_REQUIRES_OFFICIAL_EXPORT')
    if not isinstance(jobs, list) or len(jobs) != 2 or any(not isinstance(job, dict) for job in jobs):
        raise FoundationError('PRODUCT_BATCH_EXACTLY_TWO_JOBS_REQUIRED')
    identifiers = [job.get('id') for job in jobs]
    if len(set(identifiers)) != 2 or any(not isinstance(name, str) or not name
            or len(name) > 40 or not all(char.isascii() and (char.isalnum() or char in '-_') for char in name)
            for name in identifiers):
        raise FoundationError('PRODUCT_BATCH_IDENTIFIERS_INVALID')
    if any(set(job) != {'id', 'briefs', 'tool_api'} for job in jobs):
        raise FoundationError('PRODUCT_BATCH_JOB_SHAPE_INVALID')
    target = Path(output_directory).resolve()
    if target == OUTPUT.resolve() or not target.is_relative_to(OUTPUT.resolve()) or target.exists():
        raise FoundationError('PRODUCT_FRESH_CHILD_OUTPUT_REQUIRED')
    target.mkdir(parents=True)
    items = []
    for job in jobs:
        job_deadline = time.monotonic() + deadline_seconds
        result = build_product(brief_paths=job['briefs'], tool_api_path=job['tool_api'],
            output_directory=target / job['id'], deadline_seconds=deadline_seconds, native_mode=native_mode, native_runtime_mode=native_runtime_mode)
        independently_verified = False
        if result['status'] == 'LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED':
            try:
                independently_verified = verify_product_build(target / job['id'], deadline_monotonic=job_deadline)['valid']
            except (FoundationError, OSError, ValueError, TypeError, KeyError):
                independently_verified = False
                (target / job['id'] / 'FAILED_FINALIZATION.json').write_bytes(raw_json({'reason': 'PRODUCT_BATCH_INDEPENDENT_VERIFICATION_FAILED'}))
        items.append({'id': job['id'], 'status': result['status'],
            'candidate_sha256': result.get('candidate_sha256'),
            'independent_verification': independently_verified,
            'manifest_sha256': sha((target / job['id'] / 'manifest.json').read_bytes()),
            'deadline_seconds_per_brief': deadline_seconds, 'publish_allowed': False})
    report = {'version': 'two-source-products-1',
        'status': 'LOCAL_TWO_EXPORTS_VERIFIED' if all(item['independent_verification'] for item in items) else 'BLOCKED',
        'products': items, 'publish_allowed': False, 'publication_ready': False,
        'final_submission_ready': False, 'independent_brief_budgets': True,
        'network_calls': 0, 'model_calls': 0}
    (target / 'batch-report.json').write_bytes(raw_json(report))
    (target / 'BATCH_FINAL.json').write_bytes(raw_json({'status': report['status'],
        'report_sha256': sha((target / 'batch-report.json').read_bytes()), 'publish_allowed': False}))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--brief', action='append', type=Path)
    group.add_argument('--batch-spec', type=Path)
    group.add_argument('--verify', type=Path)
    parser.add_argument('--tool-api', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--native-mode', choices=NATIVE_MODES, default='prefix',
                        help='official emits the complete supported promise branch; prefix is partial.')
    parser.add_argument("--native-runtime-mode", choices=NATIVE_RUNTIME_MODES, default="host_exclusive", help="official_bank_session supports one uninterrupted fresh official session; no durable restart recovery.")
    args = parser.parse_args(argv)
    if args.verify:
        result = verify_product_build(args.verify)
    elif args.batch_spec:
        if args.out is None:
            parser.error('--out is required for generation')
        spec = strict_json(read_source(args.batch_spec).text)
        if not isinstance(spec, dict) or set(spec) != {'jobs'}:
            parser.error('Batch spec must contain exactly jobs')
        result = build_product_batch(jobs=spec['jobs'], output_directory=args.out,
                                     deadline_seconds=args.deadline_seconds, native_mode=args.native_mode, native_runtime_mode=args.native_runtime_mode)
    else:
        if args.out is None:
            parser.error('--out is required for generation')
        if args.tool_api is None:
            parser.error('--tool-api is required with --brief')
        result = build_product(brief_paths=args.brief, tool_api_path=args.tool_api,
                               output_directory=args.out, deadline_seconds=args.deadline_seconds,
                               native_mode=args.native_mode, native_runtime_mode=args.native_runtime_mode)
    print(raw_json(result).decode().strip())
    return 0 if result['status'] in {'LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED', 'LOCAL_TWO_EXPORTS_VERIFIED'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
