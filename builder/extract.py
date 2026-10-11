from __future__ import annotations

import re
from dataclasses import asdict

from .api_contract import BankContract
from .models import Document, Fact, FoundationError, Policy, Requirement

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SMALL_NUMBERS = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))


def integer_word(text: str) -> int:
    text = re.sub(r"(?<=\d)(st|nd|rd|th)$", "", text.strip().lower())
    if text.isdigit():
        return int(text)
    if text in SMALL_NUMBERS:
        return SMALL_NUMBERS[text]
    match = re.fullmatch(r"(twenty|thirty)[ -](one|two|three|four|five|six|seven|eight|nine)", text)
    if match:
        return (20 if match[1] == "twenty" else 30) + SMALL_NUMBERS[match[2]]
    raise FoundationError(f"Number phrase needs semantic interpretation: {text}")


def clean_cell(text: str) -> str:
    return text.strip().replace("`", "").replace("**", "")


def markdown_tables(document: Document) -> list[dict]:
    lines = document.text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    tables = []
    index = 0
    while index + 1 < len(lines):
        if not lines[index].lstrip().startswith("|") or not re.fullmatch(r"[\s|:\-]+", lines[index + 1].strip()):
            index += 1
            continue
        start = index
        headers = [clean_cell(cell) for cell in lines[index].strip().strip("|").split("|")]
        index += 2
        rows = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            values = [clean_cell(cell) for cell in lines[index].strip().strip("|").split("|")]
            if len(values) != len(headers):
                raise FoundationError(f"Malformed table at {document.path}:{index + 1}")
            rows.append({"values": dict(zip(headers, values)),
                         "evidence": asdict(document.evidence(offsets[index], offsets[index] + len(lines[index]), "table_row"))})
            index += 1
        tables.append({"headers": headers, "rows": rows,
                       "evidence": asdict(document.evidence(offsets[start], offsets[index - 1] + len(lines[index - 1]), "markdown_table"))})
    return tables


POLICY_SECTION_HEADINGS = frozenset({"Collection specification", "Policy", "Policies", "Memo", "Amendments"})


def explicit_policy_sections(document: Document) -> tuple[dict[int, str | None], list[dict]]:
    """Scope line numbers by original, explicit Policy IDs at real headings.

    This annotates the one original Document; it never splits or re-hashes its
    text. A second identifier needs a same-level or outer structural boundary.
    An unnamed section does not reset numbering, and order grants no authority.
    """
    scopes, definitions, identifiers, numbers = {}, [], set(), set()
    offset, current, heading, scope_heading, last_id, saw_unscoped_rule = 0, None, None, None, -1, False
    for raw in document.text.splitlines(keepends=True):
        stripped = raw.strip()
        start = offset + len(raw) - len(raw.lstrip())
        line_offset = offset
        offset += len(raw)
        found_heading = re.fullmatch(r'(#{1,6})\s+(.+)', stripped)
        if found_heading:
            heading = (len(found_heading[1]), found_heading[2], start)
        identifier = re.fullmatch(r'Policy ID: ([A-Za-z][A-Za-z0-9_.-]{0,79})', stripped)
        if identifier:
            if identifier[1] in identifiers:
                raise FoundationError('BRIEF_REQUIREMENTS_AMBIGUOUS_POLICY_ID')
            if current is None and saw_unscoped_rule:
                raise FoundationError('BRIEF_REQUIREMENTS_POLICY_ID_AFTER_UNSCOPED_RULES')
            if current is not None and (heading is None or heading[2] <= last_id
                    or heading[1] not in POLICY_SECTION_HEADINGS
                    or (scope_heading is not None and heading[0] > scope_heading[0])):
                raise FoundationError('BRIEF_REQUIREMENTS_POLICY_ID_NEEDS_SECTION_BOUNDARY')
            current = identifier[1]
            identifiers.add(current)
            scope_heading, last_id = heading, start
            definitions.append({'policy_id': current, 'evidence': {
                'source_id': document.source_id, 'start': start, 'end': start + len(stripped), 'quote': stripped}})
        scopes[line_offset] = current
        number = re.match(r'([0-9]+)\.\s+', stripped)
        if number:
            key = (current, number[1])
            if key in numbers:
                raise FoundationError('BRIEF_REQUIREMENTS_DUPLICATE_RULE_NUMBER')
            numbers.add(key)
            if current is None:
                saw_unscoped_rule = True
    return scopes, definitions


