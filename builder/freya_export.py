"""Offline Freya-shaped preview, not a published or semantically complete agent.

The pure compiler accepts the foundation's serialized plan/policy/BankContract.
An unresolved plan is boot-gated to a silent terminal before any bank operation.
No credentials, environment, HTTP clients or models are used by this module.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .api_contract import TOOL_NAMES
from .ingest import read_source
from .models import FoundationError, strict_json


FORMAT_VERSION = "freya-offline-preview-1"
CODE_EXEC_LIMIT_BYTES = 131072
CODE_EXEC_SAFE_BYTES = 114688
CODE_EXEC_VARIABLE_RESERVE_BYTES = 16384


def code_exec_request_bytes(code, variables):
    """Exact local code+vars JSON envelope, including escaping and whitespace.

    ASCII escaping is conservative for UTF-8 values. Platform-private framing is
    not observable locally; the separate 16 KiB safety margin covers that risk.
    Never confuse raw source bytes with the serialized sandbox input size.
    """
    if not isinstance(code, str) or not isinstance(variables, dict):
        raise FoundationError("Code budget needs explicit code and a variable object")
    return json.dumps({"code": code, "vars": variables}, ensure_ascii=True, allow_nan=False).encode("utf-8")


def measure_code_exec_budget(workflow, variables=None, *, reserve_variable_bytes=CODE_EXEC_VARIABLE_RESERVE_BYTES):
    if type(reserve_variable_bytes) is not int or reserve_variable_bytes < 0:
        raise FoundationError("Invalid code-exec variable reserve")
    variables = {} if variables is None else variables
    sizes = {}
    for name, node in workflow["nodes"].items():
        for index, tool in enumerate(node.get("pre_actions", []) + node.get("tools", [])):
            fn = tool["function"]
            if fn["handler"] == "__function__:code_exec":
                code = fn["parameters"]["properties"]["code"]["default"]
                sizes[name + ":" + str(index)] = len(code_exec_request_bytes(code, variables))
    peak = max(sizes.values(), default=0)
    return {"kind": "serialized_code_vars_budget", "serialized_request_bytes_by_node": sizes,
            "peak_serialized_request_bytes": peak, "reserve_variable_bytes": reserve_variable_bytes,
            "admission_upper_bound_bytes": peak + reserve_variable_bytes,
            "safe_limit_bytes": CODE_EXEC_SAFE_BYTES, "platform_limit_bytes": CODE_EXEC_LIMIT_BYTES,
            "platform_margin_bytes": CODE_EXEC_LIMIT_BYTES - CODE_EXEC_SAFE_BYTES,
            "passed": peak + reserve_variable_bytes <= CODE_EXEC_SAFE_BYTES,
            "serialization": "json.dumps({code, vars}, ensure_ascii=True, allow_nan=False), UTF-8",
            "platform_private_framing_measured": False}
CORE_INTERFACE = {
    "accepted": "serialized foundation-plan-1, foundation-policy-1, BankContract.serializable(), sources.json",
    "required_for_executable_compilation": [
        "Per-rule and unnumbered-rule node/guard binding with source evidence",
        "Per-edge deterministic predicate or reviewed intent condition",
        "Per-node prompts and extraction schema, with explicit slot reset/freshness semantics",
        "Outcome selection, refusal/interrupt/transfer precedence and error fallback decisions",
        "Validated proposal bundle and current readback consent revision",
        "Identity wrong/refusal counters and bounded response-confirmed retry decisions",
    ],
    "readiness": "Uncompiled plans stay blocked. Reviewed practice candidates may permit a controlled platform preflight; final submission readiness is separate.",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fact(policy, name):
    try:
        fact = policy["facts"][name]
        if not fact["evidence"]:
            raise KeyError(name)
        return copy.deepcopy(fact["value"])
    except (KeyError, TypeError) as exc:
        raise FoundationError(f"Export requires a source-backed fact: {name}") from exc


def _condition(variable, value):
    return {"logic": "or", "conditions": [{"variable": variable, "operator": "equals", "operand": value}]}


def _edge(target, variable, value, description):
    return {"target_node": target, "description": description, "condition": _condition(variable, value)}


def _terminal(name):
    return {"name": name, "node_type": "end_call", "on_enter": {"type": "silent"},
            "disable_inactivity_monitor": True, "transitions": [],
            "terminal": {"type": "function", "function": {"name": name,
                "handler": "__function__:end_call", "parameters": {"type": "object", "properties": {
                    "message_mode": {"type": "string", "default": "none"}}}}}}


def _code_node(name, code, transitions, fallback):
    return {"name": name, "node_type": "code", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": fallback,
            "pre_actions": [{"type": "function", "function": {"name": "code_exec",
                "handler": "__function__:code_exec", "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "default": code},
                    "timeout_ms": {"type": "number", "default": 10000}}}}}],
            "transitions": transitions}


def _request_guard(name, facts, schema):
    """Generated hooks reject before a request. Hook error handling needs live verification."""
    lines = [
        "import json, datetime",
        "if vars.get('_export_policy_admitted') != 'yes':",
        "    raise ValueError('Policy compilation incomplete')",
        "if vars.get('_export_close_attempted') == 'yes':",
        "    raise ValueError('Bank operation after close attempt')",
        "body = config.get('body')",
        "if not isinstance(body, dict):",
        "    raise ValueError('Explicit JSON object body required')",
        "if not vars.get('customer_id') or body.get('customer_id') != vars.get('customer_id'):",
        "    raise ValueError('Customer binding missing or mismatched')",
        "schema = json.loads(" + repr(_json(schema)) + ")",
        "for key in schema.get('required', []):",
        "    if key not in body:",
        "        raise ValueError('Missing request field: ' + key)",
        "for key, value in body.items():",
        "    spec = schema.get('properties', dict()).get(key)",
        "    if not isinstance(spec, dict):",
        "        raise ValueError('Unknown request field')",
        "    kind = spec.get('type')",
        "    valid = ((kind == 'string' and isinstance(value, str)) or (kind == 'number' and type(value) in (int, float)) or (kind == 'integer' and type(value) is int) or (kind == 'boolean' and type(value) is bool) or (kind == 'object' and isinstance(value, dict)) or (kind == 'array' and isinstance(value, list)))",
        "    if not valid:",
        "        raise ValueError('Request field type rejected')",
        "    if 'enum' in spec and not any(type(value) is type(item) and value == item for item in spec['enum']):",
        "        raise ValueError('Request enum rejected')",
        "    if kind in ('number', 'integer'):",
        "        if value != value or value in (float('inf'), -float('inf')):",
        "            raise ValueError('Nonfinite request number')",
        "        if value < spec.get('minimum', -float('inf')) or value > spec.get('maximum', float('inf')):",
        "            raise ValueError('Request number outside schema limits')",
        "    if spec.get('format') == 'date':",
        "        if len(value) != 10 or datetime.date.fromisoformat(value).isoformat() != value:",
        "            raise ValueError('Request date not canonical ISO date')",
    ]
    if name in {"get_account", "create_promise", "add_note"}:
        lines += ["if vars.get('_export_identity') != 'verified':",
                  "    raise ValueError('Identity verification required')"]
    if name == "verify_identity":
        lines += ["order = json.loads(" + repr(_json(facts["identity_order"])) + ")",
                  "verified = vars.get('_export_verified_fields', [])",
                  "pending = [item for item in order if item not in verified]",
                  "if not pending or body.get('field') != pending[0]:",
                  "    raise ValueError('Identity factor order rejected')"]
    if name in {"create_promise", "add_note"}:
        lines += ["products = vars.get('_export_products', [])",
                  "if not any(isinstance(p, dict) and p.get('product_id') == body.get('product_id') for p in products):",
                  "    raise ValueError('Unknown product ID')"]
    if name == "create_promise":
        lines += [
            "if vars.get('_export_validation') != 'valid':",
            "    raise ValueError('Proposal has not been validated')",
            "bundle = vars.get('_export_validated_bundle', [])",
            "revision = vars.get('_export_bundle_revision', '')",
            "if not revision or vars.get('_export_confirmed_revision') != revision:",
            "    raise ValueError('Consent missing or stale')",
            "proposal = dict((key, body.get(key)) for key in ('product_id', 'amount', 'date'))",
            "if proposal not in bundle:",
            "    raise ValueError('Write differs from confirmed bundle')",
            "written = vars.get('_export_recorded_products', [])",
            "if proposal['product_id'] in written:",
            "    raise ValueError('Recorded promise is immutable')",
            "if vars.get('_export_transfer') == 'succeeded':",
            "    raise ValueError('No promise writes after transfer')",
        ]
    if name == "add_note":
        lines += ["if " + repr(facts["notes_enabled"]) + " is not True:",
                  "    raise ValueError('Notes prohibited by supplied policy')",
                  "if vars.get('_export_transfer_requested') == 'yes' or vars.get('_export_transfer') == 'succeeded':",
                  "    raise ValueError('Notes prohibited on transfer path')",
                  "if vars.get('_export_notes_ready') != 'yes':",
                  "    raise ValueError('Deferred note decision not ready')"]
    if name == "transfer_to_agent":
        lines += ["vars['_export_transfer_requested'] = 'yes'",
                  "vars['_export_confirmed_revision'] = ''",
                  "vars['_export_deferred_notes'] = []"]
    if name == "close_call":
        lines += ["if vars.get('_export_goodbye_done') != 'yes' or vars.get('_export_outcome_ready') != 'yes':",
                  "    raise ValueError('Farewell and outcome decision required')",
                  "rows = json.loads(" + repr(_json(facts["outcome_table"])) + ")",
                  "matching = [row for row in rows if row['outcome'] == body.get('outcome')]",
                  "if len(matching) != 1:",
                  "    raise ValueError('Outcome not in source table')",
                  "reasons = matching[0]['reasons']",
                  "if (reasons and body.get('reason') not in reasons) or (not reasons and 'reason' in body):",
                  "    raise ValueError('Outcome and reason mismatch')",
                  "vars['_export_close_attempted'] = 'yes'"]
    return "\n".join(lines) + "\n"


def _result_hook(name, facts):
    lines = ["try:", "    data = result.get('data')",
             "    if not isinstance(data, dict):", "        data = dict()", "        result['data'] = data",
             "    state = 'succeeded' if data.get('ok') is True else 'failed' if data.get('ok') is False else 'unknown'",
             "    result.pop('header', None)"]
    if name == "verify_identity":
        lines += ["    if state == 'succeeded' and type(data.get('verified')) is not bool:",
                  "        state = 'unknown'",
                  "    if state == 'succeeded' and data.get('verified') is True:",
                  "        field = vars.get('verify_identity_args.field')",
                  "        if field is None:",
                  "            field = vars.get('verify_identity_args', dict()).get('field')",
                  "        order = json.loads(" + repr(_json(facts["identity_order"])) + ")",
                  "        verified = list(vars.get('_export_verified_fields', []))",
                  "        pending = [item for item in order if item not in verified]",
                  "        if pending and field == pending[0]:",
                  "            verified.append(field)",
                  "        vars['_export_verified_fields'] = verified",
                  "        vars['_export_identity'] = 'verified' if verified == order else 'pending'"]
        lines.insert(0, "import json")
    if name == "get_account":
        lines += ["    if state == 'succeeded' and not isinstance(data.get('products'), list):",
                  "        state = 'unknown'",
                  "    if state == 'succeeded' and isinstance(data.get('products'), list):",
                  "        vars['_export_products'] = data['products']"]
    if name == "create_promise":
        lines += ["    if state == 'succeeded' and (not isinstance(data.get('promise_id'), str) or not data['promise_id']):",
                  "        state = 'unknown'",
                  "    if state == 'succeeded':",
                  "        product = vars.get('create_promise_args.product_id')",
                  "        if product is None:",
                  "            product = vars.get('create_promise_args', dict()).get('product_id')",
                  "        if product:",
                  "            recorded = list(vars.get('_export_recorded_products', []))",
                  "            if product not in recorded:", "                recorded.append(product)",
                  "            vars['_export_recorded_products'] = recorded"]
    if name == "transfer_to_agent":
        lines += ["    vars['_export_transfer'] = state"]
    if name == "close_call":
        lines += ["    vars['_export_close_result'] = state"]
    lines += ["    data['_export_result_state'] = state", "except Exception:", "    pass"]
    return "\n".join(lines) + "\n"


def _bank_tool(operation, facts):
    name = operation["name"]
    body = copy.deepcopy(operation["request"])
    for key, spec in body.get("properties", {}).items():
        spec["default"] = "{{customer_id}}" if key == "customer_id" else "{{input_" + name + "_" + key + "}}"
    mappings = [{"path": "data._export_result_state", "variable": "_export_result_" + name}]
    mappings += [{"path": "data." + key, "variable": "_export_response_" + name + "_" + key}
                 for key in operation["response"].get("properties", {})]
    return {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
            "function": {"name": name, "handler": "__function__:api_call",
                "description": operation.get("summary", "") + " Confirm business success from the response before reporting completion.",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string", "default": operation["url"]},
                    "method": {"type": "string", "default": operation["method"]},
                    "headers": {"type": "object", "properties": {
                        "Content-Type": {"type": "string", "default": "application/json"}}}, "body": body}}},
            "pre_process_code": _request_guard(name, facts, operation["request"]),
            "post_process_code": _result_hook(name, facts), "response_mapping": mappings}


def validate_local_workflow(workflow):
    """Validate only the generated documented subset; this is not Freya's schema validator."""
    def no_nulls(value):
        if value is None:
            raise FoundationError("Freya preview contains null")
        if isinstance(value, dict):
            for item in value.values():
                no_nulls(item)
        elif isinstance(value, list):
            for item in value:
                no_nulls(item)
    no_nulls(workflow)
    if set(workflow) - {"initial_node", "nodes", "system_prompt"}:
        raise FoundationError("Unsupported workflow-level field")
    nodes = workflow["nodes"]
    if not nodes or workflow["initial_node"] not in nodes:
        raise FoundationError("Missing initial node")
    def check_condition(condition, top=True):
        if "logic" in condition:
            if condition["logic"] not in {"or", "and"} or (top and condition["logic"] != "or"):
                raise FoundationError("Invalid routing group")
            if not condition.get("conditions"):
                raise FoundationError("Empty routing condition")
            for child in condition["conditions"]:
                check_condition(child, False)
        elif condition.get("operator") in {"equals", "not_equals"}:
            if not isinstance(condition.get("variable"), str) or "operand" not in condition:
                raise FoundationError("Invalid variable condition")
        elif condition.get("operator") != "always":
            raise FoundationError("Unsupported preview condition")
    for name, node in nodes.items():
        if node.get("name") != name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise FoundationError("Node key/name mismatch")
        kind = node.get("node_type")
        if kind not in {"conversation", "api_request", "code", "end_call"}:
            raise FoundationError("Unsupported node type")
        if (kind != "conversation" and node.get("tools")) or (kind == "conversation" and node.get("pre_actions")):
            raise FoundationError("Tool attached to a node type that strips it")
        transitions = node.get("transitions", [])
        targets = [edge["target_node"] for edge in transitions]
        if len(targets) != len(set(targets)):
            raise FoundationError("Duplicate targets merge unpredictably")
        for edge in transitions:
            if edge["target_node"] not in nodes:
                raise FoundationError("Transition target missing")
            check_condition(edge["condition"])
        for key in ("passthrough_default_transition", "max_turn_transition"):
            if key in node and node[key] not in nodes:
                raise FoundationError("Fallback target missing")
        for mapping in node.get("response_mapping", []):
            if not mapping["path"].startswith("data."):
                raise FoundationError("Node mapping does not use HTTP data envelope")
        if kind == "end_call":
            if transitions or node.get("tools") or node.get("pre_actions"):
                raise FoundationError("Terminal has outgoing operations")
            if node.get("terminal", {}).get("function", {}).get("handler") != "__function__:end_call":
                raise FoundationError("Invalid end-call terminal")
        for tool in node.get("tools", []) + node.get("pre_actions", []):
            fn = tool["function"]
            if fn["handler"] == "__function__:api_call":
                if fn["name"] not in TOOL_NAMES:
                    raise FoundationError("Non-bank API operation")
                for mapping in tool.get("response_mapping", []):
                    if not mapping["path"].startswith("data."):
                        raise FoundationError("Mapping does not use HTTP data envelope")
                compile(tool["pre_process_code"], "pre_hook", "exec")
                compile(tool["post_process_code"], "post_hook", "exec")
            elif fn["handler"] == "__function__:code_exec":
                code = fn["parameters"]["properties"]["code"]["default"]
                if "{{" in code or "}}" in code:
                    raise FoundationError("Code contains template delimiters")
                compile(code, "code_node", "exec")
            else:
                raise FoundationError("Unsupported handler")
    # Every node must have a structural path to a terminal, including fallback paths.
    terminals = {name for name, node in nodes.items() if node["node_type"] == "end_call"}
    finishing = set(terminals)
    while True:
        enlarged = finishing | {name for name, node in nodes.items() if
            any(edge["target_node"] in finishing for edge in node.get("transitions", [])) or
            node.get("max_turn_transition") in finishing or node.get("passthrough_default_transition") in finishing}
        if enlarged == finishing:
            break
        finishing = enlarged
    if finishing != set(nodes):
        raise FoundationError("A node has no structural terminal path")
    return {"documented_subset_valid": True, "node_count": len(nodes),
            "scope": "Local structure, Python syntax and placement only; not full platform schema acceptance",
            "platform_validated": False, "conversation_validated": False, "publish_allowed": False}


