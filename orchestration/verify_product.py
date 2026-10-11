"""Measure the complete local product in one run, with network connections blocked.

Passing proves only the implemented local assertions. It does not certify general
policy interpretation, publication, or a real Freya conversation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORIES = ('builder', 'tests', 'compiler', 'tests_compiler',
                      'orchestration', 'tests_orchestration', 'integration')
TEST_DIRECTORIES = ('tests', 'tests_compiler', 'tests_orchestration', 'integration')


def source_hashes():
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in SOURCE_DIRECTORIES for path in sorted((ROOT / folder).glob('*.py'))}


def test_names(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_names(item)
        else:
            yield item.id()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    destination = args.out.resolve()
    if not destination.is_relative_to(ROOT / 'artifacts') or destination.exists():
        parser.error('Choose a fresh evidence file inside artifacts')
    before = source_hashes()
    attempts = []

    def deny(*args, **kwargs):
        attempts.append('connection_blocked')
        raise RuntimeError('Real network connections are forbidden in local product verification')

    output = io.StringIO()
    with patch.object(socket, 'create_connection', deny), patch.object(socket.socket, 'connect', deny):
        suite = unittest.TestSuite()
        for directory in TEST_DIRECTORIES:
            # Namespace test folders need not have __init__.py. Load their real
            # qualified module names so imports and test IDs remain unambiguous.
            for path in sorted((ROOT / directory).glob('test_*.py')):
                suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
                    directory + '.' + path.stem))
        names = list(test_names(suite))
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    after = source_hashes()
    unique = len(names) == len(set(names))
    record = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Complete implemented local product assertions; scripted services and local fixtures',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'test_counts_by_package': dict(Counter(name.split('.')[0] for name in names)),
        'test_ids_unique': unique, 'source_stable': before == after,
        'source_sha256': after, 'network_attempts': len(attempts),
        'local_gate_passed': result.wasSuccessful() and unique and before == after and not attempts,
        'full_semantic_accuracy_proven': False, 'live_freya_success_proven': False,
        'final_submission_ready': False, 'all_test_ids': names, 'output': output.getvalue(),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({name: record[name] for name in (
        'tests_run', 'failures', 'errors', 'skipped', 'test_counts_by_package',
        'source_stable', 'network_attempts', 'local_gate_passed')}))
    return 0 if record['local_gate_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
