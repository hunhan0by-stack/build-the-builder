"""One-shot HOST bank dispatch for PlatformBridge, using its durable journal.

This is not native Freya storage or an agent-to-host callback. The injected
transport is the trusted host wire boundary and must perform exactly one request
without retrying. Request IDs stay local: the bank contract has no idempotency
header/field. A dispatched write without a confirmed result is never resent.
There is no promises-read API with which this module could reconcile uncertainty.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json

from .models import FoundationError, strict_json


MAX_BODY_BYTES = 65_536


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class BankDispatch:
    """Local correlation metadata plus the exact, separately bounded wire shape."""
    request_id: str
    operation: str
    method: str
    url: str
    body: bytes

    def wire_request(self):
        return {"method": self.method, "url": self.url,
                "headers": {"Content-Type": "application/json"}, "body": self.body}


@dataclass(frozen=True)
class BankResponse:
    """Returned only by the trusted injected transport, never customer input."""
    request_id: str
    http_status: int
    body: bytes


class FreyaBankSafety:
    """Dispatch the bridge's already-authorized request, never arbitrary payloads.

    The durable reservation precedes the wire call. A crash in either direction
    leaves the reservation consumed. Bridge construction replays a durable
    confirmed result; an unknown result stays quarantined. Repeated dispatcher
    calls do not allocate another request ID or perform another bank write.
    """
    def __init__(self, bridge, transport):
        if not callable(transport):
            raise FoundationError("BANK_TRANSPORT_REQUIRED")
        if bridge.journal.config.get("provider_idempotency") is not False:
            raise FoundationError("UNSUPPORTED_PROVIDER_IDEMPOTENCY")
        self.bridge, self.transport = bridge, transport
        self._bridge = bridge
        self._journal = bridge.journal
        self._journal_binding = self._current_journal_binding()
        self._source_call_binding = bridge._current_binding()
        self._contract_sha256 = hashlib.sha256(_bytes(bridge.contract.serializable())).hexdigest()
        self._transport_attempts = 0

    @property
    def transport_attempts(self):
        return self._transport_attempts

    def _current_journal_binding(self):
        journal = self._journal
        return {"path": str(journal.path.resolve()), "lock_path": str(journal.lock_path.resolve()),
                "scope": journal.scope, "policy_hash": journal.policy_hash,
                "config": copy.deepcopy(journal.config)}

    def _bound(self):
        # Check before snapshot(), which reads/reconciles the journal. Rebinding
        # to another store with the same pending ID must not grant a fresh send.
        if self.bridge is not self._bridge or self.bridge.journal is not self._journal:
            raise FoundationError("BANK_JOURNAL_INSTANCE_CHANGED")
        if self._current_journal_binding() != self._journal_binding:
            raise FoundationError("BANK_JOURNAL_BINDING_CHANGED")
        if hashlib.sha256(_bytes(self.bridge.contract.serializable())).hexdigest() != self._contract_sha256:
            raise FoundationError("BANK_CONTRACT_CHANGED")
        if self.bridge._current_binding() != self._source_call_binding:
            raise FoundationError("BANK_SOURCE_OR_CALL_CHANGED")
        self.bridge.snapshot()  # independently enforces source/call/receipt pins

    def dispatch_pending(self):
        self._bound()
        pending = self.bridge.snapshot()["pending_bank_requests"]
        if not pending:
            return self._outcome(None, "no_pending")
        if len(pending) != 1:
            raise FoundationError("EXACTLY_ONE_PENDING_BANK_REQUEST_REQUIRED")
        request = pending[0]
        rid = request["request_id"]
        if request["may_dispatch"] is not True:
            # No external reconciliation path exists in the seven-tool contract.
            # Never turn a local replay ID or caller result into provider proof.
            return self._outcome(rid, "unknown")
        operation = self.bridge.contract.operations[request["operation"]]
        self.bridge.contract.validate_request(operation.name, request["payload"])
        body = _bytes(request["payload"])
        if len(body) > MAX_BODY_BYTES or operation.method != "POST":
            raise FoundationError("BANK_WIRE_REQUEST_UNSUPPORTED")
        dispatch = BankDispatch(rid, operation.name, operation.method, operation.url, body)
        self.bridge.mark_dispatched(rid)  # fsync intent/dispatch BEFORE sending
        self._bound()
        self._transport_attempts += 1
        try:
            response = self.transport(dispatch)
        except Exception:
            # Timeout/disconnect may occur after the provider has committed.
            # Neither failure nor success may be inferred from this exception.
            self._bound()
            self.bridge.resume_bank(rid, {"transport_outcome": "unknown"})
            return self._outcome(rid, "unknown")
        # BaseException (e.g. process termination) deliberately escapes: recovery
        # sees the consumed dispatch, not a fresh request that can be resent.
        self._bound()
        result = self._decode(dispatch, response)
        self.bridge.resume_bank(rid, result)
        state = self.bridge.journal.lookup_request(rid)["state"]
        return self._outcome(rid, state)

    def _decode(self, dispatch, response):
        if (type(response) is not BankResponse or response.request_id != dispatch.request_id
                or type(response.http_status) is not int or type(response.body) is not bytes
                or len(response.body) > MAX_BODY_BYTES):
            return {"transport_outcome": "unknown", "diagnostic": "BANK_RESPONSE_CORRELATION_OR_BOUND_INVALID"}
        # Official contract explicitly documents a 500 as not recorded. This is
        # a definitive failure, but this controller STILL never auto-retries it.
        operation = self.bridge.contract.operations[dispatch.operation]
        description = operation.response_descriptions.get("500", "")
        if response.http_status == 500 and "Nothing was recorded" in description:
            return {"ok": False, "error": "documented_bank_500_not_recorded"}
        if response.http_status != 200:
            return {"transport_outcome": "unknown", "diagnostic": "BANK_HTTP_OUTCOME_UNKNOWN"}
        try:
            result = strict_json(response.body.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            self.bridge.contract.business_result(dispatch.operation, result)
            return copy.deepcopy(result)
        except (ValueError, TypeError, UnicodeError, FoundationError):
            return {"transport_outcome": "unknown", "diagnostic": "BANK_RESPONSE_INVALID"}

    def _outcome(self, rid, status):
        self._bound()
        snapshot = self.bridge.snapshot()
        return {"request_id": rid, "status": status, "phase": snapshot["phase"],
                "promise_confirmed": bool(snapshot["promise_id"]),
                "transport_attempts_this_instance": self.transport_attempts,
                "automatic_retries": 0, "provider_idempotency": False,
                "persistence_authority": "host_durable_journal",
                "native_persistence_supported": False,
                "native_bank_dispatch_admitted": False, "publish_allowed": False}
