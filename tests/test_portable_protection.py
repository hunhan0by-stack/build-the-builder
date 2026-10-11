"""Merged production tests. Fresh ephemeral keys, own synthetic bytes, no sockets.

DPAPI backwards compatibility is checked by route selection with a mock, not
by recovering a user's profile or an existing private evidence bundle.
"""
import base64
import builtins
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration import private_state as ps
from integration import response_diagnostics as diagnostics
from integration.contest_evidence import ContestEvidence, ContestEvidenceError
from integration.native_pilot import NativeResponseRecorder
from orchestration import contest_chat_evidence as chat
from integration.session_guard import PreparationError
from orchestration.contest_build import FreyaClient


def fresh_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')


def blocked_import(name, *args, **kwargs):
    if name == 'cryptography' or name.startswith('cryptography.'):
        raise ImportError('TEST_ONLY_DEPENDENCY_UNAVAILABLE')
    return ORIGINAL_IMPORT(name, *args, **kwargs)


ORIGINAL_IMPORT = builtins.__import__


class NeverOpen:
    def __init__(self):
        self.attempts = 0

    def open(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError('TEST_FORBIDS_PLATFORM_DISPATCH')


def local_client(sink):
    # Bypass constructor so no environment credentials or kit auth is read.
    client = FreyaClient.__new__(FreyaClient)
    client.budget = SimpleNamespace(remaining=lambda: 30)
    client.trace, client.audit_path = [], None
    client.evidence, client.last_exchange = sink, None
    client.key = 'TEST_ONLY_SYNTHETIC_AUTH_NEVER_SENT'
    client.url = 'https://staging-app.freyavoice.ai'
    client.opener = NeverOpen()
    return client


class PortableProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.key = fresh_key()
        own_env = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
                   if name in os.environ}
        own_env.update(FREYA_EVIDENCE_PROTECTION='fernet', FREYA_EVIDENCE_KEY=self.key)
        self.env = patch.dict(os.environ, own_env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_round_trip_empty_unicode_binary_and_native_limit(self):
        for raw in (b'', 'Türkçe ödeme sözünü koru'.encode(), bytes(range(256)), os.urandom(8_000_000)):
            with self.subTest(byte_count=len(raw)):
                token = ps.protect_bytes(raw)
                self.assertTrue(token.startswith(ps.FERNET_PREFIX))
                self.assertEqual(ps.unprotect_bytes(token), raw)
                if len(raw) >= 16:
                    self.assertNotIn(raw, token)

    def test_identical_plaintext_has_distinct_authenticated_ciphertexts(self):
        raw = b'OWN_SYNTHETIC_BINARY_PAYLOAD'
        a, b = ps.protect_bytes(raw), ps.protect_bytes(raw)
        self.assertNotEqual(a, b)
        self.assertEqual(ps.unprotect_bytes(a), ps.unprotect_bytes(b))

    def test_tampered_token_never_returns_plaintext_or_tries_dpapi(self):
        token = bytearray(ps.protect_bytes(b'OWN_SYNTHETIC_SECRET'))
        index = len(ps.FERNET_PREFIX) + 25
        token[index] = ord('A') if token[index] != ord('A') else ord('B')
        with patch.object(ps, '_dpapi_unprotect', side_effect=AssertionError('NO_FALLBACK')):
            with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_RECOVERY_FAILED'):
                ps.unprotect_bytes(bytes(token))

    def test_wrong_key_never_returns_plaintext_or_tries_dpapi(self):
        token = ps.protect_bytes(b'OWN_SYNTHETIC_SECRET')
        os.environ['FREYA_EVIDENCE_KEY'] = fresh_key()
        with patch.object(ps, '_dpapi_unprotect', side_effect=AssertionError('NO_FALLBACK')):
            with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_RECOVERY_FAILED'):
                ps.unprotect_bytes(token)

    def test_lost_key_blocks_recovery(self):
        token = ps.protect_bytes(b'OWN_SYNTHETIC_SECRET')
        del os.environ['FREYA_EVIDENCE_KEY']
        with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_KEY_MISSING_OR_INVALID'):
            ps.unprotect_bytes(token)

    def test_truncated_empty_and_malformed_framed_tokens_fail_without_fallback(self):
        token = ps.protect_bytes(b'OWN_SYNTHETIC_SECRET')
        for value in (ps.FERNET_PREFIX, token[:-10], ps.FERNET_PREFIX + b'not-a-token'):
            with self.subTest(case_bytes=len(value)):
                with patch.object(ps, '_dpapi_unprotect', side_effect=AssertionError('NO_FALLBACK')):
                    with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_RECOVERY_FAILED'):
                        ps.unprotect_bytes(value)

    def test_unknown_frame_version_never_uses_legacy_decoder(self):
        with patch.object(ps, '_dpapi_unprotect', side_effect=AssertionError('NO_FALLBACK')):
            with self.assertRaisesRegex(PreparationError, 'EVIDENCE_PROTECTION_FRAME_UNSUPPORTED'):
                ps.unprotect_bytes(b'FREYA-EVIDENCE:fernet:v2\x00synthetic')

    def test_raw_legacy_bytes_use_original_dpapi_even_in_fernet_mode(self):
        with patch.object(ps, '_dpapi_unprotect', return_value=b'OWN_SYNTHETIC_LEGACY') as legacy:
            self.assertEqual(ps.unprotect_bytes(b'TEST_ONLY_LEGACY_FRAME'), b'OWN_SYNTHETIC_LEGACY')
        legacy.assert_called_once_with(b'TEST_ONLY_LEGACY_FRAME')

    def test_default_mode_remains_dpapi_without_optional_import(self):
        del os.environ['FREYA_EVIDENCE_PROTECTION']
        with patch.object(ps, '_dpapi_protect', return_value=b'TEST_ONLY_DPAPI_ROUTE') as legacy:
            with patch('builtins.__import__', side_effect=blocked_import):
                self.assertEqual(ps.protect_bytes(b'OWN_SYNTHETIC'), b'TEST_ONLY_DPAPI_ROUTE')
        legacy.assert_called_once_with(b'OWN_SYNTHETIC')

    def test_non_windows_dpapi_default_has_no_automatic_fernet_or_plaintext(self):
        del os.environ['FREYA_EVIDENCE_PROTECTION']
        with patch.object(ps, 'os', SimpleNamespace(name='posix', environ=os.environ)):
            with self.assertRaisesRegex(PreparationError, 'Windows user DPAPI profile'):
                ps.protect_bytes(b'OWN_SYNTHETIC')

    def test_explicit_fernet_roundtrip_does_not_depend_on_windows(self):
        with patch.object(ps, 'os', SimpleNamespace(name='posix', environ=os.environ)):
            with patch.object(ps, '_dpapi_protect', side_effect=AssertionError('NO_DPAPI')):
                token = ps.protect_bytes(b'OWN_SYNTHETIC')
                self.assertEqual(ps.unprotect_bytes(token), b'OWN_SYNTHETIC')

    def test_key_validation_rejects_missing_bad_length_padding_noncanonical_and_nonascii(self):
        # Canonical padding bits must be zero; the '=' and 43-symbol length
        # alone do not establish a valid canonical 32-byte key.
        noncanonical = base64.urlsafe_b64encode(b'\x00' * 32).decode()[:-2] + 'B='
        for value in (None, '', 'invalid', self.key[:-1], self.key + '\n',
                      base64.urlsafe_b64encode(b'x' * 31).decode(), noncanonical, 'é' * 43 + '='):
            with self.subTest(reason='missing_or_malformed'):
                if value is None:
                    os.environ.pop('FREYA_EVIDENCE_KEY', None)
                else:
                    os.environ['FREYA_EVIDENCE_KEY'] = value
                with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_KEY_MISSING_OR_INVALID'):
                    ps.resolve_protector()

    def test_unknown_mode_is_a_configuration_error(self):
        os.environ['FREYA_EVIDENCE_PROTECTION'] = 'automatic'
        with self.assertRaisesRegex(PreparationError, 'EVIDENCE_PROTECTION_MODE_INVALID'):
            ps.protect_bytes(b'OWN_SYNTHETIC')

    def test_missing_library_fails_preflight_without_plaintext(self):
        with patch('builtins.__import__', side_effect=blocked_import):
            with self.assertRaisesRegex(PreparationError, 'EVIDENCE_FERNET_DEPENDENCY_UNAVAILABLE'):
                NativeResponseRecorder(self.folder / 'wire')
        self.assertFalse((self.folder / 'wire').exists())

    def test_missing_key_blocks_default_contest_sink_before_first_dispatch(self):
        del os.environ['FREYA_EVIDENCE_KEY']
        client = local_client(None)
        with self.assertRaises(PreparationError):
            client.evidence = ContestEvidence(self.folder / 'evidence')
            client.api('POST', '/api/v2/workflow', {'name': 'TEST_ONLY'})
        self.assertEqual(client.opener.attempts, 0)
        self.assertFalse(list(self.folder.rglob('*.dpapi')))

    def test_missing_library_blocks_default_contest_sink_before_first_dispatch(self):
        client = local_client(None)
        with patch('builtins.__import__', side_effect=blocked_import):
            with self.assertRaises(PreparationError):
                client.evidence = ContestEvidence(self.folder / 'evidence')
                client.api('POST', '/api/v2/workflow', {'name': 'TEST_ONLY'})
        self.assertEqual(client.opener.attempts, 0)

    def test_malformed_key_blocks_default_contest_sink_before_first_dispatch(self):
        os.environ['FREYA_EVIDENCE_KEY'] = self.key + 'INVALID'
        client = local_client(None)
        with self.assertRaises(PreparationError):
            client.evidence = ContestEvidence(self.folder / 'evidence')
            client.api('POST', '/api/v2/workflow', {'name': 'TEST_ONLY'})
        self.assertEqual(client.opener.attempts, 0)
        self.assertFalse(list(self.folder.rglob('*.dpapi')))

    def test_existing_default_references_select_fernet_and_metadata_is_true(self):
        sink = ContestEvidence(self.folder / 'evidence')
        self.assertEqual(sink.recorder.protection, 'fernet_v1')
        for path in (self.folder / 'evidence' / 'wire').glob('*.dpapi'):
            self.assertTrue(path.read_bytes().startswith(ps.FERNET_PREFIX))
        metadata = [json.loads(path.read_text()) for path in sink.recorder.directory.glob('*.json')
                    if '.dpapi.' not in path.name]
        self.assertTrue(metadata)
        self.assertTrue(all(row.get('body_storage') == 'fernet_v1' for row in metadata))
        for path in self.folder.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.key.encode('ascii'), path.read_bytes())

    def test_native_full_requests_responses_headers_and_secret_persist_use_real_fernet(self):
        recorder = NativeResponseRecorder(self.folder / 'wire')
        receipt = recorder.store_full('request', b'OWN_SYNTHETIC_REQUEST')
        self.assertEqual(ps.unprotect_bytes((recorder.directory / receipt['file']).read_bytes()),
                         b'OWN_SYNTHETIC_REQUEST')
        recorder.record(method='LOCAL', path='/local', http_status=200,
                        headers={'x-request-id': 'OWN_SYNTHETIC_HEADER'}, body=b'OWN_SYNTHETIC_RESPONSE')
        headers = next(recorder.directory.glob('*.headers.dpapi'))
        self.assertIn(b'OWN_SYNTHETIC_HEADER', ps.unprotect_bytes(headers.read_bytes()))
        destination = self.folder / 'own-session.dpapi'
        ps.persist_secret(destination, 'OWN_SYNTHETIC_SESSION')
        self.assertEqual(ps.unprotect_bytes(destination.read_bytes()), b'OWN_SYNTHETIC_SESSION')

    def test_midrun_key_change_latches_before_real_client_opener(self):
        sink = ContestEvidence(self.folder / 'evidence')
        client = local_client(sink)
        os.environ['FREYA_EVIDENCE_KEY'] = fresh_key()
        with self.assertRaisesRegex(ContestEvidenceError, 'REQUEST_CAPTURE_FAILED_NO_DISPATCH'):
            client.api('POST', '/api/v2/workflow', {'name': 'TEST_ONLY'})
        self.assertEqual(client.opener.attempts, 0)
        self.assertTrue(sink.failed)
        self.assertTrue(sink.recorder.failed)
        with self.assertRaisesRegex(ContestEvidenceError, 'EVIDENCE_LATCHED_OR_SEALED'):
            client.api('POST', '/api/v2/workflow', {'name': 'TEST_ONLY'})
        self.assertEqual(client.opener.attempts, 0)

    def test_midrun_mode_change_is_rejected_by_bound_protector(self):
        protector = ps.resolve_protector()
        os.environ['FREYA_EVIDENCE_PROTECTION'] = 'dpapi'
        with self.assertRaisesRegex(PreparationError, 'EVIDENCE_PROTECTION_CONFIG_CHANGED'):
            protector(b'OWN_SYNTHETIC')

    def test_secret_error_messages_never_echo_key(self):
        os.environ['FREYA_EVIDENCE_KEY'] = self.key + 'INVALID'
        try:
            ps.protect_bytes(b'OWN_SYNTHETIC')
        except PreparationError as error:
            self.assertNotIn(self.key, str(error))
            self.assertEqual(str(error), 'EVIDENCE_FERNET_KEY_MISSING_OR_INVALID')
        else:
            self.fail('Malformed key was admitted')

    def test_secret_file_creation_failure_does_not_replace_existing_file(self):
        destination = self.folder / 'own-session.dpapi'
        ps.persist_secret(destination, 'OWN_SYNTHETIC_A')
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            ps.persist_secret(destination, 'OWN_SYNTHETIC_B')
        self.assertEqual(before, destination.read_bytes())

    def test_chat_evidence_label_tracks_real_default_and_injected_is_unverified(self):
        default = chat.ChatEvidence(self.folder / 'default')
        self.assertEqual(default.protection, 'fernet_v1')
        # Reversible fixture route stays explicitly unverified; never counted
        # as production encryption, even if environment mode says Fernet.
        injected = chat.ChatEvidence(self.folder / 'injected',
            protector=lambda value: b'TEST_ONLY_UNVERIFIED\x00' + value[::-1])
        self.assertEqual(injected.protection, 'injected_protector_not_verified')

    def test_synthetic_chat_complete_wire_join_uses_real_fernet_without_live_claim(self):
        from tests_orchestration.test_contest_chat import ChatTests
        fixture = ChatTests('test_capture_recomputes_exact_wire_join_and_public_privacy')
        fixture.setUp()
        try:
            def real_factory(directory, **kwargs):
                sink = chat.ChatEvidence(directory, **kwargs)
                fixture.sinks.append(sink)
                return sink
            result = fixture.run_capture(evidence_factory=real_factory)
            self.assertEqual(result['status'], 'CHAT_CONVERSATION_CAPTURED')
            self.assertEqual(len(fixture.http.requests), 7)
            checked = chat.verify_chat_bundle(fixture.private, result['protected_evidence_sha256'])
            self.assertTrue(checked['wire_join_verified'])
            self.assertTrue(checked['production_encryption_verified'])
            self.assertEqual(checked['protection'], 'fernet_v1')
            self.assertFalse(checked['live_success_verified'])
            self.assertFalse(checked['final_submission_ready'])
            seal_path = fixture.private / 'FINAL.json'
            seal = json.loads(seal_path.read_bytes())
            seal['protection'] = 'windows_user_dpapi'
            seal_path.write_bytes(chat.raw(seal))
            mislabeled = chat.verify_chat_bundle(fixture.private, chat.sha(seal_path.read_bytes()))
            self.assertFalse(mislabeled['production_encryption_verified'])
        finally:
            fixture.doCleanups()

    def test_model_subprocess_environment_excludes_new_key_without_model_execution(self):
        from builder import model_workflow
        own_directory = self.folder / 'model-env-probe'
        own_directory.mkdir()
        seen = []
        def blocked_process(command, **kwargs):
            seen.append(kwargs['env'])
            self.assertNotIn('FREYA_EVIDENCE_KEY', kwargs['env'])
            self.assertNotIn('FREYA_EVIDENCE_PROTECTION', kwargs['env'])
            self.assertNotIn(self.key, kwargs['input'])
            raise OSError('TEST_ONLY_NO_MODEL_PROCESS')
        provider = model_workflow.CodexWorkflowProvider(executable='TEST_ONLY_NOT_EXECUTED',
            model='gpt-6.1-sol', reasoning_effort='medium', transport='default')
        with patch.object(model_workflow, 'prompt_for', return_value='OWN_SYNTHETIC_PROMPT'):
            with patch.object(model_workflow.subprocess, 'run', side_effect=blocked_process):
                with self.assertRaises(model_workflow.FoundationError):
                    provider.generate(brief=None, api=None, references=[], directory=own_directory,
                                      remaining_seconds=1)
        self.assertEqual(len(seen), 1)

    def test_input_types_never_enter_crypto(self):
        for value in (None, 'text', bytearray(b'bytes')):
            with self.assertRaises(PreparationError):
                ps.protect_bytes(value)
        for value in (None, 'text', b''):
            with self.assertRaises(PreparationError):
                ps.unprotect_bytes(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
