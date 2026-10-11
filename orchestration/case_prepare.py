"""Prepare pinned case inputs without model, bank, publication or simulation calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from builder.case_contract import mock_base
from builder.case_model import CASE_RESPONSE, case_prompt
from builder.case_reader import read_case_folder, CaseReaderError
from builder.model_workflow import CodexWorkflowProvider
from builder.models import FoundationError, strict_json
from .case_build import select_model_sources, verify_case_snapshot
from .contest_build import ROOT, KIT, write


def prepare(case_folder, mock_url, source_manifest, output_directory, run_output,
            *, reference_only=None, max_model_attempts=3, budget_seconds=1800):
    folder = Path(case_folder).resolve()
    output = Path(output_directory).resolve()
    run_output = Path(run_output).resolve()
    mock_url = mock_base(mock_url)
    if max_model_attempts not in (1, 2, 3) or type(max_model_attempts) is not int:
        raise FoundationError('CASE_MODEL_ATTEMPTS_MUST_BE_1_TO_3')
    if type(budget_seconds) is not int or not 1 <= budget_seconds <= 1800:
        raise FoundationError('CASE_PREPARATION_BUDGET_INVALID')
    if (output == folder or output.is_relative_to(folder) or run_output == folder
            or run_output.is_relative_to(folder) or output == run_output):
        raise FoundationError('CASE_OUTPUT_MUST_BE_OUTSIDE_SOURCE')
    if output.exists() or run_output.exists():
        raise FoundationError('CASE_FRESH_OUTPUT_REQUIRED')
    manifest_path = Path(source_manifest).resolve()
    manifest_bytes = manifest_path.read_bytes()
    expected = strict_json(manifest_bytes.decode('utf-8'))
    rows = expected.get('files', [])
    if (not isinstance(rows, list) or not rows
            or any(not isinstance(r, dict) or not {'path', 'bytes', 'sha256'} <= r.keys() for r in rows)
            or len({r['path'] for r in rows}) != len(rows)):
        raise FoundationError('CASE_SOURCE_MANIFEST_INVALID')
    projection = read_case_folder(folder)
    if projection.get('complete') is not True:
        raise FoundationError('CASE_EXTRACTION_INCOMPLETE')
    pinned = {r['path']: (r['bytes'], r['sha256']) for r in rows}
    observed = {r['path']: (r['bytes'], r['sha256']) for r in projection['manifest']}
    if pinned != observed:
        raise FoundationError('CASE_SOURCE_MANIFEST_MISMATCH')
    verify_case_snapshot(folder, projection)
    selected = select_model_sources(projection, reference_only)
    references = {name: (KIT / name).read_text(encoding='utf-8') for name in
                  ('docs/workflow-format.md', 'docs/tools-and-code.md')}
    provider = CodexWorkflowProvider()
    if not Path(provider.executable).is_file():
        raise FoundationError('BUILDER_CODEX_NOT_FOUND')
    command = ['builder/build.py', str(folder), mock_url, '--out', str(run_output),
               '--max-model-attempts', str(max_model_attempts),
               '--budget-seconds', str(budget_seconds)]
    for role in reference_only or []:
        command.extend(('--reference-only', role))
    prompt = case_prompt(selected, mock_url, references)
    runtime = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for parent in ('builder', 'orchestration', 'compiler')
               for p in (ROOT / parent).rglob('*.py')}
    verify_case_snapshot(folder, projection)
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'case-projection.json', projection)
    write(output / 'model-source-projection.json', selected)
    write(output / 'model-response.schema.json', CASE_RESPONSE)
    prompt_bytes = prompt.encode('utf-8')
    (output / 'planned-model-input.txt').write_bytes(prompt_bytes)
    report = {'kind': 'case-run-input-preparation-1', 'status': 'RUN_INPUTS_PREPARED',
        'prepared_at_utc': datetime.now(timezone.utc).isoformat(),
        'case_folder': str(folder), 'mock_url': mock_url,
        'source_manifest_file': str(manifest_path),
        'source_manifest_file_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'manifest_sha256': projection['manifest_sha256'],
        'projection_sha256': projection['projection_sha256'],
        'model_projection_sha256': selected['model_projection_sha256'],
        'source_files': len(rows), 'source_manifest': projection['manifest'],
        'reference_only_paths': selected['reference_only_paths'],
        'requires_review': projection['requires_review'],
        'extraction_diagnostics': projection['diagnostics'],
        'planned_model_input_sha256': hashlib.sha256(prompt_bytes).hexdigest(),
        'runtime_sha256': runtime, 'python_arguments': command,
        'model_requested': provider.model, 'reasoning_effort_requested': provider.reasoning_effort,
        'cli_file_available': True, 'cli_authentication_verified': False,
        'model_calls': 0, 'bank_calls': 0, 'publication_calls': 0, 'simulation_calls': 0,
        'semantic_policy_verified': False, 'live_verified': False,
        'budget_is_internal_limit': True,
        'verification_scope': 'Exact source bytes, complete extraction, input roles and CLI presence. Model generation, source interpretation and native behavior are not measured.'}
    write(output / 'report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_folder')
    parser.add_argument('mock_url')
    parser.add_argument('--source-manifest', required=True)
    parser.add_argument('--out', dest='output_directory', required=True)
    parser.add_argument('--run-out', dest='run_output', required=True)
    parser.add_argument('--reference-only', action='append', default=[])
    parser.add_argument('--max-model-attempts', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--budget-seconds', type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        report = prepare(**vars(args))
    except (FoundationError, CaseReaderError, ValueError, OSError) as error:
        print(json.dumps({'status': 'REFUSED', 'diagnostic': str(error)
            if isinstance(error, (FoundationError, CaseReaderError)) else 'CASE_PREPARATION_IO_ERROR'}))
        return 1
    print(json.dumps({k: report[k] for k in ('status', 'source_files', 'manifest_sha256', 'model_calls')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
