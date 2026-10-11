"""New isolated preflight checks; no model/auth/Freya or prior test suites."""
from pathlib import Path
from types import SimpleNamespace
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from orchestration.contest_preflight import (ENTRY_FILES, EVIDENCE_FILES, KIT, KIT_FILES,
    HISTORICAL_GRAPH_PINS, PUBLIC_REPLAY_FILES, PreflightError, git_sources, inspect,
    main, public_name, sha, source_inventory)

COMMIT = 'a' * 40


class ContestPreflightTests(unittest.TestCase):
    def test_only_explicit_optional_protection_dependencies_are_public(self):
        for name in ('pyproject.toml', 'requirements-evidence-protection.txt',
                     'tests/test_portable_protection.py', 'tests/__init__.py'):
            self.assertEqual(public_name(name), name)
        for name in ('tests/key.py', 'requirements-private.txt', '.env',
                     'tests/evidence.key', 'integration/FREYA_EVIDENCE_KEY'):
            with self.assertRaises(PreflightError):
                public_name(name)

    def test_only_named_capture_test_is_public_and_private_integration_paths_stay_closed(self):
        name = 'tests_integration/test_contest_evidence.py'
        self.assertEqual(public_name(name), name)
        for blocked in ('tests_integration/test_other.py', 'tests_integration/__init__.py',
                        'tests_integration/nested/test_contest_evidence.py',
                        'integration/runs/capture.py', 'integration/state/token.json',
                        'integration/.env', 'integration/env.sh', 'integration/token.py'):
            with self.subTest(path=blocked):
                with self.assertRaises(PreflightError):
                    public_name(blocked)

    def test_replay_paths_are_exact_and_archived_graph_manifest_pins_cannot_change(self):
        for name in (*PUBLIC_REPLAY_FILES, *HISTORICAL_GRAPH_PINS):
            self.assertEqual(public_name(name), name)
        for name in ('tests/not_public.py', 'artifacts/final-builder/other/agent.workflow.json',
                     'project-management/p0-dialogue-tests-20261011/candidate/extra.json'):
            with self.assertRaises(PreflightError):
                public_name(name)
        name = next(iter(HISTORICAL_GRAPH_PINS))
        manifest = self.manifest({**self.pins, name: '0' * 64})
        result = self.check(source_manifest=manifest,
                            expected_manifest_sha256=sha(manifest.read_bytes()))
        self.assertIn('HISTORICAL_GRAPH_PIN_MISMATCH', result['blockers'])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / 'checkout'
        for name in (*ENTRY_FILES, *(KIT + '/' + name for name in KIT_FILES),
                     *('integration/' + name for name in EVIDENCE_FILES)):
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(('public source ' + name).encode())
        self.brief = self.root / 'brief.md'
        self.brief.write_text('A new bank policy.', encoding='utf-8')
        self.api = self.root / 'tools.json'
        self.api.write_text('{"openapi":"3.0.3","paths":{}}', encoding='utf-8')
        self.executable = self.root / 'fake-codex.exe'
        self.executable.write_bytes(b'Never execute this placeholder.')
        self.pins = {name: sha((self.checkout / name).read_bytes())
                     for name in source_inventory(self.checkout)}
        self.no_process = patch('orchestration.contest_preflight.subprocess.run',
            side_effect=AssertionError('preflight tests must not run a CLI or network'))
        self.no_process.start()
        self.addCleanup(self.no_process.stop)

    def check(self, **kwargs):
        arguments = dict(checkout=self.checkout, brief_path=self.brief,
            tool_api_path=self.api, expected_commit=COMMIT, executable=str(self.executable),
            git_reader=lambda root, commit, names: ({name: self.pins[name] for name in names}, []))
        arguments.update(kwargs)
        return inspect(**arguments)

    def manifest(self, contents=None):
        path = self.checkout / 'SOURCE-MANIFEST.json'
        path.write_text(json.dumps(contents if contents is not None else self.pins), encoding='utf-8')
        return path

    def test_cli_presence_never_proves_auth_model_or_live_success(self):
        result = self.check()
        self.assertTrue(result['local_preflight_passed'])
        self.assertEqual(result['status'], 'LOCAL_PRECONDITIONS_VERIFIED_EXTERNAL_AUTH_REQUIRED')
        for key in ('authentication', 'model_availability', 'codex_cli_flag_compatibility'):
            self.assertEqual(result[key], 'NOT_MEASURED')
        for key in ('model_calls', 'freya_calls', 'bank_calls', 'login_attempts', 'source_mutations'):
            self.assertEqual(result[key], 0)
        self.assertFalse(result['live_success_verified'])
        self.assertFalse(result['final_submission_ready'])

    def test_missing_cli_blocks_codex_but_not_bounded_provider(self):
        settings = dict(executable=None, executable_resolver=lambda unused: None)
        with patch.dict('os.environ', {}, clear=True):
            self.assertIn('CODEX_EXECUTABLE_NOT_FOUND', self.check(**settings)['blockers'])
            self.assertTrue(self.check(provider='bounded', **settings)['local_preflight_passed'])

    def test_current_uncommitted_entry_file_blocks_reproducibility(self):
        def reader(root, commit, names):
            return ({name: self.pins[name] for name in names if name != 'builder/build.py'}, ['builder/build.py'])
        result = self.check(git_reader=reader)
        self.assertIn('SOURCE_FILES_NOT_IN_SELECTED_COMMIT', result['blockers'])
        self.assertEqual(result['checks']['source']['git_missing_files'], ['builder/build.py'])

    def test_modified_commit_source_cannot_be_accepted(self):
        (self.checkout / 'builder/build.py').write_text('changed source', encoding='utf-8')
        result = self.check()
        self.assertIn('SOURCE_FILES_DIFFER_FROM_SELECTED_COMMIT', result['blockers'])
        self.assertEqual(result['checks']['source']['git_byte_mismatch_files'], ['builder/build.py'])

    def test_manifest_requires_independent_pin_and_complete_runtime(self):
        path = self.manifest()
        self.assertIn('EXTERNAL_MANIFEST_PIN_REQUIRED', self.check(source_manifest=path)['blockers'])
        result = self.check(source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))
        self.assertTrue(result['local_preflight_passed'])
        subset = dict(self.pins)
        subset.pop('builder/build.py')
        path = self.manifest(subset)
        self.assertIn('SOURCE_MANIFEST_RUNTIME_CLOSURE_MISSING', self.check(
            source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))['blockers'])

    def test_tampered_manifest_and_file_both_fail_closed(self):
        path = self.manifest()
        fingerprint = sha(path.read_bytes())
        path.write_text(path.read_text() + ' ', encoding='utf-8')
        self.assertIn('SOURCE_MANIFEST_HASH_MISMATCH', self.check(
            source_manifest=path, expected_manifest_sha256=fingerprint)['blockers'])
        (self.checkout / 'builder/build.py').write_text('tampered', encoding='utf-8')
        self.assertIn('SOURCE_MANIFEST_FILE_MISMATCH', self.check(
            source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))['blockers'])

    def test_portable_readme_is_exact_explicit_contest_doc_derivation(self):
        document = (self.checkout / 'CONTEST-RUN.md').read_bytes()
        (self.checkout / 'README.md').write_bytes(document)
        inventory = {**self.pins, 'README.md': sha(document)}
        path = self.manifest(inventory)
        result = self.check(source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))
        self.assertTrue(result['local_preflight_passed'])
        self.assertEqual(result['checks']['source']['derived_source_map'], {'README.md': 'CONTEST-RUN.md'})
        (self.checkout / 'README.md').write_text('unrelated README', encoding='utf-8')
        path = self.manifest({**self.pins, 'README.md': sha((self.checkout / 'README.md').read_bytes())})
        result = self.check(source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))
        self.assertIn('CHECKOUT_README_DERIVATION_MISMATCH', result['blockers'])

    def test_manifest_traversal_private_and_batch_control_names_refused(self):
        for name in ('../private.json', '.codex/auth.json', 'builder/evil\n.py'):
            with self.subTest(name=name):
                path = self.manifest({**self.pins, name: 'f' * 64})
                result = self.check(source_manifest=path, expected_manifest_sha256=sha(path.read_bytes()))
                self.assertFalse(result['local_preflight_passed'])
                self.assertTrue(set(result['blockers']) & {'PUBLIC_SOURCE_PATH_INVALID', 'PUBLIC_SOURCE_PATH_NOT_ALLOWLISTED'})

    def test_credential_input_is_refused_before_reading(self):
        private = self.root / '.codex' / 'auth.json'
        private.parent.mkdir()
        private.write_text('MUST-NOT-BE-READ', encoding='utf-8')
        original = Path.read_bytes
        def guarded(path):
            if path == private:
                self.fail('read private credential input')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded):
            result = self.check(tool_api_path=private)
        self.assertIn('PRIVATE_SOURCE_REFUSED', result['blockers'])
        self.assertNotIn('MUST-NOT-BE-READ', json.dumps(result))

    def test_invalid_budget_and_nonfinite_or_duplicate_api_refused(self):
        for value in (0, 1801, True):
            self.assertIn('DEADLINE_MUST_BE_1_TO_1800_SECONDS', self.check(deadline_seconds=value)['blockers'])
        self.assertTrue(self.check(deadline_seconds=1800)['checks']['deadline']['accepted_limit'])
        for content in ('{"openapi":"3.0.3","paths":{},"x":NaN}',
                        '{"openapi":"3.0.3","paths":{},"paths":{}}'):
            self.api.write_text(content, encoding='utf-8')
            self.assertFalse(self.check()['checks']['input_files']['tool_api']['readable'])

    def test_environment_presence_never_discloses_values_or_connects(self):
        with patch.dict('os.environ', {'FREYA_API_KEY': 'DO-NOT-EMIT-KEY',
                                     'FREYA_BANK_TEAM_KEY': 'DO-NOT-EMIT-TEAM'}, clear=True):
            result = self.check(publish=True, practice=True)
        self.assertTrue(result['local_preflight_passed'])
        self.assertFalse(result['checks']['connection_environment']['connection_verified'])
        self.assertNotIn('DO-NOT-EMIT', json.dumps(result))
        with patch.dict('os.environ', {}, clear=True):
            self.assertIn('FREYA_ENVIRONMENT_PREREQUISITE_MISSING', self.check(publish=True)['blockers'])
        self.assertIn('PRACTICE_REQUIRES_PUBLICATION', self.check(practice=True)['blockers'])

    def test_unpinned_commit_is_rejected_without_git_read(self):
        result = self.check(expected_commit='HEAD', git_reader=lambda *args: self.fail('untrusted revision executed'))
        self.assertIn('FULL_SOURCE_COMMIT_REQUIRED', result['blockers'])

    def test_source_change_during_read_only_git_check_is_detected(self):
        def reader(root, commit, names):
            (self.checkout / 'builder/build.py').write_text('changed during git check', encoding='utf-8')
            return ({name: self.pins[name] for name in names}, [])
        result = self.check(git_reader=reader)
        self.assertIn('PUBLIC_SOURCE_CHANGED_DURING_PREFLIGHT', result['blockers'])
        self.assertFalse(result['checks']['source']['source_stable_during_preflight'])

    def test_protected_evidence_public_dependency_closure_is_required(self):
        missing = self.checkout / 'integration/response_diagnostics.py'
        original = missing.read_bytes()
        missing.unlink()
        self.assertFalse(self.check(include_evidence=True)['local_preflight_passed'])
        missing.write_bytes(original)
        marker = SimpleNamespace(name='nt', environ={})
        with patch('orchestration.contest_preflight.os', marker):
            result = self.check(include_evidence=True)
        self.assertTrue(result['local_preflight_passed'])
        self.assertEqual(result['checks']['protected_evidence']['encryption_probe'], 'NOT_MEASURED')
        self.assertFalse(result['checks']['protected_evidence']['transport_hook_integration_verified'])

    def test_local_generation_always_requires_the_runner_evidence_snapshot_sources(self):
        self.assertEqual(source_inventory(self.checkout), source_inventory(self.checkout, True))
        missing = self.checkout / 'integration/contest_evidence.py'
        missing.unlink()
        self.assertIn('PUBLIC_FILE_UNAVAILABLE', self.check()['blockers'])
        subset = {name: pin for name, pin in self.pins.items() if name != 'integration/contest_evidence.py'}
        manifest = self.manifest(subset)
        self.assertIn('SOURCE_MANIFEST_RUNTIME_CLOSURE_MISSING', self.check(source_manifest=manifest,
            expected_manifest_sha256=sha(manifest.read_bytes()))['blockers'])

    def set_runner_source(self, source):
        runner = self.checkout / 'orchestration/contest_build.py'
        runner.write_text(source, encoding='utf-8')
        self.pins['orchestration/contest_build.py'] = sha(runner.read_bytes())

    def test_trace_only_comments_strings_bad_choices_and_syntax_do_not_declare_a_runner_option(self):
        sources = (
            "# parser.add_argument('--evidence-mode', choices=('protected', 'trace-only'))\n",
            '"parser.add_argument(\'--evidence-mode\', choices=(\'protected\', \'trace-only\'))"\n',
            "parser.add_argument('--other', choices=('protected', 'trace-only'))\n",
            "parser.add_argument('--evidence-mode', choices=('protected',))\n",
            "parser.add_argument('--evidence-mode', choices=runtime_choices)\n",
            "parser.add_argument('--evidence-mode', choices=('protected', 'trace-only'\n",
        )
        for source in sources:
            with self.subTest(source_kind=sources.index(source)):
                self.set_runner_source(source)
                with patch.dict(os.environ, {'FREYA_API_KEY': 'TEST_ONLY_NEVER_SENT'}, clear=True):
                    result = self.check(publish=True, evidence_mode='trace-only')
                self.assertIn('TRACE_ONLY_RUNNER_OPTION_UNAVAILABLE', result['blockers'])
                self.assertFalse(result['checks']['trace_only_runner']['option_declared_in_source'])
                self.assertFalse(result['checks']['trace_only_runner']['runtime_behavior_verified'])

    def test_trace_only_literal_option_is_source_declared_but_runtime_and_live_are_unmeasured(self):
        self.set_runner_source("import argparse\nparser = argparse.ArgumentParser()\n"
            "parser.add_argument('--evidence-mode', choices=('protected', 'trace-only'))\n")
        marker = SimpleNamespace(name='posix', environ={'FREYA_API_KEY': 'TEST_ONLY_NEVER_SENT'})
        with patch('orchestration.contest_preflight.os', marker):
            result = self.check(publish=True, evidence_mode='trace-only')
            practice = self.check(publish=True, practice=True, evidence_mode='trace-only')
            conflict = self.check(publish=True, include_evidence=True, evidence_mode='trace-only')
            missing_publish = self.check(evidence_mode='trace-only')
        self.assertTrue(result['local_preflight_passed'])
        self.assertEqual(result['status'], 'LOCAL_PUBLICATION_ONLY_PRECONDITIONS_VERIFIED_EXTERNAL_AUTH_REQUIRED')
        self.assertTrue(result['checks']['trace_only_runner']['option_declared_in_source'])
        self.assertFalse(result['checks']['trace_only_runner']['runtime_behavior_verified'])
        self.assertFalse(result['live_success_verified'])
        self.assertFalse(result['final_submission_ready'])
        self.assertEqual(result['evidence']['full_wire_request_response'], 'NOT_CAPTURED_BY_MODE')
        self.assertIn('TRACE_ONLY_PUBLICATION_ONLY', practice['blockers'])
        self.assertIn('TRACE_ONLY_PROTECTED_EVIDENCE_CONFLICT', conflict['blockers'])
        self.assertIn('TRACE_ONLY_PUBLICATION_ONLY', missing_publish['blockers'])

    def test_explicit_fernet_on_posix_checks_presence_and_dependency_without_reading_a_key(self):
        class GuardedEnvironment(dict):
            def __getitem__(self, name):
                if name == 'FREYA_EVIDENCE_KEY':
                    raise AssertionError('preflight must not read a host key')
                return super().__getitem__(name)
            def get(self, name, default=None):
                if name == 'FREYA_EVIDENCE_KEY':
                    raise AssertionError('preflight must not probe a host key')
                return super().get(name, default)
        environment = GuardedEnvironment(FREYA_EVIDENCE_PROTECTION='fernet',
            FREYA_EVIDENCE_KEY='TEST_ONLY_PRESENT_VALUE_NEVER_READ', FREYA_API_KEY='TEST_ONLY_PRESENT')
        marker = SimpleNamespace(name='posix', environ=environment)
        with patch('orchestration.contest_preflight.os', marker), \
             patch('orchestration.contest_preflight.fernet_dependency_available', return_value=True), \
             patch('integration.private_state.resolve_protector', side_effect=AssertionError('NO_CRYPTO_PROBE')), \
             patch('integration.private_state.unprotect_bytes', side_effect=AssertionError('NO_RECOVERY')):
            result = self.check(publish=True)
        self.assertTrue(result['local_preflight_passed'])
        protection = result['checks']['protected_evidence']
        self.assertEqual(protection['selected_backend'], 'fernet')
        self.assertTrue(protection['fernet_key_present'])
        self.assertTrue(protection['optional_dependency_available'])
        self.assertEqual(protection['key_validity'], 'NOT_MEASURED')
        self.assertEqual(protection['encryption_probe'], 'NOT_MEASURED')
        self.assertFalse(protection['dependency_import_verified'])
        self.assertNotIn('TEST_ONLY_PRESENT_VALUE', json.dumps(result))

    def test_fernet_missing_key_or_dependency_and_invalid_mode_block_without_reflecting_values(self):
        marker = SimpleNamespace(name='posix', environ={'FREYA_EVIDENCE_PROTECTION': 'fernet'})
        with patch('orchestration.contest_preflight.os', marker), \
             patch('orchestration.contest_preflight.fernet_dependency_available', return_value=False):
            result = self.check(include_evidence=True)
        self.assertIn('FERNET_EVIDENCE_KEY_PREREQUISITE_MISSING', result['blockers'])
        self.assertIn('FERNET_EVIDENCE_DEPENDENCY_UNAVAILABLE', result['blockers'])
        marker.environ = {'FREYA_EVIDENCE_PROTECTION': 'TEST_ONLY_PRIVATE_INVALID_LABEL'}
        with patch('orchestration.contest_preflight.os', marker):
            invalid = self.check(include_evidence=True)
        self.assertIn('EVIDENCE_PROTECTION_MODE_INVALID', invalid['blockers'])
        self.assertEqual(invalid['checks']['protected_evidence']['selected_backend'], 'INVALID')
        self.assertNotIn('TEST_ONLY_PRIVATE_INVALID_LABEL', json.dumps(invalid))

    def test_preflight_cli_forwards_mode_and_rejects_unknown_mode_without_inspection(self):
        with patch('orchestration.contest_preflight.inspect', return_value={'status': 'BLOCKED'}) as reader, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['brief.md', 'api.json', '--expected-commit', COMMIT,
                                  '--publish', '--evidence-mode', 'trace-only']), 2)
        self.assertEqual(reader.call_args.kwargs['evidence_mode'], 'trace-only')
        with patch('orchestration.contest_preflight.inspect') as reader, \
             patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as stopped:
                main(['brief.md', 'api.json', '--expected-commit', COMMIT,
                      '--evidence-mode', 'unknown'])
        self.assertEqual(stopped.exception.code, 2)
        reader.assert_not_called()

    def test_default_protected_evidence_is_not_claimed_portable_to_posix(self):
        # Patch only while inspect reads the marker; preserve native Path type.
        original = self.check
        class EnvironmentMarker:
            name = 'posix'
            import os as _os
            environ = _os.environ
        with patch('orchestration.contest_preflight.os', EnvironmentMarker):
            result = original(include_evidence=True)
        self.assertIn('DEFAULT_EVIDENCE_PROTECTOR_REQUIRES_WINDOWS', result['blockers'])

    def test_git_blob_batch_handles_missing_and_truncated_data(self):
        def response(args, **kwargs):
            self.assertIn(args[-1], ('a' * 40 + '^{commit}', '--batch'))
            if args[-1] != '--batch':
                return SimpleNamespace(returncode=0, stdout=(COMMIT + '\n').encode())
            return SimpleNamespace(returncode=0, stdout=(b'c' * 40 + b' blob 2\nok\n' +
                (COMMIT + ':builder/absent.py missing\n').encode()))
        with patch('orchestration.contest_preflight.subprocess.run', side_effect=response):
            pins, missing = git_sources(self.checkout, COMMIT, ['builder/build.py', 'builder/absent.py'])
        self.assertEqual(pins, {'builder/build.py': sha(b'ok')})
        self.assertEqual(missing, ['builder/absent.py'])
        def truncated(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=((COMMIT + '\n').encode()
                if args[-1] != '--batch' else b'c' * 40 + b' blob 5\nok'))
        with patch('orchestration.contest_preflight.subprocess.run', side_effect=truncated):
            with self.assertRaisesRegex(PreflightError, 'BATCH_INVALID'):
                git_sources(self.checkout, COMMIT, ['builder/build.py'])


if __name__ == '__main__':
    unittest.main()
