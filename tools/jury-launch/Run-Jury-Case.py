"""Portable, manifest-pinned launcher for the existing folder builder."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_folder', type=Path)
    parser.add_argument('mock_url')
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--reference-only', action='append', default=[])
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--codex-path', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--reasoning-effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--repair-from', type=Path)
    parser.add_argument('--budget-seconds', type=int, default=1800)
    parser.add_argument('--max-model-attempts', type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args(argv)
    if not args.check_only and args.out is None:
        parser.error('--out is required to start a new build')
    root = args.repo_root.resolve()
    case = args.case_folder.resolve()
    report = {'kind': 'portable-jury-launch-preflight-1',
              'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'repo_root': str(root), 'case_folder': str(case),
              'model_calls': 0, 'bank_calls': 0, 'publication_calls': 0,
              'live_verified': False, 'final_ready': False, 'status': 'REFUSED'}
    try:
        if not 1 <= args.budget_seconds <= 1800:
            raise ValueError('LAUNCH_BUDGET_INVALID')
        entry = root / 'builder/build.py'
        if not entry.is_file():
            raise ValueError('LAUNCH_SOURCE_ENTRY_MISSING')
        # The script can live outside the checkout and run from any cwd.
        sys.path.insert(0, str(root))
        from builder.case_reader import read_case_folder
        from builder.case_contract import mock_base
        from builder.models import strict_json
        from builder.model_workflow import CodexWorkflowProvider
        from orchestration.case_build import select_model_sources, verify_case_snapshot
        from orchestration.contest_build import KIT
        mock_url = mock_base(args.mock_url)
        projection = read_case_folder(case)
        if projection.get('complete') is not True:
            raise ValueError('LAUNCH_EXTRACTION_INCOMPLETE')
        expected_raw = args.source_manifest.resolve().read_bytes()
        expected = strict_json(expected_raw.decode('utf-8'))
        rows = expected.get('files') if isinstance(expected, dict) else None
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(r, dict) or not {'path', 'bytes', 'sha256'} <= r.keys() for r in rows)
                or any(not isinstance(r['path'], str) for r in rows)
                or len({r['path'] for r in rows}) != len(rows)):
            raise ValueError('LAUNCH_MANIFEST_INVALID')
        pinned = {r['path']: (r['bytes'], r['sha256']) for r in rows}
        observed = {r['path']: (r['bytes'], r['sha256']) for r in projection['manifest']}
        if pinned != observed:
            raise ValueError('LAUNCH_MANIFEST_MISMATCH')
        selected = select_model_sources(projection, args.reference_only)
        for name in ('docs/workflow-format.md', 'docs/tools-and-code.md'):
            if not (KIT / name).is_file():
                raise ValueError('LAUNCH_NATIVE_REFERENCE_MISSING')
        codex = str(args.codex_path.resolve()) if args.codex_path else os.environ.get('BUILDER_CODEX_EXECUTABLE') or shutil.which('codex')
        if not codex or not Path(codex).is_file():
            raise ValueError('LAUNCH_CODEX_PATH_REQUIRED')
        provider = CodexWorkflowProvider(executable=codex, model=args.model,
                                         reasoning_effort=args.reasoning_effort)
        env = dict(os.environ, BUILDER_CODEX_EXECUTABLE=codex,
                   BUILDER_CODEX_MODEL=provider.model,
                   BUILDER_CODEX_REASONING_EFFORT=provider.reasoning_effort)
        repair_origin = None
        previous = args.repair_from.resolve() if args.repair_from else None
        if previous:
            prior_raw = (previous / 'end-to-end-trace.json').read_bytes()
            prior = strict_json(prior_raw.decode('utf-8'))
            if (prior.get('manifest_sha256') != projection['manifest_sha256']
                    or prior.get('mock_url') != mock_url
                    or prior.get('reference_only_paths') != selected['reference_only_paths']):
                raise ValueError('CASE_REPAIR_SOURCE_MISMATCH')
            if prior.get('status') == 'STARTED':
                raise ValueError('CASE_REPAIR_RUN_STILL_ACTIVE')
            candidates = [entry for entry in prior.get('attempts', []) if entry.get('response_sha256')]
            if not candidates:
                raise ValueError('CASE_REPAIR_RESPONSE_UNAVAILABLE')
            last = candidates[-1]
            number = last.get('attempt')
            if type(number) is not int or not 1 <= number <= 3:
                raise ValueError('CASE_REPAIR_ATTEMPT_INVALID')
            response_raw = (previous / ('model-attempt-' + str(number)) / 'model-response.json').read_bytes()
            if hashlib.sha256(response_raw).hexdigest() != last['response_sha256']:
                raise ValueError('CASE_REPAIR_RESPONSE_CHANGED')
            strict_json(response_raw.decode('utf-8'))
            repair_origin = {'run_id': prior.get('run_id'), 'attempt': number,
                             'trace_sha256': hashlib.sha256(prior_raw).hexdigest(),
                             'response_sha256': last['response_sha256'],
                             'scope': 'Input hash binding only; builder validates the repair draft again.'}
        help_command = [sys.executable, '-B', '-I', str(entry), str(case), mock_url, '--help']
        help_result = subprocess.run(help_command, cwd=Path(__file__).resolve().parent,
                                     capture_output=True, timeout=20)
        if help_result.returncode != 0 or b'case_folder' not in help_result.stdout:
            raise ValueError('LAUNCH_FOLDER_ENTRY_SMOKE_FAILED')
        output = args.out.resolve() if args.out else None
        if output and (output.exists() or output == case or output.is_relative_to(case)
                       or (previous and (output == previous or output.is_relative_to(previous)))):
            raise ValueError('LAUNCH_FRESH_EXTERNAL_OUTPUT_REQUIRED')
        command = [sys.executable, '-B', str(entry), str(case), mock_url,
                   '--budget-seconds', str(args.budget_seconds),
                   '--max-model-attempts', str(args.max_model_attempts)]
        if output:
            command.extend(('--out', str(output)))
        for role in args.reference_only:
            command.extend(('--reference-only', role))
        if args.allow_partial:
            command.append('--allow-partial')
        if previous:
            command.extend(('--repair-from', str(previous)))
        verify_case_snapshot(case, projection)
        report.update(status='LAUNCH_INPUTS_VERIFIED', source_files=len(rows),
                      source_manifest_sha256=hashlib.sha256(expected_raw).hexdigest(),
                      manifest_sha256=projection['manifest_sha256'],
                      model_projection_sha256=selected['model_projection_sha256'],
                      reference_only_paths=selected['reference_only_paths'],
                      extraction_diagnostics=projection['diagnostics'],
                      cli_authentication_verified=False, cli_file_available=True,
                      folder_entry_help_passed=True, mock_url=mock_url,
                      runtime_sha256={p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for parent in ('builder', 'compiler', 'orchestration')
                          for p in (root / parent).rglob('*.py')},
                      build_arguments=command,
                      environment_overrides={name: env[name] for name in
                          ('BUILDER_CODEX_EXECUTABLE', 'BUILDER_CODEX_MODEL', 'BUILDER_CODEX_REASONING_EFFORT')},
                      allow_partial=args.allow_partial, repair_origin=repair_origin,
                      verification_scope='Pinned inputs, extraction, reference roles, import closure and CLI presence. Generation and native behavior require their own evidence.')
        if args.report:
            target = args.report.resolve()
            if (target.exists() or target == case or target.is_relative_to(case)
                    or (previous and (target == previous or target.is_relative_to(previous)))):
                raise ValueError('LAUNCH_FRESH_EXTERNAL_REPORT_REQUIRED')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('status', 'source_files', 'model_calls', 'live_verified')}, ensure_ascii=False))
        if args.check_only:
            return 0
        return subprocess.run(command, cwd=root, env=env).returncode
    except Exception as error:
        from_error = type(error).__name__
        diagnostic = str(error) if from_error in ('FoundationError', 'CaseReaderError', 'ValueError') else 'LAUNCH_CHECK_FAILED_' + from_error
        print(json.dumps({'status': 'REFUSED', 'diagnostic': diagnostic, 'model_calls': 0}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
