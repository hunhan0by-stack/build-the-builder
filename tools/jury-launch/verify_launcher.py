"""Independent subprocess probes; no model, bank or publication calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged-root', type=Path, required=True)
    parser.add_argument('--case-folder', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--codex-path', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    staged = args.staged_root.resolve()
    checkout = output / 'clean-source'
    source_manifest = json.loads((staged / 'SOURCE-MANIFEST.json').read_text(encoding='utf-8'))
    for name, digest in source_manifest.items():
        path = (staged / name).resolve()
        if not path.is_relative_to(staged):
            raise ValueError('UNSAFE_SOURCE_MANIFEST_PATH')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('SOURCE_STAGE_CHANGED:' + name)
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    case = output / 'Türkçe yeni vaka with spaces'
    shutil.copytree(args.case_folder.resolve(), case)
    manifest = output / 'case-manifest.json'
    manifest.write_bytes(args.manifest.resolve().read_bytes())
    launcher = Path(__file__).with_name('Run-Jury-Case.py')
    foreign = output / 'foreign-working-directory'
    foreign.mkdir()
    rows = []

    def probe(name, *, source=case, manifest_path=manifest, extras=(), expected=None):
        command = [sys.executable, '-B', '-I', str(launcher), str(source),
                   'https://jury-portability-fixture.example.invalid', '--repo-root', str(checkout),
                   '--source-manifest', str(manifest_path), '--reference-only', 'test-environment',
                   '--codex-path', str(args.codex_path.resolve()), '--check-only', *extras]
        result = subprocess.run(command, cwd=foreign, capture_output=True, timeout=50)
        text = result.stdout.decode('utf-8', errors='replace').strip()
        try:
            observed = json.loads(text.splitlines()[-1])
        except (ValueError, IndexError):
            observed = {'status': 'INVALID_OUTPUT'}
        passed = (result.returncode == 0 and observed.get('status') == 'LAUNCH_INPUTS_VERIFIED') if expected is None else (result.returncode != 0 and observed.get('diagnostic') == expected)
        rows.append({'name': name, 'passed': passed, 'returncode': result.returncode,
                     'observation': observed})

    probe('isolated-python-foreign-cwd-unicode-case', extras=('--report', str(output / 'actual-input-preflight.json')))
    contract = case / 'BRIEF.md'
    original = contract.read_bytes()
    contract.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    probe('same-size-source-tampering-refused', expected='LAUNCH_MANIFEST_MISMATCH')
    contract.write_bytes(original)
    added = case / 'unexpected-authority.md'
    added.write_text('Added material source.', encoding='utf-8')
    probe('unexpected-source-file-refused', expected='LAUNCH_MANIFEST_MISMATCH')
    added.unlink()
    duplicate = json.loads(manifest.read_text(encoding='utf-8'))
    duplicate['files'].append(duplicate['files'][0])
    duplicate_path = output / 'duplicate-manifest.json'
    duplicate_path.write_text(json.dumps(duplicate), encoding='utf-8')
    probe('duplicate-source-manifest-refused', manifest_path=duplicate_path, expected='LAUNCH_MANIFEST_INVALID')
    probe('output-inside-input-refused', extras=('--out', str(case / 'output')), expected='LAUNCH_FRESH_EXTERNAL_OUTPUT_REQUIRED')
    existing = output / 'existing-output'
    existing.mkdir()
    probe('existing-output-refused', extras=('--out', str(existing)), expected='LAUNCH_FRESH_EXTERNAL_OUTPUT_REQUIRED')
    probe('existing-report-refused', extras=('--report', str(output / 'actual-input-preflight.json')), expected='LAUNCH_FRESH_EXTERNAL_REPORT_REQUIRED')
    native_reference = checkout / 'data/freya-hackathon/kit-v3/build-the-builder/docs/workflow-format.md'
    reference_bytes = native_reference.read_bytes()
    native_reference.unlink()
    probe('missing-native-reference-refused', expected='LAUNCH_NATIVE_REFERENCE_MISSING')
    native_reference.write_bytes(reference_bytes)
    entry = checkout / 'builder/build.py'
    entry_bytes = entry.read_bytes()
    entry.write_text("import json, os, sys\nif '--help' in sys.argv:\n print('case_folder mock_url')\n raise SystemExit(0)\nprint(json.dumps({'child_arguments':sys.argv[1:],'cwd':os.getcwd(),'codex_path':os.environ.get('BUILDER_CODEX_EXECUTABLE'),'model':os.environ.get('BUILDER_CODEX_MODEL'),'reasoning':os.environ.get('BUILDER_CODEX_REASONING_EFFORT')}))\nraise SystemExit(17)\n", encoding='utf-8')
    try:
        future_output = output / 'stub-build-output'
        command = [sys.executable, '-B', '-I', str(launcher), str(case),
                   'https://jury-portability-fixture.example.invalid', '--repo-root', str(checkout),
                   '--source-manifest', str(manifest), '--reference-only', 'test-environment',
                   '--codex-path', str(args.codex_path.resolve()), '--out', str(future_output),
                   '--budget-seconds', '120', '--max-model-attempts', '1']
        result = subprocess.run(command, cwd=foreign, capture_output=True, timeout=50)
        messages = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
        child = next(row for row in messages if 'child_arguments' in row)
        expected_args = [str(case), 'https://jury-portability-fixture.example.invalid',
                         '--budget-seconds', '120', '--max-model-attempts', '1',
                         '--out', str(future_output), '--reference-only', 'test-environment']
        rows.append({'name': 'production-dispatch-flags-cwd-and-exit-propagation-with-inert-stub',
                     'passed': result.returncode == 17 and child['child_arguments'] == expected_args
                         and Path(child['cwd']) == checkout and child['codex_path'] == str(args.codex_path.resolve())
                         and not future_output.exists(), 'returncode': result.returncode,
                     'observation': 'Only a temporary inert CLI stub ran; no provider was invoked.'})
        repair = output / 'inert-repair-fixture'
        (repair / 'model-attempt-1').mkdir(parents=True)
        response_path = repair / 'model-attempt-1/model-response.json'
        response_path.write_bytes(b'{}')
        initial_report = json.loads((output / 'actual-input-preflight.json').read_bytes())
        role_paths = sorted(['test-environment/test-customers.json', 'test-environment/test-customers.md',
                             'example-scenarios.md', 'cloudflare-mock/example/example-response.json',
                             'cloudflare-mock/example/worker.js', 'cloudflare-mock/example/wrangler.toml'])
        prior = {'run_id': 'inert-fixture-no-generation', 'status': 'REFUSED',
                 'manifest_sha256': initial_report['manifest_sha256'],
                 'mock_url': 'https://jury-portability-fixture.example.invalid',
                 'reference_only_paths': role_paths,
                 'attempts': [{'attempt': 1, 'response_sha256': hashlib.sha256(b'{}').hexdigest()}]}
        prior_path = repair / 'end-to-end-trace.json'
        prior_path.write_text(json.dumps(prior), encoding='utf-8')
        r4_command = [sys.executable, '-B', '-I', str(launcher), str(case), prior['mock_url'],
                      '--repo-root', str(checkout), '--source-manifest', str(manifest),
                      '--reference-only', 'test-environment', '--reference-only', 'example-scenarios.md',
                      '--reference-only', 'cloudflare-mock/example', '--codex-path', str(args.codex_path.resolve()),
                      '--out', str(future_output), '--budget-seconds', '1500', '--max-model-attempts', '2',
                      '--allow-partial', '--repair-from', str(repair), '--model', 'gpt-6-astra', '--reasoning-effort', 'high']
        r4_result = subprocess.run(r4_command, cwd=foreign, capture_output=True, timeout=50)
        r4_messages = [json.loads(line) for line in r4_result.stdout.decode('utf-8').splitlines()]
        r4_child = next(row for row in r4_messages if 'child_arguments' in row)
        r4_expected = [str(case), prior['mock_url'], '--budget-seconds', '1500', '--max-model-attempts', '2',
                       '--out', str(future_output), '--reference-only', 'test-environment',
                       '--reference-only', 'example-scenarios.md', '--reference-only', 'cloudflare-mock/example',
                       '--allow-partial', '--repair-from', str(repair)]
        rows.append({'name': 'R4-equivalent-repair-partial-roles-model-reasoning-dispatch-inert-only',
                     'passed': r4_result.returncode == 17 and r4_child['child_arguments'] == r4_expected
                         and Path(r4_child['cwd']) == checkout and r4_child['model'] == 'gpt-6-astra'
                         and r4_child['reasoning'] == 'high' and not future_output.exists(),
                     'returncode': r4_result.returncode, 'observation': 'Fixture prior response and inert entry stub only; actual R4 was not replayed.'})
        def repair_refusal(name, diagnostic):
            refused = subprocess.run(r4_command + ['--check-only'], cwd=foreign, capture_output=True, timeout=50)
            observation = json.loads(refused.stdout.decode('utf-8').splitlines()[-1])
            rows.append({'name': name, 'passed': refused.returncode != 0 and observation.get('diagnostic') == diagnostic,
                         'returncode': refused.returncode, 'observation': observation})
        response_path.write_bytes(b'{ }')
        repair_refusal('tampered-repair-response-refused-before-dispatch', 'CASE_REPAIR_RESPONSE_CHANGED')
        response_path.write_bytes(b'{}')
        prior['mock_url'] = 'https://different-fixture.example.invalid'
        prior_path.write_text(json.dumps(prior), encoding='utf-8')
        repair_refusal('repair-with-different-mock-refused-before-dispatch', 'CASE_REPAIR_SOURCE_MISMATCH')
        prior['mock_url'] = 'https://jury-portability-fixture.example.invalid'
        prior['status'] = 'STARTED'
        prior_path.write_text(json.dumps(prior), encoding='utf-8')
        repair_refusal('active-repair-parent-refused-before-dispatch', 'CASE_REPAIR_RUN_STILL_ACTIVE')
    finally:
        entry.write_bytes(entry_bytes)
    # The input clone remains exactly pinned after all destructive probes.
    unchanged = all((case / row['path']).stat().st_size == row['bytes'] and
                    hashlib.sha256((case / row['path']).read_bytes()).hexdigest() == row['sha256']
                    for row in json.loads(manifest.read_bytes())['files'])
    receipt = {'kind': 'portable-launcher-independent-verification-1',
               'checked_at_utc': datetime.now(timezone.utc).isoformat(),
               'launcher_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest(),
               'staged_source_manifest_sha256': hashlib.sha256((staged / 'SOURCE-MANIFEST.json').read_bytes()).hexdigest(),
               'source_files': len(source_manifest), 'checks': rows, 'all_passed': all(r['passed'] for r in rows),
               'cloned_inputs_unchanged_after_probes': unchanged,
               'cloned_runtime_unchanged_after_probes': all(hashlib.sha256((checkout / name).read_bytes()).hexdigest() == digest for name, digest in source_manifest.items()),
               'original_source_edits': 0, 'original_input_edits': 0,
               'model_calls': 0, 'bank_calls': 0, 'publication_calls': 0,
               'production_builder_behavior_measured': False, 'final_ready': False}
    (output / 'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'all_passed': receipt['all_passed'], 'checks': len(rows), 'unchanged': unchanged}))
    return 0 if receipt['all_passed'] and unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
