"""Read-only contest prerequisites: never run Codex, authenticate, or call Freya.

A CLI file and credential environment-name presence are not authentication or
model availability. Even an accepted local check leaves these NOT_MEASURED.
Only the explicitly supplied public inputs/source closure are read. Git is used
solely for fixed read-only rev-parse/cat-file commands, never login or mutations.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time

KIT = 'data/freya-hackathon/kit-v3/build-the-builder'
KIT_FILES = ('freya.py', 'VERSION', 'README.md', 'docs/api.md',
             'docs/workflow-format.md', 'docs/tools-and-code.md',
             'examples/base-agent.json', 'tools/openapi.json', 'tools/score.py',
             'briefs/brief-a.md', 'briefs/brief-b.md', 'public-test-set.json')
ENTRY_FILES = ('builder/build.py', 'builder/model_workflow.py',
               'orchestration/contest_build.py', 'orchestration/contest_practice.py',
               'orchestration/prepare_contest_checkout.py',
               'orchestration/workspace_lock.py', 'CONTEST-RUN.md', '.gitignore')
EVIDENCE_FILES = ('__init__.py', 'contest_evidence.py', 'native_pilot.py', 'pilot.py',
                  'preflight.py', 'private_state.py', 'response_diagnostics.py',
                  'sanitize.py', 'session_guard.py', 'transport.py', 'bank_transport.py',
                  'failure_learning.py')
PUBLIC_REPLAY_FILES = frozenset((
    'tests/__init__.py', 'tests/helpers.py', 'tests/conversation_helpers.py',
    'tests/test_public_replays.py',
))
PUBLIC_LOCAL_VALIDATION_FILES = frozenset((
    'project-management/local-evidence/pub01_behavior.py',
    'project-management/local-evidence/validate_pub01_v2.py',
    'project-management/local-evidence/validate_evidence.py',
    'project-management/local-evidence/contract-v1.json',
    'project-management/local-evidence/contract-v2.json',
))
# Archived regression inputs may appear only with their original byte pins.
# Their inclusion does not measure a new hidden-brief candidate.
HISTORICAL_GRAPH_PINS = {
    'artifacts/final-builder/model-current-medium-changed-v13-20261011/agent.workflow.json':
        '8aa4257f744fe105c9d38b50908e94c457410b720ef9f35fa580c9cc502c2ae9',
    'project-management/p0-dialogue-tests-20261011/candidate/agent.workflow.json':
        '53f8becda3704d84dba4dce5352dba2f2d5cc061a7b6c4ec1ba3f3fa6577086b',
}
PROTECTED = {'.git', '.codex', '.agents', '.aws', '.freya-local'}
HEX = re.compile(r'^[0-9a-f]{64}$')
COMMIT = re.compile(r'^[0-9a-f]{40}$')
MAX_BYTES = 4_000_000


class PreflightError(ValueError):
    pass


def sha(content):
    return hashlib.sha256(content).hexdigest()


def strict_json(content):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise PreflightError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def constant(unused):
        raise PreflightError('NONFINITE_JSON_NUMBER')
    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


def public_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        raise PreflightError('PUBLIC_SOURCE_PATH_INVALID')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or
            any(part in ('', '.', '..') for part in path.parts)):
        raise PreflightError('PUBLIC_SOURCE_PATH_INVALID')
    if value in ('CONTEST-RUN.md', 'README.md', '.gitignore', '.gitattributes',
                 'pyproject.toml', 'requirements-evidence-protection.txt',
                 'requirements-case-reader.txt',
                 'tests/test_portable_protection.py', 'tests/__init__.py'):
        return value
    if value == 'tests_integration/test_contest_evidence.py':
        return value
    if (value in PUBLIC_REPLAY_FILES or value in PUBLIC_LOCAL_VALIDATION_FILES or
            value in HISTORICAL_GRAPH_PINS):
        return value
    if value.startswith(KIT + '/') and value[len(KIT) + 1:] in KIT_FILES:
        return value
    if value.startswith('integration/') and value[12:] in EVIDENCE_FILES:
        return value
    if len(path.parts) == 2 and path.parts[0] in ('builder', 'compiler', 'orchestration') and path.suffix == '.py':
        return value
    if (path.parts[0] in ('tests_orchestration', 'tests_compiler') and
            (len(path.parts) == 2 and path.suffix == '.py' or
             len(path.parts) >= 3 and path.parts[1] == 'inputs' and
             path.suffix in ('.md', '.json', '.txt', '.csv', '.eml', '.html', '.vtt'))):
        return value
    raise PreflightError('PUBLIC_SOURCE_PATH_NOT_ALLOWLISTED')


def readable_file(path):
    supplied = Path(path).absolute()
    for candidate in (supplied, supplied.resolve()):
        parts = {part.lower() for part in candidate.parts}
        name = candidate.name.lower()
        if (parts & PROTECTED or name.startswith('.env') or
                re.search(r'(?:^|[._-])(?:api[-_]?key|secret|credential|token)(?:[._-]|$)', name)):
            raise PreflightError('PRIVATE_SOURCE_REFUSED')
    # Refuse junction/reparse ancestors as well as leaf symlinks before reading.
    for candidate in (supplied, *supplied.parents):
        try:
            info = candidate.lstat()
        except OSError:
            raise PreflightError('PUBLIC_FILE_UNAVAILABLE') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PreflightError('PUBLIC_REPARSE_PATH_REFUSED')
    if not supplied.is_file() or supplied.stat().st_size > MAX_BYTES:
        raise PreflightError('PUBLIC_FILE_UNAVAILABLE_OR_TOO_LARGE')
    content = supplied.read_bytes()
    if len(content) > MAX_BYTES:
        raise PreflightError('PUBLIC_FILE_CHANGED_OR_TOO_LARGE')
    return content


def source_inventory(checkout, include_evidence=False):
    names = set(ENTRY_FILES)
    for package in ('builder', 'compiler', 'orchestration'):
        names.update(path.relative_to(checkout).as_posix()
                     for path in (checkout / package).glob('*.py'))
    names.update(KIT + '/' + name for name in KIT_FILES)
    # contest_build snapshots this source on every run, including local-only
    # generation. Protection selection never removes runtime source from pins.
    names.update('integration/' + name for name in EVIDENCE_FILES)
    return sorted(names)


def trace_only_option_declared(checkout):
    """Literal source declaration only; never execute the runner or certify it."""
    try:
        syntax = ast.parse(readable_file(
            Path(checkout) / 'orchestration/contest_build.py').decode('utf-8-sig'))
        return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'add_argument' and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == '--evidence-mode'
            and any(keyword.arg == 'choices' and isinstance(keyword.value, (ast.Tuple, ast.List))
                and {'protected', 'trace-only'} <= {item.value for item in keyword.value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                for keyword in node.keywords) for node in ast.walk(syntax))
    except (PreflightError, OSError, UnicodeError, SyntaxError):
        return False


def fernet_dependency_available():
    """Module availability only; no key read, encryption, or decoder invocation."""
    try:
        return importlib.util.find_spec('cryptography.fernet') is not None
    except (ImportError, OSError, ValueError, AttributeError):
        return False


def git_sources(repository, commit, names):
    """One batched blob read; no config/auth files or arbitrary git arguments."""
    command = ['git', '-c', 'safe.directory=' + repository.as_posix()]
    try:
        resolved = subprocess.run(command + ['rev-parse', '--verify', commit + '^{commit}'],
            cwd=repository, capture_output=True, timeout=15, check=False)
        if resolved.returncode or resolved.stdout.decode('ascii').strip() != commit:
            raise PreflightError('SOURCE_COMMIT_UNAVAILABLE')
        data = ''.join(commit + ':' + name + '\n' for name in names).encode('utf-8')
        process = subprocess.run(command + ['cat-file', '--batch'], cwd=repository,
            input=data, capture_output=True, timeout=30, check=False)
        if process.returncode:
            raise PreflightError('SOURCE_COMMIT_BLOB_READ_FAILED')
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise PreflightError('SOURCE_COMMIT_READ_UNAVAILABLE') from None
    output = process.stdout
    cursor, found, missing = 0, {}, []
    for name in names:
        end = output.find(b'\n', cursor)
        if end < cursor:
            raise PreflightError('SOURCE_COMMIT_BATCH_INVALID')
        header = output[cursor:end].split()
        cursor = end + 1
        if header[-1:] == [b'missing']:
            missing.append(name)
            continue
        if len(header) != 3 or header[1] != b'blob' or not header[2].isdigit():
            raise PreflightError('SOURCE_COMMIT_BATCH_INVALID')
        size = int(header[2])
        if size > MAX_BYTES or cursor + size >= len(output) or output[cursor + size:cursor + size + 1] != b'\n':
            raise PreflightError('SOURCE_COMMIT_BATCH_INVALID')
        found[name] = sha(output[cursor:cursor + size])
        cursor += size + 1
    if cursor != len(output):
        raise PreflightError('SOURCE_COMMIT_BATCH_INVALID')
    return found, missing


def inspect(*, checkout, brief_path, tool_api_path, expected_commit,
            source_git=None, source_manifest=None, expected_manifest_sha256=None,
            provider='codex', deadline_seconds=1800, executable=None,
            publish=False, practice=False, include_evidence=False, evidence_mode='protected',
            git_reader=git_sources,
            executable_resolver=shutil.which):
    started = time.monotonic()
    checkout = Path(checkout).absolute()
    checks, blocked = {}, []
    report = {'kind': 'contest-read-only-preflight-1', 'local_preflight_passed': False,
        'evidence_mode': evidence_mode, 'publication_only': evidence_mode == 'trace-only',
        'authentication': 'NOT_MEASURED', 'model_availability': 'NOT_MEASURED',
        'codex_cli_flag_compatibility': 'NOT_MEASURED',
        'live_success_verified': False, 'final_submission_ready': False,
        'model_calls': 0, 'freya_calls': 0, 'bank_calls': 0,
        'login_attempts': 0, 'source_mutations': 0, 'checks': checks,
        'external_prerequisites': [
            'For --provider codex, jury machine needs its own authorized Codex CLI session/model connection; no credential file is copied or inspected.',
            'CLI exec flag compatibility, authentication, model access and a real generation must be measured separately; file existence is insufficient.',
            'For --publish, authorized Freya connection and supported workspace are required; for public practice, an authorized bank team connection is additionally required.',
            'Jury must obtain the selected source commit and an independent manifest hash; a local Git object is not proof of organizer repository access.',
            'Run builder/build.py with exactly the supplied brief/API paths; generation, repair, publication and tests must share the configured 1800-second-or-less budget.',
            'No check here starts publication, a customer, a simulation, or a bank operation.']}
    def refuse(code):
        if code not in blocked:
            blocked.append(code)
    if evidence_mode not in ('protected', 'trace-only'):
        refuse('EVIDENCE_MODE_INVALID')
    if evidence_mode == 'trace-only':
        option_declared = trace_only_option_declared(checkout)
        checks['trace_only_runner'] = {'option_declared_in_source': option_declared,
            'runtime_behavior_verified': False, 'dependency_owner': 'contest_build owner'}
        if not option_declared:
            refuse('TRACE_ONLY_RUNNER_OPTION_UNAVAILABLE')
        if not publish or practice:
            refuse('TRACE_ONLY_PUBLICATION_ONLY')
        if include_evidence:
            refuse('TRACE_ONLY_PROTECTED_EVIDENCE_CONFLICT')
        report['external_prerequisites'].append(
            'Trace-only requires a separately integrated runner option; literal source declaration does not prove its runtime behavior. Complete wire, conversation, bank ledger and official evaluation remain unmeasured.')
    protected_required = bool(include_evidence or (publish and evidence_mode == 'protected'))
    report['evidence'] = {'mode': evidence_mode,
        'scope': 'PUBLICATION_EXPORT_ONLY' if evidence_mode == 'trace-only' else 'PROTECTED_CAPTURE_ON_PUBLICATION',
        'full_wire_request_response': 'NOT_CAPTURED_BY_MODE' if evidence_mode == 'trace-only' else 'NOT_MEASURED',
        'conversation': 'NOT_MEASURED', 'bank_ledger': 'NOT_MEASURED',
        'official_evaluation': 'NOT_MEASURED'}
    selected_backend = os.environ.get('FREYA_EVIDENCE_PROTECTION', 'dpapi')
    checks['protected_evidence'] = {'required': protected_required,
        'default_protector': 'Windows current-user DPAPI',
        'selected_backend': selected_backend if selected_backend in ('dpapi', 'fernet') else 'INVALID',
        'host_platform_compatible': os.name == 'nt' if selected_backend == 'dpapi' else selected_backend == 'fernet',
        'encryption_probe': 'NOT_MEASURED', 'key_validity': 'NOT_MEASURED',
        'dependency_import_verified': False,
        'cross_machine_decryption_supported': False,
        'transport_hook_integration_verified': False}
    if protected_required:
        report['external_prerequisites'].append(
            'Protected mode uses default Windows DPAPI or explicit Fernet with an optional dependency and external host-only key. Preflight checks key presence and module availability only, never reads or probes a key. The recorder validates configuration and fresh protection before platform dispatch. Historical DPAPI recovery remains Windows-profile-bound.')
        if selected_backend == 'dpapi':
            if os.name != 'nt':
                refuse('DEFAULT_EVIDENCE_PROTECTOR_REQUIRES_WINDOWS')
        elif selected_backend == 'fernet':
            key_present = 'FREYA_EVIDENCE_KEY' in os.environ
            dependency_present = fernet_dependency_available()
            checks['protected_evidence'].update(fernet_key_present=key_present,
                optional_dependency_available=dependency_present)
            if not key_present:
                refuse('FERNET_EVIDENCE_KEY_PREREQUISITE_MISSING')
            if not dependency_present:
                refuse('FERNET_EVIDENCE_DEPENDENCY_UNAVAILABLE')
        else:
            refuse('EVIDENCE_PROTECTION_MODE_INVALID')
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        refuse('DEADLINE_MUST_BE_1_TO_1800_SECONDS')
    checks['deadline'] = {'configured_seconds': deadline_seconds,
        'accepted_limit': type(deadline_seconds) is int and 1 <= deadline_seconds <= 1800,
        'actual_builder_total_measured': False}
    if provider not in ('bounded', 'codex'):
        refuse('PROVIDER_UNSUPPORTED')
    if practice and not publish:
        refuse('PRACTICE_REQUIRES_PUBLICATION')
    checks['input_files'] = {}
    for label, path, extension in (('brief', brief_path, '.md'), ('tool_api', tool_api_path, '.json')):
        try:
            if Path(path).suffix.lower() != extension:
                raise PreflightError('CONTEST_INPUT_EXTENSION_INVALID')
            content = readable_file(path)
            text = content.decode('utf-8-sig')
            if not text.strip() or '\x00' in text:
                raise PreflightError('CONTEST_INPUT_EMPTY_OR_NUL')
            if label == 'tool_api':
                api = strict_json(text)
                if not isinstance(api, dict) or 'openapi' not in api or not isinstance(api.get('paths'), dict):
                    raise PreflightError('CONTEST_OPENAPI_DOCUMENT_REQUIRED')
            checks['input_files'][label] = {'sha256': sha(content), 'bytes': len(content), 'readable': True}
        except (PreflightError, ValueError, UnicodeError, OSError) as error:
            code = str(error) if type(error) is PreflightError else 'CONTEST_INPUT_READ_FAILED'
            checks['input_files'][label] = {'readable': False, 'diagnostic': code}
            refuse(code)
    checks['python'] = {'version': list(sys.version_info[:3]), 'minimum': [3, 10],
                        'available': sys.version_info >= (3, 10)}
    if not checks['python']['available']:
        refuse('PYTHON_310_REQUIRED')
    chosen = executable or os.environ.get('BUILDER_CODEX_EXECUTABLE') or executable_resolver('codex')
    present = bool(chosen and Path(chosen).is_file())
    checks['codex_executable'] = {'file_present': present, 'executed': False,
        'required': provider == 'codex', 'authentication_verified': False}
    if provider == 'codex' and not present:
        refuse('CODEX_EXECUTABLE_NOT_FOUND')
    # Names/presence only. Values are neither emitted nor used for a request.
    checks['connection_environment'] = {'FREYA_API_KEY_present': 'FREYA_API_KEY' in os.environ,
        'FREYA_BANK_TEAM_KEY_present': 'FREYA_BANK_TEAM_KEY' in os.environ,
        'connection_verified': False}
    if publish and 'FREYA_API_KEY' not in os.environ:
        refuse('FREYA_ENVIRONMENT_PREREQUISITE_MISSING')
    if practice and 'FREYA_BANK_TEAM_KEY' not in os.environ:
        refuse('BANK_ENVIRONMENT_PREREQUISITE_MISSING')
    contents = {}
    closure = source_inventory(checkout, include_evidence)
    source_map = {}
    checks['source'] = {'expected_commit': expected_commit, 'git_read_only': True,
        'runtime_file_count': len(closure), 'workspace_sha256': contents,
        'manifest_independently_pinned': False, 'organizer_repository_access_verified': False}
    manifest = None
    source_integrity_checked = False
    try:
        if len(closure) > 512:
            raise PreflightError('PUBLIC_SOURCE_INVENTORY_TOO_LARGE')
        if source_manifest is not None:
            if not isinstance(expected_manifest_sha256, str) or not HEX.fullmatch(expected_manifest_sha256):
                raise PreflightError('EXTERNAL_MANIFEST_PIN_REQUIRED')
            encoded = readable_file(source_manifest)
            if sha(encoded) != expected_manifest_sha256:
                raise PreflightError('SOURCE_MANIFEST_HASH_MISMATCH')
            manifest = strict_json(encoded.decode('utf-8'))
            if not isinstance(manifest, dict) or not manifest or len(manifest) > 512:
                raise PreflightError('SOURCE_MANIFEST_INVALID')
            for name, fingerprint in manifest.items():
                public_name(name)
                if not isinstance(fingerprint, str) or not HEX.fullmatch(fingerprint):
                    raise PreflightError('SOURCE_MANIFEST_INVALID')
                if name in HISTORICAL_GRAPH_PINS and fingerprint != HISTORICAL_GRAPH_PINS[name]:
                    raise PreflightError('HISTORICAL_GRAPH_PIN_MISMATCH')
            if not set(closure) <= set(manifest):
                raise PreflightError('SOURCE_MANIFEST_RUNTIME_CLOSURE_MISSING')
            closure = sorted(manifest)
            checks['source']['manifest_independently_pinned'] = True
            checks['source']['manifest_sha256'] = sha(encoded)
        elif expected_manifest_sha256 is not None:
            raise PreflightError('SOURCE_MANIFEST_REQUIRED')
        for name in closure:
            public_name(name)
            contents[name] = sha(readable_file(checkout / name))
        if manifest is not None and contents != manifest:
            raise PreflightError('SOURCE_MANIFEST_FILE_MISMATCH')
        if manifest is not None and 'README.md' in contents:
            # prepare_contest_checkout explicitly copies CONTEST-RUN -> README.
            # Verify that exact derivation; never compare this with root README
            # or rewrite the archived bytes/path to make them appear equal.
            if contents['README.md'] != contents.get('CONTEST-RUN.md'):
                raise PreflightError('CHECKOUT_README_DERIVATION_MISMATCH')
            source_map['README.md'] = 'CONTEST-RUN.md'
        checks['source']['derived_source_map'] = source_map
        source_integrity_checked = True
    except (PreflightError, ValueError, UnicodeError, OSError) as error:
        refuse(str(error) if type(error) is PreflightError else 'PUBLIC_SOURCE_READ_FAILED')
    if not isinstance(expected_commit, str) or not COMMIT.fullmatch(expected_commit):
        refuse('FULL_SOURCE_COMMIT_REQUIRED')
    elif source_integrity_checked and len(contents) == len(closure):
        try:
            source_names = sorted({source_map.get(name, name) for name in closure})
            committed, source_missing = git_reader(Path(source_git or checkout).absolute(), expected_commit, source_names)
            missing = [name for name in closure if source_map.get(name, name) in source_missing]
            mismatches = [name for name in closure if source_map.get(name, name) in committed and
                          committed[source_map.get(name, name)] != contents[name]]
            checks['source'].update({'git_missing_files': missing, 'git_byte_mismatch_files': mismatches,
                'commit_byte_matches': len(closure) - len(missing) - len(mismatches)})
            if missing:
                refuse('SOURCE_FILES_NOT_IN_SELECTED_COMMIT')
            if mismatches:
                refuse('SOURCE_FILES_DIFFER_FROM_SELECTED_COMMIT')
        except PreflightError as error:
            refuse(str(error))
    if source_integrity_checked:
        try:
            if not set(source_inventory(checkout, include_evidence)) <= set(closure) or any(
                    sha(readable_file(checkout / name)) != fingerprint for name, fingerprint in contents.items()):
                raise PreflightError('PUBLIC_SOURCE_CHANGED_DURING_PREFLIGHT')
            checks['source']['source_stable_during_preflight'] = True
        except (PreflightError, OSError) as error:
            checks['source']['source_stable_during_preflight'] = False
            refuse(str(error) if type(error) is PreflightError else 'PUBLIC_SOURCE_READ_FAILED')
    report.update(status='BLOCKED' if blocked else
        'LOCAL_PUBLICATION_ONLY_PRECONDITIONS_VERIFIED_EXTERNAL_AUTH_REQUIRED'
        if evidence_mode == 'trace-only' else 'LOCAL_PRECONDITIONS_VERIFIED_EXTERNAL_AUTH_REQUIRED',
        local_preflight_passed=not blocked, blockers=blocked,
        elapsed_seconds=round(time.monotonic() - started, 6))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('brief_path', type=Path)
    parser.add_argument('tool_api_path', type=Path)
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source-git', type=Path)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--source-manifest', type=Path)
    parser.add_argument('--expected-manifest-sha256')
    parser.add_argument('--provider', choices=('bounded', 'codex'), default='codex')
    parser.add_argument('--deadline-seconds', type=int, default=1800)
    parser.add_argument('--codex-executable')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--evidence-mode', choices=('protected', 'trace-only'), default='protected',
                        help='Protected checks default DPAPI or explicit Fernet prerequisites; trace-only is refused unless the runner declares the separate option. Source declaration never proves runtime behavior.')
    parser.add_argument('--practice', action='store_true')
    parser.add_argument('--protected-evidence', action='store_true',
                        help='Check protected backend host prerequisites; key presence only, no key read, encryption probe, or DPAPI invocation.')
    args = parser.parse_args(argv)
    result = inspect(checkout=args.checkout, brief_path=args.brief_path, tool_api_path=args.tool_api_path,
        source_git=args.source_git, expected_commit=args.expected_commit,
        source_manifest=args.source_manifest, expected_manifest_sha256=args.expected_manifest_sha256,
        provider=args.provider, deadline_seconds=args.deadline_seconds, executable=args.codex_executable,
        publish=args.publish, practice=args.practice, include_evidence=args.protected_evidence,
        evidence_mode=args.evidence_mode)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 2 if result['status'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
