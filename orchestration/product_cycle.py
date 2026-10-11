"""One offline entry point for generation, export verification and mechanical cycle.

Practice preflight eligibility is scoped and is never publication authorization.
Persisted monotonic timestamps are not trusted as a new process's deadline.
No model, credential or transport integration is imported by this wrapper.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import math
import re
import time

from builder.export_pipeline import generate_export, verify_export_run
from builder.models import FoundationError, strict_json
from orchestration.repair_cycle import run_cycle, verify_cycle, sha, canonical

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / 'artifacts/product-cycle'


class ProductBlocked(Exception):
    def __init__(self, code):
        self.code = code


def _read_json(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def run_product_cycle(*, output_directory, brief_paths=(), tool_api_path=None,
                      input_directory=None, context_paths=(), cases_path=None,
                      mode='practice', compilation_envelope=None,
                      deadline_seconds=120, max_repair_attempts=2):
    """Produce three fresh packages under a single in-memory deadline.

    Known practice export only. General semantic lowering remains blocked when
    no supported conversation emitter is available. Old packages are immutable.
    """
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise ValueError('Shared deadline must be an integer in 1..1800 seconds')
    if type(max_repair_attempts) is not int or not 1 <= max_repair_attempts <= 2:
        raise ValueError('Mechanical validation attempts must be one or two')
    package = Path(output_directory).resolve()
    if not package.is_relative_to(OUTPUT_ROOT.resolve()) or package == OUTPUT_ROOT.resolve() or package.exists():
        raise ValueError('Use a fresh child of artifacts/product-cycle')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', package.name):
        raise ValueError('Product package name must be a portable local identifier')
    export = ROOT / 'artifacts/export-pipeline' / (package.name + '-export')
    repair = ROOT / 'artifacts/repair-cycle' / (package.name + '-repair')
    if export.exists() or repair.exists():
        raise ValueError('Linked export/repair package already exists; choose a fresh name')
    package.mkdir(parents=True)
    started = time.monotonic()
    deadline = started + deadline_seconds
    trace, lessons = [], []
    report = {'version': 'offline-product-cycle-1', 'status': 'BLOCKED',
              'diagnostic_code': 'NOT_RUN', 'mode': mode,
              'export_directory': str(export), 'repair_directory': str(repair),
              'deadline_seconds': deadline_seconds, 'max_repair_attempts': max_repair_attempts,
              'publish_allowed': False, 'ready_for_platform_preflight': False,
              'final_submission_ready': False, 'platform_validated': False,
              'semantic_truth_verified': False, 'automatic_semantic_repair': 'UNSUPPORTED',
              'network_calls': 0, 'model_calls': 0, 'credentials_read': False,
              'official_cases_evaluated': 0}

    def write(name, value):
        with (package / name).open('xb') as stream:
            stream.write(canonical(value))

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def guard():
        if remaining() <= 0:
            raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')

    def step(stage, code, *, input_hash=None, output_hash=None, **detail):
        trace.append({'seq': len(trace) + 1, 'stage': stage, 'diagnostic_code': code,
                      'input_sha256': input_hash, 'output_sha256': output_hash,
                      'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                      'remaining_limit_seconds': round(remaining(), 6), **detail})

    def export_check(expected_hash):
        guard()
        verified = verify_export_run(export, require_current_sources=True,
                                     trusted_manifest_sha256=expected_hash,
                                     deadline_monotonic=deadline)
        guard()
        if verified.get('status') != 'verified' or verified.get('mode') != 'practice' or not verified.get('artifact_hashes_valid') or not verified.get('ready_for_platform_preflight'):
            raise ProductBlocked('EXPORT_INDEPENDENT_VERIFICATION_BLOCKED')
        return verified

    try:
        guard()
        stage_budget = math.floor(remaining())
        if stage_budget < 1:
            raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')
        step('generation', 'GENERATION_STARTED', remaining_stage_budget_seconds=stage_budget)
        generated = generate_export(output_directory=export, input_directory=input_directory,
                                    brief_paths=brief_paths, tool_api_path=tool_api_path,
                                    context_paths=context_paths, cases_path=cases_path,
                                    mode=mode, compilation_envelope=compilation_envelope,
                                    deadline_seconds=stage_budget)
        guard()
        manifest_hash = generated.get('manifest_sha256')
        if not isinstance(manifest_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', manifest_hash):
            raise ProductBlocked('GENERATED_MANIFEST_REFERENCE_MISSING')
        report['export_manifest_sha256'] = manifest_hash
        write('generation-result.json', generated)
        step('generation', 'GENERATION_COMPLETED', output_hash=manifest_hash)
        if mode != 'practice' or generated.get('status') != 'verified':
            raise ProductBlocked('GENERAL_OR_PREVIEW_EXECUTABLE_EXPORT_UNSUPPORTED')
        verified = export_check(manifest_hash)
        write('export-verification-before.json', verified)
        step('independent_export_verification', 'EXPORT_VERIFIED', input_hash=manifest_hash,
             checked_artifacts=verified['checked_artifacts'])
        handoff_path = export / 'repair-handoff.json'
        handoff = _read_json(handoff_path)
        if handoff.get('repair_existing_sealed_evidence_allowed') is not False:
            raise ProductBlocked('SEALED_EVIDENCE_REPAIR_FORBIDDEN')
        if Path(handoff.get('workflow_path', '')).resolve() != (export / 'candidate.workflow.json').resolve():
            raise ProductBlocked('REPAIR_HANDOFF_WORKFLOW_MISMATCH')
        workflow_hash = sha((export / 'candidate.workflow.json').read_bytes())
        if handoff.get('trusted_workflow_sha256') != workflow_hash:
            raise ProductBlocked('REPAIR_HANDOFF_HASH_MISMATCH')
        sources = handoff.get('source_snapshot')
        if not isinstance(sources, dict) or not sources:
            raise ProductBlocked('REPAIR_HANDOFF_SOURCES_MISSING')
        # The exporter pins snapshots inside its NEW package. Never accept a
        # handoff instructing arbitrary files/credentials to be read by repair.
        for name in sources:
            source = Path(name).resolve(strict=True)
            if not source.is_relative_to(export) or not source.is_file():
                raise ProductBlocked('REPAIR_HANDOFF_SOURCE_PATH_INVALID')
        handed_limit = handoff.get('remaining_limit_seconds_at_handoff')
        if type(handed_limit) not in (int, float) or not math.isfinite(handed_limit) or handed_limit <= 0:
            raise ProductBlocked('REPAIR_HANDOFF_BUDGET_INVALID')
        guard()
        # memory deadline is authoritative; recorded remaining time can only
        # narrow it. A persisted monotonic value from another process is ignored.
        repair_deadline = min(deadline, time.monotonic() + handed_limit)
        repair_budget = min(remaining(), handed_limit)
        step('repair_handoff', 'PINNED_HANDOFF_ADMITTED', input_hash=sha(handoff_path.read_bytes()),
             persisted_monotonic_ignored=True, shared_deadline_applied=True,
             handed_remaining_limit_seconds=handed_limit)
        cycle = run_cycle(workflow_path=handoff['workflow_path'],
                          trusted_workflow_sha256=handoff['trusted_workflow_sha256'],
                          source_snapshot=sources, output_directory=repair,
                          max_attempts=max_repair_attempts, budget_seconds=repair_budget,
                          deadline_monotonic=repair_deadline)
        guard()
        write('repair-result.json', cycle)
        if cycle.get('status') != 'LOCAL_ROUNDTRIP_PASS' or cycle.get('cycle_integrity') != 'valid':
            raise ProductBlocked('MECHANICAL_REPAIR_CYCLE_BLOCKED')
        report['repair_manifest_sha256'] = cycle['manifest_sha256']
        report['repair_final_receipt_sha256'] = cycle['final_receipt_sha256']
        verified_cycle = verify_cycle(repair, cycle['manifest_sha256'])
        guard()
        write('repair-verification.json', verified_cycle)
        if verified_cycle.get('integrity') != 'valid' or verified_cycle.get('acceptance') != 'LOCAL_ROUNDTRIP_PASS':
            raise ProductBlocked('REPAIR_INDEPENDENT_VERIFICATION_BLOCKED')
        if len(cycle.get('attempts', [])) > max_repair_attempts:
            raise ProductBlocked('REPAIR_ATTEMPT_BOUND_VIOLATED')
        lessons.extend(_read_json(repair / 'learned-regressions.json'))
        lessons.append({'regression_id': 'product-roundtrip-gates',
                        'diagnostic_code': 'LOCAL_GENERATION_EXPORT_ROUNDTRIP_VERIFIED',
                        'lesson': 'Retain sealed-source, exact-generated-smoke and byte-roundtrip gates in future builds; mechanical success does not establish semantic completeness.'})
        step('independent_repair_verification', 'MECHANICAL_CYCLE_VERIFIED',
             input_hash=cycle['manifest_sha256'], output_hash=sha(canonical(verified_cycle)),
             attempts=len(cycle['attempts']), repaired=cycle['repaired'])
        verified_after = export_check(manifest_hash)
        write('export-verification-after.json', verified_after)
        guard()
        report.update(status='LOCAL_PRODUCT_CYCLE_PASS', diagnostic_code='LOCAL_PRODUCT_CYCLE_VERIFIED',
                      ready_for_platform_preflight=True,
                      publication_scope='controlled_synthetic_practice_pilot_only',
                      mechanical_attempts=len(cycle['attempts']), repaired=cycle['repaired'])
        step('complete', 'LOCAL_PRODUCT_CYCLE_VERIFIED', input_hash=manifest_hash,
             output_hash=cycle['manifest_sha256'], publish_allowed=False, final_submission_ready=False)
    except ProductBlocked as exc:
        report['diagnostic_code'] = exc.code
        step('blocked', exc.code)
        lessons.append({'regression_id': 'product-rejection', 'diagnostic_code': exc.code,
                        'lesson': 'Preserve the rejection and start a new reviewed run; never repair existing seals or relax source/evaluator expectations.'})
    except (FoundationError, OSError, KeyError, TypeError, ValueError) as exc:
        report['diagnostic_code'] = 'GENERATION_OR_EVIDENCE_INVALID'
        step('blocked', report['diagnostic_code'], exception_type=type(exc).__name__)
        lessons.append({'regression_id': 'product-invalid-evidence', 'diagnostic_code': report['diagnostic_code'],
                        'lesson': 'Missing or invalid production evidence blocks repair and preflight.'})
    report.update(elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                  remaining_limit_seconds=round(remaining(), 6))
    write('learned-regressions.json', lessons)
    write('trace.json', trace)
    write('report.json', report)
    inventory = [{'path': p.name, 'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())}
                 for p in sorted(package.iterdir()) if p.is_file()]
    write('manifest.json', {'version': 'offline-product-cycle-1', 'files': inventory})
    manifest_hash = sha((package / 'manifest.json').read_bytes())
    verification = verify_product_cycle(package, manifest_hash, deadline_monotonic=deadline, require_final=False)
    write('verification.json', verification)
    final_status, final_code = report['status'], report['diagnostic_code']
    if verification['integrity'] != 'valid':
        final_status, final_code = 'BLOCKED', verification['diagnostic_code']
    if remaining() <= 0:
        final_status, final_code = 'BLOCKED', 'SHARED_DEADLINE_EXHAUSTED'
    final_elapsed = time.monotonic() - started
    write('FINAL.json', {'kind': 'product-cycle-final-receipt-1', 'manifest_sha256': manifest_hash,
                        'status': final_status, 'diagnostic_code': final_code,
                        'elapsed_seconds': final_elapsed, 'deadline_seconds': deadline_seconds,
                        'publish_allowed': False, 'ready_for_platform_preflight': final_status == 'LOCAL_PRODUCT_CYCLE_PASS'})
    if remaining() <= 0 and final_status == 'LOCAL_PRODUCT_CYCLE_PASS':
        final_status, final_code = 'BLOCKED', 'SHARED_DEADLINE_EXHAUSTED'
        write('FAILED_FINALIZATION.json', {'diagnostic_code': final_code, 'manifest_sha256': manifest_hash})
    final_verification = verify_product_cycle(package, manifest_hash, deadline_monotonic=deadline)
    final_elapsed = time.monotonic() - started
    if final_elapsed >= deadline_seconds and final_status == 'LOCAL_PRODUCT_CYCLE_PASS':
        final_status, final_code = 'BLOCKED', 'SHARED_DEADLINE_EXHAUSTED'
        if not (package / 'FAILED_FINALIZATION.json').exists():
            write('FAILED_FINALIZATION.json', {'diagnostic_code': final_code, 'manifest_sha256': manifest_hash})
        final_verification = {'integrity': 'invalid', 'diagnostic_code': final_code}
    returned = {**report, 'package_directory': str(package), 'manifest_sha256': manifest_hash,
                'status': final_status, 'diagnostic_code': final_code,
                'elapsed_ms': round(final_elapsed * 1000, 3),
                'remaining_limit_seconds': round(max(0.0, deadline_seconds - final_elapsed), 6),
                'package_integrity': final_verification['integrity'],
                'final_receipt_sha256': sha((package / 'FINAL.json').read_bytes())}
    if final_verification['integrity'] != 'valid' or final_status != 'LOCAL_PRODUCT_CYCLE_PASS':
        returned.update(status='BLOCKED', diagnostic_code=final_verification.get('diagnostic_code', final_code),
                        ready_for_platform_preflight=False)
    return returned


def verify_product_cycle(directory, trusted_manifest_sha256, *, deadline_monotonic=None, require_final=True):
    """Read-only composite verification; no persisted deadline is revived."""
    try:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')
        root = Path(directory).resolve(strict=True)
        if (root / 'FAILED_FINALIZATION.json').exists():
            raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')
        if not root.is_relative_to(OUTPUT_ROOT.resolve()) or root == OUTPUT_ROOT.resolve():
            raise ProductBlocked('PRODUCT_PATH_INVALID')
        raw = (root / 'manifest.json').read_bytes()
        if sha(raw) != trusted_manifest_sha256:
            raise ProductBlocked('PRODUCT_ROOT_MISMATCH')
        manifest = strict_json(raw.decode('utf-8'))
        if manifest.get('version') != 'offline-product-cycle-1' or not isinstance(manifest.get('files'), list):
            raise ProductBlocked('PRODUCT_MANIFEST_INVALID')
        names = set()
        for item in manifest['files']:
            name = item['path']
            if not isinstance(name, str) or name in names or Path(name).name != name or name in {'manifest.json', 'verification.json'}:
                raise ProductBlocked('PRODUCT_MANIFEST_PATH_INVALID')
            names.add(name)
            data = (root / name).read_bytes()
            if sha(data) != item['sha256'] or len(data) != item['bytes']:
                raise ProductBlocked('PRODUCT_EVIDENCE_HASH_MISMATCH')
        if not {'report.json', 'trace.json', 'learned-regressions.json'} <= names:
            raise ProductBlocked('PRODUCT_REQUIRED_EVIDENCE_MISSING')
        report = _read_json(root / 'report.json')
        if report.get('publish_allowed') is not False or report.get('final_submission_ready') is not False:
            raise ProductBlocked('PRODUCT_SCOPE_OVERCLAIM')
        if report['status'] == 'LOCAL_PRODUCT_CYCLE_PASS':
            export = Path(report['export_directory']).resolve()
            repair = Path(report['repair_directory']).resolve()
            if export != ROOT / 'artifacts/export-pipeline' / (root.name + '-export') or repair != ROOT / 'artifacts/repair-cycle' / (root.name + '-repair'):
                raise ProductBlocked('PRODUCT_LINK_PATH_INVALID')
            exported = verify_export_run(export, trusted_manifest_sha256=report['export_manifest_sha256'],
                                         deadline_monotonic=deadline_monotonic)
            cycle = verify_cycle(repair, report['repair_manifest_sha256'])
            if exported.get('status') != 'verified' or cycle.get('integrity') != 'valid' or cycle.get('acceptance') != 'LOCAL_ROUNDTRIP_PASS':
                raise ProductBlocked('PRODUCT_LINKED_EVIDENCE_INVALID')
            if sha((repair / 'FINAL.json').read_bytes()) != report['repair_final_receipt_sha256']:
                raise ProductBlocked('PRODUCT_REPAIR_FINAL_RECEIPT_MISMATCH')
            repair_report = _read_json(repair / 'report.json')
            if repair_report.get('status') != 'LOCAL_ROUNDTRIP_PASS' or not 1 <= len(repair_report.get('attempts', [])) <= report['max_repair_attempts']:
                raise ProductBlocked('PRODUCT_LINKED_REPAIR_BLOCKED')
            exported_bytes = (export / 'candidate.workflow.json').read_bytes()
            if sha((repair / 'reference.workflow.json').read_bytes()) != sha(exported_bytes):
                raise ProductBlocked('PRODUCT_LINKED_WORKFLOW_MISMATCH')
            repaired_path = (repair / repair_report['final_workflow']).resolve(strict=True)
            if not repaired_path.is_relative_to(repair) or canonical(_read_json(repaired_path)) != canonical(_read_json(export / 'candidate.workflow.json')):
                raise ProductBlocked('PRODUCT_LINKED_WORKFLOW_MISMATCH')
        elif report['status'] != 'BLOCKED':
            raise ProductBlocked('PRODUCT_STATUS_INVALID')
        final_status = report['status']
        if require_final:
            receipt = _read_json(root / 'FINAL.json')
            if receipt.get('kind') != 'product-cycle-final-receipt-1' or receipt.get('manifest_sha256') != trusted_manifest_sha256 or receipt.get('publish_allowed') is not False:
                raise ProductBlocked('PRODUCT_FINAL_RECEIPT_INVALID')
            final_status = receipt.get('status')
            if final_status not in {'LOCAL_PRODUCT_CYCLE_PASS', 'BLOCKED'}:
                raise ProductBlocked('PRODUCT_FINAL_RECEIPT_INVALID')
            if final_status == 'LOCAL_PRODUCT_CYCLE_PASS':
                elapsed, budget = receipt.get('elapsed_seconds'), receipt.get('deadline_seconds')
                if type(elapsed) not in (int, float) or type(budget) is not int or not math.isfinite(elapsed) or not 0 <= elapsed < budget <= 1800 or final_status != report['status']:
                    raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')
            if receipt.get('ready_for_platform_preflight') != (final_status == 'LOCAL_PRODUCT_CYCLE_PASS'):
                raise ProductBlocked('PRODUCT_FINAL_RECEIPT_INVALID')
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise ProductBlocked('SHARED_DEADLINE_EXHAUSTED')
        return {'integrity': 'valid', 'status': final_status, 'independent_root_verified': False,
                'publish_allowed': False, 'ready_for_platform_preflight': final_status == 'LOCAL_PRODUCT_CYCLE_PASS',
                'final_submission_ready': False}
    except ProductBlocked as exc:
        return {'integrity': 'invalid', 'diagnostic_code': exc.code, 'publish_allowed': False}
    except (OSError, KeyError, TypeError, ValueError, FoundationError):
        return {'integrity': 'invalid', 'diagnostic_code': 'PRODUCT_EVIDENCE_MISSING_OR_INVALID', 'publish_allowed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brief', action='append', default=[])
    parser.add_argument('--tool-api')
    parser.add_argument('--input')
    parser.add_argument('--mode', choices=['practice', 'preview', 'general'], default='practice')
    parser.add_argument('--out', required=True)
    parser.add_argument('--deadline-seconds', type=int, default=120)
    parser.add_argument('--max-repair-attempts', type=int, default=2)
    opts = parser.parse_args(argv)
    result = run_product_cycle(output_directory=opts.out, brief_paths=opts.brief, tool_api_path=opts.tool_api,
                               input_directory=opts.input, mode=opts.mode, deadline_seconds=opts.deadline_seconds,
                               max_repair_attempts=opts.max_repair_attempts)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['status'] == 'LOCAL_PRODUCT_CYCLE_PASS' and result['package_integrity'] == 'valid' else 1


if __name__ == '__main__':
    raise SystemExit(main())
