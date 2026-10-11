"""Offline, bounded generalization benchmark with source-fixed expectations.

This measures supported typed briefs and structured customer inputs, not unseen
natural language, native Freya persistence, or official PUB-01 performance.
Existing behavioral-repair code owns generation, execution and bounded repair.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid
from unittest.mock import patch

from builder.api_contract import BankContract
from builder.models import FoundationError, strict_json
from builder.generic_emitter import MODULES, canonical

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/generalization/v1'
API = ROOT / 'data/freya-hackathon/kit-v3/build-the-builder/tools/openapi.json'
SOURCE_NAMES = tuple(sorted(set(MODULES) | {
    'compiler/typed_source.py', 'orchestration/behavioral_repair.py',
    'orchestration/generalization_benchmark.py'}))

CASES = (
    {'id': 'novel-card-short-window', 'type': 'credit_card', 'product_id': 'card-cobalt-183',
     'customer_id': 'generalization-card-183', 'identity_order': ['date_of_birth', 'phone_last4'],
     'amount': 183, 'floor': 75, 'past_due': 450, 'days_late': 3,
     'now': '2026-11-02T10:30', 'date': '2026-11-03', 'latest_date': '2026-11-04',
     'window': 2, 'deadline': '2026-11-04', 'weekdays': [0, 1, 2, 3, 4], 'holidays': [],
     'rejected_dates': ['2026-11-01', '2026-11-05'], 'renderer_fault': False},
    {'id': 'novel-loan-holiday', 'type': 'personal_loan', 'product_id': 'loan-amber-329',
     'customer_id': 'generalization-loan-329', 'identity_order': ['phone_last4', 'date_of_birth'],
     'amount': 329, 'floor': 225, 'past_due': 700, 'days_late': 17,
     'now': '2026-11-05T14:00', 'date': '2026-11-09', 'latest_date': '2026-11-12',
     'window': 4, 'deadline': '2026-11-12', 'weekdays': [0, 1, 2, 3, 4],
     'holidays': ['2026-11-06'], 'rejected_dates': ['2026-11-06', '2026-11-13'],
     'renderer_fault': False},
    {'id': 'novel-car-saturday-calendar', 'type': 'car_loan', 'product_id': 'car-jade-412',
     'customer_id': 'generalization-car-412', 'identity_order': ['date_of_birth', 'phone_last4'],
     'amount': 412, 'floor': 250, 'past_due': 900, 'days_late': 29,
     'now': '2026-11-12T09:15', 'date': '2026-11-14', 'latest_date': '2026-11-16',
     'window': 2, 'deadline': '2026-11-16', 'weekdays': [0, 1, 2, 3, 5], 'holidays': [],
     'rejected_dates': ['2026-11-13', '2026-11-17'], 'renderer_fault': False},
    {'id': 'novel-repair-loan-347', 'type': 'personal_loan', 'product_id': 'loan-violet-347',
     'customer_id': 'generalization-repair-347', 'identity_order': ['phone_last4', 'date_of_birth'],
     'amount': 347, 'floor': 230, 'past_due': 750, 'days_late': 21,
     'now': '2026-11-16T11:40', 'date': '2026-11-18', 'latest_date': '2026-11-19',
     'window': 3, 'deadline': '2026-11-19', 'weekdays': [0, 1, 2, 3, 4], 'holidays': [],
     'rejected_dates': ['2026-11-15', '2026-11-20'], 'renderer_fault': True},
)
UNSUPPORTED = ('unknown-policy-sentence', 'two-products', 'unstructured-customer-input')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(canonical(value) + '\n')


def fresh_output(path):
    raw = Path(path).absolute()
    resolved = raw.resolve()
    if (raw != resolved or not resolved.is_relative_to(OUTPUT.resolve())
            or resolved == OUTPUT.resolve() or resolved.exists()):
        raise FoundationError('Fresh real child of artifacts/generalization/v1 required')
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.mkdir()
    return resolved


def assert_cycle_integrity(directory):
    """Check a blocked cycle's captured bytes without promoting its verdict."""
    root = Path(directory)
    rows = read(root / 'manifest.json')
    if (not isinstance(rows, list) or len({row['path'] for row in rows}) != len(rows)
            or sha(root / 'manifest.json') != read(root / 'FINAL.json')['manifest_sha256']):
        raise FoundationError('Cycle evidence manifest mismatch')
    for row in rows:
        path = root / row['path']
        if (path.is_symlink() or path.resolve().parent != root.resolve()
                or path.stat().st_size != row['bytes'] or sha(path) != row['sha256']):
            raise FoundationError('Cycle evidence bytes mismatch')
    return sha(root / 'manifest.json')


