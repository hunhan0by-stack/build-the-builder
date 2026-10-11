"""Bounded structured dialogue over a lowered source-checked kernel policy.

No text/speech interpretation, practice source allowlist or network transport.
Assistant outputs are semantic dialogue events, not source-approved spoken wording.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict

from .api_contract import validate_value
from .compiled_policy import CompiledPolicy
from .models import FoundationError


def _schema(properties):
    return {"type": "object", "properties": {"kind": {"type": "string"}, **properties},
            "required": ["kind", *properties], "additionalProperties": False}


INPUTS = {
    "identity_answer": _schema({"field": {"type": "string"}, "answer": {"type": "string"}}),
    "select_product": _schema({"product_id": {"type": "string", "minLength": 1}}),
    "proposal": _schema({"amount": {"type": "number"}, "date": {"type": "string", "format": "date"}}),
    "confirm": _schema({"token": {"type": "string", "minLength": 1}, "explicit_yes": {"type": "boolean"}}),
    "close": _schema({}),
}


class CompiledConversation:
    """One product / one promise / structured inputs only. No silent partial coverage."""
    def __init__(self, compiled, variables, bank):
        if not isinstance(compiled, CompiledPolicy) or compiled.semantics.get("complete_for_supported_decisions") is not True:
            raise FoundationError("Dialogue requires a supported lowered CompiledPolicy")
        self.compiled = compiled
        self._policy_pin = self._fingerprint()
        self.session = compiled.create_session(variables, bank)
        self.variables = copy.deepcopy(variables)
        self.phase = "new"
        self.events = []
        self.selected_product = ""
        self.readback_token = ""
        self.recorded = None
        self.fault = ""

    def _fingerprint(self):
        raw = json.dumps({"policy": self.compiled.serializable(), "contract": self.compiled.contract.serializable()},
                         sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()

    def _assert_open(self):
        if self.phase in {"closed", "fault"}:
            raise FoundationError("Structured dialogue is terminal")
        try:
            unchanged = self._fingerprint() == self._policy_pin
        except (TypeError, ValueError, AttributeError):
            unchanged = False
        if not unchanged:
            self._fault("LOWERED_POLICY_CHANGED")
            raise FoundationError("Lowered policy changed after dialogue initialization")

    def _event(self, kind, **details):
        self.events.append({"seq": len(self.events) + 1, "kind": kind, "phase": self.phase,
                            "bank_call_count": len(self.session.kernel.bank.calls), **details})

    def _fault(self, code):
        self.session.state.approved_digest = ""
        self.readback_token = ""
        self.phase = "fault"
        self.fault = code
        self._event("dialogue_fault", code=code)

    def _require_phase(self, expected):
        if self.phase != expected:
            self._event("input_rejected", code="PHASE_MISMATCH", expected_phase=expected)
            raise FoundationError("Structured input is not allowed in this dialogue phase")

    def _ask_identity(self):
        fields = [field for field in self.session.state.required_identity_fields if field not in self.session.state.verified]
        if not fields:
            raise FoundationError("No identity field remains to be requested")
        self._event("ask_identity", field=fields[0])

    def start(self):
        self._assert_open()
        self._require_phase("new")
        self.phase = "identity"
        self._event("dialogue_started", mode="structured_local_kernel", model_calls=0, network_calls=0)
        self._ask_identity()
        return self.snapshot()

    def receive(self, turn):
        self._assert_open()
        if not isinstance(turn, dict) or not isinstance(turn.get("kind"), str) or turn["kind"] not in INPUTS:
            self._fault("UNSUPPORTED_INPUT")
            raise FoundationError("Only supported structured inputs are accepted; free text/callback/transfer are unsupported")
        try:
            validate_value(turn, INPUTS[turn["kind"]])
        except FoundationError:
            self._fault("MALFORMED_INPUT")
            raise
        kind = turn["kind"]
        # Identity answers stay in tool evidence; never put customer secrets into an assistant message.
        self._event("customer_input", input_kind=kind)
        if kind == "identity_answer":
            self._require_phase("identity")
            expected = [field for field in self.session.state.required_identity_fields if field not in self.session.state.verified][0]
            if turn["field"] != expected:
                self._event("input_rejected", code="IDENTITY_ORDER", expected_field=expected)
                raise FoundationError("Answer must correspond to the requested source-derived identity field")
            try:
                status = self.session.verify_answer(turn["field"], turn["answer"])
            except FoundationError:
                self._fault("IDENTITY_ACTION_REJECTED")
                raise
            if status != "succeeded":
                self._fault("IDENTITY_RESULT_UNCONFIRMED")
                raise FoundationError("Identity business result is unconfirmed")
            if self.session.state.identity_wrong >= self.compiled.policy.facts["identity_max_wrong"].value:
                self._fault("IDENTITY_BUDGET_EXHAUSTED")
                raise FoundationError("Source identity attempt budget exhausted")
            if not self.session.state.identity_verified:
                self._ask_identity()
                return self.snapshot()
            try:
                status = self.session.fetch_products()
            except FoundationError:
                self._fault("ACCOUNT_ACTION_REJECTED")
                raise
            if status != "succeeded":
                self._fault("ACCOUNT_RESULT_UNCONFIRMED")
                raise FoundationError("Account business result is unconfirmed")
            if len(self.session.state.products) != 1:
                self._fault("MULTI_PRODUCT_DIALOGUE_UNSUPPORTED")
                raise FoundationError("This bounded dialogue does not silently resolve additional products")
            self.phase = "product_selection"
            self._event("product_choices", products=[copy.deepcopy(product.details) for product in self.session.state.products])
        elif kind == "select_product":
            self._require_phase("product_selection")
            product = self.session.kernel.product(turn["product_id"])
            self.selected_product = product.product_id
            self.phase = "proposal"
            self._event("request_proposal", product_id=product.product_id, floor=str(product.floor), past_due=str(product.past_due),
                        product_deadline=product.deadline, business_weekdays=self.compiled.policy.facts["business_weekdays"].value,
                        holidays=self.compiled.policy.facts["holidays"].value,
                        source_constraints=[copy.deepcopy(item) for item in self.compiled.constraints if item["target"].startswith("payment.")])
        elif kind == "proposal":
            if self.phase not in {"proposal", "confirmation", "ready_to_write"}:
                self._require_phase("proposal")
            # Any correction revokes the prior confirmation before checking new terms.
            self.readback_token = ""
            self.phase = "proposal"
            try:
                token = self.session.set_draft([{"product_id": self.selected_product, "amount": turn["amount"], "date": turn["date"]}])
            except FoundationError:
                self._event("proposal_rejected", code="SOURCE_BOUND_REJECTED", new_confirmation_required=True)
                raise
            self.readback_token = token
            self.phase = "confirmation"
            self._event("readback", proposal=self.session.state.draft[0].payload(self.session.state.customer_id),
                        token=token, explicit_confirmation_required=True)
        elif kind == "confirm":
            self._require_phase("confirmation")
            try:
                self.session.confirm(turn["token"], turn["explicit_yes"])
            except FoundationError:
                self.phase = "proposal"
                self.readback_token = ""
                self._event("confirmation_rejected", code="CURRENT_EXPLICIT_YES_REQUIRED")
                raise
            self.phase = "ready_to_write"
            self._event("confirmation_accepted", token=self.readback_token, write_not_yet_executed=True)
        elif kind == "close":
            self._require_phase("recorded")
            self._event("goodbye", wording="unrendered_semantic_event")
            try:
                outcome, reason = self.session.kernel.outcome_for_products()
                status = self.session.finalize(outcome, reason, goodbye_spoken=True)
            except FoundationError:
                self._fault("CLOSE_ACTION_REJECTED")
                raise
            if status != "succeeded":
                self._fault("CLOSE_RESULT_UNCONFIRMED")
                raise FoundationError("Close business result is unconfirmed")
            self.phase = "closed"
            self._event("closed", outcome=outcome, reason=reason or None)
        return self.snapshot()

    def advance(self):
        self._assert_open()
        self._require_phase("ready_to_write")
        try:
            status = self.session.record_promise(self.selected_product)
        except FoundationError:
            self._fault("PROMISE_ACTION_REJECTED")
            raise
        if status != "succeeded":
            self._fault("PROMISE_RESULT_UNCONFIRMED")
            raise FoundationError("No recorded-promise claim without confirmed business success")
        self.recorded = self.session.kernel.product(self.selected_product).recorded.payload(self.session.state.customer_id)
        self.phase = "recorded"
        self._event("promise_recorded", promise=copy.deepcopy(self.recorded), wording="unrendered_semantic_event")
        return self.snapshot()

    def snapshot(self):
        return {"phase": self.phase, "identity_verified": self.session.state.identity_verified,
                "selected_product": self.selected_product, "confirmation_token": self.readback_token,
                "recorded_promise": copy.deepcopy(self.recorded), "fault": self.fault,
                "latest_event": copy.deepcopy(self.events[-1]) if self.events else None,
                "kind": "bounded_structured_compiled_dialogue", "publish_allowed": False}

    def evidence(self):
        return {"mode": "structured_local_kernel", "events": copy.deepcopy(self.events),
                "bank_calls": [asdict(call) for call in self.session.kernel.bank.calls],
                "business_results": copy.deepcopy(self.session.kernel.events), "final_state": self.snapshot(),
                "policy_fingerprint": self._policy_pin, "policy_coverage": copy.deepcopy(self.compiled.coverage),
                "supported_dialogue_phases": ["identity", "product_selection", "proposal", "confirmation", "ready_to_write", "recorded", "closed"],
                "unsupported_dialogue_features": ["free_text", "voice", "callback", "transfer", "multi_product", "persuasion", "source_approved_spoken_wording", "sms_wording"],
                "model_calls": 0, "network_calls": 0, "semantic_truth_verified": False, "export_ready": False, "publish_allowed": False}