class _SandboxSource(ast.NodeTransformer):
    """Bundle the local core without repo imports or template-like f-string braces."""
    def __init__(self, compact_runtime=False):
        self.compact_runtime = compact_runtime

    def visit_ImportFrom(self, node):
        if node.level or node.module == "__future__":
            return None
        return node

    def visit_ClassDef(self, node):
        node = self.generic_visit(node)
        if node.name == "BankContract":
            node.body = [item for item in node.body if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                         or item.name not in ({"__init__", "from_path", "freya_templates"} if self.compact_runtime else {"from_path", "freya_templates"})]
        if self.compact_runtime and node.name == "Conversation":
            node.body = [item for item in node.body if not isinstance(item, ast.FunctionDef) or item.name != "say"]
        if not node.body:
            node.body = [ast.Pass()]
        return node

    def visit_Expr(self, node):
        if self.compact_runtime and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return self.generic_visit(node)

    def visit_JoinedStr(self, node):
        values = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                values.append(value)
            else:
                expression = self.visit(value.value)
                if value.conversion == 114:
                    expression = ast.Call(func=ast.Name(id="repr", ctx=ast.Load()), args=[expression], keywords=[])
                if value.format_spec:
                    expression = ast.Call(func=ast.Name(id="format", ctx=ast.Load()),
                                          args=[expression, self.visit(value.format_spec)], keywords=[])
                else:
                    expression = ast.Call(func=ast.Name(id="str", ctx=ast.Load()), args=[expression], keywords=[])
                values.append(expression)
        return ast.copy_location(ast.Call(func=ast.Attribute(value=ast.Constant(value=""), attr="join", ctx=ast.Load()),
                                         args=[ast.List(elts=values, ctx=ast.Load())], keywords=[]), node)