def local_write_reservations(journal_rows):
    return [row for row in journal_rows if row.get('kind') == 'request_prepared'
            and row.get('data', {}).get('operation') not in {'verify_identity', 'get_account'}]


def create_inputs(directory, spec, unsupported=None, *, api_path=API):
    """Write new sources and pre-execution expectations; never edit old briefs."""
    if unsupported is not None and unsupported not in UNSUPPORTED:
        raise FoundationError('Unsupported benchmark mutation selector')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    api_path = Path(api_path).resolve()
    close_enum = BankContract.from_path(api_path).operations['close_call'].request['properties']['outcome']['enum']
    facts = {'business_weekdays': [0, 1, 2, 3, 4], 'holidays': [],
        'bands': [{'name': 'base-window', 'min_days_late': 0, 'max_days_late': 60,
                   'deadline_business_days': 5, 'persuasion_attempts': 0}],
        'floor_fields': {'credit_card': 'minimum_payment', 'personal_loan': 'instalment_amount',
                         'car_loan': 'instalment_amount'},
        'identity_max_wrong': 2, 'identity_order': ['date_of_birth', 'phone_last4'],
        'promise_max_attempts': 1, 'notes_enabled': False, 'product_limit': 1,
        'outcome_table': [{'outcome': value,
            'reasons': [] if value in {'promise', 'paid'} else ['fixture_reason'],
            'when': 'Explicit supported outcome'} for value in close_enum]}
    lines = ['Parameter ' + key + ' is ' + canonical(value) + '.' for key, value in facts.items()]
    lines.extend(['Before account read require identity_verified.',
        'Before promise write require consent_current.', 'Before promise write require date_is_business_day.',
        'Before promise write require amount_within_bounds.', 'Before close require goodbye_spoken.',
        'Before close require close_once_last.', 'Payment amount ceiling is 500.',
        'Payment date must be on or before 2026-12-31.'])
    wording = {'opening': 'This is the digital assistant. This call is recorded.',
        'identity_question': 'Please provide {field}.',
        'account_summary': 'Account {product_id} has {past_due} overdue.',
        'proposal_request': 'Please propose an amount and payment date.',
        'readback': 'Account {product_id}, amount {amount}, date {date}. Do you explicitly agree?',
        'promise_success': 'Your payment promise is recorded. A confirmation text message will be sent.',
        'goodbye': 'Thank you. Goodbye.'}
    lines.extend('Wording ' + key + ': ' + value for key, value in wording.items())
    if unsupported == 'unknown-policy-sentence':
        lines.append('Use an invented native durable-storage API.')
    base, memo = directory / 'policy.txt', directory / 'amendment.eml'
    base.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    overrides = {'identity_order': spec['identity_order'], 'business_weekdays': spec['weekdays'],
        'holidays': spec['holidays'], 'bands': [{'name': spec['id'], 'min_days_late': 0,
        'max_days_late': 60, 'deadline_business_days': spec['window'], 'persuasion_attempts': 0}]}
    memo_lines = ['From: local-benchmark@example.invalid', 'Subject: Novel typed benchmark amendment', '',
        'This amendment replaces the payment ceiling with ' + str(spec['amount']) + '.',
        'This amendment replaces the latest payment date with ' + spec['latest_date'] + '.']
    memo_lines.extend('This amendment overrides Parameter ' + key + ' with ' + canonical(value) + '.'
                      for key, value in overrides.items())
    memo.write_text('\n'.join(memo_lines) + '\n', encoding='utf-8')
    actions = [{'op': 'start'}]
    answers = {'date_of_birth': '1991-09-08', 'phone_last4': '5832'}
    for field in spec['identity_order']:
        actions += [{'op': 'input', 'value': {'kind': 'identity_answer', 'field': field, 'answer': answers[field]}},
                    {'op': 'bank', 'result': {'ok': True, 'verified': True}}]
    floor_field = 'minimum_payment' if spec['type'] == 'credit_card' else 'instalment_amount'
    product = {'product_id': spec['product_id'], 'type': spec['type'], 'past_due': spec['past_due'],
               'days_late': spec['days_late'], floor_field: spec['floor'], 'card_last4': '6734'}
    products = [product]
    if unsupported == 'two-products':
        other = copy.deepcopy(product)
        other['product_id'] += '-second'
        products.append(other)
    actions.append({'op': 'bank', 'result': {'ok': True, 'products': products}})
    if unsupported == 'unstructured-customer-input':
        actions.append({'op': 'input', 'value': {'kind': 'free_text', 'text': 'I can pay next payday.'}})
    for amount, date in [(spec['amount'] + 1, spec['date']), (spec['floor'] - 1, spec['date'])] + [
            (spec['amount'], date) for date in spec['rejected_dates']]:
        actions.append({'op': 'input', 'value': {'kind': 'proposal', 'amount': amount, 'date': date},
                        'expect_rejected': True})
    actions += [{'op': 'input', 'value': {'kind': 'proposal', 'amount': spec['amount'], 'date': spec['date']}},
        {'op': 'input', 'value': {'kind': 'confirm', 'token': '$current', 'explicit_yes': True}},
        {'op': 'advance'}, {'op': 'bank', 'result': {'ok': True, 'promise_id': spec['id'] + '-promise'}},
        {'op': 'restart'}, {'op': 'advance', 'expect_rejected': True},
        {'op': 'close'}, {'op': 'ack'}, {'op': 'bank', 'result': {'ok': True}},
        {'op': 'restart'}, {'op': 'advance', 'expect_rejected': True}]
    script = directory / 'script.json'
    write(script, {'variables': {'customer_id': spec['customer_id'], 'first_name': 'Fixture',
        'last_name': 'Customer', 'now': spec['now']}, 'actions': actions})
    expected = {'spec': copy.deepcopy(spec), 'unsupported': unsupported,
        'expected_rejection_indices': [i for i, value in enumerate(actions) if value.get('expect_rejected')],
        'expected_bank_operations': ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call'],
        'source_sha256': {str(path): sha(path) for path in (base, memo, script, api_path)}}
    expected_path = directory / 'source-expectations.json'
    write(expected_path, expected)
    return [base, memo], script, expected_path


