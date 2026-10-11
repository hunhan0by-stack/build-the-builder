"""Fresh offline public-case repetitions, using the actual local Conversation.

Inputs are hand-translated customer turns and scripted MockBank responses. This
does not execute a Freya workflow, a phone model, or an official policy judge.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch

from builder.conversation import Conversation
from builder.models import strict_json
from builder.runtime import MockBank
from orchestration.local_pub01 import ObservedMessages
from tests import test_public_replays as driver
from tests.helpers import KIT, ROOT


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read(path):
    return strict_json(path.read_text(encoding="utf-8"))


def snapshot():
    # The exporter is developed independently; it is not imported by this run.
    paths = [p for p in sorted((ROOT / "builder").glob("*.py")) if p.name != "freya_export.py"]
    paths += [ROOT / "tests/test_public_replays.py", ROOT / "tests/helpers.py",
              ROOT / "tests/conversation_helpers.py", Path(__file__).resolve(),
              ROOT / "orchestration/local_pub01.py", KIT / "public-test-set.json",
              KIT / "briefs/brief-a.md", KIT / "briefs/brief-b.md", KIT / "tools/openapi.json"]
    return [{"path": p.relative_to(ROOT).as_posix(), "sha256": digest(p.read_bytes()),
             "source_utf8": p.read_bytes().decode("utf-8")} for p in paths]


def one(case, repetition):
    events = []
    started = time.perf_counter()
    cores = []
    ledger = {"outcome": None, "reason": None, "promises": [], "notes": [], "callbacks": [], "transfers": []}

    def emit(kind, payload):
        events.append({"seq": len(events) + 1, "kind": kind, "repetition": repetition,
                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 6),
                       "payload": copy.deepcopy(payload)})

    def factory(documents, policy, contract, variables, bank):
        if type(bank) is not MockBank:
            raise RuntimeError("Only exact MockBank instances are accepted")
        variables = dict(variables, customer_id=f"local_{case['id']}_repeat_{repetition}")
        core = Conversation(documents, policy, contract, variables, bank)
        cores.append(core)
        emit("reset", {"phase": core.phase, "verified": sorted(core.session.state.verified),
                       "products": [p.product_id for p in core.session.state.products],
                       "bank_calls": [asdict(a) for a in bank.calls],
                       "consent": bool(core.session.state.approved_digest), "customer_id": variables["customer_id"]})
        core.messages = ObservedMessages(lambda message: emit("agent", message))
        original_call = bank.call

        def observed_call(action):
            before = {"identity_verified_before": core.session.state.identity_verified,
                      "consent_before": bool(core.session.state.approved_digest),
                      "phase_before": core.phase}
            result = original_call(action)
            emit("tool", {"operation": action.operation, "args": action.payload, "result": result, **before})
            if result.get("ok") is True:
                payload = {k: v for k, v in action.payload.items() if k != "customer_id"}
                if action.operation == "create_promise":
                    ledger["promises"].append(payload)
                elif action.operation in ("add_note", "schedule_callback", "transfer_to_agent"):
                    key = {"add_note": "notes", "schedule_callback": "callbacks", "transfer_to_agent": "transfers"}[action.operation]
                    ledger[key].append(payload)
                elif action.operation == "close_call":
                    ledger["outcome"] = payload["outcome"]
                    ledger["reason"] = payload.get("reason")
            return result

        bank.call = observed_call
        original_say, original_receive = core.say, core.receive
        saying = False

        def say(text):
            nonlocal saying
            emit("customer", {"text": text, "phase_before": core.phase, "input_kind": "bounded_text"})
            saying = True
            try:
                return original_say(text)
            finally:
                saying = False

        def receive(turn):
            if not saying:
                emit("customer", {"turn": asdict(turn), "phase_before": core.phase, "input_kind": "structured_turn"})
            return original_receive(turn)

        core.say, core.receive = say, receive
        return core

    try:
        with patch.object(driver, "Conversation", factory):
            core = driver.make_public_replay(copy.deepcopy(case))
        core_evidence = core.evidence()
        core_ledger = driver.local_ledger(core)
        emit("end", {"phase": core.phase, "fault": core.fault, "ledger": ledger})
        return {"events": events, "ledger": ledger, "core_evidence": core_evidence,
                "initial_state_fresh": len(cores) == 1 and events[0]["kind"] == "reset"
                    and not events[0]["payload"]["verified"] and not events[0]["payload"]["consent"]
                    and not events[0]["payload"]["products"] and not events[0]["payload"]["bank_calls"],
                "observer_core_consistent": core_ledger == ledger,
                "ended_cleanly": core.phase == "ended" and not core.fault,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "error": None}
    except Exception as exc:
        emit("error", {"type": type(exc).__name__, "message": str(exc)})
        return {"events": events, "ledger": ledger, "core_evidence": None,
                "initial_state_fresh": False, "observer_core_consistent": False,
                "ended_cleanly": False, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": {"type": type(exc).__name__, "message": str(exc)}}


def collect(output):
    if not output.is_relative_to((ROOT / "artifacts").resolve()) or output.exists():
        raise ValueError("Use a fresh directory inside artifacts")
    before = snapshot()
    output.mkdir(parents=True)
    save(output / "source-snapshot.json", before)
    cases = read(KIT / "public-test-set.json")
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append("blocked")
        raise RuntimeError("Network is forbidden for local measurement")

    records = []
    with patch.object(socket, "create_connection", deny_network), patch.object(socket.socket, "connect", deny_network):
        for case in cases:
            for repetition in range(1, 4):
                result = one(case, repetition)
                relative = f"runs/{case['id']}/repeat-{repetition}"
                directory = output / relative
                for name in ("events", "ledger", "core_evidence"):
                    save(directory / (name + ".json"), result[name])
                records.append({"case_id": case["id"], "repetition": repetition, "evidence_dir": relative,
                                "ledger_matches_official_expected": result["ledger"] == case["expected"],
                                **{k: result[k] for k in ("initial_state_fresh", "observer_core_consistent", "ended_cleanly", "elapsed_ms", "error")}})
    after = snapshot()
    save(output / "collection.json", {"schema_version": "local-public39-1", "created_at": datetime.now(timezone.utc).isoformat(),
          "scope": "hand-translated public customer turns; actual local Conversation; scripted MockBank",
          "official_simulation": False, "voice_or_model_reliability_measured": False,
          "api_key_read": False, "model_calls": 0, "network_attempts": len(network_attempts),
          "source_stable_during_runs": before == after, "runs": records})
    print(json.dumps({"runs": len(records), "ledger_matches": sum(r["ledger_matches_official_expected"] for r in records),
                      "clean_ends": sum(r["ended_cleanly"] for r in records), "source_stable": before == after,
                      "network_attempts": len(network_attempts)}))


def verify(output):
    from orchestration.public_behavior import evaluate
    if (output / "evidence-manifest.json").exists():
        raise ValueError("Sealed evidence may not be overwritten")
    collection = read(output / "collection.json")
    sources = read(output / "source-snapshot.json")
    cases = {c["id"]: c for c in strict_json(next(s["source_utf8"] for s in sources if s["path"].endswith("public-test-set.json")))}
    evaluator = ROOT / "orchestration/public_behavior.py"
    save(output / "evaluator-snapshot.json", {"path": "orchestration/public_behavior.py", "sha256": digest(evaluator.read_bytes()),
                                             "source_utf8": evaluator.read_bytes().decode("utf-8")})
    negative_tests = ROOT / "tests_orchestration/test_public_behavior.py"
    save(output / "negative-tests-snapshot.json", {"path": "tests_orchestration/test_public_behavior.py",
          "sha256": digest(negative_tests.read_bytes()), "source_utf8": negative_tests.read_bytes().decode("utf-8")})
    records = []
    for run in collection["runs"]:
        directory = output / run["evidence_dir"]
        events, ledger = read(directory / "events.json"), read(directory / "ledger.json")
        checks = evaluate(events, cases[run["case_id"]])
        save(directory / "checks.json", checks)
        failed = [c["id"] for c in checks if c["passed"] is False]
        unsupported = [c["id"] for c in checks if c["passed"] is None]
        ledger_ok = ledger == cases[run["case_id"]]["expected"]
        valid = ledger_ok and all(run[k] for k in ("initial_state_fresh", "observer_core_consistent", "ended_cleanly")) and not failed
        records.append({**run, "ledger_matches_official_expected": ledger_ok,
                        "implemented_checks_passed": not failed, "failed_checks": failed,
                        "checks_passed": sum(c["passed"] is True for c in checks), "checks_not_automated": unsupported,
                        "official_assertions_passed": sum(c["passed"] is True and "official_check" in c for c in checks),
                        "local_scope_passed": valid})
    structurally_complete = len(records) == 39 and {(r["case_id"], r["repetition"]) for r in records} == {(c, n) for c in cases for n in range(1, 4)}
    summary = {k: v for k, v in collection.items() if k != "runs"}
    summary.update({"run_count": len(records), "complete_13_cases_three_repeats": structurally_complete,
                    "ledger_matches": sum(r["ledger_matches_official_expected"] for r in records),
                    "local_scope_passes": sum(r["local_scope_passed"] for r in records),
                    "observed_implemented_check_failures": sum(len(r["failed_checks"]) for r in records),
                    "automated_assertions_passed": sum(r["checks_passed"] for r in records),
                    "official_bounded_assertions_passed": sum(r["official_assertions_passed"] for r in records),
                    "not_automated_assertion_instances": sum(len(r["checks_not_automated"]) for r in records),
                    "full_official_policy_coverage": False,
                    "batch_passed_within_local_scope": structurally_complete and collection["source_stable_during_runs"]
                        and collection["network_attempts"] == 0 and all(r["local_scope_passed"] for r in records),
                    "independent_human_root_reference": False, "runs": records})
    save(output / "summary.json", summary)
    manifest = [{"path": p.relative_to(output).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p.read_bytes())}
                for p in sorted(output.rglob("*")) if p.is_file()]
    save(output / "evidence-manifest.json", {"schema_version": "local-public39-manifest-1", "files": manifest,
          "exclusions": ["evidence-manifest.json", "external software-only local anchor"]})
    anchor = output.with_name(output.name + ".local-test-anchor.json")
    if anchor.exists():
        raise ValueError("Do not overwrite existing external anchor")
    save(anchor, {"manifest_sha256": digest((output / "evidence-manifest.json").read_bytes()),
                  "purpose": "software-only local integrity reference; not independent human custody"})
    print(json.dumps({k: summary[k] for k in ("run_count", "ledger_matches", "local_scope_passes", "observed_implemented_check_failures", "batch_passed_within_local_scope")}))
    return 0 if summary["batch_passed_within_local_scope"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect", "verify"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("Output must be in artifacts")
    return collect(output) if args.mode == "collect" else verify(output)


if __name__ == "__main__":
    raise SystemExit(main())
