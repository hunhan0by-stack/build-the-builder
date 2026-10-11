"""Commit-pinned, key-free native practice build delivery without Git at judging.

Packing requires exact local Git bytes. Reproduction requires an independently
supplied anchor SHA-256; a checksum shipped beside the ZIP is not independent
authority. It executes the real emitter/verifier and never patches their ROOT,
commit accessor, or runtime. Practice candidates are not provider final exports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
KIT_REL = 'data/freya-hackathon/kit-v3/build-the-builder'
KIT_FILES = ('briefs/brief-a.md', 'briefs/brief-b.md', 'tools/openapi.json',
             'docs/workflow-format.md', 'docs/tools-and-code.md', 'examples/base-agent.json')
EXTRA = ('builder/native_kit_emitter.py', 'builder/native_kit_verification.py',
         'builder/native_write_guard.py', 'builder/native_official_closure.py',
         'orchestration/official_native_build.py', 'orchestration/final_run_pairing.py',
         'orchestration/official_native_delivery.py')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _raw(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode()


def _fail(code):
    raise ValueError('OFFICIAL_NATIVE_DELIVERY_' + code)


def _name(value):
    if not isinstance(value, str) or ':' in value or '\\' in value:
        _fail('PATH_INVALID')
    p = PurePosixPath(value)
    if p.is_absolute() or p.as_posix() != value or any(x in ('', '.', '..', '.git')
            or x.lower().startswith('.env') or x.lower().endswith('.dpapi')
            or x.lower() == 'env.sh' for x in p.parts):
        _fail('PATH_INVALID')
    return value


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        _fail('FILE_UNAVAILABLE')
    return path.read_bytes()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: _fail('NONFINITE_JSON'))


def source_names():
    from orchestration.native_product_delivery import source_files
    return sorted(set(source_files()) | set(EXTRA))


def _git(*arguments):
    result = subprocess.run(['git', '-c', 'safe.directory=' + str(ROOT).replace('\\', '/'),
        *arguments], cwd=ROOT, capture_output=True, timeout=15)
    if result.returncode:
        _fail('EXACT_GIT_SOURCE_UNAVAILABLE')
    return result.stdout


def pack(output, *, expected_commit):
    """No auto-commit/push: every shipped Python source must match exact HEAD.

    Briefs and platform documents are explicitly separate hashed inputs; their
    inclusion never asserts that competition materials belong to source Git.
    """
    if not isinstance(expected_commit, str) or not re.fullmatch('[0-9a-f]{40}', expected_commit):
        _fail('EXACT_COMMIT_REQUIRED')
    if _git('rev-parse', 'HEAD').decode().strip() != expected_commit:
        _fail('HEAD_MISMATCH')
    output = Path(output).resolve()
    if output.exists() or not output.is_relative_to((ROOT / 'artifacts').resolve()):
        _fail('FRESH_ARTIFACT_OUTPUT_REQUIRED')
    sources = source_names()
    inputs = [KIT_REL + '/' + name for name in KIT_FILES]
    files = {}
    for name in sources:
        raw = _read(ROOT / _name(name))
        if raw != _git('show', expected_commit + ':' + name):
            _fail('DIRTY_OR_UNCOMMITTED_FILE')
        files[name] = raw
    for name in inputs:
        files[name] = _read(ROOT / _name(name))
    from orchestration.prepare_delivery import scan_public
    for name, raw in files.items():
        scan_public(raw, Path(name).suffix)
    anchor = {'schema_version': 'official-native-portable-source-anchor-1',
        'source_commit': expected_commit, 'source_git_bytes_verified': True,
        'input_git_coverage_asserted': False,
        'source_sha256': {name: _sha(files[name]) for name in sources},
        'input_sha256': {name: _sha(files[name]) for name in inputs},
        'scope': 'reviewed practice A and A+B interpretation only; no hidden final briefs',
        'live_authority': False, 'provider_final_export': False}
    output.mkdir(parents=True)
    for name, raw in files.items():
        target = output / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    anchor_raw = _raw(anchor)
    (output / 'source-anchor.json').write_bytes(anchor_raw)
    (output / 'REPRODUCE.txt').write_text(
        'Practice native candidates only; no Git, external dependencies, keys or network needed.\n'
        'Obtain the source-anchor SHA-256 independently from the reviewed submission receipt.\n'
        'python -I -B code/orchestration/official_native_delivery.py reproduce --bundle . '
        '--anchor source-anchor.json --anchor-sha256 REVIEWER_SUPPLIED_HASH --label A --output FRESH_OUTPUT\n'
        'Repeat with --label AB only if this emitter supports that source. Unsupported scope is rejected, '
        'not replaced with A and not counted as a final export.\n'
        'Expected timing includes compilation, exact graph execution, payload check and proof writes; <=1800 seconds.\n'
        'Live publication/banking authority and official final run pairing are separate requirements.\n', encoding='utf-8')
    return {'status': 'EXACT_COMMITTED_PRACTICE_SOURCE_PACKED', 'source_commit': expected_commit,
        'source_files': len(sources), 'input_files': len(inputs), 'source_anchor_sha256': _sha(anchor_raw),
        'output': str(output), 'network_calls': 0, 'final_submission_ready': False}


def validate_anchor(bundle, anchor_file, trusted_sha256):
    if not isinstance(trusted_sha256, str) or not re.fullmatch('[0-9a-f]{64}', trusted_sha256):
        _fail('INDEPENDENT_ANCHOR_HASH_REQUIRED')
    raw = _read(anchor_file)
    if _sha(raw) != trusted_sha256:
        _fail('ANCHOR_HASH_MISMATCH')
    anchor = _json(raw)
    if (not isinstance(anchor, dict) or anchor.get('schema_version') != 'official-native-portable-source-anchor-1'
            or not re.fullmatch('[0-9a-f]{40}', anchor.get('source_commit', ''))
            or anchor.get('source_git_bytes_verified') is not True
            or anchor.get('live_authority') is not False or anchor.get('provider_final_export') is not False):
        _fail('ANCHOR_SCOPE_INVALID')
    code = Path(bundle).resolve() / 'code'
    inventory = {}
    for field in ('source_sha256', 'input_sha256'):
        entries = anchor.get(field)
        if not isinstance(entries, dict) or not entries:
            _fail('ANCHOR_INVENTORY_MISSING')
        for name, sha in entries.items():
            _name(name)
            if name in inventory or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
                _fail('ANCHOR_INVENTORY_INVALID')
            target = code / name
            if not target.resolve().is_relative_to(code) or any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(code)):
                _fail('PATH_ESCAPE')
            if _sha(_read(target)) != sha:
                _fail('SOURCE_OR_INPUT_HASH_MISMATCH')
            inventory[name] = sha
    actual = {p.relative_to(code).as_posix() for p in code.rglob('*') if p.is_file()}
    if actual != set(inventory):
        _fail('EXTRA_OR_MISSING_CODE_FILE')
    # Do not admit a differently installed emitter from an original checkout.
    if Path(__file__).resolve() != (code / 'orchestration/official_native_delivery.py').resolve():
        _fail('RUN_COPIED_DELIVERY_ENTRY_REQUIRED')
    return anchor, code


def reproduce(bundle, anchor_file, trusted_sha256, *, label, output):
    """Actual source -> native graph -> checks in a ZIP, with explicit commit provenance."""
    started = time.monotonic()
    anchor, code = validate_anchor(bundle, anchor_file, trusted_sha256)
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(Path(bundle).resolve()):
        _fail('FRESH_OUTSIDE_BUNDLE_OUTPUT_REQUIRED')
    if label not in ('A', 'AB'):
        _fail('SUPPORTED_PRACTICE_LABEL_REQUIRED')
    sys.path.insert(0, str(code))  # documented copied-code entry, not PYTHONPATH/ROOT rewriting
    from builder.native_kit_emitter import build_official_native_workflow
    from builder.native_kit_verification import verify_local_candidate
    kit = code / KIT_REL
    briefs = [kit / 'briefs/brief-a.md'] + ([kit / 'briefs/brief-b.md'] if label == 'AB' else [])
    candidate = build_official_native_workflow(briefs, kit / 'tools/openapi.json', kit)
    verification = verify_local_candidate(candidate)
    body = {'name': 'Native-' + label + '-PUB01 ' + '0' * 36, 'workflow': candidate['workflow']}
    wire = json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
    if not verification['passed'] or len(wire) >= 131072:
        _fail('LOCAL_VERIFICATION_OR_PAYLOAD_FAILED')
    proof = {'schema_version': 'official-native-portable-local-build-1', 'passed': True,
        'practice_label': label, 'source_commit': anchor['source_commit'],
        'source_commit_authority': 'independently_pinned_reviewed_source_anchor; no Git in reproduction',
        'source_anchor_sha256': trusted_sha256, 'exact_working_source_sha256': anchor['source_sha256'],
        'input_inventory': candidate['input_inventory'], 'workflow_sha256': candidate['workflow_sha256'],
        'full_publication_body_bytes': len(wire), 'payload_limit_bytes': 131072,
        'planned_publication_only': True, 'bank_writes_authorized': False,
        'official_PUB01_PASS': False, 'provider_final_export': False, 'network_calls': 0,
        'local_checks': verification['checks'], 'brief_limit_seconds': 1800,
        'unsupported': candidate['readiness']['unsupported']}
    output.mkdir(parents=True)
    files = {'candidate.json': candidate, 'workflow.json': candidate['workflow'], 'local-trace.json': verification}
    for name, value in files.items():
        (output / name).write_bytes(_raw(value))
    # Validate pinned inputs again after executing/writing the graph. Provenance
    # checks count toward this brief's generation deadline too.
    validate_anchor(bundle, anchor_file, trusted_sha256)
    proof['elapsed_seconds'] = time.monotonic() - started
    if proof['elapsed_seconds'] > 1800:
        _fail('BRIEF_TIME_EXCEEDED')
    (output / 'build-proof.json').write_bytes(_raw(proof))
    if time.monotonic() - started > 1800:
        proof['passed'] = False
        proof['elapsed_seconds'] = time.monotonic() - started
        (output / 'build-proof.json').write_bytes(_raw(proof))
        _fail('BRIEF_TIME_EXCEEDED')
    return dict(proof, output=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('pack'); p.add_argument('--output', required=True); p.add_argument('--expected-commit', required=True)
    p = sub.add_parser('reproduce'); p.add_argument('--bundle', required=True); p.add_argument('--anchor', required=True)
    p.add_argument('--anchor-sha256', required=True); p.add_argument('--label', choices=('A', 'AB'), required=True)
    p.add_argument('--output', required=True)
    a = parser.parse_args()
    if a.command == 'pack':
        result = pack(a.output, expected_commit=a.expected_commit)
    else:
        result = reproduce(a.bundle, a.anchor, a.anchor_sha256, label=a.label, output=a.output)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
