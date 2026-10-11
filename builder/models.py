from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class FoundationError(ValueError):
    """A source, contract or state cannot be used safely."""


@dataclass(frozen=True)
class Evidence:
    source_id: str
    start: int
    end: int
    line_start: int
    line_end: int
    locator: str = ""


@dataclass
class Document:
    source_id: str
    path: str
    sha256: str
    byte_count: int
    kind: str
    text: str
    status: str = "processed"
    limitations: list[str] = field(default_factory=list)

    def evidence(self, start: int, end: int, locator: str = "") -> Evidence:
        if not 0 <= start < end <= len(self.text):
            raise FoundationError("Evidence span is empty or outside its source")
        return Evidence(self.source_id, start, end,
                        self.text.count("\n", 0, start) + 1,
                        self.text.count("\n", 0, end - 1) + 1, locator)

    def inventory(self) -> dict[str, Any]:
        record = asdict(self)
        record.pop("text")
        record["characters"] = len(self.text)
        record["span_coordinate_system"] = "decoded/extracted Unicode text, zero-based half-open"
        return record


@dataclass(frozen=True)
class Requirement:
    rule_id: str
    text: str
    section: str
    evidence: Evidence
    classification: str
    semantic_status: str = "requires_semantic_review"


@dataclass(frozen=True)
class Fact:
    value: Any
    evidence: tuple[Evidence, ...]
    recognizer: str


@dataclass
class Policy:
    requirements: list[Requirement]
    facts: dict[str, Fact]
    tables: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    input_order: list[str]
    schema_version: str = "foundation-policy-1"

    def serializable(self) -> dict[str, Any]:
        result = asdict(self)
        result["readiness"] = {
            "local_foundation": not any(gap["code"] != "semantic_compiler_pending" for gap in self.gaps),
            "semantic_completion": False,
            "publish_allowed": False,
            "reason": "Structural requirements and selected facts are extracted; full policy compilation and live platform validation remain pending.",
        }
        return result


def strict_json(text: str) -> Any:
    import json
    import math

    def invalid_constant(value: str) -> None:
        raise FoundationError(f"Non-finite JSON constant: {value}")

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise FoundationError("JSON exponent exceeds the finite-number adapter")
        return result

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise FoundationError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=invalid_constant, parse_float=finite_float, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as exc:
        raise FoundationError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
