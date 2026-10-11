"""Deterministic guard core for local tests. No network transport is implemented."""
from __future__ import annotations

import hashlib
import json
import copy
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .api_contract import BankContract
from .calendar import BusinessCalendar, iso_date
from .models import FoundationError, Policy


def money(value) -> Decimal:
    if isinstance(value, bool):
        raise FoundationError("Boolean is not a payment amount")
    try:
        amount = Decimal(str(value))
        valid = amount.is_finite() and amount >= 0 and amount == amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise FoundationError("Invalid payment amount") from exc
    if not valid:
        raise FoundationError("Amount must be finite, nonnegative and have at most two decimal places")
    return amount


@dataclass(frozen=True)
class Promise:
    product_id: str
    amount: Decimal
    date: str

    def payload(self, customer_id: str):
        if Decimal(str(float(self.amount))) != self.amount:
            raise FoundationError("Amount cannot be represented exactly by this JSON-number adapter")
        return {"customer_id": customer_id, "product_id": self.product_id,
                "amount": float(self.amount), "date": self.date}


@dataclass
class Product:
    product_id: str
    floor: Decimal
    past_due: Decimal
    deadline: str
    attempts: int = 0
    recorded: Promise | None = None
    note_reason: str = ""
    uncertain_write: bool = False
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    operation: str
    payload: dict


@dataclass
class CallState:
    customer_id: str
    now: str
    required_identity_fields: tuple[str, ...]
    verified: set[str] = field(default_factory=set)
    identity_wrong: int = 0
    products: list[Product] = field(default_factory=list)
    draft: tuple[Promise, ...] = ()
    approved_digest: str = ""
    transfer_reason: str = ""
    transfer_attempted: bool = False
    transfer_uncertain: bool = False
    finalizing: bool = False
    close_attempted: bool = False

    @property
    def identity_verified(self):
        return bool(self.required_identity_fields) and set(self.required_identity_fields) <= self.verified


class MockBank:
    """Scripted test double. Responses are supplied by tests, not guessed from cases."""
    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = copy.deepcopy(responses)
        self.calls: list[Action] = []

    def call(self, action: Action):
        self.calls.append(copy.deepcopy(action))
        queue = self.responses.get(action.operation, [])
        if not queue:
            raise FoundationError("Mock has no scripted response; no real API fallback exists")
        return copy.deepcopy(queue.pop(0))


