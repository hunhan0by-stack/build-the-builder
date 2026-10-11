"""Packaging closure and explicit missing-fixture tests; no model or HTTP."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
import subprocess
import sys
from unittest.mock import patch

from builder.models import FoundationError
from orchestration import prepare_contest_checkout as packing
from tests_orchestration import generated_workflow_harness as harness


class ContestCheckoutClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepare(self, **kwargs):
        with patch.object(packing, 'OUTPUT', self.root):
            return packing.prepare(self.root / 'package', **kwargs)

    def candidate(self):
        path = self.root / 'explicit-graph.json'
        path.write_text(json.dumps({'initial_node': 'listen', 'nodes': {'listen': {}}}), encoding='utf-8')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_packaged_source_has_regression_dependencies_without_claiming_final(self):
        result = self.prepare()
        manifest = json.loads((self.root / 'package/SOURCE-MANIFEST.json').read_text())
        for name in ('generated_workflow_harness.py', 'test_generated_candidate_dialogue.py',
                     'test_generated_candidate_collective.py', 'test_final_run_pairing.py',
                     'test_contest_run_pairing.py', 'test_contest_checkout_closure.py'):
            self.assertIn('tests_orchestration/' + name, manifest)
        self.assertIn('orchestration/generated_behavior.py', manifest)
        for name in (
            'tests/helpers.py',
            'tests/conversation_helpers.py',
            'tests/test_public_replays.py',
            'tests_orchestration/test_public_behavior.py',
            'integration/failure_learning.py',
            'project-management/local-evidence/pub01_behavior.py',
            'project-management/local-evidence/validate_pub01_v2.py',
            'project-management/local-evidence/validate_evidence.py',
            'project-management/local-evidence/contract-v2.json',
            'project-management/local-evidence/contract-v1.json',
        ):
            self.assertIn(name, manifest)
            with zipfile.ZipFile(self.root / 'package.zip') as archive:
                self.assertEqual(archive.read(name), (self.root / 'package' / name).read_bytes())
        for name in ('pyproject.toml', 'requirements-evidence-protection.txt',
                     'tests/__init__.py', 'tests/test_portable_protection.py',
                     'tests_orchestration/test_conversation_passthrough.py',
                     'tests_orchestration/test_behavior_cache.py',
                     'tests_orchestration/test_extraction_interface.py'):
            self.assertIn(name, manifest)
        self.assertEqual((self.root / 'package/requirements-evidence-protection.txt').read_text(),
                         'cryptography==50.0.2\n')
        with zipfile.ZipFile(self.root / 'package.zip') as archive:
            for name in ('pyproject.toml', 'requirements-evidence-protection.txt',
                         'tests/test_portable_protection.py',
                         'tests_orchestration/test_behavior_cache.py'):
                self.assertEqual(archive.read(name), (self.root / 'package' / name).read_bytes())
        self.assertFalse(result['regression_candidate_included'])
        self.assertFalse(result['provider_exports_included'])
        self.assertFalse(result['final_submission_ready'])

    def test_optional_fixture_exact_hash_and_provenance_is_in_source_manifest(self):
        candidate, pin = self.candidate()
        result = self.prepare(regression_candidate=candidate, regression_candidate_sha256=pin)
        path = self.root / 'package/tests_orchestration/inputs/candidate-regression'
        self.assertEqual((path / 'agent.workflow.json').read_bytes(), candidate.read_bytes())
        proof = json.loads((path / 'provenance.json').read_text())
        self.assertEqual(proof['candidate_sha256'], pin)
        self.assertFalse(proof['final_submission_ready'])
        self.assertTrue(result['regression_candidate_included'])
        self.assertFalse(result['provider_exports_included'])

    def test_clean_checkout_imports_crypto_dependencies_and_missing_graph_is_not_measured(self):
        self.prepare()
        program = '''import json, unittest
import tests.test_portable_protection
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests_orchestration.test_generated_candidate_dialogue',
    'tests_orchestration.test_generated_candidate_collective'])
result = unittest.TextTestRunner().run(suite)
print(json.dumps({'groups': result.testsRun, 'skipped': len(result.skipped),
                  'failures': len(result.failures), 'errors': len(result.errors),
                  'reasons': sorted(set(reason for _, reason in result.skipped))}))
'''
        environment = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
                       if name in os.environ}
        process = subprocess.run([sys.executable, '-B', '-c', program],
            cwd=self.root / 'package', env=environment, capture_output=True,
            encoding='utf-8', timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertGreater(report['groups'], 0)
        self.assertEqual(report['groups'], report['skipped'])
        self.assertEqual((report['failures'], report['errors']), (0, 0))
        self.assertTrue(all(reason.startswith('NOT_MEASURED:') for reason in report['reasons']))

    def test_wrong_fixture_hash_refused_before_output_is_created(self):
        candidate, _ = self.candidate()
        with self.assertRaisesRegex(FoundationError, 'REGRESSION_CANDIDATE_HASH_MISMATCH'):
            self.prepare(regression_candidate=candidate, regression_candidate_sha256='0' * 64)
        self.assertFalse((self.root / 'package').exists())

    def test_required_regression_dependency_missing_fails_explicitly(self):
        with patch.object(packing, 'ROOT', self.root):
            with self.assertRaisesRegex(FoundationError, 'REGRESSION_DEPENDENCY_MISSING'):
                self.prepare()
        self.assertFalse((self.root / 'package').exists())

    def test_missing_fixture_hash_is_not_implicitly_trusted(self):
        candidate, _ = self.candidate()
        with self.assertRaisesRegex(FoundationError, 'REGRESSION_CANDIDATE_AND_HASH_REQUIRED'):
            self.prepare(regression_candidate=candidate)

    def test_export_wrapper_cannot_substitute_for_regression_graph(self):
        candidate = self.root / 'wrapper.json'
        candidate.write_text('{"workflow_id":"diagnostic","workflow":{}}', encoding='utf-8')
        with self.assertRaisesRegex(FoundationError, 'REGRESSION_GRAPH_REQUIRED'):
            self.prepare(regression_candidate=candidate,
                regression_candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest())

    def test_missing_archived_and_portable_candidate_is_explicit_not_measured(self):
        with patch.object(harness, 'ROOT', self.root), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(unittest.SkipTest, 'NOT_MEASURED'):
                harness.candidate_path(self.root / 'absent.json')

    def test_missing_explicit_override_does_not_fall_back_to_history(self):
        candidate, _ = self.candidate()
        with patch.dict(os.environ, {'GENERATED_DIALOGUE_CANDIDATE': str(self.root / 'missing.json')}, clear=True):
            with self.assertRaises(FileNotFoundError):
                harness.candidate_path(candidate)

    def test_explicit_override_selects_exact_candidate(self):
        candidate, _ = self.candidate()
        with patch.dict(os.environ, {'GENERATED_DIALOGUE_CANDIDATE': str(candidate)}, clear=True):
            self.assertEqual(harness.candidate_path(), candidate)


if __name__ == '__main__':
    unittest.main()
