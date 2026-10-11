"""Produce and measure a partial native graph offline; never publish it.

This reuses a previously accepted typed compilation. It does not rerun the
closed generic milestone, contact Freya, or claim native speech/restart proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch

from builder.generic_emitter import execute_script, load_compiled
from builder.freya_export import code_exec_request_bytes
from builder.models import FoundationError, strict_json
from builder.native_freya import build_native_workflow, NativeHarness

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/native-adapter'
DEFAULT_SOURCE = ROOT / 'artifacts/generic-cycle/milestone-normal-v1'
SOURCE_MANIFEST_SHA256 = '45a8e9138de0f20bba9d60c68e3e425b1ef5a1a6fa8b9c76630672e51f3d7634'


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8') + b'\n'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def produce_native_candidate(*, source_directory, output_directory):
    started = time.monotonic()
    deadline = started + 1800

    def check_deadline():
        if time.monotonic() >= deadline:
            raise FoundationError('Shared 1800-second generation deadline exceeded')

    source, target = Path(source_directory).resolve(), Path(output_directory).resolve()
    if source != DEFAULT_SOURCE.resolve():
        raise FoundationError('Only the pinned, accepted local fixture is admitted by this measurement command')
    if target == OUTPUT.resolve() or not target.is_relative_to(OUTPUT.resolve()) or target.exists():
        raise FoundationError('A fresh native-adapter child directory is required')
    final = strict_json((source / 'FINAL.json').read_text(encoding='utf-8'))
    manifest_raw = (source / 'manifest.json').read_bytes()
    if (final.get('manifest_sha256') != digest(manifest_raw) or digest(manifest_raw) != SOURCE_MANIFEST_SHA256
            or final.get('status') != 'LOCAL_GENERIC_WORKFLOW_PASS'
            or final.get('deadline_seconds') != 1800 or type(final.get('elapsed_seconds')) not in (int, float)
            or not 0 <= final['elapsed_seconds'] < 1800 or (source / 'FAILED_FINALIZATION.json').exists()):
        raise FoundationError('Accepted source manifest changed')
    manifest = strict_json(manifest_raw.decode('utf-8'))
    entries = {row['path']: row for row in manifest}
    supplied = {}
    for name in ('workflow.json', 'script.json'):
        raw = (source / name).read_bytes()
        if digest(raw) != entries[name]['sha256'] or len(raw) != entries[name]['bytes']:
            raise FoundationError('Accepted typed input changed')
        supplied[name] = strict_json(raw.decode('utf-8'))
    bundle, script = supplied['workflow.json']['bundle'], supplied['script.json']
    compiled, documents = load_compiled(bundle)
    variables = dict(script['variables'], call_id='offline-native-fixture', session_id='offline-native-session')
    candidate = build_native_workflow(compiled, documents, bundle['wording'], initial_variables=variables)
    check_deadline()
    # This is the SAME valid input prefix on both engines; the accepted script's
    # negative proposal and its write/restart suffix are outside this partial
    # adapter comparison and are explicitly excluded, never counted as passes.
    actions = []
    for action in script['actions']:
        if action['op'] == 'advance':
            break
        if not action.get('expect_rejected'):
            actions.append(action)
    network_attempts = []

    def deny(*args, **kwargs):
        network_attempts.append('blocked')
        raise FoundationError('No network permitted in native fixture measurement')

    target.mkdir(parents=True)
    with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
        local = execute_script(compiled, documents, bundle['wording'], variables, actions,
                               target / 'prefix-host-journal.jsonl', scope='native-prefix-fixture')
        harness = NativeHarness(candidate, variables)
        measured_code_inputs = []
        original_run_code = harness.run_code

        def measured_run_code(node):
            code = candidate['workflow']['nodes'][node]['pre_actions'][0]['function']['parameters']['properties']['code']['default']
            size = len(code_exec_request_bytes(code, harness.variables))
            measured_code_inputs.append({'node': node, 'serialized_code_vars_bytes': size})
            if size >= 131072:
                raise FoundationError('Measured code_exec input reaches platform cap')
            return original_run_code(node)

        harness.run_code = measured_run_code
        for action in actions:
            check_deadline()
            if action['op'] == 'start':
                harness.start()
            elif action['op'] == 'input':
                value = dict(action['value'])
                if value['kind'] == 'proposal':
                    value.setdefault('product_id', harness.state['product']['product_id'])
                if value['kind'] == 'confirm':
                    value = {'kind': 'confirm', 'explicit_yes': value['explicit_yes'],
                             'readback_token': harness.state['readback_token']}
                harness.receive(value)
            elif action['op'] == 'bank':
                operation = harness.state['pending']['operation']
                harness.dispatch(operation)
                harness.resume(operation, {'status': 'success', 'status_code': 200,
                                          'data': action['result'], 'message': '', 'header': {}})
            else:
                raise FoundationError('Unsupported input in pinned prefix')
    native_trace = {'events': harness.state['events'], 'requests': harness.requests,
                    'traversal': harness.traversal, 'tts_events': harness.tts_events,
                    'measured_code_inputs': measured_code_inputs,
                    'final_state': harness.state}
    local_speech = [{'kind': row['kind'], 'text': row['text']} for row in local['spoken_events']]
    native_speech = [{'kind': row['kind'], 'text': row['text']} for row in native_trace['events']]
    local_requests = [{'operation': row['operation'], 'payload': row['payload']} for row in local['bank_requests']]
    native_requests = [{'operation': row['operation'], 'payload': row['body']} for row in harness.requests]
    comparison = {'same_valid_input_prefix': True, 'speech_kind_and_text_equal': local_speech == native_speech,
                  'bank_operation_and_payload_equal': local_requests == native_requests,
                  'full_workflow_equivalence_proven': False,
                  'excluded': ['rejected-proposal recovery', 'bank writes', 'goodbye completion', 'restart suffix'],
                  'native_stop_reason': harness.state.get('diagnostic'),
                  'expected_native_gate_closed': (harness.state['phase'] == 'blocked' and
                      harness.state.get('diagnostic') == 'Native write/completion admission unavailable')}
    passed = (comparison['speech_kind_and_text_equal'] and comparison['bank_operation_and_payload_equal']
              and comparison['expected_native_gate_closed'] and not network_attempts)
    report = {'status': 'PARTIAL_NATIVE_PREFIX_VERIFIED' if passed else 'FAILED',
              'synthetic_local_fixture': True, 'official_brief_A_ready': False,
              'native_json_generated': True, 'native_platform_execution_proven': False,
              'publish_allowed': False, 'real_platform_success': False,
              'accepted_416_rerun': False, 'network_attempts': len(network_attempts),
              'new_api_calls': 0, 'new_credit_consumption': 0,
              'bank_writes_enabled': False, 'elapsed_seconds': time.monotonic() - started,
              'deadline_seconds': 1800, 'readiness': candidate['readiness'], 'budget': candidate['budget'],
              'input_manifest_sha256': digest(manifest_raw),
              'measured_scope': 'Fresh structured prefix, real emitted code/hooks/mappings in a local harness'}
    report['actual_fixture_peak_code_vars_bytes'] = max(row['serialized_code_vars_bytes'] for row in measured_code_inputs)
    report['all_future_platform_inputs_bounded_by_fixture'] = False
    report['workflow_json_bytes'] = len(encoded(candidate['workflow']))
    report['workflow_json_below_131072'] = report['workflow_json_bytes'] < 131072
    files = {'candidate.workflow.json': candidate['workflow'], 'readiness.json': report,
             'native-trace.json': native_trace, 'local-prefix-trace.json': local,
             'comparison.json': comparison, 'input-prefix.json': {'variables': variables, 'actions': actions}}
    for name, value in files.items():
        check_deadline()
        (target / name).write_bytes(encoded(value))
    inventory = [{'path': p.name, 'bytes': p.stat().st_size, 'sha256': digest(p.read_bytes())}
                 for p in sorted(target.iterdir()) if p.is_file()]
    (target / 'manifest.json').write_bytes(encoded({'artifacts': inventory, 'publish_allowed': False}))
    check_deadline()
    (target / 'FINAL.json').write_bytes(encoded({'status': report['status'], 'publish_allowed': False,
                                               'elapsed_seconds_before_final_write': time.monotonic() - started,
                                               'manifest_sha256': digest((target / 'manifest.json').read_bytes())}))
    try:
        check_deadline()
    except FoundationError:
        (target / 'FAILED_FINALIZATION.json').write_bytes(encoded({'status': 'FAILED_DEADLINE', 'publish_allowed': False}))
        raise
    report['elapsed_seconds_through_sealing'] = time.monotonic() - started
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    report = produce_native_candidate(source_directory=args.source, output_directory=args.out)
    print(encoded(report).decode().strip())
    return 0 if report['status'] == 'PARTIAL_NATIVE_PREFIX_VERIFIED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
