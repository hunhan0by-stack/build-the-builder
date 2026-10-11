"""Reviewed practice sources -> bounded native policy/wording authorization.

The original brief states instructions, not literal opening/identity/readback
sentences. Templates here are DERIVED from the existing reviewed Conversation
implementation and cite both implementation spans and original instructions.
Generated template text is never represented as an official sentence quotation.
Changed/final prose is rejected by the existing practice-version admission.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path

from .compiled_policy import lower_compilation
from .extract import extract_policy
from .ingest import read_source, TEXT_FORMATS
from .models import Document, FoundationError
from .semantic_renderer import SourceRenderer, validate_wording_bundle
from .semantics import compile_practice_policy


GENERATED_KIND = "generated_reviewed_runtime_templates"
AUTHORIZATION_VERSION = "reviewed-runtime-wording-1"
_GUARDS = {
    "get_account_after_both_factors": ("identity_verified", "get_account"),
    "clear_yes_before_write": ("consent_current", "create_promise"),
    "business_day": ("date_is_business_day", "create_promise"),
    "accept_inclusive_floor_full_interval": ("amount_within_bounds", "create_promise"),
    "thank_and_goodbye_before_close": ("goodbye_spoken", "close_call"),
    "exactly_one_close": ("close_once_last", "close_call"),
}


def _implementation_source():
    path = Path(__file__).with_name("conversation.py")
    raw = path.read_bytes()
    if len(raw) > 200000:
        raise FoundationError("Reviewed implementation source exceeds the local bound")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(raw).hexdigest()
    return Document("implementation_" + digest[:16], "builder/conversation.py", digest, len(raw), "reviewed_python_source", text)


def _validate_originals(documents, source_snapshots):
    if source_snapshots is not None:
        if not isinstance(source_snapshots, dict) or len(source_snapshots) > 32:
            raise FoundationError("Reviewed source snapshots need a bounded hash-to-bytes mapping")
        for digest, raw in source_snapshots.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(raw, bytes) or len(raw) > 4000000:
                raise FoundationError("Reviewed source snapshot has invalid hash, bytes or size")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise FoundationError("Reviewed source snapshot raw hash mismatch")
    for doc in documents:
        if source_snapshots is None:
            try:
                current = read_source(Path(doc.path))
            except (OSError, FoundationError):
                raise FoundationError("Reviewed original source snapshot unavailable") from None
        else:
            raw = source_snapshots.get(doc.sha256)
            if raw is None:
                raise FoundationError("Reviewed source snapshot is missing the required original hash")
            if doc.kind not in TEXT_FORMATS:
                raise FoundationError("Reviewed source snapshot kind is unsupported")
            # A fixed filename + admitted extension prevents recorded logical paths
            # from controlling where parsing writes. Use the real parser, unpatched.
            with tempfile.TemporaryDirectory(prefix="native-source-snapshot-") as temporary:
                path = Path(temporary) / ("snapshot" + doc.kind)
                path.write_bytes(raw)
                current = read_source(path)
            current.path = doc.path  # recorded logical source identity, not a disk claim
        if current.inventory() != doc.inventory() or current.text != doc.text:
            raise FoundationError("Reviewed original source snapshot changed")


def _reference(ref, documents):
    document = next((doc for doc in documents if doc.source_id == ref["source_id"]), None)
    if document is None or not 0 <= ref["start"] < ref["end"] <= len(document.text):
        raise FoundationError("Reviewed native source span is missing")
    return {"source_id": document.source_id, "start": ref["start"], "end": ref["end"],
            "quote": document.text[ref["start"]:ref["end"]]}


class _Phrases:
    """Finite AST grammar, not a Python evaluator or new wording generator."""
    def __init__(self, implementation, semantics):
        self.document = implementation
        self.tree = ast.parse(implementation.text)
        classes = [node for node in self.tree.body if isinstance(node, ast.ClassDef) and node.name == "Conversation"]
        if len(classes) != 1:
            raise FoundationError("Reviewed Conversation implementation class is missing")
        self.methods = {node.name: node for node in classes[0].body if isinstance(node, ast.FunctionDef)}
        self.decisions = {item["id"]: item for item in semantics["decisions"]}
        self.spans = []
        self.recipes = {}

    def effect(self, effect):
        found = [item for item in self.decisions.values() if effect in item["required_effects"]]
        if len(found) != 1:
            raise FoundationError("Reviewed wording effect is ambiguous or missing: " + effect)
        return found[0]

    def calls(self, method, effect=None):
        found = [node for node in ast.walk(self.methods[method]) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "self" and node.func.attr == "_say"]
        found.sort(key=lambda node: (node.lineno, node.col_offset))
        if effect:
            identifier = self.effect(effect)["id"]
            found = [node for node in found if identifier in [arg.value for arg in node.args[1:] if isinstance(arg, ast.Constant)]]
        return found

    def span(self, node):
        lines = self.document.text.splitlines(keepends=True)
        def offset(line, byte_column):
            return sum(len(value) for value in lines[:line - 1]) + len(lines[line - 1].encode()[:byte_column].decode())
        start, end = offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)
        span = {"source_path": self.document.path, "source_sha256": self.document.sha256,
                "start": start, "end": end, "quote": self.document.text[start:end]}
        self.spans.append(span)
        return span

    def expression(self, node, *, substitutions=None):
        substitutions = substitutions or {}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for item in node.values:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    parts.append(item.value)
                    continue
                if not isinstance(item, ast.FormattedValue) or item.conversion != -1:
                    raise FoundationError("Unsupported reviewed f-string expression")
                expression = ast.unparse(item.value)
                if expression in substitutions:
                    name = substitutions[expression]
                elif isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Attribute) and ast.unparse(item.value.func) == "self._label":
                    name = "product_label"
                elif isinstance(item.value, ast.Attribute) and item.value.attr == "past_due":
                    name = "past_due"
                elif isinstance(item.value, ast.Subscript) and isinstance(item.value.slice, ast.Constant):
                    key = item.value.slice.value
                    owner = ast.unparse(item.value.value)
                    if owner == "self.variables" and key in {"title", "last_name"}:
                        name = key
                    elif owner == "product.details" and key == "days_late":
                        name = key
                    elif owner == "item" and key in {"date", "amount"}:
                        name = key
                    else:
                        raise FoundationError("Unsupported reviewed slot expression")
                elif isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Name) and item.value.func.id == "money" and len(item.value.args) == 1:
                    arg = item.value.args[0]
                    if not isinstance(arg, ast.Subscript) or not isinstance(arg.slice, ast.Constant) or arg.slice.value != "amount":
                        raise FoundationError("Unsupported reviewed money expression")
                    name = "amount"
                else:
                    raise FoundationError("Unsupported reviewed expression: " + expression)
                if item.format_spec is not None:
                    spec = self.expression(item.format_spec)
                    if spec != ".2f" or name not in {"amount", "past_due"}:
                        raise FoundationError("Unsupported reviewed display format")
                    self.recipes[name + "_display"] = {"from": name, "format": "fixed_two_decimal"}
                    name += "_display"
                self.recipes.setdefault(name, {"from": name, "format": "plain"})
                parts.append("{" + name + "}")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.expression(node.left, substitutions=substitutions) + self.expression(node.right, substitutions=substitutions)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
            if isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str) and isinstance(node.args[0], ast.Name) and node.args[0].id in substitutions:
                return substitutions[node.args[0].id]  # exactly one product; separator is unused
        raise FoundationError("Unsupported reviewed wording AST grammar")

    def phrase(self, call, documents, *, node=None, substitutions=None):
        self.spans = []
        expression = call.args[0] if node is None else node
        template = self.expression(expression, substitutions=substitutions)
        spans = [self.span(call)]
        refs = []
        decisions = []
        for arg in call.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in self.decisions:
                decision = self.decisions[arg.value]
                decisions.append(decision["id"])
                refs.append(_reference(decision["source"], documents))
        if not refs:
            raise FoundationError("Reviewed phrase lacks an admitted source rule")
        return {"template": template, "official_instructions": refs, "reviewed_decisions": decisions,
                "implementation_spans": spans, "transformation": "finite_AST_literal_and_named_slot_extraction"}


def _combine(parts):
    return {"template": " ".join(item["template"] for item in parts),
            "official_instructions": [ref for item in parts for ref in item["official_instructions"]],
            "reviewed_decisions": list(dict.fromkeys(identifier for item in parts for identifier in item["reviewed_decisions"])),
            "implementation_spans": [span for item in parts for span in item["implementation_spans"]],
            "transformation": "ordered_concatenation_of_reviewed_runtime_phrases"}


def bind_reviewed_native_sources(documents, contract, *, source_snapshots=None):
    """Return canonical reviewed A or A+B single-account-prefix bindings."""
    documents = copy.deepcopy(list(documents))
    if not documents or any(doc.kind == GENERATED_KIND for doc in documents):
        raise FoundationError("Original source documents are required for reviewed binding")
    _validate_originals(documents, source_snapshots)
    policy = extract_policy(documents, contract)
    semantics = compile_practice_policy(documents, policy, contract)
    if not semantics.get("complete_for_supplied_practice"):
        raise FoundationError("Changed/final source prose requires a new semantic interpretation")
    multi_source = bool(policy.facts["notes_enabled"].value)
    if not multi_source and policy.facts["product_limit"].value != 1:
        raise FoundationError("Reviewed native source product scope is inconsistent")
    implementation = _implementation_source()
    phrases = _Phrases(implementation, semantics)
    opening = phrases.calls("start", "digital_assistant_bank_recording_first_sentence")
    person = phrases.calls("_ask_person", "ask_title_last_name")
    if len(opening) != 1 or len(person) != 1:
        raise FoundationError("Reviewed opening/person phrase inventory is ambiguous")
    roles = {"opening": _combine([phrases.phrase(opening[0], documents), phrases.phrase(person[0], documents)])}
    questions = phrases.calls("_identity_question", "dob_then_phone_one_at_a_time")
    conditional = next((node for node in ast.walk(phrases.methods["_identity_question"]) if isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1
                        and isinstance(node.test.comparators[0], ast.Constant)), None)
    if len(questions) != 2 or conditional is None or not isinstance(questions[0].args[0], ast.IfExp):
        raise FoundationError("Reviewed two-factor wording structure needs an explicit adapter")
    first_field = conditional.test.comparators[0].value
    other = set(policy.facts["identity_order"].value) - {first_field}
    if len(other) != 1:
        raise FoundationError("Reviewed identity wording does not cover contract factors")
    identity = {first_field: phrases.phrase(questions[0], documents, node=questions[0].args[0].orelse),
                other.pop(): phrases.phrase(questions[1], documents)}
    identity_hint = {first_field: phrases.phrase(questions[0], documents, node=questions[0].args[0].body)}
    hint_decision = phrases.effect("ask_month_as_word")
    identity_hint[first_field]["official_instructions"].append(_reference(hint_decision["source"], documents))
    identity_hint[first_field]["reviewed_decisions"].append(hint_decision["id"])
    roles["identity_question"] = identity[policy.facts["identity_order"].value[0]]
    account = phrases.calls("_load_account", "product_in_words")
    if len(account) != 2:
        raise FoundationError("Reviewed account disclosure inventory is ambiguous")
    roles["account_summary"] = _combine([phrases.phrase(call, documents) for call in account])
    if multi_source:
        proposals = phrases.calls("_load_account", "ask_all_today_once")
        multi_branch = next((node for node in ast.walk(phrases.methods["_load_account"]) if isinstance(node, ast.If)
                             and ast.unparse(node.test) == "self.multi"), None)
        if multi_branch is None or len(proposals) != 1 or not any(node is proposals[0] for statement in multi_branch.body for node in ast.walk(statement)):
            raise FoundationError("Reviewed multi-source first question is not bound to its actual branch")
    else:
        proposals = [call for call in phrases.calls("_load_account", "propose_full_past_due") if isinstance(call.args[0], ast.JoinedStr)]
    if len(proposals) != 1:
        raise FoundationError("Reviewed single-product offer inventory is ambiguous")
    roles["proposal_request"] = phrases.phrase(proposals[0], documents)
    readback = phrases.calls("_readback", "product_amount_date_readback")
    lines = [node for node in ast.walk(phrases.methods["_readback"]) if isinstance(node, ast.ListComp) and isinstance(node.elt, ast.JoinedStr)]
    if len(readback) != 1 or len(lines) != 1:
        raise FoundationError("Reviewed single-product readback structure needs an adapter")
    line_template = phrases.expression(lines[0].elt)
    roles["readback"] = phrases.phrase(readback[0], documents, substitutions={"lines": line_template})
    roles["readback"]["implementation_spans"].append(phrases.span(lines[0]))
    # One account is a valid returned cardinality under the reviewed multi-source
    # brief. These overlays bind that exact prefix, not the unimplemented two/three
    # account, refusal/note, common-date adjustment, transfer or write branches.
    if multi_source:
        for role, effect in (("account_summary", "state_every_product_before_any_question"),
                             ("readback", "one_bundle_readback")):
            decision = phrases.effect(effect)
            roles[role]["official_instructions"].append(_reference(decision["source"], documents))
            roles[role]["reviewed_decisions"].append(decision["id"])
    successes = [call for call in phrases.calls("advance", "announce_only_ok_true") if isinstance(call.args[0], ast.JoinedStr)]
    sms = phrases.calls("advance", "exact_sms_sentence")
    if len(successes) != 2 or len(sms) != 1:
        raise FoundationError("Reviewed promise success inventory is ambiguous")
    # Both success and failure are f-strings bound to the same rule. The first
    # statement belongs to the succeeded branch; verify its actual AST condition.
    success_branch = next((node for node in ast.walk(phrases.methods["advance"]) if isinstance(node, ast.If)
                           and isinstance(node.test, ast.Compare) and ast.unparse(node.test.left) == "status"
                           and isinstance(node.test.comparators[0], ast.Constant) and node.test.comparators[0].value == "succeeded"), None)
    if success_branch is None or not any(node is successes[0] for statement in success_branch.body for node in ast.walk(statement)):
        raise FoundationError("Success wording is not guarded by the reviewed confirmed branch")
    roles["promise_success"] = _combine([phrases.phrase(successes[0], documents), phrases.phrase(sms[0], documents)])
    goodbye = phrases.calls("_close", "thank_and_goodbye_before_close")
    if len(goodbye) != 1:
        raise FoundationError("Reviewed goodbye phrase inventory is ambiguous")
    roles["goodbye"] = phrases.phrase(goodbye[0], documents)
    label_method = phrases.methods["_label"]
    label_ifs = [node for node in ast.walk(label_method) if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.comparators[0], ast.Constant)]
    if len(label_ifs) != 1:
        raise FoundationError("Reviewed product label grammar is unsupported")
    special_type = label_ifs[0].test.comparators[0].value
    special_returns = [node for statement in label_ifs[0].body for node in ast.walk(statement) if isinstance(node, ast.Return) and isinstance(node.value, ast.JoinedStr)]
    if len(special_returns) != 1:
        raise FoundationError("Reviewed product-label template is missing")
    label_field = label_ifs[0].test.left
    if not isinstance(label_field, ast.Subscript) or not isinstance(label_field.slice, ast.Constant):
        raise FoundationError("Reviewed product-label type field is unsupported")
    suffix_assignments = [node for statement in label_ifs[0].body for node in ast.walk(statement)
                          if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "get"]
    if len(suffix_assignments) != 1 or len(suffix_assignments[0].targets) != 1 or not isinstance(suffix_assignments[0].targets[0], ast.Name) or not isinstance(suffix_assignments[0].value.args[0], ast.Constant):
        raise FoundationError("Reviewed product-label suffix field is unsupported")
    suffix_field = suffix_assignments[0].value.args[0].value
    suffix_name = suffix_assignments[0].targets[0].id
    label_template = phrases.expression(special_returns[0].value, substitutions={suffix_name: suffix_field})
    fallback_returns = [node for node in label_method.body if isinstance(node, ast.Return)]
    if len(fallback_returns) != 1 or not isinstance(fallback_returns[0].value, ast.Call):
        raise FoundationError("Reviewed product-label fallback is unsupported")
    fallback = fallback_returns[0].value
    if (not isinstance(fallback.func, ast.Attribute) or fallback.func.attr != "replace" or len(fallback.args) != 2
            or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in fallback.args)
            or not isinstance(fallback.func.value, ast.Subscript) or not isinstance(fallback.func.value.slice, ast.Constant)
            or fallback.func.value.slice.value != label_field.slice.value):
        raise FoundationError("Reviewed product-label fallback is unsupported")
    label_recipe = {"field": label_field.slice.value, "suffix_field": suffix_field, "suffix_type": special_type,
                    "suffix_template": label_template, "fallback": {"kind": "replace", "old": fallback.args[0].value, "new": fallback.args[1].value},
                    "implementation_span": phrases.span(label_method)}
    entries = [(kind, item) for kind, item in roles.items()] + [("identity:" + key, item) for key, item in identity.items()] + [("hint:" + key, item) for key, item in identity_hint.items()]
    generated_text = "GENERATED reviewed runtime templates; NOT official sentence quotations.\n\n"
    locations = {}
    for key, item in entries:
        generated_text += key + "\n"
        start = len(generated_text)
        generated_text += item["template"] + "\n\n"
        locations[key] = (start, start + len(item["template"]))
    digest = hashlib.sha256(generated_text.encode()).hexdigest()
    generated = Document("generated_" + digest[:16], "derived:reviewed-runtime-templates", digest, len(generated_text.encode()),
                         GENERATED_KIND, generated_text, limitations=["Derived from reviewed implementation; not official literal wording or independent semantic approval."])
    all_documents = documents + [generated]
    def entry(key, item):
        start, end = locations[key]
        return {"template": item["template"], "evidence": {"source_id": generated.source_id, "start": start, "end": end, "quote": item["template"]}}
    bundle = {kind: entry(kind, item) for kind, item in roles.items()}
    identity_entries = {key: entry("identity:" + key, item) for key, item in identity.items()}
    hint_entries = {key: entry("hint:" + key, item) for key, item in identity_hint.items()}
    validate_wording_bundle(bundle, all_documents)
    rules, facts = [], []
    for name, fact in policy.facts.items():
        refs = [_reference(vars(ref), documents) for ref in fact.evidence]
        rules.append({"id": "reviewed-fact-" + name, "priority": 0, "conditions": [],
                      "effects": [{"kind": "set", "target": "policy." + name, "value": fact.value}],
                      "exceptions": [], "overrides": [], "evidence": refs})
        facts.append({"name": name, "value": fact.value, "category": "other", "evidence": refs})
    for effect, (target, operation) in _GUARDS.items():
        decision = phrases.effect(effect)
        rules.append({"id": "reviewed-guard-" + target, "priority": 0,
                      "conditions": [{"fact": "operation", "operator": "eq", "value": operation}],
                      "effects": [{"kind": "require", "target": target, "value": True}], "exceptions": [], "overrides": [],
                      "evidence": [_reference(decision["source"], documents)]})
    decision_ir = {"schema_version": "decision-ir-1", "rules": rules, "facts": facts,
        "api_bindings": [{"role": "identity_check", "operation": "verify_identity", "direction": "request", "field_path": "field",
                          "allowed_values": policy.facts["identity_order"].value,
                          "evidence": next(item["evidence"] for item in facts if item["name"] == "identity_order")}], "unresolved": []}
    compiled = lower_compilation({"kind": "source_checked_decision_ir", "schema_version": "compiler-envelope-1",
        "contract_sha256": contract.source_hash, "sources": [doc.inventory() for doc in all_documents], "decisions": decision_ir}, all_documents, contract)
    required_flow = {"right_party_confirmation": True,
        "source_evidence": [_reference(phrases.effect(effect)["source"], documents) for effect in ("ask_title_last_name", "exact_affirmation_or_own_name_only")],
        "identity_templates": identity_entries, "identity_hint_templates": hint_entries,
        "context_recipe": {"money_fields": {recipe["from"]: name for name, recipe in phrases.recipes.items() if recipe["format"] == "fixed_two_decimal"}, "product_label": label_recipe},
        "first_offer": "full_past_due_today", "first_offer_evidence": [_reference(phrases.effect(effect)["source"], documents) for effect in ("propose_full_past_due", "ask_payment_today_first")]}
    required_flow["account_count_scope"] = {"supported_count": 1, "source_product_limit": policy.facts["product_limit"].value,
                                            "multi_product_behavior_supported": False}
    if multi_source:
        required_flow["first_offer_evidence"] = [_reference(phrases.effect(effect)["source"], documents) for effect in ("propose_full_past_due", "ask_all_today_once")]
        required_flow["reviewed_multi_prefix"] = {
            "all_returned_accounts_disclosed_before_question": True, "single_bundle_confirmation": True,
            "per_product_deadline": True,
            "source_evidence": [_reference(phrases.effect(effect)["source"], documents) for effect in (
                "one_to_three_products", "state_every_product_before_any_question", "product_local_band_and_deadline", "ask_all_today_once", "one_bundle_readback")]}
    authorization = {"version": AUTHORIZATION_VERSION, "basis": "existing_reviewed_practice_implementation_and_original_instructions",
        "literal_official_sentence_claim": False, "generated_document_id": generated.source_id,
        "implementation_source": implementation.inventory(), "roles": roles, "identity_roles": identity, "identity_hint_roles": identity_hint,
        "required_flow": required_flow, "original_sources": [doc.inventory() for doc in documents],
        "api_contract": contract.serializable(),
        "semantic_truth_independently_verified": False, "publish_allowed": False}
    return {"compiled": compiled, "documents": all_documents, "wording_bundle": bundle,
        "wording_authorization": authorization, "required_flow": required_flow,
        "scope": "single_account_prefix_of_reviewed_multi_product_practice_sources" if multi_source else "reviewed_single_product_practice_sources",
        "unsupported": ["changed_final_prose", "multiple_returned_accounts", "multi_product_notes", "common_date_bundle_adjustment", "full_conversation_branch_coverage", "native_bank_write", "native_speech_completion", "trusted_restart"],
        "publish_allowed": False}


def verify_reviewed_native_binding(compiled, documents, wording_bundle, authorization, *, source_snapshots=None):
    """Canonical rederivation; no authorization boolean or caller-supplied role accepted."""
    originals = [doc for doc in documents if doc.kind != GENERATED_KIND]
    canonical = bind_reviewed_native_sources(originals, compiled.contract, source_snapshots=source_snapshots)
    expected_docs = [(doc.inventory(), doc.text) for doc in canonical["documents"]]
    if ([(doc.inventory(), doc.text) for doc in documents] != expected_docs or wording_bundle != canonical["wording_bundle"]
            or authorization != canonical["wording_authorization"]
            or compiled.serializable() != canonical["compiled"].serializable()
            or compiled.contract.serializable() != canonical["compiled"].contract.serializable()):
        raise FoundationError("Reviewed native binding differs from canonical source/implementation proof")
    return copy.deepcopy(canonical["required_flow"])


def render_reviewed_event(binding, event_id, kind, context, *, bank_outcome=None, identity_hint=False, source_snapshots=None):
    """Local proof renderer; exact derived phrases, not an SDK speech receipt."""
    flow = verify_reviewed_native_binding(binding["compiled"], binding["documents"], binding["wording_bundle"], binding["wording_authorization"], source_snapshots=source_snapshots)
    context = copy.deepcopy(context)
    bundle = copy.deepcopy(binding["wording_bundle"])
    if kind == "identity_question":
        variants = flow["identity_hint_templates"] if identity_hint else flow["identity_templates"]
        if context.get("field") not in variants:
            raise FoundationError("Reviewed identity wording lacks this factor/variant")
        bundle[kind] = variants[context["field"]]
    from .runtime import money
    for source, name in flow["context_recipe"]["money_fields"].items():
        if source in context:
            context[name] = format(money(context[source]), ".2f")
    if kind in {"account_summary", "readback", "promise_success"}:
        label = flow["context_recipe"]["product_label"]
        types = binding["compiled"].contract.operations["get_account"].response["properties"]["products"]["items"]["properties"]["type"]["enum"]
        if context.get("type") not in types:
            raise FoundationError("Reviewed product label needs a known account type")
        if context["type"] == label["suffix_type"]:
            suffix = context.get(label["suffix_field"])
            if not isinstance(suffix, str) or len(suffix) != 4 or not suffix.isascii() or not suffix.isdigit():
                raise FoundationError("Reviewed product label needs the API card suffix")
            context["product_label"] = label["suffix_template"].format_map({label["suffix_field"]: suffix})
        else:
            context["product_label"] = context["type"].replace(label["fallback"]["old"], label["fallback"]["new"])
    return SourceRenderer(binding["documents"], bundle).render(event_id, kind, context, bank_outcome=bank_outcome)