def _literal_json(value):
    # Escaping braces in string literals avoids Freya's textual template scanner.
    return repr(_json(value)).replace("{", "\\x7b").replace("}", "\\x7d")


def _bundle_core(core_sources, *, compact_runtime=False):
    names = ("models.py", "calendar.py", "api_contract.py", "runtime.py", "policy_specs.py", "semantics.py", "turns.py", "conversation.py")
    if set(core_sources) != set(names):
        raise FoundationError("Practice export requires all eight explicit core source snapshots")
    chunks = []
    for name in names:
        if not core_sources[name].strip():
            raise FoundationError("Empty practice core source: " + name)
        if compact_runtime and name in {"semantics.py", "policy_specs.py"}:
            # Admission/quote analysis has already run against full sources. Keep
            # that evidence outside sandbox requests; no runtime rule uses it.
            continue
        tree = ast.parse(core_sources[name], filename=name)
        if compact_runtime:
            tree.body = [item for item in tree.body if not isinstance(item, ast.FunctionDef)
                         or item.name not in {"strict_json", "resolve_schema", "parse_turn", "turn_contract", "conversation_plan"}]
        tree = _SandboxSource(compact_runtime).visit(tree)
        ast.fix_missing_locations(tree)
        chunks.append(ast.unparse(tree))
    code = "\n\n".join(chunks).replace("}}", "} }")
    if "{{" in code or "}}" in code:
        raise FoundationError("Bundled core still contains template delimiters")
    compile(code, "practice_core_bundle", "exec")
    return code


def _compact_practice_capsule(policy, contract, documents):
    return {"facts": {name: copy.deepcopy(item["value"]) for name, item in policy["facts"].items()},
            "gaps": copy.deepcopy(policy["gaps"]), "contract": copy.deepcopy(contract),
            "source_hashes": sorted(item["sha256"] for item in documents)}


