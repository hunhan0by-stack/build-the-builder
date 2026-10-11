"""One local source -> native graph -> checks -> candidate command.

This creates a candidate export, not a Freya-issued export or a platform PASS.
It neither opens a bank session nor publishes or runs a simulation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from builder.models import FoundationError
from builder.native_kit_emitter import build_official_native_workflow
from orchestration.product_build import ROOT
from orchestration.native_product_delivery import source_files


def _raw(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(',', ':')) + '\n').encode()


def build(briefs, api, kit, output):
    from builder.native_kit_verification import verify_local_candidate
    started = time.monotonic()
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / 'artifacts').resolve()) or output.exists():
        raise FoundationError('FRESH_WORKSPACE_ARTIFACT_OUTPUT_REQUIRED')
    candidate = build_official_native_workflow(briefs, api, kit)
    verification = verify_local_candidate(candidate)
    if not verification['passed']:
        raise FoundationError('GENERATED_NATIVE_CANDIDATE_FAILED_LOCAL_VERIFICATION')
    # This matches NativeWireTransport's actual JSON serialization. A later
    # live controller must measure its exact body too, with its real run ID.
    body = {'name': 'Native-A-PUB01 00000000-0000-0000-0000-000000000000', 'workflow': candidate['workflow']}
    actual = json.dumps(body, allow_nan=False).encode()
    if len(actual) >= 131072:
        raise FoundationError('FULL_PUBLICATION_BODY_EXCEEDS_131072_BYTES')
    code_files = tuple(dict.fromkeys((*source_files(),
        'builder/native_kit_emitter.py', 'builder/native_kit_verification.py', 'builder/native_write_guard.py',
        'builder/native_official_closure.py', 'orchestration/official_native_build.py',
        'orchestration/final_run_pairing.py')))
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in code_files}
    commit = subprocess.check_output(['git', '-c', 'safe.directory=' + str(ROOT).replace('\\', '/'), 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    elapsed = time.monotonic() - started
    if elapsed > 1800:
        raise FoundationError('OFFICIAL_BRIEF_BUILD_EXCEEDED_30_MINUTES')
    proof = {'schema_version': 'official-native-local-build-1', 'source_commit': commit,
        'exact_working_source_sha256': sources, 'commit_coverage_asserted': False,
        'command': 'python -m orchestration.official_native_build --brief [explicit source] --tool-api [explicit API] --kit [official kit] --output [fresh output]',
        'input_inventory': candidate['input_inventory'], 'workflow_sha256': candidate['workflow_sha256'],
        'full_publication_body_bytes': len(actual), 'publication_name_uuid_is_size_fixture_only': True,
        'serialization': 'NativeWireTransport json.dumps(body, allow_nan=False).encode(), UTF-8',
        'limit_bytes': 131072, 'elapsed_seconds': elapsed, 'brief_limit_seconds': 1800,
        'local_checks': verification['checks'], 'passed': True, 'network_calls': 0,
        'platform_PASS': False, 'official_PUB01_PASS': False, 'final_export': False,
        'bank_writes_authorized': False,
        'unsupported': candidate['readiness']['unsupported']}
    output.mkdir(parents=True)
    files = {'candidate.json': candidate, 'workflow.json': candidate['workflow'], 'local-trace.json': verification,
             'build-proof.json': proof}
    for name, value in files.items():
        (output / name).write_bytes(_raw(value))
    manifest = {'schema_version': 'native-build-manifest-1', 'entries': [
        {'path': name, 'bytes': (output / name).stat().st_size, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()}
        for name in sorted(files)], 'source_sha256': sources,
        'trust': 'local checksum only; independent archived anchor required for integrity attestation'}
    raw = _raw(manifest)
    (output / 'manifest.json').write_bytes(raw)
    return dict(proof, manifest_sha256=hashlib.sha256(raw).hexdigest(), output=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brief', action='append', required=True)
    parser.add_argument('--tool-api', required=True)
    parser.add_argument('--kit', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = build(args.brief, args.tool_api, args.kit, args.output)
    print(json.dumps({key: result[key] for key in ('passed', 'source_commit', 'elapsed_seconds',
        'full_publication_body_bytes', 'network_calls', 'official_PUB01_PASS', 'manifest_sha256', 'output')}, indent=2))


if __name__ == '__main__':
    main()
