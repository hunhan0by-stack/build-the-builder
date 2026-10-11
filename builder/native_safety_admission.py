"""Independent inspection of a native export's declared bank API surface.

This is an additional negative admission check, NOT a provider receipt verifier.
The currently reviewed Freya contract does not give SDK speech callbacks a
native semantic event ID or document atomic native banking persistence. No
caller readiness flag can authorize create_promise or close_call here. This
module performs no network, storage, bank dispatch or workflow modification.
Code-hook semantics are outside this static inspection's scope.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .models import FoundationError


READ_OPERATIONS = frozenset({"verify_identity", "get_account"})


def _bank_operations(openapi):
    try:
        base = openapi["servers"][0]["url"].rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError()
        result = {}
        for path, item in openapi["paths"].items():
            if not isinstance(path, str) or not path.startswith("/") or "post" not in item:
                raise ValueError()
            result[base + path] = path.removeprefix("/")
        if not READ_OPERATIONS.issubset(result.values()):
            raise ValueError()
        return result
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise FoundationError("NATIVE_BANK_CONTRACT_INVALID") from None


def inspect_native_safety(workflow, bank_openapi):
    """Inspect every declared function, including unreachable/global nodes.

    Exact published API URLs are compared with the source bank contract rather
    than trusting a caller's tool name or readiness dictionary. Any api_call
    with an unresolved/mutated URL is an unreviewed bank surface. SDK observer
    observations and a diagnostic judge PASS do not open either native gate.
    """
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict):
        raise FoundationError("NATIVE_WORKFLOW_INVALID")
    operations = _bank_operations(bank_openapi)
    functions = []
    failures = []

    def inspect_tool(value, path):
        if isinstance(value, dict):
            if "function" in value:
                fn = value["function"]
                if not isinstance(fn, dict) or not isinstance(fn.get("handler"), str):
                    failures.append({"path": path, "code": "INVALID_DECLARED_FUNCTION"})
                elif fn["handler"] == "__function__:api_call":
                    try:
                        properties = fn["parameters"]["properties"]
                        url = properties["url"]["default"]
                        method = properties["method"]["default"]
                        operation = operations.get(url) if isinstance(url, str) else None
                        if operation is None or method != "POST":
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        failures.append({"path": path, "code": "UNREVIEWED_API_SURFACE"})
                    else:
                        functions.append({"path": path, "operation": operation})
                        if operation not in READ_OPERATIONS:
                            failures.append({"path": path, "code": "NATIVE_BANK_EFFECT_NOT_ADMITTED",
                                             "operation": operation})
                elif fn["handler"] not in {"__function__:code_exec", "__function__:end_call"}:
                    failures.append({"path": path, "code": "UNREVIEWED_NATIVE_HANDLER"})
            else:
                failures.append({"path": path, "code": "INVALID_DECLARED_FUNCTION"})
        else:
            failures.append({"path": path, "code": "INVALID_DECLARED_FUNCTION"})

    # These are the documented executable function slots. Do not treat arbitrary
    # strings, tool parameter bodies, readiness metadata or examples as tools.
    for group in ("nodes", "global_nodes"):
        nodes = workflow.get(group, {})
        if not isinstance(nodes, dict):
            failures.append({"path": "/" + group, "code": "INVALID_NODE_CONTAINER"})
            continue
        for name, node in nodes.items():
            path = "/" + group + "/" + str(name)
            if not isinstance(node, dict):
                failures.append({"path": path, "code": "INVALID_NODE"})
                continue
            for placement in ("tools", "pre_actions"):
                tools = node.get(placement, [])
                if not isinstance(tools, list):
                    failures.append({"path": path + "/" + placement, "code": "INVALID_TOOL_CONTAINER"})
                    continue
                for index, item in enumerate(tools):
                    inspect_tool(item, path + "/" + placement + "/" + str(index))
            if "terminal" in node:
                inspect_tool(node["terminal"], path + "/terminal")
    return {
        "schema_version": "native-safety-inspection-1",
        "declared_api_surface_closed": not failures,
        "declared_bank_functions": functions,
        "violations": failures,
        "native_bank_writes_admitted": False,
        "native_close_call_admitted": False,
        "native_speech_completion_verified": False,
        "native_restart_safety_verified": False,
        "production_ready": False,
        "inspection_scope": "declared API surface only; code-hook semantics and provider origin not verified",
        "speech_status": "PLATFORM CLARIFICATION REQUIRED",
        "bank_restart_status": "PLATFORM CLARIFICATION REQUIRED",
        "network_calls": 0,
    }


def assert_bank_write_surface_closed(workflow, bank_openapi):
    result = inspect_native_safety(workflow, bank_openapi)
    if not result["declared_api_surface_closed"]:
        raise FoundationError("NATIVE_UNSAFE_DECLARED_API_SURFACE")
    return result
