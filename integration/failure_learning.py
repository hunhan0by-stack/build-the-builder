"""Offline failure learning from pinned, bounded, already-sanitized evidence.

No network, credential access, retry or acceptance-policy changes live here.
A diagnostic judge is never a public-case judge; chat text is not audio proof.
Records are documentary derivatives, not attestations that a correction works.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re

FORMAT = "failure-learning-1"
MAX_FILE_BYTES = 1_048_576
MAX_RECORD_BYTES = 65_536
MAX_FILES = 48
_HASH = re.compile(r"[0-9a-f]{64}")
_SIZE = re.compile(r"Code and data for code_exec total (\d{1,9}) bytes, over the (\d{1,9})-byte limit", re.I)
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:/ -]{1,160}")
_FAILURES = {"fail", "failed", "error", "blocked", "blocked_or_incomplete", "blocked_before_simulation"}


class FailureLearningError(ValueError):
    pass


def _json(raw):
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise FailureLearningError("Non-finite evidence number")
        return parsed
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise FailureLearningError("Duplicate evidence key")
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_float=finite_float,
                          parse_constant=lambda _: (_ for _ in ()).throw(FailureLearningError("Non-finite evidence number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FailureLearningError("Invalid bounded JSON evidence") from exc


def _path(root, value):
    base = Path(root).resolve(strict=True)
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else base / candidate
    # Resolve only after rejecting every existing symlink in the supplied path.
    absolute = Path(os.path.abspath(candidate))
    try:
        relative = absolute.relative_to(base)
    except ValueError as exc:
        raise FailureLearningError("Evidence outside root") from exc
    current = base
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise FailureLearningError("Symlink evidence forbidden")
    if (any(part.lower() in {".git", "env.sh", "credentials"} or part.lower().startswith(".env") for part in relative.parts)
            or absolute.suffix.lower() == ".dpapi"):
        raise FailureLearningError("Private evidence is not a learning input")
    return absolute, relative.as_posix()


def _read(root, value, *, json_required=False):
    path, name = _path(root, value)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise FailureLearningError("Missing or oversized evidence")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise FailureLearningError("Evidence grew beyond bound")
    pin = {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return pin, _json(raw) if json_required else raw


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _name(value):
    return value if (isinstance(value, str) and _SAFE_NAME.fullmatch(value)
                     and not re.search(r"(?i)fak_|bearer|api.?key|password|access.?token|secret", value)) else None


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1_000_000_000 else None


def observation_from_snapshot(snapshot):
    """Project safe structured facts; never copy arbitrary body/customer text."""
    if not isinstance(snapshot, dict):
        raise FailureLearningError("Observation snapshot must be an object")
    scope = str(snapshot.get("test_kind", snapshot.get("scope", ""))).lower()
    diagnostic = (snapshot.get("diagnostic_only") is True or "diagnostic" in scope
                  or snapshot.get("official_public_case") is False or snapshot.get("official_PUB01") is False)
    explicit_public = (snapshot.get("test_kind") == "PUB-01" or snapshot.get("case_id") == "PUB-01"
                       or ("pub-01" in scope and not diagnostic))
    kind = "diagnostic" if diagnostic else "PUB-01" if explicit_public else "unspecified"
    observation = {"test_kind": kind, "run_id": _name(snapshot.get("run_id", snapshot.get("operation_id"))),
                   "status": _name(snapshot.get("status", snapshot.get("remote_status"))),
                   "execution_mode": snapshot.get("mode") if isinstance(snapshot.get("mode"), str) and snapshot["mode"] in {"live", "offline_mock", "chat"} else "unspecified",
                   "mode": "chat" if snapshot.get("mode") == "chat" else "unspecified",
                   "bank_calls": _count(snapshot.get("bank_calls")),
                   "http_requests": _count(snapshot.get("requests", snapshot.get("total_new_http_requests"))),
                   "simulation_post_attempts": _count(snapshot.get("simulation_attempts_reserved", snapshot.get("simulation_post_attempts"))),
                   "unknown_mutations": _count(snapshot.get("unknown_mutations", snapshot.get("new_unknown_mutations"))),
                   "http_status": _count(snapshot.get("http_status")),
                   "bank_http_status": _count(snapshot.get("bank_http_status")),
                   "bank_result": snapshot.get("bank_result") if isinstance(snapshot.get("bank_result"), str) and snapshot["bank_result"] in {"confirmed_failed", "unknown", "confirmed_committed"} else None,
                   "effect_absent_confirmed": snapshot.get("effect_absent_confirmed") is True,
                   "provider_call_identity_mismatch": snapshot.get("provider_call_identity_mismatch") is True,
                   "native_speech_completion_verified": snapshot.get("native_speech_completion_verified") is True,
                   "speech_completion_required": snapshot.get("speech_completion_required") is True,
                   "audio_completion_proven": False,
                   "turn_count": _count(snapshot.get("turn_count", snapshot.get("turnCount"))),
                   "observed_code_and_data_bytes": _count(snapshot.get("observed_code_and_data_bytes")),
                   "observed_sandbox_limit_bytes": _count(snapshot.get("observed_sandbox_limit_bytes")),
                   "observed_credit_delta": None,
                   "credit_delta_is_exact_price": False}
    before, after = snapshot.get("credits_before"), snapshot.get("credits_after")
    if isinstance(before, dict) and isinstance(after, dict):
        a, b = _number(before.get("creditsLeft")), _number(after.get("creditsLeft"))
        if a is not None and b is not None:
            observation["credits_before"] = a
            observation["credits_after"] = b
            observation["observed_credit_delta"] = a - b
    diagnostic_observations = snapshot.get("diagnostic_observations", {})
    diagnostic_observations = diagnostic_observations if isinstance(diagnostic_observations, dict) else {}
    observation["user_turns"] = _count(diagnostic_observations.get("user_turns"))
    observation["assistant_turns"] = _count(diagnostic_observations.get("assistant_turns"))
    observation["native_call_variable_unresolved"] = diagnostic_observations.get("unresolved_call_id_placeholder") is True
    observation["native_flat_reader_missing"] = diagnostic_observations.get("flat_reader_missing") is True
    observation["native_call_id_observed_in_text"] = diagnostic_observations.get("direct_tts_call_id_matches") is True
    observation["native_two_node_continuity_observed"] = diagnostic_observations.get("two_node_state_continuity") is True
    binding = diagnostic_observations.get("provider_lifecycle_binding")
    observation["provider_lifecycle_binding_unverified"] = isinstance(binding, dict) and binding.get("valid") is False
    public_judge = snapshot.get("official_public_case_judge", {})
    diagnostic_judge = snapshot.get("diagnostic_judge", {})
    direct_outcome = snapshot.get("diagnostic_judge_outcome", snapshot.get("judge_outcome")) if diagnostic else snapshot.get("judge_outcome")
    judge = diagnostic_judge if diagnostic else public_judge
    outcome = judge.get("outcome", direct_outcome) if isinstance(judge, dict) else direct_outcome
    observation["judge_outcome"] = outcome if isinstance(outcome, str) and outcome in {"pass", "fail", "error", "inconclusive"} else None
    observation["official_public_case_pass"] = (kind == "PUB-01" and snapshot.get("official_result") is True
                                                   and observation["judge_outcome"] == "pass"
                                                   and snapshot.get("scenario_helper_pass") is True)
    statuses = snapshot.get("unknown_request_http_statuses")
    if observation["http_status"] is None and isinstance(statuses, list) and 503 in statuses:
        observation["http_status"] = 503
    # Only this exact known error pattern is retained as numbers, not raw text.
    error = snapshot.get("safe_error_text")
    match = _SIZE.search(error) if isinstance(error, str) else None
    if match:
        observation["observed_code_and_data_bytes"] = int(match.group(1))
        observation["observed_sandbox_limit_bytes"] = int(match.group(2))
    # NativeDiagnostic stores safe runtime-event projections, not raw wire bodies.
    events = diagnostic_observations.get("runtime_error_events", [])
    categories = []
    if isinstance(events, list):
        if len(events) > 64:
            raise FailureLearningError("Too many diagnostic runtime errors")
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("event_type") in ("error", "function_call_error"):
                categories.append(event["event_type"])
            selected = event.get("selected_json")
            if isinstance(selected, str) and len(selected) <= 6000:
                size_match = _SIZE.search(selected)
                if size_match:
                    observation["observed_code_and_data_bytes"] = int(size_match.group(1))
                    observation["observed_sandbox_limit_bytes"] = int(size_match.group(2))
    observation["runtime_error_categories"] = categories
    summary = snapshot.get("body_summary", {})
    known = summary.get("known_error_class") if isinstance(summary, dict) else None
    observation["known_service_error"] = known if isinstance(known, str) and known in {"agent service not configured", "agent service returned no response", "agent service timed out", "service unavailable"} else None
    return observation


def classify_observation(observation):
    """Classification cannot resolve a mutation, permit retries or prove PASS."""
    size, limit = observation.get("observed_code_and_data_bytes"), observation.get("observed_sandbox_limit_bytes")
    bank = observation.get("bank_result")
    ambiguous = (bool(observation.get("unknown_mutations")) or bank == "unknown"
                 or (observation.get("bank_http_status") is not None and observation["bank_http_status"] >= 500
                     and not (bank == "confirmed_failed" and observation.get("effect_absent_confirmed"))))
    if type(size) is int and type(limit) is int and limit > 0 and size > limit:
        code, subsystem, confidence = "code_exec_payload_limit", "native_emitter_and_runtime_budget", "high"
        cause = "Observed code-plus-runtime-data exceeded the returned sandbox cap"
        action = "Measure complete serialized request and runtime code/data with margin; regenerate and verify before a new test"
    elif observation.get("provider_call_identity_mismatch"):
        code, subsystem, confidence = "provider_call_identity_mismatch", "native_lifecycle", "high"
        cause = "Provider identifiers do not match the expected execution binding"
        action = "Bind documented provider call/workflow/session fields; reject mismatched evidence"
    elif observation.get("native_call_variable_unresolved") or observation.get("native_flat_reader_missing"):
        code, subsystem, confidence = "native_call_variable_unresolved", "native_lifecycle", "high"
        cause = "Diagnostic observed an unresolved call placeholder or missing flat code variable"
        action = "Use the actually observed documented variable scope; do not substitute application-side IDs for native identifiers"
    elif observation.get("provider_lifecycle_binding_unverified"):
        code, subsystem, confidence = "provider_lifecycle_binding_unverified", "native_lifecycle", "low"
        cause = "Lifecycle verification failed without a retained specific cause; identity or timestamps may be missing"
        action = "Preserve the verifier's specific failure code and expected binding before changing identity mapping"
    elif ambiguous:
        code, subsystem, confidence = "mutation_result_unknown", "bank_bridge" if bank or observation.get("bank_http_status") else "native_lifecycle", "high"
        cause = "A dispatched mutation has no definitive effect outcome; status alone cannot prove absence"
        action = "Retain durable unknown state; reconcile read-only using provider evidence; never replay the mutation blindly"
    elif bank == "confirmed_failed" and observation.get("effect_absent_confirmed"):
        code, subsystem, confidence = "bank_failure_confirmed", "bank_bridge", "high"
        cause = "Recorded definitive failed banking result with explicit no-effect confirmation"
        action = "Preserve request identity and failed result; apply corrected input only under an independently enforced retry policy"
    elif observation.get("http_status") == 503:
        code, subsystem, confidence = "agent_service_unavailable", "platform_service", "low"
        cause = ("Known response indicates agent-service configuration" if observation.get("known_service_error") == "agent service not configured"
                 else "HTTP 503 is observed; response body is insufficient to confirm configuration or transient outage")
        if observation.get("known_service_error") == "agent service not configured":
            confidence = "medium"
        action = "Inspect retained secure response metadata and platform configuration without repeating the same request"
    elif observation.get("speech_completion_required") and not observation.get("native_speech_completion_verified"):
        code, subsystem, confidence = "speech_completion_unverified", "native_speech_binding", "high"
        cause = "Required native speech-completion receipt is absent; text or a local boolean is insufficient"
        action = "Keep close_call blocked until the documented native receipt is tied to the semantic speech event"
    else:
        code, subsystem, confidence = "execution_failure_unclassified", "native_integration", "low"
        cause = "Available structured evidence does not establish a specific cause"
        action = "Gather one new targeted evidence item; avoid repeated analysis or tests without a changed hypothesis"
    return {"code": code, "affected_subsystem": subsystem, "likely_root_cause": cause,
            "confidence": confidence, "suggested_correction": action,
            "platform_configuration_hypothesis": "low_confidence_body_unavailable" if observation.get("http_status") == 503 and not observation.get("known_service_error") else "response_indicated_not_independently_confirmed" if observation.get("known_service_error") == "agent service not configured" else None,
            "ambiguous_mutation": ambiguous, "automatic_retry_allowed": False,
            "public_case_success_established": False}


def _regression(root, value, current_sources):
    pin, report = _read(root, value, json_required=True)
    if not isinstance(report, dict):
        raise FailureLearningError("Regression report must be an object")
    counts = {key: _count(report.get(key)) for key in ("tests_run", "failures", "errors", "skipped")}
    sources = report.get("source_sha256", report.get("source_pins", {}))
    fresh = False
    if isinstance(sources, dict) and sources:
        if len(sources) > MAX_FILES:
            raise FailureLearningError("Too many regression sources")
        normalized = {}
        for path, expected in sources.items():
            if not isinstance(expected, str) or not _HASH.fullmatch(expected):
                raise FailureLearningError("Regression source pin invalid")
            actual, _ = _read(root, path)
            normalized[actual["path"]] = expected
            if actual["sha256"] != expected:
                raise FailureLearningError("Regression source changed")
        # A report must cover all nominated changed sources; hashes alone aren't tests.
        fresh = bool(current_sources) and all(normalized.get(row["path"]) == row["sha256"] for row in current_sources)
    complete = (counts["tests_run"] is not None and counts["tests_run"] > 0
                and counts["failures"] == 0 and counts["errors"] == 0 and counts["skipped"] == 0)
    return {"evidence": pin, **counts, "source_pins_current": fresh,
            "reported_regression_pass": complete and fresh,
            "independently_rerun_by_recorder": False}


def record_failure(snapshot_path, *, root, source_paths=(), evidence_paths=(), correction=None,
                   regression_paths=(), output=None):
    """Create a fresh immutable learning record; never automatically run a test.

    correction is {before_source_evidence: path, repair_id: label}; the evidence
    must contain a bounded source_sha256 map captured before the code change.
    Source change plus pinned passing reports yields regression_verified only,
    not verified live repair. Unchanged code requires a new evidence hypothesis.
    """
    paths = list(source_paths) + list(evidence_paths) + list(regression_paths)
    if len(paths) > MAX_FILES:
        raise FailureLearningError("Too many learning evidence files")
    primary, snapshot = _read(root, snapshot_path, json_required=True)
    observation = observation_from_snapshot(snapshot)
    if observation["official_public_case_pass"]:
        raise FailureLearningError("Successful public-case snapshot is not a failure")
    if (observation["status"] not in _FAILURES and observation["judge_outcome"] not in {"fail", "error"}
            and not observation["unknown_mutations"] and not observation["known_service_error"]
            and observation["http_status"] not in {500, 502, 503, 504}
            and not observation["bank_http_status"] and observation["bank_result"] not in {"unknown", "confirmed_failed"}
            and not observation["provider_call_identity_mismatch"]
            and not observation["provider_lifecycle_binding_unverified"]
            and not observation["native_call_variable_unresolved"] and not observation["native_flat_reader_missing"]
            and not observation["runtime_error_categories"]
            and not (observation["speech_completion_required"] and not observation["native_speech_completion_verified"])
            and not (observation["observed_code_and_data_bytes"] and observation["observed_sandbox_limit_bytes"]
                     and observation["observed_code_and_data_bytes"] > observation["observed_sandbox_limit_bytes"])):
        raise FailureLearningError("Snapshot does not establish a failure")
    sources = [_read(root, value)[0] for value in source_paths]
    evidence = [primary] + [_read(root, value)[0] for value in evidence_paths]
    corrected, changed = None, []
    if correction is not None:
        if not isinstance(correction, dict) or set(correction) != {"before_source_evidence", "repair_id"} or not _name(correction["repair_id"]):
            raise FailureLearningError("Correction requires a repair identifier and pinned before-source evidence")
        before_pin, before_report = _read(root, correction["before_source_evidence"], json_required=True)
        before = before_report.get("source_sha256") if isinstance(before_report, dict) else None
        if (not isinstance(before, dict) or not before or len(before) > MAX_FILES
                or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in before.values())):
            raise FailureLearningError("Invalid correction before-source pins")
        evidence.append(before_pin)
        current = {row["path"]: row["sha256"] for row in sources}
        if any(path not in current for path in before):
            raise FailureLearningError("Correction source missing from current pins")
        changed = [path for path, value in before.items() if current[path] != value]
        corrected = {"repair_id": correction["repair_id"], "before_source_sha256": before,
                     "before_source_evidence": before_pin, "before_history_independently_authenticated": False,
                     "changed_sources": changed, "code_changed": bool(changed)}
    regressions = [_regression(root, path, sources) for path in regression_paths]
    classification = classify_observation(observation)
    outcome = "regression_verified_live_unverified" if changed and regressions and all(r["reported_regression_pass"] for r in regressions) else "unverified"
    record = {"format": FORMAT, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "observed_behavior": observation, "supporting_evidence": evidence,
              "source_pins": sources, "diagnosis": classification, "applied_correction": corrected,
              "regression_tests": regressions, "verified_outcome": outcome,
              "lesson_generalization": {"candidate_for_generated_agents": classification["code"] in {"code_exec_payload_limit", "mutation_result_unknown", "bank_failure_confirmed", "speech_completion_unverified", "provider_call_identity_mismatch", "provider_lifecycle_binding_unverified", "native_call_variable_unresolved"},
                                         "demonstrated_on_new_brief": False},
              "scope": {"diagnostic_judge_is_public_case_evidence": False, "text_completion_is_audio_completion": False,
                        "live_success_established": False, "performs_http": False, "performs_retry": False}}
    raw = (json.dumps(record, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise FailureLearningError("Learning record exceeds bound")
    # Re-read pins before sealing so a concurrently changed source cannot enter a seal.
    for row in evidence + sources + [r["evidence"] for r in regressions]:
        actual, _ = _read(root, row["path"])
        if actual != row:
            raise FailureLearningError("Learning input changed while recording")
    if output is not None:
        target, _ = _path(root, output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("--root", default=".")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = record_failure(args.snapshot, root=args.root, source_paths=args.source,
                            evidence_paths=args.evidence, output=args.out)
    print(json.dumps({"classification": result["diagnosis"]["code"], "outcome": result["verified_outcome"],
                      "automatic_retry_allowed": False, "output": args.out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
