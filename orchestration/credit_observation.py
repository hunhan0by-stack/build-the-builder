"""Captured workspace-credit observations, never isolated test-price or PASS.

Contract: platform-docs/api-reference/workspace-credits/get-credits.md.
Use the existing client's API/capture path; no transport, credentials, retries,
disk state or native persistence is implemented here. A recorder must remain
healthy and unsealed until the after observation is complete.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid

from builder.models import FoundationError, strict_json


def utc_now():
    return datetime.now(timezone.utc)


def _fail(code):
    raise FoundationError(code) from None


def _workspace(value):
    try:
        parsed = uuid.UUID(value)
        if parsed.int == 0 or str(parsed) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        _fail('CREDIT_WORKSPACE_BINDING_INVALID')
    return value


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _values(value):
    if not isinstance(value, dict) or not all(_number(value.get(key))
                for key in ('creditsLeft', 'creditUsdRate')):
        _fail('CREDIT_VALUES_INVALID_NO_RETRY')
    return {key: value[key] for key in ('creditsLeft', 'creditUsdRate')}


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value)
        if (not isinstance(value, str) or len(value) > 40 or not value.endswith('+00:00')
                or parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0):
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError, OverflowError):
        _fail('CREDIT_TIMESTAMP_INVALID_NO_READ')


class CreditObservation:
    """One before/after pair pinned to the SDK's already-resolved workspace.

Failures and attempted reads latch locally. This class does not make restart
or idempotency claims; the client's existing recorder owns dispatch evidence.
"""
    def __init__(self, client, workspace_id, *, clock=utc_now):
        self.client = client
        self.workspace_id = _workspace(workspace_id)
        self.endpoint = '/api/v2/workspaces/' + self.workspace_id + '/credits'
        self.clock = clock
        self.observation_id = str(uuid.uuid4())
        self._before = None
        self._before_attempted = False
        self._after_attempted = False
        self._evidence_sink = None
        self._evidence_recorder = None
        self._pin_workspace()

    def _pin_workspace(self):
        # Calling workspace_id() could itself dispatch. Only its existing pin
        # from publication is admitted; never resolve it implicitly here.
        if getattr(getattr(self.client, 'kit', None), '_ws', None) != self.workspace_id:
            _fail('CREDIT_WORKSPACE_BINDING_INVALID')

    def _healthy(self):
        self._pin_workspace()
        sink = getattr(self.client, 'evidence', None)
        recorder = getattr(sink, 'recorder', None)
        if self._evidence_sink is not None and (
                sink is not self._evidence_sink or recorder is not self._evidence_recorder):
            _fail('CREDIT_EVIDENCE_BINDING_CHANGED_NO_READ')
        if (sink is None or getattr(sink, 'failed', None) is not False
                or getattr(sink, 'sealed', None) is not False
                or getattr(sink, 'pending', 'missing') is not None
                or getattr(getattr(sink, 'recorder', None), 'failed', None) is not False
                or not isinstance(getattr(sink, 'exchanges', None), dict)
                or not isinstance(getattr(sink, 'cache', None), dict)):
            _fail('CREDIT_EVIDENCE_NOT_READY_NO_READ')
        if self._evidence_sink is None:
            self._evidence_sink, self._evidence_recorder = sink, recorder
        return sink

    def _stamp(self):
        try:
            value = self.clock()
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError()
            stamp = value.astimezone(timezone.utc).isoformat()
        except Exception:
            _fail('CREDIT_TIMESTAMP_INVALID_NO_READ')
        _parse(stamp)
        return stamp

    def _read(self, phase, started):
        sink = self._healthy()
        previous = getattr(self.client, 'last_exchange', None)
        try:
            returned = self.client.api('GET', self.endpoint)
        except Exception:
            _fail('CREDIT_READ_FAILED_NO_RETRY')
        self._healthy()
        values = _values(returned)
        ticket = getattr(self.client, 'last_exchange', None)
        try:
            if ticket == previous or str(uuid.UUID(ticket)) != ticket:
                raise ValueError()
            pair = sink.exchanges[ticket]
            request, response = sink.cache[ticket]
            wire = strict_json(response['body'].decode('utf-8'))
            if (pair['status'] != 'CAPTURED' or pair['exchange_id'] != ticket
                    or request['method'] != 'GET' or request['path'] != self.endpoint
                    or request['params'] != {} or request['body_base64'] is not None
                    or response['http_status'] != 200 or wire.get('success') is not True
                    or _values(wire['data']['result']) != values):
                raise ValueError()
        except Exception:
            _fail('CREDIT_RESPONSE_CAPTURE_UNVERIFIED_NO_RETRY')
        ended = self._stamp()
        if _parse(ended) < _parse(started):
            _fail('CREDIT_CLOCK_ORDER_INVALID_NO_RETRY')
        return {'kind': 'workspace-credit-snapshot-v1', 'status': 'OBSERVED',
            'phase': phase, 'observation_id': self.observation_id,
            'workspace_id': self.workspace_id, 'method': 'GET', 'endpoint': self.endpoint,
            'request_started_at_utc': started, 'response_received_at_utc': ended,
            'capture_exchange_id': ticket, **values,
            'numeric_fields_sha256': hashlib.sha256(_raw(values)).hexdigest()}

    def before(self):
        if self._before_attempted:
            _fail('CREDIT_BEFORE_ALREADY_ATTEMPTED_NO_RETRY')
        self._before_attempted = True
        self._healthy()
        result = self._read('BEFORE', self._stamp())
        self._before = copy.deepcopy(result)
        return copy.deepcopy(result)

    def after(self, before_snapshot):
        if self._after_attempted:
            _fail('CREDIT_AFTER_ALREADY_ATTEMPTED_NO_RETRY')
        self._after_attempted = True
        try:
            before_valid = (self._before is not None and isinstance(before_snapshot, dict)
                            and _raw(before_snapshot) == _raw(self._before))
        except (TypeError, ValueError, OverflowError):
            before_valid = False
        if not before_valid:
            _fail('CREDIT_BEFORE_INVALID_NO_READ')
        self._healthy()
        start = self._stamp()
        if _parse(start) < _parse(self._before['response_received_at_utc']):
            _fail('CREDIT_CLOCK_ORDER_INVALID_NO_READ')
        after = self._read('AFTER', start)
        if after['creditUsdRate'] != self._before['creditUsdRate']:
            _fail('CREDIT_RATE_CHANGED_NO_COMPARISON')
        decrease = self._before['creditsLeft'] - after['creditsLeft']
        try:
            if not math.isfinite(decrease):
                raise ValueError()
        except (OverflowError, ValueError):
            _fail('CREDIT_DELTA_INVALID_NO_COMPARISON')
        return {'kind': 'workspace-credit-observation-v1', 'status': 'OBSERVED_WORKSPACE_DELTA',
            'workspace_id': self.workspace_id, 'observation_id': self.observation_id,
            'before': copy.deepcopy(self._before), 'after': after,
            'observed_workspace_credit_decrease': decrease,
            'isolated_test_price_verified': False, 'official_test_pass_verified': False,
            'retries': 0, 'attribution_note': 'Workspace delta only; top-ups or parallel activity may affect it.'}
