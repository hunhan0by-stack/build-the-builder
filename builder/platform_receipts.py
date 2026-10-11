"""Local authenticated SDK callback correlator; no HTTP server or bank calls.

The Freya SDK's documented callbacks have no semantic event IDs and no signed
receipt protocol. The HMAC envelopes below are OUR relay protocol, not a Freya
feature. A trusted transport must authenticate its SDK session, register the one
expected utterance before delivery, then forward actual callbacks in sequence.
An authenticated relay signature proves the configured relay, not Freya origin.
No relay is connected by this module. Default/local-fixture receipts cannot be
used as production receipts. Agent-to-external-server calls are deliberately
absent: the competition permits only the bank tool API.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import re
import time
import uuid

from .models import FoundationError


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}")
KINDS = frozenset({"botStartedSpeaking", "botStoppedSpeaking", "botTranscript",
                   "userStartedSpeaking", "disconnected", "reset"})


@dataclass(frozen=True)
class SDKBinding:
    call_id: str
    session_id: str
    generation: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in asdict(self).values()):
            raise FoundationError("Explicit bounded call/session/generation identifiers required")


class VerifiedSDKEvent:
    """Opaque source-created evidence; a dictionary is never a trusted callback."""
    __slots__ = ("_source", "_seal", "_data", "_digest", "_received_at", "_verification_number", "_source_instance")

    def __init__(self, source, seal, data):
        if seal is not source._seal:
            raise FoundationError("SDK event cannot be constructed without source verification")
        self._source, self._seal, self._data = source, seal, copy.deepcopy(data)
        self._received_at = source._time()
        source._verification_counter += 1
        self._verification_number = source._verification_counter
        self._source_instance = source._instance_id
        self._digest = source._sign_receipt(self._sealed_data())

    def _sealed_data(self):
        return {"event": self._data, "received_at": self._received_at,
                "verification_number": self._verification_number, "source_instance": self._source_instance}


class AuthenticatedSDKSource:
    """Verify a private trusted relay, with explicit unresolved origin boundary.

    The caller owns key storage, HTTPS/session authentication and actual SDK
    event capture. Keys are never persisted or returned. Browser/user-provided
    dictionaries must NOT be signed merely because they arrived over HTTPS.
    """
    def __init__(self, binding, key, *, authority="local_fixture",
                 transport_binding_confirmed=False, clock=time.time, max_event_age=30):
        if not isinstance(binding, SDKBinding) or not isinstance(key, bytes) or len(key) < 32:
            raise FoundationError("Explicit SDK session binding and strong relay key required")
        if authority not in {"local_fixture", "configured_sdk_relay"} or type(transport_binding_confirmed) is not bool:
            raise FoundationError("Unknown SDK source authority")
        if authority == "local_fixture" and transport_binding_confirmed:
            raise FoundationError("Local fixture cannot claim an actual SDK transport")
        if type(max_event_age) not in (int, float) or not math.isfinite(max_event_age) or not 1 <= max_event_age <= 120:
            raise FoundationError("Callback freshness bound required")
        self.binding, self._key, self.authority = binding, key, authority
        self.transport_binding_confirmed, self.clock, self.max_event_age = transport_binding_confirmed, clock, max_event_age
        self._seal = object()
        self._instance_id = uuid.uuid4().hex
        self._verification_counter = 0
        self.configuration = {"binding": asdict(binding), "authority": authority,
                              "transport_binding_confirmed": transport_binding_confirmed,
                              "key_sha256": hashlib.sha256(key).hexdigest(), "max_event_age": max_event_age}

    def receive(self, envelope):
        self._assert_configuration()
        if not isinstance(envelope, dict) or set(envelope) != {"event", "signature"}:
            raise FoundationError("Authenticated SDK envelope required")
        event, signature = envelope["event"], envelope["signature"]
        if not isinstance(event, dict) or set(event) != {"protocol", "binding", "sequence", "kind", "payload", "issued_at", "nonce"}:
            raise FoundationError("SDK relay event shape invalid")
        try:
            raw = canonical(event)
        except (ValueError, TypeError):
            raise FoundationError("SDK relay event must be finite JSON") from None
        if len(raw) > 64000 or not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise FoundationError("SDK event size or authentication invalid")
        expected = hmac.new(self._key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise FoundationError("SDK relay authentication failed")
        if event["protocol"] != "freya-sdk-relay-1" or event["binding"] != asdict(self.binding):
            raise FoundationError("SDK callback call/session/generation mismatch")
        if type(event["sequence"]) is not int or event["sequence"] < 1 or event["kind"] not in KINDS:
            raise FoundationError("SDK callback sequence or kind invalid")
        self._check_freshness(event["issued_at"])
        if not isinstance(event["nonce"], str) or not IDENTIFIER.fullmatch(event["nonce"]):
            raise FoundationError("SDK callback nonce invalid")
        payload = event["payload"]
        if event["kind"] == "botTranscript":
            if not isinstance(payload, dict) or set(payload) != {"text"} or not isinstance(payload["text"], str) or not payload["text"]:
                raise FoundationError("Actual completed SDK transcript text required")
        elif payload != {}:
            raise FoundationError("Payloadless SDK event must remain payloadless")
        return VerifiedSDKEvent(self, self._seal, event)

    def _verified_data(self, event):
        self._assert_configuration()
        if not isinstance(event, VerifiedSDKEvent) or event._source is not self or event._seal is not self._seal:
            raise FoundationError("Unverified or foreign SDK callback")
        if not hmac.compare_digest(event._digest, self._sign_receipt(event._sealed_data())):
            raise FoundationError("Verified SDK callback was altered")
        # Creation-time verification cannot authorize indefinitely queued objects.
        self._check_freshness(event._data["issued_at"])
        self._check_freshness(event._received_at)
        data = copy.deepcopy(event._data)
        data.update(_relay_received_at=event._received_at, _relay_verification_number=event._verification_number,
                    _relay_source_instance=event._source_instance)
        return data

    def _time(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise FoundationError("SDK trusted clock must return a finite time")
        return value

    def _check_freshness(self, timestamp):
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or not -2 <= self._time() - timestamp <= self.max_event_age):
            raise FoundationError("SDK callback is stale or future-dated")

    def _assert_configuration(self):
        current = {"binding": asdict(self.binding), "authority": self.authority,
                   "transport_binding_confirmed": self.transport_binding_confirmed,
                   "key_sha256": hashlib.sha256(self._key).hexdigest(), "max_event_age": self.max_event_age}
        if current != self.configuration:
            raise FoundationError("SDK source configuration changed")

    def _sign_receipt(self, receipt):
        return hmac.new(self._key, canonical(receipt), hashlib.sha256).hexdigest()


class SDKReceiptAdapter:
    """One expected semantic utterance per authenticated SDK session.

    DurableJournal is host-owned storage, not assumed native platform storage.
    It is shared with PlatformBridge through external events (never overwriting
    its snapshots). A dispatched utterance is not resent after restart. Missing
    callbacks remain pending until expiration; they never fabricate completion.
    """
    def __init__(self, journal, source, *, callback_timeout=120):
        if not isinstance(source, AuthenticatedSDKSource) or type(callback_timeout) not in (int, float) or not math.isfinite(callback_timeout) or not 1 <= callback_timeout <= 420:
            raise FoundationError("Configured SDK source and bounded callback deadline required")
        self.journal, self.source, self.callback_timeout = journal, source, callback_timeout
        self.config = {**source.configuration, "callback_timeout": callback_timeout}
        states = [row["data"]["payload"] for row in journal.events()
                  if row["kind"] == "external_event" and row["data"]["event_type"] == "sdk_receipts_state"]
        if states:
            self.state = copy.deepcopy(states[-1])
            if self.state["configuration"] != self.config:
                raise FoundationError("SDK adapter source binding changed on restart")
        else:
            self.state = {"version": "sdk-receipts-1", "configuration": self.config, "revision": 0,
                          "last_sequence": 0, "seen_callbacks": {}, "used_nonces": {},
                          "pending": None, "receipts": {}, "blocked": False, "diagnostic": None}
            self._persist()
        pending = self.state["pending"]
        if pending:
            speech = self.journal.speech_state(pending["event_id"])
            if speech is None or speech["text_sha256"] != pending["text_sha256"]:
                self._block("SDK_DURABLE_SPEECH_BINDING_MISMATCH")
            if speech["dispatched"] and not pending["dispatched"]:
                pending["dispatched"] = True
                self._persist()  # interrupted dispatch snapshot, still no resend
        self.expire_pending()

    def _persist(self):
        self.state["revision"] += 1
        self._durable(self.journal.append_event, "sdk_receipts_state", copy.deepcopy(self.state),
                      event_id="sdk-receipts-state-" + str(self.state["revision"]))

    def _durable(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            self.state.update(blocked=True, diagnostic="SDK_DURABLE_RECORD_FAILED")
            raise FoundationError("SDK_DURABLE_RECORD_FAILED") from None

    def _block(self, code):
        self.state.update(blocked=True, diagnostic=code)
        self._persist()
        raise FoundationError(code)

    def expire_pending(self):
        if self.state["pending"] and self.source._time() > self.state["pending"]["deadline"] and not self.state["blocked"]:
            self._block("SDK_CALLBACK_MISSING_OR_TIMED_OUT")

    def register_pending(self, event_id, text):
        self.expire_pending()
        if self.state["blocked"]:
            raise FoundationError("SDK receipt adapter is quarantined")
        if not isinstance(event_id, str) or not IDENTIFIER.fullmatch(event_id) or not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise FoundationError("Bounded semantic event and exact rendered text required")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        complete = self.state["receipts"].get(event_id)
        if complete:
            if complete["text_sha256"] != text_hash:
                self._block("SDK_SEMANTIC_EVENT_TEXT_CONFLICT")
            return copy.deepcopy(complete)
        pending = self.state["pending"]
        if pending:
            if pending["event_id"] == event_id and pending["text_sha256"] == text_hash:
                return self.snapshot()
            self._block("SDK_OVERLAPPING_PENDING_UTTERANCE")
        if len(self.state["receipts"]) >= 128:
            self._block("SDK_RECEIPT_RESOURCE_BOUND")
        self._durable(self.journal.append_speech, event_id, text)
        existing = self._durable(self.journal.speech_state, event_id)
        if existing["acknowledged"] or existing["dispatched"]:
            self._block("SDK_SPEECH_ALREADY_CONSUMED_WITHOUT_ADAPTER_BINDING")
        registered_at = self.source._time()
        self.state["pending"] = {"event_id": event_id, "text_sha256": text_hash,
                                 "registered_at": registered_at, "dispatched_at": None,
                                 "dispatch_source_instance": None, "verification_floor": None,
                                 "deadline": registered_at + self.callback_timeout,
                                 "dispatched": False, "start_sequence": None,
                                 "transcript_sequence": None, "stop_sequence": None}
        self._persist()
        return self.snapshot()

    def mark_dispatched(self, event_id):
        self.expire_pending()
        pending = self.state["pending"]
        if self.state["blocked"] or not pending or pending["event_id"] != event_id or pending["dispatched"]:
            raise FoundationError("SDK utterance cannot be dispatched or resent")
        # Journal reservation precedes delivery. A crash here fails closed on
        # restore: journal says dispatched even if adapter snapshot did not.
        self._durable(self.journal.mark_speech_dispatched, event_id)
        pending["dispatched"] = True
        pending["dispatched_at"] = self.source._time()
        if pending["dispatched_at"] < pending["registered_at"]:
            self._block("SDK_CLOCK_REVERSED_BEFORE_DISPATCH")
        if pending["dispatched_at"] > pending["deadline"]:
            self._block("SDK_CALLBACK_MISSING_OR_TIMED_OUT")
        pending["dispatch_source_instance"] = self.source._instance_id
        pending["verification_floor"] = self.source._verification_counter
        self._persist()

    def receive_envelope(self, envelope):
        try:
            event = self.source.receive(envelope)
        except FoundationError:
            self._block("SDK_CALLBACK_AUTHENTICATION_OR_BINDING_FAILED")
        return self.on_sdk_event(event)

    def on_sdk_event(self, verified_event):
        self.expire_pending()
        if self.state["blocked"]:
            raise FoundationError("SDK receipt adapter is quarantined")
        try:
            event = self.source._verified_data(verified_event)
        except FoundationError:
            self._block("SDK_CALLBACK_NOT_FROM_CONFIGURED_SOURCE")
        seq = event["sequence"]
        digest = sha({key: value for key, value in event.items() if not key.startswith("_relay_")})
        seen = self.state["seen_callbacks"].get(str(seq))
        if seen:
            if seen != digest:
                self._block("SDK_CALLBACK_REPLAY_CONFLICT")
            return self.snapshot()  # exact replay, no new receipt or ack
        if seq != self.state["last_sequence"] + 1:
            self._block("SDK_CALLBACK_SEQUENCE_GAP_OR_REORDER")
        if event["nonce"] in self.state["used_nonces"]:
            self._block("SDK_CALLBACK_NONCE_REPLAY")
        if seq > 512:
            self._block("SDK_CALLBACK_RESOURCE_BOUND")
        self.state["seen_callbacks"][str(seq)] = digest
        self.state["used_nonces"][event["nonce"]] = seq
        self.state["last_sequence"] = seq
        kind, pending = event["kind"], self.state["pending"]
        if kind in {"disconnected", "reset"} or (kind == "userStartedSpeaking" and pending):
            self._block("SDK_INTERRUPTED_OR_RESET")
        if kind == "userStartedSpeaking":
            self._persist()
            return self.snapshot()
        if not pending or not pending["dispatched"]:
            self._block("SDK_CALLBACK_WITHOUT_DISPATCHED_SEMANTIC_UTTERANCE")
        # Both authenticated event-capture time and locally observed receipt time
        # must belong to this utterance's dispatch window. The source counter
        # additionally rejects preverified objects even at equal clock resolution.
        lower = pending.get("dispatched_at")
        registered = pending.get("registered_at")
        if (type(lower) not in (int, float) or type(registered) not in (int, float)
                or not math.isfinite(lower) or not math.isfinite(registered) or registered > lower
                or not lower <= event["issued_at"] <= pending["deadline"]
                or not lower <= event["_relay_received_at"] <= pending["deadline"]
                or (event["_relay_source_instance"] == pending.get("dispatch_source_instance")
                    and event["_relay_verification_number"] <= pending.get("verification_floor", 0))):
            self._block("SDK_CALLBACK_OUTSIDE_CURRENT_DISPATCH_WINDOW")
        speech = self._durable(self.journal.speech_state, pending["event_id"])
        if not speech or not speech["dispatched"] or speech["text_sha256"] != pending["text_sha256"]:
            self._block("SDK_DURABLE_SPEECH_BINDING_MISMATCH")
        if kind == "botStartedSpeaking":
            if pending["start_sequence"] is not None:
                self._block("SDK_OVERLAPPING_SPEAKING_TURN")
            pending["start_sequence"] = seq
        elif pending["start_sequence"] is None:
            self._block("SDK_CALLBACK_MISSING_TURN_START")
        elif kind == "botTranscript":
            actual_hash = hashlib.sha256(event["payload"]["text"].encode()).hexdigest()
            if actual_hash != pending["text_sha256"] or pending["transcript_sequence"] is not None:
                self._block("SDK_TRANSCRIPT_TEXT_OR_TURN_MISMATCH")
            pending["transcript_sequence"] = seq
        elif kind == "botStoppedSpeaking":
            if pending["stop_sequence"] is not None:
                self._block("SDK_DUPLICATE_STOP_WITHOUT_IDENTITY")
            pending["stop_sequence"] = seq
        if pending["start_sequence"] and pending["transcript_sequence"] and pending["stop_sequence"]:
            receipt = {"version": "sdk-completion-receipt-1", "event_id": pending["event_id"], "status": "completed",
                       "text_sha256": pending["text_sha256"], **asdict(self.source.binding),
                       "source_authority": self.source.authority,
                       "source_binding_confirmed": self.source.transport_binding_confirmed,
                       "registered_at": pending["registered_at"], "dispatched_at": pending["dispatched_at"],
                       "start_sequence": pending["start_sequence"], "transcript_sequence": pending["transcript_sequence"],
                       "stop_sequence": pending["stop_sequence"]}
            receipt["receipt_id"] = sha(receipt)
            receipt["auth_tag"] = self.source._sign_receipt(receipt)
            self.state["receipts"][pending["event_id"]] = receipt
            self.state["pending"] = None
            self._persist()  # durable receipt before bridge can consume it
        else:
            self._persist()
        return self.snapshot()

    def receipt(self, event_id):
        return copy.deepcopy(self.state["receipts"].get(event_id))

    def verify_receipt(self, receipt):
        try:
            self.source._assert_configuration()
        except FoundationError:
            return False
        if self.state["blocked"] or not isinstance(receipt, dict):
            return False
        stored = self.state["receipts"].get(receipt.get("event_id"))
        if stored != receipt or not stored:
            return False
        signed = {k: v for k, v in receipt.items() if k != "auth_tag"}
        return hmac.compare_digest(receipt["auth_tag"], self.source._sign_receipt(signed))

    def verify_platform_receipt(self, receipt):
        return (self.source.authority == "configured_sdk_relay" and self.source.transport_binding_confirmed
                and self.verify_receipt(receipt))

    def snapshot(self):
        pending = copy.deepcopy(self.state["pending"])
        if pending:
            row = self.journal.speech_state(pending["event_id"])
            pending["may_dispatch"] = bool(row and row["may_render"] and not pending["dispatched"] and not self.state["blocked"])
        return {"binding": asdict(self.source.binding), "pending": pending,
                "completed_event_ids": list(self.state["receipts"]), "blocked": self.state["blocked"],
                "diagnostic": self.state["diagnostic"], "source_authority": self.source.authority,
                "authenticated_relay_configured": self.source.transport_binding_confirmed,
                "freya_origin_independently_proven": False, "live_calls_by_adapter": 0,
                "publication_ready": False, "storage_location": "host_DurableJournal_not_platform_filesystem"}
