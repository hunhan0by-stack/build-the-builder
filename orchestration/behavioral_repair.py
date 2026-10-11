"""Bounded source-backed renderer repair with measured executable behavior.

This is a portable local workflow, not a native Freya agent. Optional faults are
explicitly synthetic renderer role-selection defects: wrong templates remain
genuine source quotes, and the unchanged guarded runtime actually speaks them.
Only diagnosed static-quote role bindings can be patched; bank-policy repair is
unsupported. Acceptance uses fixed source roles, bank rules and full event traces.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import socket
from string import Formatter
import subprocess
import sys
import time
import zipfile
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.compiled_policy import lower_compilation
from builder.generic_emitter import (canonical, digest, emit_compiled_workflow,
    execute_script, write_workflow, load_compiled)
from builder.ingest import collect_sources, read_source
from builder.models import FoundationError, strict_json
from compiler.core import compile_sources
from compiler.typed_source import TypedSourceProvider, source_wording

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/behavioral-repair'
API = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder/tools/openapi.json'


def create_demo_inputs(directory, memo_ceiling=237, *, api_path=None):
    """Fresh supported memo fixture; never edits a previous brief or evidence."""
    if type(memo_ceiling) is not int or not 100 <= memo_ceiling < 400:
        raise FoundationError('Fixture memo ceiling must be an integer in 100..399')
    from orchestration.generic_cycle import demo_inputs
    policies, script = demo_inputs(Path(directory), api_path=api_path)
    # Fresh fixture creation is automatic. This is not a generated-code repair.
    policies[1].write_text(policies[1].read_text(encoding='utf-8').replace(
        'ceiling with 275.', 'ceiling with ' + str(memo_ceiling) + '.'), encoding='utf-8')
    value = strict_json(script.read_text(encoding='utf-8'))
    for action in value['actions']:
        if action.get('value', {}).get('amount') == 275:
            action['value']['amount'] = memo_ceiling
    script.write_text(canonical(value) + '\n', encoding='utf-8')
    return policies, script


def _role_diagnosis(expected, observed):
    """Fixed oracle: exact full trace equality, then explain a bounded mismatch."""
    if observed == expected:
        return {'passed': True, 'code': 'EXACT_BEHAVIOR_EQUIVALENT', 'repairable': False,
                'changed_events': [], 'bank_requests_equal': True}
    if not isinstance(observed, dict) or not isinstance(observed.get('spoken_events'), list):
        return {'passed': False, 'code': 'UNSUPPORTED_BEHAVIOR_DIFFERENCE', 'repairable': False}
    # Text bindings and their local-fixture receipt hashes are derived from the
    # rendered text. Accepting them as a repair diagnosis is not accepting them
    # as passing behavior: the entire original trace still must match on retest.
    changed = []
    actual = observed['spoken_events']
    target = expected['spoken_events']
    if len(actual) == len(target):
        for wrong, right in zip(actual, target):
            if wrong != right:
                changed.append({'event_id': right['event_id'], 'kind': right['kind'],
                    'actual_text': wrong.get('text'), 'expected_text': right['text']})
    bank_equal = observed.get('bank_requests') == expected['bank_requests']
    # Only the specifically supported pair of source-role mistakes is repairable.
    by_kind = {row['kind']: row for row in target}
    swapped_quotes = all(row['actual_text'] == by_kind[
        'goodbye' if row['kind'] == 'opening' else 'opening']['text']
        for row in changed if row['kind'] in {'opening', 'goodbye'})
    repairable = (bank_equal and swapped_quotes and len(actual) == len(target) and len(changed) == 2
        and {row['kind'] for row in changed} == {'opening', 'goodbye'}
        and all(left.get('event_id') == right['event_id'] and left.get('kind') == right['kind']
                for left, right in zip(actual, target)))
    return {'passed': False, 'code': 'SOURCE_ROLE_RENDERER_MISMATCH' if repairable
            else 'UNSUPPORTED_BEHAVIOR_DIFFERENCE', 'repairable': repairable,
            'changed_events': changed, 'bank_requests_equal': bank_equal}


def _configuration_diagnosis(expected, observed, canonical_wording, candidate_wording):
    """Diagnose bounded source-quote role mistakes without hiding state changes.

    Only source-backed, static replacement quotes are supported. Bank/policy,
    event order, request IDs, rejected actions and unaffected state must match.
    Derived local speech bindings/receipt hashes are checked before normalizing.
    This is a deterministic configuration repair, never a bank-policy repair.
    """
    if observed == expected:
        return _role_diagnosis(expected, observed)
    blocked = {'passed': False, 'code': 'UNSUPPORTED_BEHAVIOR_DIFFERENCE',
               'repairable': False, 'changed_events': [], 'bank_requests_equal': False}
    if not isinstance(observed, dict) or observed.get('bank_requests') != expected.get('bank_requests'):
        return {**blocked, 'code': 'BANK_TRACE_MISMATCH'}
    blocked['bank_requests_equal'] = True
    try:
        changed_roles = [kind for kind in canonical_wording
                         if candidate_wording.get(kind) != canonical_wording[kind]]
        if (set(candidate_wording) != set(canonical_wording)
                or not 1 <= len(changed_roles) <= 2):
            return blocked
        for kind in changed_roles:
            entry = candidate_wording[kind]
            if (entry not in canonical_wording.values()
                    or any(field is not None for _, field, _, _ in Formatter().parse(entry['template']))):
                return blocked
        actual, target = observed['spoken_events'], expected['spoken_events']
        if not isinstance(actual, list) or len(actual) != len(target):
            return blocked
        changed, replacements = [], {}
        normalized = copy.deepcopy(observed)
        for index, (wrong, right) in enumerate(zip(actual, target)):
            if wrong == right:
                continue
            kind = right['kind']
            entry = candidate_wording.get(kind)
            if (kind not in changed_roles or set(wrong) != set(right)
                    or wrong['event_id'] != right['event_id'] or wrong['kind'] != kind
                    or wrong['text'] != entry['template'] or wrong['evidence'] != entry['evidence']):
                return blocked
            replacements[right['event_id']] = (wrong['text'], right['text'])
            changed.append({'event_id': right['event_id'], 'kind': kind,
                'actual_text': wrong['text'], 'expected_text': right['text']})
            normalized['spoken_events'][index] = copy.deepcopy(right)
        if not changed or {row['kind'] for row in changed} != set(changed_roles):
            return blocked
        if len(observed['checkpoints']) != len(expected['checkpoints']):
            return blocked
        pairs = list(zip(normalized['checkpoints'], expected['checkpoints']))
        pairs.append((normalized['final'], expected['final']))
        for wrong_state, right_state in pairs:
            for event_id, (wrong_text, right_text) in replacements.items():
                bindings = wrong_state.get('speech_bindings', {})
                if event_id in bindings:
                    if bindings[event_id] != wrong_text:
                        return blocked
                    bindings[event_id] = right_text
                receipts = wrong_state.get('speech_receipts', {})
                if event_id in receipts:
                    wrong_hash = hashlib.sha256(wrong_text.encode()).hexdigest()
                    expected_wrong = {'text_sha256': wrong_hash,
                        'receipt_sha256': digest({'event_id': event_id, 'status': 'completed',
                            'authority': 'local_fixture', 'text_sha256': wrong_hash}),
                        'authority': 'local_fixture', 'adapter_verified': True}
                    if receipts[event_id] != expected_wrong:
                        return blocked
                    receipts[event_id] = copy.deepcopy(right_state['speech_receipts'][event_id])
        if normalized != expected:
            return {**blocked, 'code': 'NON_RENDERER_STATE_DIFFERENCE'}
        return {'passed': False, 'code': 'SOURCE_ROLE_RENDERER_MISMATCH', 'repairable': True,
                'changed_events': changed, 'changed_roles': changed_roles,
                'bank_requests_equal': True, 'unaffected_trace_equal': True,
                'repair_scope': 'source_backed_static_renderer_role_config_only'}
    except (KeyError, TypeError, ValueError, AttributeError):
        return blocked


def _safe_directory(path):
    raw = Path(path).absolute()
    resolved = raw.resolve()
    if (not resolved.is_relative_to(OUTPUT.resolve()) or resolved == OUTPUT.resolve()
            or raw != resolved or resolved.exists()):
        raise FoundationError('Choose a fresh real child of artifacts/behavioral-repair')
    return resolved


def run_behavioral_cycle(*, output_directory, policy_paths, api_path, script_path,
        deadline_seconds=1800, inject_renderer_role_fault=False, started_monotonic=None,
        inject_single_role_fault=False):
    now = time.monotonic()
    start = now if started_monotonic is None else started_monotonic
    if type(start) not in (int, float) or not math.isfinite(start) or start > now:
        raise FoundationError('Invalid shared production start time')
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('Brief limit must be 1..1800 seconds')
    if (type(inject_renderer_role_fault) is not bool or type(inject_single_role_fault) is not bool
            or inject_renderer_role_fault and inject_single_role_fault):
        raise FoundationError('Explicit boolean diagnostic fixture selection required')
    deadline = start + deadline_seconds
    package = _safe_directory(output_directory)
    package.mkdir(parents=True)
    report = {'status': 'BLOCKED', 'scope': ['single_product', 'single_promise',
        'structured_customer_inputs'], 'native_freya_export': False, 'publish_allowed': False,
        'platform_success': False, 'receipt_authority': 'local_fixture', 'model_calls': 0,
        'network_calls': 0, 'network_attempts': 0, 'manual_code_changes_during_cycle': False,
        'injected_renderer_role_fault': inject_renderer_role_fault,
        'injected_single_role_fault': inject_single_role_fault,
        'synthetic_defect_kind': ('single_static_source_role_binding' if inject_single_role_fault
            else 'paired_static_source_role_bindings' if inject_renderer_role_fault else None),
        'bank_policy_repair_supported': False,
        'deadline_seconds': deadline_seconds, 'attempts': [], 'passed_tests': [],
        'failed_tests': [], 'repair_kind': 'not_needed', 'measured_changed_behavior': False}

    def check_deadline():
        if time.monotonic() >= deadline:
            raise FoundationError('Shared brief deadline exceeded')

    def save(name, value):
        check_deadline()
        (package / name).write_text(canonical(value) + '\n', encoding='utf-8')

    def deny(*args, **kwargs):
        report['network_attempts'] += 1
        raise FoundationError('Network access forbidden in local behavioral repair')

    try:
        check_deadline()
        raw = [read_source(Path(path)) for path in [*policy_paths, api_path, script_path]]
        pins = {doc.path: doc.sha256 for doc in raw}
        documents = collect_sources([Path(path) for path in policy_paths])
        api = read_source(Path(api_path))
        api_document = strict_json(api.text)
        contract = BankContract(api_document, api.source_id, api.sha256)
        # Unsupported sentences fail here; no natural-language or model fallback.
        envelope = compile_sources(policy_paths=[Path(path) for path in policy_paths],
            tool_api_path=Path(api_path), provider=TypedSourceProvider())
        compiled = lower_compilation(envelope, documents, contract)
        canonical_wording = source_wording(documents)
        script = strict_json(Path(script_path).read_text(encoding='utf-8'))
        save('source-inventory.json', [asdict(doc) for doc in raw])
        save('compilation.json', envelope)
        save('lowered-policy.json', compiled.serializable())
        save('script.json', script)
        upstream = {'compilation': envelope, 'api_document': api_document}
        canonical_workflow = emit_compiled_workflow(compiled, documents, contract,
            canonical_wording, pins, upstream)
        with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
            expected = execute_script(compiled, documents, canonical_wording,
                script['variables'], script['actions'], package / 'canonical.jsonl')
        operations = [row['operation'] for row in expected['bank_requests']]
        if (operations != ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']
                or expected['final']['phase'] != 'closed'):
            raise FoundationError('Unsupported terminal scenario: one promise and completed close required')
        save('canonical-trace.json', expected)
        report['passed_tests'].append('canonical_source_execution_and_one_promise_safety')
        source_oracle_hash = digest({'compilation': envelope, 'wording': canonical_wording,
            'script': script, 'expected_trace': expected})
        report['fixed_acceptance_sha256'] = source_oracle_hash
        candidate_wording = copy.deepcopy(canonical_wording)
        if inject_renderer_role_fault:
            candidate_wording['opening'], candidate_wording['goodbye'] = (
                candidate_wording['goodbye'], candidate_wording['opening'])
        elif inject_single_role_fault:
            candidate_wording['proposal_request'] = copy.deepcopy(candidate_wording['opening'])
        for attempt in (1, 2):
            check_deadline()
            candidate = emit_compiled_workflow(compiled, documents, contract,
                candidate_wording, pins, upstream)
            save('workflow-' + str(attempt) + '.json', {k: v for k, v in candidate.items() if k != 'code'})
            export = write_workflow(candidate, package / ('workflow-' + str(attempt) + '.pyz'))
            out = package / ('export-trace-' + str(attempt) + '.json')
            process = subprocess.run([sys.executable, '-I', str(export), '--script', str(package / 'script.json'),
                '--journal', str(package / ('export-' + str(attempt) + '.jsonl')), '--out', str(out)],
                cwd=package, capture_output=True, text=True, timeout=max(.01, deadline - time.monotonic()))
            check_deadline()
            if process.returncode != 0:
                save('execution-error-' + str(attempt) + '.json', {'returncode': process.returncode,
                    'stderr': process.stderr[-4000:]})
                raise FoundationError('Execution failure: semantic repair not justified')
            observed = strict_json(out.read_text(encoding='utf-8'))
            diagnosis = _configuration_diagnosis(expected, observed, canonical_wording, candidate_wording)
            save('diagnosis-' + str(attempt) + '.json', diagnosis)
            record = {'attempt': attempt, 'returncode': process.returncode, **diagnosis,
                'fixed_acceptance_sha256': source_oracle_hash, 'observed_trace_sha256': digest(observed)}
            report['attempts'].append(record)
            if not diagnosis['passed']:
                report['failed_tests'].append('canonical_behavior_equivalence_attempt_' + str(attempt))
                if not (attempt == 1 and diagnosis['repairable']
                        and (inject_renderer_role_fault or inject_single_role_fault)):
                    raise FoundationError('Unrecognized behavior mismatch; automatic repair refused')
                # Patch only diagnosed configuration entries. Rules/IR/runtime,
                # scripts, oracle and all unaffected wording remain unchanged.
                before = copy.deepcopy(candidate_wording)
                source_bindings = source_wording(documents)
                changed_roles = diagnosis['changed_roles']
                changes = []
                for kind in changed_roles:
                    changes.append({'path': '/wording/' + kind, 'before': before[kind],
                        'after': source_bindings[kind], 'source_requirement': source_bindings[kind]['evidence']})
                    candidate_wording[kind] = copy.deepcopy(source_bindings[kind])
                unchanged = all(before[k] == candidate_wording[k] for k in before if k not in changed_roles)
                if not unchanged:
                    raise FoundationError('Repair changed unrelated configuration')
                save('repair-plan.json', {'schema_version': 'minimal-source-role-repair-1',
                    'changes': changes, 'unaffected_config_equal': unchanged,
                    'bank_requests_equal': diagnosis['bank_requests_equal'],
                    'unaffected_trace_equal': diagnosis['unaffected_trace_equal'],
                    'fixed_acceptance_sha256': source_oracle_hash,
                    'compilation_sha256': digest(envelope),
                    'runtime_code_sha256': canonical_workflow['code_sha256'],
                    'before_wording_sha256': digest(before), 'after_wording_sha256': digest(candidate_wording)})
                report['repair_kind'] = ('source_backed_renderer_role_rebinding' if inject_renderer_role_fault
                                        else 'source_backed_renderer_minimal_patch')
                report['repair_changed_config_paths'] = [row['path'] for row in changes]
                report['measured_changed_behavior'] = True
                continue
            report['passed_tests'].extend(['isolated_executable_behavior', 'exact_spoken_and_bank_trace_equivalence',
                'single_promise_across_restart', 'close_after_matching_local_fixture_completion'])
            report['verified_export'] = export.name
            report['verified_workflow'] = 'workflow-' + str(attempt) + '.json'
            report['verified_trace'] = out.name
            break
        else:
            raise FoundationError('Bounded repair exhausted')
        if digest({'compilation': envelope, 'wording': canonical_wording, 'script': script,
                   'expected_trace': expected}) != source_oracle_hash:
            raise FoundationError('Acceptance criteria changed during repair')
        if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != pin for path, pin in pins.items()):
            raise FoundationError('Source changed during production')
        if any(hashlib.sha256((ROOT / path).read_text(encoding='utf-8').encode()).hexdigest() != pin
               for path, pin in canonical_workflow['code_sha256'].items()):
            raise FoundationError('Original runtime changed during production')
        if report['network_attempts']:
            raise FoundationError('Network access attempted')
        report['source_and_code_unchanged'] = True
        report['status'] = 'LOCAL_BEHAVIORAL_EXPORT_PASS'
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    report['total_production_seconds'] = round(time.monotonic() - start, 6)
    report['within_30_minute_brief_limit'] = time.monotonic() < deadline
    if not report['within_30_minute_brief_limit']:
        report['status'] = 'BLOCKED'
    try:
        commit = subprocess.run(['git', '-c', 'safe.directory=' + ROOT.as_posix(), 'rev-parse', 'HEAD'],
            cwd=ROOT, capture_output=True, text=True, timeout=2)
        report['commit_reference'] = commit.stdout.strip() if commit.returncode == 0 else None
    except Exception:
        report['commit_reference'] = None
    (package / 'report.json').write_text(canonical(report) + '\n', encoding='utf-8')
    inventory = [{'path': path.name, 'bytes': path.stat().st_size,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(package.iterdir()) if path.is_file()]
    (package / 'manifest.json').write_text(canonical(inventory) + '\n', encoding='utf-8')
    final = {'status': report['status'], 'manifest_sha256': hashlib.sha256((package / 'manifest.json').read_bytes()).hexdigest(),
        'elapsed_seconds': round(time.monotonic() - start, 6), 'deadline_seconds': deadline_seconds}
    if time.monotonic() >= deadline:
        final['status'] = report['status'] = 'BLOCKED'
    (package / 'FINAL.json').write_text(canonical(final) + '\n', encoding='utf-8')
    return report


def verify_behavioral_cycle(directory):
    """Read-only artifact validation; does not claim independent root trust."""
    root = Path(directory)
    final = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
    elapsed, limit = final.get('elapsed_seconds'), final.get('deadline_seconds')
    if (final.get('status') != 'LOCAL_BEHAVIORAL_EXPORT_PASS' or type(limit) is not int
        or not 1 <= limit <= 1800 or type(elapsed) not in (float, int)
        or not math.isfinite(elapsed) or not 0 <= elapsed < limit):
        raise FoundationError('Successful bounded final receipt required')
    if hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest() != final['manifest_sha256']:
        raise FoundationError('Manifest mismatch')
    rows = strict_json((root / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len({row['path'] for row in rows}) != len(rows):
        raise FoundationError('Unique evidence manifest required')
    if {p.name for p in root.iterdir()} != {row['path'] for row in rows} | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('Evidence inventory mismatch')
    for row in rows:
        path = root / row['path']
        if path.is_symlink() or path.resolve().parent != root.resolve() or path.stat().st_size != row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise FoundationError('Evidence bytes mismatch')
    report = strict_json((root / 'report.json').read_text(encoding='utf-8'))
    if report['status'] != final['status'] or report['publish_allowed'] is not False or report['platform_success'] is not False or report['network_attempts']:
        raise FoundationError('Local boundary mismatch')
    names = {row['path'] for row in rows}
    if any(report.get(key) not in names for key in ('verified_workflow', 'verified_export', 'verified_trace')):
        raise FoundationError('Verified export paths absent from inventory')
    workflow = strict_json((root / report['verified_workflow']).read_text(encoding='utf-8'))
    with zipfile.ZipFile(root / report['verified_export']) as archive:
        bundle = strict_json(archive.read('workflow.json').decode())
        if bundle != workflow['bundle'] or digest(bundle) != workflow['bundle_sha256']:
            raise FoundationError('Executable bundle mismatch')
        for name, pin in workflow['code_sha256'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != pin:
                raise FoundationError('Executable code mismatch')
    compiled, documents = load_compiled(bundle)
    if bundle['wording'] != source_wording(documents):
        raise FoundationError('Final renderer role binding differs from original source')
    expected = strict_json((root / 'canonical-trace.json').read_text(encoding='utf-8'))
    observed = strict_json((root / report['verified_trace']).read_text(encoding='utf-8'))
    if expected != observed or not _role_diagnosis(expected, observed)['passed']:
        raise FoundationError('Final measured behavior differs')
    script = strict_json((root / 'script.json').read_text(encoding='utf-8'))
    source_oracle = digest({'compilation': bundle['compilation'], 'wording': source_wording(documents),
        'script': script, 'expected_trace': expected})
    if report['fixed_acceptance_sha256'] != source_oracle or any(row['fixed_acceptance_sha256'] != source_oracle for row in report['attempts']):
        raise FoundationError('Acceptance criteria changed')
    if report['measured_changed_behavior']:
        initial = strict_json((root / 'export-trace-1.json').read_text(encoding='utf-8'))
        initial_workflow = strict_json((root / 'workflow-1.json').read_text(encoding='utf-8'))
        before_wording = initial_workflow['bundle']['wording']
        canonical_wording = source_wording(documents)
        diagnosis = _configuration_diagnosis(expected, initial, canonical_wording, before_wording)
        if (not diagnosis.get('repairable') or diagnosis['passed']
                or report['repair_kind'] not in {'source_backed_renderer_role_rebinding', 'source_backed_renderer_minimal_patch'}):
            raise FoundationError('Repair lacks an executed behavioral defect')
        # New receipts carry a minimal-diff plan. Historical role-swap receipts
        # remain readable without pretending they included this newer evidence.
        if 'repair-plan.json' in names:
            plan = strict_json((root / 'repair-plan.json').read_text(encoding='utf-8'))
            expected_changes = [{'path': '/wording/' + kind, 'before': before_wording[kind],
                'after': canonical_wording[kind], 'source_requirement': canonical_wording[kind]['evidence']}
                for kind in diagnosis['changed_roles']]
            if (plan['changes'] != expected_changes or plan['unaffected_config_equal'] is not True
                    or plan['unaffected_trace_equal'] is not True or plan['bank_requests_equal'] is not True
                    or plan['fixed_acceptance_sha256'] != source_oracle
                    or plan['compilation_sha256'] != digest(bundle['compilation'])
                    or plan['before_wording_sha256'] != digest(before_wording)
                    or plan['after_wording_sha256'] != digest(bundle['wording'])
                    or plan['runtime_code_sha256'] != workflow['code_sha256']):
                raise FoundationError('Minimal repair plan differs from executed evidence')
    return {'valid': True, 'status': report['status'], 'manifest_sha256': final['manifest_sha256'],
            'measured_changed_behavior': report['measured_changed_behavior'], 'platform_success': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--memo-ceiling', type=int, default=237)
    parser.add_argument('--policy', type=Path, action='append')
    parser.add_argument('--tool-api', type=Path, default=API)
    parser.add_argument('--script', type=Path)
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--inject-renderer-role-fault', action='store_true')
    parser.add_argument('--inject-single-role-fault', action='store_true')
    args = parser.parse_args()
    started = time.monotonic()
    if args.demo:
        if args.policy or args.script:
            parser.error('Demo cannot be combined with existing inputs')
        policies, script = create_demo_inputs(args.out.with_name(args.out.name + '-inputs'), args.memo_ceiling,
                                             api_path=args.tool_api)
    else:
        if not args.policy or not args.script:
            parser.error('Existing typed policies and structured script required')
        policies, script = args.policy, args.script
    result = run_behavioral_cycle(output_directory=args.out, policy_paths=policies, api_path=args.tool_api,
        script_path=script, deadline_seconds=args.deadline_seconds,
        inject_renderer_role_fault=args.inject_renderer_role_fault, started_monotonic=started,
        inject_single_role_fault=args.inject_single_role_fault)
    if result['status'] == 'LOCAL_BEHAVIORAL_EXPORT_PASS':
        result['verification'] = verify_behavioral_cycle(args.out)
    print(canonical(result))
    return 0 if result['status'] == 'LOCAL_BEHAVIORAL_EXPORT_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