class GuardedSession:
    def __init__(self, contract: BankContract, policy: Policy, state: CallState, bank: MockBank):
        if type(bank) is not MockBank:
            raise FoundationError("Only the local MockBank transport is supported")
        required = {"business_weekdays", "holidays", "bands", "floor_fields", "identity_max_wrong",
                    "identity_order", "promise_max_attempts", "outcome_table", "notes_enabled", "product_limit"}
        if required - policy.facts.keys() or any(gap["code"] != "semantic_compiler_pending" for gap in policy.gaps):
            raise FoundationError("Local guard kernel requires resolved parameter facts; semantic compilation is still pending")
        self.contract, self.policy, self.state, self.bank = contract, policy, state, bank
        self.calendar = BusinessCalendar.from_policy(policy)
        self.events: list[dict] = []
        self.outcomes = {row["outcome"]: row["reasons"] for row in policy.facts["outcome_table"].value}
        fields = contract.operations["verify_identity"].request["properties"].get("field", {}).get("enum", [])
        if not state.required_identity_fields or len(set(state.required_identity_fields)) != len(state.required_identity_fields):
            raise FoundationError("Explicit, unique identity factor order required")
        if not set(state.required_identity_fields) <= set(fields):
            raise FoundationError("Identity factors do not match this API contract")
        if "identity_order" not in policy.facts or tuple(policy.facts["identity_order"].value) != state.required_identity_fields:
            raise FoundationError("Identity factor order must match the source-derived policy")

    def ensure_open(self):
        if self.state.close_attempted or self.state.finalizing:
            raise FoundationError("No new customer action is allowed after finalization starts")

    def require_verified(self):
        self.ensure_open()
        if not self.state.identity_verified:
            raise FoundationError("Identity gate blocks this operation")

    def _perform(self, action: Action) -> tuple[str, dict]:
        self.contract.validate_request(action.operation, action.payload)
        response = self.bank.call(action)
        status = self.contract.business_result(action.operation, response)
        self.events.append({"operation": action.operation, "business_result": status})
        return status, response

    def verify_answer(self, field_name: str, answer: str):
        self.ensure_open()
        limit = self.policy.facts["identity_max_wrong"].value
        if self.state.identity_wrong >= limit:
            raise FoundationError("Identity error limit is exhausted")
        remaining = [name for name in self.state.required_identity_fields if name not in self.state.verified]
        if not remaining or field_name != remaining[0]:
            raise FoundationError("Identity factors must be checked in the supplied policy order")
        if field_name == "date_of_birth":
            iso_date(answer)
        elif field_name == "phone_last4" and (not isinstance(answer, str) or not re.fullmatch(r"[0-9]{4}", answer)):
            raise FoundationError("Phone suffix must contain exactly four digits")
        status, response = self._perform(Action("verify_identity", {"customer_id": self.state.customer_id,
                                                                   "field": field_name, "answer": answer}))
        if status != "succeeded":
            return status
        if type(response.get("verified")) is not bool:
            return "unknown"
        if response.get("verified") is True:
            self.state.verified.add(field_name)
        elif response.get("verified") is False and not response.get("hint"):
            self.state.identity_wrong += 1
        # all_verified alone is never enough to bypass a locally unverified factor.
        return status

    def fetch_products(self):
        self.require_verified()
        if self.state.products:
            raise FoundationError("Account snapshot was already loaded")
        status, response = self._perform(Action("get_account", {"customer_id": self.state.customer_id}))
        if status != "succeeded":
            return status
        records = response.get("products")
        if not isinstance(records, list) or not 1 <= len(records) <= self.policy.facts["product_limit"].value:
            raise FoundationError("Successful account response has no product snapshot")
        products, identifiers = [], set()
        for record in records:
            try:
                identifier = record["product_id"]
                floor_field = self.policy.facts["floor_fields"].value[record["type"]]
                past_due, floor = money(record["past_due"]), money(record[floor_field])
                deadline = self.calendar.deadline(self.state.now, record["days_late"], self.policy.facts["bands"].value)
            except KeyError as exc:
                raise FoundationError("Account record or floor binding is incomplete") from exc
            if not isinstance(identifier, str) or not identifier or identifier in identifiers or floor > past_due:
                raise FoundationError("Invalid/duplicate product identifier or inconsistent payment limits")
            identifiers.add(identifier)
            products.append(Product(identifier, floor, past_due, deadline.isoformat(), details=copy.deepcopy(record)))
        self.state.products = products
        return status

    def product(self, identifier: str):
        matches = [product for product in self.state.products if product.product_id == identifier]
        if len(matches) != 1:
            raise FoundationError("Product ID must come from the account snapshot")
        return matches[0]

    def set_draft(self, proposals: list[dict]):
        self.require_verified()
        self.state.approved_digest = ""  # An invalid correction also revokes old approval.
        if self.state.transfer_attempted:
            raise FoundationError("No promise proposals are accepted after transfer starts")
        draft = []
        seen = set()
        for proposal in proposals:
            product = self.product(proposal["product_id"])
            if product.product_id in seen or product.recorded or product.uncertain_write:
                raise FoundationError("Duplicate, recorded or uncertain promise cannot be changed")
            amount = money(proposal["amount"])
            if not product.floor <= amount <= product.past_due:
                raise FoundationError("Payment amount outside source-derived floor/full interval")
            self.calendar.validate_promise_date(proposal["date"], self.state.now,
                                                date.fromisoformat(product.deadline))
            seen.add(product.product_id)
            draft.append(Promise(product.product_id, amount, proposal["date"]))
        if not draft:
            raise FoundationError("An empty proposal bundle cannot be confirmed")
        order = {product.product_id: index for index, product in enumerate(self.state.products)}
        self.state.draft = tuple(sorted(draft, key=lambda promise: order[promise.product_id]))
        self.state.approved_digest = ""  # Every revision invalidates previous consent.
        return self.draft_digest()

    def draft_digest(self):
        return hashlib.sha256(json.dumps([promise.payload(self.state.customer_id) for promise in self.state.draft],
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def confirm(self, readback_digest: str, explicit_yes: bool):
        self.require_verified()
        self.state.approved_digest = ""
        if self.state.transfer_attempted or not self.state.draft or explicit_yes is not True or readback_digest != self.draft_digest():
            raise FoundationError("A clear yes to the current read-back is required")
        self.state.approved_digest = readback_digest

    def record_promise(self, identifier: str):
        self.require_verified()
        if self.state.transfer_attempted or not self.state.approved_digest or self.state.approved_digest != self.draft_digest():
            raise FoundationError("Promise requires valid current confirmation")
        product = self.product(identifier)
        proposal = next((promise for promise in self.state.draft if promise.product_id == identifier), None)
        if proposal is None or product.recorded or product.uncertain_write:
            raise FoundationError("Promise is not approved, already recorded, or uncertain")
        if product.attempts >= self.policy.facts["promise_max_attempts"].value:
            raise FoundationError("Source-derived promise retry limit exhausted")
        product.attempts += 1
        try:
            status, response = self._perform(Action("create_promise", proposal.payload(self.state.customer_id)))
        except FoundationError:
            product.uncertain_write = True
            raise
        if status == "succeeded" and response.get("promise_id"):
            product.recorded, product.note_reason = proposal, ""
        elif status == "unknown" or (status == "succeeded" and not response.get("promise_id")):
            product.uncertain_write = True
            status = "unknown"
        return status

    def defer_note(self, identifier: str, reason: str):
        self.require_verified()
        if self.state.transfer_attempted or self.product(identifier).recorded:
            raise FoundationError("Notes are forbidden after transfer or for a promised product")
        allowed = self.contract.operations["add_note"].request["properties"]["reason"].get("enum", [])
        if reason not in allowed:
            raise FoundationError("Note reason is not allowed by this API")
        self.product(identifier).note_reason = reason

    def schedule_callback(self, day: str, time: str):
        self.ensure_open()
        if "callback_hours" not in self.policy.facts or not self.calendar.callback_allowed(day, time, self.state.now, self.policy.facts["callback_hours"].value):
            raise FoundationError("Callback is outside the source-derived window")
        return self._perform(Action("schedule_callback", {"customer_id": self.state.customer_id, "date": day, "time": time}))[0]

    def transfer(self, queue: str, reason: str):
        self.ensure_open()
        if self.state.transfer_attempted:
            raise FoundationError("Transfer already attempted")
        if reason not in self.outcomes.get("transferred", []):
            raise FoundationError("Transfer reason must come from the supplied outcome table")
        action = Action("transfer_to_agent", {"customer_id": self.state.customer_id, "queue": queue})
        self.contract.validate_request(action.operation, action.payload)
        self.state.transfer_attempted = True
        self.state.approved_digest = ""
        for product in self.state.products:
            product.note_reason = ""
        try:
            status, _ = self._perform(action)
        except FoundationError:
            self.state.transfer_uncertain = True
            raise
        if status == "succeeded":
            self.state.transfer_reason = reason
        elif status in {"failed", "unknown"}:
            self.state.transfer_uncertain = True
        return status

    def outcome_for_products(self):
        if self.state.transfer_uncertain:
            raise FoundationError("Ambiguous tool state cannot be reported as a known outcome")
        if self.state.transfer_reason:
            return "transferred", self.state.transfer_reason
        if any(p.uncertain_write for p in self.state.products):
            raise FoundationError("Ambiguous promise state cannot be reported as a known payment outcome")
        if any(product.recorded for product in self.state.products):
            return "promise", ""
        if not self.state.products or any(not product.note_reason for product in self.state.products):
            raise FoundationError("Each unpromised product needs a resolved outcome")
        first = next((product for product in self.state.products if product.note_reason != "paid"), None)
        return ("no_promise", first.note_reason) if first else ("paid", "")

    def finalize(self, outcome: str, reason: str, *, goodbye_spoken: bool):
        self.ensure_open()
        if goodbye_spoken is not True:
            raise FoundationError("Goodbye must precede the bank close operation")
        if outcome not in self.outcomes or (self.outcomes[outcome] and reason not in self.outcomes[outcome]) or (not self.outcomes[outcome] and reason):
            raise FoundationError("Outcome/reason does not match the source table")
        if self.state.products or self.state.transfer_attempted:
            if (outcome, reason) != self.outcome_for_products():
                raise FoundationError("Closing contradicts confirmed product/transfer state")
        elif outcome in {"promise", "paid", "transferred"}:
            raise FoundationError("Successful outcome has no confirming state")
        actions = []
        if self.policy.facts["notes_enabled"].value and not self.state.transfer_attempted:
            for product in self.state.products:
                if not product.recorded:
                    actions.append(Action("add_note", {"customer_id": self.state.customer_id,
                                  "product_id": product.product_id, "reason": product.note_reason}))
        payload = {"customer_id": self.state.customer_id, "outcome": outcome}
        if reason:
            payload["reason"] = reason
        actions.append(Action("close_call", payload))
        for action in actions:
            self.contract.validate_request(action.operation, action.payload)
        self.state.finalizing = True
        overall_status = "succeeded"
        first_error = None
        for action in actions:
            if action.operation == "close_call":
                self.state.close_attempted = True
            try:
                status, _ = self._perform(action)
            except FoundationError as exc:
                status = "unknown"
                first_error = first_error or exc
            if status != "succeeded":
                overall_status = status if overall_status == "succeeded" else overall_status
        # Even if a note fails, close is attempted once, last. A failed batch never passes.
        if first_error:
            raise first_error
        return overall_status
