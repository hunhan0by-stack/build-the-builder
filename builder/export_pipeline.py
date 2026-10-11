"""Offline generation -> snapshot verification -> export -> sealed roundtrip.

Public integration points:
  generate_export(input_directory=..., output_directory=..., mode='practice')
  generate_export(brief_paths=..., tool_api_path=..., output_directory=...)
  verify_export_run(directory, require_current_sources=False)

General lowering accepts lower_compilation(envelope, documents, contract).
CompiledPolicy's typed session is not a supported general Conversation emitter.
The repair handoff pins NEW generated workflow bytes and explicit source snapshots;
it never repairs an already sealed artifact or changes evaluator expectations.

Only verify_export_run's current result grants scoped practice preflight eligibility.
A report without a valid COMPLETE commit, manifest, trace, smoke and roundtrip is
publication-blocked. This module does not publish, call models or read credentials.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib
import json
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .api_contract import BankContract, Operation
from .freya_export import (CODE_EXEC_SAFE_BYTES, code_exec_request_bytes, measure_code_exec_budget,
                           _compact_practice_capsule, export_practice_workflow, export_workflow, validate_local_workflow)
from .ingest import PROTECTED_DIRECTORIES, collect_sources, read_source
from .models import Document, FoundationError, strict_json
from .pipeline import build


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_OUTPUT = ROOT / "artifacts/export-pipeline"
VERSION = "offline-export-product-1"
MAX_BYTES = 20_000_000
CORE_FILES = ("models.py", "calendar.py", "api_contract.py", "runtime.py", "policy_specs.py",
              "semantics.py", "turns.py", "conversation.py")
IMPLEMENTATION_FILES = CORE_FILES + ("freya_export.py", "pipeline.py", "plan.py", "extract.py",
                                   "ingest.py", "validate.py", "export_pipeline.py")
GENERAL_IMPLEMENTATION = ("builder/compiled_policy.py", "compiler/core.py", "compiler/schema.py")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _relative(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise FoundationError("Artifact name must be a portable relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {".", "..", ""} for part in name.split("/")):
        raise FoundationError("Artifact path traversal rejected")
    if set(part.lower() for part in path.parts) & PROTECTED_DIRECTORIES or path.name.lower().startswith(".env"):
        raise FoundationError("Protected artifact path rejected")
    return name


def _artifact_path(directory, name):
    name = _relative(name)
    base = Path(directory).resolve()
    supplied = base / name
    resolved = supplied.resolve()
    if not resolved.is_relative_to(base) or any(part.is_symlink() for part in (supplied, *supplied.parents) if part != base.parent):
        raise FoundationError("Artifact escapes its snapshot or uses a symbolic link")
    return resolved


def _read_bytes(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise FoundationError("Missing or oversized artifact: " + path.name)
    return path.read_bytes()


def _load_json(path):
    return strict_json(_read_bytes(path).decode("utf-8"))


def _write_artifact(directory, name, raw):
    target = _artifact_path(directory, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(raw)
        stream.flush()


def _inventory(directory):
    result = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            if name in {"manifest.json", "COMPLETE.json", "FAILED.json"}:
                continue
            raw = _read_bytes(_artifact_path(directory, name))
            result.append({"path": name, "bytes": len(raw), "sha256": _sha(raw)})
    return result


def _foundation_snapshot(directory, *, general=False):
    """Admit every declared foundation artifact before making an export package."""
    directory = Path(directory).resolve()
    # Preserve source admission rules for the entry path; no config/secret readers.
    manifest_source = read_source(directory / "run-manifest.json")
    manifest_raw = _read_bytes(directory / "run-manifest.json")
    manifest = strict_json(manifest_source.text)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise FoundationError("Foundation artifact inventory is missing")
    records, raw_files = {}, {"run-manifest.json": manifest_raw}
    for record in artifacts:
        name = _relative(record["path"])
        if name in records or name == "run-manifest.json":
            raise FoundationError("Duplicate/reserved foundation artifact")
        path = _artifact_path(directory, name)
        read_source(path)  # Refuse potential credential filenames before raw reading.
        raw = _read_bytes(path)
        if len(raw) != record["bytes"] or _sha(raw) != record["sha256"]:
            raise FoundationError("Foundation artifact hash mismatch: " + name)
        records[name], raw_files[name] = record, raw
    required = {"sources.json", "api-contract.json", "trace.jsonl"}
    if not general:
        required |= {"policy.ir.json", "workflow-plan.json"}
    if not required <= records.keys():
        raise FoundationError("Foundation is missing required export inputs")
    loaded = {name: strict_json(raw_files[name].decode("utf-8")) for name in required if name.endswith(".json")}
    trace = [strict_json(line) for line in raw_files["trace.jsonl"].decode("utf-8").splitlines() if line]
    if not trace or trace[-1].get("stage") != "complete":
        raise FoundationError("Foundation has no completed generation trace")
    sources = loaded["sources.json"]
    ids = [item["source_id"] for item in sources]
    if len(ids) != len(set(ids)):
        raise FoundationError("Duplicate foundation source IDs")
    docs = []
    for item in sources:
        if item["role"] != "policy":
            continue
        name = "source-text/" + item["source_id"] + ".txt"
        if name not in raw_files:
            raise FoundationError("Policy source text is missing from foundation")
        record = {key: item[key] for key in ("source_id", "path", "sha256", "byte_count", "kind", "status", "limitations")}
        record["text"] = raw_files[name].decode("utf-8")
        docs.append(record)
    if not docs:
        raise FoundationError("Foundation has no policy documents")
    return raw_files, loaded, docs


def _generated_general_inputs(brief_paths, tool_api_path, directory):
    """Ingest new briefs for the typed lowering adapter without the practice parser."""
    docs = collect_sources([Path(path) for path in brief_paths])
    if not docs or any(doc.status != "processed" for doc in docs):
        raise FoundationError("General lowering requires processed explicit policy sources")
    api_source = read_source(Path(tool_api_path))
    contract = BankContract(strict_json(api_source.text), api_source.source_id, api_source.sha256)
    sources = [{**doc.inventory(), "role": "policy"} for doc in docs]
    sources.append({**api_source.inventory(), "role": "api_contract"})
    files = {"sources.json": _json_bytes(sources), "api-contract.json": _json_bytes(contract.serializable()),
             "trace.jsonl": _canonical({"stage": "ingest", "utc": _utc(), "mode": "offline"}) + b"\n" +
                             _canonical({"stage": "complete", "utc": _utc(), "network_calls": 0, "model_calls": 0}) + b"\n"}
    for doc in docs + [api_source]:
        files["source-text/" + doc.source_id + ".txt"] = doc.text.encode("utf-8")
    for name, raw in files.items():
        _write_artifact(directory, name, raw)
    _write_artifact(directory, "run-manifest.json", _json_bytes({"schema_version": "general-lowering-input-1",
        "publish_allowed": False, "artifacts": [{"path": name, "bytes": len(raw), "sha256": _sha(raw)} for name, raw in sorted(files.items())]}))


def _contract(serialized):
    result = BankContract.__new__(BankContract)
    result.source_id, result.source_hash = serialized["source_id"], serialized["source_sha256"]
    result.server = serialized["server"]
    result.operations = {name: Operation(**item) for name, item in serialized["operations"].items()}
    return result


def _lower_general(envelope, documents, contract, adapter=None):
    if adapter is None:
        try:
            adapter = importlib.import_module("builder.compiled_policy").lower_compilation
        except (ImportError, AttributeError):
            return None, {"code": "general_lowering_adapter_unavailable", "detail": "lower_compilation adapter is not installed; no executable general export was created"}
    if envelope is None:
        return None, {"code": "compilation_envelope_missing", "detail": "A source-evidenced compilation envelope must be supplied; no model or inferred policy is substituted"}
    compiled = adapter(copy.deepcopy(envelope), [Document(**item) for item in documents], _contract(contract))
    def serialized(value):
        if hasattr(value, "serializable"):
            return value.serializable()
        return asdict(value) if is_dataclass(value) else value
    lowered = {name: serialized(getattr(compiled, name)) for name in ("policy", "semantics", "plan", "coverage")}
    if hasattr(compiled, "constraints"):
        lowered["constraints"] = serialized(compiled.constraints)
    _canonical(lowered)  # Reject nonserializable/NaN lowering results.
    lowered["typed_create_session_available"] = callable(getattr(compiled, "create_session", None))
    return lowered, {"code": "general_conversation_export_unsupported",
        "detail": "Typed CompiledPolicy lowering is available, but generic Conversation -> Freya export has no admitted emitter; final_submission_ready=false"}


def _smoke(workflow, contract, policy, *, deadline_monotonic=None):
    """Execute exact generated Python with synthetic responses; never a platform judge."""
    nodes = workflow["nodes"]
    state = {"customer_id": "synthetic_export_smoke", "first_name": "Dana", "last_name": "Smith", "title": "Ms", "now": "2026-10-14T11:00"}
    events = []
    byte_measurements = []
    def code(name):
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise FoundationError("Shared export budget exhausted before generated step")
        snippet = nodes[name]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
        size = len(code_exec_request_bytes(snippet, state))
        byte_measurements.append({"node": name, "serialized_request_bytes": size})
        if size > CODE_EXEC_SAFE_BYTES:
            raise FoundationError("Exact generated code+vars exceeds safe request budget")
        exec(snippet, {"vars": state, "__name__": "__main__"})
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise FoundationError("Shared export budget exhausted during generated step")
    def drive():
        code("bridge_drive")
        if state.get("_bridge_speech"):
            events.append({"kind": "speech", "text": state["_bridge_speech"]})
        if state.get("_bridge_fault") or state.get("_bridge_route") == "bridge_fault":
            raise FoundationError("Exact generated driver smoke entered fault")
    def turn(values):
        state["_bridge_turn_json"] = json.dumps(values)
        events.append({"kind": "customer", "turn": values})
        code("bridge_ingest_turn")
        if state.get("_bridge_ingest") != "succeeded":
            raise FoundationError("Generated turn ingestion failed")
        drive()
    def response(operation, data):
        if state.get("_bridge_route") != "bank_" + operation:
            raise FoundationError("Generated smoke requested the wrong bank operation")
        pending = strict_json(state["_bridge_pending_json"])
        tool = nodes["bank_" + operation]["pre_actions"][0]
        config, result = {"body": copy.deepcopy(pending["payload"])}, {"data": copy.deepcopy(data)}
        exec(tool["pre_process_code"], {"vars": state, "config": config})
        if config["body"] != pending["payload"]:
            raise FoundationError("Pre-hook changed the guarded request")
        _contract(contract).validate_request(operation, config["body"])
        exec(tool["post_process_code"], {"vars": state, "result": result})
        mappings = nodes["bank_" + operation]["response_mapping"]
        if mappings != [{"path": "data._bridge_payload", "variable": "_bridge_http_response"}]:
            raise FoundationError("Generated response mapping adapter differs")
        state["_bridge_http_response"] = result["data"].get("_bridge_payload")
        events.append({"kind": "bank", "operation": operation, "payload": config["body"], "response": data})
        code("bridge_observe")
        if state.get("_bridge_response_state") != "succeeded":
            raise FoundationError("Generated response observation failed")
        drive()
    code("bridge_init")
    drive()
    if state.get("_bridge_phase") != "person" or not events or "call is recorded" not in events[0]["text"]:
        raise FoundationError("Generated opening disclosure missing")
    turn({"intents": ["yes"]})
    for field, answer in (("date_of_birth", "1990-01-01"), ("phone_last4", "1234")):
        turn({"intents": ["identity_answer"], "answer": answer})
        if strict_json(state["_bridge_pending_json"])["payload"]["field"] != field:
            raise FoundationError("Generated identity order mismatch")
        response("verify_identity", {"ok": True, "verified": True})
    record = {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 400,
              "days_late": policy["facts"]["bands"]["value"][0]["min_days_late"], "minimum_payment": 100}
    response("get_account", {"ok": True, "products": [record]})
    turn({"intents": ["yes"]})
    if state.get("_bridge_phase") != "readback":
        raise FoundationError("Generated smoke skipped readback")
    turn({"intents": ["yes"]})
    response("create_promise", {"ok": True, "promise_id": "synthetic_smoke_promise"})
    response("close_call", {"ok": True})
    bank_events = [event for event in events if event["kind"] == "bank"]
    expected = ["verify_identity", "verify_identity", "get_account", "create_promise", "close_call"]
    if state.get("_bridge_route") != "bridge_end" or [event["operation"] for event in bank_events] != expected:
        raise FoundationError("Generated smoke did not close once and last")
    if bank_events[3]["payload"] != {"customer_id": state["customer_id"], "product_id": "p1", "amount": 400.0, "date": "2026-10-14"} or bank_events[4]["payload"] != {"customer_id": state["customer_id"], "outcome": "promise"}:
        raise FoundationError("Independent synthetic outcome check failed")
    return {"kind": "exact_generated_python_synthetic_smoke", "passed": True,
            "canonical_workflow_sha256": _sha(_canonical(workflow)), "events": events,
            "code_exec_byte_budget": {"safe_limit_bytes": CODE_EXEC_SAFE_BYTES,
                "peak_serialized_request_bytes": max(item["serialized_request_bytes"] for item in byte_measurements),
                "steps": byte_measurements},
            "network_calls": 0, "model_calls": 0, "official_cases_evaluated": 0,
            "scope": "One independent synthetic happy path; not all rule clauses, extraction model or platform behavior"}


def generate_export(*, output_directory, input_directory=None, brief_paths=(), tool_api_path=None,
                    context_paths=(), cases_path=None, mode="practice", compilation_envelope=None,
                    lowering_adapter=None, deadline_seconds=120):
    """Create a NEW local package. Existing/failed outputs are never overwritten."""
    if mode not in {"practice", "preview", "general"}:
        raise FoundationError("Unknown export mode")
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 1800:
        raise FoundationError("Export deadline must be 1..1800 seconds")
    allowed, output = ALLOWED_OUTPUT.resolve(), Path(output_directory).resolve()
    if not allowed.is_relative_to(ROOT) or output == allowed or not output.is_relative_to(allowed) or output.exists():
        raise FoundationError("Use a fresh child directory under artifacts/export-pipeline")
    if input_directory is not None and (brief_paths or tool_api_path or context_paths or cases_path):
        raise FoundationError("Choose either an existing foundation or explicit generation sources")
    if input_directory is None and (not brief_paths or tool_api_path is None):
        raise FoundationError("Explicit briefs and a tool API are required for generation")
    started, start_utc = time.monotonic(), _utc()
    deadline = started + deadline_seconds
    # Early admission for reused inputs means corrupt old data creates no new package.
    admitted = _foundation_snapshot(input_directory, general=mode == "general") if input_directory is not None else None
    original_sources = {str(ROOT / "builder" / name): (ROOT / "builder" / name).read_bytes() for name in IMPLEMENTATION_FILES}
    if mode == "general" and lowering_adapter is None:
        for name in GENERAL_IMPLEMENTATION:
            if (ROOT / name).is_file():
                original_sources[str(ROOT / name)] = (ROOT / name).read_bytes()
    core_sources = {name: original_sources[str(ROOT / "builder" / name)].decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") for name in CORE_FILES}
    if time.monotonic() > deadline:
        raise FoundationError("Export product budget exhausted during input admission")
    run_id = _sha(_canonical({"started": start_utc, "output": str(output)}))[:24]
    output.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        _write_artifact(output, name, _json_bytes(value))
    def record(stage, **details):
        if time.monotonic() - started > deadline_seconds:
            raise FoundationError("Export product time budget exhausted")
        event = {"seq": 1 + record.count, "stage": stage, "utc": _utc(), "elapsed_ms": round((time.monotonic() - started) * 1000), **details}
        with (output / "trace.jsonl").open("ab") as stream:
            stream.write(_canonical(event) + b"\n")
            stream.flush()
        record.count += 1
    record.count = 0
    try:
        write("RUNNING.json", {"run_id": run_id, "started_utc": start_utc, "publish_allowed": False})
        record("start", mode=mode, run_id=run_id, publish_allowed=False)
        if admitted is None:
            generation = output / "generation"
            if mode == "general":
                _generated_general_inputs(brief_paths, tool_api_path, generation)
            else:
                build(brief_paths=brief_paths, tool_api_path=tool_api_path, output_path=generation,
                      context_paths=context_paths, cases_path=cases_path, deadline_seconds=deadline_seconds)
            admitted = _foundation_snapshot(generation, general=mode == "general")
        raw_files, loaded, documents = admitted
        for name, raw in raw_files.items():
            _write_artifact(output, "snapshot/" + name, raw)
        for path, raw in original_sources.items():
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            relative = Path(path).relative_to(ROOT).as_posix()
            stored_name = Path(path).name if Path(path).parent == ROOT / "builder" else relative
            _write_artifact(output, "implementation/" + stored_name, text.encode("utf-8"))
        snapshot_hashes = {name: _sha(raw) for name, raw in raw_files.items()}
        record("generation_verified", snapshot_sha256=snapshot_hashes)
        upstream = {"reference": "snapshot/trace.jsonl", "sha256": snapshot_hashes["trace.jsonl"]}
        candidate, gate = None, False
        if mode == "general":
            if compilation_envelope is not None:
                write("compilation-envelope.json", compilation_envelope)
            lowered, diagnostic = _lower_general(compilation_envelope, documents, loaded["api-contract.json"], lowering_adapter)
            if lowered is not None:
                write("compiled-policy.json", lowered)
            report = {"mode": mode, "status": "general_executable_export_unsupported", "diagnostic": diagnostic,
                      "lowering_completed": lowered is not None, "publish_allowed": False, "executable_candidate": False,
                      "general_conversation_emitter_supported": False, "final_submission_ready": False}
            if lowered is not None:
                report["lowering_coverage"] = lowered["coverage"]
                report["lowered_semantics"] = lowered["semantics"]
                report["typed_create_session_available"] = lowered["typed_create_session_available"]
            record("unsupported_export", diagnostic_code=diagnostic["code"], executable_candidate=False)
        else:
            arguments = (loaded["workflow-plan.json"], loaded["policy.ir.json"], loaded["api-contract.json"], loaded["sources.json"])
            if mode == "practice":
                produced = export_practice_workflow(*arguments, upstream_trace=upstream, policy_documents=documents, core_sources=core_sources)
            else:
                produced = export_workflow(*arguments, upstream_trace=upstream)
            candidate = produced["workflow"]
            if mode == "practice":
                write("runtime-evidence.json", produced["runtime_evidence"])
                write("code-budget.json", produced["code_budget"])
            canonical_hash = _sha(_canonical(candidate))
            if produced["provenance"]["workflow_sha256"] != canonical_hash:
                raise FoundationError("Producer workflow/provenance mismatch")
            write("candidate.workflow.json", candidate)
            record("export_generated", canonical_workflow_sha256=canonical_hash)
            reread = _load_json(output / "candidate.workflow.json")
            validate_local_workflow(reread)
            # Different serialization, identical semantics. Do not repair any source.
            _write_artifact(output, "roundtrip.workflow.json", _canonical(reread) + b"\n")
            roundtrip_hash = _sha(_canonical(_load_json(output / "roundtrip.workflow.json")))
            if roundtrip_hash != canonical_hash:
                raise FoundationError("Export roundtrip semantic divergence")
            roundtrip = {"passed": True, "candidate_canonical_sha256": canonical_hash,
                         "roundtrip_canonical_sha256": roundtrip_hash, "policy_rewritten": False}
            write("roundtrip-check.json", roundtrip)
            record("roundtrip_verified", canonical_workflow_sha256=canonical_hash)
            if mode == "practice":
                smoke = _smoke(reread, loaded["api-contract.json"], loaded["policy.ir.json"], deadline_monotonic=deadline)
                write("smoke-check.json", smoke)
                record("smoke_verified", canonical_workflow_sha256=canonical_hash, test_scope="synthetic_local_generated_python")
                gate = produced.get("publish_allowed") is True and smoke["passed"] is True
            else:
                write("smoke-check.json", {"passed": False, "not_run": True, "reason": "Uncompiled preview is intentionally boot-gated", "network_calls": 0, "model_calls": 0})
            producer_metadata = {key: value for key, value in produced.items() if key not in {"workflow", "tool_catalog", "publish_allowed", "runtime_evidence"}}
            producer_metadata["producer_preflight_eligibility"] = produced.get("publish_allowed") is True
            report = {"mode": mode, "status": "verification_required", "producer": producer_metadata,
                      "publish_allowed": False, "eligibility_after_complete_verification": gate,
                      "executable_candidate": mode == "practice", "final_submission_ready": False,
                      "publication_scope": "controlled_synthetic_practice_pilot_only" if gate else "blocked_preview",
                      "candidate_canonical_sha256": canonical_hash}
        report["network_calls"], report["model_calls"], report["api_key_read"] = 0, 0, False
        report["verification_authority"] = "verify_export_run must validate COMPLETE, manifest, trace, smoke and roundtrip before publication eligibility"
        write("export-report.json", report)
        # Pin inputs for a separate repair cycle without invoking it or changing its output root.
        if candidate is not None:
            repair_sources = {str(output / "snapshot" / name): digest for name, digest in snapshot_hashes.items()}
            for path, raw in original_sources.items():
                relative = Path(path).relative_to(ROOT).as_posix()
                stored_name = Path(path).name if Path(path).parent == ROOT / "builder" else relative
                normalized = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                repair_sources[str(output / "implementation" / stored_name)] = _sha(normalized)
            write("repair-handoff.json", {"workflow_path": str(output / "candidate.workflow.json"),
                "trusted_workflow_sha256": _sha(_read_bytes(output / "candidate.workflow.json")),
                "source_snapshot": repair_sources,
                "reference_scope": "software-pinned generator output; not an independently notarized policy truth",
                "repair_existing_sealed_evidence_allowed": False, "semantic_repair_provider": "unsupported",
                "runner": "orchestration.repair_cycle.run_cycle", "max_attempts": 2,
                "budget_seconds": deadline_seconds, "deadline_monotonic": deadline,
                "remaining_limit_seconds_at_handoff": max(0.0, deadline - time.monotonic()),
                "output_root_required_by_runner": "artifacts/repair-cycle",
                "invoked_by_this_pipeline": False,
                "reason": "Repair runner owns a different output root; caller may dispatch this pinned handoff within the same shared deadline"})
        if any(_read_bytes(path) != raw for path, raw in original_sources.items()):
            raise FoundationError("Implementation source changed during export; mixed snapshots rejected")
        record("sources_rechecked", implementation_snapshots_stable=True)
        record("complete", mode=mode, publish_allowed=False, eligibility_after_commit=gate,
               final_submission_ready=False, network_calls=0, model_calls=0, api_key_read=False)
        artifacts = _inventory(output)
        manifest = {"version": VERSION, "run_id": run_id, "mode": mode, "artifacts": artifacts,
                    "original_implementation_sha256": {path: _sha(raw) for path, raw in original_sources.items()},
                    "budget_seconds": deadline_seconds,
                    "publish_allowed": False, "eligibility_after_complete_verification": gate,
                    "final_submission_ready": False, "network_calls": 0, "model_calls": 0}
        write("manifest.json", manifest)
        # Verify the would-be committed package before creating the last, exclusive marker.
        checked = _verify_package(output, manifest, require_complete=False, deadline_monotonic=deadline)
        if time.monotonic() > deadline:
            raise FoundationError("Shared export budget exhausted before completion commit")
        manifest_sha256 = _sha(_read_bytes(output / "manifest.json"))
        write("COMPLETE.json", {"version": VERSION, "run_id": run_id,
            "manifest_sha256": manifest_sha256, "finished_utc": _utc(),
            "publish_allowed": False, "verification_required": True})
        _check_commit(output, manifest)
        return {**checked, "publish_allowed": gate, "ready_for_platform_preflight": gate,
                "manifest_sha256": manifest_sha256, "output_directory": str(output), "independent_root_verified": False}
    except BaseException as exc:
        try:
            write("FAILED.json", {"run_id": run_id, "status": "failed_or_interrupted", "error_type": type(exc).__name__,
                                  "publish_allowed": False, "final_submission_ready": False})
        except OSError:
            pass
        raise


def _check_commit(directory, manifest):
    commit = _load_json(directory / "COMPLETE.json")
    if commit.get("version") != VERSION or commit.get("run_id") != manifest.get("run_id") or commit.get("manifest_sha256") != _sha(_read_bytes(directory / "manifest.json")):
        raise FoundationError("Completion marker does not match the manifest")


def _verify_package(directory, manifest, *, require_complete=True, require_current_sources=False, deadline_monotonic=None):
    directory = Path(directory).resolve()
    if manifest.get("version") != VERSION or manifest.get("final_submission_ready") is not False:
        raise FoundationError("Unsupported or misleading export manifest")
    if (directory / "FAILED.json").exists():
        raise FoundationError("Failed/interrupted package cannot be published")
    if require_complete:
        _check_commit(directory, manifest)
    inventory, names = manifest.get("artifacts", []), set()
    if not inventory:
        raise FoundationError("Package inventory missing")
    for record in inventory:
        name = _relative(record["path"])
        if name in names or name in {"manifest.json", "COMPLETE.json", "FAILED.json"}:
            raise FoundationError("Duplicate/reserved package inventory entry")
        names.add(name)
        raw = _read_bytes(_artifact_path(directory, name))
        if len(raw) != record["bytes"] or _sha(raw) != record["sha256"]:
            raise FoundationError("Export artifact changed: " + name)
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()} - {"manifest.json", "COMPLETE.json"}
    if actual != names:
        raise FoundationError("Package contains missing or untracked evidence")
    required = {"RUNNING.json", "export-report.json", "trace.jsonl", "snapshot/run-manifest.json"}
    if not required <= names:
        raise FoundationError("Essential export evidence missing")
    running = _load_json(directory / "RUNNING.json")
    if running.get("run_id") != manifest.get("run_id"):
        raise FoundationError("Start marker belongs to another run")
    trace = [strict_json(line) for line in _read_bytes(directory / "trace.jsonl").decode("utf-8").splitlines() if line]
    if not trace or trace[0].get("stage") != "start" or trace[-1].get("stage") != "complete" or any(event.get("seq") != index for index, event in enumerate(trace, 1)):
        raise FoundationError("Export trace is incomplete or out of order")
    if trace[0].get("run_id") != manifest.get("run_id"):
        raise FoundationError("Export trace belongs to another run")
    raw_files, loaded, _ = _foundation_snapshot(directory / "snapshot", general=manifest["mode"] == "general")
    generation = next((event for event in trace if event["stage"] == "generation_verified"), {})
    if generation.get("snapshot_sha256") != {name: _sha(raw) for name, raw in raw_files.items()}:
        raise FoundationError("Snapshot evidence is not bound to the export trace")
    report = _load_json(directory / "export-report.json")
    if report.get("final_submission_ready") is not False or report.get("mode") != manifest["mode"]:
        raise FoundationError("Report readiness/scope mismatch")
    eligible = False
    if manifest["mode"] in {"practice", "preview"}:
        essential = {"candidate.workflow.json", "roundtrip.workflow.json", "roundtrip-check.json", "smoke-check.json", "repair-handoff.json"}
        if not essential <= names:
            raise FoundationError("Candidate test/roundtrip evidence missing")
        workflow = _load_json(directory / "candidate.workflow.json")
        validate_local_workflow(workflow)
        digest = _sha(_canonical(workflow))
        roundtrip = _load_json(directory / "roundtrip-check.json")
        if _sha(_canonical(_load_json(directory / "roundtrip.workflow.json"))) != digest or roundtrip != {"passed": True, "candidate_canonical_sha256": digest, "roundtrip_canonical_sha256": digest, "policy_rewritten": False}:
            raise FoundationError("Roundtrip proof differs from the candidate")
        if report.get("candidate_canonical_sha256") != digest or report["producer"]["provenance"]["workflow_sha256"] != digest:
            raise FoundationError("Candidate/report provenance mismatch")
        provenance = report["producer"]["provenance"]
        for key, filename in (("plan_sha256", "workflow-plan.json"), ("policy_sha256", "policy.ir.json"),
                              ("api_contract_sha256", "api-contract.json"), ("source_inventory_sha256", "sources.json")):
            if provenance.get(key) != _sha(_canonical(loaded[filename])):
                raise FoundationError("Producer source provenance differs from sealed snapshot: " + filename)
        upstream = report["producer"]["provenance"]["upstream_trace"]
        if upstream != {"reference": "snapshot/trace.jsonl", "sha256": _sha(raw_files["trace.jsonl"])}:
            raise FoundationError("Candidate references another generation trace")
        exported = [event for event in trace if event["stage"] in {"export_generated", "roundtrip_verified", "smoke_verified"}]
        if any(event.get("canonical_workflow_sha256") != digest for event in exported):
            raise FoundationError("Artifact hash differs from the run trace")
        handoff = _load_json(directory / "repair-handoff.json")
        if Path(handoff.get("workflow_path", "")).name != "candidate.workflow.json" or handoff.get("trusted_workflow_sha256") != _sha(_read_bytes(directory / "candidate.workflow.json")) or handoff.get("repair_existing_sealed_evidence_allowed") is not False:
            raise FoundationError("Repair handoff does not pin this generated candidate")
        if manifest["mode"] == "practice":
            if not {"runtime-evidence.json", "code-budget.json"} <= names:
                raise FoundationError("Practice byte budget or external source evidence is missing")
            budget = _load_json(directory / "code-budget.json")
            if budget != measure_code_exec_budget(workflow) or budget != report["producer"].get("code_budget") or budget.get("passed") is not True:
                raise FoundationError("Practice serialized code+vars byte budget mismatch")
            evidence = _load_json(directory / "runtime-evidence.json")
            _, _, documents = _foundation_snapshot(directory / "snapshot")
            if evidence.get("policy") != loaded["policy.ir.json"] or evidence.get("contract") != loaded["api-contract.json"] or evidence.get("documents") != documents:
                raise FoundationError("External runtime evidence differs from source snapshot")
            expected_capsule = _compact_practice_capsule(loaded["policy.ir.json"], loaded["api-contract.json"], documents)
            driver = workflow["nodes"]["bridge_drive"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
            assignments = [node for node in ast.walk(ast.parse(driver)) if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "_bridge_config" for target in node.targets)]
            if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Call) or len(assignments[0].value.args) != 1 or not isinstance(assignments[0].value.args[0], ast.Constant):
                raise FoundationError("Runtime capsule cannot be read independently")
            configuration = strict_json(assignments[0].value.args[0].value)
            if configuration != {"capsule": expected_capsule, "capsule_sha256": _sha(_canonical(expected_capsule)), "evidence_sha256": _sha(_canonical(evidence))}:
                raise FoundationError("Runtime capsule is not bound to external source evidence")
            if provenance.get("runtime_capsule_sha256") != configuration["capsule_sha256"] or provenance.get("runtime_evidence_sha256") != configuration["evidence_sha256"]:
                raise FoundationError("Runtime capsule provenance differs from snapshot")
            smoke = _load_json(directory / "smoke-check.json")
            # Recompute from exact artifact bytes; PASS text alone is never enough.
            recomputed = _smoke(workflow, loaded["api-contract.json"], loaded["policy.ir.json"], deadline_monotonic=deadline_monotonic)
            if smoke != recomputed:
                raise FoundationError("Smoke evidence fails independent recomputation")
            snapshots = report["producer"]["provenance"]["core_source_sha256"]
            if set(snapshots) != set(CORE_FILES) or any(_sha(_read_bytes(directory / "implementation" / name)) != expected for name, expected in snapshots.items()):
                raise FoundationError("Executable candidate core snapshots are mismatched")
            eligible = report.get("eligibility_after_complete_verification") is True and report.get("publication_scope") == "controlled_synthetic_practice_pilot_only"
    elif report.get("executable_candidate") is not False or report.get("publish_allowed") is not False:
        raise FoundationError("Unsupported general export claims executable readiness")
    if manifest.get("eligibility_after_complete_verification") is not eligible:
        raise FoundationError("Manifest publication eligibility contradicts export evidence")
    if require_current_sources:
        for path, digest in manifest["original_implementation_sha256"].items():
            admitted = Path(path).resolve()
            known = (admitted.parent == ROOT / "builder" and admitted.name in IMPLEMENTATION_FILES) or admitted in {ROOT / name for name in GENERAL_IMPLEMENTATION}
            if not known or _sha(_read_bytes(admitted)) != digest:
                raise FoundationError("Current implementation no longer matches the sealed snapshot")
    return {"status": "verified" if eligible else "verified_blocked", "run_id": manifest["run_id"],
            "mode": manifest["mode"], "artifact_hashes_valid": True, "snapshot_trace_bound": True,
            "publish_allowed": eligible and require_complete, "publication_scope": "controlled_synthetic_practice_pilot_only" if eligible else "blocked",
            "ready_for_platform_preflight": eligible and require_complete, "final_submission_ready": False,
            "simulation_allowed_before_platform_preflight": False, "platform_validated": False,
            "network_calls": 0, "model_calls": 0, "api_key_read": False, "official_cases_evaluated": 0,
            "checked_artifacts": len(names)}


def verify_export_run(directory, *, require_current_sources=False, trusted_manifest_sha256=None,
                      deadline_monotonic=None):
    """Read-only, idempotent verifier. Missing/altered/interrupted evidence fails closed."""
    try:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise FoundationError("Shared export verification budget exhausted before verification")
        directory = Path(directory).resolve()
        if directory == ALLOWED_OUTPUT.resolve() or not directory.is_relative_to(ALLOWED_OUTPUT.resolve()):
            raise FoundationError("Verify only export-pipeline packages")
        manifest = _load_json(directory / "manifest.json")
        manifest_hash = _sha(_read_bytes(directory / "manifest.json"))
        if trusted_manifest_sha256 is not None and (not isinstance(trusted_manifest_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", trusted_manifest_sha256) or manifest_hash != trusted_manifest_sha256):
            raise FoundationError("Pinned manifest root mismatch")
        checked = _verify_package(directory, manifest, require_current_sources=require_current_sources,
                                  deadline_monotonic=deadline_monotonic)
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise FoundationError("Shared export verification budget exhausted before returning eligibility")
        return {**checked,
                "output_directory": str(directory), "manifest_sha256": manifest_hash,
                "caller_pinned_root_verified": trusted_manifest_sha256 is not None, "independent_root_verified": False}
    except (FoundationError, OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "blocked", "diagnostic": str(exc), "publish_allowed": False, "final_submission_ready": False,
                "network_calls": 0, "model_calls": 0, "api_key_read": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline immutable generation/verify/export product pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--input")
    run.add_argument("--brief", action="append", default=[])
    run.add_argument("--tool-api")
    run.add_argument("--context", action="append", default=[])
    run.add_argument("--cases")
    run.add_argument("--mode", choices=("practice", "preview", "general"), default="practice")
    run.add_argument("--compilation-envelope")
    run.add_argument("--out", required=True)
    run.add_argument("--deadline-seconds", type=int, default=120)
    verify = commands.add_parser("verify")
    verify.add_argument("directory")
    verify.add_argument("--require-current-sources", action="store_true")
    verify.add_argument("--trusted-manifest-sha256")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify":
            result = verify_export_run(arguments.directory, require_current_sources=arguments.require_current_sources,
                                       trusted_manifest_sha256=arguments.trusted_manifest_sha256)
        else:
            envelope = strict_json(read_source(Path(arguments.compilation_envelope)).text) if arguments.compilation_envelope else None
            result = generate_export(output_directory=arguments.out, input_directory=arguments.input,
                brief_paths=arguments.brief, tool_api_path=arguments.tool_api, context_paths=arguments.context,
                cases_path=arguments.cases, mode=arguments.mode, compilation_envelope=envelope, deadline_seconds=arguments.deadline_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"verified", "verified_blocked"} else 2
    except (FoundationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "diagnostic": str(exc), "publish_allowed": False, "final_submission_ready": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