def extract_requirements(document: Document) -> list[Requirement]:
    numbered = list(re.finditer(r"(?m)^(\d+)\.\s+", document.text))
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+)$", document.text))
    requirements = []
    scopes, _ = explicit_policy_sections(document)
    repeated = len({match[1] for match in numbered}) != len(numbered)
    for index, match in enumerate(numbered):
        end = numbered[index + 1].start() if index + 1 < len(numbered) else len(document.text)
        next_heading = next((h for h in headings if match.end() < h.start() < end), None)
        if next_heading:
            end = next_heading.start()
        while end > match.end() and document.text[end - 1].isspace():
            end -= 1
        previous_headings = [h for h in headings if h.start() < match.start()]
        section = previous_headings[-1][1] if previous_headings else ""
        text = document.text[match.end():end]
        kind = "numbered_requirement_block"
        # Existing unique-number documents keep their exact historic IDs.
        # Repeated numbers require distinct explicit scopes, not renumbering.
        scoped_id = f":p{scopes[match.start()]}" if repeated else ""
        requirements.append(Requirement(f"{document.source_id}{scoped_id}:r{match[1]}", text, section,
                                        document.evidence(match.start(), end, "numbered_rule_" + match[1]), kind))
    if not numbered and document.text.strip():
        # Raw materials stay available as grounded blocks, never silently interpreted as complete policy.
        for index, match in enumerate(re.finditer(r"(?ms)\S.*?(?=\n\s*\n|\Z)", document.text), 1):
            requirements.append(Requirement(f"{document.source_id}:b{index}", match[0], "raw_material",
                                            document.evidence(match.start(), match.end(), "raw_block"), "raw_evidence_block"))
    return requirements


