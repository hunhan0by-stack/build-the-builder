"""Fresh allowlisted source checkout; no private pilot data or credentials."""
from pathlib import Path
import argparse
import json
import shutil
import zipfile
import re

from builder.models import FoundationError
from orchestration.contest_build import ROOT, OUTPUT, KIT, digest, write
from orchestration.prepare_delivery import scan_public


def prepare(directory, *, regression_candidate=None, regression_candidate_sha256=None):
    directory = Path(directory).resolve()
    if directory.exists() or not directory.is_relative_to(OUTPUT.resolve()) or directory == OUTPUT.resolve():
        raise FoundationError('CONTEST_FRESH_CHECKOUT_REQUIRED')
    names = ['CONTEST-RUN.md', '.gitignore', '.gitattributes', 'pyproject.toml',
             'requirements-evidence-protection.txt', 'tests/__init__.py',
             'tests/test_portable_protection.py']
    # Required regression dependencies are explicit rather than silently omitted
    # by glob. A graph fixture is optional and never a provider final export.
    required_regressions = ['tests_orchestration/' + name for name in (
        'generated_workflow_harness.py', 'test_generated_candidate_dialogue.py',
        'test_generated_candidate_collective.py', 'test_final_run_pairing.py',
        'test_contest_run_pairing.py', 'test_contest_checkout_closure.py')]
    if any(not (ROOT / name).is_file() for name in required_regressions):
        raise FoundationError('CONTEST_REGRESSION_DEPENDENCY_MISSING')
    names.extend(required_regressions)
    # Public-only local validators and their exact import/data dependencies.
    # Never copy local-evidence as a directory or include collected records.
    required_public_dependencies = (
        'tests/helpers.py',
        'tests/conversation_helpers.py',
        'tests/test_public_replays.py',
        'tests_orchestration/test_public_behavior.py',
        'integration/failure_learning.py',
        'project-management/local-evidence/pub01_behavior.py',
        'project-management/local-evidence/validate_pub01_v2.py',
        'project-management/local-evidence/validate_evidence.py',
        'project-management/local-evidence/contract-v2.json',
        'project-management/local-evidence/contract-v1.json',
    )
    if any(not (ROOT / name).is_file() for name in required_public_dependencies):
        raise FoundationError('CONTEST_PUBLIC_VALIDATION_DEPENDENCY_MISSING')
    names.extend(required_public_dependencies)
    for package in ('builder', 'compiler', 'orchestration'):
        names.extend(path.relative_to(ROOT).as_posix() for path in (ROOT / package).glob('*.py'))
    for package, pattern in (('tests_orchestration', 'test_contest_build.py'),
                             ('tests_orchestration', 'test_contest_practice.py'),
                             ('tests_orchestration', 'test_contest_main_wire.py'),
                             ('tests_orchestration', 'test_contest_entry_evidence.py'),
                             ('tests_orchestration', 'test_contest_chat.py'),
                             ('tests_orchestration', 'test_contest_chat_entry.py'),
                             ('tests_orchestration', 'test_model_wire.py'),
                             ('tests_orchestration', 'test_native_defaults.py'),
                             ('tests_orchestration', 'test_python_format.py'),
                             ('tests_orchestration', 'test_extraction_interface.py'),
                             ('tests_orchestration', 'test_model_diagnostics.py'),
                             ('tests_orchestration', 'test_generated_behavior.py'),
                             ('tests_orchestration', 'test_behavior_cache.py'),
                             ('tests_orchestration', 'test_conversation_passthrough.py'),
                             ('tests_orchestration', 'test_causal_repair_observations.py'),
                             ('tests_orchestration', 'test_contest_behavior_pipeline.py'),
                             ('tests_orchestration', 'test_credit_observation.py'),
                             ('tests_orchestration', 'test_contest_preflight.py'),
                             ('tests_orchestration', 'test_single_markdown_sections.py'),
                             ('tests_compiler', 'test_brief_requirements.py'),
                             ('tests_compiler', 'test_brief_formats.py')):
        names.extend(path.relative_to(ROOT).as_posix() for path in (ROOT / package).glob(pattern))
    for package in ('tests_orchestration', 'tests_compiler'):
        names.extend(path.relative_to(ROOT).as_posix() for path in (ROOT / package / 'inputs').rglob('*') if path.is_file())
        init = ROOT / package / '__init__.py'
        if init.exists():
            names.append(init.relative_to(ROOT).as_posix())
    names.append('tests_integration/test_contest_evidence.py')
    if (ROOT/'tests_integration/__init__.py').exists():
        names.append('tests_integration/__init__.py')
    names.extend('integration/' + name for name in ('__init__.py', 'contest_evidence.py',
        'native_pilot.py', 'pilot.py', 'preflight.py', 'private_state.py',
        'response_diagnostics.py', 'sanitize.py', 'session_guard.py', 'transport.py', 'bank_transport.py'))
    for relative in ('freya.py', 'VERSION', 'README.md', 'docs/api.md', 'docs/workflow-format.md',
                     'docs/tools-and-code.md', 'examples/base-agent.json', 'tools/openapi.json',
                     'tools/score.py',
                     'briefs/brief-a.md', 'briefs/brief-b.md', 'public-test-set.json'):
        names.append((KIT / relative).relative_to(ROOT).as_posix())
    regression = None
    if regression_candidate is not None or regression_candidate_sha256 is not None:
        if regression_candidate is None or not isinstance(regression_candidate_sha256, str) or not re.fullmatch('[a-f0-9]{64}', regression_candidate_sha256):
            raise FoundationError('CONTEST_REGRESSION_CANDIDATE_AND_HASH_REQUIRED')
        candidate = Path(regression_candidate)
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 4_000_000:
            raise FoundationError('CONTEST_REGRESSION_CANDIDATE_UNAVAILABLE')
        candidate_raw = candidate.read_bytes()
        if digest(candidate_raw) != regression_candidate_sha256:
            raise FoundationError('CONTEST_REGRESSION_CANDIDATE_HASH_MISMATCH')
        scan_public(candidate_raw, '.json')
        from builder.models import strict_json
        graph = strict_json(candidate_raw.decode('utf-8'))
        if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), dict) or graph.get('initial_node') not in graph['nodes']:
            raise FoundationError('CONTEST_REGRESSION_GRAPH_REQUIRED')
        regression = candidate_raw
    directory.mkdir(parents=True)
    hashes = {}
    for name in sorted(set(names)):
        source = ROOT / name
        if source.is_symlink():
            raise FoundationError('CONTEST_CHECKOUT_SYMLINK_REFUSED')
        content = source.read_bytes()
        try:
            scan_public(content, source.suffix)
        except FoundationError as error:
            # This exact public negative fixture contains a deliberately fake
            # Bearer marker. Never exempt a changed file or another path.
            if not (str(error)=='DELIVERY_SENSITIVE_VALUE_REFUSED' and
                    name=='tests_orchestration/test_model_diagnostics.py' and
                    digest(content)=='1492d0493373ef8135d0dd3bd61da3d23fc3f84fbe86dae24b4076ba9088cbba'):
                raise
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[name] = digest(content)
    if regression is not None:
        fixture_name = 'tests_orchestration/inputs/candidate-regression/agent.workflow.json'
        fixture = directory / fixture_name
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(regression)
        hashes[fixture_name] = digest(regression)
        provenance_name = 'tests_orchestration/inputs/candidate-regression/provenance.json'
        write(directory / provenance_name, {'kind': 'optional-typed-regression-fixture-1',
            'candidate_sha256': regression_candidate_sha256,
            'origin': 'caller-selected generated graph; not an official final export',
            'candidate_origin_authenticated': False, 'natural_extraction_measured': False,
            'final_submission_ready': False})
        hashes[provenance_name] = digest((directory / provenance_name).read_bytes())
    (directory / 'README.md').write_bytes((ROOT / 'CONTEST-RUN.md').read_bytes())
    hashes['README.md'] = digest((directory / 'README.md').read_bytes())
    write(directory / 'SOURCE-MANIFEST.json', hashes)
    archive = directory.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(directory).as_posix())
    return {'status': 'SOURCE_CHECKOUT_PREPARED', 'directory': str(directory),
            'source_files': len(hashes), 'archive_sha256': digest(archive.read_bytes()),
            'regression_candidate_included': regression is not None,
            'regression_candidate_sha256': regression_candidate_sha256,
            'provider_exports_included': False, 'final_submission_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--regression-candidate', type=Path,
        help='Optional generated graph for typed regressions; never a final provider export.')
    parser.add_argument('--regression-candidate-sha256',
        help='Independent expected SHA256 of the optional regression graph.')
    args = parser.parse_args()
    print(json.dumps(prepare(args.out, regression_candidate=args.regression_candidate,
        regression_candidate_sha256=args.regression_candidate_sha256)))


if __name__ == '__main__':
    main()
