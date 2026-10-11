"""Protected, full contest evidence; no transport, retries, or live authority.

The caller must capture exact wire bytes before decoding or returning a result.
Reuse NativeResponseRecorder (8 MB complete bodies + encrypted headers), not
ResponseDiagnostics alone (which retains only a 64 KB prefix). Public seals
contain hashes/IDs and joins, never customer text, request variables or keys.
Host evidence storage is not durable storage inside a native workflow.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from builder.freya_lifecycle import verify_simulation_call_binding
from builder.models import strict_json
from .native_pilot import NativeResponseRecorder
from .pilot import iteration_matches
from .private_state import protect_bytes, unprotect_bytes


class ContestEvidenceError(ValueError):
    """Only bounded codes, never reflected private payloads."""


MODEL_KEYS = ('assistantLlmOverride', 'routingLlmOverride',
              'extractionLlmOverride', 'personaLlmOverride', 'analysisLlmOverride')
ROLE_KEYS = ('session', 'simulation', 'iteration', 'call', 'ledger', 'readback', 'versions')
ARTIFACT_KEYS = ('brief', 'tool_api', 'provider_export', 'source_manifest')
ID_KEYS = ('run_id', 'workspace_id', 'workflow_id', 'agent_id', 'version_id',
           'simulation_id', 'persona_id', 'iteration_id', 'call_id')
HASH_KEYS = ('customer_sha256', 'export_sha256', 'brief_sha256',
             'tool_api_sha256', 'source_manifest_sha256')
NO_AUTHORITY = {'network_calls': 0, 'may_dispatch': False, 'may_retry': False,
                'may_write_bank': False, 'native_persistence_proven': False,
                'official_policy_pass_verified': False}


def _require(ok, code):
    if not ok:
        raise ContestEvidenceError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _json(raw):
    try:
        return strict_json(raw.decode('utf-8'))
    except Exception:
        raise ContestEvidenceError('EVIDENCE_JSON_INVALID') from None


def _write(path, value):
    with path.open('xb') as stream:
        stream.write(_raw(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def _binding(value):
    _require(isinstance(value, dict) and set(value) == set(ID_KEYS + HASH_KEYS) |
             {'version_number', 'models', 'case_id'}, 'BINDING_FIELDS_INVALID')
    _require(isinstance(value['case_id'], str) and re.fullmatch(r'PUB-\d{2}', value['case_id']),
             'BINDING_CASE_INVALID')
    for key in ID_KEYS:
        try:
            parsed = uuid.UUID(value[key])
            valid = parsed.int != 0 and str(parsed) == value[key]
        except (ValueError, TypeError, AttributeError):
            valid = False
        _require(valid, 'BINDING_ID_INVALID')
    for key in HASH_KEYS:
        _require(isinstance(value[key], str) and re.fullmatch('[a-f0-9]{64}', value[key]),
                 'BINDING_HASH_INVALID')
    _require(type(value['version_number']) is int and value['version_number'] > 0,
             'BINDING_VERSION_INVALID')
    models = value['models']
    _require(isinstance(models, dict) and set(models) == set(MODEL_KEYS), 'MODEL_KEYS_INVALID')
    for pair in models.values():
        _require(isinstance(pair, dict) and set(pair) == {'provider', 'model'}, 'MODEL_PAIR_INVALID')
        for item in pair.values():
            _require(isinstance(item, str) and re.fullmatch('[a-zA-Z0-9_.-]{1,80}', item)
                     and not re.search('(?i)fak_|token|secret|password|bearer', item),
                     'MODEL_NAME_INVALID')
    return _json(_raw(value))


def _result(value):
    if isinstance(value, dict) and 'success' in value:
        _require(value.get('success') is True and isinstance(value.get('data'), dict)
                 and isinstance(value['data'].get('result'), dict), 'PROVIDER_ENVELOPE_INVALID')
        return value['data']['result']
    _require(isinstance(value, dict), 'RESPONSE_OBJECT_REQUIRED')
    return value


def _join(binding, exchanges, artifacts):
    """Recompute joins from protected wire bytes, never a passed=True summary."""
    b = _binding(binding)
    _require(set(exchanges) == set(ROLE_KEYS) and set(artifacts) == set(ARTIFACT_KEYS),
             'REQUIRED_EVIDENCE_MISSING')
    results = {}
    requests = {}
    for role, pair in exchanges.items():
        req, response = pair
        _require(type(response['http_status']) is int and 200 <= response['http_status'] < 300,
                 'HTTP_RESULT_NOT_SUCCESS')
        requests[role] = req
        results[role] = _result(_json(response['body']))
    session = results['session']
    _require(requests['session']['method'] == 'POST'
             and requests['session']['path'] == '/v1/practice/sessions'
             and _json(base64.b64decode(requests['session']['body_base64'], validate=True)) ==
                 {'case': b['case_id']}, 'SESSION_REQUEST_MISMATCH')
    variables = session.get('variables')
    _require(isinstance(variables, dict) and isinstance(variables.get('customer_id'), str)
             and bool(variables['customer_id'])
             and _sha(variables['customer_id'].encode()) == b['customer_sha256'],
             'CUSTOMER_SCOPE_MISMATCH')
    simulation = requests['simulation']
    request = _json(base64.b64decode(simulation['body_base64'], validate=True))
    _require(simulation['method'] == 'POST' and simulation['path'] == '/api/v2/simulations'
             and request.get('agentId') == b['agent_id']
             and request.get('agentVersionId') == b['version_id']
             and request.get('selectedPersonaIds') == [b['persona_id']]
             and request.get('customerVariablesMap') == {b['persona_id']: variables}
             and request.get('runsPerPersona') == 1 and request.get('maxConcurrentCalls') == 1
             and type(request.get('maxRetries')) is int and request['maxRetries'] == 0
             and all(request.get(key) == pair for key, pair in b['models'].items()),
             'SIMULATION_REQUEST_SCOPE_MISMATCH')
    created = results['simulation'].get('simulation', results['simulation'])
    _require(created.get('id') == b['simulation_id'], 'SIMULATION_RESPONSE_ID_MISMATCH')
    settings = created.get('settings', {})
    _require(isinstance(settings, dict) and type(settings.get('maxRetries')) is int
             and settings['maxRetries'] == 0, 'SIMULATION_RETRY_SETTING_MISMATCH')
    paths = {'iteration': f"/api/v2/simulations/{b['simulation_id']}/iterations/{b['iteration_id']}",
             'call': '/api/v2/call/' + b['call_id'],
             'ledger': '/v1/practice/ledger/' + variables['customer_id'],
             'readback': '/api/v2/workflow/' + b['workflow_id'],
             'versions': '/api/v2/versions/workflow/' + b['workflow_id']}
    for role, path in paths.items():
        _require(requests[role]['method'] == 'GET' and requests[role]['path'] == path,
                 'RESPONSE_REQUEST_JOIN_MISMATCH')
    _require(all(requests[role]['params'].get('workspaceId') == b['workspace_id']
                 for role in ('readback', 'versions')), 'READBACK_WORKSPACE_MISMATCH')
    detail = results['iteration']
    iteration = detail.get('iteration')
    runs = detail.get('runs')
    _require(isinstance(iteration, dict) and iteration.get('id') == b['iteration_id']
             and iteration_matches(iteration, b['version_id'], b['version_number'], b['models'])
             and isinstance(runs, list) and len(runs) == 1, 'ITERATION_MODEL_VERSION_MISMATCH')
    call = results['call'].get('call')
    try:
        life = verify_simulation_call_binding(runs[0], call, workspace_id=b['workspace_id'],
            workflow_id=b['workflow_id'], simulation_id=b['simulation_id'], persona_id=b['persona_id'])
    except Exception:
        raise ContestEvidenceError('CALL_SCOPE_MISMATCH') from None
    _require(life['lifecycle_ended'] and call.get('entityVersionId') == b['version_id']
             and call.get('entityVersionNumber') == b['version_number']
             and isinstance(call.get('messages'), list) and bool(call['messages']),
             'CALL_TRANSCRIPT_VERSION_OR_END_MISSING')
    ledger = results['ledger']
    _require(ledger.get('ok') is True and isinstance(ledger.get('calls'), list), 'LEDGER_MISSING')
    def ledger_customer_scope(value):
        if isinstance(value, dict):
            _require('customer_id' not in value or value['customer_id'] == variables['customer_id'],
                     'LEDGER_FOREIGN_CUSTOMER')
            for item in value.values():
                ledger_customer_scope(item)
        elif isinstance(value, list):
            for item in value:
                ledger_customer_scope(item)
    ledger_customer_scope(ledger)
    versions = results['versions'].get('versions')
    _require(isinstance(versions, list) and len([v for v in versions if isinstance(v, dict)
             and v.get('id') == b['version_id'] and v.get('versionNumber') == b['version_number']
             and v.get('isPublished') is True]) == 1, 'PUBLISHED_VERSION_MISMATCH')
    export = _json(artifacts['provider_export'])
    readback = results['readback'].get('workflow')
    _require(isinstance(readback, dict) and export.get('workflow_id') == b['workflow_id']
             and export.get('agent_id') == b['agent_id'] and readback.get('id') == b['workflow_id']
             and readback.get('agentId') == b['agent_id']
             and isinstance(export.get('workflow'), dict)
             and export['workflow'] == readback.get('workflow'), 'EXPORT_READBACK_MISMATCH')
    for role, key in [('brief', 'brief_sha256'), ('tool_api', 'tool_api_sha256'),
                      ('provider_export', 'export_sha256'), ('source_manifest', 'source_manifest_sha256')]:
        _require(_sha(artifacts[role]) == b[key], 'ARTIFACT_INPUT_HASH_MISMATCH')
    return {'wire_join_verified': True, 'transcript_messages': len(call['messages']),
            'ledger_tool_events': len(ledger['calls']),
            'iteration_outcome': runs[0].get('outcome') if runs[0].get('outcome') in
                ('pass', 'fail', 'unknown') else 'unclassified', **NO_AUTHORITY}


class ContestEvidence:
    """Fresh host recorder. Existing or pending state never grants a resend."""
    def __init__(self, directory, *, secrets=(), protector=protect_bytes):
        self.directory = Path(directory)
        _require(not self.directory.exists(), 'EVIDENCE_DIRECTORY_ALREADY_EXISTS')
        self.directory.mkdir(parents=True)
        self.recorder = NativeResponseRecorder(self.directory / 'wire', secrets=secrets, protector=protector)
        self.pending = None
        self.failed = False
        self.sealed = False
        self.exchanges, self.artifacts, self.cache = {}, {}, {}
        self.recorder.preflight()

    def _healthy(self):
        _require(not self.failed and not self.recorder.failed and not self.sealed, 'EVIDENCE_LATCHED_OR_SEALED')

    def begin_exchange(self, *, method, path, body=None, params=None):
        self._healthy()
        _require(self.pending is None, 'UNRESOLVED_EXCHANGE_NO_RETRY')
        _require(method in ('GET', 'POST', 'PUT') and isinstance(path, str)
                 and re.fullmatch(r'/(?:api/v2|v1/practice)/[A-Za-z0-9_/-]+', path)
                 and (body is None or isinstance(body, bytes)) and isinstance(params or {}, dict),
                 'REQUEST_CAPTURE_INVALID')
        request = _json(_raw({'method': method, 'path': path, 'params': params or {},
                   'body_base64': base64.b64encode(body).decode('ascii') if body is not None else None}))
        ticket = str(uuid.uuid4())
        try:
            receipt = self.recorder.store_full('request', _raw(request))
            dispatch = {'exchange_id': ticket, 'request': receipt, 'status': 'PENDING_NO_RETRY'}
            _write(self.directory / (ticket + '.dispatch.json'), dispatch)
            self.pending = (ticket, request, receipt)
            return ticket
        except Exception:
            self.failed = True
            raise ContestEvidenceError('REQUEST_CAPTURE_FAILED_NO_DISPATCH') from None

    def complete_exchange(self, ticket, *, http_status, headers, body, complete=True):
        self._healthy()
        _require(self.pending is not None and self.pending[0] == ticket, 'RESPONSE_WITHOUT_OWN_REQUEST')
        try:
            _require(type(http_status) is int and isinstance(body, bytes) and complete is True,
                     'RESPONSE_INCOMPLETE')
            request, req_receipt = self.pending[1:]
            receipt = self.recorder.record(method=request['method'], path=request['path'],
                http_status=http_status, headers=headers, body=body, body_length_known=True)
            pair = {'exchange_id': ticket, 'request': req_receipt, 'response': receipt,
                    'http_status': http_status, 'status': 'CAPTURED'}
            _write(self.directory / (ticket + '.exchange.json'), pair)
            self.exchanges[ticket] = pair
            self.cache[ticket] = (request, {'http_status': http_status, 'body': body})
            self.pending = None
            return _json(_raw(pair))
        except Exception:
            self.failed = True
            raise ContestEvidenceError('RESPONSE_CAPTURE_FAILED_NO_FURTHER_REQUEST') from None

    def mark_unknown(self, ticket):
        """Caller observed no complete response. Never clear or replay the slot."""
        self._healthy()
        _require(self.pending is not None and self.pending[0] == ticket, 'UNKNOWN_WITHOUT_OWN_REQUEST')
        self.failed = True
        try:
            _write(self.directory / (ticket + '.unknown.json'),
                   {'exchange_id': ticket, 'status': 'RESULT_UNKNOWN_NO_RETRY', **NO_AUTHORITY})
        except Exception:
            raise ContestEvidenceError('UNKNOWN_MARKER_PERSISTENCE_FAILED_STILL_BLOCKED') from None

    def pin_artifact(self, role, body):
        self._healthy()
        _require(role in ARTIFACT_KEYS and role not in self.artifacts and isinstance(body, bytes),
                 'ARTIFACT_CAPTURE_INVALID')
        try:
            self.artifacts[role] = self.recorder.store_full('artifact', body)
            self.cache['artifact:' + role] = body
        except Exception:
            self.failed = True
            raise ContestEvidenceError('ARTIFACT_CAPTURE_FAILED') from None

    def seal(self, *, binding, roles):
        self._healthy()
        _require(self.pending is None, 'UNRESOLVED_EXCHANGE_NO_SEAL')
        _require(isinstance(roles, dict) and set(roles) == set(ROLE_KEYS)
                 and len(set(roles.values())) == len(ROLE_KEYS)
                 and all(ticket in self.exchanges for ticket in roles.values()), 'ROLE_EXCHANGES_MISSING')
        result = _join(binding, {r: self.cache[t] for r, t in roles.items()},
                       {r: self.cache['artifact:' + r] for r in self.artifacts})
        manifest = []
        for path in sorted(self.directory.rglob('*')):
            if path.is_file():
                raw = path.read_bytes()
                manifest.append({'path': path.relative_to(self.directory).as_posix(),
                                 'bytes': len(raw), 'sha256': _sha(raw)})
        value = {'format': 'contest-protected-evidence-v1',
                 'sealed_at_utc': datetime.now(timezone.utc).isoformat(), 'binding': _binding(binding),
                 'roles': {r: self.exchanges[t] for r, t in roles.items()}, 'artifacts': self.artifacts,
                 'manifest': manifest, 'result': result,
                 'trust_boundary': 'retained transport bytes/local hashes; no provider signature or policy verdict'}
        _write(self.directory / 'FINAL.json', value)
        self.sealed = True
        return {'path': str(self.directory / 'FINAL.json'),
                'sha256': _sha((self.directory / 'FINAL.json').read_bytes()), 'result': result}


def verify_bundle(directory, expected_seal_sha256, *, decoder=unprotect_bytes):
    """Offline, explicit local recovery; no plaintext is written or returned."""
    root = Path(directory).resolve()
    seal_raw = (root / 'FINAL.json').read_bytes()
    _require(_sha(seal_raw) == expected_seal_sha256, 'SEAL_HASH_MISMATCH')
    seal = _json(seal_raw)
    _require(seal.get('format') == 'contest-protected-evidence-v1', 'SEAL_FORMAT_INVALID')
    manifest = seal.get('manifest')
    _require(isinstance(manifest, list) and manifest, 'MANIFEST_MISSING')
    pins = {}
    for row in manifest:
        _require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256'}
                 and isinstance(row['path'], str) and row['path'] not in pins, 'MANIFEST_ROW_INVALID')
        path = (root / row['path']).resolve()
        _require(path != root and path.is_relative_to(root) and path.is_file(), 'MANIFEST_PATH_INVALID')
        raw = path.read_bytes()
        _require(len(raw) == row['bytes'] and _sha(raw) == row['sha256'], 'MANIFEST_CONTENT_MISMATCH')
        pins[row['path']] = row

    def recover(receipt):
        _require(isinstance(receipt, dict) and receipt.get('complete') is True
                 and isinstance(receipt.get('file'), str), 'PROTECTED_RECEIPT_INVALID')
        name = 'wire/' + receipt['file']
        _require(name in pins and receipt.get('ciphertext_sha256') == pins[name]['sha256'],
                 'PROTECTED_FILE_NOT_PINNED')
        try:
            raw = decoder((root / name).read_bytes())
        except Exception:
            raise ContestEvidenceError('PROTECTED_RECOVERY_FAILED') from None
        _require(isinstance(raw, bytes) and len(raw) == receipt.get('plaintext_bytes')
                 and _sha(raw) == receipt.get('plaintext_sha256'), 'PROTECTED_PLAINTEXT_MISMATCH')
        return raw

    exchanges = {}
    _require(isinstance(seal.get('roles'), dict) and set(seal['roles']) == set(ROLE_KEYS), 'REQUIRED_EVIDENCE_MISSING')
    for role, pair in seal['roles'].items():
        _require(isinstance(pair, dict) and pair.get('status') == 'CAPTURED', 'EXCHANGE_NOT_COMPLETE')
        exchanges[role] = (_json(recover(pair.get('request'))),
                          {'http_status': pair.get('http_status'), 'body': recover(pair.get('response'))})
    _require(isinstance(seal.get('artifacts'), dict) and set(seal['artifacts']) == set(ARTIFACT_KEYS),
             'REQUIRED_EVIDENCE_MISSING')
    return _join(seal.get('binding'), exchanges, {r: recover(v) for r, v in seal['artifacts'].items()})
