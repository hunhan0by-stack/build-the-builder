from __future__ import annotations

import re
from dataclasses import asdict

from .models import FoundationError
from .policy_specs import A, B, API_HASH, BRIEF_A_HASH, BRIEF_B_HASH, OVERLAYS, PRIORITIES


def compile_practice_policy(documents, policy, contract):
    by_hash = {doc.sha256: doc for doc in documents}
    admitted = {BRIEF_A_HASH, BRIEF_B_HASH}
    if BRIEF_A_HASH not in by_hash or set(by_hash) - admitted or contract.source_hash != API_HASH:
        return {"kind": "reviewed_practice_semantics", "source_version_admitted": False,
                "complete_for_supplied_practice": False, "publish_allowed": False,
                "reason": "Curated interpretation applies only to the reviewed A/B/API versions. Changed or extra policy sources require review.",
                "decisions": [], "atoms": [], "constraints": [], "source_contracts": [], "overlays": []}
    if any(gap["code"] != "semantic_compiler_pending" for gap in policy.gaps):
        raise FoundationError("Practice semantic binding has unresolved source facts")
    decisions, atoms, constraints = [], [], []
    for profile, digest, specs in (("A", BRIEF_A_HASH, A), ("B", BRIEF_B_HASH, B)):
        if digest not in by_hash:
            continue
        doc = by_hash[digest]
        rules = [rule for rule in policy.requirements if rule.evidence.source_id == doc.source_id]
        if len(rules) != len(specs):
            raise FoundationError("Reviewed rule inventory differs from the admitted source")
        for rule in rules:
            number = int(rule.rule_id.rsplit(":r", 1)[1])
            identifier = f"{profile}.{number}"
            stage, trigger, effects, forbidden = specs[number]
            decisions.append({"id": identifier, "source_rule_id": rule.rule_id, "stage": stage,
                "trigger": trigger, "required_effects": effects, "forbidden_effects": forbidden,
                "priority": PRIORITIES.get(identifier, 50), "overlays": OVERLAYS.get(identifier, []),
                "source": asdict(rule.evidence), "implementation": "builder.conversation.Conversation",
                "interpretation": "human_reviewed_practice_binding", "independent_clause_test_coverage": "see scenario trace; not inferred from atom count"})
            for kind, values in (("required", effects), ("forbidden", forbidden)):
                for index, effect in enumerate(values, 1):
                    constraints.append({"id": f"{identifier}.{kind}.{index}", "decision_id": identifier,
                        "kind": kind, "condition": trigger, "effect": effect, "stage": stage,
                        "priority": PRIORITIES.get(identifier, 50), "source": asdict(rule.evidence),
                        "binding": "reviewed_practice_instruction", "independent_test_pass": "not_inferred"})
            # Keep every non-whitespace character, including quote fragments and sub-bullets.
            # Sentence cuts identify source fragments, not an automatic proof of atomic semantics.
            text_start = doc.text.index(rule.text, rule.evidence.start, rule.evidence.end)
            boundaries = [0] + [match.end() for match in re.finditer(r'(?<=[.!?])\s+(?=[A-Z"`]|\n)|\n\s*-\s+', rule.text)] + [len(rule.text)]
            for left, right in zip(boundaries, boundaries[1:]):
                fragment = rule.text[left:right]
                if not fragment.strip():
                    continue
                atom_id = f"{identifier}.{1 + sum(atom['decision_id'] == identifier for atom in atoms)}"
                atoms.append({"id": atom_id, "decision_id": identifier, "text": fragment,
                    "source": asdict(doc.evidence(text_start + left, text_start + right, "reviewed_rule_fragment")),
                    "required_effects": effects, "forbidden_effects": forbidden,
                    "status": "source_fragment_bound_to_reviewed_decision"})
    contracts = []
    for digest, identifier, phrase in (
        (BRIEF_A_HASH, "A.call_now", "Every call starts with these."),
        (BRIEF_A_HASH, "A.calendar", "Business days are Monday to Friday"),
        (BRIEF_A_HASH, "A.transfer_precedence", "If a transfer follows a recorded promise"),
        (BRIEF_A_HASH, "A.no_notes", "Brief A never uses `add_note`."),
        (BRIEF_B_HASH, "B.inheritance", "Every rule of Brief A applies unless"),
        (BRIEF_B_HASH, "B.closing", "The outcome table of Brief A applies.")):
        if digest not in by_hash:
            continue
        doc = by_hash[digest]
        start = doc.text.index(phrase)
        end = doc.text.find("\n\n", start)
        if identifier == "A.call_now":
            end = doc.text.find("\n## ", start)
        end = len(doc.text) if end < 0 else end
        contracts.append({"id": identifier, "text": doc.text[start:end],
                          "source": asdict(doc.evidence(start, end, "unnumbered_contract"))})
    for name, identifier in (("outcome_table", "A.outcome_table"), ("bands", "A.band_table")):
        for evidence in policy.facts[name].evidence:
            doc = next(doc for doc in documents if doc.source_id == evidence.source_id)
            contracts.append({"id": identifier, "text": doc.text[evidence.start:evidence.end],
                              "value": policy.facts[name].value, "source": asdict(evidence)})
    return {"kind": "reviewed_practice_semantics", "source_version_admitted": True,
            "complete_for_supplied_practice": True, "general_semantic_compiler": False,
            "publish_allowed": False, "decisions": decisions, "atoms": atoms, "constraints": constraints, "source_contracts": contracts,
            "overlays": [{"id": key, "replaces_or_specializes": value} for key, value in OVERLAYS.items()] if BRIEF_B_HASH in by_hash else [],
            "precedence": [["A.32", "A.30"], ["A.32", "A.41"], ["A.29", "A.30"],
                           ["A.42", "A.45"], ["A.transfer_precedence", "promise_outcome"]],
            "remaining_limits": ["Changed/final briefs need new semantic interpretation",
                "Free-text adapter is bounded English, not a general language/voice model",
                "Ambiguous/malformed tool results are quarantined; missing source error policy is not invented",
                "Freya executable export and live model/platform evaluation remain pending"]}
