"""Fresh one-shot native pilot. Missing independent admission means NO key read.

The producer's trusted Python verifier must *derive* readiness from source and
execution evidence. JSON booleans, a local SDK fixture and the old prefix are
never an admission. This controller does not implement native speech/storage.
It publishes no edited graph, never resumes old calls, and never retries POST.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from builder.freya_export import measure_code_exec_budget
from builder.freya_lifecycle import LifecycleError, verify_simulation_call_binding
from builder.models import strict_json
from .pilot import (ROOT, KIT, PilotController, atomic_json, fixed_models, identifier,
                    iteration_matches, ledger_score, no_added_actions,
                    no_lifecycle_actions, persona, retained_fields)
from .preflight import file_hash, read_strict
from .private_state import persist_secret, protect_secret
from .response_diagnostics import ResponseDiagnostics
from .sanitize import sanitize
from .session_guard import Budget, PreparationError, SessionGuard, digest, one_case_payload
from .transport import NoRedirect, TransportError

_SEAL = object()
GATES = ('publication_ready', 'bank_request_response_bound', 'speech_completion_bound',
         'restart_write_safety_proven', 'provider_call_lifecycle_bound',
         'critical_tests_passed', 'full_local_PUB01_path_verified')
MODEL_KEYS = ('assistantLlmOverride', 'routingLlmOverride', 'extractionLlmOverride',
              'personaLlmOverride', 'analysisLlmOverride')


def _commit():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                   text=True, stderr=subprocess.DEVNULL).strip()


def _pins_match(pins):
    if not isinstance(pins, dict) or not pins:
        raise PreparationError('Native source/evidence pins missing')
    for path, expected in pins.items():
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise PreparationError('Native pin hash invalid')
        target = Path(path) if Path(path).is_absolute() else ROOT / path
        if file_hash(target) != expected:
            raise PreparationError('Native pinned source/evidence changed')


class NativeAdmission:
    """Opaque in-process result of a trusted verifier, never deserialized JSON.

    A Python callback is a trust boundary, not a sandbox. It must independently
    replay the producer proof; checking caller-supplied yes/no fields is invalid.
    The seal stops accidental substitution of operation/readiness JSON for that
    callback. It does not claim to authenticate arbitrary executable Python.
    """
    def __init__(self, seal, workflow, operation, pins, authority):
        if seal is not _SEAL:
            raise PreparationError('Independent native verifier receipt required')
        self.workflow = copy.deepcopy(workflow)
        self.operation = copy.deepcopy(operation)
        self.pins = copy.deepcopy(pins)
        self.authority = authority
        self._seal = seal
        self._digest = digest(self._content())

    def _content(self):
        return {'workflow': self.workflow, 'operation': self.operation,
                'pins': self.pins, 'authority': self.authority}

    def check(self, *, live=False):
        if self._seal is not _SEAL or digest(self._content()) != self._digest:
            raise PreparationError('Native admission receipt altered')
        if live and self.authority != 'independent_native_product_verifier':
            raise PreparationError('Synthetic receipts cannot authorize a live pilot')
        if 'agent_config' in self.workflow:
            # Official kit: omit agent_config, then bind all five simulation
            # overrides. Reject an incompatible source, never edit its graph.
            raise PreparationError('Native source workflow must omit agent_config')
        _pins_match(self.pins)
        if self.operation.get('source_commit') != _commit():
            raise PreparationError('Native source commit changed')


def derive_native_admission(package_path, operation_path, product_verifier):
    """Callback API: replay product proof first, then bind its exact export.

    Existing verify_product_build returns publish_allowed=False and is therefore
    rejected. A future reviewed verifier must return the seven derived gates,
    evidence_authority='source_supported_native_contract', tested_scope='A/PUB-01',
    and hashed proof/source/runtime inventories. Completion/restart bindings must
    follow a supported native contract, not locally invented receipt flags.
    Prior live success is NOT required: full_local_PUB01_path_verified is fresh
    local execution of the exact generated graph. Actual platform success is
    established only after this one test by transcript, ledger and official judge.
    """
    if not callable(product_verifier):
        raise PreparationError('Independent native product verifier missing')
    package = Path(package_path).resolve()
    operation_file = Path(operation_path).resolve()
    proof = product_verifier(package)
    if (not isinstance(proof, dict) or proof.get('publish_allowed') is not True
            or proof.get('evidence_authority') != 'source_supported_native_contract'
            or proof.get('tested_scope') != 'A/PUB-01'
            or any(proof.get(key) is not True for key in GATES)):
        raise PreparationError('Native producer has unproven conversation/write/restart gates')
    # A positive callback cannot erase a veto in the producer's actual package.
    readiness = read_strict(package / 'readiness.json')
    final = read_strict(package / 'FINAL.json')
    if (readiness.get('publish_allowed') is not True or final.get('publish_allowed') is not True
            or readiness.get('native_readiness', {}).get('bank_writes_enabled') is not True):
        raise PreparationError('Native package still seals publication or bank writes')
    manifest_path = package / 'manifest.json'
    if file_hash(manifest_path) != final.get('manifest_sha256'):
        raise PreparationError('Native producer manifest changed')
    rows = read_strict(manifest_path).get('files')
    if not isinstance(rows, list) or not rows or len({r['path'] for r in rows}) != len(rows):
        raise PreparationError('Native producer file inventory invalid')
    pins = {str(manifest_path): file_hash(manifest_path), str(package / 'FINAL.json'): file_hash(package / 'FINAL.json')}
    for row in rows:
        name = row['path']
        path = package / name
        if (Path(name).name != name or path.is_symlink() or path.stat().st_size != row['bytes']
                or file_hash(path) != row['sha256']):
            raise PreparationError('Native producer artifact changed')
        pins[str(path)] = row['sha256']
    candidate = package / 'agent.workflow.json'
    if candidate.name not in {r['path'] for r in rows} or file_hash(candidate) != proof.get('candidate_sha256'):
        raise PreparationError('Native verifier and candidate bytes differ')
    operation = read_strict(operation_file)
    if (operation.get('schema_version') != 'native-operation-1'
            or operation.get('candidate_sha256') != file_hash(candidate)
            or operation.get('case_id') != 'PUB-01'
            or operation.get('source_commit') != _commit()):
        raise PreparationError('Fresh operation identity/candidate/commit not bound')
    identifier(operation.get('operation_id'))
    identifier(operation.get('workspace_id'))
    for inventory in ('source_sha256', 'runtime_sha256', 'proof_file_sha256'):
        _pins_match(proof.get(inventory))
        pins.update(proof[inventory])
    parents = operation.get('parent_file_sha256')
    _pins_match(parents)
    # Bind both previous real operations, including their retained locks. The
    # complete prepared snapshot is mandatory; no smaller substituted list.
    plan_path = ROOT / 'integration/native-operation-plan.json'
    plan = read_strict(plan_path)
    if parents != plan['parent_file_sha256'] or operation['workspace_id'] != plan['workspace_id']:
        raise PreparationError('Old uncertain/failed operation separation not proven')
    forbidden = sorted({value for row in plan['old_refs_forbidden_for_mutations'] for value in row.values()})
    if operation.get('forbidden_resource_ids') != forbidden:
        raise PreparationError('Native fresh-resource exclusion inventory differs from old operations')
    pins.update(parents)
    pins[str(plan_path)] = file_hash(plan_path)
    authority = operation.get('authority_reference')
    if not authority or file_hash(authority) != operation.get('authority_sha256'):
        raise PreparationError('Fresh human operation authority record missing')
    pins[str(Path(authority).resolve())] = operation['authority_sha256']
    pins[str(operation_file)] = file_hash(operation_file)
    pins[str(Path(__file__).resolve())] = file_hash(__file__)
    pins[str(KIT / 'freya.py')] = file_hash(KIT / 'freya.py')
    pins[str(KIT / 'public-test-set.json')] = file_hash(KIT / 'public-test-set.json')
    workflow = read_strict(candidate)
    if candidate.stat().st_size >= 131072:
        raise PreparationError('Native export exceeds 131072-byte limit')
    no_lifecycle_actions(workflow)
    budget = measure_code_exec_budget(workflow, operation.get('initial_variables', {}), reserve_variable_bytes=16384)
    if budget.get('passed') is not True or budget['admission_upper_bound_bytes'] >= 114688:
        raise PreparationError('Native code/vars byte budget not admitted')
    receipt = NativeAdmission(_SEAL, workflow, operation, pins, 'independent_native_product_verifier')
    receipt.check(live=True)
    return receipt


class NativeResponseRecorder(ResponseDiagnostics):
    """Full bounded wire body + safe prefix metadata, durably encrypted before use.

    8 MB is a local diagnostic bound, not a platform quota. Incomplete wire
    bodies latch failure. Headers use the existing private trace receipt.
    """
    LIMIT = 8_000_000

    def store_full(self, kind, raw):
        if self.failed:
            raise PreparationError('Native recorder is latched')
        try:
            if not isinstance(raw, bytes) or len(raw) > self.LIMIT:
                raise ValueError('Full native evidence bound exceeded')
            cipher = self.protector(raw)
            if not isinstance(cipher, bytes) or not cipher or cipher == raw or (len(raw) >= 16 and raw in cipher):
                raise ValueError('Native evidence not encrypted')
            self.directory.mkdir(parents=True, exist_ok=True)
            name = str(uuid.uuid4()) + '.' + kind + '.dpapi'
            with (self.directory / name).open('xb') as stream:
                stream.write(cipher)
                stream.flush()
                os.fsync(stream.fileno())
            receipt = {'file': name, 'plaintext_bytes': len(raw), 'plaintext_sha256': hashlib.sha256(raw).hexdigest(),
                       'ciphertext_sha256': file_hash(self.directory / name), 'complete': True}
            atomic_json(self.directory / (name + '.json'), receipt)
            return receipt
        except Exception:
            self.failed = True
            raise PreparationError('Full native evidence persistence failed') from None

    def record(self, **kwargs):
        if not kwargs.get('body_length_known', True) or kwargs.get('observed_bytes', len(kwargs['body'])) != len(kwargs['body']):
            self.failed = True
            raise PreparationError('Native wire body incomplete; no further request')
        complete = self.store_full('response', kwargs['body'])
        prefix = super().record(**kwargs)
        # Separate durable link preserves existing receipt/trace format.
        try:
            atomic_json(self.directory / (complete['file'] + '.link.json'),
                        {'full_response': complete, 'prefix_and_trace_receipt': prefix})
        except Exception:
            self.failed = True
            raise PreparationError('Full native response receipt link failed') from None
        return complete


class NativeWireTransport:
    """Fixed Freya/bank origins; no redirects, retry or arbitrary agent tool API.

    This builder creates a practice session and reads its ledger only. Native
    bank tools remain inside the exported workflow, as the competition requires.
    """
    def __init__(self, key, team, recorder, *, opener=None):
        if not key or not isinstance(team, str) or len(team) < 16:
            raise PreparationError('Native credentials missing')
        self.key, self.team, self.recorder = key, team, recorder
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __call__(self, method, path, *, body=None, params=None, timeout=20):
        if self.recorder.failed:
            raise PreparationError('Native recorder failure prohibits further network')
        bank = path.startswith('/v1/practice/')
        if method not in {'GET', 'POST'} or not re.fullmatch(r'/(?:api/v2|v1/practice)/[A-Za-z0-9_/-]+', path):
            raise PreparationError('Native wire route invalid')
        if bank and not ((method == 'POST' and path == '/v1/practice/sessions' and body == {'case': 'PUB-01'})
                         or (method == 'GET' and re.fullmatch('/v1/practice/ledger/[A-Za-z0-9_-]+', path))):
            raise PreparationError('Native builder cannot call bank agent tools')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 20:
            raise PreparationError('Native wire timeout invalid')
        self.recorder.store_full('request', json.dumps({'method': method, 'path': path, 'body': body,
                                      'params': params or {}}, allow_nan=False).encode())
        base = 'https://freya-bank-mock.freyavoice.workers.dev' if bank else 'https://staging-app.freyavoice.ai'
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (native-freya-pilot)'}
        headers.update({'x-team-key': self.team} if bank else {'Authorization': 'Bearer ' + self.key})
        query = urllib.parse.urlencode(params or {})
        request = urllib.request.Request(base + path + ('?' + query if query else ''), method=method,
                 data=json.dumps(body, allow_nan=False).encode() if body is not None else None, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc  # capture the entire error before raising a safe status
            with response:
                raw = response.read(self.recorder.LIMIT + 1)
                status = getattr(response, 'status', getattr(response, 'code', 200))
                self.recorder.record(method=method, path=path, http_status=status, headers=response.headers,
                      body=raw, body_length_known=len(raw) <= self.recorder.LIMIT,
                      failure_class='http_error' if not 200 <= status < 300 else None)
            if not 200 <= status < 300:
                raise TransportError('native_http_error', status)
            value = strict_json(raw.decode('utf-8'))
            if bank and isinstance(value, dict):
                return value
            if (not isinstance(value, dict) or value.get('success') is not True
                    or not isinstance(value.get('data'), dict) or not isinstance(value['data'].get('result'), dict)):
                raise PreparationError('Native response envelope invalid')
            return value['data']['result']
        except TransportError:
            raise
        except Exception:
            raise PreparationError('Native wire request failed; never retry a mutation') from None


class NativeOperationGuard(SessionGuard):
    def __init__(self, directory, transport, admission, *, clock=time.monotonic, budget=None):
        admission.check()
        self.admission = admission
        self.operation = admission.operation
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.workspace_id = identifier(self.operation['workspace_id'])
        self.run_id = identifier(self.operation['operation_id'])
        self.transport, self.clock = transport, clock
        self.budget = budget or Budget(max_requests=75, max_mutations=6, deadline_seconds=900, http_timeout_seconds=20)
        if (self.budget.max_requests > 75 or self.budget.max_mutations > 6
                or self.budget.deadline_seconds > 900 or self.budget.http_timeout_seconds > 20):
            raise PreparationError('Native pilot budget exceeds scoped authorization')
        self.start = clock()
        self.deadline = self.start + self.budget.deadline_seconds
        self.requests = self.mutations = self.simulations = 0
        self.pending, self.reserved_posts = set(), set()
        self.simulation_id = None
        self.simulation_terminal = self.closed = False
        self.remote_idle_at = None
        self.mutex, self.scope_mutex = threading.Lock(), threading.Lock()
        self.refs = {}
        self.customer_id = None
        self.phase = 'prepare'
        owner = hashlib.sha256(self.workspace_id.encode()).hexdigest()
        self.lock_path = self.directory / (owner + '.native-owner.json')
        self.consumed_path = self.directory / (self.run_id + '.native-authorization.json')
        self.journal_path = self.directory / (self.run_id + '.native.jsonl')
        self.owner = {'run_id': self.run_id, 'workspace_id': self.workspace_id,
                      'admission_sha256': admission._digest, 'journal': self.journal_path.name}
        for path in (self.consumed_path, self.lock_path):
            try:
                with path.open('x', encoding='utf-8') as stream:
                    json.dump(self.owner, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise PreparationError('Native operation already owned/consumed; no restart or replay') from None
        self._record('opened', budget=vars(self.budget), parent_evidence_preserved=True,
                     candidate_sha256=self.operation.get('candidate_sha256'))

    def intact(self):
        self.admission.check()
        if any(read_strict(path) != self.owner for path in (self.lock_path, self.consumed_path)):
            raise PreparationError('Native operation owner changed')

    def call(self, method, path, **kwargs):
        with self.scope_mutex:
            self.intact()
            body, params = kwargs.get('body'), kwargs.get('params') or {}
            wf, ver = self.refs.get('workflow_id'), self.refs.get('version_id')
            read = {'/api/v2/simulations', f'/api/v2/workspaces/{self.workspace_id}/credits'}
            if wf:
                read.update({'/api/v2/workflow/' + wf, '/api/v2/versions/workflow/' + wf})
                if ver:
                    read.add('/api/v2/versions/workflow/' + wf + '/' + ver)
            if self.simulation_id:
                base = '/api/v2/simulations/' + self.simulation_id
                read.update({base, base + '/iterations'})
                if self.refs.get('iteration_id'):
                    read.add(base + '/iterations/' + self.refs['iteration_id'])
            if self.refs.get('simulation_call_id'):
                read.add('/api/v2/call/' + self.refs['simulation_call_id'])
            if self.customer_id:
                read.add('/v1/practice/ledger/' + self.customer_id)
            if method == 'GET':
                if path not in read or params.get('workspaceId', self.workspace_id) != self.workspace_id:
                    raise PreparationError('Native read outside owned scope')
            elif method == 'POST':
                key = (path, digest(params))
                if key in self.reserved_posts:
                    raise PreparationError('Native POST already reserved; never replay')
                if path == '/api/v2/workflow':
                    expected = {'name': 'Native-A-PUB01 ' + self.run_id, 'workflow': self.admission.workflow}
                    if self.phase != 'prepare' or body != expected or params not in ({}, {'dryRun': 'true'}):
                        raise PreparationError('Native publication must use the exact admitted fresh graph')
                elif wf and path == '/api/v2/versions/workflow/' + wf + '/publish':
                    if self.phase != 'prepare' or body != {'versionId': ver} or params:
                        raise PreparationError('Native publish not bound to its own version')
                elif path == '/v1/practice/sessions':
                    if self.phase != 'bank_ready' or body != {'case': 'PUB-01'} or params:
                        raise PreparationError('Native bank session before publication proof')
                elif path == '/api/v2/personas':
                    if not self.customer_id or body != {'personas': [persona(self.case)]} or params:
                        raise PreparationError('Native persona not bound to exact official case')
                elif path == '/api/v2/simulations':
                    expected = one_case_payload(self.refs['agent_id'], self.refs['persona_id'], self.variables,
                          name='Native-A-PUB01 ' + self.run_id, agent_model=fixed_models()[0], judge_model=fixed_models()[1])
                    expected['agentVersionId'] = ver
                    if self.phase != 'bank_ready' or body != expected or params:
                        raise PreparationError('Native simulation persona/session/version/models differ')
                else:
                    raise PreparationError('Old chat, PUT, rerun or unrelated mutation forbidden')
                if self.remote_idle_at is None or self.clock() - self.remote_idle_at > 30:
                    raise PreparationError('Fresh complete idle inventory required before mutation')
                self.reserved_posts.add(key)
            else:
                raise PreparationError('Native method forbidden')
            return super().call(method, path, **kwargs)

    def close(self):
        with self.scope_mutex:
            self.intact()
            if self.closed or self.pending or not self.simulation_terminal:
                raise PreparationError('Native uncertainty retains owner and consumed authorization')
            self._record('scope_closed_consumed', requests=self.requests, mutations=self.mutations, simulations=self.simulations)
            self.closed = True  # both markers remain permanently; old locks untouched


def conversation_evidence(call, call_id):
    """Text-call completion, NOT an audio-playback certificate.

    The call schema permits freeform roles and missing message on diagnostic
    rows. Require actual user/assistant text without rejecting those rows.
    """
    if not isinstance(call, dict) or call.get('id') != call_id or not isinstance(call.get('messages'), list):
        return {'valid': False, 'audio_completion_proven': False}
    rows = call['messages']
    text = lambda role: [r for r in rows if isinstance(r, dict) and r.get('role') == role
                        and isinstance(r.get('message'), str) and r['message'].strip()]
    fault = False
    for row in rows:
        if isinstance(row, dict) and row.get('role') == 'system':
            event = row.get('message')
            try:
                event = json.loads(event) if isinstance(event, str) else event
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('event') in {'error', 'function_call_error'}:
                fault = True
    ended = isinstance(call.get('endedAt'), str) and bool(call['endedAt'].strip()) and call.get('endedReason') == 'assistant-ended-call'
    return {'valid': bool(text('user') and text('assistant') and ended and not fault),
            'user_turns': len(text('user')), 'assistant_turns': len(text('assistant')),
            'ended_by_agent': ended, 'runtime_fault': fault, 'audio_completion_proven': False}


_LEDGER_OWNERSHIP_SEAL = object()


class SessionLedgerOwnership:
    """Opaque scope join from reviewed transport objects, never JSON flags.

    Parsing is not transport authentication. Only the guarded controller or an
    independent authenticated-wire verifier may supply objects to the factory.
    The receipt neither changes ledger rows nor proves official judge success.
    """
    def __init__(self, seal, ledger, customer_id, scope):
        if seal is not _LEDGER_OWNERSHIP_SEAL:
            raise PreparationError('Reviewed session ledger ownership required')
        self._seal, self._ledger_digest = seal, digest(ledger)
        self._customer_digest = hashlib.sha256(customer_id.encode()).hexdigest()
        self._scope = copy.deepcopy(scope)
        self._fingerprint = digest(self._content())

    def _content(self):
        return {'scope': self._scope, 'ledger_sha256': self._ledger_digest,
                'customer_sha256': self._customer_digest}

    def matches(self, ledger, customer_id):
        return (self._seal is _LEDGER_OWNERSHIP_SEAL and digest(self._content()) == self._fingerprint
                and digest(ledger) == self._ledger_digest
                and hashlib.sha256(customer_id.encode()).hexdigest() == self._customer_digest)


def derive_session_ledger_ownership(ledger, customer_id, *, ledger_request, simulation_request,
        iteration, run, call, workspace_id, workflow_id, agent_id, version_id, version_number,
        simulation_id, persona_id, reservation_id):
    """Join exact session-scoped GET to the measured simulation and native state.

    Inputs must be authenticated/recorded transport objects, not producer status
    reports. The same function is used by live controllers and retained-wire
    re-evaluation. Missing/contradictory runtime ownership remains fail closed.
    """
    identifier(reservation_id)
    if (not isinstance(customer_id, str) or not re.fullmatch('[A-Za-z0-9_-]+', customer_id)
            or not isinstance(ledger, dict) or ledger.get('ok') is not True or not isinstance(ledger.get('calls'), list)
            or not isinstance(ledger_request, dict) or ledger_request.get('method') != 'GET'
            or ledger_request.get('path') != '/v1/practice/ledger/' + customer_id
            or ledger_request.get('body') is not None or ledger_request.get('params') not in (None, {})):
        raise PreparationError('Exact authenticated customer ledger GET required')
    request = simulation_request
    variables = request.get('customerVariablesMap') if isinstance(request, dict) else None
    if (not isinstance(variables, dict) or set(variables) != {persona_id}
            or not isinstance(variables[persona_id], dict)
            or variables[persona_id].get('customer_id') != customer_id
            or request.get('selectedPersonaIds') != [persona_id]
            or request.get('agentId') != agent_id or request.get('agentVersionId') != version_id
            or request.get('runsPerPersona') != 1 or request.get('maxConcurrentCalls') != 1
            or request.get('maxRetries') != 0):
        raise PreparationError('Ledger simulation/customer/version join missing')
    models = {key: request.get(key) for key in MODEL_KEYS}
    if (any(not isinstance(value, dict) for value in models.values())
            or type(version_number) is not int
            or not iteration_matches(iteration, version_id, version_number, models)):
        raise PreparationError('Ledger measured iteration/version join missing')
    try:
        lifecycle = verify_simulation_call_binding(run, call, workspace_id=workspace_id,
            workflow_id=workflow_id, simulation_id=simulation_id, persona_id=persona_id)
    except LifecycleError:
        raise PreparationError('Ledger actual call scope unproven') from None
    if not lifecycle['lifecycle_ended']:
        raise PreparationError('Ledger actual call not terminal')
    call_id, phases, confirmed_guards = lifecycle['call_id'], [], []
    for row in call.get('messages', []):
        if not isinstance(row, dict): continue
        tables = []
        snapshot = row.get('workflowSnapshot')
        if isinstance(snapshot, dict) and isinstance(snapshot.get('variableTable'), dict):
            tables.append((snapshot['variableTable'], False))
        event = row.get('message')
        if isinstance(event, str):
            try: event = strict_json(event)
            except (ValueError, TypeError): event = None
        if (row.get('role') == 'system' and isinstance(event, dict)
                and event.get('event') == 'route_evaluated' and isinstance(event.get('variable_table'), dict)):
            tables.append((event['variable_table'], True))
        for table, ordered_route in tables:
            def value(name):
                item = table.get(name)
                return item.get('value') if isinstance(item, dict) and 'value' in item else item
            if 'customer_id' in table and value('customer_id') != customer_id:
                raise PreparationError('Runtime customer differs from ledger scope')
            if 'call.call_id' in table and value('call.call_id') not in (None, '', call_id):
                raise PreparationError('Runtime call differs from ledger scope')
            raw = value('_native_state_json')
            if raw not in (None, ''):
                try: state = strict_json(raw) if isinstance(raw, str) else None
                except (ValueError, TypeError): state = None
                if (not isinstance(state, dict) or state.get('customer_id') != customer_id
                        or state.get('call_id') != call_id):
                    raise PreparationError('Runtime source ownership missing or contradictory')
                if state.get('phase') in ('write_pending', 'promise_confirmed'):
                    if (not isinstance(state.get('readback_token'), str) or not state['readback_token']
                            or state.get('consent') != state['readback_token']):
                        raise PreparationError('Runtime current explicit consent missing')
                    # Provider snapshots can be retroactively enriched. Only
                    # ordered engine route events establish transition timing.
                    if ordered_route: phases.append(state['phase'])
            raw = value('_native_write_state')
            if raw not in (None, ''):
                try: guard = strict_json(raw) if isinstance(raw, str) else None
                except (ValueError, TypeError): guard = None
                if (not isinstance(guard, dict) or guard.get('reservation_id') != reservation_id
                        or guard.get('phase') in ('unknown', 'failed')):
                    raise PreparationError('Runtime reservation or bank result unproven')
                if guard.get('phase') == 'confirmed':
                    if guard.get('call_id') != call_id:
                        raise PreparationError('Confirmed bank result belongs to another call')
                    try: payload = strict_json(guard.get('payload_json', ''))
                    except (ValueError, TypeError): payload = None
                    if not isinstance(payload, dict) or payload.get('customer_id') != customer_id:
                        raise PreparationError('Confirmed bank payload belongs to another customer')
                    if ordered_route: confirmed_guards.append(payload)
    if ('write_pending' not in phases or 'promise_confirmed' not in phases
            or phases.index('write_pending') >= phases.index('promise_confirmed') or not confirmed_guards):
        raise PreparationError('Runtime pending/confirmed ownership join missing')
    writes = [row for row in ledger.get('calls', []) if isinstance(row, dict) and row.get('tool') == 'create_promise']
    if (len(writes) != 1 or not isinstance(writes[0].get('args'), dict)
            or any(payload != dict(customer_id=customer_id,
                **{key: writes[0]['args'].get(key) for key in ('product_id', 'amount', 'date')})
                for payload in confirmed_guards)):
        raise PreparationError('Confirmed native payload and actual ledger write differ')
    return SessionLedgerOwnership(_LEDGER_OWNERSHIP_SEAL, ledger, customer_id,
        {'ledger_request': ledger_request, 'simulation_request_sha256': digest(request),
         'iteration_sha256': digest(iteration), 'run_sha256': digest(run), 'call_sha256': digest(call),
         'workspace_id': workspace_id, 'workflow_id': workflow_id, 'version_id': version_id,
         'simulation_id': simulation_id, 'reservation_id': reservation_id})


def bank_chain_evidence(case, ledger, customer_id, *, ownership=None):
    """Additional request/result checks; the official scorer remains unmodified.

    For this one simple case, require the actual two-factor/read/write/close
    chain, a returned promise ID and one last close. This is not evidence that
    speech preceded the write: the official conversation judge handles consent.
    """
    rows = ledger.get('calls') if isinstance(ledger, dict) else None
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        return {'valid': False, 'consent_proven_by_ledger': False}
    verified, products, successes = set(), set(), []
    valid = True
    for row in rows:
        args, result, tool = row.get('args'), row.get('result'), row.get('tool')
        scoped = isinstance(ownership, SessionLedgerOwnership) and ownership.matches(ledger, customer_id)
        if (not isinstance(args, dict) or not isinstance(result, dict)
                or ('customer_id' in row and row['customer_id'] != customer_id)
                or ('customer_id' in args and args['customer_id'] != customer_id)
                or ('customer_id' in result and result['customer_id'] != customer_id)
                or ('customer_id' not in args and not scoped)
                or ('status' in row and (type(row['status']) is not int or row['status'] != 200))):
            valid = False
            continue
        if tool == 'verify_identity':
            field = args.get('field')
            if (field not in case['identity'] or args.get('answer') != case['identity'][field]
                    or result.get('ok') is not True or result.get('verified') is not True):
                valid = False
            else:
                verified.add(field)
        elif tool == 'get_account':
            if verified != {'date_of_birth', 'phone_last4'} or result.get('ok') is not True:
                valid = False
            account = result.get('products')
            if not isinstance(account, list) or any(not isinstance(p, dict) or not isinstance(p.get('product_id'), str) for p in account):
                valid = False
            else:
                products.update(p['product_id'] for p in account)
        elif tool == 'create_promise':
            promised = {k: args.get(k) for k in ('product_id', 'amount', 'date')}
            if (args.get('product_id') not in products or type(args.get('amount')) not in (int, float)
                    or result.get('ok') is not True or not isinstance(result.get('promise_id'), str) or not result['promise_id']):
                valid = False
            successes.append(promised)
        elif tool == 'close_call':
            if (result.get('ok') is not True or args.get('outcome') != case['expected']['outcome']
                    or args.get('reason') != case['expected'].get('reason')):
                valid = False
        else:
            valid = False  # unrelated notes/transfers are outside this case
    tools = [r.get('tool') for r in rows]
    valid = (valid and tools.count('get_account') == 1 and tools.count('create_promise') == 1
             and tools.count('close_call') == 1 and tools[-1] == 'close_call'
             and successes == case['expected']['promises'])
    records = ledger.get('promises', [])
    writes = [r for r in rows if r.get('tool') == 'create_promise']
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        valid = False
    elif writes:
        record, write = records[0], writes[0]
        if 'customer_id' in record and record['customer_id'] != customer_id:
            valid = False
        # The official ledger includes record IDs. Legacy explicit-owner local
        # fixtures without them retain their prior meaning; scope-based owner
        # acceptance requires the actual returned ID to match the record.
        write_result = write.get('result')
        if not isinstance(write_result, dict): valid = False
        if ('promise_id' in record or scoped) and record.get('promise_id') != (write_result.get('promise_id') if isinstance(write_result, dict) else None):
            valid = False
        if {k: record.get(k) for k in ('product_id', 'amount', 'date')} != case['expected']['promises'][0]:
            valid = False
    return {'valid': valid, 'operation_order': tools, 'promise_requests': tools.count('create_promise'),
            'close_requests': tools.count('close_call'), 'consent_proven_by_ledger': False}


class NativePilotController(PilotController):
    def __init__(self, output, transport, admission, *, state_directory, recorder,
                 clock=time.monotonic, sleeper=time.sleep, protector=protect_secret, mode='offline_mock', secrets=(), budget=None):
        if not isinstance(admission, NativeAdmission) or mode not in {'offline_mock', 'live'}:
            raise PreparationError('Native verifier admission and explicit controller mode required')
        admission.check(live=mode == 'live')
        if mode == 'live' and (not isinstance(transport, NativeWireTransport) or transport.recorder is not recorder):
            raise PreparationError('Live native controller requires its full wire recorder')
        self.output = Path(output)
        if self.output.exists():
            raise PreparationError('Native run output exists; reconcile without replay')
        self.output.mkdir(parents=True)
        self.receipt, self.recorder = admission, recorder
        self.workspace_id = admission.operation['workspace_id']
        self.clock, self.sleeper, self.protector = clock, sleeper, protector
        self.secrets, self.progress = list(secrets), lambda stage: None
        self.case = next(row for row in read_strict(KIT / 'public-test-set.json') if row['id'] == 'PUB-01')
        self.state = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'mode': mode, 'status': 'reserved',
                      'refs': {}, 'stage': 'local_admission', 'official_result': False,
                      'scenario_helper_pass': False, 'evidence_complete': False, 'parent_evidence_preserved': True,
                      'platform_model_defaults_are_runtime_evidence': False,
                      'model_binding_authority': 'five_simulation_overrides_and_measured_iteration'}
        self.guard = NativeOperationGuard(state_directory, transport, admission, clock=clock, budget=budget)
        self.guard.case = self.case
        self.state['run_id'] = self.guard.run_id
        self.credit_after_attempted = self.preflight_passed = False
        self.checkpoint()

    def store_ref(self, key, extractor):
        def store(value):
            ref = identifier(extractor(value))
            forbidden = self.receipt.operation.get('forbidden_resource_ids', [])
            if ref in forbidden or ref in self.state['refs'].values():
                raise PreparationError('Native response aliases old or another owned resource')
            self.state['refs'][key] = ref
            self.guard.refs[key] = ref
            self.checkpoint()
        return store

    def inventory(self):
        self.guard.call('GET', '/api/v2/simulations', params={'workspaceId': self.workspace_id, 'limit': 1000, 'offset': 0})
        if self.guard.remote_idle_at is None:
            raise PreparationError('Native workspace active/unknown/incompletely listed')

    def versions(self):
        wf = self.state['refs']['workflow_id']
        result = self.guard.call('GET', '/api/v2/versions/workflow/' + wf,
                    params={'workspaceId': self.workspace_id, 'entityId': wf, 'limit': 100, 'offset': 0})
        rows = result.get('versions')
        if (not isinstance(rows, list) or not rows or result.get('total') != len(rows)
                or any(not isinstance(r, dict) or type(r.get('versionNumber')) is not int
                       or r['versionNumber'] < 1 or r.get('entityId') != wf or r.get('workspaceId') != self.workspace_id for r in rows)):
            raise PreparationError('Native version inventory incomplete or belongs to another workflow')
        return max(rows, key=lambda row: row['versionNumber'])

    def graph(self, graph):
        if not isinstance(graph, dict) or retained_fields(self.receipt.workflow, graph):
            raise PreparationError('Native platform silently changed the admitted graph')
        no_lifecycle_actions(graph)
        no_added_actions(self.receipt.workflow, graph)
        # Freya may add agent_config defaults or omit them in a snapshot. They
        # do not identify the model used for this simulation. The guard binds
        # all five outgoing overrides; iteration_matches verifies actual params.

    def collect_credit_after(self):
        if self.credit_after_attempted:
            return
        self.credit_after_attempted = True
        if not self.preflight_passed or self.recorder.failed:
            self.state['credits_after_unavailable_reason'] = 'recorder_unavailable_no_further_network'
            return
        self.state['credits_after'] = self.credits()
        if 'credits_before' in self.state:
            self.state['observed_credit_delta'] = self.state['credits_before']['creditsLeft'] - self.state['credits_after']['creditsLeft']
        self.state['delta_is_exact_test_price'] = False

    def run(self):
        try:
            self.checkpoint('encrypted_recorder_preflight')
            self.recorder.preflight()
            self.preflight_passed = True
            self.state['credits_before'] = self.credits()
            if self.state['credits_before']['creditsLeft'] <= 0:
                raise PreparationError('No positive native pilot credits')
            self.inventory()
            self.checkpoint('fresh_native_publication')
            body = {'name': 'Native-A-PUB01 ' + self.guard.run_id, 'workflow': self.receipt.workflow}
            if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode()) >= 131072:
                raise PreparationError('Native actual publication body exceeds size cap')
            def dry_valid(value):
                if value.get('valid') is not True:
                    raise PreparationError('Native dry-run validation not confirmed')
            self.guard.call('POST', '/api/v2/workflow', body=body, params={'dryRun': 'true'}, validate_result=dry_valid)
            def created(value):
                item = value.get('workflow', {})
                if item.get('workspaceId') != self.workspace_id:
                    raise PreparationError('Native created workflow workspace differs')
                self.graph(item.get('workflow'))
                self.store_ref('workflow_id', lambda r: r['workflow']['id'])(value)
                self.store_ref('agent_id', lambda r: r['workflow']['agentId'])(value)
            self.guard.call('POST', '/api/v2/workflow', body=body, persist_result=created)
            version = self.versions()
            self.store_ref('version_id', lambda r: r['id'])(version)
            self.state['published_version_number'] = version['versionNumber']
            self.checkpoint()
            wf, ver = self.state['refs']['workflow_id'], self.state['refs']['version_id']
            if version.get('isPublished') is not True:
                self.inventory()
                self.guard.call('POST', '/api/v2/versions/workflow/' + wf + '/publish', body={'versionId': ver})
            latest = self.versions()
            if latest.get('id') != ver or latest.get('isPublished') is not True:
                raise PreparationError('Native own published version unconfirmed')
            detail = self.guard.call('GET', '/api/v2/versions/workflow/' + wf + '/' + ver,
                       params={'workspaceId': self.workspace_id, 'entityId': wf, 'versionId': ver}).get('version', {})
            if any(detail.get(k) != latest.get(k) for k in ('id', 'entityId', 'workspaceId', 'versionNumber', 'isPublished')):
                raise PreparationError('Native published snapshot identity mismatch')
            snapshot = detail.get('configSnapshot')
            if not isinstance(snapshot, dict):
                raise PreparationError('Native published snapshot missing')
            self.graph(snapshot.get('workflow', snapshot))
            self.state['published_graph_verified'] = True
            self.guard.phase = 'bank_ready'
            self.inventory()
            self.checkpoint('one_bank_session_persona_simulation')
            def session(value):
                variables = value.get('variables')
                if (not isinstance(variables, dict) or set(variables) != set(self.case['variables']) | {'customer_id'}
                        or any(variables.get(k) != v for k, v in self.case['variables'].items())
                        or any(not isinstance(v, str) for v in variables.values())
                        or not re.fullmatch('[A-Za-z0-9_-]+', variables['customer_id'])):
                    raise PreparationError('Native bank session variables differ from official case')
                self.customer_id = self.guard.customer_id = variables['customer_id']
                self.secrets.append(self.customer_id)
                persist_secret(self.output / 'bank-session.dpapi', self.customer_id, protector=self.protector)
                self.guard.variables = variables
                self.state['bank_customer_id_sha256'] = hashlib.sha256(self.customer_id.encode()).hexdigest()
                self.checkpoint()
            self.guard.call('POST', '/v1/practice/sessions', body={'case': 'PUB-01'}, persist_result=session)
            def store_persona(value):
                if not isinstance(value.get('personas'), list) or len(value['personas']) != 1:
                    raise PreparationError('Native persona result not singular')
                self.store_ref('persona_id', lambda r: r['personas'][0]['id'])(value)
            self.guard.call('POST', '/api/v2/personas', body={'personas': [persona(self.case)]}, persist_result=store_persona)
            self.inventory()
            request = one_case_payload(self.state['refs']['agent_id'], self.state['refs']['persona_id'], self.guard.variables,
                      name='Native-A-PUB01 ' + self.guard.run_id, agent_model=fixed_models()[0], judge_model=fixed_models()[1])
            request['agentVersionId'] = ver
            self.guard.call('POST', '/api/v2/simulations', body=request,
                            persist_result=self.store_ref('simulation_id', lambda r: r.get('simulation', r)['id']))
            sim = self.state['refs']['simulation_id']
            self.checkpoint('bounded_native_simulation_wait')
            status = None
            while status not in {'completed', 'failed'}:
                status = self.guard.call('GET', '/api/v2/simulations/' + sim).get('simulation', {}).get('status')
                self.state['remote_status'] = status
                self.checkpoint()
                if status not in {'running', 'completed', 'failed'}:
                    raise PreparationError('Native remote status unknown/paused; retain owner')
                if status == 'running':
                    self.sleeper(min(10, max(0, self.guard.deadline - self.clock())))
            self.checkpoint('conversation_bank_ledger_official_judge')
            # Read bank evidence even if the run has zero turns or its judge fails.
            ledger = self.guard.call('GET', '/v1/practice/ledger/' + self.customer_id)
            self.save('ledger.sanitized.json', ledger)
            score = ledger_score(self.case, ledger)
            models = {k: request[k] for k in MODEL_KEYS}
            rows = self.guard.call('GET', '/api/v2/simulations/' + sim + '/iterations').get('iterations')
            if not isinstance(rows, list) or len(rows) != 1 or not iteration_matches(rows[0], ver, self.state['published_version_number'], models):
                raise PreparationError('Native measured version/models mismatch')
            self.store_ref('iteration_id', lambda r: r['id'])(rows[0])
            it = self.state['refs']['iteration_id']
            detail = self.guard.call('GET', '/api/v2/simulations/' + sim + '/iterations/' + it, params={'limit': 100, 'offset': 0})
            if (detail.get('runsTotal') != 1 or not isinstance(detail.get('runs'), list) or len(detail['runs']) != 1
                    or detail.get('iteration', {}).get('id') != it
                    or not iteration_matches(detail.get('iteration'), ver, self.state['published_version_number'], models)):
                raise PreparationError('Native measured run/version/models not singular')
            run = detail['runs'][0]
            if run.get('personaId') != self.state['refs']['persona_id']:
                raise PreparationError('Native measured persona mismatch')
            self.store_ref('simulation_call_id', lambda r: r['callId'])(run)
            call_id = self.state['refs']['simulation_call_id']
            call = self.guard.call('GET', '/api/v2/call/' + call_id).get('call')
            try:
                lifecycle = verify_simulation_call_binding(run, call, workspace_id=self.workspace_id,
                    workflow_id=wf, simulation_id=sim, persona_id=self.state['refs']['persona_id'])
            except LifecycleError as exc:
                self.save('call-lifecycle-evidence.json', {'valid': False, 'diagnostic': str(exc),
                    'speech_completion_proven': False, 'native_variable_ingress_proven': False})
                raise PreparationError('Native simulation call lifecycle not correlated') from None
            self.save('call-lifecycle-evidence.json', lifecycle)
            ownership = None
            if any(isinstance(row.get('args'), dict) and 'customer_id' not in row['args']
                   for row in ledger.get('calls', []) if isinstance(row, dict)):
                ownership = derive_session_ledger_ownership(ledger, self.customer_id,
                    ledger_request={'method': 'GET', 'path': '/v1/practice/ledger/' + self.customer_id},
                    simulation_request=request, iteration=detail['iteration'], run=run, call=call,
                    workspace_id=self.workspace_id, workflow_id=wf, agent_id=self.state['refs']['agent_id'],
                    version_id=ver, version_number=self.state['published_version_number'], simulation_id=sim,
                    persona_id=self.state['refs']['persona_id'], reservation_id=self.guard.run_id)
            chain = bank_chain_evidence(self.case, ledger, self.customer_id, ownership=ownership)
            self.save('bank-chain-evidence.json', chain)
            evidence = conversation_evidence(call, call_id)
            self.save('conversation-evidence.json', evidence)
            if isinstance(call, dict):
                self.save('transcript.sanitized.json', {'id': call.get('id'), 'endedAt': call.get('endedAt'),
                          'endedReason': call.get('endedReason'), 'messages': [{k: r[k] for k in ('role', 'message') if k in r}
                           for r in call.get('messages', []) if isinstance(r, dict)]})
            self.save('judge.sanitized.json', detail)
            self.collect_credit_after()
            if 'credits_after' not in self.state:
                raise PreparationError('Native final credits missing')
            self.guard.close()
            passed = (status == 'completed' and lifecycle['valid'] and lifecycle['lifecycle_ended']
                      and evidence['valid'] and score['pass'] and chain['valid'] and run.get('outcome') == 'pass')
            result = {'status': 'finished', 'scenario_helper_pass': passed, 'official_result': self.state['mode'] == 'live',
                      'evidence_complete': True, 'kit_ledger_score': score, 'bank_chain': chain, 'conversation': evidence,
                      'call_lifecycle': lifecycle,
                      'official_public_case_judge': {'outcome': run.get('outcome'), 'reason': run.get('reason'), 'checks': self.case['checks']},
                      'full_policy_independently_validated': False}
            self.save('result.json', result)
            self.state.update({k: result[k] for k in ('status', 'scenario_helper_pass', 'official_result', 'evidence_complete')})
        except Exception as exc:
            self.state.update(status='blocked_or_incomplete', official_result=False, scenario_helper_pass=False, evidence_complete=False,
                              blocker=str(exc) if isinstance(exc, PreparationError) else 'native_response_or_persistence_failure')
            try:
                self.collect_credit_after()
            except Exception:
                self.state['credits_after_unavailable_reason'] = 'read_blocked_or_failed_no_retry'
        finally:
            self.state.update(ended_at_utc=datetime.now(timezone.utc).isoformat(), requests=self.guard.requests,
                       mutations=self.guard.mutations, simulation_attempts_reserved=self.guard.simulations,
                       unknown_mutations=len(self.guard.pending), native_authorization_consumed=True)
            self.checkpoint()
        return sanitize(self.state, self.secrets)


def run_native_pilot(package_path, operation_path, admission_callback=None, live=False):
    """Public product CLI hook. No callback/unproven proof => no environment read.

    callback(package_path, operation_path) must return derive_native_admission's
    receipt. Offline default verifies only and opens no operation/network/key.
    Credential loading is the existing user's secure launcher responsibility.
    """
    blocked = {'status': 'blocked_before_credentials', 'credentials_read': False,
               'network_calls': 0, 'simulation_attempts_reserved': 0, 'official_result': False}
    if type(live) is not bool or not callable(admission_callback):
        return dict(blocked, blocker='independent_native_admission_callback_required')
    try:
        receipt = admission_callback(Path(package_path), Path(operation_path))
        if not isinstance(receipt, NativeAdmission):
            raise PreparationError('Native callback did not return an independently derived receipt')
        receipt.check(live=True)
    except Exception:
        return dict(blocked, blocker='native_conversation_write_restart_admission_unproven')
    if not live:
        return {'status': 'offline_admitted', 'credentials_read': False, 'network_calls': 0,
                'simulation_attempts_reserved': 0, 'official_result': False}
    # All producer gates have already completed before the first key read.
    key, team = os.environ.get('FREYA_API_KEY', ''), os.environ.get('FREYA_BANK_TEAM_KEY', '')
    output = ROOT / 'integration/runs' / ('native-' + receipt.operation['operation_id'])
    recorder = NativeResponseRecorder(output / 'http-diagnostics', secrets=(key, team))
    controller = NativePilotController(output, NativeWireTransport(key, team, recorder), receipt,
                state_directory=ROOT / 'integration/state', recorder=recorder, mode='live', secrets=(key, team))
    return controller.run()
