"""Separate protected text-chat evidence; no practice, grading or retry authority.

The inherited recorder uses Windows-user DPAPI or explicit optional Fernet.
The chat join is deliberately separate from the seven-role public-practice join.
Injected protectors are labelled unverified and are intended for synthetic tests.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from builder.models import strict_json
from integration.contest_evidence import ContestEvidence, ContestEvidenceError
from integration.private_state import protect_bytes, unprotect_bytes, ciphertext_backend


ARTIFACT_KEYS = ('brief', 'tool_api', 'provider_export', 'source_manifest', 'fixture', 'chat_source_manifest')
ROLE_KEYS = ('before_readback', 'before_versions', 'call', 'after_readback', 'after_versions')
ID_KEYS = ('run_id', 'workspace_id', 'workflow_id', 'agent_id', 'version_id', 'call_id')
RUNTIME_FILES = ('orchestration/contest_chat.py', 'orchestration/contest_chat_evidence.py',
                 'orchestration/contest_build.py')
EVENT_MODEL = {'provider': 'experimental-5', 'model': 'experimental-5'}
NO_VERDICT = {'ledger_verdict': 'NOT_MEASURED', 'policy_verdict': 'NOT_MEASURED',
              'runtime_model_observation': 'NOT_MEASURED', 'speech_completion_proven': False,
              'bank_write_verified': False, 'live_success_verified': False,
              'final_submission_ready': False, 'may_retry': False,
              'controller_end_dispatched': False}


def require(ok, code):
    if not ok:
        raise ContestEvidenceError(code)


def raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def parse(body):
    try:
        return strict_json(body.decode('utf-8'))
    except Exception:
        raise ContestEvidenceError('CHAT_JSON_INVALID') from None


def identifier(value):
    try:
        parsed = uuid.UUID(value)
        valid = isinstance(value, str) and parsed.int != 0 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    require(valid, 'CHAT_CANONICAL_ID_REQUIRED')
    return value


def fingerprint(value):
    require(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value),
            'CHAT_SHA256_REQUIRED')
    return value


def write_new(path, value):
    with Path(path).open('xb') as stream:
        stream.write(raw(value) + b'\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())


def fixture_value(body):
    """Only explicitly supplied fixtures; no customer or scenario synthesis."""
    require(isinstance(body, bytes) and len(body) <= 100_000, 'CHAT_FIXTURE_TOO_LARGE')
    value = parse(body)
    require(isinstance(value, dict) and set(value) == {'customerVariables', 'messages'},
            'CHAT_OFFICIAL_FIXTURE_FIELDS_REQUIRED')
    variables, messages = value['customerVariables'], value['messages']
    require(isinstance(variables, dict) and isinstance(variables.get('customer_id'), str)
            and 0 < len(variables['customer_id']) <= 512 and len(raw(variables)) <= 64_000,
            'CHAT_OFFICIAL_CUSTOMER_VARIABLES_REQUIRED')
    require(isinstance(messages, list) and 1 <= len(messages) <= 32
            and all(isinstance(message, str) and 1 <= len(message) <= 10_000
                    for message in messages), 'CHAT_DETERMINISTIC_MESSAGES_REQUIRED')
    return value


def check_publication(binding, export, readback, versions):
    workflow = readback.get('workflow') if isinstance(readback, dict) else None
    require(isinstance(workflow, dict) and workflow.get('id') == binding['workflow_id']
            and workflow.get('agentId') == binding['agent_id']
            and isinstance(export, dict) and export.get('workflow_id') == binding['workflow_id']
            and export.get('agent_id') == binding['agent_id']
            and isinstance(export.get('workflow'), dict)
            and workflow.get('workflow') == export['workflow'], 'CHAT_EXPORT_READBACK_MISMATCH')
    graph = workflow['workflow']
    config = graph.get('agent_config', {}).get('llmConfig', {})
    require(isinstance(config, dict) and all(config.get(key) == value
            for key, value in EVENT_MODEL.items()), 'CHAT_EVENT_MODEL_CONFIG_REQUIRED')
    values = versions.get('versions') if isinstance(versions, dict) else None
    require(isinstance(values, list) and len([row for row in values if isinstance(row, dict)
            and row.get('id') == binding['version_id']
            and type(row.get('versionNumber')) is int
            and row.get('versionNumber') == binding['version_number']
            and row.get('isPublished') is True]) == 1, 'CHAT_SELECTED_PUBLISHED_VERSION_REQUIRED')
    return graph


def checked_response(pair):
    request, response = pair
    require(type(response.get('http_status')) is int
            and 200 <= response['http_status'] < 300, 'CHAT_HTTP_RESULT_NOT_SUCCESS')
    envelope = parse(response['body'])
    require(isinstance(envelope, dict) and envelope.get('success') is True
            and isinstance(envelope.get('data'), dict)
            and isinstance(envelope['data'].get('result'), dict), 'CHAT_SUCCESS_ENVELOPE_REQUIRED')
    return request, envelope['data']['result']


def check_turn(request, result, binding, fixture, index, call_id, used_ids):
    require(request['method'] == 'POST' and request['path'] == '/api/v2/chat/message'
            and request['params'] == {}, 'CHAT_TURN_REQUEST_MISMATCH')
    body = parse(base64.b64decode(request['body_base64'], validate=True))
    expected = ({'workflowId': binding['workflow_id'], 'versionId': binding['version_id'],
                 'message': fixture['messages'][0], 'customerVariables': fixture['customerVariables']}
                if index == 0 else {'callId': call_id, 'message': fixture['messages'][index]})
    require(body == expected, 'CHAT_TURN_FIXTURE_OR_CALL_MISMATCH')
    received = identifier(result.get('callId'))
    require(call_id is None or received == call_id, 'CHAT_RESPONSE_FOREIGN_CALL')
    message_id = result.get('messageId')
    require(isinstance(message_id, str) and 0 < len(message_id) <= 160
            and message_id not in used_ids and isinstance(result.get('response'), str),
            'CHAT_RESPONSE_MESSAGE_INVALID_OR_REUSED')
    require('endedByAgent' not in result or type(result['endedByAgent']) is bool,
            'CHAT_END_FLAG_INVALID')
    used_ids.add(message_id)
    return received, message_id, result.get('endedByAgent') is True


def join(binding, exchanges, artifacts):
    require(isinstance(binding, dict) and set(binding) == set(ID_KEYS) |
            {'version_number', 'artifact_sha256', 'runtime_sha256', 'generation_run_id',
             'generation_input_sha256'}, 'CHAT_BINDING_FIELDS_INVALID')
    for key in ID_KEYS:
        identifier(binding[key])
    identifier(binding['generation_run_id'])
    require(type(binding['version_number']) is int and binding['version_number'] > 0,
            'CHAT_VERSION_NUMBER_INVALID')
    pins = binding['artifact_sha256']
    require(isinstance(pins, dict) and set(pins) == set(ARTIFACT_KEYS)
            and isinstance(artifacts, dict) and set(artifacts) == set(ARTIFACT_KEYS),
            'CHAT_ARTIFACTS_REQUIRED')
    for role, content in artifacts.items():
        require(isinstance(content, bytes) and sha(content) == fingerprint(pins[role]),
                'CHAT_ARTIFACT_HASH_MISMATCH')
    generation = binding['generation_input_sha256']
    require(isinstance(generation, dict) and set(generation) == {'brief', 'tool_api', 'source_manifest'}
            and all(fingerprint(digest) == pins[name] for name, digest in generation.items()),
            'CHAT_GENERATION_INPUT_HASH_MISMATCH')
    runtime = binding['runtime_sha256']
    source = parse(artifacts['chat_source_manifest'])
    require(isinstance(runtime, dict) and set(RUNTIME_FILES) <= set(runtime)
            and source == runtime
            and all(isinstance(name, str) and name.endswith('.py') and
                    re.fullmatch(r'(?:builder|compiler|orchestration|integration)/[A-Za-z0-9_]+\.py|'
                                 r'data/freya-hackathon/kit-v3/build-the-builder/freya\.py', name)
                    and fingerprint(digest) for name, digest in runtime.items()),
            'CHAT_RUNTIME_SOURCE_JOIN_MISMATCH')
    fixture, export = fixture_value(artifacts['fixture']), parse(artifacts['provider_export'])
    require(isinstance(exchanges, dict) and set(exchanges) == set(ROLE_KEYS) | {'turns'}
            and isinstance(exchanges['turns'], list)
            and 1 <= len(exchanges['turns']) <= len(fixture['messages']), 'CHAT_ROLES_REQUIRED')
    results = {}
    expected_paths = {'before_readback': '/api/v2/workflow/' + binding['workflow_id'],
                      'after_readback': '/api/v2/workflow/' + binding['workflow_id'],
                      'before_versions': '/api/v2/versions/workflow/' + binding['workflow_id'],
                      'after_versions': '/api/v2/versions/workflow/' + binding['workflow_id'],
                      'call': '/api/v2/call/' + binding['call_id']}
    for role in ROLE_KEYS:
        request, results[role] = checked_response(exchanges[role])
        require(request['method'] == 'GET' and request['path'] == expected_paths[role]
                and request['body_base64'] is None
                and request['params'] == ({} if role == 'call' else {'workspaceId': binding['workspace_id']}),
                'CHAT_READBACK_REQUEST_MISMATCH')
    graph = check_publication(binding, export, results['before_readback'], results['before_versions'])
    check_publication(binding, export, results['after_readback'], results['after_versions'])
    call_id, used_ids, response_ids, agent_end = None, set(), [], False
    for index, pair in enumerate(exchanges['turns']):
        require(not agent_end, 'CHAT_TURN_AFTER_AGENT_END')
        request, result = checked_response(pair)
        call_id, message_id, agent_end = check_turn(request, result, binding, fixture,
                                                  index, call_id, used_ids)
        response_ids.append(message_id)
    require(call_id == binding['call_id'] and (agent_end
            or len(exchanges['turns']) == len(fixture['messages'])), 'CHAT_SEQUENCE_INCOMPLETE')
    call = results['call'].get('call')
    require(isinstance(call, dict) and call.get('id') == call_id
            and call.get('workspaceId') == binding['workspace_id']
            and call.get('assistantWorkflowId') == binding['workflow_id']
            and call.get('entityVersionId') == binding['version_id']
            and type(call.get('entityVersionNumber')) is int
            and call['entityVersionNumber'] == binding['version_number'], 'CHAT_CALL_VERSION_OR_SCOPE_MISMATCH')
    messages = call.get('messages')
    require(isinstance(messages, list) and messages and all(isinstance(row, dict) for row in messages)
            and all(sum(row.get('messageId') == name for row in messages) == 1 for name in response_ids),
            'CHAT_CALL_MESSAGE_IDS_MISSING_OR_DUPLICATE')
    # Ordered user-text subsequence verifies the dispatched input, without
    # assuming that engine/tool events occupy particular transcript positions.
    user_text = [row.get('message') for row in messages if row.get('role') == 'user']
    position = 0
    for text in fixture['messages'][:len(response_ids)]:
        while position < len(user_text) and user_text[position] != text:
            position += 1
        require(position < len(user_text), 'CHAT_CALL_INPUT_TEXT_MISSING')
        position += 1
    ended = call.get('endedAt') is not None
    if ended:
        try:
            start = datetime.fromisoformat(call['startedAt'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(call['endedAt'].replace('Z', '+00:00'))
            valid = start.tzinfo is not None and end.tzinfo is not None and end >= start
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        require(valid, 'CHAT_CALL_END_TIMESTAMP_INVALID')
    closing = ('AGENT_END_CONFIRMED' if agent_end and ended else
               'AGENT_END_CLAIM_UNCONFIRMED' if agent_end else
               'CALL_READBACK_ENDED_WITHOUT_AGENT_FLAG' if ended else 'CALL_REMAINS_OPEN')
    initial = graph.get('nodes', {}).get(graph.get('initial_node'), {})
    return {'status': 'CHAT_CONVERSATION_CAPTURED', 'wire_join_verified': True,
            'turns_dispatched': len(response_ids), 'messages_supplied': len(fixture['messages']),
            'transcript_messages': len(messages), 'lifecycle_ended': ended,
            'closing_observation': closing, 'event_model_configuration_verified': True,
            'fixture_origin': 'caller_identified_official_fixture',
            'organizer_signature_verified': False,
            'chat_entry_code_execution': ('NOT_VERIFIED' if initial.get('node_type') == 'code'
                                          else 'NOT_APPLICABLE'), **NO_VERDICT}


class ChatEvidence(ContestEvidence):
    """Existing exact-wire capture with a stricter chat-only request allowlist."""
    def __init__(self, directory, *, secrets=(), protector=protect_bytes):
        super().__init__(directory, secrets=secrets, protector=protector)
        self.sequence = []
        self.protection = self.recorder.protection

    def begin_exchange(self, *, method, path, body=None, params=None):
        allowed = ((method == 'POST' and path == '/api/v2/chat/message') or
                   (method == 'GET' and re.fullmatch(
                       r'/api/v2/(?:workflow|versions/workflow|call)/[a-f0-9-]{36}', path)))
        require(allowed, 'CHAT_REQUEST_OUTSIDE_CAPTURE_SCOPE')
        if method == 'POST':
            require(isinstance(body, bytes) and (params or {}) == {}, 'CHAT_TURN_BODY_OR_PARAMS_INVALID')
            value = parse(body)
            require(isinstance(value, dict) and set(value) in (
                {'workflowId', 'versionId', 'message', 'customerVariables'}, {'callId', 'message'}),
                'CHAT_BRANCH_OVERRIDE_OR_OTHER_FIELD_FORBIDDEN')
            require(isinstance(value['message'], str) and 1 <= len(value['message']) <= 10_000,
                    'CHAT_MESSAGE_LENGTH_INVALID')
            if 'callId' in value:
                identifier(value['callId'])
            else:
                identifier(value['workflowId'])
                identifier(value['versionId'])
                require(isinstance(value['customerVariables'], dict), 'CHAT_CUSTOMER_VARIABLES_INVALID')
        ticket = super().begin_exchange(method=method, path=path, body=body, params=params)
        try:
            # The ordinal is persisted before returning to the HTTP transport.
            # Its failure leaves the inherited pending record and blocks send.
            write_new(self.directory / ('chat-sequence-' + str(len(self.sequence) + 1) + '.json'),
                      {'ordinal': len(self.sequence) + 1, 'exchange_id': ticket,
                       'request_sha256': self.pending[2]['plaintext_sha256']})
            self.sequence.append(ticket)
        except Exception:
            self.failed = True
            raise ContestEvidenceError('CHAT_SEQUENCE_PERSISTENCE_FAILED_NO_DISPATCH') from None
        return ticket

    def pin_artifact(self, role, body):
        self._healthy()
        require(role in ARTIFACT_KEYS and role not in self.artifacts and isinstance(body, bytes),
                'CHAT_ARTIFACT_CAPTURE_INVALID')
        try:
            self.artifacts[role] = self.recorder.store_full('artifact', body)
            self.cache['artifact:' + role] = body
        except Exception:
            self.failed = True
            raise ContestEvidenceError('CHAT_ARTIFACT_CAPTURE_FAILED') from None

    def seal(self, *, binding, roles):
        self._healthy()
        require(self.pending is None and isinstance(roles, dict)
                and set(roles) == set(ROLE_KEYS) | {'turns'} and isinstance(roles['turns'], list),
                'CHAT_UNRESOLVED_OR_MISSING_ROLES')
        tickets = [roles[name] for name in ROLE_KEYS] + roles['turns']
        require(len(tickets) == len(set(tickets)) and set(tickets) == set(self.exchanges),
                'CHAT_ROLE_EXCHANGE_SET_MISMATCH')
        ordered = [roles['before_readback'], roles['before_versions']] + roles['turns'] + [
            roles['call'], roles['after_readback'], roles['after_versions']]
        require(ordered == self.sequence, 'CHAT_DISPATCH_ORDER_MISMATCH')
        pairs = {name: self.cache[roles[name]] for name in ROLE_KEYS}
        pairs['turns'] = [self.cache[ticket] for ticket in roles['turns']]
        result = join(binding, pairs, {name: self.cache['artifact:' + name] for name in self.artifacts})
        manifest = [{'path': path.relative_to(self.directory).as_posix(), 'bytes': len(path.read_bytes()),
                     'sha256': sha(path.read_bytes())} for path in sorted(self.directory.rglob('*')) if path.is_file()]
        value = {'format': 'contest-chat-protected-evidence-v1',
                 'sealed_at_utc': datetime.now(timezone.utc).isoformat(), 'binding': binding,
                 'protection': self.protection, 'roles': {name: self.exchanges[roles[name]] for name in ROLE_KEYS},
                 'turns': [self.exchanges[ticket] for ticket in roles['turns']],
                 'sequence': self.sequence,
                 'artifacts': self.artifacts, 'manifest': manifest, 'result': result,
                 'trust_boundary': 'local retained wire bytes/hashes; no provider signature or grading verdict'}
        write_new(self.directory / 'FINAL.json', value)
        self.sealed = True
        return {'sha256': sha((self.directory / 'FINAL.json').read_bytes()),
                'protection': self.protection, 'result': result}


def verify_chat_bundle(directory, expected_seal_sha256, *, decoder=unprotect_bytes):
    """Explicit offline recovery/recomputation. Returns no private text or vars."""
    root = Path(directory).resolve()
    content = (root / 'FINAL.json').read_bytes()
    require(sha(content) == fingerprint(expected_seal_sha256), 'CHAT_SEAL_HASH_MISMATCH')
    seal = parse(content)
    require(seal.get('format') == 'contest-chat-protected-evidence-v1', 'CHAT_SEAL_FORMAT_INVALID')
    require(isinstance(seal.get('manifest'), list) and seal['manifest'], 'CHAT_MANIFEST_MISSING')
    pins = {}
    for row in seal['manifest']:
        require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256'}
                and isinstance(row['path'], str) and row['path'] not in pins, 'CHAT_MANIFEST_ROW_INVALID')
        path = (root / row['path']).resolve()
        require(path.is_relative_to(root) and path != root and path.is_file(), 'CHAT_MANIFEST_PATH_INVALID')
        stored = path.read_bytes()
        require(type(row['bytes']) is int and len(stored) == row['bytes']
                and sha(stored) == fingerprint(row['sha256']), 'CHAT_MANIFEST_CONTENT_MISMATCH')
        pins[row['path']] = row

    recovered_backends = set()

    def recover(receipt):
        require(isinstance(receipt, dict) and receipt.get('complete') is True
                and isinstance(receipt.get('file'), str), 'CHAT_PROTECTED_RECEIPT_INVALID')
        name = 'wire/' + receipt['file']
        require(name in pins and receipt.get('ciphertext_sha256') == pins[name]['sha256'],
                'CHAT_PROTECTED_FILE_NOT_PINNED')
        try:
            ciphertext = (root / name).read_bytes()
            plaintext = decoder(ciphertext)
            if decoder is unprotect_bytes:
                recovered_backends.add(ciphertext_backend(ciphertext))
        except Exception:
            raise ContestEvidenceError('CHAT_PROTECTED_RECOVERY_FAILED') from None
        require(isinstance(plaintext, bytes) and len(plaintext) == receipt.get('plaintext_bytes')
                and sha(plaintext) == receipt.get('plaintext_sha256'), 'CHAT_PROTECTED_PLAINTEXT_MISMATCH')
        return plaintext

    def exchange(pair):
        require(isinstance(pair, dict) and pair.get('status') == 'CAPTURED', 'CHAT_EXCHANGE_INCOMPLETE')
        return parse(recover(pair.get('request'))), {'http_status': pair.get('http_status'),
                                                   'body': recover(pair.get('response'))}

    require(isinstance(seal.get('roles'), dict) and set(seal['roles']) == set(ROLE_KEYS)
            and isinstance(seal.get('turns'), list), 'CHAT_ROLES_REQUIRED')
    ordered_pairs = [seal['roles']['before_readback'], seal['roles']['before_versions']] + seal['turns'] + [
        seal['roles']['call'], seal['roles']['after_readback'], seal['roles']['after_versions']]
    require(all(isinstance(pair, dict) for pair in ordered_pairs), 'CHAT_EXCHANGE_INCOMPLETE')
    sequence = [identifier(pair.get('exchange_id')) for pair in ordered_pairs]
    require(sequence == seal.get('sequence') and len(sequence) == len(set(sequence)),
            'CHAT_DISPATCH_ORDER_MISMATCH')
    for ordinal, pair in enumerate(ordered_pairs, 1):
        name = 'chat-sequence-' + str(ordinal) + '.json'
        require(name in pins and parse((root / name).read_bytes()) == {
            'ordinal': ordinal, 'exchange_id': pair['exchange_id'],
            'request_sha256': pair['request']['plaintext_sha256']}, 'CHAT_DISPATCH_SEQUENCE_NOT_PINNED')
    exchanges = {name: exchange(pair) for name, pair in seal['roles'].items()}
    exchanges['turns'] = [exchange(pair) for pair in seal['turns']]
    require(isinstance(seal.get('artifacts'), dict) and set(seal['artifacts']) == set(ARTIFACT_KEYS),
            'CHAT_ARTIFACTS_REQUIRED')
    result = join(seal.get('binding'), exchanges,
                  {name: recover(receipt) for name, receipt in seal['artifacts'].items()})
    return {**result, 'protection': seal.get('protection'),
            'production_encryption_verified': decoder is unprotect_bytes
                and seal.get('protection') in ('windows_user_dpapi', 'fernet_v1')
                and recovered_backends == {seal.get('protection')}}