def extract_policy(documents: list[Document], contract: BankContract) -> Policy:
    unique = list({doc.source_id: doc for doc in reversed(documents)}.values())[::-1]
    requirements = [rule for doc in unique for rule in extract_requirements(doc)]
    tables = [table for doc in unique for table in markdown_tables(doc)]
    facts: dict[str, Fact] = {}
    gaps: list[dict] = []
    blocked_facts: set[str] = set()

    def put(name, value, document, start, end, recognizer):
        evidence = document.evidence(start, end, recognizer)
        candidate = Fact(value, (evidence,), recognizer)
        if name in blocked_facts:
            return
        if name in facts and facts[name].value != value:
            gaps.append({"code": "conflicting_fact", "severity": "critical", "fact": name,
                         "candidates": [asdict(facts[name]), asdict(candidate)],
                         "resolution": "Explicit authority/scope reasoning required; input order is not authority."})
            blocked_facts.add(name)
            del facts[name]
        elif name in facts:
            facts[name] = Fact(value, facts[name].evidence + (evidence,), recognizer)
        else:
            facts[name] = candidate

    product_schema = contract.operations["get_account"].response.get("properties", {}).get("products", {}).get("items", {})
    product_properties = product_schema.get("properties", {})
    product_types = product_properties.get("type", {}).get("enum", [])
    for doc in unique:
        if doc.status != "processed":
            gaps.append({"code": doc.status, "severity": "critical", "source_id": doc.source_id})
            continue
        for match in re.finditer(r"Business days are ([^.]+)\.", doc.text, re.I):
            sentence = match[1]
            weekday_match = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+to\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)", sentence, re.I)
            if weekday_match:
                first, last = (WEEKDAYS.index(v.lower()) for v in weekday_match.groups())
                weekdays = []
                current = first
                while True:
                    weekdays.append(current)
                    if current == last:
                        break
                    current = (current + 1) % 7
                put("business_weekdays", weekdays, doc, match.start(), match.end(), "weekday_range_v1")
                holidays = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", sentence)
                if "holiday" in sentence.lower():
                    put("holidays", holidays, doc, match.start(), match.end(), "explicit_holiday_dates_v1")
        for match in re.finditer(r"\b([A-Za-z]+|\d+) wrong answers in total", doc.text, re.I):
            put("identity_max_wrong", integer_word(match[1]), doc, match.start(), match.end(), "wrong_answer_limit_v1")
        for match in re.finditer(r"first the date of birth, then the last four digits of the phone number on file", doc.text, re.I):
            fields = contract.operations["verify_identity"].request["properties"]["field"].get("enum", [])
            if {"date_of_birth", "phone_last4"} <= set(fields):
                put("identity_order", ["date_of_birth", "phone_last4"], doc, match.start(), match.end(), "practice_identity_order_v1")
        for rule in [r for r in requirements if r.evidence.source_id == doc.source_id]:
            if "create_promise" in rule.text:
                match = re.search(r"try (once|twice|\d+|[a-z]+) (?:more|again)", rule.text, re.I)
                if match:
                    extra = 1 if match[1].lower() == "once" else 2 if match[1].lower() == "twice" else integer_word(match[1])
                    put("promise_max_attempts", 1 + extra, doc, rule.evidence.start, rule.evidence.end, "explicit_extra_retry_v1")
            if "floor" in rule.text.lower():
                mapping = {}
                for match in re.finditer(r"`([^`]+)`\s+for\s+(.+?)(?=\s+and\s+`|\.|$)", rule.text):
                    field = match[1]
                    if field not in product_properties:
                        continue
                    for product_type in product_types:
                        if product_type.replace("_", " ").lower() in match[2].lower():
                            mapping[product_type] = field
                if mapping:
                    put("floor_fields", mapping, doc, rule.evidence.start, rule.evidence.end, "explicit_floor_field_mapping_v1")
        for match in re.finditer(r"from (\d{2}:\d{2}) to (\d{2}:\d{2}), both included", doc.text, re.I):
            put("callback_hours", {"start": match[1], "end": match[2], "inclusive": True}, doc, match.start(), match.end(), "inclusive_time_window_v1")
        if re.search(r"Every rule of Brief A applies", doc.text, re.I):
            if not any(re.search(r"^#\s*Brief A\b", other.text, re.M) for other in unique):
                gaps.append({"code": "missing_inherited_brief", "severity": "critical", "source_id": doc.source_id})

    for table in tables:
        keys = set(table["headers"])
        doc = next(doc for doc in unique if doc.source_id == table["evidence"]["source_id"])
        if {"days_late", "Band", "Deadline", "Persuasion attempts"} <= keys:
            bands = []
            for row in table["rows"]:
                values = row["values"]
                interval = re.fullmatch(r"(\d+)\s+to\s+(\d+)", values["days_late"])
                deadline = re.search(r"(\d+(?:st|nd|rd|th)?) business day after today", values["Deadline"])
                if not interval or not deadline:
                    gaps.append({"code": "unsupported_band_semantics", "severity": "critical", "evidence": row["evidence"]})
                    continue
                bands.append({"name": values["Band"], "min_days_late": int(interval[1]), "max_days_late": int(interval[2]),
                              "deadline_business_days": integer_word(deadline[1]), "persuasion_attempts": integer_word(values["Persuasion attempts"])})
            if len(bands) == len(table["rows"]):
                put("bands", bands, doc, table["evidence"]["start"], table["evidence"]["end"], "band_table_v1")
        if {"Outcome", "Reason", "When"} <= keys:
            rows = [{"outcome": row["values"]["Outcome"],
                     "reasons": [] if row["values"]["Reason"].lower() == "none" else [s.strip() for s in row["values"]["Reason"].split(",")],
                     "when": row["values"]["When"]} for row in table["rows"]]
            allowed = contract.operations["close_call"].request.get("properties", {}).get("outcome", {}).get("enum", [])
            if set(row["outcome"] for row in rows) != set(allowed):
                gaps.append({"code": "outcome_table_api_mismatch", "severity": "critical", "source_id": doc.source_id})
            put("outcome_table", rows, doc, table["evidence"]["start"], table["evidence"]["end"], "outcome_table_v1")

    # Explicit practice A/B overlay, not a general authority resolver. Keep it labelled.
    base = next((doc for doc in unique if re.search(r"^#\s*Brief A\b", doc.text, re.M)), None)
    overlay = next((doc for doc in unique if re.search(r"^#\s*Brief B\b", doc.text, re.M)
                    and "Every rule of Brief A applies unless a rule below replaces it." in doc.text), None)
    if base:
        if overlay:
            match = re.search(r"`get_account` returns ([a-z]+|\d+) to ([a-z]+|\d+) products", overlay.text, re.I)
            if match:
                put("product_limit", integer_word(match[2]), overlay, match.start(), match.end(), "practice_b_product_limit_v1")
            rule = next((r for r in requirements if r.evidence.source_id == overlay.source_id
                         and "For each product without a promise call `add_note`" in r.text), None)
            if rule:
                put("notes_enabled", True, overlay, rule.evidence.start, rule.evidence.end, "practice_b_explicit_notes_overlay_v1")
        else:
            match = re.search(r"early-stage collections, (one|\d+) product", base.text, re.I)
            if match:
                put("product_limit", integer_word(match[1]), base, match.start(), match.end(), "practice_a_product_limit_v1")
            match = re.search(r"Brief A never uses `add_note`\.", base.text)
            if match:
                put("notes_enabled", False, base, match.start(), match.end(), "practice_a_explicit_no_notes_v1")
    for name in ("business_weekdays", "holidays", "bands", "floor_fields", "identity_max_wrong", "identity_order", "promise_max_attempts", "outcome_table", "notes_enabled", "product_limit"):
        if name not in facts:
            gaps.append({"code": "missing_or_unresolved_fact", "severity": "critical", "fact": name})
    gaps.append({"code": "semantic_compiler_pending", "severity": "critical",
                 "detail": "General semantic compilation remains pending. A separate reviewed practice-version binding may exist; parameter extraction alone does not establish full semantic coverage.",
                 "requirement_ids": [r.rule_id for r in requirements]})
    return Policy(requirements, facts, tables, gaps, [doc.source_id for doc in documents])
