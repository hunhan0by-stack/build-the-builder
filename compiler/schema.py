"""Finite decision IR vocabulary. Unknown fields and semantics are rejected."""
from copy import deepcopy


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def array(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


TEXT = {"type": "string", "minLength": 1, "maxLength": 16000}
IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"}
SCALAR = {"type": ["string", "number", "boolean", "null"]}
NUMBER = {"type": "number"}
INTEGER = {"type": "integer"}
BAND = obj({"name": TEXT, "min_days_late": INTEGER, "max_days_late": INTEGER,
            "deadline_business_days": INTEGER, "persuasion_attempts": INTEGER})
OUTCOME = obj({"outcome": TEXT, "reasons": array(TEXT), "when": TEXT})
VALUE = {"anyOf": [SCALAR, array(SCALAR), array(BAND, 1), array(OUTCOME, 1),
                    {"type": "object", "additionalProperties": TEXT, "minProperties": 1},
                    obj({"start": TEXT, "end": TEXT, "inclusive": {"type": "boolean"}})]}
EVIDENCE = obj({"source_id": TEXT, "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 1}, "quote": TEXT})
PREDICATE = obj({"fact": IDENTIFIER, "operator": {"enum": ["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "present"]},
                 "value": VALUE})
EFFECT = obj({"kind": {"enum": ["require", "forbid", "allow", "set", "limit", "announce"]},
              "target": IDENTIFIER, "value": VALUE})
RULE = obj({"id": IDENTIFIER, "priority": {"type": "integer", "minimum": 0},
            "conditions": array(PREDICATE), "effects": array(EFFECT, 1),
            "exceptions": array(obj({"conditions": array(PREDICATE, 1), "effects": array(EFFECT, 1),
                                     "evidence": array(EVIDENCE, 1)})),
            "overrides": array(IDENTIFIER), "evidence": array(EVIDENCE, 1)})
FACT = obj({"name": IDENTIFIER, "value": VALUE,
            "category": {"enum": ["identity", "calendar", "date", "amount", "retry", "outcome", "conversation", "other"]},
            "evidence": array(EVIDENCE, 1)})
ROLES = ["identity_check", "account_read", "promise_write", "note_write", "callback_write", "transfer", "close"]
BINDING = obj({"role": {"enum": ROLES}, "operation": TEXT,
               "direction": {"enum": ["request", "response"]}, "field_path": TEXT,
               "allowed_values": array(VALUE), "evidence": array(EVIDENCE, 1)})
UNRESOLVED = obj({"id": IDENTIFIER, "reason": TEXT, "evidence": array(EVIDENCE, 1)})
BASE_SCHEMA = obj({"schema_version": {"const": "decision-ir-1"}, "rules": array(RULE, 1),
                   "facts": array(FACT), "api_bindings": array(BINDING, 1),
                   "unresolved": array(UNRESOLVED)})


def output_schema(contract):
    schema = deepcopy(BASE_SCHEMA)
    schema["properties"]["api_bindings"]["items"]["properties"]["operation"] = {
        "type": "string", "enum": sorted(contract.operations)}
    return schema
