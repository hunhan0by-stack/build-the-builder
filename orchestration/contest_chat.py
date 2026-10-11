"""Explicitly authorized text-chat capture, never a hidden-bank practice grader.

Supply the actual organizer fixture and its independently obtained SHA-256.
This module creates no fixture, persona, simulation, bank session or ledger.
It never forces chat/end, branches a call, retries, or modifies a workflow.
Use a fresh FreyaClient and the SAME per-brief Budget as generation/publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import uuid

from builder.models import FoundationError
from orchestration.contest_chat_evidence import (
    ARTIFACT_KEYS, RUNTIME_FILES, ChatEvidence, ContestEvidenceError, NO_VERDICT,
    check_publication, check_turn, fingerprint, fixture_value, identifier,
    parse, raw, require, sha, write_new,
)
from orchestration.workspace_lock import official_workspace_mutex


ROOT = Path(__file__).resolve().parents[1]


def run_chat(*, client, publication, workspace_id, brief_path, tool_api_path,
             source_manifest_path, provider_export_path, fixture_path,
             official_fixture_sha256, generation_run_id, generation_input_sha256,
             directory, budget, live_authorized=False,
             private_directory=None, evidence_factory=ChatEvidence):
    """Bounded deterministic turns. A returned capture is never a policy PASS.

    live_authorized must reflect a NEW human authorization for this chat scope;
    authorization for an earlier public-practice test cannot be reused here.
    A custom evidence_factory is solely a test seam, not an encryption claim.
    """
    require(live_authorized is True, 'CHAT_NEW_HUMAN_AUTHORIZATION_REQUIRED')
    require(getattr(client, 'budget', None) is budget, 'CHAT_SAME_PER_BRIEF_BUDGET_REQUIRED')
    require(getattr(client, 'evidence', None) is None
            and getattr(client, 'audit_path', None) is None
            and not getattr(client, 'trace', []), 'CHAT_FRESH_CLIENT_REQUIRED')
    output = Path(directory).resolve()
    require(not output.exists(), 'CHAT_FRESH_OUTPUT_REQUIRED')
    run_id = str(uuid.uuid4())
    private = Path(private_directory or ROOT / 'integration/runs' / ('chat-' + run_id)).resolve()
    require(not private.exists() and not private.is_relative_to(output)
            and not output.is_relative_to(private), 'CHAT_SEPARATE_FRESH_PRIVATE_DIRECTORY_REQUIRED')
    output.mkdir(parents=True)
    started = budget.clock()
    trace = {'kind': 'contest-chat-capture-1', 'run_id': run_id, 'status': 'STARTED',
             'started_at_utc': datetime.now(timezone.utc).isoformat(), 'provider_requests_attempted': 0,
             'turn_responses_captured': 0, **NO_VERDICT}
    sink = None
    try:
        workspace = identifier(workspace_id)
        require(isinstance(publication, dict), 'CHAT_PUBLICATION_REQUIRED')
        binding = {name: identifier(publication.get(name))
                   for name in ('workflow_id', 'agent_id', 'version_id')}
        require(type(publication.get('version_number')) is int and publication['version_number'] > 0,
                'CHAT_VERSION_NUMBER_INVALID')
        binding.update(run_id=run_id, workspace_id=workspace,
                       version_number=publication['version_number'])
        paths = {'brief': brief_path, 'tool_api': tool_api_path, 'provider_export': provider_export_path,
                 'source_manifest': source_manifest_path, 'fixture': fixture_path}
        artifacts = {}
        for name in paths:
            path = Path(paths[name])
            require(path.is_file() and path.stat().st_size <= 4_000_000, 'CHAT_INPUT_FILE_INVALID')
            artifacts[name] = path.read_bytes()
            require(len(artifacts[name]) <= 4_000_000, 'CHAT_INPUT_FILE_INVALID')
        pins = {name: sha(content) for name, content in artifacts.items()}
        binding['generation_run_id'] = identifier(generation_run_id)
        require(isinstance(generation_input_sha256, dict)
                and set(generation_input_sha256) == {'brief', 'tool_api', 'source_manifest'}
                and all(fingerprint(value) == pins[name] for name, value in generation_input_sha256.items()),
                'CHAT_GENERATION_INPUT_HASH_MISMATCH')
        binding['generation_input_sha256'] = dict(generation_input_sha256)
        require(pins['fixture'] == fingerprint(official_fixture_sha256), 'CHAT_OFFICIAL_FIXTURE_HASH_MISMATCH')
        require(pins['provider_export'] == fingerprint(publication.get('export_sha256')),
                'CHAT_PUBLICATION_EXPORT_HASH_MISMATCH')
        require(isinstance(parse(artifacts['tool_api']), dict), 'CHAT_TOOL_API_OBJECT_REQUIRED')
        fixture, exported = fixture_value(artifacts['fixture']), parse(artifacts['provider_export'])
        source = parse(artifacts['source_manifest'])
        require(isinstance(source, dict) and source
                and isinstance(source.get('orchestration/contest_build.py'), str),
                'CHAT_GENERATION_SOURCE_MANIFEST_REQUIRED')
        from orchestration.contest_preflight import EVIDENCE_FILES
        runtime = {path.relative_to(ROOT).as_posix(): sha(path.read_bytes())
                   for package in ('builder', 'compiler', 'orchestration')
                   for path in (ROOT / package).glob('*.py')}
        runtime.update({'integration/' + name: sha((ROOT / 'integration' / name).read_bytes())
                        for name in EVIDENCE_FILES})
        kit_path = 'data/freya-hackathon/kit-v3/build-the-builder/freya.py'
        runtime[kit_path] = sha((ROOT / kit_path).read_bytes())
        require(set(RUNTIME_FILES) <= set(runtime), 'CHAT_CURRENT_RUNTIME_SOURCE_PIN_REQUIRED')
        artifacts['chat_source_manifest'] = raw(runtime)
        pins['chat_source_manifest'] = sha(artifacts['chat_source_manifest'])
        binding.update(artifact_sha256=pins, runtime_sha256=runtime)
        trace['input_sha256'] = pins
        trace['runtime_sha256'] = runtime
        budget.remaining()
        sink = evidence_factory(private, secrets=tuple(value for value in (getattr(client, 'key', None),)
                                                       if isinstance(value, str) and value))
        require(isinstance(sink, ChatEvidence), 'CHAT_SEPARATE_EVIDENCE_PROFILE_REQUIRED')
        for name, content in artifacts.items():
            sink.pin_artifact(name, content)
        client.evidence = sink
        roles = {'turns': []}

        def request(role, method, path, body=None, **params):
            budget.remaining()
            trace['provider_requests_attempted'] += 1
            value = client.api(method, path, body, **params)
            ticket = client.last_exchange
            require(ticket in sink.cache and ticket in sink.exchanges,
                    'CHAT_EXACT_WIRE_EXCHANGE_REQUIRED')
            if role == 'turns':
                roles['turns'].append(ticket)
            else:
                roles[role] = ticket
            return value

        with official_workspace_mutex(ROOT / 'integration/state', workspace):
            before = request('before_readback', 'GET', '/api/v2/workflow/' + binding['workflow_id'],
                             workspaceId=workspace)
            versions = request('before_versions', 'GET', '/api/v2/versions/workflow/' + binding['workflow_id'],
                               workspaceId=workspace)
            check_publication(binding, exported, before, versions)
            call_id, used_ids = None, set()
            for index, message in enumerate(fixture['messages']):
                body = ({'workflowId': binding['workflow_id'], 'versionId': binding['version_id'],
                         'message': message, 'customerVariables': fixture['customerVariables']}
                        if index == 0 else {'callId': call_id, 'message': message})
                response = request('turns', 'POST', '/api/v2/chat/message', body)
                # Count only completed protected responses; unknown dispatches
                # remain pending in the private recorder and cannot be replayed.
                trace['turn_responses_captured'] += 1
                call_id, _, ended = check_turn(sink.cache[roles['turns'][-1]][0], response,
                                               binding, fixture, index, call_id, used_ids)
                if ended:
                    break
            binding['call_id'] = call_id
            request('call', 'GET', '/api/v2/call/' + call_id)
            request('after_readback', 'GET', '/api/v2/workflow/' + binding['workflow_id'], workspaceId=workspace)
            request('after_versions', 'GET', '/api/v2/versions/workflow/' + binding['workflow_id'], workspaceId=workspace)
            budget.remaining()
            seal = sink.seal(binding=binding, roles=roles)
            trace.update(seal['result'], protected_evidence_sha256=seal['sha256'],
                         protection=seal['protection'])
    except Exception as error:
        if sink and not sink.sealed:
            sink.failed = True
        code = str(error) if isinstance(error, ContestEvidenceError) else ''
        # Never reflect provider/customer text, raw stderr or exception paths.
        deadline = isinstance(error, FoundationError) and str(error) == 'CONTEST_DEADLINE_EXCEEDED'
        trace.update(status='BLOCKED', diagnostic='CHAT_PER_BRIEF_DEADLINE_EXCEEDED' if deadline else
                     code if re.fullmatch(r'CHAT_[A-Z0-9_]{1,100}', code) else 'CHAT_CAPTURE_FAILED_NO_RETRY')
        if sink:
            trace['pending_exchange_retained'] = sink.pending is not None
    finally:
        trace['elapsed_seconds'] = round(budget.clock() - started, 6)
        trace['shared_budget_elapsed_seconds'] = round(budget.clock() - budget.started, 6)
        trace['shared_deadline_seconds'] = round(budget.end - budget.started, 6)
        if budget.clock() >= budget.end:
            trace.update(status='BLOCKED', diagnostic='CHAT_PER_BRIEF_DEADLINE_EXCEEDED')
        trace['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
        write_new(output / 'FINAL.json', trace)
    return trace
