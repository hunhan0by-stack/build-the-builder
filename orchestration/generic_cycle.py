"""One command: typed sources -> workflow -> behavioral test -> bounded repair.

Only the explicitly supported policy grammar and structured dialogue are claimed.
All bank responses and speech notifications here are local fixtures, not Freya.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile
from unittest.mock import patch

from builder.compiled_policy import lower_compilation
from builder.generic_emitter import canonical, digest, emit_compiled_workflow, execute_script, write_workflow
from builder.ingest import collect_sources, read_source
from builder.api_contract import BankContract
from builder.models import FoundationError, strict_json
from compiler.core import compile_sources
from compiler.typed_source import TypedSourceProvider, source_wording

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder/tools/openapi.json'


def demo_inputs(directory, *, api_path=None):
    """Fresh fixture from the supplied API; the local kit is only a default."""
    directory.mkdir(parents=True, exist_ok=False)
    contract = BankContract.from_path(API if api_path is None else Path(api_path))
    facts = {'business_weekdays': [0, 1, 2, 3, 4], 'holidays': [],
             'bands': [{'name': 'supported-window', 'min_days_late': 0, 'max_days_late': 60,
                        'deadline_business_days': 5, 'persuasion_attempts': 0}],
             'floor_fields': {'credit_card': 'minimum_payment', 'personal_loan': 'instalment_amount', 'car_loan': 'instalment_amount'},
             'identity_max_wrong': 2, 'identity_order': ['date_of_birth', 'phone_last4'],
             'promise_max_attempts': 1, 'notes_enabled': False, 'product_limit': 1,
             'outcome_table': [{'outcome': outcome, 'reasons': [] if outcome in {'promise', 'paid'} else ['fixture_reason'],
                               'when': 'Explicit supported outcome'} for outcome in contract.operations['close_call'].request['properties']['outcome']['enum']]}
    lines = ['Parameter ' + name + ' is ' + canonical(value) + '.' for name, value in facts.items()]
    lines += ['Before account read require identity_verified.', 'Before promise write require consent_current.',
              'Before promise write require date_is_business_day.', 'Before promise write require amount_within_bounds.',
              'Before close require goodbye_spoken.', 'Before close require close_once_last.',
              'Payment amount ceiling is 500.', 'Payment date must be on or before 2026-10-20.']
    wording = {'opening': 'This is the digital assistant. This call is recorded.',
               'identity_question': 'Please provide {field}.',
               'account_summary': 'Account {product_id} has {past_due} overdue.',
               'proposal_request': 'Please propose an amount and payment date.',
               'readback': 'Account {product_id}, amount {amount}, date {date}. Do you explicitly agree?',
               'promise_success': 'Your payment promise is recorded. A confirmation text message will be sent.',
               'goodbye': 'Thank you. Goodbye.'}
    lines += ['Wording ' + key + ': ' + text for key, text in wording.items()]
    base, memo = directory / 'policy.txt', directory / 'amendment.eml'
    base.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    memo.write_text('From: approved-source@example.invalid\nSubject: New bounded amendment\nContent-Type: text/plain; charset=utf-8\n\n'
                    'This amendment replaces the payment ceiling with 275.\n'
                    'This amendment overrides Parameter identity_order with ["phone_last4", "date_of_birth"].\n', encoding='utf-8')
    actions = [{'op': 'start'},
        {'op': 'input', 'value': {'kind': 'identity_answer', 'field': 'phone_last4', 'answer': '4471'}},
        {'op': 'bank', 'result': {'ok': True, 'verified': True}},
        {'op': 'input', 'value': {'kind': 'identity_answer', 'field': 'date_of_birth', 'answer': '1988-03-14'}},
        {'op': 'bank', 'result': {'ok': True, 'verified': True}},
        {'op': 'bank', 'result': {'ok': True, 'products': [{'product_id': 'new-product', 'type': 'credit_card',
                    'past_due': 500, 'days_late': 8, 'minimum_payment': 100, 'card_last4': '1234'}]}},
        {'op': 'input', 'value': {'kind': 'proposal', 'amount': 400, 'date': '2026-10-19'}, 'expect_rejected': True},
        {'op': 'input', 'value': {'kind': 'proposal', 'amount': 275, 'date': '2026-10-19'}},
        {'op': 'input', 'value': {'kind': 'confirm', 'token': '$current', 'explicit_yes': True}},
        {'op': 'advance'}, {'op': 'bank', 'result': {'ok': True, 'promise_id': 'new-promise'}},
        {'op': 'restart'}, {'op': 'advance', 'expect_rejected': True},
        {'op': 'close'}, {'op': 'ack'}, {'op': 'bank', 'result': {'ok': True}},
        {'op': 'restart'}, {'op': 'advance', 'expect_rejected': True}]
    script = directory / 'script.json'
    script.write_text(canonical({'variables': {'customer_id': 'new-generic-customer', 'first_name': 'New',
                           'last_name': 'Customer', 'now': '2026-10-14T11:00'}, 'actions': actions}) + '\n', encoding='utf-8')
    return [base, memo], script


def run_generic_cycle(*, output_directory, policy_paths, api_path, script_path,
                      deadline_seconds=1800, inject_serialization_fault=False, synthetic_fixture=False,
                      started_monotonic=None):
    now = time.monotonic()
    start = now if started_monotonic is None else started_monotonic
    if type(start) not in (int, float) or not math.isfinite(start) or start > now:
        raise FoundationError('Invalid shared production start time')
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('Brief limit must be 1..1800 seconds')
    deadline = start + deadline_seconds
    package = Path(output_directory).resolve()
    if not package.is_relative_to(ROOT / 'artifacts/generic-cycle') or package.exists():
        raise FoundationError('Choose a fresh child of artifacts/generic-cycle')
    package.mkdir(parents=True)
    report = {'status': 'BLOCKED', 'platform_success': False, 'native_freya_export': False,
              'receipt_authority': 'local_fixture', 'model_calls': 0, 'network_calls': 0,
              'manual_code_changes_during_cycle': False, 'deadline_seconds': deadline_seconds,
              'synthetic_fixture': synthetic_fixture, 'attempts': [], 'passed_tests': [], 'failed_tests': []}

    def check_deadline():
        if time.monotonic() >= deadline:
            raise FoundationError('Shared brief deadline exceeded')

    def save(name, value):
        check_deadline()
        (package / name).write_text(canonical(value) + '\n', encoding='utf-8')

    try:
        raw_inputs = [read_source(path) for path in [*policy_paths, api_path, script_path]]
        pins = {item.path: item.sha256 for item in raw_inputs}
        documents = collect_sources(policy_paths)
        api_source = read_source(api_path)
        api_document = strict_json(api_source.text)
        contract = BankContract(api_document, api_source.source_id, api_source.sha256)
        envelope = compile_sources(policy_paths=policy_paths, tool_api_path=api_path, provider=TypedSourceProvider())
        compiled = lower_compilation(envelope, documents, contract)
        wording = source_wording(documents)
        script = strict_json(Path(script_path).read_text(encoding='utf-8'))
        save('compilation.json', envelope)
        save('lowered-policy.json', compiled.serializable())
        save('source-inventory.json', [asdict(item) for item in raw_inputs])
        save('script.json', script)
        workflow = emit_compiled_workflow(compiled, documents, contract, wording, pins,
                                          {'compilation': envelope, 'api_document': api_document})
        save('workflow.json', {key: value for key, value in workflow.items() if key != 'code'})
        code_pins = dict(workflow['code_sha256'])
        network_attempts = []

        def deny(*args, **kwargs):
            network_attempts.append('blocked')
            raise FoundationError('No live connections in local workflow measurement')

        with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
            local = execute_script(compiled, documents, wording, script['variables'], script['actions'], package / 'local.jsonl')
        if network_attempts:
            raise FoundationError('Local workflow attempted network access')
        save('local-trace.json', local)
        report['passed_tests'].append('source_compilation_and_local_execution')
        for attempt in range(1, 3):
            check_deadline()
            export = package / ('workflow-' + str(attempt) + '.pyz')
            write_workflow(workflow, export)
            injected = inject_serialization_fault and attempt == 1
            if injected:
                export.write_bytes(export.read_bytes()[:23])
            out = package / ('export-trace-' + str(attempt) + '.json')
            process = subprocess.run([sys.executable, '-I', str(export), '--script', str(package / 'script.json'),
                         '--journal', str(package / ('export-' + str(attempt) + '.jsonl')), '--out', str(out)],
                         cwd=package, capture_output=True, text=True, timeout=max(0.01, deadline - time.monotonic()))
            check_deadline()
            record = {'attempt': attempt, 'returncode': process.returncode, 'injected_serialization_fault': injected,
                      'runtime_error': process.stderr[-4000:] if process.returncode else None}
            report['attempts'].append(record)
            if process.returncode:
                report['failed_tests'].append('export_execution_attempt_' + str(attempt))
                if not injected:
                    raise FoundationError('Export behavior failed; no semantic or manual repair permitted')
                # Deterministic mechanical regeneration from fixed compilation/runtime;
                # neither source rules nor expected results are altered.
                continue
            exported = strict_json(out.read_text(encoding='utf-8'))
            if local != exported:
                save('comparison.json', {'equal': False, 'local_sha256': digest(local), 'export_sha256': digest(exported)})
                raise FoundationError('Local and exported behavior differs')
            operations = [item['operation'] for item in local['bank_requests']]
            if operations != ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']:
                raise FoundationError('Measured operation ordering/count unexpected')
            if local['final']['phase'] != 'closed':
                raise FoundationError('Workflow failed to close after completion receipt')
            goodbye = next(item for item in local['spoken_events'] if item['kind'] == 'goodbye')
            save('comparison.json', {'equal': True, 'spoken_events_equal': True, 'bank_requests_equal': True,
                'all_checkpoints_equal': True, 'local_sha256': digest(local), 'export_sha256': digest(exported),
                'operation_sequence': operations, 'promise_requests': operations.count('create_promise'),
                'close_requests': operations.count('close_call'), 'goodbye_event_id': goodbye['event_id'],
                'restart_count': sum(item['op'] == 'restart' for item in script['actions']),
                'platform_speech_receipt': False, 'receipt_authority': 'local_fixture'})
            report['passed_tests'] += ['isolated_export_execution', 'exact_event_and_bank_trace_equivalence',
                                      'single_promise_across_restart', 'close_after_matching_completion']
            report['verified_export'] = export.name
            break
        else:
            raise FoundationError('Bounded mechanical repair exhausted')
        if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value for path, value in pins.items()):
            raise FoundationError('Sources changed during cycle')
        if any(hashlib.sha256((ROOT / path).read_text(encoding='utf-8').encode()).hexdigest() != value for path, value in code_pins.items()):
            raise FoundationError('Runtime changed during cycle')
        report['source_and_code_unchanged'] = True
        report['repair_kind'] = 'fixed-input serialization regeneration' if inject_serialization_fault else 'not_needed'
        report['status'] = 'LOCAL_GENERIC_WORKFLOW_PASS'
    except Exception as exc:
        report['status'] = 'BLOCKED'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    report['total_production_seconds'] = round(time.monotonic() - start, 6)
    report['within_30_minute_brief_limit'] = time.monotonic() < deadline
    if not report['within_30_minute_brief_limit']:
        report['status'] = 'BLOCKED'
    report['commit_reference'] = None
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, timeout=2)
        if commit.returncode == 0:
            report['commit_reference'] = commit.stdout.strip()
    except Exception:
        pass
    (package / 'report.json').write_text(canonical(report) + '\n', encoding='utf-8')
    manifest = [{'path': path.name, 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted(package.iterdir()) if path.is_file()]
    (package / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    final = {'status': report['status'], 'elapsed_seconds': round(time.monotonic() - start, 6),
             'deadline_seconds': deadline_seconds,
             'manifest_sha256': hashlib.sha256((package / 'manifest.json').read_bytes()).hexdigest()}
    if time.monotonic() >= deadline:
        (package / 'FAILED_FINALIZATION.json').write_text('{"reason":"brief deadline exceeded"}\n', encoding='utf-8')
        report['status'] = 'BLOCKED'
        final['status'] = 'BLOCKED'
    (package / 'FINAL.json').write_text(canonical(final) + '\n', encoding='utf-8')
    if time.monotonic() >= deadline:
        (package / 'FAILED_FINALIZATION.json').write_text('{"reason":"brief deadline exceeded"}\n', encoding='utf-8')
        report['status'] = 'BLOCKED'
    return report


def verify_generic_cycle(directory):
    """Read-only gate; a provisional report alone is never accepted."""
    root = Path(directory)
    if (root / 'FAILED_FINALIZATION.json').exists():
        raise FoundationError('Finalization deadline failed')
    final = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
    elapsed, budget = final.get('elapsed_seconds'), final.get('deadline_seconds')
    if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0 or type(budget) is not int or not 1 <= budget <= 1800
            or final.get('status') != 'LOCAL_GENERIC_WORKFLOW_PASS' or elapsed >= budget):
        raise FoundationError('Successful final receipt required')
    manifest_path = root / 'manifest.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != final['manifest_sha256']:
        raise FoundationError('Final manifest mismatch')
    manifest = strict_json(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, list) or len({entry['path'] for entry in manifest}) != len(manifest):
        raise FoundationError('Manifest entries must be unique')
    names = {entry['path'] for entry in manifest}
    actual = {path.name for path in root.iterdir() if path.is_file()}
    if actual != names | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('Untracked or missing evidence file')
    required = {'report.json', 'comparison.json', 'local-trace.json', 'compilation.json',
                'lowered-policy.json', 'source-inventory.json', 'workflow.json', 'script.json', 'local.jsonl'}
    if not required <= names:
        raise FoundationError('Mandatory evidence missing from manifest')
    for entry in manifest:
        path = root / entry['path']
        if path.resolve().parent != root.resolve() or path.stat().st_size != entry['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise FoundationError('Evidence mismatch')
    report = strict_json((root / 'report.json').read_text(encoding='utf-8'))
    if report['status'] != 'LOCAL_GENERIC_WORKFLOW_PASS':
        raise FoundationError('Cycle did not pass')
    export_name = report['verified_export']
    if export_name not in names or not export_name.startswith('workflow-') or not export_name.endswith('.pyz'):
        raise FoundationError('Verified executable export missing')
    attempt = export_name.removeprefix('workflow-').removesuffix('.pyz')
    trace_name = 'export-trace-' + attempt + '.json'
    if trace_name not in names:
        raise FoundationError('Export execution trace missing')
    workflow = strict_json((root / 'workflow.json').read_text(encoding='utf-8'))
    with zipfile.ZipFile(root / export_name) as archive:
        bundle = strict_json(archive.read('workflow.json').decode())
        if bundle != workflow['bundle'] or digest(bundle) != workflow['bundle_sha256']:
            raise FoundationError('Executable policy bundle differs from evidence')
        for name, expected in workflow['code_sha256'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise FoundationError('Executable runtime differs from evidence')
    if strict_json((root / 'compilation.json').read_text(encoding='utf-8')) != bundle['compilation']:
        raise FoundationError('Compilation differs from executable')
    from builder.generic_emitter import load_compiled
    rebuilt, docs = load_compiled(bundle)
    if canonical(rebuilt.serializable()) != canonical(strict_json((root / 'lowered-policy.json').read_text(encoding='utf-8'))):
        raise FoundationError('Lowered policy differs from source-linked compilation')
    inventory = strict_json((root / 'source-inventory.json').read_text(encoding='utf-8'))
    indexed = {item['path']: item for item in inventory}
    if {key: item['sha256'] for key, item in indexed.items()} != bundle['source_snapshots']:
        raise FoundationError('Source inventory does not bind the complete snapshots')
    if any(indexed.get(doc.path) != asdict(doc) for doc in docs):
        raise FoundationError('Executable policy text differs from source inventory')
    api_rows = [item for item in inventory if item['sha256'] == bundle['contract_sha256']]
    if not api_rows or any(strict_json(item['text']) != bundle['api_document'] for item in api_rows):
        raise FoundationError('Executable API differs from source inventory')
    local = strict_json((root / 'local-trace.json').read_text(encoding='utf-8'))
    exported = strict_json((root / trace_name).read_text(encoding='utf-8'))
    if local != exported:
        raise FoundationError('Evidence traces differ despite report')
    operations = [item['operation'] for item in local['bank_requests']]
    if (operations != ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']
            or local['final']['phase'] != 'closed' or local['final']['pending_bank_requests']):
        raise FoundationError('Measured terminal behavior failed')
    fields = [request['payload']['field'] for request in local['bank_requests'] if request['operation'] == 'verify_identity']
    if fields != rebuilt.policy.facts['identity_order'].value:
        raise FoundationError('Identity trace differs from compiled source order')
    promise = next(request['payload'] for request in local['bank_requests'] if request['operation'] == 'create_promise')
    for constraint in rebuilt.constraints:
        if constraint['target'] == 'payment.amount' and promise['amount'] > constraint['value']:
            raise FoundationError('Promise trace exceeds compiled source amount')
        if constraint['target'] == 'payment.date_on_or_before' and promise['date'] > constraint['value']:
            raise FoundationError('Promise trace exceeds compiled source date')
    script = strict_json((root / 'script.json').read_text(encoding='utf-8'))
    if len(local['checkpoints']) != len(script['actions']):
        raise FoundationError('Incomplete action trace')
    for request in local['bank_requests']:
        if local['final']['resolved'].get(request['request_id'], {}).get('status') != 'confirmed':
            raise FoundationError('Unconfirmed bank request cannot pass')
    goodbye = next((item for item in local['spoken_events'] if item['kind'] == 'goodbye'), None)
    if goodbye is None:
        raise FoundationError('Goodbye proof missing')
    preclose = [point for point in local['checkpoints'] if any(req['operation'] == 'close_call' for req in point['pending_bank_requests'])]
    text_hash = hashlib.sha256(goodbye['text'].encode()).hexdigest()
    def valid_receipt(point):
        receipt = point.get('speech_receipts', {}).get(goodbye['event_id'], {})
        return (receipt.get('adapter_verified') is True and receipt.get('authority') == 'local_fixture'
                and receipt.get('text_sha256') == text_hash
                and point.get('speech_bindings', {}).get(goodbye['event_id']) == goodbye['text'])
    if not preclose or not all(valid_receipt(point) for point in preclose):
        raise FoundationError('Close lacks completed speech receipt evidence')
    return {'valid': True, 'status': report['status'], 'manifest_sha256': final['manifest_sha256']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--brief', action='append', default=[], type=Path)
    parser.add_argument('--api', default=API, type=Path)
    parser.add_argument('--script', type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--inject-serialization-fault', action='store_true')
    parser.add_argument('--deadline-seconds', default=1800, type=int)
    args = parser.parse_args(argv)
    started = time.monotonic()
    if args.demo:
        policy_paths, script_path = demo_inputs(args.out.with_name(args.out.name + '-inputs'), api_path=args.api)
    else:
        policy_paths, script_path = args.brief, args.script
        if not policy_paths or script_path is None:
            parser.error('Explicit brief and script, or --demo required')
    result = run_generic_cycle(output_directory=args.out, policy_paths=policy_paths, api_path=args.api,
                               script_path=script_path, deadline_seconds=args.deadline_seconds,
                               inject_serialization_fault=args.inject_serialization_fault, synthetic_fixture=args.demo,
                               started_monotonic=started)
    print(canonical({**result, 'invocation_total_seconds': round(time.monotonic() - started, 6)}))
    return 0 if result['status'] == 'LOCAL_GENERIC_WORKFLOW_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
