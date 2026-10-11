"""Recompute the integrity and local scope result of a sealed 39-run package."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

from builder.models import strict_json
from orchestration.public_behavior import evaluate

ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return strict_json(path.read_bytes().decode("utf-8"))


def audit(package):
    package = Path(package).resolve()
    if not package.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Package must remain in artifacts")
    manifest = read(package / "evidence-manifest.json")
    anchor = read(package.with_name(package.name + ".local-test-anchor.json"))
    failures = []
    if sha((package / "evidence-manifest.json").read_bytes()) != anchor["manifest_sha256"]:
        failures.append("software_anchor_mismatch")
    names = set()
    for entry in manifest["files"]:
        name = entry["path"]
        target = (package / name).resolve()
        if name in names or not target.is_relative_to(package) or not target.is_file():
            failures.append("invalid_manifest_path")
            continue
        names.add(name)
        raw = target.read_bytes()
        if len(raw) != entry["bytes"] or sha(raw) != entry["sha256"]:
            failures.append("file_integrity:" + name)
    actual_names = {p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file() and p.name != "evidence-manifest.json"}
    if names != actual_names:
        failures.append("manifest_inventory_mismatch")
    snapshots = read(package / "source-snapshot.json")
    evaluator = read(package / "evaluator-snapshot.json")
    negative_tests = read(package / "negative-tests-snapshot.json")
    for source in snapshots + [evaluator, negative_tests]:
        if sha(source["source_utf8"].encode("utf-8")) != source["sha256"]:
            failures.append("embedded_source_hash:" + source["path"])
    if sha((ROOT / evaluator["path"]).read_bytes()) != evaluator["sha256"]:
        failures.append("current_evaluator_differs")
    cases = {c["id"]: c for c in strict_json(next(s["source_utf8"] for s in snapshots if s["path"].endswith("public-test-set.json")))}
    summary = read(package / "summary.json")
    for run in summary["runs"]:
        directory = package / run["evidence_dir"]
        expected_checks = evaluate(read(directory / "events.json"), cases[run["case_id"]])
        if expected_checks != read(directory / "checks.json"):
            failures.append("recomputed_checks_disagree")
        if read(directory / "ledger.json") != cases[run["case_id"]]["expected"]:
            failures.append("ledger_disagrees")
    complete = len(summary["runs"]) == 39 and {(r["case_id"], r["repetition"]) for r in summary["runs"]} == {(c, n) for c in cases for n in range(1, 4)}
    if not complete:
        failures.append("incomplete_repetitions")
    return {"integrity_and_recalculation_passed": not failures, "files_checked": len(names),
            "runs_checked": len(summary["runs"]), "failures": failures,
            "independent_human_root_reference": False, "official_simulation": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    args = parser.parse_args()
    result = audit(args.package)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity_and_recalculation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
