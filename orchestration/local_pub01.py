"""Measure the real local Conversation core, with scripted customer and MockBank.

The observer copies actual speech and tool results. It never substitutes an agent
utterance, an action, a verdict, or an expected final ledger. A software seal is
not Hunhan's independently protected root reference or evidence of live reliability.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

from builder.api_contract import BankContract
from builder.conversation import Conversation
from builder.extract import extract_policy
from builder.ingest import collect_sources
from builder.models import strict_json
from builder.runtime import MockBank

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "data/freya-hackathon/kit-v3/build-the-builder"
EVALUATOR = ROOT / "project-management/local-evidence"
sys.path.insert(0, str(EVALUATOR))
from pub01_behavior import evaluate
from validate_pub01_v2 import validate, CONTRACT_PATH


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_lines(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, ensure_ascii=False, allow_nan=False) + "\n" for v in values), encoding="utf-8")


class ObservedMessages(list):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def append(self, value):
        super().append(value)
        self.callback(value)


class Observer:
    def __init__(self, repetition):
        self.repetition = repetition
        self.started = time.perf_counter()
        self.events = []
        self.state = {"dob_verified": False, "phone_verified": False, "wrong_answers": 0,
                      "consent": False, "account_read": False, "promises": [], "notes": [],
                      "callbacks": [], "transfers": [], "close_count": 0, "closed": False}
        self.ledger = {"outcome": None, "reason": None, "promises": [], "notes": [], "callbacks": [], "transfers": []}
        self.emit("reset", {"state": self.state})
        self.state_seq = 1

    def emit(self, kind, payload):
        self.events.append({"seq": len(self.events) + 1, "repetition": self.repetition,
                            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 6),
                            "kind": kind, "payload": copy.deepcopy(payload)})

    def message(self, role, text):
        self.emit("message", {"role": role, "text": text})

    def state_event(self):
        self.emit("state", {"state": self.state})
        self.state_seq = self.events[-1]["seq"]

    def observe_bank(self, original):
        def call(action):
            response = original(action)
            self.emit("tool", {"tool": action.operation, "args": action.payload, "result": response,
                               "state_before_seq": self.state_seq, "state_after_seq": len(self.events) + 2})
            if action.operation == "close_call":
                self.state["close_count"] += 1
            if response.get("ok") is True:
                if action.operation == "verify_identity":
                    if response.get("verified") is True:
                        field = {"date_of_birth": "dob_verified", "phone_last4": "phone_verified"}[action.payload["field"]]
                        self.state[field] = True
                    elif response.get("verified") is False and not response.get("hint"):
                        self.state["wrong_answers"] += 1
                elif action.operation == "get_account":
                    self.state["account_read"] = True
                elif action.operation == "create_promise":
                    promise = {k: action.payload[k] for k in ("product_id", "amount", "date")}
                    self.state["promises"].append(promise)
                    self.ledger["promises"].append(copy.deepcopy(promise))
                elif action.operation == "close_call":
                    self.state["closed"] = True
                    self.ledger["outcome"] = action.payload["outcome"]
                    self.ledger["reason"] = action.payload.get("reason")
                elif action.operation in ("add_note", "schedule_callback", "transfer_to_agent"):
                    category = {"add_note": "notes", "schedule_callback": "callbacks", "transfer_to_agent": "transfers"}[action.operation]
                    payload = {k: v for k, v in action.payload.items() if k != "customer_id"}
                    self.state[category].append(payload)
                    self.ledger[category].append(copy.deepcopy(payload))
            self.state_event()
            return response
        return call


def one_run(case, repetition, customer_id):
    docs = collect_sources([KIT / "briefs/brief-a.md"])
    contract = BankContract.from_path(KIT / "tools/openapi.json")
    policy = extract_policy(docs, contract)
    # Mock inputs are customer identity/account facts, never expected verdicts.
    products = [dict(product, product_id=f"p{index}") for index, product in enumerate(case["products"], 1)]
    bank = MockBank({"verify_identity": [{"ok": True, "verified": True, "all_verified": False},
                                          {"ok": True, "verified": True, "all_verified": True}],
                     "get_account": [{"ok": True, "products": products}],
                     "create_promise": [{"ok": True, "promise_id": f"local-promise-{repetition}"}],
                     "close_call": [{"ok": True}]})
    observed = Observer(repetition)
    # Retain the exact MockBank class. This per-instance callback only observes the
    # original method's returned result and leaves the method's action unchanged.
    bank.call = observed.observe_bank(bank.call)
    core = Conversation(docs, policy, contract, dict(case["variables"], customer_id=customer_id), bank)
    core.messages = ObservedMessages(lambda message: observed.message("agent", message["text"]))
    core.start()
    dob = datetime.fromisoformat(case["identity"]["date_of_birth"]).strftime("%d %B %Y").lstrip("0")
    utterances = ["Yes", dob, case["identity"]["phone_last4"], "I can pay the full amount today.", "Yes"]
    for text in utterances:
        observed.message("customer", text)
        core.say(text)
        # Consent is the actual core's current digest, not a scripted PASS flag.
        observed.state["consent"] = bool(core.session.state.approved_digest)
        observed.state_event()
    for _ in range(10):
        if core.phase not in ("recording", "post_record"):
            break
        core.advance()
    if core.phase != "ended" or core.fault:
        raise RuntimeError(f"Local core did not end cleanly: phase={core.phase}")
    # Cross-check observer-derived promises/outcome against actual core state.
    actual_promises = [{k: p.recorded.payload(customer_id)[k] for k in ("product_id", "amount", "date")}
                       for p in core.session.state.products if p.recorded]
    # The core uses an empty string for omitted reason; the bank ledger stores null.
    core_outcome = dict(core.outcome)
    if core_outcome["reason"] == "":
        core_outcome["reason"] = None
    if actual_promises != observed.ledger["promises"] or any(
            core_outcome[k] != observed.ledger[k] for k in ("outcome", "reason")):
        raise RuntimeError("Observer/core ledger disagreement")
    observed.emit("end", {"state": observed.state, "ledger": observed.ledger})
    api = strict_json((KIT / "tools/openapi.json").read_text(encoding="utf-8"))
    checks = evaluate(observed.events, case, api, customer_id)
    return observed, checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    output = Path(args.out).resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()) or output.exists():
        raise ValueError("Use a fresh directory inside artifacts")
    contract_raw = CONTRACT_PATH.read_bytes()
    contract = strict_json(contract_raw.decode("utf-8"))
    case = next(c for c in strict_json((KIT / "public-test-set.json").read_text(encoding="utf-8")) if c["id"] == "PUB-01")
    sources = sorted((ROOT / "builder").glob("*.py")) + [Path(__file__).resolve(), EVALUATOR / "pub01_behavior.py",
                                                         EVALUATOR / "validate_pub01_v2.py", EVALUATOR / "validate_evidence.py", CONTRACT_PATH]
    code_snapshot = [{"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p.read_bytes()),
                      "source_utf8": p.read_text(encoding="utf-8")} for p in sources]
    output.mkdir(parents=True)
    agent = {"kind": "executable-local-conversation-core-with-scripted-customer-and-MockBank",
             "entrypoint": "builder.conversation.Conversation", "code_snapshot": code_snapshot,
             "live_agent_export": False, "real_agent_reliability_measured": False}
    write_json(output / "agent.generated.json", agent)
    agent_hash = sha((output / "agent.generated.json").read_bytes())
    customers = [f"local-PUB-01-{i}" for i in (1, 2, 3)]
    inputs = {"schema_version": "2.0", "contract_sha256": sha(contract_raw), "case_id": "PUB-01",
              "batch_id": output.name, "agent_sha256": agent_hash, "plan_version": "reviewed-practice-conversation",
              "simulator_version": "real-local-core-bridge-1", "evaluator_version": "2.0", "customers": customers}
    write_json(output / "input-manifest.json", inputs)
    for target, source in contract["source_files"].items():
        path = output / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((KIT / source).read_bytes())
    started = time.perf_counter()
    lifecycle = []
    metrics = []

    def trace(event, repetition=None):
        lifecycle.append({"seq": len(lifecycle) + 1, "utc": datetime.now(timezone.utc).isoformat(),
                          "elapsed_ms": round((time.perf_counter() - started) * 1000, 6),
                          "event": event, "repetition": repetition})

    trace("batch_start")
    report = {"schema_version": "2.0", "kind": "scripted-local-simulation", "case_id": "PUB-01",
              "batch_id": output.name, "network_calls": 0, "model_calls": 0,
              "agent_sha256": agent_hash, "status": "NOT_RUN", "runs": []}
    for repetition, customer in enumerate(customers, 1):
        trace("run_start", repetition)
        measured_start = time.perf_counter()
        observed, checks = one_run(case, repetition, customer)
        elapsed_ms = (time.perf_counter() - measured_start) * 1000
        base = output / f"runs/{repetition}"
        write_lines(base / "timeline.jsonl", observed.events)
        write_lines(base / "transcript.jsonl", [e for e in observed.events if e["kind"] == "message"])
        write_lines(base / "tool-events.jsonl", [e for e in observed.events if e["kind"] == "tool"])
        write_json(base / "initial-state.json", observed.events[0]["payload"]["state"])
        write_json(base / "final-ledger.json", observed.ledger)
        write_json(base / "checks.json", checks)
        status = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"
        report["runs"].append({"repetition": repetition, "customer_id": customer, "agent_sha256": agent_hash, "status": status})
        metrics.append({"repetition": repetition, "status": status, "elapsed_ms": round(elapsed_ms, 3),
                        "checks_passed": sum(c["status"] == "PASS" for c in checks), "checks_total": len(checks),
                        "tool_calls": sum(e["kind"] == "tool" for e in observed.events),
                        "failed_checks": [c["check_id"] for c in checks if c["status"] != "PASS"]})
        trace("run_end", repetition)
    trace("batch_end")
    # Refuse mixed-version measurements when another owner changed imported code.
    if any(sha((ROOT / item["path"]).read_bytes()) != item["sha256"] for item in code_snapshot):
        raise RuntimeError("Code changed during measurement; retain this incomplete batch and use a new one")
    report["status"] = "PASS" if all(r["status"] == "PASS" for r in report["runs"]) else "FAIL"
    write_json(output / "report.json", report)
    write_lines(output / "run-trace.jsonl", lifecycle)
    paths = contract["batch_files"] + list(contract["source_files"]) + [
        f"runs/{i}/{name}" for i in (1, 2, 3) for name in contract["run_files"]]
    manifest = {"schema_version": "2.0", "files": []}
    for relative in paths:
        raw = (output / relative).read_bytes()
        manifest["files"].append({"path": relative, "bytes": len(raw), "sha256": sha(raw)})
    write_json(output / "evidence-manifest.json", manifest)
    anchor = sha((output / "evidence-manifest.json").read_bytes())
    anchor_path = output.parent / (output.name + ".local-test-anchor.json")
    write_json(anchor_path, {"batch_id": output.name, "manifest_sha256": anchor,
                             "kind": "software-recorded-local-test-reference-not-independent-Hunhan-anchor"})
    verification = validate(output, strict_json(anchor_path.read_text(encoding="utf-8"))["manifest_sha256"])
    summary = {"batch": output.relative_to(ROOT).as_posix(), "acceptance": verification["acceptance"],
               "integrity": verification["integrity"], "runs": metrics,
               "errors": verification["errors"], "network_calls": 0, "model_calls": 0,
               "core_executed": True, "software_test_anchor_only": True,
               "official_freya_result": False, "real_agent_reliability_measured": False}
    write_json(output.parent / (output.name + ".verification.json"), verification)
    write_json(output.parent / (output.name + ".measurement.json"), summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if verification["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
