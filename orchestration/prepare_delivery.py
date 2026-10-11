"""Offline, immutable delivery bundle: two practice JSON exports and matched proof.

This assembles reviewed local deliverables, never publishes or asserts live Freya
success. A historical test baseline is explicitly separate from fresh checks.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from builder.export_pipeline import CORE_FILES, _verify_package, verify_export_run
from builder.models import FoundationError, strict_json

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/delivery'
DEFAULT_A = ROOT / 'artifacts/export-pipeline/practice-a-bytebudget-v1'
DEFAULT_B = ROOT / 'artifacts/export-pipeline/practice-b-bytebudget-v1'
BASELINE = ROOT / 'artifacts/product-readiness/acceptance-generic-v1.json'
NATIVE_FIXTURE = 'artifacts/generic-cycle/milestone-normal-v1'
NATIVE_FIXTURE_FILES = ('workflow.json', 'script.json', 'manifest.json', 'FINAL.json')
NATIVE_REQUIRED_CODE = {'orchestration/native_candidate.py', 'builder/native_freya.py',
                        'builder/platform_receipts.py', 'orchestration/prepare_delivery.py'}
PUBLIC_KIT = 'data/freya-hackathon/kit-v3/build-the-builder/'
PUBLIC_FILES = tuple(PUBLIC_KIT + p for p in (
    'README.md', 'VERSION', 'briefs/brief-a.md', 'briefs/brief-b.md',
    'tools/openapi.json', 'public-test-set.json', 'docs/workflow-format.md',
    'docs/tools-and-code.md', 'docs/api.md', 'examples/base-agent.json'))
CODE_DIRECTORIES = {'builder', 'compiler', 'orchestration', 'tests', 'tests_compiler', 'tests_orchestration'}
SECRET_KEYS = {'apikey', 'secret', 'password', 'authorization', 'accesstoken',
               'refreshtoken', 'credentials', 'rawreceipt', 'publishedagentid',
               'agentid', 'simulationid'}
VALUE_MARKERS = (
    re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'),
    re.compile(r'\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}'),
    re.compile(r'\bBearer\s+(?!YOUR|PLACEHOLDER|\{|\$)[A-Za-z0-9_.-]{20,}', re.I),
    re.compile(r'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'),
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8') + b'\n'


def relative(name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise FoundationError('DELIVERY_UNSAFE_PATH')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in name.split('/')):
        raise FoundationError('DELIVERY_UNSAFE_PATH')
    if any(p.lower() in {'.git', '.aws', '.codex', '.agents', '.freya-local'}
           or p.lower().startswith('.env') for p in path.parts):
        raise FoundationError('DELIVERY_PROTECTED_PATH')
    return name


def scan_public(raw, suffix):
    """Reject credential value markers without ever echoing their values."""
    text = raw.decode('utf-8')
    if any(pattern.search(text) for pattern in VALUE_MARKERS):
        raise FoundationError('DELIVERY_SENSITIVE_VALUE_REFUSED')
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                key_normalized = re.sub(r'[^a-z]', '', key.lower())
                if key_normalized in SECRET_KEYS and item not in (None, False, '', 'not_read', 'unavailable'):
                    # Schema definitions are descriptive, never credentials.
                    if not isinstance(item, dict) or not set(item) <= {'type', 'description', 'example', 'required'}:
                        raise FoundationError('DELIVERY_SENSITIVE_KEY_REFUSED')
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    if suffix == '.json':
        walk(strict_json(text))
    elif suffix == '.jsonl':
        for line in text.splitlines():
            if line:
                walk(strict_json(line))


def git_bytes(*args):
    process = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, timeout=15)
    if process.returncode:
        raise FoundationError('DELIVERY_COMMIT_UNAVAILABLE')
    return process.stdout


def resolve_commit(commit):
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise FoundationError('DELIVERY_EXACT_COMMIT_REQUIRED')
    if git_bytes('rev-parse', 'HEAD').decode().strip() != commit:
        raise FoundationError('DELIVERY_COMMIT_HEAD_MISMATCH')
    if git_bytes('rev-parse', commit + '^{commit}').decode().strip() != commit:
        raise FoundationError('DELIVERY_COMMIT_UNAVAILABLE')
    return commit


def admitted_code(commit):
    tracked = git_bytes('ls-tree', '-r', '--name-only', commit).decode().splitlines()
    return sorted(name for name in tracked if PurePosixPath(name).parent.as_posix() in CODE_DIRECTORIES
                  and name.endswith('.py'))


def fixture_anchor(native_cli_raw):
    tree = ast.parse(native_cli_raw.decode('utf-8'))
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'SOURCE_MANIFEST_SHA256'
                      for target in node.targets) and isinstance(node.value, ast.Constant)]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(r'[0-9a-f]{64}', values[0]):
        raise FoundationError('DELIVERY_NATIVE_ANCHOR_UNAVAILABLE')
    return values[0]


def verify_native_fixture(directory, anchor):
    root = Path(directory)
    manifest_raw = (root / 'manifest.json').read_bytes()
    final = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
    if (sha(manifest_raw) != anchor or final.get('manifest_sha256') != anchor
            or final.get('status') != 'LOCAL_GENERIC_WORKFLOW_PASS'
            or final.get('deadline_seconds') != 1800
            or type(final.get('elapsed_seconds')) not in (int, float)
            or not 0 <= final['elapsed_seconds'] < 1800
            or (root / 'FAILED_FINALIZATION.json').exists()):
        raise FoundationError('DELIVERY_NATIVE_FIXTURE_ANCHOR_MISMATCH')
    rows = strict_json(manifest_raw.decode())
    entries = {row['path']: row for row in rows}
    if len(entries) != len(rows):
        raise FoundationError('DELIVERY_NATIVE_FIXTURE_DUPLICATE_ENTRY')
    for name in ('workflow.json', 'script.json'):
        raw = (root / name).read_bytes()
        if name not in entries or sha(raw) != entries[name]['sha256'] or len(raw) != entries[name]['bytes']:
            raise FoundationError('DELIVERY_NATIVE_FIXTURE_INPUT_CHANGED')
        scan_public(raw, '.json')
    # This is only a pinned source slice. It never re-attests the full historical
    # generic package whose manifest also names intentionally excluded files.
    return anchor


def prepare_delivery(*, output_directory, commit_sha, candidate_a=DEFAULT_A,
                     candidate_b=DEFAULT_B, acceptance_path=BASELINE):
    commit = resolve_commit(commit_sha)
    target = Path(output_directory).resolve()
    if target == OUTPUT.resolve() or not target.is_relative_to(OUTPUT.resolve()) or target.exists():
        raise FoundationError('DELIVERY_FRESH_CHILD_REQUIRED')
    sources = {'A': Path(candidate_a).resolve(), 'B': Path(candidate_b).resolve()}
    checks = {}
    for label, directory in sources.items():
        checked = verify_export_run(directory)
        if checked.get('status') != 'verified' or checked.get('mode') != 'practice':
            raise FoundationError('DELIVERY_CANDIDATE_PROOF_BLOCKED_' + label)
        checks[label] = checked
        evidence = strict_json((directory / 'runtime-evidence.json').read_text(encoding='utf-8'))
        brief_names = {Path(doc['path']).name for doc in evidence['documents']}
        if 'brief-a.md' not in brief_names or ('brief-b.md' in brief_names) != (label == 'B'):
            raise FoundationError('DELIVERY_PRACTICE_PROFILE_MISMATCH_' + label)
        for name in CORE_FILES:
            snapshot = (directory / 'implementation' / name).read_bytes()
            committed = git_bytes('show', commit + ':builder/' + name)
            if snapshot.replace(b'\r\n', b'\n') != committed.replace(b'\r\n', b'\n'):
                raise FoundationError('DELIVERY_CANDIDATE_COMMIT_CODE_MISMATCH_' + label)
    acceptance = strict_json(Path(acceptance_path).read_text(encoding='utf-8'))
    if any(acceptance.get(key) != value for key, value in
           {'tests_run': 416, 'failures': 0, 'errors': 0, 'network_attempts': 0}.items()):
        raise FoundationError('DELIVERY_HISTORICAL_BASELINE_INVALID')
    pending = {}
    def add(name, raw):
        relative(name)
        scan_public(raw, PurePosixPath(name).suffix)
        if name in pending:
            raise FoundationError('DELIVERY_DUPLICATE_ENTRY')
        pending[name] = raw
    code = admitted_code(commit)
    if not NATIVE_REQUIRED_CODE <= set(code):
        raise FoundationError('DELIVERY_NATIVE_CODE_NOT_COMMITTED')
    for name in code:
        add('code/' + name, git_bytes('show', commit + ':' + name))
    for name in PUBLIC_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT / PUBLIC_KIT):
            raise FoundationError('DELIVERY_PUBLIC_SOURCE_ESCAPE')
        add('code/' + name, path.read_bytes())
    anchor = fixture_anchor(pending['code/orchestration/native_candidate.py'])
    verify_native_fixture(ROOT / NATIVE_FIXTURE, anchor)
    for name in NATIVE_FIXTURE_FILES:
        add('code/' + NATIVE_FIXTURE + '/' + name, (ROOT / NATIVE_FIXTURE / name).read_bytes())
    for label, directory in sources.items():
        manifest = strict_json((directory / 'manifest.json').read_text(encoding='utf-8'))
        names = [entry['path'] for entry in manifest['artifacts']] + ['manifest.json', 'COMPLETE.json']
        for name in names:
            relative(name)
            path = directory / name
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise FoundationError('DELIVERY_SOURCE_ESCAPE')
            add('proof/' + label + '/' + name, path.read_bytes())
        add('agents/practice-' + label.lower() + '.workflow.json',
            (directory / 'candidate.workflow.json').read_bytes())
    baseline_keys = ('checked_at_utc', 'scope', 'tests_run', 'failures', 'errors', 'skipped',
                     'network_attempts', 'source_stable', 'local_gate_passed',
                     'full_semantic_accuracy_proven', 'live_freya_success_proven')
    history = {key: acceptance[key] for key in baseline_keys if key in acceptance}
    history.update({'historical': True, 'rerun_by_delivery': False,
                    'source_report_sha256': sha(Path(acceptance_path).read_bytes()),
                    'current_full_suite_claim': False,
                    'committed_files_without_baseline_pin': [name for name in code
                        if name not in acceptance.get('source_sha256', {})],
                    'committed_files_different_from_baseline_pin': [name for name in code
                        if name in acceptance.get('source_sha256', {})
                        and sha(pending['code/' + name]) != acceptance['source_sha256'][name]]})
    add('historical-baseline.json', canonical(history))
    commands = {label: ['python', '-m', 'builder.export_pipeline', 'run', '--mode', 'practice',
        '--brief', PUBLIC_KIT + 'briefs/brief-a.md',
        *(['--brief', PUBLIC_KIT + 'briefs/brief-b.md'] if label == 'B' else []),
        '--tool-api', PUBLIC_KIT + 'tools/openapi.json', '--out',
        'artifacts/export-pipeline/reproduced-' + label.lower(), '--deadline-seconds', '120']
        for label in sources}
    report = {'version': 'local-delivery-2', 'status': 'LOCAL_DELIVERY_PREPARED',
        'commit_sha': commit, 'code_whitelist': code, 'public_source_whitelist': list(PUBLIC_FILES),
        'public_sources_are_explicit_local_snapshots': True,
        'commands_working_directory': 'code', 'reproduction_commands': commands,
        'native_partial_reproduction_command': ['python', '-m', 'orchestration.native_candidate',
            '--out', 'artifacts/native-adapter/reproduced-prefix'],
        'native_fixture': {'directory': 'code/' + NATIVE_FIXTURE,
            'files': list(NATIVE_FIXTURE_FILES), 'manifest_anchor_sha256': anchor,
            'synthetic_fixture': True, 'full_historical_generic_proof_included': False,
            'purpose': 'Pinned typed inputs for local partial native prefix reproduction only'},
        'verify_command': ['python', '-m', 'orchestration.prepare_delivery', 'verify', '<package>'],
        'agent_format': 'Freya workflow JSON, official practice A and A+B',
        'agents': {label: {'file': 'agents/practice-' + label.lower() + '.workflow.json',
            'sha256': sha(pending['agents/practice-' + label.lower() + '.workflow.json']),
            'proof': 'proof/' + label, 'source_manifest_sha256': checks[label]['manifest_sha256'],
            'checked_artifacts': checks[label]['checked_artifacts']} for label in sources},
        'historical_baseline_tests': 416, 'historical_baseline_rerun': False,
        'fresh_checks': ['candidate_export_proof_recomputed_A', 'candidate_export_proof_recomputed_B',
                         'candidate_core_matches_exact_commit', 'public_whitelist_sensitive_scan'],
        'publication_ready': False, 'publish_allowed': False, 'final_submission_ready': False,
        'native_receipt_storage_proven': False, 'real_platform_success_proven': False,
        'unsupported': ['Native persistent receipt storage and trusted host acknowledgement not proven',
                        'General unseen brief semantic accuracy not proven',
                        'Official model/customer evaluation not run by this delivery'],
        'generic_python_zip_is_native_final_export': False,
        'network_calls': 0, 'model_calls': 0, 'credentials_read': False, 'pushed': False}
    add('delivery-report.json', canonical(report))
    add('README.txt', ('Local conditional delivery. Publication remains blocked.\n'
        'Exact commit: ' + commit + '\nRun reproduction commands from code/.\n'
        'agents/: two practice Freya JSON exports. proof/: matched immutable export evidence.\n'
        '416 tests are a historical baseline, not rerun by this bundle step.\n').encode())
    # Validate every input before making a new package; never copy a whole tree.
    target.mkdir(parents=True)
    for name, raw in pending.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
    manifest = {'version': 'local-delivery-2', 'commit_sha': commit,
        'files': [{'path': name, 'bytes': len(raw), 'sha256': sha(raw)}
                  for name, raw in sorted(pending.items())]}
    (target / 'manifest.json').write_bytes(canonical(manifest))
    final = {'status': 'LOCAL_DELIVERY_PREPARED', 'commit_sha': commit,
             'manifest_sha256': sha((target / 'manifest.json').read_bytes()),
             'publication_ready': False}
    (target / 'FINAL.json').write_bytes(canonical(final))
    return verify_delivery(target, expected_commit=commit)


def verify_delivery(directory, *, expected_commit=None):
    root = Path(directory).resolve()
    if root == OUTPUT.resolve() or not root.is_relative_to(OUTPUT.resolve()):
        raise FoundationError('DELIVERY_PACKAGE_ROOT_REFUSED')
    final = strict_json((root / 'FINAL.json').read_text(encoding='utf-8'))
    if final.get('status') != 'LOCAL_DELIVERY_PREPARED' or final.get('publication_ready') is not False:
        raise FoundationError('DELIVERY_FINAL_SCOPE_INVALID')
    if expected_commit is not None and final.get('commit_sha') != expected_commit:
        raise FoundationError('DELIVERY_EXPECTED_COMMIT_MISMATCH')
    raw_manifest = (root / 'manifest.json').read_bytes()
    if sha(raw_manifest) != final.get('manifest_sha256'):
        raise FoundationError('DELIVERY_MANIFEST_MISMATCH')
    manifest = strict_json(raw_manifest.decode())
    inventory = manifest.get('files', [])
    names = {relative(entry['path']) for entry in inventory}
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if len(names) != len(inventory) or actual != names | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('DELIVERY_INVENTORY_MISMATCH')
    if manifest.get('commit_sha') != final.get('commit_sha'):
        raise FoundationError('DELIVERY_COMMIT_BINDING_MISMATCH')
    for entry in inventory:
        path = root / entry['path']
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise FoundationError('DELIVERY_PATH_ESCAPE')
        raw = path.read_bytes()
        if len(raw) != entry['bytes'] or sha(raw) != entry['sha256']:
            raise FoundationError('DELIVERY_EVIDENCE_MISMATCH')
        scan_public(raw, path.suffix)
    report = strict_json((root / 'delivery-report.json').read_text(encoding='utf-8'))
    if (report.get('version') != 'local-delivery-2' or manifest.get('version') != 'local-delivery-2'
            or report.get('commit_sha') != final['commit_sha'] or any(report.get(k) is not False for k in
            ('publication_ready', 'publish_allowed', 'final_submission_ready', 'real_platform_success_proven'))):
        raise FoundationError('DELIVERY_REPORT_SCOPE_MISMATCH')
    commit = final['commit_sha']
    if (not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)
            or git_bytes('rev-parse', commit + '^{commit}').decode().strip() != commit
            or report['code_whitelist'] != admitted_code(commit)):
        raise FoundationError('DELIVERY_COMMIT_CODE_WHITELIST_MISMATCH')
    for name in report['code_whitelist']:
        if (root / 'code' / name).read_bytes() != git_bytes('show', commit + ':' + name):
            raise FoundationError('DELIVERY_COMMITTED_CODE_MISMATCH')
    required = {'historical-baseline.json', 'README.txt', 'delivery-report.json'}
    required |= {'code/' + name for name in report['code_whitelist'] + report['public_source_whitelist']}
    if report.get('version') == 'local-delivery-2':
        if not NATIVE_REQUIRED_CODE <= set(report['code_whitelist']):
            raise FoundationError('DELIVERY_NATIVE_CODE_NOT_COMMITTED')
        native = report['native_fixture']
        anchor = fixture_anchor((root / 'code/orchestration/native_candidate.py').read_bytes())
        if (native.get('directory') != 'code/' + NATIVE_FIXTURE
                or native.get('files') != list(NATIVE_FIXTURE_FILES)
                or native.get('manifest_anchor_sha256') != anchor
                or native.get('synthetic_fixture') is not True
                or native.get('full_historical_generic_proof_included') is not False):
            raise FoundationError('DELIVERY_NATIVE_FIXTURE_SCOPE_INVALID')
        required |= {'code/' + NATIVE_FIXTURE + '/' + name for name in NATIVE_FIXTURE_FILES}
        verify_native_fixture(root / 'code' / NATIVE_FIXTURE, anchor)
    for label in ('A', 'B'):
        item = report['agents'][label]
        if item.get('file') != 'agents/practice-' + label.lower() + '.workflow.json' or item.get('proof') != 'proof/' + label:
            raise FoundationError('DELIVERY_AGENT_PATH_INVALID')
        required.add(item['file'])
        workflow = (root / item['file']).read_bytes()
        proof = root / ('proof/' + label)
        if sha(workflow) != item['sha256'] or workflow != (proof / 'candidate.workflow.json').read_bytes():
            raise FoundationError('DELIVERY_AGENT_PROOF_MISMATCH')
        proof_manifest = strict_json((proof / 'manifest.json').read_text(encoding='utf-8'))
        required |= {'proof/' + label + '/' + row['path'] for row in proof_manifest['artifacts']}
        required |= {'proof/' + label + '/manifest.json', 'proof/' + label + '/COMPLETE.json'}
        if sha((proof / 'manifest.json').read_bytes()) != item['source_manifest_sha256']:
            raise FoundationError('DELIVERY_PROOF_ROOT_MISMATCH')
        checked = _verify_package(proof, proof_manifest)
        if checked['status'] != 'verified' or checked['mode'] != 'practice':
            raise FoundationError('DELIVERY_MATCHED_PROOF_BLOCKED')
        evidence = strict_json((proof / 'runtime-evidence.json').read_text(encoding='utf-8'))
        brief_names = {Path(doc['path']).name for doc in evidence['documents']}
        if 'brief-a.md' not in brief_names or ('brief-b.md' in brief_names) != (label == 'B'):
            raise FoundationError('DELIVERY_PRACTICE_PROFILE_MISMATCH')
        for name in CORE_FILES:
            snapshot = (proof / 'implementation' / name).read_bytes().replace(b'\r\n', b'\n')
            code = (root / 'code/builder' / name).read_bytes().replace(b'\r\n', b'\n')
            if snapshot != code:
                raise FoundationError('DELIVERY_CORE_CODE_PIN_MISMATCH')
    if required != names:
        raise FoundationError('DELIVERY_REQUIRED_FILE_MISSING')
    return {'status': 'LOCAL_DELIVERY_PREPARED', 'valid': True,
            'commit_sha': final['commit_sha'], 'manifest_sha256': final['manifest_sha256'],
            'files': len(names), 'publication_ready': False, 'publish_allowed': False,
            'network_calls': 0, 'model_calls': 0, 'pushed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('prepare')
    run.add_argument('--out', required=True, type=Path)
    run.add_argument('--commit', required=True)
    run.add_argument('--candidate-a', default=DEFAULT_A, type=Path)
    run.add_argument('--candidate-b', default=DEFAULT_B, type=Path)
    verify = sub.add_parser('verify')
    verify.add_argument('directory', type=Path)
    verify.add_argument('--expected-commit')
    args = parser.parse_args(argv)
    try:
        result = (prepare_delivery(output_directory=args.out, commit_sha=args.commit,
                    candidate_a=args.candidate_a, candidate_b=args.candidate_b)
                  if args.command == 'prepare' else
                  verify_delivery(args.directory, expected_commit=args.expected_commit))
        print(canonical(result).decode().strip())
        return 0
    except (FoundationError, OSError, KeyError, TypeError, ValueError):
        print('{"status":"BLOCKED","publication_ready":false,"diagnostic":"DELIVERY_VALIDATION_FAILED"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