def _guard_code_exec_inputs(workflow):
    """Guard growth before dispatch; never truncate the authoritative replay log."""
    codes = {}
    for name, node in workflow["nodes"].items():
        for tool in node.get("pre_actions", []):
            if tool["function"]["handler"] == "__function__:code_exec":
                codes[name] = tool["function"]["parameters"]["properties"]["code"]["default"]
    overhead = len(code_exec_request_bytes("", {})) - len(json.dumps("").encode()) - len(json.dumps({}).encode())
    peak_literal_bytes = 0
    def check(prefix=""):
        return (prefix + "if " + str(peak_literal_bytes) + " + len(json.dumps(vars, ensure_ascii=True, allow_nan=False).encode('utf-8')) + " + str(overhead) + " > " + str(CODE_EXEC_SAFE_BYTES) + ":\n" +
                prefix + "    raise ValueError('Serialized code+vars budget exhausted; replay retained without another bank action')\n")
    for _ in range(8):
        for name, code in codes.items():
            guarded = "try:\n    import json\n" + check("    ") + "\n".join("    " + line for line in code.splitlines()) + "\n" + check("    ")
            guarded += "except Exception as error:\n    vars['_bridge_fault'] = str(error)\n    vars['_bridge_route'] = 'bridge_fault'\n    vars['_bridge_has_speech'] = 'no'\n    vars['_bridge_speech'] = ''\n"
            if name == "bridge_ingest_turn":
                guarded += "    vars['_bridge_ingest'] = 'failed'\n    vars['_bridge_turn_json'] = ''\n"
            if name == "bridge_observe":
                guarded += "    vars['_bridge_response_state'] = 'failed'\n    vars.pop('_bridge_http_response', None)\n"
            workflow["nodes"][name]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"] = guarded
        actual = max(len(json.dumps(workflow["nodes"][name]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"], ensure_ascii=True).encode()) for name in codes)
        if actual == peak_literal_bytes:
            break
        peak_literal_bytes = actual
    else:
        raise FoundationError("Code budget guard did not converge")
    for name, node in workflow["nodes"].items():
        if name.startswith("bank_"):
            tool = node["pre_actions"][0]
            tool["pre_process_code"] = "import json\n" + check() + tool["pre_process_code"]
            # Reject an oversized mapped response before putting it into vars.
            tool["post_process_code"] = "\n".join([
                "import json", "data = result.get('data')", "if isinstance(data, dict):",
                "    observed = dict(data)", "    prospective = dict(vars)", "    prospective['_bridge_http_response'] = observed",
                "    if " + str(peak_literal_bytes) + " + len(json.dumps(prospective, ensure_ascii=True, allow_nan=False).encode('utf-8')) + " + str(overhead) + " > " + str(CODE_EXEC_SAFE_BYTES) + ":",
                "        vars['_bridge_fault'] = 'Serialized code+vars budget exhausted by bank response'",
                "        vars['_bridge_route'] = 'bridge_fault'", "        data['_bridge_payload'] = None",
                "    else:", "        data['_bridge_payload'] = observed", "result.pop('header', None)",
            ]) + "\n"
    return peak_literal_bytes


def export_practice_workflow(plan, policy, contract, sources, *, upstream_trace, policy_documents, core_sources,
                             initial_variables=None, reserve_variable_bytes=CODE_EXEC_VARIABLE_RESERVE_BYTES):
    """Create a practice-version-bound replay bridge, executable locally with observed responses.

    Actual HTTP requests are engine pre-actions, never a Python HTTP client. The
    core is replayed from serialized customer events and observed bank responses.
    It can therefore suspend before each external action, including finalization.
    Live Freya sandbox, extraction, interrupt and passthrough behavior is unverified.
    """
    from .models import Document, Evidence, Fact, Policy, Requirement
    from .api_contract import BankContract, Operation
    from .semantics import compile_practice_policy

    preview = export_workflow(plan, policy, contract, sources, upstream_trace=upstream_trace)
    docs = [Document(**item) for item in policy_documents]
    parsed_policy = Policy([Requirement(**{**item, "evidence": Evidence(**item["evidence"])}) for item in policy["requirements"]],
        {name: Fact(value=item["value"], evidence=tuple(Evidence(**entry) for entry in item["evidence"]), recognizer=item["recognizer"])
         for name, item in policy["facts"].items()}, policy["tables"], policy["gaps"], policy["input_order"], policy["schema_version"])
    parsed_contract = BankContract.__new__(BankContract)
    parsed_contract.source_id, parsed_contract.source_hash = contract["source_id"], contract["source_sha256"]
    parsed_contract.server = contract["server"]
    parsed_contract.operations = {name: Operation(**item) for name, item in contract["operations"].items()}
    for doc in docs:
        source = next((item for item in sources if item["source_id"] == doc.source_id), {})
        if source.get("role") != "policy" or source.get("sha256") != doc.sha256 or hashlib.sha256(doc.text.encode("utf-8")).hexdigest() != doc.sha256:
            raise FoundationError("Practice policy text does not match its admitted source hash")
    semantics = compile_practice_policy(docs, parsed_policy, parsed_contract)
    if not semantics["complete_for_supplied_practice"]:
        raise FoundationError("Executable bridge is restricted to the reviewed practice A/B/API versions")
    bundle = _bundle_core(core_sources, compact_runtime=True)
    capsule = _compact_practice_capsule(policy, contract, policy_documents)
    evidence_package = {"policy": copy.deepcopy(policy), "contract": copy.deepcopy(contract),
                        "documents": copy.deepcopy(policy_documents), "semantics": copy.deepcopy(semantics)}
    configuration = {"capsule": capsule, "capsule_sha256": _digest(capsule), "evidence_sha256": _digest(evidence_package)}
    driver = bundle + "\n" + "\n".join([
        "_bridge_config = json.loads(" + _literal_json(configuration) + ")",
        "def compile_practice_policy(documents, policy, contract):",
        "    actual = dict(facts=dict((name, item.value) for name, item in policy.facts.items()), gaps=policy.gaps, contract=contract.serializable(), source_hashes=sorted(doc.sha256 for doc in documents))",
        "    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()",
        "    if digest != _bridge_config['capsule_sha256']:",
        "        raise FoundationError('Runtime capsule differs from source-admitted practice export')",
        "    return dict(complete_for_supplied_practice=True, source_version_admitted=True, evidence_sha256=_bridge_config['evidence_sha256'])",
        "class _BridgeNeeded(BaseException):",
        "    def __init__(self, action):",
        "        self.action = action",
        "def _bridge_call(self, action):",
        "    position = len(self.calls)",
        "    self.calls.append(copy.deepcopy(action))",
        "    responses = json.loads(vars.get('_bridge_responses_json', '[]'))",
        "    if position >= len(responses):",
        "        raise _BridgeNeeded(action)",
        "    observed = responses[position]",
        "    if observed['operation'] != action.operation or observed['payload'] != action.payload:",
        "        raise FoundationError('Replay action differs from observed bank request')",
        "    return copy.deepcopy(observed['response'])",
        "MockBank.call = _bridge_call",
        "_c = None",
        "_needed = None",
        "vars['_bridge_route'] = 'bridge_fault'",
        "vars['_bridge_has_speech'] = 'no'",
        "vars['_bridge_turn_json'] = ''",
        "try:",
        "    spec = _bridge_config['capsule']",
        "    documents = [Document('', '', digest, 0, '', '') for digest in spec['source_hashes']]",
        "    facts = dict((name, Fact(value=value, evidence=tuple(), recognizer='source_admitted_export_capsule')) for name, value in spec['facts'].items())",
        "    policy = Policy([], facts, [], spec['gaps'], [], 'foundation-policy-1')",
        "    contract = BankContract.__new__(BankContract)",
        "    contract.source_id = spec['contract']['source_id']",
        "    contract.source_hash = spec['contract']['source_sha256']",
        "    contract.server = spec['contract']['server']",
        "    contract.operations = dict((name, Operation(**item)) for name, item in spec['contract']['operations'].items())",
        "    _c = Conversation(documents, policy, contract, json.loads(vars['_bridge_call_json']), MockBank(dict()))",
        "    _c.start()",
        "    events = json.loads(vars.get('_bridge_events_json', '[]'))",
        "    for event in events:",
        "        if event['kind'] == 'turn':",
        "            values = dict(event['turn'])",
        "            values['intents'] = tuple(values['intents'])",
        "            _c.receive(Turn(**values))",
        "        elif event['kind'] == 'advance':",
        "            _c.advance()",
        "        else:",
        "            raise FoundationError('Unknown replay event')",
        "    while _c.phase in ('recording', 'post_record'):",
        "        events.append(dict(kind='advance'))",
        "        vars['_bridge_events_json'] = json.dumps(events)",
        "        _c.advance()",
        "except _BridgeNeeded as pending:",
        "    _needed = pending.action",
        "except Exception as error:",
        "    vars['_bridge_fault'] = str(error)",
        "if _c is not None:",
        "    cursor = int(vars.get('_bridge_speech_cursor', 0))",
        "    speech = ' '.join(item['text'] for item in _c.messages[cursor:])",
        "    vars['_bridge_speech_cursor'] = len(_c.messages)",
        "    vars['_bridge_speech'] = speech",
        "    vars['_bridge_has_speech'] = 'yes' if speech else 'no'",
        "    vars['_bridge_phase'] = _c.phase",
        "    vars['_bridge_readback_token'] = _c.confirmation_token if _c.phase == 'readback' else ''",
        "    vars['_bridge_identity_field'] = next((name for name in _c.session.state.required_identity_fields if name not in _c.session.state.verified), '')",
        "    vars['_bridge_products_for_prompt'] = json.dumps([p.details for p in _c.session.state.products]) if _c.session.state.identity_verified else '[]'",
        "    if _needed is not None:",
        "        vars['_bridge_pending_json'] = json.dumps(asdict(_needed), sort_keys=True)",
        "        vars['_bridge_route'] = 'bank_' + _needed.operation",
        "        vars.pop('_bridge_http_response', None)",
        "        for name, operation in contract.operations.items():",
        "            for key in operation.request.get('properties', dict()):",
        "                vars.pop('_bridge_arg_' + key, None)",
        "        for key, value in _needed.payload.items():",
        "            vars['_bridge_arg_' + key] = value",
        "    elif not vars.get('_bridge_fault') and _c.phase == 'ended' and _c.outcome and _c.outcome['tool_confirmed']:",
        "        vars['_bridge_route'] = 'bridge_end'",
        "    elif not vars.get('_bridge_fault') and _c.phase not in ('new', 'ended', 'fault'):",
        "        vars['_bridge_route'] = 'bridge_listen'",
    ]) + "\n"
    # Dependency imports and class construction can fail before the driver body.
    # A small outer wrapper keeps that failure from routing to an external action.
    driver = "vars['_bridge_route'] = 'bridge_fault'\nvars['_bridge_has_speech'] = 'no'\ntry:\n" + "\n".join("    " + line for line in driver.splitlines()) + "\nexcept Exception as error:\n    vars['_bridge_fault'] = str(error)\n"
    if "{{" in driver or "}}" in driver:
        raise FoundationError("Bridge driver contains template delimiters")
    nodes = {"bridge_end": _terminal("bridge_end"), "bridge_fault": _terminal("bridge_fault")}
    nodes["bridge_fault"]["terminal"]["function"]["parameters"]["properties"] = {
        "message_mode": {"type": "string", "default": "custom"},
        "spoken_message": {"type": "string", "default": "I could not confirm that this request was completed."}}
    init = "\n".join([
        "import json", "vars['_bridge_events_json'] = '[]'", "vars['_bridge_responses_json'] = '[]'",
        "vars['_bridge_speech_cursor'] = 0", "vars['_bridge_fault'] = ''", "vars['_bridge_bank_closed'] = 'no'",
        "vars['_bridge_turn_json'] = ''", "vars['_bridge_pending_json'] = ''",
        "vars['_bridge_call_json'] = json.dumps(dict((key, vars.get(key, '')) for key in ('customer_id', 'first_name', 'last_name', 'title', 'now')))",
    ]) + "\n"
    nodes["bridge_init"] = _code_node("bridge_init", init, [{"target_node": "bridge_drive", "description": "Initialize call state", "condition": {"operator": "always"}}], "bridge_fault")
    nodes["bridge_drive"] = _code_node("bridge_drive", driver, [
        _edge("bridge_speak", "_bridge_has_speech", "yes", "Speak source-derived response before pending bank action"),
        _edge("bridge_dispatch", "_bridge_has_speech", "no", "No new response to speak")], "bridge_fault")
    routes = ["bridge_listen", "bridge_end", "bridge_fault"] + ["bank_" + name for name in contract["operations"]]
    dispatch = [_edge(target, "_bridge_route", target, "Replay engine selects next action") for target in routes]
    nodes["bridge_dispatch"] = _code_node("bridge_dispatch", "pass\n", dispatch, "bridge_fault")
    nodes["bridge_speak"] = {"name": "bridge_speak", "node_type": "conversation", "on_enter": {"type": "tts", "text": "{{_bridge_speech}}"},
        "task_message": "Say the supplied response exactly. No tool call is allowed in this turn.", "passthrough": True,
        "passthrough_default_transition": "bridge_dispatch", "transitions": [{"target_node": "bridge_dispatch", "description": "Response delivered", "condition": {"operator": "always"}}]}
    intent_names = sorted(__import__("builder.turns", fromlist=["INTENTS"]).INTENTS)
    extraction = ("Return one JSON object for ONLY the customer's current message. Allowed keys: intents (array of " + ", ".join(intent_names) +
        "), date_text, amount, answer, time, product_id, all_products (boolean). All other values are strings. "
        "Use intents=[\"unknown\"] when unclear. Do not infer agreement from a question, greeting or silence. "
        "Phase: {{_bridge_phase}}. Identity field currently asked: {{_bridge_identity_field}}. "
        "Products available after identity: {{_bridge_products_for_prompt}}. Call date/time: {{now}}. "
        "Preserve vague date wording; never turn next week into an exact date. Preserve self-corrections; copy the customer's final stated value. "
        "A proposal during hardship must include both intents. Product IDs must come from the supplied products. "
        "Do not return consent_token; the engine binds explicit consent to the current readback. Do not copy prior-message slots.")
    nodes["bridge_listen"] = {"name": "bridge_listen", "node_type": "conversation", "on_enter": {"type": "silent"},
        "task_message": "Wait for the customer's answer to the question already spoken. No tool call is allowed in this turn. Do not add or invent policy decisions.",
        "extraction_schema": [{"name": "_bridge_turn_json", "type": "string", "description": extraction,
                               "min_confidence": 0.6, "validation_pattern": "^[\\s\\S]*$", "max_retries": 1}],
        "transitions": [{"target_node": "bridge_ingest_turn", "description": "A new customer event was extracted",
                         "condition": {"logic": "or", "conditions": [{"variable": "_bridge_turn_json", "operator": "not_equals", "operand": ""}]}}],
        "max_node_turns": 10, "max_turn_transition": "bridge_fault"}
    ingest = "\n".join([
        "import json", "vars['_bridge_ingest'] = 'failed'", "try:",
        "    value = json.loads(vars.get('_bridge_turn_json', ''))",
        "    allowed = ('intents', 'date_text', 'amount', 'answer', 'time', 'product_id', 'all_products')",
        "    if not isinstance(value, dict) or set(value) - set(allowed) or not isinstance(value.get('intents'), list) or not value['intents']:",
        "        raise ValueError('Malformed extraction event')",
        "    if set(value['intents']) - set(" + repr(intent_names) + ") or len(set(value['intents'])) != len(value['intents']):",
        "        raise ValueError('Unknown or duplicate intents')",
        "    for key in ('date_text', 'amount', 'answer', 'time', 'product_id'):",
        "        if key in value and not isinstance(value[key], str):",
        "            raise ValueError('Turn slots must be strings')",
        "    if 'all_products' in value and type(value['all_products']) is not bool:",
        "        raise ValueError('all_products must be boolean')",
        "    value['consent_token'] = vars.get('_bridge_readback_token', '') if value['intents'] == ['yes'] else ''",
        "    events = json.loads(vars.get('_bridge_events_json', '[]'))",
        "    events.append(dict(kind='turn', turn=value))", "    vars['_bridge_events_json'] = json.dumps(events)",
        "    vars['_bridge_ingest'] = 'succeeded'", "except Exception as error:", "    vars['_bridge_fault'] = str(error)",
        "vars['_bridge_turn_json'] = ''",
    ]) + "\n"
    nodes["bridge_ingest_turn"] = _code_node("bridge_ingest_turn", ingest, [
        _edge("bridge_drive", "_bridge_ingest", "succeeded", "Validated current customer event"),
        _edge("bridge_fault", "_bridge_ingest", "failed", "Extraction rejected without bank operation")], "bridge_fault")
    response_code = "\n".join([
        "import json", "vars['_bridge_response_state'] = 'failed'", "try:",
        "    observed = vars.get('_bridge_http_response')",
        "    if isinstance(observed, str):", "        observed = json.loads(observed)",
        "    if not isinstance(observed, dict) or type(observed.get('ok')) is not bool:",
        "        raise ValueError('Ambiguous bank response; no success or retry is inferred')",
        "    pending = json.loads(vars['_bridge_pending_json'])",
        "    responses = json.loads(vars.get('_bridge_responses_json', '[]'))",
        "    responses.append(dict(operation=pending['operation'], payload=pending['payload'], response=observed))",
        "    vars['_bridge_responses_json'] = json.dumps(responses)",
        "    if pending['operation'] == 'close_call':", "        vars['_bridge_bank_closed'] = 'yes'",
        "    vars['_bridge_response_state'] = 'succeeded'", "except Exception as error:", "    vars['_bridge_fault'] = str(error)",
        "vars.pop('_bridge_http_response', None)",
    ]) + "\n"
    nodes["bridge_observe"] = _code_node("bridge_observe", response_code, [
        _edge("bridge_drive", "_bridge_response_state", "succeeded", "Observed business response becomes replay input"),
        _edge("bridge_fault", "_bridge_response_state", "failed", "Unconfirmed bank response is quarantined")], "bridge_fault")
    for name, operation in contract["operations"].items():
        tool = copy.deepcopy(preview["tool_catalog"][name])
        for key, spec in tool["function"]["parameters"]["properties"]["body"]["properties"].items():
            spec["default"] = "{{_bridge_arg_" + key + "}}"
        tool["pre_process_code"] = "\n".join([
            "import json", "pending = json.loads(vars.get('_bridge_pending_json', ''))",
            "if pending.get('operation') != " + repr(name) + " or pending.get('payload') != config.get('body'):",
            "    raise ValueError('Bank request differs from guarded replay action')",
            "if vars.get('_bridge_bank_closed') == 'yes':", "    raise ValueError('Bank operation after close')",
        ]) + "\n"
        tool["post_process_code"] = "\n".join([
            "try:", "    data = result.get('data')", "    if isinstance(data, dict):",
            "        observed = dict(data)", "        data['_bridge_payload'] = observed", "    result.pop('header', None)",
            "except Exception:", "    pass",
        ]) + "\n"
        tool.pop("response_mapping")
        nodes["bank_" + name] = {"name": "bank_" + name, "node_type": "api_request", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": "bridge_observe", "pre_actions": [tool],
            "response_mapping": [{"path": "data._bridge_payload", "variable": "_bridge_http_response"}],
            "transitions": [{"target_node": "bridge_observe", "description": "Observe actual tool response", "condition": {"operator": "always"}}]}
    workflow = {"initial_node": "bridge_init", "nodes": nodes,
                "system_prompt": "You are Freya Bank's digital assistant. Speak English. The supplied state machine determines responses and bank actions. Never invent an operation result, policy or protected account information."}
    _guard_code_exec_inputs(workflow)
    budget = measure_code_exec_budget(workflow, initial_variables, reserve_variable_bytes=reserve_variable_bytes)
    if not budget["passed"]:
        raise FoundationError("Serialized code+vars request exceeds safe byte budget before export admission")
    validation = validate_local_workflow(workflow)
    # This permission concerns publishing a candidate for a controlled practice
    # preflight. It is not platform validation or final/hackathon readiness.
    validation.pop("publish_allowed")
    validation["checker_grants_publication_permission"] = False
    reported_semantics = copy.deepcopy(semantics)
    reported_semantics["legacy_core_publish_allowed"] = reported_semantics.pop("publish_allowed")
    profile = "B" if parsed_policy.facts["notes_enabled"].value else "A"
    readiness = {
        "status": "ready_for_controlled_practice_preflight",
        "practice_profile": profile,
        "ready_for_platform_preflight": True,
        "publish_allowed": True,
        "publication_scope": "controlled_synthetic_practice_pilot_only",
        "practice_pilot_allowed": True,
        "first_pilot_max_cases": 1,
        "recommended_first_case": "PUB-01" if profile == "A" else "PUB-11",
        "simulation_preflight_verified": False,
        "simulation_allowed_before_preflight": False,
        "hard_export_blockers": [],
        "platform_checks_before_simulation": [
            "Verify candidate, upstream artifacts, trace and current core-source hashes",
            "Save/publish in the intended synthetic practice workspace with the fixed event model",
            "Read back the saved graph and verify tools, guards, mappings and code are retained",
            "Run the initial text-chat turn and confirm bootstrap/driver imports and expected disclosure/question",
            "If startup faults or graph fields are stripped, stop and repair before any simulation",
            "Run only the selected single practice case first; inspect bank ledger and complete transcript",
        ],
        "local_execution_test_evidence": "Separate matching local-checks/preflight-evidence report; not inferred by this compiler",
        "final_submission_ready": False,
        "general_final_compiler_ready": False,
        "final_blockers": ["Changed/final source semantic compilation", "Repeated official scenario results", "Full final export/trace submission validation"],
    }
    result = {"format_version": "freya-practice-replay-3", "workflow": workflow, "validation": validation,
        "code_budget": budget, "runtime_evidence": evidence_package,
        "publish_allowed": True, "publication_scope": readiness["publication_scope"],
        "ready_for_platform_preflight": True, "practice_pilot_allowed": True,
        "final_submission_ready": False, "readiness": readiness,
        "boot_gated": False, "executable_candidate": True, "practice_only": True,
        "network_calls": 0, "model_calls": 0, "api_key_read": False,
        "provenance": {**preview["provenance"], "workflow_sha256": _digest(workflow),
                       "runtime_capsule_sha256": _digest(capsule), "runtime_evidence_sha256": _digest(evidence_package),
                       "core_source_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in core_sources.items()}},
        "format_basis": preview["format_basis"], "semantics": reported_semantics,
        "gaps": [
            {"code": "general_final_compiler_pending", "blocks": "final_submission", "blocks_practice_preflight": False, "detail": "Reviewed A/B/API hashes only; changed sources are rejected."},
            {"code": "sandbox_dependencies_unverified", "blocks": "simulation_until_platform_preflight", "blocks_practice_preflight": False, "detail": "Bundled core needs dataclasses, decimal, hashlib, copy, math, typing and urllib.parse as well as json/re/datetime; only local execution verified."},
            {"code": "platform_execution_unverified", "blocks": "simulation_until_platform_preflight", "blocks_practice_preflight": False, "detail": "Code size, persistence, post-hooks, response mapping, TTS passthrough and extraction need real platform save/readback/chat validation."},
            {"code": "free_text_extraction_unverified", "blocks": "broad_or_final_readiness", "blocks_practice_preflight": False, "detail": "Structured JSON extraction and partial/corrected speech have not been evaluated with the fixed phone model; inspect these in the first controlled practice case."},
            {"code": "between_write_interrupts_unverified", "blocks": "broad_or_final_readiness", "blocks_practice_preflight": False, "detail": "Passthrough write batches do not establish that customer interruptions can arrive between individual writes."},
            {"code": "unprovided_error_policy", "blocks": "broad_or_final_readiness", "blocks_practice_preflight": False, "detail": "Ambiguous responses enter a fault terminal rather than inventing a successful record or unspecified close outcome."},
        ], "core_interface": {"accepted": "Reviewed Conversation.receive(Turn), advance(), source policy and observed API responses",
            "transport": "Replay/suspend at MockBank.call boundary; platform pre-actions supply actual responses",
            "core_files_modified": False}}
    return result


def export_workflow(plan, policy, contract, sources, *, upstream_trace):
    """Pure deterministic compiler. Inputs are not mutated; no files are read."""
    if plan.get("kind") != "abstract_workflow_plan" or plan.get("schema_version") != "foundation-plan-1":
        raise FoundationError("Exporter needs an explicit foundation-plan-1 adapter")
    if policy.get("schema_version") != "foundation-policy-1":
        raise FoundationError("Exporter needs an explicit foundation-policy-1 adapter")
    operations = contract.get("operations", {})
    if set(operations) != TOOL_NAMES:
        raise FoundationError("Export requires all seven source-derived bank operations")
    known_sources = {record["source_id"]: record for record in sources}
    if len(known_sources) != len(sources):
        raise FoundationError("Duplicate source IDs")
    if contract.get("source_id") not in known_sources or known_sources[contract["source_id"]]["sha256"] != contract.get("source_sha256"):
        raise FoundationError("API source hash is not bound to source inventory")
    for requirement in policy.get("requirements", []):
        source = known_sources.get(requirement["evidence"]["source_id"], {})
        if source.get("role") != "policy":
            raise FoundationError("Only policy sources can become agent instructions")
    if not isinstance(upstream_trace, dict) or not upstream_trace.get("reference") or not re.fullmatch(r"[0-9a-f]{64}", upstream_trace.get("sha256", "")):
        raise FoundationError("An upstream trace reference and SHA-256 are required")
    facts = {name: _fact(policy, name) for name in ("identity_order", "notes_enabled", "outcome_table")}
    if not facts["identity_order"] or len(set(facts["identity_order"])) != len(facts["identity_order"]) or type(facts["notes_enabled"]) is not bool:
        raise FoundationError("Invalid identity or notes policy")
    tools = {name: _bank_tool(operation, facts) for name, operation in operations.items()}
    gaps = [{"code": "rule_binding_pending", "rule_id": rule["rule_id"], "source": rule["evidence"],
             "detail": "Rule text retained; no Freya control-flow/guard binding proves full implementation."}
            for rule in policy.get("requirements", [])]
    gaps += [{"code": "unnumbered_policy_binding_pending", "detail": "Unnumbered constraints and overlays require source-aware semantic compilation."},
             {"code": "platform_validation_pending", "detail": "No platform save/readback, hook error or extraction/routing behavior has been verified."},
             {"code": "conversation_validation_pending", "detail": "No official ledger/transcript result or repeated live conversation test exists for this export."}]
    expected_bindings = {
        "verify_identity": ("customer_id", "field", "answer"), "get_account": ("customer_id",),
        "create_promise": ("customer_id", "product_id", "amount", "date"),
        "add_note": ("customer_id", "product_id", "reason"),
        "schedule_callback": ("customer_id", "date", "time"),
        "transfer_to_agent": ("customer_id", "queue"), "close_call": ("customer_id", "outcome", "reason"),
    }
    for name, fields in expected_bindings.items():
        missing = sorted(set(fields) - operations[name]["request"].get("properties", {}).keys())
        if missing:
            gaps.append({"code": "api_guard_binding_pending", "operation": name, "missing_fields": missing,
                         "detail": "Request schema preserved, but these guard bindings require an explicit changed-schema adapter."})
    identity_enum = operations["verify_identity"]["request"].get("properties", {}).get("field", {}).get("enum", [])
    if any(field not in identity_enum for field in facts["identity_order"]):
        gaps.append({"code": "identity_schema_binding_pending", "detail": "Source identity factors do not match the API field enum."})
    nodes = {"export_blocked": _terminal("export_blocked"), "end": _terminal("end")}
    init_code = "\n".join([
        "vars['_export_policy_admitted'] = 'no'", "vars['_export_state'] = 'blocked'",
        "vars['_export_identity'] = 'pending'", "vars['_export_verified_fields'] = []",
        "vars['_export_products'] = []", "vars['_export_validation'] = 'pending'",
        "vars['_export_validated_bundle'] = []", "vars['_export_bundle_revision'] = ''",
        "vars['_export_confirmed_revision'] = ''", "vars['_export_recorded_products'] = []",
        "vars['_export_transfer_requested'] = 'no'", "vars['_export_transfer'] = 'pending'",
        "vars['_export_deferred_notes'] = []", "vars['_export_notes_ready'] = 'no'",
        "vars['_export_goodbye_done'] = 'no'", "vars['_export_outcome_ready'] = 'no'",
        "vars['_export_close_attempted'] = 'no'", "vars['_export_close_result'] = 'pending'",
    ]) + "\n"
    initial = plan.get("initial")
    plan_nodes = plan.get("nodes", [])
    ids = [node.get("id") for node in plan_nodes]
    if len(ids) != len(set(ids)) or initial not in ids or "end" not in ids or set(ids) & {"export_bootstrap", "export_blocked", "bank_close"}:
        raise FoundationError("Missing/duplicate/reserved plan node")
    nodes["export_bootstrap"] = _code_node("export_bootstrap", init_code, [
        _edge(initial, "_export_state", "ready", "Policy compilation admitted"),
        _edge("export_blocked", "_export_state", "blocked", "Uncompiled preview ends before bank operations")], "export_blocked")
    placements = []
    for node in plan_nodes:
        name = node["id"]
        if name == "end":
            if node.get("next") or node.get("bank_operations"):
                raise FoundationError("Plan end must be terminal")
            continue
        transitions = []
        for target in node.get("next", []):
            if target not in ids:
                raise FoundationError("Plan target missing")
            if target == "end" and name != "finalize":
                raise FoundationError("Only finalization can enter normal end")
            transitions.append(_edge("bank_close" if target == "end" else target,
                                     "_export_next_" + name, target, "Source-derived transition pending"))
            gaps.append({"code": "edge_semantics_pending", "node": name, "target": target,
                         "detail": "Abstract successor has no compiled predicate; route variable is intentionally not extracted."})
        attached = []
        for operation in node.get("bank_operations", []):
            if operation not in operations:
                raise FoundationError("Plan calls unknown bank operation")
            if operation == "close_call":
                if name != "finalize":
                    raise FoundationError("close_call outside finalization")
                continue
            if operation == "add_note" and facts["notes_enabled"] is False:
                continue
            if operation == "get_account" and name != "account":
                raise FoundationError("Account lookup outside identity-gated stage")
            attached.append(copy.deepcopy(tools[operation]))
            placements.append({"node": name, "operation": operation, "status": "guarded_preview_not_platform_verified"})
        task = node.get("purpose", "") + "\nFollow the supplied source policy. Do not claim an API operation succeeded without its business result."
        if not attached:
            task += " No tool call is allowed in this turn."
        nodes[name] = {"name": name, "node_type": "conversation", "on_enter": {"type": "llm"},
                       "task_message": task, "tools": attached, "transitions": transitions,
                       "max_node_turns": 10, "max_turn_transition": "export_blocked"}
    if "finalize" not in nodes or "close_call" not in next(n for n in plan_nodes if n["id"] == "finalize")["bank_operations"]:
        raise FoundationError("Plan must include explicit bank closure")
    close = copy.deepcopy(tools["close_call"])
    mapping = close.pop("response_mapping")
    nodes["bank_close"] = {"name": "bank_close", "node_type": "api_request", "on_enter": {"type": "silent"},
        "passthrough": True, "passthrough_default_transition": "export_blocked", "pre_actions": [close],
        "response_mapping": mapping, "transitions": [
            _edge("end", "_export_result_close_call", "succeeded", "Bank confirms closure"),
            _edge("export_blocked", "_export_result_close_call", "failed", "Bank closure failed") ]}
    placements.append({"node": "bank_close", "operation": "close_call", "status": "engine_action_after_farewell_pending"})
    policy_text = "\n\n".join(rule["text"] for rule in policy.get("requirements", []))
    workflow = {"initial_node": "export_bootstrap", "nodes": nodes,
                "system_prompt": "Speak English. Follow the supplied bank policy. Never invent a tool result or disclose account details before verified identity.\n\nSOURCE POLICY:\n" + policy_text}
    validation = validate_local_workflow(workflow)
    provenance = {"plan_sha256": _digest(plan), "policy_sha256": _digest(policy),
        "api_contract_sha256": _digest(contract), "source_inventory_sha256": _digest(sources),
        "sources": [{"source_id": source["source_id"], "sha256": source["sha256"], "role": source["role"]} for source in sources],
        "upstream_trace": copy.deepcopy(upstream_trace), "workflow_sha256": _digest(workflow)}
    format_basis = [{"source_id": record["source_id"], "sha256": record["sha256"],
                     "reference": record.get("path", "")}
                    for record in sources if Path(record.get("path", "")).name in {"workflow-format.md", "tools-and-code.md"}]
    return {"format_version": FORMAT_VERSION, "workflow": workflow, "tool_catalog": tools, "format_basis": format_basis,
            "validation": validation, "gaps": gaps, "provenance": provenance, "core_interface": copy.deepcopy(CORE_INTERFACE),
            "compilation_map": {"bank_placements": placements, "fully_compiled_rules": [],
                                "rule_text_in_prompt_is_not_semantic_coverage": True},
            "publish_allowed": False, "boot_gated": True, "network_calls": 0, "model_calls": 0, "api_key_read": False}


def export_directory(input_directory, output_directory, *, practice=False):
    """Read a hash-verified foundation snapshot; write only under artifacts/export-preview/."""
    source_dir = Path(input_directory).resolve()
    output = Path(output_directory).resolve()
    workspace = Path.cwd().resolve()
    allowed = (workspace / "artifacts" / "export-preview").resolve()
    if not allowed.is_relative_to(workspace) or output == allowed or not output.is_relative_to(allowed):
        raise FoundationError("Export outputs require a subdirectory of artifacts/export-preview")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FoundationError("Output contains files; preserve existing runs with a new directory")
    started = datetime.now(timezone.utc).isoformat()
    manifest_source = read_source(source_dir / "run-manifest.json")
    manifest = strict_json(manifest_source.text)
    needed = {"workflow-plan.json", "policy.ir.json", "api-contract.json", "sources.json", "trace.jsonl"}
    records = {record["path"]: record for record in manifest["artifacts"]}
    if not needed <= records.keys():
        raise FoundationError("Run manifest is missing export inputs")
    loaded, input_hashes = {}, {}
    for name in sorted(needed):
        record = records[name]
        source = read_source(source_dir / name, max_bytes=20_000_000)
        if source.sha256 != record["sha256"] or source.byte_count != record["bytes"]:
            raise FoundationError("Export input changed: " + name)
        input_hashes[name] = source.sha256
        if name != "trace.jsonl":
            loaded[name] = strict_json(source.text)
    arguments = (loaded["workflow-plan.json"], loaded["policy.ir.json"], loaded["api-contract.json"], loaded["sources.json"])
    upstream = {"reference": str(source_dir / "trace.jsonl"), "sha256": input_hashes["trace.jsonl"]}
    if practice:
        documents = []
        for source in loaded["sources.json"]:
            if source["role"] != "policy":
                continue
            name = "source-text/" + source["source_id"] + ".txt"
            if name not in records:
                raise FoundationError("Policy text missing from run manifest")
            text_source = read_source(source_dir / name)
            if text_source.sha256 != records[name]["sha256"] or text_source.byte_count != records[name]["bytes"]:
                raise FoundationError("Policy source snapshot changed")
            documents.append({key: source[key] for key in ("source_id", "path", "sha256", "byte_count", "kind", "status", "limitations")})
            documents[-1]["text"] = text_source.text
            input_hashes[name] = text_source.sha256
        core_dir = Path(__file__).resolve().parent
        core_sources = {name: (core_dir / name).read_text(encoding="utf-8") for name in
                        ("models.py", "calendar.py", "api_contract.py", "runtime.py", "policy_specs.py", "semantics.py", "turns.py", "conversation.py")}
        preview = export_practice_workflow(*arguments, upstream_trace=upstream, policy_documents=documents, core_sources=core_sources)
    else:
        preview = export_workflow(*arguments, upstream_trace=upstream)
    preview["provenance"]["input_file_sha256"] = input_hashes
    preview["provenance"]["upstream_manifest_sha256"] = manifest_source.sha256
    contents = {
        "candidate.workflow.json": preview["workflow"],
        "export-report.json": {key: value for key, value in preview.items() if key not in {"workflow", "tool_catalog", "runtime_evidence"}},
    }
    if "runtime_evidence" in preview:
        contents["runtime-evidence.json"] = preview["runtime_evidence"]
        contents["code-budget.json"] = preview["code_budget"]
    if "readiness" in preview:
        contents["practice-ready.json"] = {**preview["readiness"],
            "candidate_workflow_sha256": preview["provenance"]["workflow_sha256"],
            "provenance": preview["provenance"]}
    if "tool_catalog" in preview:
        contents["tool-catalog.json"] = preview["tool_catalog"]
    raws = {name: (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") for name, value in contents.items()}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in raws.items()}
    trace = [{"stage": "start", "utc": started, "purpose": "Compile the reviewed practice replay candidate" if practice else "Compile a blocked local Freya-format preview"},
             {"stage": "inputs_verified", "input_sha256": input_hashes},
             {"stage": "export", "artifact_sha256": hashes, "canonical_workflow_sha256": preview["provenance"]["workflow_sha256"]},
             {"stage": "complete", "utc": datetime.now(timezone.utc).isoformat(), "network_calls": 0,
              "model_calls": 0, "api_key_read": False, "publish_allowed": preview["publish_allowed"]}]
    trace[-1]["boot_gated"] = preview["boot_gated"]
    trace[-1]["practice_only"] = preview.get("practice_only", False)
    trace[-1]["publication_scope"] = preview.get("publication_scope", "blocked_preview")
    trace[-1]["final_submission_ready"] = False
    raws["trace.jsonl"] = ("".join(_json(event) + "\n" for event in trace)).encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    for name, raw in raws.items():
        with (output / name).open("xb") as stream:
            stream.write(raw)
    return {"output": str(output), "files": sorted(raws), "publish_allowed": preview["publish_allowed"], "boot_gated": preview["boot_gated"],
            "publication_scope": preview.get("publication_scope", "blocked_preview"),
            "ready_for_platform_preflight": preview.get("ready_for_platform_preflight", False), "final_submission_ready": False,
            "executable_candidate": preview.get("executable_candidate", False), "practice_only": preview.get("practice_only", False),
            "local_documented_subset_valid": True, "unresolved_items": len(preview["gaps"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a blocked offline Freya preview from foundation artifacts")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--practice", action="store_true", help="Bind the reviewed practice conversation engine; platform execution remains unverified")
    arguments = parser.parse_args(argv)
    try:
        print(json.dumps(export_directory(arguments.input, arguments.out, practice=arguments.practice), ensure_ascii=False))
        return 0
    except (FoundationError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc), "publish_allowed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
