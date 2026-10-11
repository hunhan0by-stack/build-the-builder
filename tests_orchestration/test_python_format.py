"""Synthetic source only: no generated candidate execution or network calls."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from builder.models import FoundationError
from builder.python_format import FormatRefused, canonical_bytes, normalize_code_blocks, normalize_python
from builder.model_workflow import CodexWorkflowProvider
from builder.model_wire import ACTION, NODE, render_plan
from builder.api_contract import BankContract
from tests_orchestration.test_model_wire import empty
from tests_orchestration.test_extraction_interface import listener


class PythonFormatTests(unittest.TestCase):
    def test_nested_ops_ast_equal_and_idempotent_without_execution(self):
        code = 'raise RuntimeError("must never execute")\nresult = {"a": {"b": {"c": 1}}}\n'
        actual, receipt = normalize_python(code)
        self.assertEqual(actual, code.replace('}}}', '} } }'))
        self.assertEqual(ast.dump(ast.parse(code)), ast.dump(ast.parse(actual)))
        self.assertEqual(receipt['inserted_spaces'], 2)
        self.assertTrue(receipt['ast_equivalent'])
        self.assertTrue(receipt['compile_checked'])
        self.assertFalse(receipt['code_executed'])
        self.assertFalse(receipt['semantic_correctness_verified'])
        self.assertEqual(normalize_python(actual)[0], actual)
        self.assertEqual(normalize_python(actual)[1]['status'], 'UNCHANGED')

    def test_unicode_crlf_formfeed_type_comment_preserved(self):
        for code in ('deger = {"çağrı": {"ö": 2}}\r\n',
                     '\fresult = {"a": {"b": 1}}\n',
                     'result = {"a": {"b": 1}} # type: dict\n',
                     'label = f"value {1}"\nresult = {"a": {"b": 1}}\n'):
            with self.subTest(code=code):
                actual, receipt = normalize_python(code)
                self.assertEqual(actual, code.replace('}}', '} }'))
                self.assertTrue(receipt['ast_equivalent'])

    def test_string_comment_template_and_fstring_delimiters_refused_atomically(self):
        for code in ('result = "}}"', 'result = {"a": {"b": 1}} # }}',
                     'result = "{{name}}"', 'result = f"escaped }}"',
                     'result = f"{ {1: {2: 3}} }"', 'result = { {1:2}} # {{',
                     'result = {"a": {"b": 1}}\ntext = "}}"'):
            blocks = [{'id': 'safe', 'code': 'result = {"a": {"b": 1}}'},
                      {'id': 'unsafe', 'code': code}]
            original = copy.deepcopy(blocks)
            with self.subTest(code=code), self.assertRaises(FormatRefused) as caught:
                normalize_code_blocks(blocks, 'a' * 64)
            self.assertEqual(blocks, original)
            self.assertEqual(caught.exception.receipt['status'], 'REFUSED')
            self.assertNotIn('output_code_blocks_sha256', caught.exception.receipt)

    def test_invalid_python_and_unproven_ast_refused(self):
        for code in ('result = {"a": }}', 'return {"a": {"b": 1}}'):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'ORIGINAL_PYTHON_NOT_PROVEN'):
                normalize_python(code)
        with patch('builder.python_format.ast.dump', side_effect=['one', 'two']):
            with self.assertRaisesRegex(ValueError, 'AST_CHANGED_REFUSED'):
                normalize_python('result = {"a": {"b": 1}}')

    def test_invalid_block_shape_and_hash_refused(self):
        for blocks, raw_hash in (([], None), ([], 'x'), ([{'id': 'a', 'code': 3}], 'a'*64),
            ([{'id': 'a', 'code': ''}, {'id': 'a', 'code': ''}], 'a'*64)):
            with self.subTest(blocks=blocks), self.assertRaisesRegex(FoundationError, 'CODE_BLOCK_INVALID'):
                normalize_code_blocks(blocks, raw_hash)

    def test_unchanged_source_and_hash_certificate(self):
        blocks = [{'id': 'one', 'code': 'result = {"a": {"b": 1}}'},
                  {'id': 'two', 'code': '# unchanged\nresult = 17\n'}]
        original = copy.deepcopy(blocks)
        actual, receipt = normalize_code_blocks(blocks, 'a'*64)
        self.assertEqual(blocks, original)
        self.assertEqual(actual[1], blocks[1])
        self.assertEqual(receipt['raw_response_sha256'], 'a'*64)
        self.assertEqual(receipt['input_code_blocks_sha256'], hashlib.sha256(canonical_bytes(blocks)).hexdigest())
        self.assertEqual(receipt['output_code_blocks_sha256'], hashlib.sha256(canonical_bytes(actual)).hexdigest())

    def test_existing_strict_gate_still_rejects_raw_and_accepts_normalized_all_hooks(self):
        contract = BankContract.from_path(Path(__file__).parent/'inputs/openapi.json')
        for placement in ('code_exec', 'pre_process_ref', 'post_process_ref'):
            node = empty(NODE)
            node.update(name='compute', node_type='code' if placement == 'code_exec' else 'api_request')
            action = empty(ACTION)
            action['kind'] = 'code_exec' if placement == 'code_exec' else 'verify_identity'
            action['code_ref' if placement == 'code_exec' else placement] = 'one'
            node['pre_actions'] = [action]
            plan = {'initial_node':'compute', 'system_prompt':'', 'nodes':[node]}
            blocks = [{'id':'one', 'code':'result = {"a": {"b": 1}}'}]
            with self.subTest(placement=placement):
                with self.assertRaisesRegex(FoundationError, 'TEMPLATE_DELIMITERS_REFUSED'):
                    render_plan(plan, blocks, contract.freya_templates())
                normalized, _ = normalize_code_blocks(blocks, 'a'*64)
                built = render_plan(plan, normalized, contract.freya_templates())
                rendered = built['nodes']['compute']['pre_actions'][0]
                actual = (rendered['function']['parameters']['properties']['code']['default']
                    if placement == 'code_exec' else rendered[placement.replace('_ref', '_code')])
                self.assertEqual(actual, normalized[0]['code'])

    def test_provider_preserves_raw_records_trace_and_unresolved_gate(self):
        for unresolved, code, expected_error in (([], 'result = {"a": {"b": 1}}', None),
            (['Missing source authority'], 'result = {"a": {"b": 1}}', 'UNRESOLVED_MODEL_POLICY'),
            ([], 'result = "}}"', 'TEMPLATE_DELIMITERS_REFUSED')):
            with self.subTest(error=expected_error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                node = empty(NODE)
                node.update(name='compute', node_type='code')
                action = empty(ACTION)
                action.update(kind='code_exec', code_ref='one')
                node['pre_actions'] = [action]
                response = {'workflow_plan':{'initial_node':'listen', 'system_prompt':'', 'nodes':[listener(),node]},
                    'code_blocks':[{'id':'one', 'code':code}], 'unresolved':unresolved,
                    'coverage':[{'quote':'Synthetic only.', 'nodes':['compute'], 'implementation':'Unit only.'}], 'limitations':[]}
                raw = json.dumps(response, indent=2).encode('utf-8') + b'\n'
                def process(*args, **kwargs):
                    (root/'model-response.json').write_bytes(raw)
                    events = '\n'.join(json.dumps({'type':t}) for t in ('thread.started','turn.started','turn.completed'))
                    return SimpleNamespace(returncode=0, stdout=events, stderr='')
                with patch('builder.model_workflow.subprocess.run', side_effect=process) as mocked:
                    provider = CodexWorkflowProvider(executable='synthetic-unused')
                    arguments = dict(brief='Synthetic only.', api={}, references={}, directory=root,
                                     remaining_seconds=10, tool_templates={'tools':{}})
                    if expected_error:
                        with self.assertRaisesRegex(FoundationError, expected_error):
                            provider.generate(**arguments)
                    else:
                        workflow, metadata = provider.generate(**arguments)
                        self.assertEqual(metadata['code_normalization']['status'], 'NORMALIZED')
                        self.assertFalse(metadata['semantic_correctness_verified'])
                    self.assertEqual(mocked.call_count, 1)
                self.assertEqual((root/'model-response.json').read_bytes(), raw)
                receipt = json.loads((root/'code-normalization.json').read_bytes())
                self.assertEqual(receipt['raw_response_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertFalse(receipt['code_executed'])
                if expected_error == 'TEMPLATE_DELIMITERS_REFUSED':
                    self.assertEqual(receipt['status'], 'REFUSED')
                    self.assertFalse((root/'normalized-code-blocks.json').exists())
                    self.assertFalse((root/'assembled-response.json').exists())
                else:
                    self.assertEqual(receipt['output_code_blocks_sha256'], hashlib.sha256((root/'normalized-code-blocks.json').read_bytes()).hexdigest())
                    self.assertTrue((root/'assembled-response.json').exists())


if __name__ == '__main__':
    unittest.main()
