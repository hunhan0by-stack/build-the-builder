"""Bounded native graph repair, executed locally without publication authority.

Only a single static source-quote role binding at the successful technical
farewell can be repaired. This is distinct from the portable renderer demo.
No bank policy, generated code, source requirement or oracle is rewritten.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import socket
import tempfile
import time
from unittest.mock import patch

from builder.models import FoundationError, strict_json
from builder.native_kit_emitter import build_official_native_workflow, _cfg
from builder.native_kit_verification import (
    local_http, local_variables, prepare_local_pub01, verify_local_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
LEAF = ('nodes', 'official_goodbye', 'tools', 0, 'function', 'parameters',
        'properties', 'spoken_message', 'default')
ALLOWED_PATH = '/workflow/' + '/'.join(map(str, LEAF))
PAYLOAD_CAP = 131072
BRIEF_CAP = 1800


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':'))


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def publication_bytes(candidate):
    """Exact standalone publisher serialization, including its fixed size name."""
    body = {'name': 'Native-A-PUB01 00000000-0000-0000-0000-000000000000',
            'workflow': candidate['workflow']}
    return len(json.dumps(body, allow_nan=False).encode())


def validate_standalone_report(report, before, candidate):
    """Reject resealed producer size/time claims and local-authority promotion.

    Time is retained local timer evidence, not independently attested execution
    duration. Validating finite values and ordering does not grant a trusted clock.
    The complete graph and callback traces are separately reexecuted below.
    """
    measured_bytes = publication_bytes(candidate)
    if (type(report.get('whole_publication_body_bytes')) is not int
            or report['whole_publication_body_bytes'] != measured_bytes
            or measured_bytes >= PAYLOAD_CAP
            or type(report.get('payload_limit_bytes')) is not int
            or report['payload_limit_bytes'] != PAYLOAD_CAP
            or type(report.get('brief_limit_seconds')) is not int
            or report['brief_limit_seconds'] != BRIEF_CAP):
        raise FoundationError('NATIVE_REPAIR_REPORT_PAYLOAD_OR_CAP_DIFFERS')
    elapsed, whole = report.get('elapsed_seconds'), report.get('whole_elapsed_seconds_before_final_seal')
    if (type(elapsed) not in (int, float) or type(whole) not in (int, float)
            or not math.isfinite(elapsed) or not math.isfinite(whole)
            or not 0 <= elapsed <= whole < BRIEF_CAP):
        raise FoundationError('NATIVE_REPAIR_REPORT_ELAPSED_INVALID')
    for field in ('platform_PASS', 'official_PUB01_PASS', 'native_authority_granted',
                  'bank_policy_repair_supported', 'natural_failed_memo_demonstrated',
                  'portable_renderer_repair'):
        if report.get(field) is not False:
            raise FoundationError('NATIVE_REPAIR_REPORT_AUTHORITY_PROMOTED')
    if (type(report.get('network_calls')) is not int or report['network_calls'] != 0
            or report.get('source_commit') is not None
            or report.get('source_commit_not_attested_by_this_runner') is not True
            or report.get('source_before_after_equal') is not True
            or report.get('passed') is not True):
        raise FoundationError('NATIVE_REPAIR_REPORT_SCOPE_DIFFERS')
    if (report.get('workflow_before_sha256') != sha(before['workflow'])
            or report.get('workflow_after_sha256') != sha(candidate['workflow'])
            or before.get('workflow_sha256') != sha(before['workflow'])
            or candidate.get('workflow_sha256') != sha(candidate['workflow'])):
        raise FoundationError('NATIVE_REPAIR_REPORT_GRAPH_DIGEST_DIFFERS')


def leaf(graph):
    value = graph
    for key in LEAF:
        value = value[key]
    return value


def set_leaf(graph, value):
    parent = graph
    for key in LEAF[:-1]:
        parent = parent[key]
    parent[LEAF[-1]] = value


def failclosed_bank_checks(candidate):
    """Execute actual hooks/result code with explicitly unconfirmed fixtures."""
    results = []
    for kind, envelope in (
        ('generic_500', local_http({'ok': True}, status_code=500)),
        ('malformed_ok', local_http({'ok': 'true'})),
    ):
        with tempfile.TemporaryDirectory(prefix='native-repair-safety-') as registry:
            h = prepare_local_pub01(candidate, local_variables(registry))
            h.receive({'kind': 'confirm', 'readback_token': h.state['readback_token'],
                       'explicit_yes': True})
            h.dispatch('create_promise')
            h.resume('create_promise', envelope)
            replay_blocked = False
            try:
                h.resume('create_promise', envelope)
            except FoundationError:
                replay_blocked = True
            operations = [r['operation'] for r in h.requests]
            passed = (operations.count('create_promise') == 1
                      and 'close_call' not in operations
                      and h.current_node == 'native_blocked'
                      and not h.variables.get('_native_close_armed')
                      and replay_blocked
                      and not any(e['kind'] == 'promise_success' for e in h.state['events']))
            results.append({'fixture': kind, 'passed': passed,
                            'requests': h.requests, 'semantic_events': h.state['events'],
                            'final_phase': h.state['phase'], 'node': h.current_node,
                            'replayed_result_blocked': replay_blocked,
                            'automatic_retry_observed': operations.count('create_promise') > 1})
    return {'passed': all(r['passed'] for r in results), 'fixtures': results,
            'evidence_kind': 'local_actual_export_execution_with_synthetic_bank_results',
            'native_restart_guarantee_claim': False}


def diagnose(reference, candidate, expected_trace, observed_trace):
    """Fixed complete trace oracle plus exact one-leaf graph delta grammar."""
    rejected = {'passed': False, 'repairable': False, 'code': 'UNSUPPORTED_NATIVE_DIFFERENCE'}
    try:
        # Export metadata may never gain authority during a local repair.
        ignored = {'workflow', 'workflow_sha256'}
        if ({k: v for k, v in candidate.items() if k not in ignored}
                != {k: v for k, v in reference.items() if k not in ignored}):
            return rejected
        if candidate.get('workflow_sha256') != sha(candidate['workflow']):
            return {**rejected, 'code': 'NATIVE_CANDIDATE_DIGEST_MISMATCH'}
        if candidate['workflow'] == reference['workflow'] and observed_trace == expected_trace:
            return {'passed': True, 'repairable': False, 'code': 'EXACT_NATIVE_TRACE_EQUIVALENT'}
        fixed_graph = copy.deepcopy(candidate['workflow'])
        wrong, right = leaf(fixed_graph), leaf(reference['workflow'])
        source_wording = _cfg(reference)['wording']
        # Only static, genuine source quotes qualify; no generated free text.
        static_quotes = {v['template'] for v in source_wording.values()
                         if '{' not in v['template'] and '}' not in v['template']}
        if wrong == right or wrong not in static_quotes or right != source_wording['goodbye']['template']:
            return rejected
        set_leaf(fixed_graph, right)
        if fixed_graph != reference['workflow']:
            return rejected
        normalized = copy.deepcopy(observed_trace)
        if normalized['terminal_fixture']['farewell'] != wrong:
            return rejected
        normalized['terminal_fixture']['farewell'] = right
        if normalized != expected_trace:
            return {**rejected, 'code': 'UNAFFECTED_NATIVE_TRACE_MISMATCH'}
        return {'passed': False, 'repairable': True, 'code': 'NATIVE_STATIC_FAREWELL_ROLE_MISMATCH',
                'allowed_path': ALLOWED_PATH, 'actual_quote': wrong, 'expected_quote': right,
                'bank_requests_equal': observed_trace['requests'] == expected_trace['requests'],
                'unaffected_complete_trace_equal': True}
    except (KeyError, TypeError, IndexError, FoundationError):
        return rejected


def repair_candidate(reference, candidate, diagnosis):
    if diagnosis.get('repairable') is not True or diagnosis.get('allowed_path') != ALLOWED_PATH:
        raise FoundationError('NATIVE_REPAIR_OUTSIDE_BOUNDED_GRAMMAR')
    fixed = copy.deepcopy(candidate)
    set_leaf(fixed['workflow'], leaf(reference['workflow']))
    if fixed['workflow'] != reference['workflow']:
        raise FoundationError('NATIVE_REPAIR_UNAFFECTED_GRAPH_CHANGED')
    fixed['workflow_sha256'] = reference['workflow_sha256']
    return fixed


def diagnose_and_repair_native(candidate, compiled, official_evidence, *, source_reference):
    """Main Builder bridge with its unchanged, source-derived generic oracle.

    source_reference must be freshly emitted from the same immutable source
    bundle/compiled policy, never copied from a failed graph. No fault is injected
    here. The caller retains publication/readback and serialized-export gates.
    The callback is reviewed executable code, not producer-provided PASS JSON.
    """
    def execute(graph):
        with patch.object(socket.socket, 'connect', side_effect=FoundationError('NETWORK_FORBIDDEN')), \
             patch('urllib.request.OpenerDirector.open', side_effect=FoundationError('NETWORK_FORBIDDEN')):
            return official_evidence(graph, compiled)
    expected = execute(source_reference)
    before = execute(candidate)
    if expected.get('passed') is not True:
        raise FoundationError('NATIVE_REPAIR_SOURCE_REFERENCE_FAILED')
    diagnosis = diagnose(source_reference, candidate, expected['trace'], before['trace'])
    unaffected = lambda evidence: [item for item in evidence['checks']
                                  if item['id'] != 'source_farewell_local_fixture']
    if unaffected(before) != unaffected(expected):
        raise FoundationError('NATIVE_REPAIR_UNAFFECTED_REQUIREMENT_CHANGED')
    repaired = candidate
    after = before
    status = 'not_needed'
    if diagnosis['passed']:
        if before != expected or not before.get('passed'):
            raise FoundationError('NATIVE_REPAIR_FIXED_REQUIREMENT_ORACLE_FAILED')
    else:
        repaired = repair_candidate(source_reference, candidate, diagnosis)
        after = execute(repaired)
        if not after.get('passed') or after != expected:
            raise FoundationError('NATIVE_REPAIR_FIXED_REQUIREMENT_RETEST_FAILED')
        status = 'repaired'
    receipt = {'schema_version': 'native-graph-repair-receipt-1', 'status': status,
        'failure_origin': 'none' if status == 'not_needed' else 'provided_graph_role_defect',
        'natural_failed_memo_demonstrated': False, 'fault_injection_performed': False,
        'allowed_path': ALLOWED_PATH if status == 'repaired' else None,
        'compiled_policy_sha256': sha(compiled.serializable()),
        'source_reference_graph_sha256': sha(source_reference['workflow']),
        'fixed_evidence_sha256': sha(expected), 'before_candidate': copy.deepcopy(candidate),
        'after_candidate': copy.deepcopy(repaired), 'before_evidence': before,
        'after_evidence': after, 'diagnosis': diagnosis,
        'before_graph_sha256': sha(candidate['workflow']),
        'after_graph_sha256': sha(repaired['workflow']),
        'bank_requests_equal': before['trace']['requests'] == after['trace']['requests'],
        'full_repaired_trace_equals_fixed_oracle': after == expected,
        'bank_guard_code_changed': False, 'bank_policy_repair_supported': False,
        'unknown_bank_fixture_run_for_this_memo': False,
        'network_calls': 0, 'platform_PASS': False, 'publish_allowed': False}
    return {'candidate': repaired, 'evidence': after, 'receipt': receipt, 'status': status}


def verify_native_graph_receipt(receipt, compiled, official_evidence, *, source_reference):
    """Reexecute archived pre/post graphs against a freshly source-emitted oracle."""
    replay = diagnose_and_repair_native(receipt['before_candidate'], compiled, official_evidence,
                                       source_reference=source_reference)
    if replay['receipt'] != receipt:
        raise FoundationError('NATIVE_GRAPH_REPAIR_RECEIPT_REEXECUTION_DIFFERS')
    return {'valid': True, 'status': replay['status'],
            'attempts_reexecuted': 1 if replay['status'] == 'not_needed' else 2,
            'bank_requests_equal': receipt['bank_requests_equal'],
            'fixed_evidence_sha256': receipt['fixed_evidence_sha256'],
            'platform_PASS': False, 'network_calls': 0}


def run_native_repair(briefs, api, kit, output, *, inject_fault=False, failed_candidate=None):
    """One command: fresh source compile, execution, diagnosis, patch, retest, export.

    A naturally failed candidate is accepted only inside the same narrow grammar.
    Unsupported briefs retain the compiler rejection, never become supported.
    """
    started = time.monotonic()
    out = Path(output).resolve()
    if not out.is_relative_to((ROOT / 'artifacts').resolve()) or out.exists():
        raise FoundationError('FRESH_WORKSPACE_ARTIFACT_OUTPUT_REQUIRED')
    if inject_fault and failed_candidate is not None:
        raise FoundationError('EXPLICIT_SINGLE_FAILURE_ORIGIN_REQUIRED')
    source_pins = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for folder in ('builder', 'compiler', 'orchestration', 'integration')
                  for path in sorted((ROOT / folder).glob('*.py'))}
    with patch.object(socket.socket, 'connect', side_effect=FoundationError('NETWORK_FORBIDDEN')), \
         patch('urllib.request.OpenerDirector.open', side_effect=FoundationError('NETWORK_FORBIDDEN')):
        reference = build_official_native_workflow(briefs, api, kit)
        input_pins = {item['path']: item['sha256'] for item in reference['input_inventory']}
        for name in ('docs/tools-and-code.md', 'docs/workflow-format.md',
                     'examples/base-agent.json', 'briefs/brief-a.md'):
            path = (Path(kit) / name).resolve()
            input_pins[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        cfg = _cfg(reference)
        expected = verify_local_candidate(reference)
        candidate = copy.deepcopy(reference if failed_candidate is None else failed_candidate)
        if inject_fault:
            set_leaf(candidate['workflow'], cfg['wording']['identity_question']['template'])
            candidate['workflow_sha256'] = sha(candidate['workflow'])
        initial = verify_local_candidate(candidate)
        diagnosis = diagnose(reference, candidate, expected, initial)
        attempts = [{'attempt': 1, 'passed': diagnosis['passed'], 'trace': initial}]
        repaired = candidate
        if not diagnosis['passed']:
            repaired = repair_candidate(reference, candidate, diagnosis)
            final = verify_local_candidate(repaired)
            result = diagnose(reference, repaired, expected, final)
            attempts.append({'attempt': 2, 'passed': result['passed'], 'trace': final})
            if not result['passed']:
                raise FoundationError('NATIVE_REPAIR_FIXED_ORACLE_RETEST_FAILED')
        safety = failclosed_bank_checks(repaired)
        if not safety['passed']:
            raise FoundationError('NATIVE_REPAIR_BANK_FAILCLOSED_FAILED')
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != pin for path, pin in input_pins.items()):
        raise FoundationError('NATIVE_REPAIR_SOURCE_ORACLE_CHANGED')
    if any(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != pin for path, pin in source_pins.items()):
        raise FoundationError('NATIVE_REPAIR_IMPLEMENTATION_CHANGED_DURING_RUN')
    size = publication_bytes(repaired)
    elapsed = time.monotonic() - started
    if size >= PAYLOAD_CAP or elapsed >= BRIEF_CAP:
        raise FoundationError('NATIVE_REPAIR_PAYLOAD_OR_30_MINUTE_LIMIT')
    report = {'schema_version': 'native-behavioral-repair-1', 'passed': True,
              'scope': 'native exported graph locally executed; static farewell role repair only',
              'failure_origin': 'explicit_injected_fixture' if inject_fault else
                  ('provided_failed_candidate' if failed_candidate is not None else 'none'),
              'natural_failed_memo_demonstrated': False, 'portable_renderer_repair': False,
              'platform_PASS': False, 'official_PUB01_PASS': False,
              'network_calls': 0, 'bank_policy_repair_supported': False,
              'diagnosis': diagnosis, 'attempts': attempts,
              'repair_cycles': len(attempts) - 1, 'input_sha256': input_pins,
              'source_arguments': {'briefs': [str(Path(p).resolve()) for p in briefs],
                                   'api': str(Path(api).resolve()), 'kit': str(Path(kit).resolve())},
              'fixed_oracle_trace_sha256': sha(expected), 'safety': safety,
              'whole_publication_body_bytes': size, 'payload_limit_bytes': PAYLOAD_CAP,
              'elapsed_seconds': elapsed, 'brief_limit_seconds': BRIEF_CAP,
              'command_argv': ['python', '-m', 'orchestration.native_behavioral_repair',
                  *[item for brief in briefs for item in ('--brief', str(brief))],
                  '--tool-api', str(api), '--kit', str(kit), '--output', str(out),
                  *(['--inject-farewell-role-fault'] if inject_fault else [])],
              'workflow_before_sha256': sha(candidate['workflow']),
              'workflow_after_sha256': sha(repaired['workflow']),
              'source_sha256': source_pins, 'source_before_after_equal': True,
              'source_commit': None, 'source_commit_not_attested_by_this_runner': True,
              'native_authority_granted': False}
    out.mkdir(parents=True)
    files = {'repair-proof.json': report, 'candidate.json': repaired, 'before-candidate.json': candidate,
             'workflow.json': repaired['workflow'], 'fixed-oracle.json': expected,
             'attempts.json': attempts, 'bank-safety.json': safety}
    for name, value in files.items():
        (out / name).write_text(canonical(value) + '\n', encoding='utf-8')
    report['whole_elapsed_seconds_before_final_seal'] = time.monotonic() - started
    if report['whole_elapsed_seconds_before_final_seal'] >= BRIEF_CAP:
        raise FoundationError('NATIVE_REPAIR_30_MINUTE_LIMIT_DURING_ARTIFACT_SEAL')
    (out / 'repair-proof.json').write_text(canonical(report) + '\n', encoding='utf-8')
    manifest = {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in files}
    (out / 'manifest.json').write_text(canonical({'schema_version': 'native-repair-manifest-1',
        'files': manifest, 'integrity_scope': 'local checksums; independent anchor required'}) + '\n', encoding='utf-8')
    if time.monotonic() - started >= BRIEF_CAP:
        raise FoundationError('NATIVE_REPAIR_30_MINUTE_LIMIT_AFTER_ARTIFACT_SEAL')
    return report


def verify_native_repair(directory):
    """Recompile fixed source and reexecute both recorded graphs, not PASS flags."""
    directory = Path(directory).resolve()
    if not directory.is_relative_to((ROOT / 'artifacts').resolve()) or directory.is_symlink():
        raise FoundationError('NATIVE_REPAIR_EVIDENCE_OUTSIDE_WORKSPACE')
    manifest = strict_json((directory / 'manifest.json').read_text())
    expected_files = {'repair-proof.json', 'candidate.json', 'before-candidate.json',
                      'workflow.json', 'fixed-oracle.json', 'attempts.json', 'bank-safety.json'}
    if set(manifest.get('files', {})) != expected_files:
        raise FoundationError('NATIVE_REPAIR_EVIDENCE_FILE_SET_CHANGED')
    for name, pin in manifest['files'].items():
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != pin:
            raise FoundationError('NATIVE_REPAIR_EVIDENCE_HASH_CHANGED')
    report = strict_json((directory / 'repair-proof.json').read_text())
    before = strict_json((directory / 'before-candidate.json').read_text())
    candidate = strict_json((directory / 'candidate.json').read_text())
    validate_standalone_report(report, before, candidate)
    for path, pin in report['input_sha256'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != pin:
            raise FoundationError('NATIVE_REPAIR_FIXED_SOURCE_CHANGED')
    for path, pin in report['source_sha256'].items():
        path = Path(path)
        if path.is_absolute() or '..' in path.parts or hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != pin:
            raise FoundationError('NATIVE_REPAIR_IMPLEMENTATION_CHANGED')
    args = report['source_arguments']
    with patch.object(socket.socket, 'connect', side_effect=FoundationError('NETWORK_FORBIDDEN')), \
         patch('urllib.request.OpenerDirector.open', side_effect=FoundationError('NETWORK_FORBIDDEN')):
        reference = build_official_native_workflow(args['briefs'], args['api'], args['kit'])
        expected = verify_local_candidate(reference)
        first = verify_local_candidate(before)
        diagnosis = diagnose(reference, before, expected, first)
        final = verify_local_candidate(candidate)
        safety = failclosed_bank_checks(candidate)
    attempts = [{'attempt': 1, 'passed': diagnosis['passed'], 'trace': first}]
    if not diagnosis['passed']:
        repaired = repair_candidate(reference, before, diagnosis)
        if candidate != repaired:
            raise FoundationError('NATIVE_REPAIR_EXPORT_NOT_EXACT_BOUNDED_PATCH')
        attempts.append({'attempt': 2, 'passed': diagnose(reference, candidate, expected, final)['passed'], 'trace': final})
    elif candidate != before:
        raise FoundationError('NATIVE_REPAIR_UNNECESSARY_EXPORT_CHANGE')
    checked = (attempts[-1]['passed'] and safety['passed']
               and report['diagnosis'] == diagnosis and report['attempts'] == attempts
               and report['repair_cycles'] == len(attempts) - 1
               and report['fixed_oracle_trace_sha256'] == sha(expected)
               and strict_json((directory / 'fixed-oracle.json').read_text()) == expected
               and strict_json((directory / 'workflow.json').read_text()) == candidate['workflow']
               and strict_json((directory / 'attempts.json').read_text()) == attempts
               and strict_json((directory / 'bank-safety.json').read_text()) == safety)
    if not checked:
        raise FoundationError('NATIVE_REPAIR_REEXECUTED_EVIDENCE_DIFFERS')
    return {'valid': True, 'attempts_reexecuted': len(attempts), 'repair_cycles': len(attempts) - 1,
            'fixed_oracle_trace_sha256': sha(expected), 'bank_failclosed': safety['passed'],
            'network_calls': 0, 'platform_PASS': False,
            'integrity_scope': 'reexecution and local checksum consistency; independent anchor required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brief', action='append', required=True)
    parser.add_argument('--tool-api', required=True)
    parser.add_argument('--kit', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--inject-farewell-role-fault', action='store_true')
    parser.add_argument('--failed-candidate', type=Path)
    args = parser.parse_args()
    failed = strict_json(args.failed_candidate.read_text()) if args.failed_candidate else None
    result = run_native_repair(args.brief, args.tool_api, args.kit, args.output,
                              inject_fault=args.inject_farewell_role_fault, failed_candidate=failed)
    print(json.dumps({k: result[k] for k in ('passed', 'failure_origin', 'repair_cycles',
        'elapsed_seconds', 'whole_publication_body_bytes', 'network_calls', 'platform_PASS')}))


if __name__ == '__main__':
    main()
