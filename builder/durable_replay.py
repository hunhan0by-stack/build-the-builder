"""Offline, fsynced, hash-linked replay journal with fail-closed writer locks.

The journal records intent/dispatch/result, never performs a bank operation.
A restored dispatched request without a result is UNKNOWN, not retryable.
Hash links detect corruption; they are not signatures against full-file rewrites.
Lock files left by a crashed writer require explicit operator recovery. This
module never guesses that a lock is stale or silently removes another lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .models import strict_json

OPERATIONS = frozenset({'verify_identity', 'get_account', 'create_promise', 'add_note',
                        'schedule_callback', 'transfer_to_agent', 'close_call'})
FINGERPRINT_GUARDED_WRITES = frozenset({'create_promise', 'close_call'})
HASH = re.compile(r'[0-9a-f]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}')
MAX_RECORD_BYTES = 4_000_000
MAX_JOURNAL_BYTES = 100_000_000


class DurableReplayError(ValueError):
    """A bounded diagnostic code; never echoes request values or credentials."""


def _raw(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise DurableReplayError('NON_JSON_RECORD') from None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class DurableJournal:
    def __init__(self, path, scope, policy_hash, provider_idempotency=False):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise DurableReplayError('INVALID_SCOPE')
        if not isinstance(policy_hash, str) or not HASH.fullmatch(policy_hash):
            raise DurableReplayError('INVALID_POLICY_HASH')
        if type(provider_idempotency) is not bool:
            raise DurableReplayError('INVALID_PROVIDER_CAPABILITY')
        original = Path(path)
        if original.suffix != '.jsonl' or original.is_symlink():
            raise DurableReplayError('INVALID_JOURNAL_PATH')
        if any(p.is_symlink() for p in original.parents):
            raise DurableReplayError('SYMLINK_JOURNAL_PATH')
        self.path = original.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        self.scope, self.policy_hash = scope, policy_hash
        self.config = {'scope': scope, 'policy_hash': policy_hash,
                       'provider_idempotency': provider_idempotency}
        self._inflight = set()
        self._owner_pid = os.getpid()
        with self._lock():
            events = self._load()
            if not events:
                self._append(events, 'configuration', self.config)

    @contextmanager
    def _lock(self):
        token = _raw({'owner': str(uuid.uuid4()), 'pid': os.getpid()})
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise DurableReplayError('WRITER_LOCK_PRESENT_EXPLICIT_RECOVERY_REQUIRED') from None
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            # Release only our exact lock. Never clear a stale/replaced lock.
            try:
                if self.lock_path.is_symlink() or self.lock_path.read_bytes() != token:
                    raise DurableReplayError('WRITER_LOCK_OWNERSHIP_CHANGED')
                self.lock_path.unlink()
            except OSError:
                raise DurableReplayError('WRITER_LOCK_RELEASE_FAILED') from None

    def _load(self):
        if not self.path.exists():
            return []
        if not self.path.is_file() or self.path.is_symlink() or self.path.stat().st_size > MAX_JOURNAL_BYTES:
            raise DurableReplayError('JOURNAL_PATH_OR_SIZE_INVALID')
        raw = self.path.read_bytes()
        if not raw or not raw.endswith(b'\n'):
            raise DurableReplayError('JOURNAL_TRUNCATED')
        events, previous = [], '0' * 64
        try:
            for index, line in enumerate(raw.splitlines(), 1):
                record = strict_json(line.decode('utf-8'))
                if set(record) != {'version', 'seq', 'prev_sha256', 'scope', 'policy_hash',
                                   'event_id', 'kind', 'data', 'sha256'}:
                    raise DurableReplayError('JOURNAL_RECORD_INVALID')
                if record['version'] != 1 or type(record['seq']) is not int or record['seq'] != index or record['prev_sha256'] != previous:
                    raise DurableReplayError('JOURNAL_CHAIN_INVALID')
                if record['scope'] != self.scope or record['policy_hash'] != self.policy_hash:
                    raise DurableReplayError('JOURNAL_SCOPE_OR_POLICY_MISMATCH')
                unsigned = {k: v for k, v in record.items() if k != 'sha256'}
                if record['sha256'] != _sha(_raw(unsigned)):
                    raise DurableReplayError('JOURNAL_HASH_INVALID')
                expected_id = _sha(_raw({'scope': self.scope, 'seq': index,
                                         'kind': record['kind'], 'data': record['data']}))
                if record['event_id'] != expected_id:
                    raise DurableReplayError('JOURNAL_EVENT_ID_INVALID')
                previous = record['sha256']
                events.append(record)
            if events[0]['kind'] != 'configuration' or events[0]['data'] != self.config:
                raise DurableReplayError('JOURNAL_CONFIGURATION_MISMATCH')
            self._state(events)  # Reject invalid event transitions even if resealed.
            return events
        except DurableReplayError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise DurableReplayError('JOURNAL_CORRUPT') from None

    def _append(self, events, kind, data):
        unsigned = {'version': 1, 'seq': len(events) + 1,
                    'prev_sha256': events[-1]['sha256'] if events else '0' * 64,
                    'scope': self.scope, 'policy_hash': self.policy_hash,
                    'event_id': _sha(_raw({'scope': self.scope, 'seq': len(events) + 1,
                                           'kind': kind, 'data': data})), 'kind': kind,
                    'data': copy.deepcopy(data)}
        record = {**unsigned, 'sha256': _sha(_raw(unsigned))}
        raw = _raw(record) + b'\n'
        if len(raw) > MAX_RECORD_BYTES or (self.path.stat().st_size if self.path.exists() else 0) + len(raw) > MAX_JOURNAL_BYTES:
            raise DurableReplayError('JOURNAL_SIZE_LIMIT')
        # Validate before persisting; a journal never writes an invalid transition.
        self._state(events + [record])
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'ab') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        events.append(record)
        return copy.deepcopy(record)

    def _state(self, events):
        requests, speech, external = {}, {}, {}
        latest_snapshot = None
        for i, event in enumerate(events):
            kind, data = event['kind'], event['data']
            if not isinstance(data, dict):
                raise DurableReplayError('JOURNAL_EVENT_INVALID')
            if kind == 'configuration':
                if i != 0 or data != self.config:
                    raise DurableReplayError('JOURNAL_CONFIGURATION_MISMATCH')
            elif kind == 'request_prepared':
                rid = data['request_id']
                if not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid) or rid in requests or data['operation'] not in OPERATIONS or not isinstance(data['payload'], dict):
                    raise DurableReplayError('JOURNAL_REQUEST_INVALID')
                fingerprint = self._fingerprint(data['operation'], data['payload'])
                if data['fingerprint'] != fingerprint or data['payload_sha256'] != _sha(_raw(data['payload'])):
                    raise DurableReplayError('JOURNAL_REQUEST_FINGERPRINT_INVALID')
                self._check_write_fingerprint(data['operation'], fingerprint, requests)
                requests[rid] = {**copy.deepcopy(data), 'state': 'prepared', 'result': None, 'status': None}
            elif kind == 'request_dispatched':
                request = requests[data['request_id']]
                if request['state'] != 'prepared':
                    raise DurableReplayError('JOURNAL_DOUBLE_DISPATCH')
                request['state'] = 'dispatched'
            elif kind in {'request_result', 'request_reconciled'}:
                request = requests[data['request_id']]
                if request['state'] not in {'dispatched', 'unknown'} or data['status'] not in {'confirmed', 'failed', 'unknown'} or not isinstance(data['result'], dict):
                    raise DurableReplayError('JOURNAL_RESULT_TRANSITION_INVALID')
                if (data['status'] == 'confirmed' and data['result'].get('ok') is not True) or (data['status'] == 'failed' and data['result'].get('ok') is not False):
                    raise DurableReplayError('RESULT_STATUS_PAYLOAD_MISMATCH')
                request.update(state=data['status'], status=data['status'], result=copy.deepcopy(data['result']))
            elif kind == 'speech_created':
                sid = data['speech_event_id']
                if sid in speech or not isinstance(data['text'], str) or data['text_sha256'] != _sha(data['text'].encode('utf-8')):
                    raise DurableReplayError('JOURNAL_SPEECH_INVALID')
                speech[sid] = {**copy.deepcopy(data), 'acknowledged': False, 'dispatched': False}
            elif kind == 'speech_dispatched':
                row = speech[data['speech_event_id']]
                if row['dispatched'] or row['acknowledged']:
                    raise DurableReplayError('JOURNAL_DOUBLE_SPEECH_DISPATCH')
                row['dispatched'] = True
            elif kind == 'speech_acknowledged':
                row = speech[data['speech_event_id']]
                if row['acknowledged'] or data['text_sha256'] != row['text_sha256']:
                    raise DurableReplayError('JOURNAL_SPEECH_ACK_CONFLICT')
                row['acknowledged'] = True
            elif kind == 'snapshot':
                latest_snapshot = copy.deepcopy(data['snapshot'])
                if not isinstance(latest_snapshot, dict):
                    raise DurableReplayError('JOURNAL_SNAPSHOT_INVALID')
            elif kind == 'external_event':
                eid = data['external_event_id']
                if eid in external:
                    raise DurableReplayError('JOURNAL_DUPLICATE_EXTERNAL_EVENT')
                external[eid] = copy.deepcopy(data)
            else:
                raise DurableReplayError('JOURNAL_EVENT_KIND_INVALID')
        return requests, speech, external, latest_snapshot

    def _fingerprint(self, operation, payload):
        return _sha(_raw({'scope': self.scope, 'policy_hash': self.policy_hash,
                          'operation': operation, 'payload_sha256': _sha(_raw(payload))}))

    @staticmethod
    def _check_write_fingerprint(operation, fingerprint, requests):
        # A new bridge ID must not turn an unresolved/confirmed identical write
        # into a second dispatch. A failed result may permit a new explicit
        # attempt; its policy budget is enforced by the bridge, not this journal.
        if operation in FINGERPRINT_GUARDED_WRITES and any(
                row['fingerprint'] == fingerprint and row['state'] in
                {'prepared', 'dispatched', 'unknown', 'confirmed'}
                for row in requests.values()):
            raise DurableReplayError('WRITE_FINGERPRINT_ALREADY_PENDING_OR_CONFIRMED')

    def _view_request(self, request):
        result = copy.deepcopy(request)
        if result['state'] == 'dispatched' and (os.getpid() != self._owner_pid or result['request_id'] not in self._inflight):
            result['state'] = 'unknown'
        result['may_dispatch'] = result['state'] == 'prepared'
        result['provider_idempotency'] = self.config['provider_idempotency']
        return result

    def prepare_request(self, operation, payload, request_id=None):
        if operation not in OPERATIONS or not isinstance(payload, dict):
            raise DurableReplayError('INVALID_REQUEST')
        fingerprint = self._fingerprint(operation, payload)
        rid = request_id if request_id is not None else 'request-' + fingerprint
        if not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid):
            raise DurableReplayError('INVALID_REQUEST_ID')
        with self._lock():
            events = self._load()
            requests = self._state(events)[0]
            if rid in requests:
                if requests[rid]['fingerprint'] != fingerprint:
                    raise DurableReplayError('REQUEST_ID_PAYLOAD_CONFLICT')
                return self._view_request(requests[rid])
            self._check_write_fingerprint(operation, fingerprint, requests)
            data = {'request_id': rid, 'operation': operation, 'payload': copy.deepcopy(payload),
                    'payload_sha256': _sha(_raw(payload)), 'fingerprint': fingerprint}
            self._append(events, 'request_prepared', data)
            return self._view_request(self._state(events)[0][rid])

    def queue_request(self, operation, payload, request_id=None):
        return self.prepare_request(operation, payload, request_id)['request_id']

    def lookup_request(self, request_id):
        with self._lock():
            requests = self._state(self._load())[0]
            return self._view_request(requests[request_id]) if request_id in requests else None

    def mark_dispatched(self, request_id):
        with self._lock():
            events = self._load()
            requests = self._state(events)[0]
            if request_id not in requests or requests[request_id]['state'] != 'prepared':
                raise DurableReplayError('REQUEST_NOT_DISPATCHABLE')
            self._append(events, 'request_dispatched', {'request_id': request_id})
            self._inflight.add(request_id)
            return self._view_request(self._state(events)[0][request_id])

    def _result(self, request_id, result, status, reconciliation):
        if not isinstance(result, dict) or status not in {'confirmed', 'failed', 'unknown'}:
            raise DurableReplayError('INVALID_RESULT')
        if (status == 'confirmed' and result.get('ok') is not True) or (status == 'failed' and result.get('ok') is not False):
            raise DurableReplayError('RESULT_STATUS_PAYLOAD_MISMATCH')
        _raw(result)
        with self._lock():
            events = self._load()
            requests = self._state(events)[0]
            if request_id not in requests:
                raise DurableReplayError('RESULT_REQUEST_UNKNOWN')
            request = requests[request_id]
            if request['status'] is not None and request['status'] == status and _raw(request['result']) == _raw(result):
                return self._view_request(request)
            if request['state'] in {'confirmed', 'failed'}:
                raise DurableReplayError('RESULT_REPLAY_CONFLICT')
            if request['state'] == 'prepared':
                raise DurableReplayError('RESULT_BEFORE_DISPATCH')
            if not reconciliation and (os.getpid() != self._owner_pid or request_id not in self._inflight):
                raise DurableReplayError('EXPLICIT_RECONCILIATION_REQUIRED')
            kind = 'request_reconciled' if reconciliation else 'request_result'
            self._append(events, kind, {'request_id': request_id, 'result': copy.deepcopy(result), 'status': status})
            self._inflight.discard(request_id)
            return self._view_request(self._state(events)[0][request_id])

    def record_result(self, request_id, result, status='confirmed'):
        return self._result(request_id, result, status, False)

    def reconcile_result(self, request_id, result, status='confirmed'):
        return self._result(request_id, result, status, True)

    def append_speech(self, event_id, text):
        if not isinstance(event_id, str) or not IDENTIFIER.fullmatch(event_id) or not isinstance(text, str) or not text:
            raise DurableReplayError('INVALID_SPEECH')
        with self._lock():
            events = self._load()
            rows = self._state(events)[1]
            if event_id in rows:
                if rows[event_id]['text'] != text:
                    raise DurableReplayError('SPEECH_EVENT_CONFLICT')
                return copy.deepcopy(rows[event_id])
            self._append(events, 'speech_created', {'speech_event_id': event_id, 'text': text,
                                                   'text_sha256': _sha(text.encode('utf-8'))})
            return copy.deepcopy(self._state(events)[1][event_id])

    def mark_speech_dispatched(self, event_id):
        with self._lock():
            events = self._load()
            row = self._state(events)[1].get(event_id)
            if row is None or row['dispatched'] or row['acknowledged']:
                raise DurableReplayError('SPEECH_NOT_DISPATCHABLE')
            self._append(events, 'speech_dispatched', {'speech_event_id': event_id})

    def acknowledge_speech(self, event_id, text_sha256):
        with self._lock():
            events = self._load()
            row = self._state(events)[1].get(event_id)
            if row is None or row['text_sha256'] != text_sha256:
                raise DurableReplayError('SPEECH_ACK_EVENT_MISMATCH')
            if row['acknowledged']:
                return copy.deepcopy(row)
            self._append(events, 'speech_acknowledged', {'speech_event_id': event_id, 'text_sha256': text_sha256})
            return copy.deepcopy(self._state(events)[1][event_id])

    def speech_state(self, event_id):
        with self._lock():
            row = self._state(self._load())[1].get(event_id)
            if row is None:
                return None
            result = copy.deepcopy(row)
            result['may_render'] = not row['acknowledged'] and not row['dispatched']
            return result

    def append_event(self, kind, data, event_id=None):
        if not isinstance(kind, str) or not kind or not isinstance(data, dict):
            raise DurableReplayError('INVALID_EXTERNAL_EVENT')
        eid = event_id or 'event-' + _sha(_raw({'scope': self.scope, 'kind': kind, 'data': data}))
        if not isinstance(eid, str) or not IDENTIFIER.fullmatch(eid):
            raise DurableReplayError('INVALID_EXTERNAL_EVENT_ID')
        body = {'external_event_id': eid, 'event_type': kind, 'payload': copy.deepcopy(data)}
        with self._lock():
            events = self._load()
            row = self._state(events)[2].get(eid)
            if row is not None:
                if _raw(row) != _raw(body):
                    raise DurableReplayError('EXTERNAL_EVENT_CONFLICT')
                return copy.deepcopy(row)
            return self._append(events, 'external_event', body)

    def save_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise DurableReplayError('INVALID_SNAPSHOT')
        with self._lock():
            events = self._load()
            previous = self._state(events)[3]
            if previous is not None and _raw(previous) == _raw(snapshot):
                return copy.deepcopy(previous)
            self._append(events, 'snapshot', {'snapshot': copy.deepcopy(snapshot)})
            return copy.deepcopy(snapshot)

    def latest_snapshot(self):
        with self._lock():
            return self._state(self._load())[3]

    def events(self):
        with self._lock():
            return copy.deepcopy(self._load())