def assert_trace(trace, expected):
    """Separate acceptance against frozen source declarations, not report PASS."""
    spec = expected['spec']
    requests = trace['bank_requests']
    if [row['operation'] for row in requests] != expected['expected_bank_operations']:
        raise FoundationError('Source expectation bank operation sequence mismatch')
    if [row['payload']['field'] for row in requests[:2]] != spec['identity_order']:
        raise FoundationError('Source expectation identity order mismatch')
    if any(row['payload']['customer_id'] != spec['customer_id'] for row in requests):
        raise FoundationError('Source expectation customer scope mismatch')
    wanted = {'customer_id': spec['customer_id'], 'product_id': spec['product_id'],
              'amount': spec['amount'], 'date': spec['date']}
    if requests[3]['payload'] != wanted:
        raise FoundationError('Source expectation promise mismatch')
    if requests[4]['payload'] != {'customer_id': spec['customer_id'], 'outcome': 'promise'}:
        raise FoundationError('Source expectation close mismatch')
    products = trace['final']['products']
    if (len(products) != 1 or products[0]['deadline'] != spec['deadline']
            or products[0]['floor'] != spec['floor'] or products[0]['product_id'] != spec['product_id']
            or trace['final']['phase'] != 'closed'):
        raise FoundationError('Source expectation product/calendar/final phase mismatch')
    if [row['action_index'] for row in trace['rejected']] != expected['expected_rejection_indices']:
        raise FoundationError('Source expectation negative-action rejection mismatch')
    if trace['network_calls'] or trace['real_platform_speech_proven']:
        raise FoundationError('Local benchmark boundary mismatch')
    return {'source_expected_identity_order': True, 'source_expected_single_promise': True,
        'source_expected_calendar_and_floor': True, 'negative_actions_rejected': len(trace['rejected']),
        'local_fixture_close_completed': True}


