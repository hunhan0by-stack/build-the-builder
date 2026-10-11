from __future__ import annotations

from dataclasses import asdict

from .calendar import BusinessCalendar
from .models import FoundationError
from .plan import validate_graph


def validate_foundation(documents, policy, contract, plan):
    sources = {document.source_id: document for document in documents}
    issues = []

    def evidence_check(record):
        doc = sources.get(record["source_id"])
        if not doc:
            raise FoundationError("Evidence refers to an unknown source")
        expected = asdict(doc.evidence(record["start"], record["end"], record.get("locator", "")))
        if expected != record:
            raise FoundationError("Evidence coordinates do not match the extracted source")

    for rule in policy.requirements:
        evidence_check(asdict(rule.evidence))
        doc = sources[rule.evidence.source_id]
        if rule.text not in doc.text[rule.evidence.start:rule.evidence.end]:
            raise FoundationError("Requirement text is not contained in its cited source span")
    for fact in policy.facts.values():
        if not fact.evidence:
            raise FoundationError("Parameter has no source evidence")
        for evidence in fact.evidence:
            evidence_check(asdict(evidence))
    for table in policy.tables:
        evidence_check(table["evidence"])
        for row in table["rows"]:
            evidence_check(row["evidence"])
    try:
        BusinessCalendar.from_policy(policy)
    except FoundationError as exc:
        issues.append({"code": "calendar_unresolved", "detail": str(exc)})
    bands = policy.facts.get("bands")
    if bands:
        previous = []
        for band in bands.value:
            if not (0 <= band["min_days_late"] <= band["max_days_late"] and
                    1 <= band["deadline_business_days"] <= 3660 and band["persuasion_attempts"] >= 0):
                issues.append({"code": "invalid_band", "band": band})
            if any(band["min_days_late"] <= other["max_days_late"] and other["min_days_late"] <= band["max_days_late"] for other in previous):
                issues.append({"code": "overlapping_bands"})
            previous.append(band)
    floor = policy.facts.get("floor_fields")
    properties = contract.operations["get_account"].response["properties"]["products"]["items"]["properties"]
    if floor and (set(floor.value) != set(properties["type"].get("enum", [])) or
                  any(properties.get(field, {}).get("type") != "number" for field in floor.value.values())):
        issues.append({"code": "incomplete_or_untyped_floor_binding"})
    for name in ("identity_max_wrong", "promise_max_attempts"):
        fact = policy.facts.get(name)
        if fact and (type(fact.value) is not int or not 1 <= fact.value <= 100):
            issues.append({"code": "invalid_attempt_budget", "fact": name})
    graph = validate_graph(plan)
    unresolved = [gap for gap in policy.gaps if gap["code"] != "semantic_compiler_pending"]
    return {"evidence_spans_valid": True, "api_contract_parsed": True,
            "local_structure_valid": not issues and not unresolved,
            "issues": issues, "policy_gaps": policy.gaps, "graph": graph,
            "semantic_completion": False, "official_cases_evaluated": False,
            "network_calls": 0, "model_calls": 0, "api_key_read": False,
            "publish_allowed": False}
