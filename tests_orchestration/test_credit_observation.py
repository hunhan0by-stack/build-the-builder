"""Fresh fake-client credit snapshots only; no old test groups or live access."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from builder.models import FoundationError
from orchestration.credit_observation import CreditObservation


WORKSPACE = '00000000-0000-4000-8000-000000000001'


class FakeClient:
    def __init__(self, values):
        self.kit = SimpleNamespace(_ws=WORKSPACE)
        self.evidence = SimpleNamespace(failed=False, pending=None, sealed=False,
            recorder=SimpleNamespace(failed=False), exchanges={}, cache={})
        self.last_exchange = None
        self.values = list(values)
        self.calls = []

    def api(self, method, path):
        self.calls.append((method, path))
        returned = self.values.pop(0)
        if isinstance(returned, Exception):
            self.evidence.failed = True
            self.evidence.pending = ('unknown',)
            raise returned
        ticket = str(uuid.uuid4())
        self.last_exchange = ticket
        self.evidence.exchanges[ticket] = {'status': 'CAPTURED', 'exchange_id': ticket}
        self.evidence.cache[ticket] = (
            {'method': method, 'path': path, 'params': {}, 'body_base64': None},
            {'http_status': 200, 'body': json.dumps({'success': True,
                'data': {'result': returned, 'message': 'SYNTHETIC_PRIVATE_MESSAGE'}}).encode()})
        return copy.deepcopy(returned)


class CreditObservationTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')),
            patch('socket.getaddrinfo', side_effect=AssertionError('DNS_FORBIDDEN')),
            patch('subprocess.Popen', side_effect=AssertionError('MODEL_PROCESS_FORBIDDEN')),
            patch('integration.private_state.protect_bytes', side_effect=AssertionError('DPAPI_FORBIDDEN')),
            patch('integration.private_state.unprotect_bytes', side_effect=AssertionError('RECOVERY_FORBIDDEN')),
            patch('os.environ', {}),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def make(self, values, times=None):
        client = FakeClient(values)
        origin = datetime(2026, 10, 11, tzinfo=timezone.utc)
        clock = iter(times or [origin + timedelta(seconds=n) for n in range(4)])
        return client, CreditObservation(client, WORKSPACE, clock=lambda: next(clock))

    def test_same_workspace_rate_and_captured_ordered_delta_is_not_test_price(self):
        client, observation = self.make([
            {'creditsLeft': 499.99, 'creditUsdRate': .01, 'credential': 'SYNTHETIC_PRIVATE_KEY'},
            {'creditsLeft': 499.97, 'creditUsdRate': .01}])
        before = observation.before()
        result = observation.after(before)
        self.assertEqual(client.calls, [('GET', '/api/v2/workspaces/' + WORKSPACE + '/credits')] * 2)
        self.assertAlmostEqual(result['observed_workspace_credit_decrease'], .02)
        self.assertFalse(result['isolated_test_price_verified'])
        self.assertFalse(result['official_test_pass_verified'])
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assertNotIn('credential', json.dumps(result))
        self.assertNotEqual(result['before']['capture_exchange_id'], result['after']['capture_exchange_id'])

    def test_topup_is_negative_workspace_delta_not_negative_test_price(self):
        client, observation = self.make([{'creditsLeft': 199.99, 'creditUsdRate': .01},
                                        {'creditsLeft': 499.99, 'creditUsdRate': .01}])
        result = observation.after(observation.before())
        self.assertEqual(result['observed_workspace_credit_decrease'], -300)
        self.assertFalse(result['isolated_test_price_verified'])
        self.assertEqual(len(client.calls), 2)

    def test_missing_or_modified_before_never_sends_after_read(self):
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        with self.assertRaisesRegex(FoundationError, '^CREDIT_BEFORE_INVALID_NO_READ$'):
            observation.after(None)
        self.assertEqual(client.calls, [])
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        before = observation.before()
        before['creditsLeft'] = 500
        with self.assertRaisesRegex(FoundationError, '^CREDIT_BEFORE_INVALID_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)
        client, observation = self.make([{'creditsLeft': 1, 'creditUsdRate': 0}])
        before = observation.before()
        before['creditsLeft'] = True  # Python equality would equate True and 1.
        with self.assertRaisesRegex(FoundationError, '^CREDIT_BEFORE_INVALID_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)

    def test_bad_numbers_latch_and_never_allow_new_after_read(self):
        for invalid in (True, False, None, '200', -1, float('nan'), float('inf'), 10 ** 1000):
            for field in ('creditsLeft', 'creditUsdRate'):
                with self.subTest(field=field, value_type=type(invalid).__name__):
                    value = {'creditsLeft': 200, 'creditUsdRate': .01, field: invalid}
                    client, observation = self.make([value])
                    with self.assertRaisesRegex(FoundationError, '^CREDIT_VALUES_INVALID_NO_RETRY$'):
                        observation.before()
                    with self.assertRaises(FoundationError):
                        observation.after(None)
                    with self.assertRaisesRegex(FoundationError, '^CREDIT_BEFORE_ALREADY_ATTEMPTED_NO_RETRY$'):
                        observation.before()
                    self.assertEqual(len(client.calls), 1)

    def test_failed_unknown_pending_or_sealed_evidence_prevents_read(self):
        for field, value in (('failed', True), ('pending', ('unknown',)), ('sealed', True)):
            with self.subTest(field=field):
                client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
                setattr(client.evidence, field, value)
                with self.assertRaisesRegex(FoundationError, '^CREDIT_EVIDENCE_NOT_READY_NO_READ$'):
                    observation.before()
                self.assertEqual(client.calls, [])
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        before = observation.before()
        client.evidence.failed = True
        with self.assertRaisesRegex(FoundationError, '^CREDIT_EVIDENCE_NOT_READY_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)

    def test_provider_failure_hides_exception_and_has_no_retry(self):
        client, observation = self.make([RuntimeError('SYNTHETIC_PRIVATE_KEY')])
        with self.assertRaisesRegex(FoundationError, '^CREDIT_READ_FAILED_NO_RETRY$') as error:
            observation.before()
        self.assertNotIn('PRIVATE', str(error.exception))
        with self.assertRaises(FoundationError):
            observation.before()
        with self.assertRaises(FoundationError):
            observation.after(None)
        self.assertEqual(len(client.calls), 1)

    def test_workspace_pin_change_or_invalid_identity_cannot_dispatch(self):
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        client.kit._ws = '00000000-0000-4000-8000-000000000002'
        with self.assertRaisesRegex(FoundationError, '^CREDIT_WORKSPACE_BINDING_INVALID$'):
            observation.before()
        self.assertEqual(client.calls, [])
        for value in ('bad', 'ABCDEF00-0000-4000-8000-000000000001',
                      '00000000-0000-0000-0000-000000000000', None):
            with self.assertRaises(FoundationError):
                CreditObservation(FakeClient([]), value)

    def test_rate_change_rejects_comparison_and_cannot_read_again(self):
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01},
                                        {'creditsLeft': 199, 'creditUsdRate': .02}])
        before = observation.before()
        with self.assertRaisesRegex(FoundationError, '^CREDIT_RATE_CHANGED_NO_COMPARISON$'):
            observation.after(before)
        with self.assertRaisesRegex(FoundationError, '^CREDIT_AFTER_ALREADY_ATTEMPTED_NO_RETRY$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 2)

    def test_backward_clock_before_after_read_does_not_dispatch(self):
        origin = datetime(2026, 10, 11, tzinfo=timezone.utc)
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}],
            [origin, origin + timedelta(seconds=2), origin + timedelta(seconds=1)])
        before = observation.before()
        with self.assertRaisesRegex(FoundationError, '^CREDIT_CLOCK_ORDER_INVALID_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)

    def test_uncaptured_or_false_capture_cannot_become_credit_evidence(self):
        for mutation in ('missing', 'wrong-path', 'wrong-body'):
            with self.subTest(mutation=mutation):
                client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
                real = client.api
                def false_capture(method, path):
                    result = real(method, path)
                    ticket = client.last_exchange
                    if mutation == 'missing':
                        client.evidence.exchanges.clear()
                    elif mutation == 'wrong-path':
                        client.evidence.cache[ticket][0]['path'] = '/api/v2/workspaces/foreign/credits'
                    else:
                        result['creditsLeft'] = 500
                    return result
                client.api = false_capture
                with self.assertRaisesRegex(FoundationError, '^CREDIT_RESPONSE_CAPTURE_UNVERIFIED_NO_RETRY$'):
                    observation.before()
                with self.assertRaises(FoundationError):
                    observation.after(None)
                self.assertEqual(len(client.calls), 1)

    def test_healthy_sink_swap_cannot_bypass_sealed_failed_original(self):
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        before = observation.before()
        original = client.evidence
        original.failed, original.sealed = True, True
        client.evidence = FakeClient([]).evidence
        self.assertIsNot(client.evidence, original)
        with self.assertRaisesRegex(FoundationError, '^CREDIT_EVIDENCE_BINDING_CHANGED_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)

    def test_same_sink_with_replaced_healthy_recorder_cannot_read_after(self):
        client, observation = self.make([{'creditsLeft': 200, 'creditUsdRate': .01}])
        before = observation.before()
        original = client.evidence.recorder
        original.failed = True
        client.evidence.recorder = SimpleNamespace(failed=False)
        self.assertIsNot(client.evidence.recorder, original)
        with self.assertRaisesRegex(FoundationError, '^CREDIT_EVIDENCE_BINDING_CHANGED_NO_READ$'):
            observation.after(before)
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
