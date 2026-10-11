"""Strict host-side Freya response correlation and durable chat dispatch safety.

Documented provider callId/messageId and measured call-detail fields are used.
Response parsing is not authentication: the controller must supply its own TLS
transport result. Host storage is not native storage; this module never supplies
call_id/session_id variables or authorizes speech completion or bank writes.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import uuid

from .durable_replay import DurableJournal


class LifecycleError(ValueError):
    """Bounded diagnostics, without payloads or credentials."""


def _uuid(value):
    if not isinstance(value, str):
        raise LifecycleError('INVALID_PROVIDER_UUID')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise LifecycleError('INVALID_PROVIDER_UUID') from None
    if str(parsed) != value.lower() or parsed.int == 0:
        raise LifecycleError('INVALID_PROVIDER_UUID')
    return str(parsed)


def _text(value, limit=160):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise LifecycleError('INVALID_PROVIDER_FIELD')
    return value


def _digest(value, *, limit=4_000_000):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         allow_nan=False, separators=(',', ':')).encode()
    except (TypeError, ValueError, UnicodeError):
        raise LifecycleError('INVALID_JSON_PAYLOAD') from None
    if len(raw) > limit:
        raise LifecycleError('PAYLOAD_TOO_LARGE')
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise LifecycleError('INVALID_PROVIDER_TIMESTAMP') from None
    if parsed.tzinfo is None:
        raise LifecycleError('UNZONED_PROVIDER_TIMESTAMP')
    return parsed


def _result(response):
    _digest(response)
    if (not isinstance(response, dict) or response.get('success') is not True
            or not isinstance(response.get('data'), dict)
            or not isinstance(response['data'].get('result'), dict)):
        raise LifecycleError('INVALID_SUCCESS_ENVELOPE')
    return response['data']['result']


def verify_simulation_call_binding(run, call, *, workspace_id, workflow_id,
                                   simulation_id, persona_id):
    """Validate scoped run -> GET /call/{callId} identities, not TTS.

    A text response, an ended call or message.endTime is not audio completion.
    Official judge and ledger verification remain separate controller gates.
    The controller must authenticate and scope the iteration and call reads.
    id/workspaceId/assistantWorkflowId are measured call metadata required by
    this adapter, not guaranteed fields in the kit's Calls table. simulationId
    is optional: the documented correlation is run.callId -> GET /call/{id}.
    """
    if not isinstance(run, dict) or not isinstance(call, dict):
        raise LifecycleError('CALL_BINDING_OBJECT_REQUIRED')
    # Call detail includes per-event workflow snapshots; match protected capture.
    _digest([run, call], limit=8_000_000)
    call_id = _uuid(run.get('callId'))
    expected = {'id': call_id, 'workspaceId': _uuid(workspace_id),
                'assistantWorkflowId': _uuid(workflow_id)}
    expected_simulation_id = _uuid(simulation_id)
    if _uuid(run.get('personaId')) != _uuid(persona_id):
        raise LifecycleError('SIMULATION_PERSONA_MISMATCH')
    for key, value in expected.items():
        if _uuid(call.get(key)) != value:
            raise LifecycleError('CALL_DETAIL_IDENTITY_MISMATCH')
    simulation_id_present = 'simulationId' in call
    if simulation_id_present:
        if _uuid(call['simulationId']) != expected_simulation_id:
            raise LifecycleError('CALL_DETAIL_SIMULATION_MISMATCH')
    run_id = _text(run.get('id'))  # Kit calls this run id; do not assume UUID.
    started, ended = call.get('startedAt'), call.get('endedAt')
    if started is not None:
        _timestamp(started)
    lifecycle_ended = ended is not None
    if lifecycle_ended:
        finish = _timestamp(ended)
        if started is None or finish < _timestamp(started):
            raise LifecycleError('CALL_END_BEFORE_START_OR_START_MISSING')
    reason = call.get('endedReason')
    if reason is not None:
        _text(reason, 4096)
    return {'valid': True, 'call_id': call_id, 'run_id': run_id,
            'detail_response_correlated': True, 'lifecycle_ended': lifecycle_ended,
            'correlation_source': 'scoped_iteration_run_call_id_to_requested_call_details',
            'correlation_requires_authenticated_controller': True,
            'simulation_id': expected_simulation_id,
            'call_detail_simulation_id_present': simulation_id_present,
            'call_detail_simulation_id_verified': simulation_id_present,
            'ended_reason': reason, 'speech_completion_proven': False,
            'native_variable_ingress_proven': False,
            'provider_transport_authenticated_by_this_module': False}


class FreyaCallLifecycle:
    """One host-owned chat call with dispatch-before-send and no blind replay.

    Each next dispatch has a deterministic sequence slot. append_event checks
    this slot atomically under the durable journal writer lock, so two writers
    cannot turn separate request labels into two sends. Concurrent lock failure
    aborts; it is not automatically retried.
    """
    def __init__(self, journal: DurableJournal, *, workspace_id, workflow_id):
        if not isinstance(journal, DurableJournal):
            raise LifecycleError('HOST_DURABLE_JOURNAL_REQUIRED')
        self.journal = journal
        config = {'workspace_id': _uuid(workspace_id),
                  'workflow_id': _uuid(workflow_id)}
        self.journal.append_event('freya_lifecycle.config', config,
                                  event_id='freya-lifecycle-config')
        self.state()

    def state(self):
        state = {'call_id': None, 'lifecycle': 'not_started',
                 'pending': None, 'last_message_id': None, 'used_requests': []}
        for row in self.journal.events():
            if row['kind'] != 'external_event':
                continue
            event, data = row['data']['event_type'], row['data']['payload']
            if event == 'freya_lifecycle.dispatched':
                if state['pending'] is not None or data['request_id'] in state['used_requests']:
                    raise LifecycleError('LIFECYCLE_DISPATCH_CONFLICT')
                if data['kind'] == 'start':
                    if state['lifecycle'] != 'not_started':
                        raise LifecycleError('SECOND_CHAT_START_FORBIDDEN')
                elif state['lifecycle'] != 'active' or data['call_id'] != state['call_id']:
                    raise LifecycleError('INACTIVE_OR_WRONG_CALL_DISPATCH')
                state['used_requests'].append(data['request_id'])
                state['pending'] = data
                state['lifecycle'] = 'unknown_' + data['kind']
            elif event == 'freya_lifecycle.response':
                pending = state['pending']
                if pending is None or data['request_id'] != pending['request_id']:
                    raise LifecycleError('RESPONSE_WITHOUT_OWN_DISPATCH')
                if pending['kind'] != 'start' and data['call_id'] != state['call_id']:
                    raise LifecycleError('PROVIDER_CALL_CHANGED')
                state['call_id'] = data['call_id']
                state['last_message_id'] = data.get('message_id', state['last_message_id'])
                state['lifecycle'] = 'ended' if data['ended'] else 'active'
                state['pending'] = None
        state.update({'may_start': state['lifecycle'] == 'not_started',
                      'may_continue': state['lifecycle'] == 'active',
                      'may_retry_pending': False, 'native_identity_admission': False,
                      'speech_completion_proven': False,
                      'host_storage_only': True})
        return state

    def reserve_dispatch(self, request_id, kind, payload):
        """Persist before caller sends once. Timeout/crash keeps UNKNOWN."""
        if (not isinstance(request_id, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120}', request_id)
                or kind not in {'start', 'turn', 'end'} or not isinstance(payload, dict)):
            raise LifecycleError('INVALID_LIFECYCLE_REQUEST')
        state = self.state()
        if request_id in state['used_requests'] or state['pending'] is not None:
            raise LifecycleError('REQUEST_ALREADY_USED_OR_UNRESOLVED')
        if kind == 'start':
            if set(payload) - {'workflowId', 'message', 'customerVariables', 'versionId'}:
                raise LifecycleError('UNDOCUMENTED_OR_BRANCH_START_FIELDS')
            if (not state['may_start'] or 'callId' in payload
                    or _uuid(payload.get('workflowId')) != self._config()['workflow_id']):
                raise LifecycleError('INVALID_START_OR_SECOND_START')
            if 'customerVariables' in payload and not isinstance(payload['customerVariables'], dict):
                raise LifecycleError('INVALID_CUSTOMER_VARIABLES')
            if 'versionId' in payload:
                _uuid(payload['versionId'])
        elif not state['may_continue'] or _uuid(payload.get('callId')) != state['call_id']:
            raise LifecycleError('INACTIVE_OR_WRONG_CALL')
        elif set(payload) - {'callId', 'message'}:
            raise LifecycleError('UNDOCUMENTED_OR_BRANCH_CONTINUATION_FIELDS')
        if kind != 'end':
            _text(payload.get('message'), 10000)
        elif set(payload) != {'callId'}:
            raise LifecycleError('UNDOCUMENTED_END_FIELDS')
        data = {'request_id': request_id, 'kind': kind,
                'call_id': state['call_id'], 'request_sha256': _digest(payload)}
        added = self.journal.append_event('freya_lifecycle.dispatched', data,
                    event_id='freya-dispatch-slot-' + str(len(state['used_requests']) + 1))
        # append_event returns the existing external-event body on duplicate.
        # Only its newly appended record grants a one-time dispatch reservation.
        if added.get('kind') != 'external_event':
            raise LifecycleError('DISPATCH_SLOT_ALREADY_CONSUMED')
        return self.state()

    def _config(self):
        for row in self.journal.events():
            if row['kind'] == 'external_event' and row['data']['event_type'] == 'freya_lifecycle.config':
                return row['data']['payload']
        raise LifecycleError('LIFECYCLE_CONFIG_MISSING')

    def record_response(self, request_id, response):
        state = self.state()
        pending = state['pending']
        if pending is None or pending['request_id'] != request_id:
            raise LifecycleError('RESPONSE_WITHOUT_OWN_DISPATCH')
        result = _result(response)
        call_id = _uuid(result.get('callId'))
        if pending['kind'] != 'start' and call_id != state['call_id']:
            raise LifecycleError('PROVIDER_CALL_CHANGED')
        data = {'request_id': request_id, 'call_id': call_id,
                'response_sha256': _digest(response)}
        if pending['kind'] == 'end':
            if result.get('ended') is not True:
                raise LifecycleError('END_NOT_CONFIRMED')
            _text(result.get('endedReason'), 4096)
            if 'alreadyEnded' in result and type(result['alreadyEnded']) is not bool:
                raise LifecycleError('INVALID_ALREADY_ENDED_FLAG')
            data.update(ended=True, already_ended=result.get('alreadyEnded', False))
        else:
            if not isinstance(result.get('response'), str) or len(result['response']) > 1_000_000:
                raise LifecycleError('MISSING_CHAT_RESPONSE')
            data['message_id'] = _text(result.get('messageId'), 1024)
            if 'endedByAgent' in result and type(result['endedByAgent']) is not bool:
                raise LifecycleError('INVALID_AGENT_END_FLAG')
            data['ended'] = result.get('endedByAgent', False)
        self.journal.append_event('freya_lifecycle.response', data,
                                  event_id='freya-response-' + request_id)
        return self.state()
