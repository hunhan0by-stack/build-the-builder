"""Read-only pairing of two selected final Builder runs and Freya exports.

The selection digest is supplied independently by a reviewer. It identifies the
official input files and selected run witnesses; this module does not discover
hidden briefs, authenticate HTTP transport, grade calls, publish or read keys.
Structured trace/receipt fields below are our local pairing format, not an
additional Freya trace-format requirement. Kit v3 permits plain-text traces.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import uuid

from builder.models import FoundationError, strict_json

MAX_FILE_BYTES = 4_000_000
FILE_FIELDS = ('export', 'trace', 'build_receipt', 'workflow_readback',
               'versions_readback', 'generated_workflow')


def _fail(code):
    raise FoundationError('FINAL_PAIRING_' + code)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        _fail('HASH_INVALID')
    return value


def _commit(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
        _fail('COMMIT_INVALID')
    return value


def _name(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        _fail('PATH_INVALID')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or not path.parts
            or any(part in ('', '.', '..', '.git', '.freya-local')
                   or part.lower().startswith('.env') or part.lower().endswith('.dpapi')
                   or part.lower() == 'env.sh' for part in path.parts)):
        _fail('PATH_INVALID')
    return value


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int
    except ValueError:
        valid = False
    if not valid:
        _fail('PROVIDER_ID_INVALID')
    return value


def _read(path):
    path = Path(path)
    if any(part in ('.git', '.freya-local') or part.lower().startswith('.env')
           or part.lower().endswith('.dpapi') or part.lower() == 'env.sh' for part in path.parts):
        _fail('PRIVATE_FILE_FORBIDDEN')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        _fail('FILE_UNAVAILABLE')
    return path.read_bytes()


def _json(raw):
    value = strict_json(raw.decode('utf-8'))
    if not isinstance(value, dict):
        _fail('OBJECT_REQUIRED')
    return value


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        _fail('TIME_INVALID')
    if result.tzinfo is None:
        _fail('TIME_INVALID')
    return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode()


def _full_bank_surface(workflow, api):
    """Minimum declared banking surface; no claim about code-hook semantics."""
    try:
        base = api['servers'][0]['url'].rstrip('/')
        urls = {base + path: path.removeprefix('/') for path, item in api['paths'].items()
                if isinstance(item, dict) and 'post' in item}
        declared = set()
        for group in ('nodes', 'global_nodes'):
            for node in workflow.get(group, {}).values():
                for tool in node.get('pre_actions', []) + node.get('tools', []):
                    fn = tool['function']
                    if fn.get('handler') == '__function__:api_call':
                        props = fn['parameters']['properties']
                        if props['method']['default'] != 'POST' or props['url']['default'] not in urls:
                            _fail('NON_BANK_API_SURFACE')
                        declared.add(urls[props['url']['default']])
        if not {'verify_identity', 'get_account', 'create_promise', 'close_call'} <= declared:
            _fail('DIAGNOSTIC_OR_READ_ONLY_PREFIX')
    except (KeyError, IndexError, TypeError, AttributeError):
        _fail('BANK_SURFACE_INVALID')
    return sorted(declared)


def verify_pairing(package_directory, selection_path, *, trusted_selection_sha256, expected_commit):
    """Verify all file bytes and cross-bind selected run/inputs/versions/exports."""
    expected_commit = _commit(expected_commit)
    explicit = Path(package_directory)
    package = explicit.resolve()
    selection_file = Path(selection_path)
    if (explicit.is_symlink() or not package.is_dir()
            or selection_file.resolve().is_relative_to(package)):
        _fail('INDEPENDENT_SELECTION_REQUIRED')
    # Reject private paths without opening them or following package links.
    _name(selection_file.name)
    selection_raw = _read(selection_file)
    if _digest(selection_raw) != _hash(trusted_selection_sha256):
        _fail('SELECTION_REFERENCE_MISMATCH')
    selection = _json(selection_raw)
    if (selection.get('version') != 'final-run-selection-1'
            or selection.get('input_origin') != 'official_final'
            or selection.get('commit_sha') != expected_commit):
        _fail('OFFICIAL_SELECTION_REQUIRED')
    manifest_raw = _read(package / 'manifest.json')
    if _digest(manifest_raw) != _hash(selection['manifest_sha256']):
        _fail('MANIFEST_REFERENCE_MISMATCH')
    manifest = _json(manifest_raw)
    if manifest.get('commit_sha') != expected_commit or manifest.get('version') != 'final-run-package-1':
        _fail('PACKAGE_COMMIT_MISMATCH')
    contents = {}
    for row in manifest['files']:
        name = _name(row['path'])
        path = package / name
        if name in contents or path.is_symlink() or not path.resolve().is_relative_to(package):
            _fail('PACKAGE_PATH_INVALID')
        raw = _read(path)
        if type(row['bytes']) is not int or len(raw) != row['bytes'] or _digest(raw) != _hash(row['sha256']):
            _fail('FILE_HASH_MISMATCH')
        contents[name] = raw
    actual = set()
    for path in package.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(package):
            _fail('PACKAGE_PATH_INVALID')
        if path.is_file():
            actual.add(_name(path.relative_to(package).as_posix()))
    if actual != set(contents) | {'manifest.json'}:
        _fail('PACKAGE_INVENTORY_MISMATCH')
    descriptor = _json(contents['pairing.json'])
    if descriptor.get('version') != manifest['version'] or descriptor.get('commit_sha') != expected_commit:
        _fail('PACKAGE_COMMIT_MISMATCH')
    pairs, selected = descriptor.get('pairs'), selection.get('pairs')
    if not isinstance(pairs, list) or len(pairs) != 2 or not isinstance(selected, list) or len(selected) != 2:
        _fail('TWO_FINAL_RUNS_REQUIRED')
    selected_map = {row['brief_id']: row for row in selected}
    if len(selected_map) != 2 or {row['brief_id'] for row in pairs} != set(selected_map):
        _fail('BRIEF_SELECTION_MISMATCH')
    results, run_ids, used = [], set(), {'pairing.json'}
    for pair in pairs:
        chosen = selected_map[pair['brief_id']]
        if chosen.get('input_origin') != 'official_final':
            _fail('OFFICIAL_SELECTION_REQUIRED')
        sources = pair['briefs']
        if not isinstance(sources, list) or not sources or len(sources) > 32 or len(set(sources)) != len(sources):
            _fail('BRIEF_FILES_INVALID')
        wanted = chosen['brief_sha256']
        if set(sources) != set(wanted):
            _fail('BRIEF_HASH_MISMATCH')
        for name in sources:
            if _digest(contents[_name(name)]) != _hash(wanted[name]):
                _fail('BRIEF_HASH_MISMATCH')
            used.add(name)
        api_name = _name(pair['tool_api'])
        if _digest(contents[api_name]) != _hash(chosen['tool_api_sha256']):
            _fail('TOOL_API_HASH_MISMATCH')
        used.add(api_name)
        witnesses = {}
        for field in FILE_FIELDS:
            name = _name(pair[field])
            if _digest(contents[name]) != _hash(chosen[field + '_sha256']):
                _fail('SELECTED_' + field.upper() + '_MISMATCH')
            witnesses[field] = _json(contents[name])
            used.add(name)
        receipt, trace = witnesses['build_receipt'], witnesses['trace']
        run_id = _uuid(chosen['run_id'])
        if run_id in run_ids:
            _fail('DISTINCT_BUILDER_RUNS_REQUIRED')
        run_ids.add(run_id)
        for record, kind in ((receipt, 'official_final_builder_run'), (trace, 'official_final_builder_trace')):
            if (record.get('kind') != kind or record.get('input_origin') != 'official_final'
                    or record.get('scope') != 'full_final_phone_agent'
                    or record.get('run_id') != run_id or record.get('commit_sha') != expected_commit
                    or record.get('brief_id') != pair['brief_id'] or record.get('manual_agent_edits') is not False):
                _fail('SELECTED_BUILD_RUN_MISMATCH')
        if (trace.get('brief_sha256') != wanted
                or trace.get('tool_api_sha256') != chosen['tool_api_sha256']
                or receipt.get('trace_sha256') != chosen['trace_sha256']):
            _fail('TRACE_INPUT_BINDING_MISMATCH')
        elapsed = trace.get('elapsed_seconds')
        wall = (_timestamp(trace['ended_at_utc']) - _timestamp(trace['started_at_utc'])).total_seconds()
        if (type(elapsed) not in (int, float) or not math.isfinite(elapsed)
                or not 0 <= elapsed <= 1800 or not 0 <= wall <= 1800
                or trace.get('includes_agent_tests_in_elapsed') is not True):
            _fail('THIRTY_MINUTE_LIMIT')
        if not all(isinstance(trace.get(field), list) for field in ('model_calls', 'tests', 'freya_actions')):
            _fail('BUILDER_TRACE_INCOMPLETE')
        for model_call in trace['model_calls']:
            if (not isinstance(model_call, dict)
                    or not isinstance(model_call.get('model'), str) or not model_call['model']
                    or not isinstance(model_call.get('purpose'), str) or not model_call['purpose']):
                _fail('MODEL_TRACE_INCOMPLETE')
        if not all(isinstance(action, dict) for action in trace['freya_actions']):
            _fail('BUILDER_TRACE_INCOMPLETE')
        workflow_id, agent_id, version_id = (_uuid(chosen[key]) for key in ('workflow_id', 'agent_id', 'version_id'))
        version = chosen['version_number']
        if type(version) is not int or version < 1:
            _fail('VERSION_INVALID')
        binding = {'workflow_id': workflow_id, 'agent_id': agent_id,
                   'version_id': version_id, 'version_number': version}
        if receipt.get('selected_publication') != binding:
            _fail('SELECTED_PUBLICATION_MISMATCH')
        if not any(action.get('kind') == 'export' and action.get('publication') == binding
                   for action in trace['freya_actions']):
            _fail('EXPORT_NOT_IN_SELECTED_TRACE')
        exported, readback = witnesses['export'], witnesses['workflow_readback'].get('workflow', {})
        # Exact wrapper written by kit-v3 freya.export_workflow; no local plain
        # workflow/pyz or diagnostic report can stand in for that export.
        if (set(exported) != {'workflow_id', 'agent_id', 'updated_at', 'workflow'}
                or exported['workflow_id'] != workflow_id or exported['agent_id'] != agent_id
                or readback.get('id') != workflow_id or readback.get('agentId') != agent_id
                or exported['updated_at'] != readback.get('updatedAt')
                or _canonical(exported['workflow']) != _canonical(readback.get('workflow'))
                or _canonical(exported['workflow']) != _canonical(witnesses['generated_workflow'])):
            _fail('FREYA_EXPORT_READBACK_MISMATCH')
        versions = witnesses['versions_readback']['versions']
        if (not isinstance(versions, list) or not versions
                or any(not isinstance(row, dict) or type(row.get('versionNumber')) is not int
                       or row['versionNumber'] < 1 for row in versions)):
            _fail('PUBLISHED_VERSION_MISMATCH')
        published = [row for row in versions if row.get('isPublished') is True]
        # The kit export helper reads the workflow's current JSON, not a pinned
        # version endpoint. A newer unpublished version makes that pairing
        # ambiguous, so do not certify it as the selected published export.
        if (len(published) != 1 or published[0].get('id') != version_id
                or published[0].get('versionNumber') != version
                or max(row['versionNumber'] for row in versions) != version):
            _fail('PUBLISHED_VERSION_MISMATCH')
        model = chosen['agent_model']
        actual_model = exported['workflow'].get('agent_config', {}).get('llmConfig', {})
        if {key: actual_model.get(key) for key in ('provider', 'model')} != model:
            _fail('AGENT_MODEL_MISMATCH')
        results.append({'brief_id': pair['brief_id'], 'selected_run_id': run_id, **binding,
                        'export_sha256': chosen['export_sha256'], 'trace_sha256': chosen['trace_sha256'],
                        'elapsed_seconds': elapsed, 'declared_bank_operations':
                        _full_bank_surface(exported['workflow'], _json(contents[api_name]))})
    if used != set(contents):
        _fail('UNPAIRED_FILE')
    return {'status': 'FINAL_ARTIFACT_PAIRING_VERIFIED', 'valid': True, 'commit_sha': expected_commit,
            'pairs': results, 'independent_selection_sha256': trusted_selection_sha256,
            'network_calls': 0, 'credentials_read': False, 'git_writes': 0,
            'provider_transport_authenticated_by_this_module': False,
            'git_commit_authenticated_by_this_module': False,
            'official_call_evaluation_verified': False, 'final_submission_ready': False,
            'scope': 'Artifact/run pairing against reviewer-selected official input and witness hashes; no call grading or native safety certification'}


def verify_contest_pairing(package_directory, selection_path, *, trusted_selection_sha256, expected_commit):
    """Read original contest traces directly, without synthesizing legacy events.

    Independent selection supplies official input origin and commit/source pins.
    The builder's publication receipt is a reported fact, not authenticated HTTP
    evidence. Unlike the legacy format, no absent readback/version file is invented.
    """
    expected_commit = _commit(expected_commit)
    explicit = Path(package_directory)
    package = explicit.resolve()
    selection_file = Path(selection_path)
    if explicit.is_symlink() or not package.is_dir() or selection_file.resolve().is_relative_to(package):
        _fail('INDEPENDENT_SELECTION_REQUIRED')
    selected_raw = _read(selection_file)
    if _digest(selected_raw) != _hash(trusted_selection_sha256):
        _fail('SELECTION_REFERENCE_MISMATCH')
    selection = _json(selected_raw)
    if (selection.get('version') != 'contest-final-selection-1' or
            selection.get('input_origin') != 'official_final' or selection.get('commit_sha') != expected_commit):
        _fail('OFFICIAL_SELECTION_REQUIRED')
    pairs = selection.get('pairs')
    if not isinstance(pairs, list) or len(pairs) != 2:
        _fail('TWO_FINAL_RUNS_REQUIRED')
    results, runs, directories, briefs = [], set(), set(), set()
    shared_source_pins = None
    required = ('trace.json', 'FINAL.json', 'manifest.json', 'input-brief.md',
                'input-api.json', 'agent.workflow.json', 'provider-export.json', 'runtime-source-manifest.json')
    statuses = {'PROVIDER_EXPORTED_TESTS_REQUIRED', 'PRACTICE_CASE_PASSED',
                'PRACTICE_CASE_FAILED', 'CHAT_CONVERSATION_CAPTURED'}
    for chosen in pairs:
        if chosen.get('input_origin') != 'official_final' or chosen.get('manual_agent_edits') is not False:
            _fail('OFFICIAL_SELECTION_REQUIRED')
        label = _name(chosen['brief_id'])
        directory_name = _name(chosen['run_directory'])
        directory = package / directory_name
        if label in briefs or directory_name in directories or directory.is_symlink() or not directory.resolve().is_relative_to(package):
            _fail('DISTINCT_BUILDER_RUNS_REQUIRED')
        directories.add(directory_name)
        briefs.add(label)
        content = {name: _read(directory / name) for name in required}
        wanted = chosen.get('files_sha256')
        if not isinstance(wanted, dict) or set(wanted) != set(required):
            _fail('CONTEST_SELECTION_FILES_REQUIRED')
        for name, raw in content.items():
            if _digest(raw) != _hash(wanted[name]):
                _fail('SELECTED_CONTEST_FILE_MISMATCH')
        manifest = _json(content['manifest.json'])
        # Current runner's manifest excludes itself and its subsequently written FINAL.
        for name, fingerprint in manifest.items():
            path = directory / _name(name)
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                _fail('PACKAGE_PATH_INVALID')
            if _digest(_read(path)) != _hash(fingerprint):
                _fail('FILE_HASH_MISMATCH')
        actual = set()
        for path in directory.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                _fail('PACKAGE_PATH_INVALID')
            if path.is_file():
                actual.add(_name(path.relative_to(directory).as_posix()))
        if actual != set(manifest) | {'manifest.json', 'FINAL.json'} or not set(required) - {'manifest.json', 'FINAL.json'} <= set(manifest):
            _fail('PACKAGE_INVENTORY_MISMATCH')
        final, trace = _json(content['FINAL.json']), _json(content['trace.json'])
        run_id = _uuid(trace['run_id'])
        if run_id in runs or run_id != _uuid(chosen['run_id']):
            _fail('DISTINCT_BUILDER_RUNS_REQUIRED')
        runs.add(run_id)
        if (trace.get('kind') != 'contest-builder-run-1' or trace.get('status') not in statuses or
                final.get('status') != trace['status'] or final.get('manifest_sha256') != _digest(content['manifest.json'])):
            _fail('CONTEST_TERMINAL_EXPORT_REQUIRED')
        if trace.get('inputs', {}).get('brief_sha256') != _digest(content['input-brief.md']) or trace.get('inputs', {}).get('tool_api_sha256') != _digest(content['input-api.json']):
            _fail('TRACE_INPUT_BINDING_MISMATCH')
        source_pins = chosen.get('committed_runtime_sha256')
        if not isinstance(source_pins, dict) or not source_pins:
            _fail('COMMITTED_RUNTIME_PINS_REQUIRED')
        for name, fingerprint in source_pins.items():
            _name(name)
            _hash(fingerprint)
        if shared_source_pins is None:
            shared_source_pins = source_pins
        elif source_pins != shared_source_pins:
            _fail('CONTEST_SOURCE_COMMIT_BINDING_MISMATCH')
        if trace.get('runtime_sha256') != source_pins or _json(content['runtime-source-manifest.json']) != source_pins:
            _fail('CONTEST_SOURCE_COMMIT_BINDING_MISMATCH')
        elapsed = trace.get('elapsed_seconds')
        wall = (_timestamp(trace['ended_at']) - _timestamp(trace['started_at'])).total_seconds()
        deadline = trace.get('deadline_seconds')
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or not 0 < deadline <= 1800 or
                type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= deadline or
                not 0 <= wall <= 1800):
            _fail('THIRTY_MINUTE_LIMIT')
        compilation = trace.get('compilation', {})
        calls, attempts = compilation.get('calls'), compilation.get('attempts', [])
        if type(calls) is not int or calls < 0 or not isinstance(attempts, list) or len(attempts) != calls:
            _fail('MODEL_TRACE_INCOMPLETE')
        for attempt in attempts:
            if not isinstance(attempt, dict) or not all(isinstance(attempt.get(key), str) and attempt[key] for key in ('model_requested', 'purpose')):
                _fail('MODEL_TRACE_INCOMPLETE')
        actions, tests = trace.get('provider_actions'), trace.get('tests')
        if not isinstance(actions, list) or not isinstance(tests, list) or not all(isinstance(item, dict) for item in actions + tests):
            _fail('BUILDER_TRACE_INCOMPLETE')
        publication = trace.get('publication', {})
        binding = {key: _uuid(publication[key]) for key in ('workflow_id', 'agent_id', 'version_id')}
        version = publication.get('version_number')
        if type(version) is not int or version < 1 or publication.get('status') != 'EXPORTED' or publication.get('export_sha256') != _digest(content['provider-export.json']):
            _fail('CONTEST_PUBLICATION_MISMATCH')
        if any(chosen.get(key) != value for key, value in {**binding, 'version_number': version}.items()):
            _fail('SELECTED_PUBLICATION_MISMATCH')
        exported = _json(content['provider-export.json'])
        if (set(exported) != {'workflow_id', 'agent_id', 'updated_at', 'workflow'} or
                any(exported.get(key) != binding[key] for key in ('workflow_id', 'agent_id'))):
            _fail('FREYA_EXPORT_WRAPPER_MISMATCH')
        model = exported['workflow'].get('agent_config', {}).get('llmConfig', {})
        if {key: model.get(key) for key in ('provider', 'model')} != {'provider': 'experimental-5', 'model': 'experimental-5'}:
            _fail('AGENT_MODEL_MISMATCH')
        # Reuse the runner's documented allowance for inert platform defaults.
        from orchestration.contest_build import verify_provider_graph
        verify_provider_graph(_json(content['agent.workflow.json']), exported['workflow'])
        workflow_path = '/api/v2/workflow/' + binding['workflow_id']
        publish_path = '/api/v2/versions/workflow/' + binding['workflow_id'] + '/publish'
        position = 0
        for method, path in (('PUT', workflow_path), ('POST', publish_path),
                             ('GET', workflow_path), ('GET', '/api/v2/versions/workflow/' + binding['workflow_id'])):
            match = next((index for index in range(position, len(actions)) if
                          actions[index].get('method') == method and actions[index].get('path') == path and
                          actions[index].get('status') == 'RESPONSE_RECEIVED'), None)
            if match is None:
                _fail('CONTEST_PUBLICATION_ACTIONS_INCOMPLETE')
            position = match + 1
        results.append({'brief_id': label, 'run_id': run_id, **binding, 'version_number': version,
            'trace_sha256': wanted['trace.json'], 'export_sha256': wanted['provider-export.json'],
            'elapsed_seconds': elapsed, 'model_calls_recorded': calls,
            'builder_reported_model_independently_observed': compilation.get('model_independently_observed') is True,
            'model_independently_observed_by_this_module': False,
            'declared_bank_operations': _full_bank_surface(exported['workflow'], _json(content['input-api.json']))})
    return {'status': 'CONTEST_ARTIFACT_PAIRING_VERIFIED', 'valid': True, 'commit_sha': expected_commit,
        'pairs': results, 'independent_selection_sha256': trusted_selection_sha256,
        'original_trace_bytes_preserved': True, 'events_synthesized': 0, 'network_calls': 0,
        'credentials_read': False, 'git_writes': 0, 'final_submission_ready': False,
        'provider_transport_authenticated_by_this_module': False, 'git_commit_authenticated_by_this_module': False,
        'published_version_readback_independently_verified': False, 'official_call_evaluation_verified': False,
        'scope': 'Original contest trace, source/input/export pairing only; publication/version are builder-reported, no missing wire evidence or live verdict invented'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--selection', required=True, type=Path)
    parser.add_argument('--trusted-selection-sha256', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--trace-format', choices=('legacy', 'contest'), default='legacy')
    args = parser.parse_args(argv)
    try:
        verifier = verify_contest_pairing if args.trace_format == 'contest' else verify_pairing
        result = verifier(args.package, args.selection,
            trusted_selection_sha256=args.trusted_selection_sha256, expected_commit=args.expected_commit)
    except (FoundationError, KeyError, OSError, ValueError, TypeError, AttributeError) as error:
        result = {'valid': False, 'status': 'BLOCKED', 'diagnostic':
                  str(error) if isinstance(error, FoundationError) else type(error).__name__,
                  'final_submission_ready': False, 'network_calls': 0, 'credentials_read': False}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result['valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
