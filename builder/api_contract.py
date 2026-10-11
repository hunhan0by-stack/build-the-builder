from __future__ import annotations

import copy
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from .ingest import read_source
from .models import FoundationError, strict_json

TOOL_NAMES = frozenset({"verify_identity", "get_account", "create_promise", "add_note",
                        "schedule_callback", "transfer_to_agent", "close_call"})


def resolve_schema(schema: dict, document: dict, chain: tuple[str, ...] = ()) -> dict:
    if not isinstance(schema, dict):
        raise FoundationError("Boolean or non-object schemas require an explicit adapter")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise FoundationError("Remote schema references are disabled in offline mode")
        if reference in chain:
            raise FoundationError("Recursive schemas require an explicit adapter")
        target = document
        try:
            for token in reference[2:].split("/"):
                target = target[token.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise FoundationError(f"Unresolved schema reference: {reference}") from exc
        if set(schema) - {"$ref", "description", "title"}:
            raise FoundationError("Semantic $ref siblings require an explicit adapter")
        return resolve_schema(target, document, chain + (reference,))
    result = copy.deepcopy(schema)
    for keyword in ("properties", "$defs"):
        if keyword in result:
            result[keyword] = {name: resolve_schema(value, document, chain)
                               for name, value in result[keyword].items()}
    if "items" in result:
        result["items"] = resolve_schema(result["items"], document, chain)
    if isinstance(result.get("additionalProperties"), dict):
        result["additionalProperties"] = resolve_schema(result["additionalProperties"], document, chain)
    for keyword in ("allOf", "oneOf", "anyOf"):
        if keyword in result:
            result[keyword] = [resolve_schema(value, document, chain) for value in result[keyword]]
    supported = {"type", "properties", "$defs", "items", "additionalProperties", "required",
                 "allOf", "oneOf", "anyOf", "enum", "const", "nullable", "minItems", "maxItems",
                 "uniqueItems", "minLength", "maxLength", "pattern", "format", "minimum", "maximum",
                 "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minProperties", "maxProperties",
                 "description", "title", "default", "example", "examples", "deprecated", "$schema", "$id"}
    unsupported = set(result) - supported
    if unsupported:
        raise FoundationError(f"Schema features need support: {sorted(unsupported)}")
    if result.get("format") not in {None, "date", "date-time"}:
        raise FoundationError(f"Schema format needs an explicit adapter: {result['format']}")
    if any(type(result.get(key)) is bool for key in ("exclusiveMinimum", "exclusiveMaximum")):
        raise FoundationError("OpenAPI boolean exclusive bounds need an explicit adapter")
    if "multipleOf" in result and (type(result["multipleOf"]) not in (int, float) or result["multipleOf"] <= 0):
        raise FoundationError("multipleOf must be positive")
    if "allOf" in result and any("properties" in part for part in result["allOf"]):
        raise FoundationError("Composed object schemas need an explicit field-merging adapter")
    return result


def validate_value(value: Any, schema: dict, path: str = "$", strict_fields: bool = False) -> None:
    for candidate in schema.get("allOf", []):
        validate_value(value, candidate, path, strict_fields)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            successes = 0
            for candidate in schema[keyword]:
                try:
                    validate_value(value, candidate, path, strict_fields)
                    successes += 1
                except FoundationError:
                    pass
            if not successes or (keyword == "oneOf" and successes != 1):
                raise FoundationError(f"{path}: {keyword} mismatch")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind else []
    if schema.get("nullable") and value is None:
        return
    checks = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str), "boolean": lambda: type(value) is bool,
              "integer": lambda: type(value) is int,
              "number": lambda: type(value) in (int, float) and math.isfinite(value),
              "null": lambda: value is None}
    if kinds and not any(checks.get(k, lambda: False)() for k in kinds):
        raise FoundationError(f"{path}: expected {kind}")
    if "enum" in schema and not any(type(value) is type(x) and value == x for x in schema["enum"]):
        raise FoundationError(f"{path}: value not in enum")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise FoundationError(f"{path}: const mismatch")
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", math.inf):
            raise FoundationError(f"{path}: object field count outside contract")
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise FoundationError(f"{path}: missing fields {sorted(missing)}")
        for key, item in value.items():
            if key in properties:
                validate_value(item, properties[key], f"{path}.{key}", strict_fields)
            elif strict_fields or schema.get("additionalProperties") is False:
                raise FoundationError(f"{path}: unknown field {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_value(item, schema["additionalProperties"], f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise FoundationError(f"{path}: array length outside contract")
        if schema.get("uniqueItems") and len({repr(x) for x in value}) != len(value):
            raise FoundationError(f"{path}: duplicate array items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_value(item, schema["items"], f"{path}[{index}]", strict_fields)
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise FoundationError(f"{path}: string length outside contract")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise FoundationError(f"{path}: pattern mismatch")
        if schema.get("format") == "date":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError as exc:
                raise FoundationError(f"{path}: invalid ISO calendar date") from exc
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
            except ValueError as exc:
                raise FoundationError(f"{path}: invalid timezone-aware datetime") from exc
    elif type(value) in (int, float):
        if not math.isfinite(value):
            raise FoundationError(f"{path}: non-finite number")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise FoundationError(f"{path}: number outside contract")
        for keyword, operator in (("exclusiveMinimum", lambda a, b: a <= b), ("exclusiveMaximum", lambda a, b: a >= b)):
            boundary = schema.get(keyword)
            if type(boundary) in (int, float) and operator(value, boundary):
                raise FoundationError(f"{path}: {keyword} violation")
        if "multipleOf" in schema and not math.isclose(value / schema["multipleOf"], round(value / schema["multipleOf"])):
            raise FoundationError(f"{path}: multipleOf violation")


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    url: str
    summary: str
    request: dict
    response: dict
    response_descriptions: dict


class BankContract:
    def __init__(self, document: dict, source_id: str, source_hash: str):
        self.source_id, self.source_hash = source_id, source_hash
        servers = document.get("servers", [])
        if not servers or not isinstance(servers[0].get("url"), str):
            raise FoundationError("API contract needs an explicit server URL")
        self.server = servers[0]["url"].rstrip("/")
        parsed = urlsplit(self.server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FoundationError("API server must be an HTTP URL without embedded credentials")
        self.operations: dict[str, Operation] = {}
        for path, item in document.get("paths", {}).items():
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                name = operation.get("operationId", path.rsplit("/", 1)[-1])
                if name not in TOOL_NAMES:
                    raise FoundationError(f"Operation not allowed by the hackathon contract: {name}")
                if name in self.operations:
                    raise FoundationError(f"Duplicate operation: {name}")
                if item.get("parameters") or operation.get("parameters") or "{" in path or not path.startswith("/") or "?" in path or "#" in path:
                    raise FoundationError("Path/query/header parameters need explicit adapter support")
                try:
                    request = operation["requestBody"]["content"]["application/json"]["schema"]
                    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
                except KeyError as exc:
                    raise FoundationError(f"Explicit JSON request/200 response schemas required for {name}") from exc
                self.operations[name] = Operation(name, method.upper(), path, self.server + path,
                    operation.get("summary", ""), resolve_schema(request, document), resolve_schema(response, document),
                    {status: value.get("description", "") for status, value in operation["responses"].items()})
        if set(self.operations) != TOOL_NAMES:
            raise FoundationError(f"Missing bank operations: {sorted(TOOL_NAMES - self.operations.keys())}")

    @classmethod
    def from_path(cls, path):
        source = read_source(path)
        return cls(strict_json(source.text), source.source_id, source.sha256)

    def validate_request(self, name: str, payload: dict) -> None:
        if name not in self.operations:
            raise FoundationError("Unknown bank operation")
        validate_value(payload, self.operations[name].request, strict_fields=True)

    def business_result(self, name: str, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise FoundationError("Business response must be an object")
        validate_value(payload, self.operations[name].response)
        if type(payload.get("ok")) is not bool:
            return "unknown"
        return "succeeded" if payload["ok"] else "failed"

    def serializable(self):
        return {"source_id": self.source_id, "source_sha256": self.source_hash,
                "server": self.server, "network_executed": False,
                "operations": {name: asdict(value) for name, value in self.operations.items()}}

    def freya_templates(self):
        templates = {}
        for name, operation in self.operations.items():
            body = copy.deepcopy(operation.request)
            for field_name, spec in body.get("properties", {}).items():
                variable = "customer_id" if field_name == "customer_id" else f"input_{name}_{field_name}"
                spec["default"] = "{{" + variable + "}}"
            templates[name] = {
                "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
                "function": {"name": name, "handler": "__function__:api_call",
                    "description": operation.summary,
                    "parameters": {"type": "object", "properties": {
                        "url": {"type": "string", "default": operation.url},
                        "method": {"type": "string", "default": operation.method},
                        "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                        "body": body}}}}
        return {"kind": "unattached_tool_templates", "publish_allowed": False,
                "source_sha256": self.source_hash, "source_id": self.source_id,
                "note": "Review only. Tools still require policy guards, node placement and platform validation.", "tools": templates}
