"""Bounded offline export round-trip and safe serialization regeneration.

This is not a semantic repair provider. It never edits sources, expectations,
runtime policy, existing evidence or old outputs. A malformed newly generated
JSON working copy may be regenerated from a separately hash-pinned workflow.
Every accepted result remains local-structure-only and publication-blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

from builder.freya_export import validate_local_workflow
from builder.models import FoundationError, strict_json

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 20_000_000
HASH = re.compile(r'[0-9a-f]{64}')
PROTECTED = {'.freya-local', '.codex', '.agents', '.git', '.ssh', '.aws', '.azure', '.gcp'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


class CycleBlocked(Exception):
    def __init__(self, code):
        self.code = code


def _admitted(path):
    original = Path(path)
    resolved = original.resolve(strict=True)
    # Explicit workspace-local files only. No recursive discovery or secrets.
    if not resolved.is_relative_to(ROOT) or not resolved.is_file() or original.is_symlink():
        raise CycleBlocked('INPUT_PATH_REJECTED')
    if any(part.lower() in PROTECTED for part in resolved.parts) or resolved.name.lower().startswith('.env'):
        raise CycleBlocked('PROTECTED_INPUT_REJECTED')
    if resolved.stat().st_size > MAX_BYTES:
        raise CycleBlocked('INPUT_SIZE_LIMIT')
    return resolved


def run_cycle(*, workflow_path, trusted_workflow_sha256, source_snapshot,
              output_directory, candidate_bytes=None, max_attempts=2,
              budget_seconds=120, deadline_monotonic=None):
    """Produce a fresh sealed local cycle, never rewrite the supplied workflow.

    source_snapshot is {explicit_local_path: expected_SHA256}. The caller must
    obtain reference hashes independently; deriving them from corrupt evidence
    does not establish provenance. Candidate bytes are a NEW producer working
    copy, not a repair target for previously sealed test evidence.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
        raise ValueError('max_attempts must be one or two')
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 1800:
        raise ValueError('budget_seconds must be finite and in (0, 1800]')
    if deadline_monotonic is not None and (type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic)):
        raise ValueError('deadline_monotonic must be a finite shared-clock deadline')
    output = Path(output_directory).resolve()
    allowed_output = (ROOT / 'artifacts' / 'repair-cycle').resolve()
    if not output.is_relative_to(allowed_output) or output == allowed_output or output.exists():
        raise ValueError('Use a fresh child of artifacts/repair-cycle')
    output.mkdir(parents=True)
    started = time.monotonic()
    artifacts, steps, learning, attempts = [], [], [], []
    pinned = {}
    report = {'kind': 'bounded-offline-serialization-repair-cycle', 'version': '1',
              'status': 'BLOCKED', 'diagnostic_code': 'NOT_RUN', 'attempts': attempts,
              'budget_seconds': budget_seconds, 'max_attempts': max_attempts,
              'shared_deadline_applied': deadline_monotonic is not None,
              'repair_scope': 'corrupted_new_output_serialization_only_not_source_policy_regeneration',
              'automatic_semantic_repair': 'UNSUPPORTED', 'model_calls': 0,
              'network_calls': 0, 'credentials_read': False,
              'semantic_truth_verified': False, 'platform_validated': False,
              'publish_allowed': False, 'trusted_reference_scope': 'caller-provided-hashes-not-independent-Hunhan-signature'}

    def write(name, raw):
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as stream:
            stream.write(raw)
        artifacts.append({'path': name, 'bytes': len(raw), 'sha256': sha(raw)})
        return sha(raw)

    def remaining():
        now = time.monotonic()
        left = budget_seconds - (now - started)
        if deadline_monotonic is not None:
            left = min(left, deadline_monotonic - now)
        return max(0.0, left)

    def record(stage, code, *, input_sha=None, output_sha=None, repair='none', **details):
        steps.append({'seq': len(steps) + 1, 'stage': stage, 'diagnostic_code': code,
                      'input_sha256': input_sha, 'output_sha256': output_sha,
                      'repair_operation': repair,
                      'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                      'budget_seconds': budget_seconds, 'remaining_limit_seconds': round(remaining(), 6),
                      'remaining_attempts': max_attempts - len(attempts), **details})

    def ensure_current():
        if remaining() <= 0:
            raise CycleBlocked('TIME_BUDGET_EXHAUSTED')
        for path, expected in pinned.items():
            try:
                current = _admitted(path).read_bytes()
            except (OSError, CycleBlocked):
                raise CycleBlocked('SOURCE_SNAPSHOT_CHANGED') from None
            if sha(current) != expected:
                raise CycleBlocked('SOURCE_SNAPSHOT_CHANGED')

    def learn(code, input_sha, operation):
        learning.append({'regression_id': f'local-cycle-{len(learning) + 1}',
                         'diagnostic_code': code, 'input_sha256': input_sha,
                         'repair_operation': operation,
                         'required_future_gate': code,
                         'lesson': {'GENERATED_JSON_INVALID': 'Regenerate serialization from unchanged pinned reference; do not patch policy.',
                                    'SEMANTIC_DIVERGENCE': 'Preserve policy and evaluator expectations; semantic repair needs a supported provider.',
                                    'SOURCE_SNAPSHOT_CHANGED': 'Start a new cycle from newly reviewed sources; never reuse mixed-version evidence.'}.get(code,
                                     'Keep this rejection as a regression; do not relax the acceptance gate.')})

    try:
        if not isinstance(trusted_workflow_sha256, str) or not HASH.fullmatch(trusted_workflow_sha256):
            raise CycleBlocked('TRUSTED_WORKFLOW_REFERENCE_MISSING')
        if not isinstance(source_snapshot, dict) or not source_snapshot:
            raise CycleBlocked('SOURCE_SNAPSHOT_MISSING')
        workflow = _admitted(workflow_path)
        raw_reference = workflow.read_bytes()
        if sha(raw_reference) != trusted_workflow_sha256:
            raise CycleBlocked('TRUSTED_WORKFLOW_HASH_MISMATCH')
        pinned[workflow] = trusted_workflow_sha256
        # Pin the independent validator and cycle implementation as well.
        from builder import freya_export, models
        snapshots = dict(source_snapshot)
        for module in (freya_export, models):
            path = Path(module.__file__).resolve()
            snapshots.setdefault(str(path), sha(path.read_bytes()))
        snapshots.setdefault(str(Path(__file__).resolve()), sha(Path(__file__).read_bytes()))
        rows = []
        for original, expected in snapshots.items():
            if not isinstance(expected, str) or not HASH.fullmatch(expected):
                raise CycleBlocked('SOURCE_REFERENCE_INVALID')
            path = _admitted(original)
            raw = path.read_bytes()
            if sha(raw) != expected:
                raise CycleBlocked('SOURCE_HASH_MISMATCH')
            pinned[path] = expected
            rows.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': expected, 'bytes': len(raw)})
            name = 'sources/' + expected + '.bin'
            if not (output / name).exists():
                write(name, raw)
        write('source-snapshot.json', canonical(rows))
        write('reference.workflow.json', raw_reference)
        ensure_current()
        try:
            reference = strict_json(raw_reference.decode('utf-8'))
            trusted_validation = validate_local_workflow(reference)
        except (FoundationError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
            raise CycleBlocked('REFERENCE_WORKFLOW_INVALID') from None
        ensure_current()
        write('reference.validation.json', canonical(trusted_validation))
        record('reference_validation', 'REFERENCE_LOCAL_STRUCTURE_VALID', input_sha=sha(raw_reference),
               output_sha=sha(canonical(trusted_validation)), independent_validation=True)
        candidate = raw_reference if candidate_bytes is None else candidate_bytes
        if not isinstance(candidate, bytes) or len(candidate) > MAX_BYTES:
            raise CycleBlocked('CANDIDATE_BYTES_INVALID')
        for attempt in range(1, max_attempts + 1):
            ensure_current()
            candidate_hash = write(f'attempts/{attempt}/candidate.workflow.json', candidate)
            attempt_row = {'attempt': attempt, 'input_sha256': candidate_hash,
                           'diagnostic_code': 'NOT_RUN', 'local_structure_valid': False}
            attempts.append(attempt_row)
            # Validate the persisted bytes independently, rather than the in-memory
            # producer object or an earlier PASS declaration.
            persisted = (output / f'attempts/{attempt}/candidate.workflow.json').read_bytes()
            if sha(persisted) != candidate_hash:
                raise CycleBlocked('GENERATED_EVIDENCE_HASH_MISMATCH')
            try:
                parsed = strict_json(persisted.decode('utf-8'))
            except (ValueError, UnicodeError):
                attempt_row['diagnostic_code'] = 'GENERATED_JSON_INVALID'
                record('independent_validation', 'GENERATED_JSON_INVALID', input_sha=candidate_hash,
                       independent_validation=True, attempt=attempt)
                learn('GENERATED_JSON_INVALID', candidate_hash, 'regenerate_pinned_json_serialization')
                write(f'attempts/{attempt}/diagnostic.json', canonical(attempt_row))
                ensure_current()
                if attempt == max_attempts:
                    raise CycleBlocked('ATTEMPT_BUDGET_EXHAUSTED')
                candidate = canonical(reference)
                record('regeneration', 'SAFE_SERIALIZATION_REGENERATION', input_sha=candidate_hash,
                       output_sha=sha(candidate), repair='regenerate_pinned_json_serialization', attempt=attempt)
                continue
            if canonical(parsed) != canonical(reference):
                attempt_row['diagnostic_code'] = 'SEMANTIC_DIVERGENCE'
                write(f'attempts/{attempt}/diagnostic.json', canonical(attempt_row))
                learn('SEMANTIC_DIVERGENCE', candidate_hash, 'none_unsupported')
                raise CycleBlocked('SEMANTIC_DIVERGENCE')
            try:
                validation = validate_local_workflow(parsed)
            except (FoundationError, ValueError, TypeError, KeyError, AttributeError):
                raise CycleBlocked('CANDIDATE_STRUCTURE_INVALID') from None
            ensure_current()
            validation_hash = write(f'attempts/{attempt}/validation.json', canonical(validation))
            attempt_row.update(diagnostic_code='LOCAL_ROUNDTRIP_VALID', local_structure_valid=True,
                               output_sha256=validation_hash)
            write(f'attempts/{attempt}/diagnostic.json', canonical(attempt_row))
            record('independent_validation', 'LOCAL_ROUNDTRIP_VALID', input_sha=candidate_hash,
                   output_sha=validation_hash, independent_validation=True, attempt=attempt)
            report.update(status='LOCAL_ROUNDTRIP_PASS', diagnostic_code='LOCAL_ROUNDTRIP_VALID',
                          final_workflow=f'attempts/{attempt}/candidate.workflow.json',
                          final_workflow_sha256=candidate_hash)
            break
    except CycleBlocked as exc:
        report['diagnostic_code'] = exc.code
        record('blocked', exc.code)
        if not learning or learning[-1]['diagnostic_code'] != exc.code:
            learn(exc.code, None, 'none_blocked')
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        report['diagnostic_code'] = 'MISSING_OR_INVALID_INPUT_EVIDENCE'
        record('blocked', report['diagnostic_code'])
        learn(report['diagnostic_code'], None, 'none_blocked')
    report.update(elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                  remaining_limit_seconds=round(remaining(), 6), repaired=len(attempts) > 1 and report['status'] == 'LOCAL_ROUNDTRIP_PASS')
    write('learned-regressions.json', canonical(learning))
    write('steps.json', canonical(steps))
    write('report.json', canonical(report))
    manifest = {'kind': 'local-software-cycle-seal-not-independent-trust', 'files': artifacts.copy()}
    write('cycle-manifest.json', canonical(manifest))
    # Separate verifier re-reads every sealed byte; manifest does not include itself.
    manifest_path = output / 'cycle-manifest.json'
    manifest_hash = sha(manifest_path.read_bytes())
    verification = verify_cycle(output, manifest_hash, require_final=False)
    write('verification.json', canonical(verification))
    # The sealed report records local checks. This exclusive receipt, written
    # after heavy sealing/verification, is the mandatory final acceptance gate.
    final_status = report['status']
    final_code = report['diagnostic_code']
    if verification['integrity'] != 'valid':
        final_status, final_code = 'BLOCKED', 'FINAL_EVIDENCE_VERIFICATION_FAILED'
    if remaining() <= 0:
        final_status, final_code = 'BLOCKED', 'TIME_BUDGET_EXHAUSTED'
    final_elapsed = time.monotonic() - started
    effective_budget = budget_seconds if deadline_monotonic is None else max(0.0, min(budget_seconds, deadline_monotonic - started))
    receipt = {'kind': 'repair-cycle-final-receipt-1', 'manifest_sha256': manifest_hash,
               'status': final_status, 'diagnostic_code': final_code,
               'elapsed_seconds': final_elapsed, 'effective_budget_seconds': effective_budget,
               'publish_allowed': False}
    write('FINAL.json', canonical(receipt))
    # A late final receipt write can never revive eligibility on later reads.
    if remaining() <= 0 and final_status == 'LOCAL_ROUNDTRIP_PASS':
        final_status, final_code = 'BLOCKED', 'TIME_BUDGET_EXHAUSTED'
        write('FAILED_FINALIZATION.json', canonical({'diagnostic_code': final_code, 'manifest_sha256': manifest_hash}))
    final_verified = verify_cycle(output, manifest_hash)
    returned_elapsed = time.monotonic() - started
    if returned_elapsed >= effective_budget and final_status == 'LOCAL_ROUNDTRIP_PASS':
        final_status, final_code = 'BLOCKED', 'TIME_BUDGET_EXHAUSTED'
        if not (output / 'FAILED_FINALIZATION.json').exists():
            write('FAILED_FINALIZATION.json', canonical({'diagnostic_code': final_code, 'manifest_sha256': manifest_hash}))
        final_verified = {'integrity': 'invalid', 'diagnostic_code': final_code}
    return {**report, 'status': final_status, 'diagnostic_code': final_code,
            'elapsed_ms': round(returned_elapsed * 1000, 3),
            'remaining_limit_seconds': round(max(0.0, effective_budget - returned_elapsed), 6),
            'cycle_integrity': final_verified['integrity'], 'manifest_sha256': manifest_hash,
            'final_receipt_sha256': sha((output / 'FINAL.json').read_bytes())}


def verify_cycle(directory, trusted_manifest_sha256, *, require_final=True):
    """Read-only byte seal check; neither acceptance nor independent root trust."""
    try:
        root = Path(directory).resolve(strict=True)
        if (root / 'FAILED_FINALIZATION.json').exists():
            raise CycleBlocked('TIME_BUDGET_EXHAUSTED')
        raw = (root / 'cycle-manifest.json').read_bytes()
        if not isinstance(trusted_manifest_sha256, str) or not HASH.fullmatch(trusted_manifest_sha256) or sha(raw) != trusted_manifest_sha256:
            raise CycleBlocked('CYCLE_ROOT_MISMATCH')
        manifest = strict_json(raw.decode('utf-8'))
        if manifest.get('kind') != 'local-software-cycle-seal-not-independent-trust' or not isinstance(manifest.get('files'), list):
            raise CycleBlocked('CYCLE_MANIFEST_INVALID')
        seen = set()
        for entry in manifest['files']:
            name = entry['path']
            if name in seen or not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts or name in {'cycle-manifest.json', 'verification.json'}:
                raise CycleBlocked('CYCLE_MANIFEST_PATH_INVALID')
            seen.add(name)
            path = root / name
            if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink():
                raise CycleBlocked('CYCLE_MANIFEST_PATH_INVALID')
            contents = path.read_bytes()
            if sha(contents) != entry['sha256'] or len(contents) != entry['bytes']:
                raise CycleBlocked('CYCLE_EVIDENCE_HASH_MISMATCH')
        if not {'report.json', 'steps.json', 'learned-regressions.json'} <= seen:
            raise CycleBlocked('CYCLE_REQUIRED_EVIDENCE_MISSING')
        acceptance = None
        if require_final:
            receipt = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
            if receipt.get('kind') != 'repair-cycle-final-receipt-1' or receipt.get('manifest_sha256') != trusted_manifest_sha256 or receipt.get('publish_allowed') is not False:
                raise CycleBlocked('CYCLE_FINAL_RECEIPT_INVALID')
            if receipt.get('status') not in {'LOCAL_ROUNDTRIP_PASS', 'BLOCKED'}:
                raise CycleBlocked('CYCLE_FINAL_RECEIPT_INVALID')
            if receipt['status'] == 'LOCAL_ROUNDTRIP_PASS':
                elapsed, budget = receipt.get('elapsed_seconds'), receipt.get('effective_budget_seconds')
                if type(elapsed) not in (int, float) or type(budget) not in (int, float) or not math.isfinite(elapsed) or not math.isfinite(budget) or not 0 <= elapsed < budget <= 1800:
                    raise CycleBlocked('TIME_BUDGET_EXHAUSTED')
                recorded = strict_json((root / 'report.json').read_text(encoding='utf-8'))
                if recorded.get('status') != 'LOCAL_ROUNDTRIP_PASS':
                    raise CycleBlocked('CYCLE_FINAL_RECEIPT_INVALID')
            acceptance = receipt['status']
        return {'integrity': 'valid', 'verified_files': len(seen), 'independent_trust': False,
                'semantic_truth_verified': False, 'publish_allowed': False, 'acceptance': acceptance}
    except CycleBlocked as exc:
        return {'integrity': 'invalid', 'diagnostic_code': exc.code, 'publish_allowed': False}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return {'integrity': 'invalid', 'diagnostic_code': 'CYCLE_EVIDENCE_MISSING_OR_INVALID', 'publish_allowed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--trusted-workflow-sha256', required=True)
    parser.add_argument('--source-snapshot', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-attempts', type=int, default=2)
    parser.add_argument('--budget-seconds', type=float, default=120)
    options = parser.parse_args(argv)
    snapshot_path = _admitted(options.source_snapshot)
    snapshot = strict_json(snapshot_path.read_text(encoding='utf-8'))
    result = run_cycle(workflow_path=options.workflow, trusted_workflow_sha256=options.trusted_workflow_sha256,
                       source_snapshot=snapshot, output_directory=options.out,
                       max_attempts=options.max_attempts, budget_seconds=options.budget_seconds)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['status'] == 'LOCAL_ROUNDTRIP_PASS' and result['cycle_integrity'] == 'valid' else 1


if __name__ == '__main__':
    raise SystemExit(main())
