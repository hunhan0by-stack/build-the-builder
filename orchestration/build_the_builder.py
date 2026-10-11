"""One offline entry: supported brief -> compile -> native checks -> verify -> export.

Optional behavioral repair runs a separately verified portable workflow. Its
host evidence never opens native Freya banking, speech, persistence or live gates.
Fresh products use the existing product-build output boundary; the immutable
command record is stored separately under artifacts/unified-builder/v1/.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
from pathlib import Path
import socket
import subprocess
import time
import uuid
from unittest.mock import patch

from builder.models import FoundationError, strict_json
from orchestration.prepare_delivery import scan_public
from orchestration.product_build import ROOT, OUTPUT as PRODUCT_OUTPUT, build_product, verify_product_build

OUTPUT = ROOT / 'artifacts/unified-builder/v1'
MODULE = 'orchestration/build_the_builder.py'
REPAIR_MODULE = 'orchestration/behavioral_repair.py'
SUCCESS = 'LOCAL_BUILDER_EXPORT_VERIFIED'


def raw_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode() + b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def _pins(repair):
    names = [MODULE] + ([REPAIR_MODULE] if repair else [])
    return {name: sha((ROOT / name).read_bytes()) for name in names}


def _head():
    # A recorded HEAD identifies the checkout, not an assertion that new files
    # are committed. Final delivery independently binds its entire code closure.
    try:
        result = subprocess.run(['git', '-c', 'safe.directory=' + ROOT.as_posix(),
                                 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                timeout=10, check=True)
        value = result.stdout.decode().strip()
        return value if len(value) == 40 and all(c in '0123456789abcdef' for c in value) else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _validate_jobs(jobs, repair):
    if not isinstance(jobs, list) or len(jobs) not in (1, 2):
        raise FoundationError('BUILDER_ONE_OR_TWO_JOBS_REQUIRED')
    identifiers = []
    for job in jobs:
        expected = {'id', 'briefs', 'tool_api'} | ({'behavior_script'} if repair else set())
        if not isinstance(job, dict) or set(job) != expected:
            raise FoundationError('BUILDER_JOB_SHAPE_INVALID')
        identifier = job['id']
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 40 or any(
                not c.isascii() or not (c.isalnum() or c in '-_') for c in identifier):
            raise FoundationError('BUILDER_JOB_ID_INVALID')
        if not isinstance(job['briefs'], (list, tuple)) or not 1 <= len(job['briefs']) <= 8:
            raise FoundationError('BUILDER_BOUNDED_SOURCES_REQUIRED')
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        raise FoundationError('BUILDER_JOB_IDS_MUST_BE_UNIQUE')


def build_builder(*, jobs, output_directory, deadline_seconds=1800, behavioral_repair=False,
                  native_mode='prefix', native_runtime_mode='host_exclusive'):
    """Compile one/two briefs; each budget includes optional repair and verification."""
    started = time.monotonic()
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError('BUILDER_DEADLINE_MUST_BE_1_TO_1800')
    if type(behavioral_repair) is not bool:
        raise FoundationError('BUILDER_REPAIR_FLAG_INVALID')
    if native_mode not in ('prefix', 'official'):
        raise FoundationError('BUILDER_NATIVE_MODE_INVALID')
    from builder.native_write_guard import validate_runtime_mode
    validate_runtime_mode(native_runtime_mode)
    if native_runtime_mode == 'official_bank_session' and native_mode != 'official':
        raise FoundationError('BUILDER_BANK_SESSION_REQUIRES_OFFICIAL_EXPORT')
    _validate_jobs(jobs, behavioral_repair)
    explicit = Path(output_directory)
    target = explicit.resolve()
    if explicit.is_symlink() or target == OUTPUT.resolve() or not target.is_relative_to(
            OUTPUT.resolve()) or target.exists():
        raise FoundationError('BUILDER_FRESH_RECORD_DIRECTORY_REQUIRED')
    pins = _pins(behavioral_repair)
    target.mkdir(parents=True)
    network_attempts = []
    def deny(*args, **kwargs):
        network_attempts.append('blocked')
        raise FoundationError('BUILDER_NETWORK_FORBIDDEN')
    report = {'version': 'unified-builder-1', 'status': 'BLOCKED', 'products': [],
              'publish_allowed': False, 'publication_ready': False,
              'final_submission_ready': False, 'platform_success': False,
              'live_enabled': False, 'network_calls': 0, 'model_calls': 0,
              'observed_checkout_commit': _head(), 'commit_attestation': False,
              'entry_runtime_sha256': pins, 'deadline_seconds_per_brief': deadline_seconds,
              'whole_command_measured': True, 'native_mode': native_mode, 'native_runtime_mode': native_runtime_mode,
              'unsupported': ['Arbitrary native atomic restart/provider idempotency',
                              'Unseen arbitrary natural-language interpretation',
                              'Full brief branches and multi-product native workflow'],
              'scope': ('source-bound complete single-product promise workflow; local fixture verification, no live authority'
                        if native_mode == 'official' else 'supported typed or reviewed sources; explicit native read-only prefix')}
    final_deadline = started + deadline_seconds
    with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
        for index, job in enumerate(jobs):
            job_start = started if index == 0 else time.monotonic()
            final_deadline = job_start + deadline_seconds
            item = {'id': job['id'], 'status': 'BLOCKED', 'publish_allowed': False,
                    'diagnostic': None, 'behavioral_repair': None}
            product_path = PRODUCT_OUTPUT / ('unified-' + uuid.uuid4().hex)
            item['product_directory'] = product_path.relative_to(ROOT).as_posix()
            try:
                remaining = math.floor(final_deadline - time.monotonic())
                if remaining < 1:
                    raise FoundationError('BUILDER_SHARED_DEADLINE_EXCEEDED')
                result = build_product(brief_paths=job['briefs'], tool_api_path=job['tool_api'],
                    output_directory=product_path, deadline_seconds=remaining, native_mode=native_mode, native_runtime_mode=native_runtime_mode)
                if result['status'] != 'LOCAL_SOURCE_NATIVE_PRODUCT_VERIFIED':
                    raise FoundationError(result.get('diagnostic', 'BUILDER_SOURCE_OR_NATIVE_EXPORT_BLOCKED'))
                if behavioral_repair:
                    repair = importlib.import_module('orchestration.behavioral_repair')
                    repair_path = ROOT / 'artifacts/behavioral-repair' / ('unified-' + uuid.uuid4().hex)
                    repaired = repair.run_behavioral_cycle(output_directory=repair_path,
                        policy_paths=job['briefs'], api_path=job['tool_api'],
                        script_path=job['behavior_script'], deadline_seconds=deadline_seconds,
                        inject_renderer_role_fault=False, started_monotonic=job_start)
                    repair_verified = repair.verify_behavioral_cycle(repair_path)
                    item['behavioral_repair'] = {'directory': repair_path.relative_to(ROOT).as_posix(),
                                                'report': repaired,
                                                'verification': repair_verified,
                                                'applies_to_native_export': False}
                    if (repaired['status'] != 'LOCAL_BEHAVIORAL_EXPORT_PASS'
                            or not repair_verified.get('valid') or time.monotonic() >= final_deadline):
                        raise FoundationError('BUILDER_BEHAVIORAL_ACCEPTANCE_BLOCKED')
                verified = verify_product_build(product_path, deadline_monotonic=final_deadline)
                if not verified.get('valid') or verified.get('publish_allowed') is not False:
                    raise FoundationError('BUILDER_INDEPENDENT_EXPORT_VERIFICATION_FAILED')
                trace_a = read(product_path / 'critical-evidence.json')
                trace_b = read(product_path / 'serialized-export-evidence.json')
                if trace_a != trace_b or not trace_a['passed']:
                    raise FoundationError('BUILDER_LOCAL_EXPORT_TRACE_MISMATCH')
                item.update(status=SUCCESS, native_export=verified,
                    export_path=(product_path / 'agent.workflow.json').relative_to(ROOT).as_posix(),
                    manifest_sha256=sha((product_path / 'manifest.json').read_bytes()),
                    actual_export_bytes=result['actual_export_bytes'],
                    compared_event_traces=[(product_path / name).relative_to(ROOT).as_posix()
                        for name in ('critical-evidence.json', 'serialized-export-evidence.json')],
                    passed_checks=sum(row['passed'] for row in trace_a['checks']),
                    failed_checks=sum(not row['passed'] for row in trace_a['checks']),
                    trace_equal=True, native_readiness=result['native_readiness'])
                if native_mode == 'official':
                    item.update(native_export_scope=result['native_export_scope'],
                                full_publication_body_bytes=result['full_publication_body_bytes'],
                                provider_final_export=False, official_PUB01_PASS=False)
            except (FoundationError, OSError, ValueError, TypeError, KeyError, ImportError) as error:
                # Diagnostics are local exception codes; inputs or credentials
                # are never interpolated into this public command record.
                item['diagnostic'] = str(error) if isinstance(error, FoundationError) else type(error).__name__
            item['elapsed_seconds_including_verification'] = time.monotonic() - job_start
            if time.monotonic() >= final_deadline:
                item.update(status='BLOCKED', diagnostic='BUILDER_SHARED_DEADLINE_EXCEEDED')
            report['products'].append(item)
    if _pins(behavioral_repair) != pins:
        report['diagnostic'] = 'BUILDER_ENTRY_RUNTIME_CHANGED'
    elif network_attempts:
        report['diagnostic'] = 'BUILDER_NETWORK_ATTEMPTED'
    elif all(item['status'] == SUCCESS for item in report['products']):
        report['status'] = SUCCESS
    report.update(elapsed_seconds=time.monotonic() - started, network_attempts=len(network_attempts))
    _seal(target, report, started, final_deadline)
    return read(target / 'run-report.json')


def _seal(target, report, started, deadline):
    if time.monotonic() >= deadline:
        report.update(status='BLOCKED', diagnostic='BUILDER_SHARED_DEADLINE_EXCEEDED')
    report['elapsed_seconds'] = time.monotonic() - started
    raw = raw_json(report)
    scan_public(raw, '.json')
    (target / 'run-report.json').write_bytes(raw)
    manifest = {'files': [{'path': 'run-report.json', 'bytes': len(raw), 'sha256': sha(raw)}]}
    (target / 'manifest.json').write_bytes(raw_json(manifest))
    (target / 'FINAL.json').write_bytes(raw_json({'status': report['status'], 'publish_allowed': False,
        'manifest_sha256': sha((target / 'manifest.json').read_bytes()),
        'elapsed_seconds': time.monotonic() - started}))
    if time.monotonic() >= deadline:
        # An immutable failure marker takes precedence over a prior PASS seal.
        (target / 'FAILED_FINALIZATION.json').write_bytes(raw_json({'reason': 'BUILDER_SHARED_DEADLINE_EXCEEDED'}))
        report.update(status='BLOCKED', diagnostic='BUILDER_SHARED_DEADLINE_EXCEEDED')
        (target / 'run-report.json').write_bytes(raw_json(report))


def verify_builder(directory):
    """Verify the command record and re-execute each native export's fixed checks."""
    explicit = Path(directory)
    root = explicit.resolve()
    if explicit.is_symlink() or not root.is_dir() or (root / 'FAILED_FINALIZATION.json').exists():
        raise FoundationError('BUILDER_FINALIZATION_BLOCKED')
    if {p.name for p in root.iterdir()} != {'run-report.json', 'manifest.json', 'FINAL.json'} or any(
            p.is_symlink() or not p.is_file() for p in root.iterdir()):
        raise FoundationError('BUILDER_RECORD_INVENTORY_INVALID')
    final, manifest = read(root / 'FINAL.json'), read(root / 'manifest.json')
    if final.get('status') != SUCCESS or final.get('publish_allowed') is not False or sha(
            (root / 'manifest.json').read_bytes()) != final.get('manifest_sha256'):
        raise FoundationError('BUILDER_RECORD_SEAL_INVALID')
    raw = (root / 'run-report.json').read_bytes()
    if manifest != {'files': [{'path': 'run-report.json', 'bytes': len(raw), 'sha256': sha(raw)}]}:
        raise FoundationError('BUILDER_RECORD_HASH_MISMATCH')
    report = strict_json(raw.decode())
    from builder.native_write_guard import validate_runtime_mode
    validate_runtime_mode(report.get('native_runtime_mode', 'host_exclusive'))
    if report.get('native_mode', 'prefix') not in ('prefix', 'official'):
        raise FoundationError('BUILDER_RECORD_NATIVE_MODE_INVALID')
    if report.get('status') != SUCCESS or any(report.get(name) is not False for name in (
            'publish_allowed', 'publication_ready', 'final_submission_ready', 'platform_success', 'live_enabled')):
        raise FoundationError('BUILDER_RECORD_SCOPE_PROMOTION_REJECTED')
    if (not isinstance(report.get('products'), list) or len(report['products']) not in (1, 2)
            or type(report.get('deadline_seconds_per_brief')) is not int
            or not 1 <= report['deadline_seconds_per_brief'] <= 1800
            or type(report.get('elapsed_seconds')) not in (float, int)
            or not 0 <= report['elapsed_seconds'] < len(report['products']) * report['deadline_seconds_per_brief']
            or report.get('network_calls') != 0 or report.get('network_attempts') != 0):
        raise FoundationError('BUILDER_RECORD_BUDGET_OR_SHAPE_INVALID')
    repairs = [row.get('behavioral_repair') for row in report['products']]
    if report.get('entry_runtime_sha256') != _pins(any(row is not None for row in repairs)):
        raise FoundationError('BUILDER_ENTRY_RUNTIME_CHANGED')
    results = []
    for row in report['products']:
        product = ROOT / row['product_directory']
        if not product.resolve().is_relative_to(PRODUCT_OUTPUT.resolve()) or product.is_symlink():
            raise FoundationError('BUILDER_PRODUCT_PATH_INVALID')
        checked = verify_product_build(product)
        if (row['status'] != SUCCESS or not checked['valid'] or checked['publish_allowed'] is not False
                or sha((product / 'manifest.json').read_bytes()) != row['manifest_sha256']
                or row.get('native_export') != checked or row.get('publish_allowed') is not False
                or type(row.get('elapsed_seconds_including_verification')) not in (float, int)
                or not 0 <= row['elapsed_seconds_including_verification'] < report['deadline_seconds_per_brief']):
            raise FoundationError('BUILDER_PRODUCT_RECHECK_FAILED')
        evidence = read(product / 'critical-evidence.json')
        product_report = read(product / 'readiness.json')
        if product_report.get('native_runtime_mode', 'host_exclusive') != report.get('native_runtime_mode', 'host_exclusive'):
            raise FoundationError('BUILDER_PRODUCT_NATIVE_RUNTIME_MODE_MISMATCH')
        if product_report.get('native_mode', 'prefix') != report.get('native_mode', 'prefix'):
            raise FoundationError('BUILDER_PRODUCT_NATIVE_MODE_MISMATCH')
        if report.get('native_mode') == 'official' and (
                row.get('native_export_scope') != product_report.get('native_export_scope')
                or row.get('full_publication_body_bytes') != product_report.get('full_publication_body_bytes')
                or row.get('provider_final_export') is not False or row.get('official_PUB01_PASS') is not False):
            raise FoundationError('BUILDER_OFFICIAL_SCOPE_OR_BUDGET_MISMATCH')
        expected_traces = [(product / name).relative_to(ROOT).as_posix()
            for name in ('critical-evidence.json', 'serialized-export-evidence.json')]
        if (row.get('passed_checks') != sum(entry['passed'] for entry in evidence['checks'])
                or row.get('failed_checks') != sum(not entry['passed'] for entry in evidence['checks'])
                or row.get('trace_equal') is not True or row.get('compared_event_traces') != expected_traces
                or row.get('export_path') != (product / 'agent.workflow.json').relative_to(ROOT).as_posix()
                or row.get('actual_export_bytes') != (product / 'agent.workflow.json').stat().st_size
                or row.get('native_readiness') != product_report['native_readiness']):
            raise FoundationError('BUILDER_PRODUCT_CLAIMS_NOT_DERIVED_FROM_EVIDENCE')
        behavioral = row.get('behavioral_repair')
        if behavioral is not None:
            repair_path = ROOT / behavioral['directory']
            if (repair_path.is_symlink() or not repair_path.resolve().is_relative_to(
                    (ROOT / 'artifacts/behavioral-repair').resolve())
                    or behavioral.get('applies_to_native_export') is not False):
                raise FoundationError('BUILDER_BEHAVIORAL_PATH_OR_SCOPE_INVALID')
            module = importlib.import_module('orchestration.behavioral_repair')
            repair_checked = module.verify_behavioral_cycle(repair_path)
            if (not repair_checked.get('valid') or repair_checked != behavioral.get('verification')
                    or read(repair_path / 'report.json') != behavioral.get('report')):
                raise FoundationError('BUILDER_BEHAVIORAL_EVIDENCE_CHANGED')
        results.append({'id': row['id'], 'valid': True, 'native_export': checked})
    return {'status': SUCCESS, 'valid': True, 'products': results, 'publish_allowed': False,
            'platform_success': False, 'network_calls': 0,
            'behavioral_record_recheck': 'Native product re-executed; optional portable behavioral evidence independently verified in its own scope'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--brief', action='append', type=Path)
    group.add_argument('--batch-spec', type=Path)
    group.add_argument('--verify', type=Path)
    parser.add_argument('--tool-api', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--behavioral-repair', action='store_true')
    parser.add_argument('--behavior-script', type=Path)
    parser.add_argument('--native-mode', choices=('official', 'prefix'), default='official',
                        help='Default produces the full supported native promise branch; prefix is explicitly partial.')
    parser.add_argument("--native-runtime-mode", choices=("host_exclusive", "official_bank_session"), default="host_exclusive")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            if args.behavioral_repair or args.behavior_script:
                parser.error('Verification cannot change the generated workflow')
            result = verify_builder(args.verify)
        else:
            if args.out is None:
                parser.error('--out must name a fresh child of artifacts/unified-builder/v1')
            if args.batch_spec:
                spec = read(args.batch_spec)
                if not isinstance(spec, dict) or set(spec) != {'jobs'} or args.behavior_script:
                    parser.error('Batch spec must contain exactly jobs; repair scripts belong to each job')
                jobs = spec['jobs']
            else:
                if args.tool_api is None or args.behavioral_repair != bool(args.behavior_script):
                    parser.error('--tool-api is required; behavioral repair requires --behavior-script')
                jobs = [{'id': 'agent', 'briefs': args.brief, 'tool_api': args.tool_api}]
                if args.behavioral_repair:
                    jobs[0]['behavior_script'] = args.behavior_script
            result = build_builder(jobs=jobs, output_directory=args.out,
                deadline_seconds=args.deadline_seconds, behavioral_repair=args.behavioral_repair,
                native_mode=args.native_mode, native_runtime_mode=args.native_runtime_mode)
    except (FoundationError, OSError, ValueError, TypeError, KeyError) as error:
        result = {'status': 'BLOCKED', 'diagnostic': str(error) if isinstance(error, FoundationError)
                  else type(error).__name__, 'publish_allowed': False, 'platform_success': False}
    print(raw_json(result).decode().strip())
    return 0 if result['status'] == SUCCESS else 2


if __name__ == '__main__':
    raise SystemExit(main())
