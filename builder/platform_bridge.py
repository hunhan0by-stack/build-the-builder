"""Bounded, asynchronous policy bridge; it never sends a bank request.

The caller must durably mark a request dispatched BEFORE sending it. Speech
receipts are accepted only through an explicit adapter verifier: this module
cannot establish their provider origin. No local acknowledgement is synthesized.
Only ordered identity, account selection, one promise and closing are supported.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import TYPE_CHECKING

from .api_contract import validate_value
from .calendar import BusinessCalendar, iso_date
from .durable_replay import DurableReplayError
from .models import FoundationError

if TYPE_CHECKING:
    from .compiled_policy import CompiledPolicy


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def amount(value):
    if type(value) not in (int, float):
        raise FoundationError("Amount must be a finite JSON number")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal("0.01")):
            raise InvalidOperation()
    except (InvalidOperation, ValueError):
        raise FoundationError("Invalid payment amount") from None
    return result


class PlatformBridge:
    """Supported structured inputs only; free text is interpreted by the caller.

    DurableJournal stores write-ahead requests and snapshots. A changed source,
    call variable or receipt-authority binding cannot revive an existing scope.
    """
    def __init__(self, compiled: CompiledPolicy, variables, journal, *,
                 receipt_verifier=None, receipt_authority="untrusted"):
        if receipt_authority not in {"untrusted", "local_fixture", "platform_adapter"}:
            raise FoundationError("Unknown speech receipt authority")
        if receipt_verifier is not None and not callable(receipt_verifier):
            raise FoundationError("Speech verifier must be an explicit callable")
        if not isinstance(variables, dict) or not isinstance(variables.get("customer_id"), str) or not variables["customer_id"]:
            raise FoundationError("Explicit call customer_id required")
        try:
            datetime.fromisoformat(variables["now"])
        except (KeyError, TypeError, ValueError):
            raise FoundationError("Explicit valid call now required") from None
        if compiled.semantics.get("complete_for_supported_decisions") is not True:
            raise FoundationError("Supported compiled decisions required")
        self.compiled, self.contract, self.journal = compiled, compiled.contract, journal
        self.variables = copy.deepcopy(variables)
        self.calendar = BusinessCalendar.from_policy(compiled.policy)
        self.facts = {key: copy.deepcopy(fact.value) for key, fact in compiled.policy.facts.items()}
        self.order = self.facts["identity_order"]
        if not self.order or not set(self.order) <= {"phone_last4", "date_of_birth"}:
            raise FoundationError("Unsupported identity adapter")
        self.receipt_verifier, self.receipt_authority = receipt_verifier, receipt_authority
        self.binding = self._current_binding()
        prior = journal.latest_snapshot()
        if prior is None:
            self.state = {"schema_version": "platform-bridge-1", "binding_sha256": self.binding,
                          "phase": "new", "verified": [], "identity_wrong": 0,
                          "products": [], "selected": None, "draft": None, "readback_token": "",
                          "consent": "", "promise_id": None, "bank_counter": 0, "event_counter": 0,
                          "events": [], "pending_request": None, "resolved": {}, "speech_bindings": {}, "speech_receipts": {},
                          "close": None, "goodbye_event_id": None, "diagnostic": None}
            self._save()
        else:
            if prior.get("schema_version") != "platform-bridge-1" or prior.get("binding_sha256") != self.binding:
                raise FoundationError("Journal source/call/receipt binding mismatch")
            self.state = copy.deepcopy(prior)
            self._recover()

    def _save(self):
        self._assert_binding()
        self.journal.save_snapshot(copy.deepcopy(self.state))

    def _current_binding(self):
        shape = {name: {"request": operation.request, "response": operation.response,
                        "method": operation.method, "url": operation.url}
                 for name, operation in self.contract.operations.items()}
        return fingerprint({"compiled": self.compiled.serializable(), "contract": self.contract.source_hash,
                            "contract_shape": shape, "variables": self.variables,
                            "receipt_authority": self.receipt_authority})

    def _assert_binding(self):
        if self._current_binding() != self.binding:
            raise FoundationError("Source policy, contract or call binding changed during execution")

    def _recover(self):
        pending = self.state["pending_request"]
        if pending:
            # The intent snapshot precedes prepare_request. Either order can be
            # interrupted without losing the stable ID or allocating a new one.
            self.journal.prepare_request(pending["operation"], pending["payload"], request_id=pending["request_id"])
            item = self.journal.lookup_request(pending["request_id"])
            if item.get("status") in {"confirmed", "failed"} and "result" in item:
                self._apply_result(pending, item["result"], item["status"])
                self._save()
        for event_id, text in self.state["speech_bindings"].items():
            self.journal.append_speech(event_id, text)
        # Restore only a completed, durably acknowledged goodbye whose adapter
        # validation was already snapshotted. Never synthesize an acknowledgement.
        goodbye_id = self.state["goodbye_event_id"]
        if self.state["phase"] == "awaiting_goodbye" and goodbye_id:
            speech = self.journal.speech_state(goodbye_id)
            proof = self.state["speech_receipts"].get(goodbye_id)
            if (speech and speech["acknowledged"] is True and proof
                    and proof["text_sha256"] == speech["text_sha256"]
                    and proof["authority"] == self.receipt_authority and proof["adapter_verified"] is True):
                self.state["phase"] = "closing"
                self._queue("close_call", self.state["close"])
                self._save()

    def _emit(self, kind, context, *, bank_outcome=None):
        if self.state["event_counter"] >= 256:
            raise FoundationError("Bridge event resource bound exhausted")
        self.state["event_counter"] += 1
        event = {"event_id": "bridge-speech-" + str(self.state["event_counter"]),
                 "kind": kind, "context": copy.deepcopy(context)}
        if bank_outcome is not None:
            event["bank_outcome"] = copy.deepcopy(bank_outcome)
        self.state["events"].append(event)
        return event["event_id"]

    def _queue(self, operation, payload):
        self._assert_binding()
        if self.state["pending_request"] or self.state["phase"] in {"closed", "blocked"}:
            raise FoundationError("Pending, closed or quarantined bridge cannot queue another request")
        validate_value(payload, self.contract.operations[operation].request, strict_fields=True)
        if self.state["bank_counter"] >= 128:
            raise FoundationError("Bridge request resource bound exhausted")
        speech = self.journal.speech_state(self.state["goodbye_event_id"]) if operation == "close_call" else None
        goodbye = operation == "close_call" and self.state["phase"] == "closing" and speech is not None and speech["acknowledged"] is True
        self._guard(operation, draft=self.state["draft"] if operation == "create_promise" else None, goodbye=goodbye)
        self.state["bank_counter"] += 1
        request = {"request_id": "bridge-bank-" + str(self.state["bank_counter"]),
                   "operation": operation, "payload": copy.deepcopy(payload)}
        self.state["pending_request"] = request
        self._save()  # write-ahead intent, before the durable request reservation
        self.journal.prepare_request(operation, payload, request_id=request["request_id"])
        self._save()

    def _guard(self, operation, *, draft=None, preparing=False, goodbye=False):
        verified = self.state["verified"] == self.order
        checks = {"identity_verified": verified,
                  "consent_current": bool(self.state["consent"]) and self.state["consent"] == self.state["readback_token"],
                  "goodbye_spoken": goodbye, "close_once_last": not any(item["operation"] == "close_call" for item in self.state["resolved"].values()),
                  "no_write_after_transfer": True, "amount_within_bounds": draft is not None,
                  "date_is_business_day": draft is not None}
        for rule in self.compiled.constraints:
            if operation not in rule["operations"]:
                continue
            target, value = rule["target"], rule["value"]
            if target == "consent_current" and preparing:
                continue
            if target == "payment.amount":
                if draft is not None and amount(draft["amount"]) > amount(value):
                    raise FoundationError("Compiled payment ceiling exceeded")
            elif target in {"payment.date_on_or_before", "payment.date_on_or_after"}:
                if draft is not None:
                    day, boundary = iso_date(draft["date"]), iso_date(value)
                    if (target.endswith("before") and day > boundary) or (target.endswith("after") and day < boundary):
                        raise FoundationError("Compiled payment date boundary exceeded")
            elif target not in checks or not checks[target]:
                raise FoundationError("Compiled source guard blocks operation: " + target)

    def start(self):
        self._assert_binding()
        if self.state["phase"] != "new":
            raise FoundationError("Bridge was already started")
        self.state["phase"] = "identity"
        self._emit("opening", {key: self.variables.get(key, "") for key in ("first_name", "last_name")})
        self._emit("identity_question", {"field": self.order[0]})
        self._save()
        return self.snapshot()

    def receive(self, event):
        self._assert_binding()
        if not isinstance(event, dict) or self.state["pending_request"] or self.state["phase"] in {"new", "blocked", "closed", "closing", "awaiting_goodbye"}:
            raise FoundationError("Input cannot advance this bridge state")
        kind, phase = event.get("kind"), self.state["phase"]
        if kind == "identity_answer" and phase == "identity":
            remaining = self.order[len(self.state["verified"]):]
            field, answer = event.get("field"), event.get("answer")
            if not remaining or field != remaining[0] or self.state["identity_wrong"] >= self.facts["identity_max_wrong"]:
                raise FoundationError("Identity factor order or attempt limit violated")
            if field == "date_of_birth":
                iso_date(answer)
            elif not isinstance(answer, str) or not re.fullmatch(r"[0-9]{4}", answer):
                raise FoundationError("Phone suffix must be four digits")
            self._queue("verify_identity", {"customer_id": self.variables["customer_id"], "field": field, "answer": answer})
        elif kind == "proposal" and phase in {"proposal", "readback", "confirmed"}:
            # An invalid amendment still revokes stale consent, durably.
            self.state.update(consent="", readback_token="", draft=None, phase="proposal")
            self._save()
            draft = {"product_id": event.get("product_id"), "amount": event.get("amount"), "date": event.get("date")}
            self._validate_draft(draft)
            self.state["draft"] = draft
            token = fingerprint({"binding": self.binding, "draft": draft, "revision": self.state["event_counter"] + 1})
            self.state.update(readback_token=token, phase="readback")
            self._emit("readback", {**self._product_context(), **draft, "readback_token": token})
            self._save()
        elif kind == "confirm" and phase == "readback":
            self.state["consent"] = ""
            self._save()
            if event.get("explicit_yes") is not True or event.get("readback_token") != self.state["readback_token"]:
                raise FoundationError("Explicit approval of the current readback required")
            self.state.update(consent=self.state["readback_token"], phase="confirmed")
            self._save()  # approval alone NEVER dispatches or queues a write
        else:
            raise FoundationError("Unsupported structured input for this phase")
        return self.snapshot()

    def _select(self, product):
        self.state.update(selected=product["product_id"], phase="proposal")
        self._emit("account_summary", copy.deepcopy(product))
        self._emit("proposal_request", {**product, "floor": product["floor"]})

    def _product_context(self):
        matches = [p for p in self.state["products"] if p["product_id"] == self.state["selected"]]
        if len(matches) != 1:
            raise FoundationError("No unique selected account product")
        return copy.deepcopy(matches[0])

    def _validate_draft(self, draft):
        product = self._product_context()
        if draft["product_id"] != product["product_id"] or self.state["promise_id"]:
            raise FoundationError("Proposal product is unselected or already recorded")
        value = amount(draft["amount"])
        if not amount(product["floor"]) <= value <= amount(product["past_due"]):
            raise FoundationError("Payment outside source-derived floor/full interval")
        self.calendar.validate_promise_date(draft["date"], self.variables["now"], iso_date(product["deadline"]))
        payload = {"customer_id": self.variables["customer_id"], **draft}
        validate_value(payload, self.contract.operations["create_promise"].request, strict_fields=True)
        self._guard("create_promise", draft=draft, preparing=True)

    def advance(self):
        self._assert_binding()
        if self.state["phase"] != "confirmed" or self.state["pending_request"]:
            raise FoundationError("Advance requires separate current confirmed readback")
        self._validate_draft(self.state["draft"])
        self._guard("create_promise", draft=self.state["draft"])
        self.state["phase"] = "recording"
        self._queue("create_promise", {"customer_id": self.variables["customer_id"], **self.state["draft"]})
        return self.snapshot()

    def mark_dispatched(self, request_id):
        self._assert_binding()
        if not self.state["pending_request"] or self.state["pending_request"]["request_id"] != request_id:
            raise FoundationError("Dispatch does not match pending bank request")
        try:
            self.journal.mark_dispatched(request_id)
        except DurableReplayError as exc:
            raise FoundationError(str(exc)) from None
        self._save()
        return self.snapshot()

    def resume_bank(self, request_id, result):
        self._assert_binding()
        seen = self.state["resolved"].get(request_id)
        if seen:
            if seen["result_sha256"] != fingerprint(result):
                raise FoundationError("Conflicting replay of bank result")
            return self.snapshot()
        pending = self.state["pending_request"]
        if not pending or pending["request_id"] != request_id:
            raise FoundationError("Bank result does not match pending request")
        item = self.journal.lookup_request(request_id)
        if item.get("may_dispatch") is True:
            raise FoundationError("Bank result cannot precede dispatch reservation")
        status = "unknown"
        try:
            validate_value(result, self.contract.operations[pending["operation"]].response)
            if result.get("ok") is True:
                self._validate_business(pending, result)
                status = "confirmed"
            elif result.get("ok") is False:
                status = "failed"
        except (FoundationError, AttributeError, TypeError, KeyError):
            status = "unknown"
        durable_result = result if isinstance(result, dict) else {"bridge_unvalidated_result": result}
        if item.get("state") == "unknown":
            self.journal.reconcile_result(request_id, durable_result, status=status)
        else:
            self.journal.record_result(request_id, durable_result, status=status)
        self._apply_result(pending, result, status)
        self._save()
        return self.snapshot()

    def _validate_business(self, pending, result):
        operation = pending["operation"]
        if operation == "verify_identity":
            if type(result.get("verified")) is not bool:
                raise FoundationError("Identity result missing verification fact")
        elif operation == "get_account":
            self._parse_products(result)
        elif operation == "create_promise":
            if not isinstance(result.get("promise_id"), str) or not result["promise_id"].strip():
                raise FoundationError("Promise success missing durable promise identifier")

    def _parse_products(self, result):
        rows = result.get("products")
        if not isinstance(rows, list) or len(rows) != 1 or len(rows) > self.facts["product_limit"]:
            raise FoundationError("Single-product bridge rejects account count outside its supported/source bounds")
        products, ids = [], set()
        for row in rows:
            identifier = row.get("product_id")
            floor_key = self.facts["floor_fields"][row["type"]]
            floor, full = amount(row[floor_key]), amount(row["past_due"])
            if not isinstance(identifier, str) or not identifier or identifier in ids or floor > full:
                raise FoundationError("Account snapshot has inconsistent products")
            deadline = self.calendar.deadline(self.variables["now"], row["days_late"], self.facts["bands"])
            products.append({**copy.deepcopy(row), "floor": row[floor_key], "deadline": deadline.isoformat()})
            ids.add(identifier)
        return products

    def _apply_result(self, pending, result, status):
        operation, request_id = pending["operation"], pending["request_id"]
        self.state["resolved"][request_id] = {"operation": operation, "status": status, "result_sha256": fingerprint(result)}
        self.state["pending_request"] = None
        if status != "confirmed":
            self.state.update(phase="blocked", consent="", diagnostic="bank_" + status)
            return
        if operation == "verify_identity":
            if result["verified"]:
                self.state["verified"].append(pending["payload"]["field"])
            elif not result.get("hint"):
                self.state["identity_wrong"] += 1
            if self.state["identity_wrong"] >= self.facts["identity_max_wrong"]:
                self.state.update(phase="blocked", diagnostic="identity_attempts_exhausted")
            elif self.state["verified"] == self.order:
                self.state["phase"] = "account"
                self._queue("get_account", {"customer_id": self.variables["customer_id"]})
            else:
                self.state["phase"] = "identity"
                self._emit("identity_question", {"field": self.order[len(self.state["verified"])]})
        elif operation == "get_account":
            self.state["products"] = self._parse_products(result)
            self._select(self.state["products"][0])
        elif operation == "create_promise":
            self.state.update(promise_id=result["promise_id"], phase="post_record", consent="")
            self._emit("promise_success", {**self._product_context(), **self.state["draft"], "promise_id": result["promise_id"]},
                       bank_outcome={"operation": operation, "result": result})
        elif operation == "close_call":
            self.state["phase"] = "closed"

    def request_close(self, outcome, reason=""):
        self._assert_binding()
        if self.state["pending_request"] or self.state["phase"] in {"new", "blocked", "closing", "awaiting_goodbye", "closed"}:
            raise FoundationError("Uncertain, pending or closed state cannot close")
        table = {row["outcome"]: row["reasons"] for row in self.facts["outcome_table"]}
        if outcome not in table or (table[outcome] and reason not in table[outcome]) or (not table[outcome] and reason):
            raise FoundationError("Closing outcome/reason lacks source support")
        if outcome == "promise":
            if not self.state["promise_id"] or self.state["phase"] != "post_record":
                raise FoundationError("Closing promise requires confirmed bank record")
        elif outcome in {"paid", "transferred"} or self.state["products"]:
            raise FoundationError("Closing branch outside the supported bridge subset")
        payload = {"customer_id": self.variables["customer_id"], "outcome": outcome}
        if reason:
            payload["reason"] = reason
        validate_value(payload, self.contract.operations["close_call"].request, strict_fields=True)
        self.state.update(close=payload, phase="awaiting_goodbye")
        context = {"outcome": outcome}
        if reason:
            context["reason"] = reason
        self.state["goodbye_event_id"] = self._emit("goodbye", context)
        self._save()
        return self.snapshot()

    def bind_speech(self, event_id, text):
        self._assert_binding()
        if not isinstance(text, str) or not text.strip() or not any(e["event_id"] == event_id for e in self.state["events"]):
            raise FoundationError("Speech text must bind an emitted event")
        previous = self.state["speech_bindings"].get(event_id)
        if previous is not None and previous != text:
            raise FoundationError("Conflicting rendered speech")
        self.state["speech_bindings"][event_id] = text
        self._save()
        self.journal.append_speech(event_id, text)
        return self.snapshot()

    def acknowledge_speech(self, event_id, receipt):
        self._assert_binding()
        text = self.state["speech_bindings"].get(event_id)
        if (text is None or not isinstance(receipt, dict) or receipt.get("event_id") != event_id
                or receipt.get("status") != "completed" or receipt.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()
                or self.receipt_authority == "untrusted" or self.receipt_verifier is None
                or self.receipt_verifier(copy.deepcopy(receipt)) is not True):
            raise FoundationError("Exact completed speech receipt from an explicit trusted adapter required")
        self.state["speech_receipts"][event_id] = {"text_sha256": receipt["text_sha256"],
                                                 "receipt_sha256": fingerprint(receipt),
                                                 "authority": self.receipt_authority, "adapter_verified": True}
        self._save()  # receipt admission, before durable acknowledgement
        self.journal.acknowledge_speech(event_id, receipt["text_sha256"])
        if event_id == self.state["goodbye_event_id"]:
            if self.state["phase"] != "awaiting_goodbye":
                raise FoundationError("Goodbye acknowledgement cannot repeat close")
            self._guard("close_call", goodbye=True)
            self.state["phase"] = "closing"
            # _queue rechecks close guards; durable speech_state is authoritative.
            self._queue("close_call", self.state["close"])
        self._save()
        return self.snapshot()

    def snapshot(self):
        self._assert_binding()
        value = copy.deepcopy(self.state)
        pending = value.pop("pending_request")
        value["pending_bank_requests"] = []
        if pending:
            item = self.journal.lookup_request(pending["request_id"])
            value["pending_bank_requests"] = [{**pending, "state": item["state"], "may_dispatch": item["may_dispatch"]}]
        value["speech_receipt_authority"] = self.receipt_authority
        value["speech_origin_verified_by_bridge"] = False
        value["publish_allowed"] = False
        return value

    def evidence(self):
        return {"kind": "asynchronous-supported-platform-bridge", "snapshot": self.snapshot(),
                "journal_events": self.journal.events(), "live_bank_calls_by_bridge": 0,
                "full_brief_semantic_coverage": False, "receipt_origin_trust_boundary": "caller-supplied adapter verifier"}
