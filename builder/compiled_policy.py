"""Typed lowering of source-checked model IR to the existing local guard kernel.

This deliberately does not bypass Conversation's reviewed-practice hash admission.
Unknown decisions block lowering; source-valid IR is not semantic certification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from compiler.core import validate_decisions
from .api_contract import validate_value
from .calendar import BusinessCalendar, iso_date
from .models import Fact, FoundationError, Policy, Requirement
from .plan import workflow_plan
from .runtime import CallState, GuardedSession, MockBank, money


REQUIRED_FACTS = {"business_weekdays", "holidays", "bands", "floor_fields", "identity_max_wrong",
                  "identity_order", "promise_max_attempts", "outcome_table", "notes_enabled", "product_limit"}
FACT_NAMES = REQUIRED_FACTS | {"callback_hours"}
BOOLEAN_GUARDS = {"identity_verified", "consent_current", "amount_within_bounds", "date_is_business_day",
                  "goodbye_spoken", "close_once_last", "no_write_after_transfer"}
MONEY_TARGET = "payment.amount"
DATE_TARGETS = {"payment.date_on_or_before", "payment.date_on_or_after"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _validate_facts(facts, contract):
    missing = REQUIRED_FACTS - facts.keys()
    if missing or set(facts) - FACT_NAMES:
        raise FoundationError(f"Unsupported/missing kernel parameters: {sorted(missing | (set(facts) - FACT_NAMES))}")
    values = {key: item.value for key, item in facts.items()}
    days = values["business_weekdays"]
    if not isinstance(days, list) or len(set(days)) != len(days) or any(type(day) is not int for day in days):
        raise FoundationError("Invalid business weekdays")
    holidays = values["holidays"]
    if not isinstance(holidays, list) or len(set(holidays)) != len(holidays):
        raise FoundationError("Invalid holiday list")
    calendar = BusinessCalendar.from_policy(Policy([], facts, [], [], []))
    for holiday in holidays:
        iso_date(holiday)
    for name in ("identity_max_wrong", "promise_max_attempts", "product_limit"):
        maximum = 3 if name == "product_limit" else 100
        if type(values[name]) is not int or not 1 <= values[name] <= maximum:
            raise FoundationError("Kernel attempt/product limit outside supported bounds")
    if type(values["notes_enabled"]) is not bool:
        raise FoundationError("notes_enabled must be boolean")
    fields = contract.operations["verify_identity"].request["properties"]["field"].get("enum", [])
    order = values["identity_order"]
    # The kernel has explicit normalization only for these factor formats. No invented new identity adapter.
    if not isinstance(order, list) or not order or len(set(order)) != len(order) or not set(order) <= set(fields) or not set(order) <= {"date_of_birth", "phone_last4"}:
        raise FoundationError("Unsupported identity factor binding")
    if set(order) != set(fields):
        raise FoundationError("All contract identity factors must remain mandatory")
    products = contract.operations["get_account"].response["properties"]["products"]["items"]["properties"]
    floor = values["floor_fields"]
    if not isinstance(floor, dict) or set(floor) != set(products["type"]["enum"]) or any(products.get(field, {}).get("type") != "number" for field in floor.values()):
        raise FoundationError("Unsupported/incomplete payment floor mapping")
    bands = values["bands"]
    if not isinstance(bands, list) or not bands:
        raise FoundationError("Explicit delinquency bands required")
    prior = []
    for band in bands:
        keys = {"name", "min_days_late", "max_days_late", "deadline_business_days", "persuasion_attempts"}
        if set(band) != keys or any(type(band[key]) is not int for key in keys - {"name"}):
            raise FoundationError("Invalid band value types")
        if not (0 <= band["min_days_late"] <= band["max_days_late"] and 1 <= band["deadline_business_days"] <= 3660 and 0 <= band["persuasion_attempts"] <= 100):
            raise FoundationError("Invalid band limits")
        if any(band["min_days_late"] <= other["max_days_late"] and other["min_days_late"] <= band["max_days_late"] for other in prior):
            raise FoundationError("Contradictory overlapping bands")
        prior.append(band)
    table = values["outcome_table"]
    allowed = contract.operations["close_call"].request["properties"]["outcome"].get("enum", [])
    if not isinstance(table, list) or len({row["outcome"] for row in table}) != len(table) or {row["outcome"] for row in table} != set(allowed):
        raise FoundationError("Outcome table must match complete current API enum")
    for row in table:
        if len(set(row["reasons"])) != len(row["reasons"]):
            raise FoundationError("Duplicate closing reason")
        if row["outcome"] in {"promise", "paid"} and row["reasons"]:
            raise FoundationError("Kernel does not support reason on promise/paid")
        if row["outcome"] not in {"promise", "paid"} and not row["reasons"]:
            raise FoundationError("Reason-bearing outcome needs explicit permitted reasons")
    if "callback_hours" in values:
        hours = values["callback_hours"]
        if set(hours) != {"start", "end", "inclusive"} or hours["inclusive"] is not True:
            raise FoundationError("Only explicit inclusive callback windows are supported")
        for value in (hours["start"], hours["end"]):
            if not re.fullmatch(r"\d{2}:\d{2}", value):
                raise FoundationError("Invalid callback window")
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError:
                raise FoundationError("Invalid callback window") from None
        if hours["start"] > hours["end"]:
            raise FoundationError("Cross-midnight callback windows unsupported")
    return calendar


def _operations(rule, contract):
    if not rule["conditions"]:
        return tuple(sorted(contract.operations))
    if len(rule["conditions"]) != 1:
        raise FoundationError("Unsupported compound lowering condition")
    condition = rule["conditions"][0]
    if condition["fact"] != "operation":
        raise FoundationError("Unsupported dynamic lowering condition")
    if condition["operator"] == "eq" and isinstance(condition["value"], str):
        operations = [condition["value"]]
    elif condition["operator"] == "in" and isinstance(condition["value"], list):
        operations = condition["value"]
    else:
        raise FoundationError("Unsupported operation predicate")
    if not operations or not set(operations) <= set(contract.operations):
        raise FoundationError("Lowering predicate refers to unsupported operation")
    return tuple(sorted(set(operations)))


def _winning_effects(decisions):
    """Only explicit override paths suppress conflicting effects, never priority alone."""
    rules = {rule["id"]: rule for rule in decisions["rules"]}

    def reaches(source, target):
        pending, visited = list(rules[source]["overrides"]), set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(rules[current]["overrides"])
        return False

    groups = {}
    for rule in rules.values():
        if rule["exceptions"]:
            raise FoundationError("Unsupported exception lowering; exception semantics cannot be ignored")
        for index, effect in enumerate(rule["effects"]):
            key = (_json(rule["conditions"]), effect["target"])
            groups.setdefault(key, []).append((rule, index, effect))
    winners, superseded = [], []
    for entries in groups.values():
        active = [entry for entry in entries if not any(other[0]["id"] != entry[0]["id"] and reaches(other[0]["id"], entry[0]["id"]) for other in entries)]
        if len({_json({"kind": item[2]["kind"], "value": item[2]["value"]}) for item in active}) != 1:
            raise FoundationError("Contradictory effects without explicit memo override")
        winners.extend(active)
        superseded.extend((rule["id"], index) for rule, index, _ in entries if (rule, index, _) not in active)
    return winners, superseded


@dataclass
class CompiledPolicy:
    policy: Policy
    semantics: dict
    plan: dict
    coverage: dict
    constraints: list[dict]
    contract: object

    def create_session(self, variables, bank):
        if type(bank) is not MockBank:
            raise FoundationError("Compiled integration supports local MockBank only")
        if not isinstance(variables, dict) or not isinstance(variables.get("customer_id"), str) or not variables["customer_id"].strip():
            raise FoundationError("Explicit customer_id required")
        try:
            datetime.fromisoformat(variables["now"])
        except (KeyError, TypeError, ValueError):
            raise FoundationError("Explicit valid call now required") from None
        return CompiledSession(self, variables, bank)

    def serializable(self):
        return {"kind": "lowered_kernel_policy", "policy": self.policy.serializable(),
                "semantics": self.semantics, "plan": self.plan, "coverage": self.coverage,
                "constraints": self.constraints, "publish_allowed": False}


class CompiledSession:
    """Programmatic local bridge; not a substitute for general language Conversation."""
    def __init__(self, compiled, variables, bank):
        self.compiled = compiled
        state = CallState(variables["customer_id"], variables["now"], tuple(compiled.policy.facts["identity_order"].value))
        self.kernel = GuardedSession(compiled.contract, copy.deepcopy(compiled.policy), state, bank)
        self.trace = []

    @property
    def state(self):
        return self.kernel.state

    def _guard(self, operation, *, proposal=None, goodbye_spoken=False, preparing_draft=False):
        for constraint in self.compiled.constraints:
            if operation not in constraint["operations"]:
                continue
            target, value = constraint["target"], constraint["value"]
            if target == "consent_current" and preparing_draft:
                continue
            if target == MONEY_TARGET and proposal is not None and money(proposal["amount"]) > money(value):
                raise FoundationError("Compiled amount ceiling blocks proposal")
            if target in DATE_TARGETS and proposal is not None:
                actual, boundary = iso_date(proposal["date"]), iso_date(value)
                if (target.endswith("before") and actual > boundary) or (target.endswith("after") and actual < boundary):
                    raise FoundationError("Compiled date constraint blocks proposal")
            checks = {"identity_verified": self.state.identity_verified,
                      "consent_current": bool(self.state.approved_digest) and self.state.approved_digest == self.kernel.draft_digest(),
                      "goodbye_spoken": goodbye_spoken,
                      "close_once_last": not self.state.close_attempted,
                      "no_write_after_transfer": not self.state.transfer_attempted}
            if target in checks and not checks[target]:
                raise FoundationError("Compiled source guard blocks operation: " + target)
            # Amount/date validity is additionally enforced by the kernel before any write.
        self.trace.append({"operation": operation, "phase": "proposal_validation" if preparing_draft else "operation_guard",
                           "source_guards_checked": True, "bank_operation_executed_by_this_check": False})

    def verify_answer(self, field_name, answer):
        self._guard("verify_identity")
        return self.kernel.verify_answer(field_name, answer)

    def fetch_products(self):
        self._guard("get_account")
        return self.kernel.fetch_products()

    def set_draft(self, proposals):
        # Revoke old confirmation before validating corrections, even if a compiled check rejects them.
        self.state.approved_digest = ""
        for proposal in proposals:
            self._guard("create_promise", proposal=proposal, preparing_draft=True)
        return self.kernel.set_draft(proposals)

    def confirm(self, readback_digest, explicit_yes):
        return self.kernel.confirm(readback_digest, explicit_yes)

    def record_promise(self, identifier):
        proposal = next((p.payload(self.state.customer_id) for p in self.state.draft if p.product_id == identifier), None)
        self._guard("create_promise", proposal=proposal)
        return self.kernel.record_promise(identifier)

    def defer_note(self, identifier, reason):
        self._guard("add_note")
        return self.kernel.defer_note(identifier, reason)

    def transfer(self, queue, reason):
        self._guard("transfer_to_agent")
        return self.kernel.transfer(queue, reason)

    def finalize(self, outcome, reason, *, goodbye_spoken):
        self._guard("close_call", goodbye_spoken=goodbye_spoken)
        return self.kernel.finalize(outcome, reason, goodbye_spoken=goodbye_spoken)


def lower_compilation(envelope, documents, contract):
    if envelope.get("kind") != "source_checked_decision_ir" or envelope.get("schema_version") != "compiler-envelope-1":
        raise FoundationError("Unsupported compiler envelope")
    if envelope.get("contract_sha256") != contract.source_hash:
        raise FoundationError("Compiler/API version mismatch")
    if envelope.get("sources") != [document.inventory() for document in documents]:
        raise FoundationError("Compiler source inventory does not match supplied raw documents")
    decisions = validate_decisions(copy.deepcopy(envelope["decisions"]), documents, contract)
    sources = {document.source_id: document for document in documents}

    def evidence(ref):
        return sources[ref["source_id"]].evidence(ref["start"], ref["end"], "model_ir_exact_quote")

    winners, superseded = _winning_effects(decisions)
    facts = {item["name"]: Fact(copy.deepcopy(item["value"]), tuple(evidence(ref) for ref in item["evidence"]), "source_checked_model_lowering") for item in decisions["facts"]}
    constraints, produced = [], set()
    for rule, index, effect in winners:
        target, value = effect["target"], effect["value"]
        if effect["kind"] == "set" and target.startswith("policy."):
            name = target[7:]
            if rule["conditions"] or name not in FACT_NAMES or name not in facts or _json(facts[name].value) != _json(value):
                raise FoundationError("Unsupported or inconsistent parameter assignment")
            produced.add(name)
        elif effect["kind"] == "limit" and target == MONEY_TARGET:
            money(value)
            constraints.append({"decision_id": rule["id"], "operations": list(_operations(rule, contract)), "target": target, "value": value})
        elif effect["kind"] == "require" and target in DATE_TARGETS:
            iso_date(value)
            constraints.append({"decision_id": rule["id"], "operations": list(_operations(rule, contract)), "target": target, "value": value})
        elif effect["kind"] == "require" and target in BOOLEAN_GUARDS and value is True:
            constraints.append({"decision_id": rule["id"], "operations": list(_operations(rule, contract)), "target": target, "value": value})
        else:
            raise FoundationError("Unsupported decision lowering: " + target)
    if produced != set(facts):
        raise FoundationError("Every kernel fact requires an active source-linked assignment rule")
    _validate_facts(facts, contract)
    # Draft construction must not require consent; that guard is enforced at record_promise.
    for constraint in constraints:
        if constraint["target"] == "consent_current" and constraint["operations"] != ["create_promise"]:
            raise FoundationError("Consent guard only supports promise writes")
        if constraint["target"] in {MONEY_TARGET, *DATE_TARGETS, "amount_within_bounds", "date_is_business_day"} and constraint["operations"] != ["create_promise"]:
            raise FoundationError("Payment guards only support promise writes")
        supported_operations = {"identity_verified": {"get_account", "create_promise", "add_note"},
                                "goodbye_spoken": {"close_call"}, "close_once_last": {"close_call"},
                                "no_write_after_transfer": {"create_promise", "add_note"}}
        if constraint["target"] in supported_operations and not set(constraint["operations"]) <= supported_operations[constraint["target"]]:
            raise FoundationError("Guard mapped to unsupported operation")
    earliest = [iso_date(item["value"]) for item in constraints if item["target"] == "payment.date_on_or_after"]
    latest = [iso_date(item["value"]) for item in constraints if item["target"] == "payment.date_on_or_before"]
    if earliest and latest and max(earliest) > min(latest):
        raise FoundationError("Contradictory compiled payment date interval")
    requirements = [Requirement(rule["id"], rule["evidence"][0]["quote"], "compiled_kernel", evidence(rule["evidence"][0]),
                                "model_source_rule", "lowered_supported_kernel_decision") for rule in decisions["rules"]]
    policy = Policy(requirements, facts, [], [], [d.source_id for d in documents], "compiled-kernel-policy-1")
    cited = {d.source_id: {position for rule in decisions["rules"] for ref in rule["evidence"] if ref["source_id"] == d.source_id for position in range(ref["start"], ref["end"])} for d in documents}
    uncited = []
    for document in documents:
        # Expose contiguous nonwhitespace blocks not mentioned in decision evidence. No hidden full-coverage claim.
        start = None
        for position in range(len(document.text) + 1):
            missing = position < len(document.text) and position not in cited[document.source_id] and not document.text[position].isspace()
            if missing and start is None:
                start = position
            elif not missing and start is not None:
                uncited.append({"source_id": document.source_id, "start": start, "end": position,
                               "status": "not_semantically_accounted_for", "text": document.text[start:position]})
                start = None
    coverage = {"kind": "supported_kernel_lowering_coverage", "supplied_rules": len(requirements),
                "lowered_effects": len(winners), "superseded_effects": len(superseded),
                "unsupported_decisions": [], "unresolved_count": 0, "full_source_semantic_coverage": False,
                "uncited_source_blocks": uncited,
                "supported_phases": ["identity", "account", "proposal_validation", "readback_confirmation", "promise_recording", "transfer", "finalization"],
                "unsupported_phases": ["opening_wording", "person_check", "third_party_conversation", "persuasion", "hardship_conversation", "free_text_intent", "callback_execution"],
                "supported_tools": ["verify_identity", "get_account", "create_promise", "add_note", "transfer_to_agent", "close_call"],
                "unsupported_tools": ["schedule_callback"],
                "decision_status": [{"id": rule["id"], "status": "lowered_to_kernel" if any(entry[0]["id"] == rule["id"] for entry in winners) else "superseded_by_explicit_override"} for rule in decisions["rules"]],
                "sources": [{"source_id": d.source_id, "nonwhitespace_characters": sum(not char.isspace() for char in d.text),
                             "cited_nonwhitespace_characters": len({position for rule in decisions["rules"] for ref in rule["evidence"] if ref["source_id"] == d.source_id for position in range(ref["start"], ref["end"]) if not d.text[position].isspace()})} for d in documents],
                "semantic_truth_verified": False, "model_evaluation_performed": False}
    semantics = {"kind": "typed_model_kernel_semantics", "source_version_admitted": True,
                 "complete_for_supplied_practice": False, "complete_for_supported_decisions": True,
                 "general_semantic_compiler": False, "decisions": decisions["rules"], "publish_allowed": False,
                 "conversation_compatible": False,
                 "remaining_limits": ["Reviewed practice Conversation/export remains version-bound; no admission bypass",
                                      "Source citation validity does not establish complete or correct model interpretation"]}
    plan = workflow_plan(policy)
    plan.update({"kind": "compiled_kernel_plan", "entry_point": "builder.compiled_policy.CompiledSession",
                 "supported_methods": ["verify_answer", "fetch_products", "set_draft", "confirm", "record_promise", "defer_note", "transfer", "finalize"],
                 "conversation_compatible": False, "publish_allowed": False})
    return CompiledPolicy(policy, semantics, plan, coverage, constraints, contract)
