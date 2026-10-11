"""Offline checks binding pilot admission reports to an actual producer export.

Passing verifies supplied evidence consistency, not all policy semantics or Freya
schema support. The coordinator must review the underlying test evidence.
"""
import hashlib
import json
from pathlib import Path

from builder.models import FoundationError, strict_json

from .session_guard import PreparationError


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_strict(path):
    try:
        return strict_json(Path(path).read_text(encoding='utf-8-sig'))
    except FoundationError:
        raise PreparationError("Evidence contains duplicate keys, non-finite numbers or invalid JSON") from None


def reject_blocked_metadata(value):
    """Inspect readiness metadata, not arbitrary code strings or prompt language."""
    if isinstance(value, list):
        for item in value:
            reject_blocked_metadata(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {'publish_allowed', 'semantic_completion', 'behavior_bindings_complete', 'is_freya_export'} and item is not True:
                raise PreparationError("Evidence explicitly blocks publication or behavioral readiness")
            if key in {'boot_gated', 'preview', 'is_preview'} and item is not False:
                raise PreparationError("Preview or boot-gated output is not pilot-ready")
            if key in {'gaps', 'policy_gaps', 'unresolved', 'unresolved_items', 'unresolved_rules', 'pending_bindings'} and item:
                raise PreparationError("Unresolved policy or behavior binding evidence remains")
            if key in {'kind', 'format_version', 'schema_version', 'status'} and isinstance(item, str):
                if 'preview' in item.lower() or item in {'blocked', 'rule_binding_pending', 'edge_semantics_pending'}:
                    raise PreparationError("Blocked preview metadata contradicts admission")
            reject_blocked_metadata(item)


def check_entry(export_path, format_report_path, safety_report_path, producer_manifest_path):
    paths = [Path(p) for p in (export_path, format_report_path, safety_report_path, producer_manifest_path)]
    if not all(p.is_file() for p in paths):
        raise PreparationError("Producer export and entry evidence are incomplete")
    export, format_report, safety_report, producer = [read_strict(p) for p in paths]
    if not all(isinstance(report, dict) for report in (format_report, safety_report, producer)):
        raise PreparationError("Admission reports must be objects")
    for record in (export, format_report, safety_report, producer):
        reject_blocked_metadata(record)
    # The current exporter writes a plain candidate.workflow.json plus a sidecar.
    # Positive replacement reports cannot erase that package's readiness veto.
    sidecar = paths[0].parent / 'export-report.json'
    if sidecar.is_file():
        reject_blocked_metadata(read_strict(sidecar))
    workflow = export.get('workflow', export) if isinstance(export, dict) else None
    if (not isinstance(workflow, dict) or workflow.get('is_freya_export') is False
            or not isinstance(workflow.get('nodes'), dict) or not workflow['nodes']
            or workflow.get('initial_node') not in workflow['nodes']):
        raise PreparationError("An abstract workflow plan is not a Freya producer export")
    if workflow['initial_node'] == 'export_bootstrap' and 'export_blocked' in workflow['nodes']:
        raise PreparationError("Current exporter's blocked bootstrap is not an admitted behavior plan")
    sha = file_hash(paths[0])
    if any(report.get('export_sha256') != sha for report in (format_report, safety_report, producer)):
        raise PreparationError("Admission reports do not bind to the same export bytes")
    if format_report.get('local_format_valid') is not True:
        raise PreparationError("Local format evidence did not pass")
    checks = ('identity_before_account', 'current_consent_before_write', 'confirmed_success_before_claim',
              'bounded_write_attempts', 'close_once_last')
    if any(safety_report.get(key) is not True for key in checks):
        raise PreparationError("Identity/consent/write/closure evidence is incomplete")
    if (producer.get('publish_allowed') is not True or producer.get('semantic_completion') is not True
            or producer.get('behavior_bindings_complete') is not True
            or safety_report.get('behavior_bindings_complete') is not True):
        raise PreparationError("Positive producer policy/behavior readiness evidence is missing")
    if producer.get('kind') != 'producer_export' or not producer.get('trace_sha256'):
        raise PreparationError("Actual producer run and trace evidence are missing")
    trace_relative = Path(producer.get('trace_path', ''))
    if trace_relative.is_absolute() or set(part.lower() for part in trace_relative.parts) & {
            '..', '.freya-local', '.aws', '.codex', '.agents', '.git'}:
        raise PreparationError("Producer trace path must stay inside its evidence package")
    trace = (paths[3].parent / trace_relative).resolve()
    if not trace.is_relative_to(paths[3].parent.resolve()):
        raise PreparationError("Producer trace path escapes its evidence package")
    if not trace.is_file() or file_hash(trace) != producer['trace_sha256']:
        raise PreparationError("Producer trace integrity failed")
    return {'admission_evidence_consistent': True, 'export_sha256': sha,
            'platform_validated': False, 'live_execution_authorized': False,
            'note': 'Coordinator still binds budget, workspace slot and current capacity before live execution.'}


def check_practice_entry(package, *, case_id='PUB-01'):
    """Explicitly scoped pilot admission; never promotes the final/general gate."""
    from builder.freya_export import validate_local_workflow
    root = Path(__file__).resolve().parents[1]
    package = Path(package).resolve()
    if not package.is_relative_to(root / 'artifacts/export-preview') or case_id != 'PUB-01':
        raise PreparationError('Only the authorized A/PUB-01 practice pilot is supported')
    workflow = read_strict(package / 'candidate.workflow.json')
    report = read_strict(package / 'export-report.json')
    if (report.get('format_version') != 'freya-practice-replay-2'
            or report.get('practice_only') is not True or report.get('executable_candidate') is not True
            or report.get('boot_gated') is not False or workflow.get('initial_node') != 'bridge_init'):
        raise PreparationError('Candidate is a blocked preview or lacks an executable practice binding')
    ready = read_strict(package / 'practice-ready.json')
    evidence = read_strict(package / 'preflight-evidence.json')
    scope = 'controlled_synthetic_practice_pilot_only'
    if (report.get('publish_allowed') is not True or report.get('ready_for_platform_preflight') is not True
            or report.get('publication_scope') != scope or report.get('practice_pilot_allowed') is not True):
        raise PreparationError('Producer does not explicitly allow the scoped practice preflight')
    fields = {'status': 'ready_for_controlled_practice_preflight', 'practice_profile': 'A',
              'ready_for_platform_preflight': True, 'publish_allowed': True, 'publication_scope': scope,
              'practice_pilot_allowed': True, 'first_pilot_max_cases': 1, 'recommended_first_case': 'PUB-01',
              'simulation_preflight_verified': False, 'simulation_allowed_before_preflight': False,
              'hard_export_blockers': []}
    for record in (ready, report.get('readiness', {})):
        if not isinstance(record, dict) or any(type(record.get(key)) is not type(expected)
                                               or record.get(key) != expected for key, expected in fields.items()):
            raise PreparationError('Practice readiness sidecar contradicts the single-case preflight scope')
    tests = evidence.get('local_tests', {})
    smoke = evidence.get('exact_candidate_python_smoke', {})
    if (evidence.get('ready_for_controlled_platform_preflight') is not True
            or evidence.get('publication_scope') != scope or evidence.get('publish_allowed') is not True
            or type(evidence.get('practice_pilot_case_limit')) is not int
            or evidence.get('practice_pilot_case_limit') != 1 or evidence.get('recommended_case') != 'PUB-01'
            or evidence.get('simulation_allowed_before_preflight') is not False
            or evidence.get('simulation_preflight_verified') is not False
            or evidence.get('artifact_hashes_verified') is not True
            or evidence.get('core_snapshot_matches_current') is not True
            or tests.get('passed') is not True or tests.get('failures') != 0 or tests.get('errors') != 0
            or type(tests.get('tests_run')) is not int or tests['tests_run'] < 1
            or smoke.get('passed') is not True or smoke.get('final_route') != 'bridge_end'
            or smoke.get('operations') != ['verify_identity', 'verify_identity', 'get_account', 'create_promise', 'close_call']):
        raise PreparationError('Matching local execution evidence is incomplete or grants unsafe simulation permission')
    semantics = report.get('semantics', {})
    if semantics.get('source_version_admitted') is not True or semantics.get('complete_for_supplied_practice') is not True:
        raise PreparationError('Reviewed practice policy binding is incomplete')
    allowed_risks = {'general_final_compiler_pending', 'sandbox_dependencies_unverified',
                     'platform_execution_unverified', 'free_text_extraction_unverified',
                     'between_write_interrupts_unverified', 'unprovided_error_policy'}
    gaps = report.get('gaps')
    if not isinstance(gaps, list) or any(not isinstance(gap, dict) or gap.get('code') not in allowed_risks
                                         or gap.get('blocks_practice_preflight') is not False for gap in gaps):
        raise PreparationError('Unreviewed policy/behavior gap blocks the practice pilot')
    provenance = report.get('provenance', {})
    canonical = hashlib.sha256(json.dumps(workflow, sort_keys=True, ensure_ascii=False,
                                          allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    if canonical != provenance.get('workflow_sha256'):
        raise PreparationError('Practice workflow does not match its producer provenance')
    if (ready.get('candidate_workflow_sha256') != canonical or ready.get('provenance') != provenance
            or evidence.get('candidate_file_sha256') != file_hash(package / 'candidate.workflow.json')
            or evidence.get('candidate_canonical_sha256') != canonical):
        raise PreparationError('Practice sidecars are not bound to the same candidate')
    trace_path = package / 'trace.jsonl'
    trace = [strict_json(line) for line in trace_path.read_text(encoding='utf-8-sig').splitlines() if line]
    export_event = next((row for row in trace if row.get('stage') == 'export'), {})
    for name in ('candidate.workflow.json', 'export-report.json', 'practice-ready.json'):
        if export_event.get('artifact_sha256', {}).get(name) != file_hash(package / name):
            raise PreparationError('Producer trace and practice package bytes disagree')
    snapshots = provenance.get('core_source_sha256', {})
    names = {'models.py', 'calendar.py', 'api_contract.py', 'runtime.py', 'policy_specs.py',
             'semantics.py', 'turns.py', 'conversation.py'}
    if set(snapshots) != names or any(file_hash(root / 'builder' / name) != sha for name, sha in snapshots.items()):
        raise PreparationError('Practice core snapshot is stale')
    upstream = Path(provenance.get('upstream_trace', {}).get('reference', '')).resolve()
    if not upstream.is_relative_to(root / 'artifacts') or file_hash(upstream) != provenance.get('upstream_trace', {}).get('sha256'):
        raise PreparationError('Upstream producer trace is missing, changed or outside artifacts')
    if file_hash(upstream.parent / 'run-manifest.json') != provenance.get('upstream_manifest_sha256'):
        raise PreparationError('Upstream producer manifest changed')
    for relative, sha in provenance.get('input_file_sha256', {}).items():
        target = (upstream.parent / relative).resolve()
        if not target.is_relative_to(upstream.parent) or file_hash(target) != sha:
            raise PreparationError('Upstream source/plan/input bytes changed')
    kit = root / 'data/freya-hackathon/kit-v3/build-the-builder'
    a_sha = file_hash(kit / 'briefs/brief-a.md')
    policy_sources = [row.get('sha256') for row in provenance.get('sources', []) if row.get('role') == 'policy']
    if policy_sources != [a_sha]:
        raise PreparationError('Pilot must bind only the reviewed Brief A policy')
    validation = validate_local_workflow(workflow)
    if validation.get('documented_subset_valid') is not True:
        raise PreparationError('Local workflow shape did not pass')
    for name, node in workflow['nodes'].items():
        for tool in node.get('pre_actions', []) + node.get('tools', []):
            fn = tool.get('function', {})
            if fn.get('handler') == '__function__:api_call':
                expected = 'https://freya-bank-mock.freyavoice.workers.dev/v1/' + fn.get('name', '')
                actual = fn.get('parameters', {}).get('properties', {}).get('url', {}).get('default')
                if actual != expected or tool.get('config', {}).get('maxRetries') != 0 or not tool.get('pre_process_code'):
                    raise PreparationError('Bank action URL, write retries or guarded arguments are unsafe')
    return {'practice_admission_consistent': True, 'scope': 'A/PUB-01 single pilot only',
            'export_sha256': file_hash(package / 'candidate.workflow.json'),
            'report_sha256': file_hash(package / 'export-report.json'), 'trace_sha256': file_hash(trace_path),
            'ready_for_platform_preflight': True, 'simulation_allowed_now': False,
            'final_admission': False, 'platform_validated': False,
            'remaining_risks': [row['code'] for row in gaps]}
