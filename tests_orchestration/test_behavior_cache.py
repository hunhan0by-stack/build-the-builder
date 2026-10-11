"""Cached code must preserve isolation, diagnostics and execution bounds."""
import importlib
import time
import unittest

FAST = importlib.import_module('orchestration.generated_behavior')

class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        FAST._checked_code.cache_clear()

    def execute(self, code, state=None, **extra):
        value = {} if state is None else state
        FAST._execute(code, value, time.monotonic() + 2, **extra)
        return value

    def test_cached_code_uses_fresh_customer_state(self):
        code = "vars['answer'] = vars['input'] + 1"
        self.assertEqual(self.execute(code, {'input': 1})['answer'], 2)
        self.assertEqual(self.execute(code, {'input': 9})['answer'], 10)
        self.assertEqual(FAST._checked_code.cache_info().hits, 1)

    def test_updated_code_cannot_reuse_old_bytecode(self):
        self.assertEqual(self.execute("vars['answer'] = 1")['answer'], 1)
        self.assertEqual(self.execute("vars['answer'] = 9")['answer'], 9)

    def test_api_hook_context_is_not_shared(self):
        code = "config['body'] = dict(customer_id=vars['customer_id'])"
        first, second = {}, {}
        self.execute(code, {'customer_id': 'first'}, config=first)
        self.execute(code, {'customer_id': 'second'}, config=second)
        self.assertEqual(first['body'], {'customer_id': 'first'})
        self.assertEqual(second['body'], {'customer_id': 'second'})

    def test_cached_code_still_enforces_expired_deadline(self):
        code = "vars['answer'] = 1"
        self.execute(code)
        with self.assertRaisesRegex(FAST.BehaviorError, 'BEHAVIOR_ACTION_BUDGET_EXCEEDED'):
            FAST._execute(code, {}, time.monotonic() - 1)

    def test_cached_code_still_enforces_instruction_limit(self):
        code = "while vars.get('loop'):\n    pass"
        self.execute(code, {'loop': False})
        with self.assertRaisesRegex(FAST.BehaviorError, 'BEHAVIOR_ACTION_BUDGET_EXCEEDED'):
            self.execute(code, {'loop': True})

    def test_cache_cannot_authorize_changed_unsafe_source(self):
        self.execute("vars['answer'] = 1")
        for code in ("import os", "vars['answer'] = open('file')", "vars['answer'] = str.__class__"):
            with self.subTest(code=code), self.assertRaises(FAST.BehaviorError):
                self.execute(code)
        self.assertEqual(FAST._checked_code.cache_info().currsize, 1)

    def test_cache_has_a_fixed_memory_entry_bound(self):
        for index in range(32):
            self.execute("vars['answer'] = " + str(index))
        self.assertEqual(FAST._checked_code.cache_info().currsize, 8)

    def test_invalid_syntax_and_compile_failures_preserve_diagnostics(self):
        for code, diagnostic in (
            ('return 1', 'BEHAVIOR_ACTION_EXECUTION_FAILED'),
            ('break', 'BEHAVIOR_ACTION_EXECUTION_FAILED'),
            ("vars[ = 1", 'BEHAVIOR_CODE_SYNTAX_INVALID'),
            (['not-a-string'], 'BEHAVIOR_CODE_INVALID')):
            with self.subTest(code=code), self.assertRaisesRegex(FAST.BehaviorError, diagnostic):
                self.execute(code)

if __name__ == '__main__':
    unittest.main(verbosity=2)
