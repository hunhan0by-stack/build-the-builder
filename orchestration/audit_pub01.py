"""Compare the measured bundle to official files and its separate local test seal."""
import hashlib
import json
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    bundle = root / "artifacts/pub01-core-runs/core-v2"
    kit = root / "data/freya-hackathon/kit-v3/build-the-builder"
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))
    case = next(c for c in read(kit / "public-test-set.json") if c["id"] == "PUB-01")
    sources = {"sources/public-test-set.json": "public-test-set.json",
               "sources/bank-openapi.json": "tools/openapi.json", "sources/brief-a.md": "briefs/brief-a.md"}
    source_match = all((bundle / p).read_bytes() == (kit / original).read_bytes() for p, original in sources.items())
    manifest_raw = (bundle / "evidence-manifest.json").read_bytes()
    anchor = read(bundle.parent / "core-v2.local-test-anchor.json")
    seal_match = sha(manifest_raw) == anchor["manifest_sha256"]
    manifest = json.loads(manifest_raw)
    artifact_match = all(len((bundle / f["path"]).read_bytes()) == f["bytes"] and
                         sha((bundle / f["path"]).read_bytes()) == f["sha256"] for f in manifest["files"])
    agent = read(bundle / "agent.generated.json")
    frozen_code_valid = all(sha(f["source_utf8"].encode("utf-8")) == f["sha256"] for f in agent["code_snapshot"])
    current_code_drift = [f["path"] for f in agent["code_snapshot"] if sha((root / f["path"]).read_bytes()) != f["sha256"]]
    runs = []
    order = ["verify_identity", "verify_identity", "get_account", "create_promise", "close_call"]
    for index in (1, 2, 3):
        base = bundle / f"runs/{index}"
        ledger = read(base / "final-ledger.json")
        tools = [json.loads(line) for line in (base / "tool-events.jsonl").read_text(encoding="utf-8").splitlines()]
        checks = read(base / "checks.json")
        runs.append({"repetition": index, "ledger_matches_official_expected": ledger == case["expected"],
                     "tool_order_matches": [e["payload"]["tool"] for e in tools] == order,
                     "close_reason_omitted": "reason" not in tools[-1]["payload"]["args"],
                     "all_checks_pass": len(checks) == 16 and all(c["status"] == "PASS" for c in checks)})
    consistent = source_match and seal_match and artifact_match and frozen_code_valid and all(
        all(v for k, v in run.items() if k != "repetition") for run in runs)
    result = {"official_case": "PUB-01", "sources_exact": source_match, "local_test_seal_matches": seal_match,
              "all_manifest_files_match": artifact_match, "embedded_code_snapshot_valid": frozen_code_valid,
              "current_code_drift_after_measurement": current_code_drift, "runs": runs,
              "measured_bundle_consistent": consistent, "network_calls": 0,
              "independent_Hunhan_anchor_verified": False, "official_Freya_success": False,
              "note": "This audit applies to the frozen local measured version, not later code or live reliability."}
    (bundle.parent / "core-v2.consistency.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
