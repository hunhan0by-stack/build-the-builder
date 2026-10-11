"""Regression for full native transcripts exceeding the ordinary API body cap."""
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from builder.models import FoundationError
from orchestration.contest_build import Budget, FreyaClient
from tests_orchestration.test_contest_build import CaptureSpy

CALL = '/api/v2/call/83ff0f54-6879-4ef0-b8c6-1e56a7e14fa5'

class Response(BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}

class CallResponseSizeTests(unittest.TestCase):
    def make(self, size):
        payload = b'{"success":true,"data":{"result":{"text":"' + b'x'*size + b'"}}}'
        sink = CaptureSpy()
        with patch.dict('os.environ', {'FREYA_API_KEY':'synthetic-only'}):
            client = FreyaClient(Budget(60), [])
        client.evidence = sink
        client.opener = SimpleNamespace(open=lambda *a, **kw: Response(payload))
        return client, sink, payload

    def test_full_call_larger_than_four_mb_is_captured_and_parsed(self):
        client, sink, payload = self.make(7_300_000)
        result = client.api('GET', CALL)
        self.assertEqual(len(result['text']), 7_300_000)
        self.assertEqual(sink.cache[0]['body'], payload)
        self.assertIsNone(sink.pending)

    def test_larger_cap_does_not_apply_to_other_endpoints(self):
        client, sink, _ = self.make(4_000_001)
        with self.assertRaises(FoundationError):
            client.api('GET', '/api/v2/workflow/test')
        self.assertTrue(sink.failed)
        self.assertEqual(sink.cache, [])

    def test_call_read_remains_bounded_and_unverified_when_truncated(self):
        client, sink, _ = self.make(8_000_001)
        with self.assertRaises(FoundationError):
            client.api('GET', CALL)
        self.assertTrue(sink.failed)
        self.assertEqual(sink.cache, [])

    def test_large_call_binding_keeps_identity_and_terminal_checks(self):
        from builder.freya_lifecycle import verify_simulation_call_binding, LifecycleError
        identifier='11111111-1111-4111-8111-111111111111'
        run={'id':'synthetic-run','callId':identifier,'personaId':identifier}
        call={'id':identifier,'workspaceId':identifier,'assistantWorkflowId':identifier,
              'startedAt':'2026-10-11T16:00:00Z','endedAt':'2026-10-11T16:04:00Z',
              'messages':[{'message':'x'*7_300_000}]}
        kwargs=dict(workspace_id=identifier,workflow_id=identifier,simulation_id=identifier,persona_id=identifier)
        self.assertTrue(verify_simulation_call_binding(run,call,**kwargs)['lifecycle_ended'])
        call['workspaceId']='22222222-2222-4222-8222-222222222222'
        with self.assertRaisesRegex(LifecycleError,'IDENTITY_MISMATCH'):
            verify_simulation_call_binding(run,call,**kwargs)

if __name__ == '__main__':
    unittest.main()
