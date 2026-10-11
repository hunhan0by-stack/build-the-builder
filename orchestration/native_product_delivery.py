"""Committed, portable offline delivery of two freshly verified native prefixes.

No commit, push, token, platform test or publication is performed. The copied
code is executed normally from code/; no verifier ROOT or runtime is patched.
All source bytes must already exist in exact Git HEAD, including this module.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from builder.models import FoundationError, strict_json
from orchestration.prepare_delivery import relative, scan_public
from orchestration.product_build import RUNTIME_FILES, OFFICIAL_RUNTIME, OFFICIAL_FILES, verify_product_build

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/native-product-delivery'
PACK_BATCH = 'code/artifacts/product-build/delivery-batch'
MODULE = 'orchestration/native_product_delivery.py'
KIT_REL = 'data/freya-hackathon/kit-v3/build-the-builder'
PUBLIC_KIT_FILES = tuple(KIT_REL + '/' + name for name in
                         (*OFFICIAL_FILES, 'tools/openapi.json'))
HOST_CONNECTION_FILES = ('builder/freya_lifecycle.py', 'builder/freya_completion_binding.py',
                         'builder/freya_bank_safety.py', 'builder/platform_bridge.py')
BUILDER_ENTRY_FILES = ('orchestration/build_the_builder.py',
                       'orchestration/behavioral_repair.py', 'integration/failure_learning.py',
                       'orchestration/jury_preflight.py', 'builder/native_safety_admission.py',
                       'orchestration/generalization_benchmark.py')
SCOPE_FLAGS = ('publish_allowed', 'publication_ready', 'final_submission_ready',
               'real_platform_success_proven')


def raw_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode() + b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return strict_json(Path(path).read_text(encoding='utf-8'))


def git_bytes(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, timeout=15)
    if result.returncode:
        raise FoundationError('NATIVE_DELIVERY_COMMIT_UNAVAILABLE')
    return result.stdout


def source_files():
    """Local Python import closure plus runtime content dependencies."""
    # Include the concrete host connections for review and reuse. Their presence
    # in this offline bundle is never a native persistence/receipt capability.
    pending, names = (list(RUNTIME_FILES) + list(OFFICIAL_RUNTIME) + list(HOST_CONNECTION_FILES)
                      + list(BUILDER_ENTRY_FILES) + [MODULE]), set()
    while pending:
        name = relative(pending.pop())
        if name in names:
            continue
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise FoundationError('NATIVE_DELIVERY_SOURCE_UNAVAILABLE')
        names.add(name)
        for parent in Path(name).parents:
            if parent.as_posix() != '.':
                init = parent.as_posix() + '/__init__.py'
                if (ROOT / init).is_file() and init not in names:
                    pending.append(init)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = name.split('/')[:-1]
                    if node.level > len(parts):
                        raise FoundationError('NATIVE_DELIVERY_IMPORT_ESCAPE')
                    modules = ['.'.join(parts[:len(parts) - node.level + 1]
                                       + (node.module or '').split('.')).strip('.')]
                elif node.module:
                    modules = [node.module]
            for module in modules:
                if module.split('.')[0] not in {'builder', 'compiler', 'orchestration'}:
                    continue
                candidate = module.replace('.', '/') + '.py'
                if not (ROOT / candidate).is_file():
                    candidate = module.replace('.', '/') + '/__init__.py'
                if (ROOT / candidate).is_file() and candidate not in names:
                    pending.append(candidate)
    return sorted(names)


def public_inputs():
    """Official public documents required by the default full-native entry.

    They are public input bytes, not Python source or an assertion of Git
    tracking. They get independent manifest pins and exact inventory checks.
    """
    result = {}
    for name in PUBLIC_KIT_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise FoundationError('NATIVE_DELIVERY_PUBLIC_INPUT_UNAVAILABLE')
        raw = path.read_bytes()
        scan_public(raw, path.suffix)
        result[name] = raw
    return result


def committed_sources():
    commit = git_bytes('rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise FoundationError('NATIVE_DELIVERY_EXACT_HEAD_REQUIRED')
    names = source_files()
    tracked = set(git_bytes('ls-tree', '-r', '--name-only', commit).decode().splitlines())
    if not set(names) <= tracked:
        raise FoundationError('NATIVE_DELIVERY_UNCOMMITTED_DEPENDENCY')
    result = {}
    for name in names:
        local = (ROOT / name).read_bytes()
        if local != git_bytes('show', commit + ':' + name):
            raise FoundationError('NATIVE_DELIVERY_DIRTY_DEPENDENCY')
        scan_public(local, '.py')
        result[name] = local
    return commit, result


def verify_batch(batch):
    root = Path(batch).resolve()
    report_raw = (root / 'batch-report.json').read_bytes()
    report, final = strict_json(report_raw.decode()), read(root / 'BATCH_FINAL.json')
    if (report.get('version') != 'two-source-products-1'
            or report.get('status') != 'LOCAL_TWO_EXPORTS_VERIFIED'
            or final.get('status') != report['status']
            or final.get('report_sha256') != sha(report_raw)
            or final.get('publish_allowed') is not False
            or any(report.get(flag) is not False for flag in SCOPE_FLAGS[:3])
            or report.get('independent_brief_budgets') is not True
            or report.get('network_calls') != 0):
        raise FoundationError('NATIVE_DELIVERY_BATCH_RECEIPT_INVALID')
    products = report.get('products')
    if not isinstance(products, list) or len(products) != 2:
        raise FoundationError('NATIVE_DELIVERY_TWO_PRODUCTS_REQUIRED')
    ids = [item.get('id') for item in products if isinstance(item, dict)]
    if len(ids) != 2 or len(set(ids)) != 2 or any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', i) for i in ids):
        raise FoundationError('NATIVE_DELIVERY_JOB_IDS_INVALID')
    if {p.name for p in root.iterdir()} != set(ids) | {'batch-report.json', 'BATCH_FINAL.json'}:
        raise FoundationError('NATIVE_DELIVERY_BATCH_INVENTORY_INVALID')
    checked = {}
    for item in products:
        job = root / item['id']
        if job.is_symlink() or not job.is_dir():
            raise FoundationError('NATIVE_DELIVERY_JOB_PATH_INVALID')
        proof = verify_product_build(job)
        readiness = read(job / 'readiness.json')
        if (proof.get('valid') is not True or proof.get('publish_allowed') is not False
                or item.get('independent_verification') is not True
                or item.get('publish_allowed') is not False
                or item.get('status') != proof['status']
                or item.get('candidate_sha256') != proof['candidate_sha256']
                or item.get('manifest_sha256') != proof['manifest_sha256']
                or item.get('deadline_seconds_per_brief') != readiness['deadline_seconds']):
            raise FoundationError('NATIVE_DELIVERY_JOB_PROOF_MISMATCH')
        for entry in read(job / 'source-bytes.json'):
            raw = base64.b64decode(entry['base64'], validate=True)
            scan_public(raw, Path(entry['path']).suffix.lower())
        checked[item['id']] = proof
    return report, checked


def run_portable_verification(target):
    """Normal execution against copied __file__-relative code/source sidecars."""
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-m', 'orchestration.native_product_delivery',
        'verify', '--bundle', str(target)], cwd=target / 'code', env=environment,
        capture_output=True, timeout=120)
    if result.returncode:
        # Never echo raw child output, which can contain arbitrary source text.
        raise FoundationError('NATIVE_DELIVERY_PORTABLE_REPLAY_FAILED')
    answer = strict_json(result.stdout.decode())
    if answer.get('valid') is not True or answer.get('real_platform_success_proven') is not False:
        raise FoundationError('NATIVE_DELIVERY_PORTABLE_REPLAY_INVALID')
    return answer


def build_delivery(batch_path, out):
    target = Path(out).resolve()
    if target == OUTPUT.resolve() or not target.is_relative_to(OUTPUT.resolve()) or target.exists():
        raise FoundationError('NATIVE_DELIVERY_FRESH_CHILD_REQUIRED')
    # Commit admission before any output; no auto-commit or partial success.
    commit, sources = committed_sources()
    inputs = public_inputs()
    report, checked = verify_batch(batch_path)
    pending = {}
    def add(name, raw):
        relative(name)
        scan_public(raw, Path(name).suffix)
        if name in pending:
            raise FoundationError('NATIVE_DELIVERY_DUPLICATE_ENTRY')
        pending[name] = raw
    for name, raw in sources.items():
        add('code/' + name, raw)
    for name, raw in inputs.items():
        add('code/' + name, raw)
    batch = Path(batch_path).resolve()
    for name in ('batch-report.json', 'BATCH_FINAL.json'):
        add(PACK_BATCH + '/' + name, (batch / name).read_bytes())
    agents = {}
    for job_id, proof in checked.items():
        job = batch / job_id
        names = [row['path'] for row in read(job / 'manifest.json')['files']] + ['manifest.json', 'FINAL.json']
        for name in names:
            relative(name)
            path = job / name
            if path.is_symlink() or not path.resolve().is_relative_to(job):
                raise FoundationError('NATIVE_DELIVERY_EVIDENCE_ESCAPE')
            add(PACK_BATCH + '/' + job_id + '/' + name, path.read_bytes())
        agent_name = 'agents/' + job_id + '.workflow.json'
        add(agent_name, (job / 'agent.workflow.json').read_bytes())
        agents[job_id] = {**proof, 'file': agent_name, 'proof': PACK_BATCH + '/' + job_id}
    pins = {name: sha(raw) for name, raw in sources.items()}
    modes = {job_id: read(batch / job_id / 'readiness.json').get('native_mode', 'prefix') for job_id in checked}
    status = 'LOCAL_NATIVE_BRANCH_DELIVERY' if 'official' in modes.values() else 'LOCAL_NATIVE_PREFIX_DELIVERY'
    delivery = {'version': 'native-product-delivery-1', 'status': status,
        'commit_sha': commit, 'source_sha256': pins, 'agents': agents,
        'public_input_sha256': {name: sha(raw) for name, raw in inputs.items()}, 'native_modes': modes,
        'batch_report_sha256': sha((batch / 'batch-report.json').read_bytes()),
        'verify_command': ['python', '-m', 'orchestration.native_product_delivery', 'verify', '--bundle', '..'],
        'command_working_directory': 'code', 'runtime_paths_patched': False,
        'source_snapshots_and_byte_sidecars_included': True,
        'scope': ('Two source-bound local native proofs; official mode covers the complete single-product promise branch; prefix mode is read-only'),
        'unsupported': ['Local completion/bank fixtures confer no actual platform authority or arbitrary restart guarantee',
                        'Final unseen brief general interpretation and official Freya customer success unproven'],
        'network_calls': 0, 'model_calls': 0, 'credentials_read': False, 'pushed': False,
        **{flag: False for flag in SCOPE_FLAGS}}
    add('delivery-report.json', raw_json(delivery))
    add('README.txt', ('Local source-bound native delivery; platform and final readiness remain false.\n'
        'Exact source commit: ' + commit + '\n'
        'From code/: python -m orchestration.native_product_delivery verify --bundle ..\n'
        'The verifier replays both archived proofs; no original input paths or runtime patches.\n'
        'Read-only commit authentication, from code/: python -m orchestration.jury_preflight '
        '--bundle .. --clone PATH_TO_EXACT_COMMIT_CLONE --expected-commit ' + commit +
        ' --trusted-manifest-sha256 HASH_FROM_INDEPENDENT_REVIEWER\n'
        'The manifest reference must be supplied independently, outside this bundle. '
        'An exact Git clone is required for source commit authentication.\n'
        'Offline build and verification require Python standard library and Git; '
        'no Freya key or external model credentials are required.\n'
        'Builder entry, from code/: python -m orchestration.build_the_builder '
        '--brief PATH_TO_SUPPORTED_BRIEF --tool-api PATH_TO_TOOL_SCHEMA '
        '--out artifacts/unified-builder/v1/new-run --deadline-seconds 1800\n'
        'Supported input: bounded typed Parameter/Wording rules and supported amendments, '
        'one product and one promise; unknown requirements fail explicitly.\n'
        'The Builder CLI defaults to --native-mode official: source-compatible retry2 and '
        'product_label success wording; complete local promise/closure checks. '
        'Use --native-mode prefix only for explicitly partial identity/account proofs. '
        'Official public reference documents and bank API are included under code/data/.\n'
        'Optional --behavioral-repair --behavior-script PATH_TO_STRUCTURED_SCRIPT '
        'also checks a portable local workflow; it does not open native banking or speech gates.\n'
        'Local generalization matrix, from code/: python -m orchestration.generalization_benchmark '
        '--tool-api PATH_TO_PUBLIC_TOOL_SCHEMA --out artifacts/generalization/v1/new-run '
        '--deadline-seconds 1800\n'
        'Supply the public bank schema explicitly; the matrix does not require an original kit directory. '
        'It measures bounded typed briefs and structured dialogue, with one deliberate renderer fault; '
        'it does not establish unseen natural-language interpretation or official Freya success.\n').encode())
    # Recheck all active sources AND both proofs after assembling bytes.
    if committed_sources() != (commit, sources):
        raise FoundationError('NATIVE_DELIVERY_SOURCE_CHANGED_DURING_BUILD')
    if public_inputs() != inputs:
        raise FoundationError('NATIVE_DELIVERY_PUBLIC_INPUT_CHANGED_DURING_BUILD')
    _, rechecked = verify_batch(batch)
    if checked != rechecked:
        raise FoundationError('NATIVE_DELIVERY_PROOF_CHANGED_DURING_BUILD')
    for job_id in checked:
        for name in [row['path'] for row in read(batch / job_id / 'manifest.json')['files']] + ['manifest.json', 'FINAL.json']:
            if (batch / job_id / name).read_bytes() != pending[PACK_BATCH + '/' + job_id + '/' + name]:
                raise FoundationError('NATIVE_DELIVERY_PROOF_CHANGED_DURING_BUILD')
    target.mkdir(parents=True)
    try:
        for name, raw in pending.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(raw)
        manifest = {'version': delivery['version'], 'commit_sha': commit,
            'files': [{'path': name, 'bytes': len(raw), 'sha256': sha(raw)} for name, raw in sorted(pending.items())]}
        (target / 'manifest.json').write_bytes(raw_json(manifest))
        (target / 'FINAL.json').write_bytes(raw_json({'status': delivery['status'], 'commit_sha': commit,
            'manifest_sha256': sha((target / 'manifest.json').read_bytes()), **{flag: False for flag in SCOPE_FLAGS}}))
        return run_portable_verification(target)
    except Exception:
        (target / 'FAILED_FINALIZATION.json').write_bytes(raw_json({'status': 'BLOCKED', 'publication_ready': False}))
        raise


def verify_delivery(directory, *, expected_commit=None):
    target = Path(directory).resolve()
    if (target / 'FAILED_FINALIZATION.json').exists():
        raise FoundationError('NATIVE_DELIVERY_FINALIZATION_FAILED')
    final, report = read(target / 'FINAL.json'), read(target / 'delivery-report.json')
    raw_manifest = (target / 'manifest.json').read_bytes()
    manifest = strict_json(raw_manifest.decode())
    commit = final.get('commit_sha')
    if (not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)
            or (expected_commit is not None and commit != expected_commit)
            or report.get('version') != 'native-product-delivery-1'
            or manifest.get('version') != report['version']
            or report.get('status') not in ('LOCAL_NATIVE_PREFIX_DELIVERY', 'LOCAL_NATIVE_BRANCH_DELIVERY') or final.get('status') != report['status']
            or report.get('commit_sha') != commit or manifest.get('commit_sha') != commit
            or sha(raw_manifest) != final.get('manifest_sha256')
            or any(final.get(flag) is not False or report.get(flag) is not False for flag in SCOPE_FLAGS)):
        raise FoundationError('NATIVE_DELIVERY_FINAL_BINDING_INVALID')
    rows = manifest['files']
    names = {relative(row['path']) for row in rows}
    actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
    if len(names) != len(rows) or actual != names | {'manifest.json', 'FINAL.json'}:
        raise FoundationError('NATIVE_DELIVERY_INVENTORY_MISMATCH')
    for row in rows:
        path = target / row['path']
        if path.is_symlink() or not path.resolve().is_relative_to(target):
            raise FoundationError('NATIVE_DELIVERY_PATH_ESCAPE')
        raw = path.read_bytes()
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise FoundationError('NATIVE_DELIVERY_BYTES_CHANGED')
        scan_public(raw, path.suffix)
    if (target / 'code').resolve() != ROOT.resolve():
        raise FoundationError('NATIVE_DELIVERY_RUN_FROM_COPIED_CODE_REQUIRED')
    if set(report['source_sha256']) != set(source_files()):
        raise FoundationError('NATIVE_DELIVERY_SOURCE_CLOSURE_MISMATCH')
    for name, digest in report['source_sha256'].items():
        if sha((ROOT / relative(name)).read_bytes()) != digest:
            raise FoundationError('NATIVE_DELIVERY_SOURCE_PIN_MISMATCH')
    inputs = public_inputs()
    if report.get('public_input_sha256') != {name: sha(raw) for name, raw in inputs.items()}:
        raise FoundationError('NATIVE_DELIVERY_PUBLIC_INPUT_PIN_MISMATCH')
    batch = target / PACK_BATCH
    batch_report, checks = verify_batch(batch)
    modes = {job_id: read(batch / job_id / 'readiness.json').get('native_mode', 'prefix') for job_id in checks}
    expected_status = 'LOCAL_NATIVE_BRANCH_DELIVERY' if 'official' in modes.values() else 'LOCAL_NATIVE_PREFIX_DELIVERY'
    if report.get('native_modes') != modes or report['status'] != expected_status:
        raise FoundationError('NATIVE_DELIVERY_NATIVE_SCOPE_MISMATCH')
    if report['batch_report_sha256'] != sha((batch / 'batch-report.json').read_bytes()) or set(report['agents']) != set(checks):
        raise FoundationError('NATIVE_DELIVERY_BATCH_BINDING_MISMATCH')
    for job_id, proof in checks.items():
        entry = report['agents'][job_id]
        if (entry != {**proof, 'file': 'agents/' + job_id + '.workflow.json', 'proof': PACK_BATCH + '/' + job_id}
                or (target / entry['file']).read_bytes() != (batch / job_id / 'agent.workflow.json').read_bytes()):
            raise FoundationError('NATIVE_DELIVERY_AGENT_PROOF_MISMATCH')
    return {'valid': True, 'status': report['status'], 'commit_sha': commit,
            'agents': checks, 'portable_archived_proofs_recomputed': True,
            'git_commit_authenticated_in_clone': False, 'network_calls': 0,
            **{flag: False for flag in SCOPE_FLAGS}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    build = sub.add_parser('build')
    build.add_argument('--batch', required=True, type=Path)
    build.add_argument('--out', required=True, type=Path)
    verify = sub.add_parser('verify')
    verify.add_argument('--bundle', required=True, type=Path)
    verify.add_argument('--expected-commit')
    args = parser.parse_args(argv)
    try:
        result = build_delivery(args.batch, args.out) if args.operation == 'build' else verify_delivery(args.bundle, expected_commit=args.expected_commit)
    except (FoundationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(raw_json({'status': 'BLOCKED', 'valid': False, 'publication_ready': False}).decode().strip())
        return 2
    print(raw_json(result).decode().strip())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