def run_benchmark(output_directory, *, deadline_seconds=1800, api_path=API):
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('Brief deadline must be an integer in 1..1800 seconds')
    started = time.monotonic()
    api_path = Path(api_path).resolve()
    # The supplied contract is mandatory and is pinned before any source
    # generation. Clean jury packages need no repository data directory.
    api_contract_pin = sha(api_path)
    output = fresh_output(output_directory)
    source_before = {name: sha(ROOT / name) for name in SOURCE_NAMES}
    from orchestration.behavioral_repair import run_behavioral_cycle, verify_behavioral_cycle
    token = uuid.uuid4().hex
    report = {'status': 'BLOCKED', 'scope': ['single_product', 'single_promise', 'structured_customer_inputs'],
        'platform_success': False, 'official_pub01_success': False, 'native_freya_export': False,
        'publish_allowed': False, 'synthetic_bounded_benchmark': True, 'model_calls': 0,
        'network_calls': 0, 'network_attempts': 0, 'deadline_seconds': deadline_seconds,
        'source_before_sha256': source_before, 'api_contract_path': str(api_path),
        'api_contract_sha256': api_contract_pin, 'cases': [], 'unsupported_cases': []}

    def deny(*args, **kwargs):
        report['network_attempts'] += 1
        raise FoundationError('No network permitted in local generalization benchmark')

    def check():
        if time.monotonic() - started >= deadline_seconds:
            raise FoundationError('Whole benchmark deadline exceeded')

    try:
        with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
            jobs = [(copy.deepcopy(spec), None) for spec in CASES] + [
                    (copy.deepcopy(CASES[0]), kind) for kind in UNSUPPORTED]
            for spec, unsupported in jobs:
                check()
                case_started = time.monotonic()
                identifier = unsupported or spec['id']
                policies, script, expectation_path = create_inputs(output / identifier, spec, unsupported,
                    api_path=api_path)
                expectation_pin = sha(expectation_path)
                evidence = ROOT / 'artifacts/behavioral-repair' / ('generalization-v1-' + token + '-' + identifier)
                outcome = run_behavioral_cycle(output_directory=evidence, policy_paths=policies,
                    api_path=api_path, script_path=script, deadline_seconds=deadline_seconds,
                    inject_renderer_role_fault=False,
                    inject_single_role_fault=bool(spec['renderer_fault']) and unsupported is None,
                    started_monotonic=case_started)
                check()
                expected = read(expectation_path)
                if sha(expectation_path) != expectation_pin or any(sha(path) != pin for path, pin in expected['source_sha256'].items()):
                    raise FoundationError('Frozen source or independent expectation changed')
                item = {'id': identifier, 'evidence_directory': evidence.relative_to(ROOT).as_posix(),
                    'source_expectation_sha256': expectation_pin,
                    'elapsed_whole_brief_seconds': round(time.monotonic() - case_started, 6),
                    'cycle_status': outcome['status'], 'attempts': len(outcome['attempts']),
                    'network_calls': outcome['network_calls'], 'model_calls': outcome['model_calls']}
                if unsupported:
                    if (outcome['status'] != 'BLOCKED' or outcome['attempts'] or 'verified_export' in outcome
                            or 'error' not in outcome):
                        raise FoundationError('Unsupported scope accepted or hidden as repair')
                    journal = evidence / 'canonical.jsonl'
                    journal_rows = [strict_json(line) for line in journal.read_text(encoding='utf-8').splitlines()] if journal.exists() else []
                    # No real banking occurs; additionally inspect reservations so
                    # a rejected brief cannot conceal a local write attempt.
                    writing = local_write_reservations(journal_rows)
                    if writing:
                        raise FoundationError('Unsupported scope reached a local write reservation')
                    item.update({'expected_rejection': True, 'error': outcome['error'],
                        'local_write_reservations': len(writing), 'actual_bank_api_calls': 0,
                        'verified_export': None, 'cycle_manifest_sha256': assert_cycle_integrity(evidence)})
                    report['unsupported_cases'].append(item)
                else:
                    if outcome['status'] != 'LOCAL_BEHAVIORAL_EXPORT_PASS':
                        raise FoundationError('Supported source case did not produce verified behavior')
                    verification = verify_behavioral_cycle(evidence)
                    observed = read(evidence / outcome['verified_trace'])
                    fixed = read(evidence / 'canonical-trace.json')
                    if observed != fixed:
                        raise FoundationError('Export trace differs from local canonical execution')
                    item.update(assert_trace(observed, expected))
                    attempt1 = outcome['attempts'][0]
                    repaired = len(outcome['attempts']) > 1
                    if repaired != spec['renderer_fault'] or not outcome['attempts'][-1]['passed']:
                        raise FoundationError('Measured repair outcome differs from explicit fault selection')
                    item.update({'first_attempt_success': attempt1['passed'], 'repair_success': repaired,
                        'repair_cycles': len(outcome['attempts']) - 1, 'repair_kind': outcome['repair_kind'],
                        'explicit_synthetic_fault': spec['renderer_fault'], 'exact_trace_equivalence': True,
                        'verified_export': (evidence / outcome['verified_export']).relative_to(ROOT).as_posix(),
                        'verified_export_sha256': sha(evidence / outcome['verified_export']),
                        'cycle_manifest_sha256': verification['manifest_sha256'],
                        'fixed_acceptance_sha256': outcome['fixed_acceptance_sha256']})
                    report['cases'].append(item)
                item['elapsed_whole_brief_seconds'] = round(time.monotonic() - case_started, 6)
                if item['network_calls'] or item['model_calls']:
                    raise FoundationError('Nonlocal case result')
                check()
        report['source_after_sha256'] = {name: sha(ROOT / name) for name in SOURCE_NAMES}
        if report['source_after_sha256'] != source_before or report['network_attempts'] or sha(api_path) != api_contract_pin:
            raise FoundationError('Source mutation or network attempt during benchmark')
        report['first_attempt_successes'] = sum(row['first_attempt_success'] for row in report['cases'])
        report['repair_successes'] = sum(row['repair_success'] for row in report['cases'])
        report['repair_cycles'] = sum(row['repair_cycles'] for row in report['cases'])
        report['supported_cases_passed'] = len(report['cases'])
        report['unsupported_cases_rejected'] = len(report['unsupported_cases'])
        report['first_attempt_denominator_supported_cases_only'] = len(report['cases'])
        report['first_attempt_success_fraction'] = str(report['first_attempt_successes']) + '/' + str(len(report['cases']))
        unfaulted = [row for row in report['cases'] if not row['explicit_synthetic_fault']]
        report['unfaulted_first_attempt_successes'] = sum(row['first_attempt_success'] for row in unfaulted)
        report['unfaulted_first_attempt_denominator'] = len(unfaulted)
        report['synthetic_fault_cases'] = len(report['cases']) - len(unfaulted)
        report['status'] = 'LOCAL_GENERALIZATION_BENCHMARK_PASS'
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    report['total_elapsed_seconds'] = round(time.monotonic() - started, 6)
    report['within_30_minute_limit'] = report['total_elapsed_seconds'] < deadline_seconds
    if not report['within_30_minute_limit']:
        report['status'] = 'BLOCKED'
    head = subprocess.run(['git', '-c', 'safe.directory=' + ROOT.as_posix(), 'rev-parse', 'HEAD'],
        cwd=ROOT, capture_output=True, text=True, timeout=3)
    report['commit_reference'] = head.stdout.strip() if head.returncode == 0 else None
    write(output / 'report.json', report)
    inventory = [{'path': path.relative_to(output).as_posix(), 'bytes': path.stat().st_size,
        'sha256': sha(path)} for path in sorted(output.rglob('*')) if path.is_file()]
    write(output / 'manifest.json', inventory)
    final_elapsed = round(time.monotonic() - started, 6)
    final = {'status': report['status'], 'manifest_sha256': sha(output / 'manifest.json'),
        'elapsed_seconds_including_seal': final_elapsed, 'deadline_seconds': deadline_seconds,
        'within_30_minutes': final_elapsed < deadline_seconds, 'official_pub01_success': False}
    if final_elapsed >= deadline_seconds:
        final['status'] = report['status'] = 'BLOCKED'
    write(output / 'FINAL.json', final)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--tool-api', type=Path, default=API,
                        help='Explicit bank API contract; use the shipped api-document.json in a clean jury package')
    args = parser.parse_args()
    report = run_benchmark(args.out, deadline_seconds=args.deadline_seconds, api_path=args.tool_api)
    print(canonical(report))
    return 0 if report['status'] == 'LOCAL_GENERALIZATION_BENCHMARK_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
