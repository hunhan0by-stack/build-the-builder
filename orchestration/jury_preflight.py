"""Read-only clone/commit authentication of an existing offline delivery.

Prefix mode checks archived witnesses; explicit complete-native mode also
reconstructs and locally executes the exact source-bound export. Neither is
a Freya success or live bank authorization.
The manifest reference must come from the reviewer, outside the bundle.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import time

from builder.models import FoundationError, strict_json
from builder.native_safety_admission import assert_bank_write_surface_closed

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ('publish_allowed', 'publication_ready', 'final_submission_ready',
         'real_platform_success_proven')
PACK = 'code/artifacts/product-build/delivery-batch'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _name(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise FoundationError('JURY_PATH_INVALID')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('', '.', '..') for p in path.parts):
        raise FoundationError('JURY_PATH_INVALID')
    return value


def _git(clone, *args):
    result = subprocess.run(['git', '-c', 'safe.directory=' + clone.as_posix(), *args],
                            cwd=clone, capture_output=True, timeout=15)
    if result.returncode:
        raise FoundationError('JURY_COMMIT_UNAVAILABLE')
    return result.stdout


def _json(raw):
    return strict_json(raw.decode('utf-8'))


def _constant(raw, name):
    for node in ast.parse(raw.decode('utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise FoundationError('JURY_SOURCE_CONSTANT_MISSING')


def _closure(source, tracked):
    module = 'orchestration/native_product_delivery.py'
    pending = list(_constant(source['orchestration/product_build.py'], 'RUNTIME_FILES'))
    # Legacy prefix receipts predate the explicit dynamically loaded closure.
    # New committed product source must ship every declared official runtime.
    tree = ast.parse(source['orchestration/product_build.py'].decode('utf-8'))
    if any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OFFICIAL_RUNTIME'
           for t in node.targets) for node in tree.body):
        pending += list(_constant(source['orchestration/product_build.py'], 'OFFICIAL_RUNTIME'))
    pending += list(_constant(source[module], 'HOST_CONNECTION_FILES'))
    pending += list(_constant(source[module], 'BUILDER_ENTRY_FILES')) + [module]
    names = set()
    while pending:
        name = _name(pending.pop())
        if name in names:
            continue
        if name not in source:
            raise FoundationError('JURY_SOURCE_CLOSURE_MISSING')
        names.add(name)
        for parent in PurePosixPath(name).parents:
            init = str(parent / '__init__.py')
            if init in tracked and init not in names:
                pending.append(init)
        for node in ast.walk(ast.parse(source[name].decode('utf-8'))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = name.split('/')[:-1]
                    if node.level > len(parts):
                        raise FoundationError('JURY_IMPORT_ESCAPE')
                    modules = ['.'.join(parts[:len(parts) - node.level + 1] + (node.module or '').split('.')).strip('.')]
                elif node.module:
                    modules = [node.module]
            for module_name in modules:
                if module_name.split('.')[0] not in {'builder', 'compiler', 'orchestration'}:
                    continue
                candidate = module_name.replace('.', '/') + '.py'
                if candidate not in tracked:
                    candidate = module_name.replace('.', '/') + '/__init__.py'
                if candidate in tracked and candidate not in names:
                    pending.append(candidate)
    if names != set(source):
        raise FoundationError('JURY_SOURCE_CLOSURE_MISMATCH')


def _public_input_pins(contents, report, source, *, required):
    pins = report.get('public_input_sha256')
    if pins is None and not required:
        return 0  # Original prefix-only archives did not ship this input set.
    files = _constant(source['orchestration/product_build.py'], 'OFFICIAL_FILES')
    prefix = _constant(source['orchestration/native_product_delivery.py'], 'KIT_REL') + '/'
    expected = {prefix + name for name in (*files, 'briefs/brief-b.md', 'tools/openapi.json')}
    if not isinstance(pins, dict) or set(pins) != expected:
        raise FoundationError('JURY_PUBLIC_INPUT_INVENTORY_INVALID')
    for name, digest in pins.items():
        _name(name)
        if 'code/' + name not in contents or sha(contents['code/' + name]) != digest:
            raise FoundationError('JURY_PUBLIC_INPUT_PIN_MISMATCH')
    return len(pins)


def _native_scope_check(mode, workflow, api, readiness, directory):
    """Prefix must have no write surface; official mode must truly replay.

    Merely declaring official mode never bypasses canonical graph/source
    reconstruction, request-byte budget or the guarded local trace checks.
    """
    if mode == 'prefix':
        return assert_bank_write_surface_closed(workflow, api), 0
    if mode != 'official':
        raise FoundationError('JURY_NATIVE_MODE_INVALID')
    native = readiness.get('native_readiness', {})
    if (readiness.get('native_mode') != 'official'
            or readiness.get('native_export_scope') != 'complete_single_product_promise_branch'
            or any(readiness.get(flag) is not False for flag in
                   ('publish_allowed', 'publication_ready', 'final_submission_ready', 'live_enabled',
                    'official_PUB01_PASS', 'provider_final_export', 'native_playback_verified'))
            or any(native.get(flag) is not False for flag in
                   ('bank_writes_enabled', 'publish_allowed', 'native_atomic_persistence_claim',
                    'provider_idempotency_claim', 'automatic_restart_allowed'))
            or any(native.get(flag, False) is not False for flag in ('persistent_restart_proven', 'speech_completed'))):
        raise FoundationError('JURY_EXPORT_SCOPE_OR_WITNESS_INVALID')
    from orchestration.product_build import verify_product_build
    checked = verify_product_build(directory, deadline_monotonic=time.monotonic() + 1800)
    if (checked.get('valid') is not True or checked.get('publish_allowed') is not False
            or type(checked.get('critical_checks')) is not int or checked['critical_checks'] < 1):
        raise FoundationError('JURY_OFFICIAL_SOURCE_REPLAY_FAILED')
    return {'scope': 'Exact source-derived complete single-product native promise branch; synthetic local bank/completion only',
            'source_export_reconstruction_verified': True, 'actual_local_replay_verified': True,
            'no_live_write_authorization': True}, checked['critical_checks']


def _archived_bank_api(contents, proof, readiness):
    """Normalized API must match exactly one hash-bound raw source sidecar."""
    api = _json(contents[proof + '/api-document.json'])
    matches = 0
    for row in _json(contents[proof + '/source-bytes.json']):
        raw = base64.b64decode(row['base64'], validate=True)
        if (len(raw) != row['bytes'] or sha(raw) != row['sha256']
                or readiness['source_hashes'].get(row['path']) != row['sha256']):
            raise FoundationError('JURY_ARCHIVED_SOURCE_BYTES_INVALID')
        if raw.lstrip().startswith(b'{') and _json(raw) == api:
            matches += 1
    if matches != 1:
        raise FoundationError('JURY_BANK_API_SOURCE_BINDING_INVALID')
    return api


def preflight(bundle, *, clone, expected_commit, trusted_manifest_sha256):
    """Authenticate exact committed code; replay official-mode exports locally."""
    if not isinstance(expected_commit, str) or not re.fullmatch('[0-9a-f]{40}', expected_commit):
        raise FoundationError('JURY_EXPLICIT_COMMIT_REQUIRED')
    if not isinstance(trusted_manifest_sha256, str) or not re.fullmatch('[0-9a-f]{64}', trusted_manifest_sha256):
        raise FoundationError('JURY_EXTERNAL_MANIFEST_REQUIRED')
    explicit, clone = Path(bundle), Path(clone).resolve()
    target = explicit.resolve()
    if explicit.is_symlink() or not target.is_dir():
        raise FoundationError('JURY_BUNDLE_PATH_INVALID')
    if any(p.is_symlink() for p in target.rglob('*')):
        raise FoundationError('JURY_BUNDLE_LINK_FORBIDDEN')
    manifest_raw = (target / 'manifest.json').read_bytes()
    if sha(manifest_raw) != trusted_manifest_sha256:
        raise FoundationError('JURY_MANIFEST_REFERENCE_MISMATCH')
    manifest = _json(manifest_raw)
    rows = manifest['files']
    contents = {}
    for row in rows:
        name = _name(row['path'])
        if name in contents:
            raise FoundationError('JURY_DUPLICATE_FILE')
        raw = (target / name).read_bytes()
        if type(row['bytes']) is not int or len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise FoundationError('JURY_FILE_HASH_MISMATCH')
        contents[name] = raw
    actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
    if actual != set(contents) | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('JURY_INVENTORY_MISMATCH')
    final, report = _json((target / 'FINAL.json').read_bytes()), _json(contents['delivery-report.json'])
    if (manifest.get('version') != 'native-product-delivery-1'
            or report.get('version') != manifest['version']
            or any(item.get('commit_sha') != expected_commit for item in (final, report, manifest))
            or final.get('manifest_sha256') != trusted_manifest_sha256
            or report.get('status') not in ('LOCAL_NATIVE_PREFIX_DELIVERY', 'LOCAL_NATIVE_BRANCH_DELIVERY')
            or final.get('status') != report['status']
            or any(item.get(flag) is not False for item in (final, report) for flag in FLAGS)):
        raise FoundationError('JURY_OFFLINE_SCOPE_REQUIRED')
    if _git(clone, 'rev-parse', expected_commit + '^{commit}').decode().strip() != expected_commit:
        raise FoundationError('JURY_COMMIT_MISMATCH')
    tracked = set(_git(clone, 'ls-tree', '-r', '--name-only', expected_commit).decode().splitlines())
    source = {}
    for name, digest in report['source_sha256'].items():
        _name(name)
        raw = contents['code/' + name]
        if sha(raw) != digest or raw != _git(clone, 'show', expected_commit + ':' + name):
            raise FoundationError('JURY_COMMITTED_SOURCE_MISMATCH')
        source[name] = raw
    if {name[5:] for name in contents if name.startswith('code/') and name.endswith('.py')} != set(source):
        raise FoundationError('JURY_UNPINNED_CODE')
    _closure(source, tracked)
    agents = report['agents']
    if len(agents) != 2:
        raise FoundationError('JURY_TWO_EXPORTS_REQUIRED')
    result_agents = {}
    modes = {identifier: _json(contents[PACK + '/' + identifier + '/readiness.json']).get('native_mode', 'prefix')
             for identifier in agents}
    if any(mode not in ('prefix', 'official') for mode in modes.values()):
        raise FoundationError('JURY_NATIVE_MODE_INVALID')
    expected_status = 'LOCAL_NATIVE_BRANCH_DELIVERY' if 'official' in modes.values() else 'LOCAL_NATIVE_PREFIX_DELIVERY'
    if (report['status'] != expected_status or report.get('native_modes', modes) != modes
            or ('official' in modes.values() and report.get('native_modes') != modes)):
        raise FoundationError('JURY_NATIVE_SCOPE_STATUS_MISMATCH')
    public_count = _public_input_pins(contents, report, source, required='official' in modes.values())
    replay_count = 0
    for identifier, item in agents.items():
        _name(identifier)
        proof = PACK + '/' + identifier
        agent = 'agents/' + identifier + '.workflow.json'
        raw = contents[agent]
        readiness = _json(contents[proof + '/readiness.json'])
        mode = modes[identifier]
        safety, replayed = _native_scope_check(mode, _json(raw), _archived_bank_api(contents, proof, readiness),
                                              readiness, target / proof)
        trace_a, trace_b = contents[proof + '/critical-evidence.json'], contents[proof + '/serialized-export-evidence.json']
        if (item.get('file') != agent or item.get('proof') != proof
                or raw != contents[proof + '/agent.workflow.json']
                or sha(raw) != item.get('candidate_sha256')
                or sha(raw) != readiness.get('candidate_sha256')
                or len(raw) != readiness.get('actual_export_bytes') or len(raw) >= 131072
                or any(readiness.get(flag) is not False for flag in FLAGS[:3])
                or readiness.get('live_enabled') is not False
                or (mode == 'prefix' and any(readiness.get('native_readiness', {}).get(flag) is not False
                       for flag in ('bank_writes_enabled', 'persistent_restart_proven', 'speech_completed', 'publish_allowed')))
                or type(readiness.get('deadline_seconds')) is not int
                or not 1 <= readiness['deadline_seconds'] <= 1800
                or type(readiness.get('elapsed_seconds')) not in (int, float)
                or not 0 <= readiness['elapsed_seconds'] < readiness['deadline_seconds']
                or trace_a != trace_b or _json(trace_a).get('passed') is not True):
            raise FoundationError('JURY_EXPORT_SCOPE_OR_WITNESS_INVALID')
        result_agents[identifier] = {'bytes': len(raw), 'sha256': sha(raw), 'archived_trace_equal': True,
                                    'runtime_reexecuted': replayed > 0, 'checks_replayed': replayed,
                                    'native_mode': mode, 'native_bank_writes_enabled': False,
                                    'static_declared_api_inspection': safety}
        replay_count += replayed
    readme = contents['README.txt'].decode()
    for required in ('orchestration.build_the_builder', '--brief', '--tool-api',
                     '--out artifacts/unified-builder/v1/new-run', '--deadline-seconds 1800',
                     'Parameter/Wording', 'one product and one promise', 'unknown requirements fail'):
        if required not in readme:
            raise FoundationError('JURY_BUILDER_INSTRUCTIONS_INCOMPLETE')
    return {'status': 'LOCAL_JURY_PREFLIGHT_VERIFIED', 'valid': True, 'commit_sha': expected_commit,
            'git_commit_authenticated_in_clone': True, 'external_manifest_reference_matched': True,
            'files_checked': len(contents), 'committed_sources_checked': len(source), 'agents': result_agents,
            'checks_replayed': replay_count, 'public_inputs_checked': public_count,
            'network_calls': 0, 'credentials_read': False, 'git_writes': 0,
            'instructions': {'supported_input': 'bounded typed Parameter/Wording and supported amendments; single product/single promise',
                             'fresh_output_required': True, 'maximum_brief_seconds': 1800,
                             'runtime_replay_command_from_bundle_code': report['verify_command']},
            'scope': 'Exact committed offline delivery authenticated; official-mode complete branch locally replayed, prefix statically write-denied; no platform or PUB-01 outcome',
            **{flag: False for flag in FLAGS}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--clone', type=Path, default=ROOT)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--trusted-manifest-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        result = preflight(args.bundle, clone=args.clone, expected_commit=args.expected_commit,
                           trusted_manifest_sha256=args.trusted_manifest_sha256)
    except (FoundationError, OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as error:
        result = {'status': 'BLOCKED', 'valid': False,
                  'diagnostic': str(error) if isinstance(error, FoundationError) else type(error).__name__,
                  **{flag: False for flag in FLAGS}}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
