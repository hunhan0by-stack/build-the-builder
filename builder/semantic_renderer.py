"""Pure rendering of source-quoted semantic events; never acknowledges speech.

Callers own event sequencing, IDs, validated variable bindings and bank evidence.
This renderer performs no model, API, file I/O or state mutation.
"""
from __future__ import annotations

import copy
import math
import re
import string
import unicodedata
from decimal import Decimal

from .models import FoundationError


REQUIRED_KINDS = frozenset({"opening", "identity_question", "account_summary", "proposal_request",
                            "readback", "promise_success", "goodbye"})
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_FORMATTER = string.Formatter()


def _placeholders(template):
    try:
        parts = list(_FORMATTER.parse(template))
    except ValueError:
        raise FoundationError("Invalid source wording placeholder syntax") from None
    names = []
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if not _NAME.fullmatch(field) or spec or conversion:
            raise FoundationError("Only simple named source wording placeholders are supported")
        names.append(field)
    return tuple(dict.fromkeys(names))


def validate_wording_bundle(bundle, documents, *, required_kinds=REQUIRED_KINDS):
    """Return a defensive copy after exact source/quote/template validation.

    Evidence offsets are zero-based half-open extracted Unicode coordinates.
    Required kinds can only be made stricter, never omit the seven export gates.
    """
    if not isinstance(bundle, dict) or not isinstance(required_kinds, (set, frozenset, tuple, list)):
        raise FoundationError("Source wording bundle and required kinds must be explicit")
    required = set(required_kinds)
    if not REQUIRED_KINDS <= required or required - REQUIRED_KINDS:
        raise FoundationError("Unsupported or weakened source wording requirements")
    if set(bundle) != required:
        raise FoundationError("Missing or unknown mandatory source wording kind")
    by_id = {}
    for document in documents:
        if document.status != "processed" or not document.text.strip():
            raise FoundationError("Wording requires processed nonempty source documents")
        if document.source_id in by_id and document != by_id[document.source_id]:
            raise FoundationError("Conflicting wording source identifier")
        by_id[document.source_id] = document
    for kind, entry in bundle.items():
        if not isinstance(entry, dict) or set(entry) != {"template", "evidence"}:
            raise FoundationError("Wording entry needs only template and exact evidence")
        template, ref = entry["template"], entry["evidence"]
        if not isinstance(template, str) or not template.strip() or len(template) > 16000:
            raise FoundationError("Source wording template is empty or oversized")
        if not isinstance(ref, dict) or set(ref) != {"source_id", "start", "end", "quote"}:
            raise FoundationError("Wording evidence fields are invalid")
        if not isinstance(ref["source_id"], str) or type(ref["start"]) is not int or type(ref["end"]) is not int:
            raise FoundationError("Wording evidence needs a source identifier and integer offsets")
        document = by_id.get(ref["source_id"])
        if document is None or not 0 <= ref["start"] < ref["end"] <= len(document.text):
            raise FoundationError("Wording evidence source/offset is invalid")
        if template != ref["quote"] or document.text[ref["start"]:ref["end"]] != template:
            raise FoundationError("Wording template must exactly equal its cited source quote")
        _placeholders(template)
    return copy.deepcopy(bundle)


def _safe_value(value):
    if isinstance(value, str):
        rendered = value
    elif type(value) is int:
        try:
            rendered = str(value)
        except ValueError:
            raise FoundationError("Wording numeric value is oversized") from None
    elif type(value) is float and math.isfinite(value):
        rendered = str(value)
    elif isinstance(value, Decimal) and value.is_finite():
        rendered = str(value)
    else:
        raise FoundationError("Wording context must contain finite plain scalar values")
    if not rendered or len(rendered) > 512 or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in rendered):
        raise FoundationError("Wording context is empty, oversized or contains hidden/control characters")
    if any(char in rendered for char in "{}<>"):
        raise FoundationError("Wording context may not inject placeholders or markup")
    return rendered


def _confirmed_promise(bank_outcome):
    if not isinstance(bank_outcome, dict) or set(bank_outcome) != {"operation", "result"}:
        raise FoundationError("Promise success wording requires explicit bank result evidence")
    if bank_outcome["operation"] != "create_promise":
        raise FoundationError("Promise success wording requires create_promise business confirmation")
    result = bank_outcome["result"]
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("promise_id"), str) or not result["promise_id"].strip():
        raise FoundationError("Promise success wording is blocked without confirmed recorded promise")


class SourceRenderer:
    def __init__(self, documents, bundle):
        # Documents and bundle remain caller-owned; subsequent mutation cannot replace admitted wording.
        self._bundle = validate_wording_bundle(bundle, documents)

    def render(self, event_id, kind, context, *, bank_outcome=None):
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise FoundationError("Caller-assigned stable event identifier required")
        if not isinstance(kind, str) or kind not in self._bundle:
            raise FoundationError("Unsupported semantic wording event")
        if not isinstance(context, dict) or any(not isinstance(key, str) or not _NAME.fullmatch(key) for key in context):
            raise FoundationError("Wording context requires plain named bindings")
        if kind == "promise_success":
            _confirmed_promise(bank_outcome)
        elif bank_outcome is not None:
            raise FoundationError("Bank confirmation is only accepted for promise success wording")
        template = self._bundle[kind]["template"]
        placeholders = _placeholders(template)
        if set(placeholders) - context.keys():
            raise FoundationError("Source wording has unbound required placeholders")
        bindings = {key: _safe_value(value) for key, value in context.items()}
        try:
            text = template.format_map(bindings)
        except (KeyError, ValueError):
            raise FoundationError("Source wording could not be rendered safely") from None
        if len(text) > 32000:
            raise FoundationError("Rendered source wording exceeds output limit")
        # No 'spoken', speech_ack or goodbye acknowledgement is inferred from rendering.
        return {"event_id": event_id, "kind": kind, "text": text,
                "evidence": copy.deepcopy(self._bundle[kind]["evidence"])}
