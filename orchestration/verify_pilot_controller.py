"""Independent local acceptance of the complete integration test suite.

Socket connections are blocked around discovery and execution. This verifies
scripted transport behavior, not a live service, a model, or the official judge.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def sources():
    files = sorted((ROOT / "integration").glob("*.py"))
    files += [ROOT / "tests_orchestration/test_pilot_acceptance.py", Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def names(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from names(item)
        else:
            yield item.id()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    if not output.is_relative_to(ROOT / "artifacts") or output.exists():
        raise ValueError("Use a fresh acceptance report inside artifacts")
    before = sources()
    attempts = []

    def deny(*args, **kwargs):
        attempts.append("blocked")
        raise RuntimeError("Network connections are forbidden in local acceptance")

    capture = io.StringIO()
    with patch.object(socket, "create_connection", deny), patch.object(socket.socket, "connect", deny):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "integration"), pattern="test_*.py", top_level_dir=str(ROOT))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName("tests_orchestration.test_pilot_acceptance"))
        ids = list(names(suite))
        result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
    after = sources()
    controller_ids = [i for i in ids if any(label in i.split(".")[-2] for label in ("Pilot", "Controller", "EndToEnd"))]
    evidence = {"checked_at": datetime.now(timezone.utc).isoformat(), "scope": "offline complete integration suite with scripted transports",
                "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                "source_stable": before == after, "network_attempts": len(attempts),
                "controller_tests": len(controller_ids), "controller_test_ids": controller_ids, "all_test_ids": ids,
                "source_sha256": after, "passed": result.wasSuccessful() and before == after and not attempts and bool(controller_ids),
                "official_simulation": False, "output": capture.getvalue()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("tests_run", "failures", "errors", "controller_tests", "source_stable", "network_attempts", "passed")}))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
