"""Passive wiring for the documented Freya browser SDK, with durable host evidence.

The SDK reference documents on/off/connect and speech callbacks, but exposes NO
runtime callId/sessionId or utterance ID. A host-local observation ID therefore
is not a provider identity. This integration cannot authorize native close_call.
No bank calls, token requests, public server or network operations run here.

Host use: construct an SDKReceiptAdapter with a host DurableJournal, attach an
observer to the exact client, register_expected before TTS, then reserve_delivery
before delivery. On restart a reserved utterance is NEVER delivered again.
The browser source below wires the actual JS SDK, not a guessed Python SDK.
Its host capability must be installed by a trusted browser/host embedding; it is
NOT a public endpoint which signs arbitrary browser dictionaries. No signing key
is embedded in JS. Caller-supplied vars or end-of-call reports are not receipts.

Authoritative local contracts:
  platform-docs/guides/embed/reference.md (on/off; speaking/transcript callbacks)
  platform-docs/guides/embed/overview.md (single-use session embed token)
  platform-docs/api-reference/webhooks/end-of-call-report-webhook.md (posthoc)
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import threading
import uuid

from .models import FoundationError, strict_json
from .platform_receipts import IDENTIFIER, SDKReceiptAdapter


EVENTS = ("botStartedSpeaking", "botTranscript", "botStoppedSpeaking",
          "userStartedSpeaking", "disconnected", "error")


def _native_digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise FoundationError("Native speech evidence must be finite JSON") from None
    return hashlib.sha256(encoded).hexdigest()


class NativeSpeechBatchBinding:
    """Bind the exported renderer's *whole* TTS turn to SDK observations.

    Native Freya currently joins fresh semantic events into one TTS response.
    Registering only a goodbye string would silently discard earlier speech or
    associate the stop callback with the wrong event. This host-side bridge
    checks the full ordered batch and maps its receipt back to each exact event.
    Native event IDs remain our IDs. No new SDK method, provider identity or
    platform control channel is invented. Consequently require_native_close()
    still fails until an official provider binding/ingress exists.
    """
    def __init__(self, observer):
        if not isinstance(observer, FreyaCompletionBinding):
            raise FoundationError("Documented callback observer required")
        self.observer = observer
        self._config = copy.deepcopy(observer._config)
        self._batches = {}
        for row in observer.adapter.journal.events():
            if (row["kind"] == "external_event"
                    and row["data"]["event_type"] == "freya_native_speech_batch"):
                record = row["data"]["payload"]
                if record.get("configuration") != self._config:
                    raise FoundationError("Native speech observer binding changed on restore")
                self._validate_record(record)
                existing = self._batches.get(record["batch_id"])
                if existing is not None and existing != record:
                    raise FoundationError("Native speech batch changed on restore")
                self._batches[record["batch_id"]] = copy.deepcopy(record)

    def _validate_events(self, events, rendered_text):
        if not isinstance(events, list) or not 1 <= len(events) <= 64:
            raise FoundationError("Bounded nonempty native semantic batch required")
        fields = {"event_id", "kind", "text", "evidence", "call_id", "session_id", "generation"}
        identities = set()
        generation = events[0].get("generation") if isinstance(events[0], dict) else None
        for event in events:
            if (not isinstance(event, dict) or set(event) != fields
                    or not isinstance(event["event_id"], str)
                    or not IDENTIFIER.fullmatch(event["event_id"])
                    or event["event_id"] in identities
                    or not isinstance(event["kind"], str) or not IDENTIFIER.fullmatch(event["kind"])
                    or not isinstance(event["text"], str) or not event["text"].strip()
                    or len(event["text"]) > 32000
                    or type(event["generation"]) is not int or event["generation"] < 1
                    or event["generation"] != generation
                    or event["call_id"] != self.observer.adapter.source.binding.call_id
                    or event["session_id"] != self.observer.adapter.source.binding.session_id):
                raise FoundationError("Native speech semantic identity or host scope mismatch")
            identities.add(event["event_id"])
        if (not isinstance(rendered_text, str) or len(rendered_text) > 32000
                or rendered_text != " ".join(event["text"] for event in events)):
            raise FoundationError("Native TTS must equal the complete ordered semantic batch")
        return _native_digest(events)

    def _validate_record(self, record):
        if set(record) != {"version", "configuration", "batch_id", "events", "events_sha256", "rendered_text"}:
            raise FoundationError("Native speech batch record shape invalid")
        digest = self._validate_events(record["events"], record["rendered_text"])
        if (record["version"] != "freya-native-speech-batch-1"
                or record["events_sha256"] != digest
                or record["batch_id"] != "native-tts-" + digest):
            raise FoundationError("Native speech batch digest mismatch")

    def register_native_batch(self, events, rendered_text):
        """Use fresh exported events and exact native_speak text before TTS."""
        with self.observer._lock:
            self.observer._active()
            digest = self._validate_events(events, rendered_text)
            record = {"version": "freya-native-speech-batch-1", "configuration": self._config,
                      "batch_id": "native-tts-" + digest, "events": copy.deepcopy(events),
                      "events_sha256": digest, "rendered_text": rendered_text}
            old = self._batches.get(record["batch_id"])
            if old is not None and old != record:
                raise FoundationError("Native batch configuration conflict")
            prior_events = {e["event_id"] for b in self._batches.values() for e in b["events"]}
            if old is None and (prior_events.intersection(e["event_id"] for e in events)
                                or len(self._batches) >= 64):
                raise FoundationError("Native semantic event reused or batch budget exceeded")
            if old is None:
                # Record binding before arming callbacks. Any write failure blocks
                # progress; no code here sends TTS or a bank request.
                self.observer.adapter._durable(self.observer.adapter.journal.append_event,
                    "freya_native_speech_batch", copy.deepcopy(record), event_id=record["batch_id"])
                self._batches[record["batch_id"]] = copy.deepcopy(record)
            self.observer.register_expected(record["batch_id"], rendered_text)
            return record["batch_id"]

    def register_native_output(self, variables):
        """Read actual native_driver output; never accept a completion boolean.

        The caller must already own this runtime observation. Serialized vars
        alone do not authenticate Freya origin. Journaled batches establish the
        exact prior prefix, so a skipped, replayed or rewritten turn is rejected.
        """
        with self.observer._lock:
            self.observer._active()
            if not isinstance(variables, dict) or variables.get("_native_has_speech") != "yes":
                raise FoundationError("Actual native speech output required")
            raw = variables.get("_native_state_json")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16000:
                raise FoundationError("Bounded native runtime state required")
            try:
                state = strict_json(raw)
            except (ValueError, TypeError, RecursionError):
                raise FoundationError("Native runtime state JSON invalid") from None
            events = state.get("events") if isinstance(state, dict) else None
            prior = [event for batch in self._batches.values() for event in batch["events"]]
            if (not isinstance(events, list) or not 1 <= len(events) <= 64
                    or type(state.get("event_counter")) is not int or state["event_counter"] != len(events)
                    or len(events) <= len(prior) or events[:len(prior)] != prior
                    or any(not isinstance(e, dict) or e.get("event_id") != "native-speech-" + str(i + 1)
                           for i, e in enumerate(events))
                    or state.get("call_id") != self.observer.adapter.source.binding.call_id
                    or state.get("session_id") != self.observer.adapter.source.binding.session_id):
                raise FoundationError("Native runtime event history changed, skipped or replayed")
            return self.register_native_batch(events[len(prior):], variables.get("_native_speech"))

    def reserve_native_delivery(self, batch_id):
        if batch_id not in self._batches:
            raise FoundationError("Unknown native speech batch")
        self.observer.reserve_delivery(batch_id)

    def completion_for(self, event_id):
        """Return authenticated *host observation* for an exact semantic event."""
        with self.observer._lock:
            current = {"observation_id": self.observer.observation_id,
                       "binding": asdict(self.observer.adapter.source.binding),
                       "source_configuration": self.observer.adapter.config}
            if current != self._config or self.observer._config != self._config:
                raise FoundationError("Native speech observer configuration changed")
            for record in self._batches.values():
                event = next((e for e in record["events"] if e["event_id"] == event_id), None)
                if event is None:
                    continue
                receipt = self.observer.receipt(record["batch_id"])
                observed = bool(receipt and self.observer.adapter.verify_receipt(receipt)
                                and receipt.get("text_sha256") == hashlib.sha256(record["rendered_text"].encode()).hexdigest())
                return {"event_id": event_id, "kind": event["kind"], "batch_id": record["batch_id"],
                        "semantic_event_sha256": _native_digest(event),
                        "host_observed_completion": observed,
                        "receipt": copy.deepcopy(receipt) if observed else None,
                        "provider_event_identity_attested": False,
                        "native_completion_ingress_available": False,
                        "can_authorize_close_call": False}
            raise FoundationError("Unknown native semantic event")

    def require_native_close(self, goodbye_event_id):
        """Never invoke close_call based on goodbye_spoken or relay signatures."""
        result = self.completion_for(goodbye_event_id)
        if result["kind"] != "goodbye":
            raise FoundationError("NATIVE_CLOSE_REQUIRES_EXACT_GOODBYE_EVENT")
        if not result["host_observed_completion"]:
            raise FoundationError("NATIVE_GOODBYE_COMPLETION_MISSING")
        # SDK callback data lacks provider IDs; no midcall native ingress is
        # documented. Local correlation is valuable evidence, never authority.
        raise FoundationError("NATIVE_COMPLETION_PROVIDER_BINDING_AND_INGRESS_UNAVAILABLE")


class FreyaCompletionBinding:
    """Single-instance callback capability; observations never claim Freya origin.

    `client` is the host's trusted SDK handle exposing documented on/off methods.
    Python callback wiring is also useful for host wrappers and local fixtures.
    Actual browser wiring is supplied by browser_observer_source(). An HTTP body
    cannot be submitted to this class as a trusted callback.
    """
    def __init__(self, adapter, client, observation_id):
        if not isinstance(adapter, SDKReceiptAdapter):
            raise FoundationError("Existing durable SDK receipt adapter required")
        if not isinstance(observation_id, str) or not IDENTIFIER.fullmatch(observation_id):
            raise FoundationError("Bounded host observation identifier required")
        if not callable(getattr(client, "on", None)) or not callable(getattr(client, "off", None)):
            raise FoundationError("Documented SDK on/off client interface required")
        # This module cannot independently authenticate provider runtime IDs.
        if adapter.source.transport_binding_confirmed:
            raise FoundationError("SDK reference does not attest runtime call/session identity")
        self.adapter, self._client = adapter, client
        self.observation_id = observation_id
        self._lock = threading.RLock()
        self._handlers, self._attached, self._retired = {}, False, False
        self._config = {"observation_id": observation_id,
                        "binding": asdict(adapter.source.binding),
                        "source_configuration": copy.deepcopy(adapter.config)}
        records = [row["data"]["payload"] for row in adapter.journal.events()
                   if row["kind"] == "external_event"
                   and row["data"]["event_type"] == "freya_observer_state"]
        if records and records[-1]["configuration"] != self._config:
            raise FoundationError("Observer host binding changed on restore")
        self._revision = records[-1]["revision"] if records else 0
        self._record("restored" if records else "created")

    def _record(self, stage):
        self._revision += 1
        try:
            self.adapter.journal.append_event("freya_observer_state",
                {"revision": self._revision, "stage": stage,
                 "configuration": copy.deepcopy(self._config),
                 "provider_runtime_binding_attested": False},
                event_id="freya-observer-state-" + str(self._revision))
        except Exception:
            self._retired = True
            self.adapter.state.update(blocked=True, diagnostic="SDK_OBSERVER_DURABLE_RECORD_FAILED")
            raise FoundationError("SDK_OBSERVER_DURABLE_RECORD_FAILED") from None

    def attach(self):
        with self._lock:
            if self._retired or self._attached:
                raise FoundationError("Observer cannot be attached twice or after retirement")
            self._record("attaching_before_connect")
            self._attached = True
            try:
                for kind in EVENTS:
                    # Exact handler identity is retained for documented off().
                    def handler(payload=None, *, _kind=kind):
                        return self._capture(_kind, payload)
                    self._handlers[kind] = handler
                    self._client.on(kind, handler)
            except Exception:
                self._retired = True
                self._detach_handlers()
                self.adapter.state.update(blocked=True, diagnostic="SDK_OBSERVER_REGISTRATION_FAILED")
                self._record("registration_failed")
                raise FoundationError("SDK_OBSERVER_REGISTRATION_FAILED") from None
            self._record("attached")
            return self

    def _detach_handlers(self):
        failed = False
        for kind, handler in list(self._handlers.items()):
            try:
                self._client.off(kind, handler)
            except Exception:
                failed = True
        self._handlers.clear()
        self._attached = False
        return failed

    def detach(self):
        with self._lock:
            self._retired = True  # old captured callbacks become unusable first
            failed = self._detach_handlers()
            if self.adapter.state["pending"]:
                self.adapter._block("SDK_OBSERVER_DETACHED_WITH_PENDING_UTTERANCE")
            self._record("detached")
            if failed:
                raise FoundationError("SDK_OBSERVER_UNSUBSCRIBE_FAILED")

    def _active(self):
        if self._retired or not self._attached:
            raise FoundationError("SDK observer is not active")
        if self._config != {"observation_id": self.observation_id,
                            "binding": asdict(self.adapter.source.binding),
                            "source_configuration": self.adapter.config}:
            raise FoundationError("SDK observer configuration changed")

    def register_expected(self, event_id, exact_text):
        with self._lock:
            self._active()
            return self.adapter.register_pending(event_id, exact_text)

    def reserve_delivery(self, event_id):
        """Durable no-resend reservation; caller performs actual TTS separately."""
        with self._lock:
            self._active()
            self.adapter.mark_dispatched(event_id)
            self._record("delivery_reserved")

    def _capture(self, kind, payload):
        with self._lock:
            self._active()
            if kind == "error":
                # Conservatively quarantine any actual transport error. Never
                # persist arbitrary error text or pretend the turn completed.
                if not isinstance(payload, dict) or not isinstance(payload.get("message"), str) or type(payload.get("fatal")) is not bool:
                    self.adapter._block("SDK_OBSERVER_ERROR_SHAPE_INVALID")
                kind, data = "reset", {}
            elif kind == "botTranscript":
                if not isinstance(payload, dict) or set(payload) != {"text"}:
                    self.adapter._block("SDK_OBSERVER_TRANSCRIPT_SHAPE_INVALID")
                data = copy.deepcopy(payload)
            else:
                if payload not in (None, {}):
                    self.adapter._block("SDK_OBSERVER_PAYLOADLESS_EVENT_INVALID")
                data = {}
            # Signing occurs only INSIDE the callback installed on this client.
            # Our HMAC authenticates the host observation, never provider origin.
            event = {"protocol": "freya-sdk-relay-1",
                     "binding": asdict(self.adapter.source.binding),
                     "sequence": self.adapter.state["last_sequence"] + 1,
                     "kind": kind, "payload": data,
                     "issued_at": self.adapter.source._time(), "nonce": uuid.uuid4().hex}
            return self.adapter.receive_envelope(
                {"event": event, "signature": self.adapter.source._sign_receipt(event)})

    def receipt(self, event_id):
        return self.adapter.receipt(event_id)

    def verify_platform_receipt(self, receipt):
        # A signed private relay cannot fill the SDK's undocumented identity gap.
        return False

    def snapshot(self):
        return {"version": "freya-passive-observer-1",
                "observation_id": self.observation_id, "attached": self._attached,
                "retired": self._retired, "adapter": self.adapter.snapshot(),
                "provider_runtime_binding_attested": False,
                "native_simulation_hook_available": False,
                "production_ready": False, "publication_ready": False,
                "bank_requests": 0, "network_calls_by_module": 0}


def inspect_end_of_call_report(report):
    """Extract posthoc IDs, never turn a webhook dictionary into speech proof.

    The OpenAPI contract does not document authentication for this webhook or
    a semantic utterance identifier. Alert-destination X-Freya-Signature docs
    describe another API; their protocol must not be guessed for this webhook.
    """
    if not isinstance(report, dict) or report.get("type") != "end-of-call-report":
        raise FoundationError("Documented end-of-call-report required")
    result = {}
    for key in ("callId", "workspaceId"):
        try:
            value = report[key]
            if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
                raise ValueError
            result[key] = value
        except (KeyError, ValueError, TypeError, AttributeError):
            raise FoundationError("Documented UUID call/workspace identifiers required") from None
    return {**result, "evidence_kind": "untrusted_posthoc_call_association",
            "provider_origin_verified": False, "semantic_completion_proven": False,
            "can_authorize_close_call": False}


def browser_observer_source():
    """Actual SDK registration/client wiring; no credentials or network in source.

    Usage: const observer = createFreyaCompletionObserver(window.Freya, options,
    trustedHostCapability); await observer.connect();
    options are the documented {baseUrl,token,mode,metadata,customerVariables}.
    Caller acquires token separately through documented POST /api/token/embed.
    This function never creates an endpoint or performs fetch, nor signs browser
    payloads. The supplied host capability owns durable register/reserve/capture.
    A standard web page alone has no documented authenticated host callback path.
    """
    return r'''function createFreyaCompletionObserver(Freya, options, host) {
  if (!Freya || typeof Freya.FreyaClient !== "function") throw new Error("SDK missing");
  for (const method of ["registerExpected", "reserveDelivery", "capture", "retire"]) {
    if (!host || typeof host[method] !== "function") throw new Error("Trusted host capability missing");
  }
  const client = new Freya.FreyaClient(options);
  let retired = false, connected = false, failure = null;
  let callbacks = Promise.resolve();
  const handlers = new Map();
  // Copy payload synchronously at actual callback time before queueing it.
  const capture = (kind, payload) => {
    if (retired || failure) return;
    let data;
    if (kind === "error") {
      if (!payload || typeof payload.message !== "string" || typeof payload.fatal !== "boolean") {
        failure = new Error("SDK error shape invalid");
        callbacks = callbacks.then(() => host.retire("invalid_error"));
        return;
      }
      kind = "reset"; data = {}; // conservative quarantine, no raw error body forwarded
    } else if (kind === "botTranscript") {
      if (!payload || Object.keys(payload).length !== 1 || typeof payload.text !== "string") {
        failure = new Error("SDK transcript shape invalid");
        callbacks = callbacks.then(() => host.retire("invalid_transcript"));
        return;
      }
      data = {text: payload.text};
    } else data = {};
    const capturedAt = Date.now(); // host must validate this observation, not trust timestamp alone
    callbacks = callbacks.then(() => {
      if (failure) return;
      return host.capture(kind, data, capturedAt);
    }).catch(error => { failure = error; return host.retire("callback_failure"); });
  };
  for (const kind of ["botStartedSpeaking", "botTranscript", "botStoppedSpeaking", "userStartedSpeaking", "disconnected", "error"]) {
    const handler = payload => capture(kind, payload);
    handlers.set(kind, handler);
    client.on(kind, handler); // all observers registered BEFORE connect()
  }
  return Object.freeze({
    async connect() {
      if (retired || connected || failure) throw new Error("Observer connection unavailable");
      connected = true; // attempt cannot be repeated: token establishment is single-use
      try { await client.connect(); } catch (error) { failure = error; await host.retire("connect_failed"); throw error; }
    },
    async expect(eventId, text) {
      if (retired || failure) throw new Error("Observer unavailable");
      await callbacks; // previous callbacks must settle before a new semantic expectation
      if (failure) throw failure;
      return host.registerExpected(eventId, text);
    },
    async reserveDelivery(eventId) {
      if (retired || failure) throw new Error("Observer unavailable");
      await callbacks;
      if (failure) throw failure;
      return host.reserveDelivery(eventId); // caller invokes TTS separately; observer never speaks
    },
    async settle() { await callbacks; if (failure) throw failure; },
    async dispose() {
      if (retired) return;
      retired = true;
      for (const [kind, handler] of handlers) client.off(kind, handler);
      await callbacks;
      await host.retire("observer_disposed");
      // caller controls disconnect(); disposal does not silently end a call
    },
    get productionReady() { return false; }
  });
}'''
