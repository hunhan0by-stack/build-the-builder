"""Reviewable design output. This is deliberately not an executable Freya agent."""
from __future__ import annotations

from dataclasses import asdict

from .models import FoundationError, Policy


def workflow_plan(policy: Policy) -> dict:
    specs = [
        ("opening", [], ["person_check"], "Bank/digital-assistant/recording disclosure; no debt disclosure."),
        ("person_check", [], ["identity", "third_party", "finalize", "transfer"], "Resolve who is speaking before security questions."),
        ("third_party", ["schedule_callback"], ["finalize"], "No account disclosure; validate callback against call now."),
        ("identity", ["verify_identity"], ["account", "finalize", "transfer"], "Source-derived factor order and wrong-answer budget."),
        ("account", ["get_account"], ["disclose"], "Both factors verified; validate the complete product snapshot."),
        ("disclose", [], ["negotiate", "transfer", "finalize"], "State products in API order before negotiating."),
        ("negotiate", [], ["validate_proposal", "transfer", "finalize"], "Handle intent, exceptions and per-product persuasion budgets."),
        ("validate_proposal", [], ["readback", "negotiate"], "Check amount, product ID, calendar and per-product deadline."),
        ("readback", [], ["write_promise", "validate_proposal", "negotiate", "transfer"], "Explicit yes to the current product/amount/date bundle."),
        ("write_promise", ["create_promise"], ["result"], "Use confirmed arguments; bounded explicit retry; no duplicate writes."),
        ("result", [], ["negotiate", "transfer", "finalize"], "Announce a recorded promise only on confirmed business success."),
        ("transfer", ["transfer_to_agent"], ["finalize"], "Invalidate unwritten consent, preserve recorded promises, discard deferred notes."),
        ("finalize", ["add_note", "close_call"], ["end"], "Goodbye first; deferred notes only when enabled and no transfer; close once, last."),
        ("end", [], [], "Terminal: no further bank operations."),
    ]
    nodes = [{"id": name, "bank_operations": tools, "next": targets, "purpose": purpose,
              "status": "design_pending_semantic_compilation"} for name, tools, targets, purpose in specs]
    return {"kind": "abstract_workflow_plan", "schema_version": "foundation-plan-1",
            "is_freya_export": False, "publish_allowed": False, "initial": "opening", "nodes": nodes,
            "policy_schema": policy.schema_version,
            "guards_implemented_locally": ["identity_order", "identity_wrong_budget", "account_gate",
                "source_calendar", "product_id_binding", "amount_interval", "consent_digest",
                "bounded_promise_retry", "immutable_recorded_promise", "deferred_notes",
                "transfer_precedence", "close_once_last"],
            "pending": ["Semantic compilation of every rule, subrule and exception",
                "General document authority, scope, contradictions and final-brief overlays",
                "Conversation wording, intent recognition, interruptions and persuasion state",
                "Freya node routing, code/pre-actions, response mapping and terminal export",
                "Independent official-case conversation evaluation and live platform validation"]}


def validate_graph(plan: dict) -> dict:
    nodes = {node["id"]: node for node in plan["nodes"]}
    if len(nodes) != len(plan["nodes"]) or plan["initial"] not in nodes:
        raise FoundationError("Plan contains duplicate nodes or a missing initial node")
    for node in nodes.values():
        if any(target not in nodes for target in node["next"]):
            raise FoundationError("Plan transition targets a missing node")
        if "close_call" in node["bank_operations"] and node["id"] != "finalize":
            raise FoundationError("Bank close is exposed outside finalization")
    reached, pending = set(), [plan["initial"]]
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(nodes[name]["next"])
    if reached != set(nodes) or nodes["end"]["next"] or nodes["end"]["bank_operations"]:
        raise FoundationError("Plan has unreachable nodes or a nonterminal end")
    return {"structural_graph_valid": True, "reachable_nodes": len(reached),
            "conversation_behavior_validated": False, "platform_validated": False}


def coverage(policy: Policy) -> dict:
    return {"kind": "structural_and_parameter_coverage", "full_policy_coverage": False,
            "requirement_blocks": len(policy.requirements), "fully_compiled_requirements": 0,
            "parameter_facts": len(policy.facts),
            "requirements": [{"rule_id": rule.rule_id, "source": asdict(rule.evidence),
                              "status": rule.semantic_status} for rule in policy.requirements],
            "parameters": {name: {"recognizer": fact.recognizer,
                "source": [asdict(e) for e in fact.evidence],
                "status": "recognized_parameter_requires_semantic_review"} for name, fact in policy.facts.items()},
            "note": "A recognized number/table is not proof that the surrounding rule and its exceptions are implemented."}
