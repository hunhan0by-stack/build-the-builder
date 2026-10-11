import copy
import json
import sys
import unittest

from builder.model_diagnostics import diagnose_model_response
from builder.models import FoundationError


BRIEF = 'Identity before account. Confirm the current payment proposal.'


def response():
    return {'workflow_json': json.dumps({'initial_node': 'collect', 'nodes': {'collect': {}}}),
            'unresolved': [], 'limitations': [],
            'coverage': [{'quote': 'Identity before account.', 'nodes': ['collect'],
                          'implementation': 'Identity guard in controller.'}]}


def codes(value):
    return {row['code'] for row in value['issues']}


class ModelDiagnosticTests(unittest.TestCase):
    def test_unresolved_and_bad_workflow_syntax_are_reported_together(self):
        value = response()
        value['unresolved'] = ['A real implementation gap.']
        value['workflow_json'] = '{"nodes": {"collect": '
        diagnosed = diagnose_model_response(json.dumps(value), BRIEF)
        self.assertEqual(diagnosed['unresolved_count'], 1)
        self.assertEqual(codes(diagnosed), {'UNRESOLVED_POLICY', 'JSON_SYNTAX_INVALID'})
        syntax = next(row for row in diagnosed['issues'] if row['code'] == 'JSON_SYNTAX_INVALID')
        self.assertEqual(syntax['scope'], 'workflow')
        self.assertEqual(syntax['line'], 1)
        self.assertGreater(syntax['column'], 1)

    def test_valid_syntax_reports_bad_source_node_contract_and_policy_together(self):
        value = response()
        value['unresolved'] = ['Keep this gap.']
        value['coverage'][0].update(quote='Invented clause.', nodes=['absent'])
        observed = []
        def validator(workflow):
            observed.append(copy.deepcopy(workflow))
            raise FoundationError('CONTEST_TOOL_ENDPOINT_MISMATCH')
        diagnosed = diagnose_model_response(json.dumps(value), BRIEF, validator)
        self.assertEqual(len(observed), 1)
        self.assertTrue(diagnosed['workflow_json_valid'])
        self.assertEqual(codes(diagnosed), {'UNRESOLVED_POLICY', 'COVERAGE_SOURCE_MISMATCH',
                                          'COVERAGE_NODE_MISMATCH', 'VALIDATOR_REJECTED'})
        self.assertEqual(diagnosed['issues'][-1]['validator_code'], 'CONTEST_TOOL_ENDPOINT_MISMATCH')

    def test_duplicate_keys_in_outer_and_inner_json_fail_without_key_disclosure(self):
        secret = 'private-secret-key-name'
        outer = '{"' + secret + '":1,"' + secret + '":2}'
        self.assertEqual(codes(diagnose_model_response(outer, BRIEF)), {'JSON_DUPLICATE_KEY'})
        value = response()
        value['workflow_json'] = '{"nodes":{},"nodes":{}}'
        diagnosed = diagnose_model_response(json.dumps(value), BRIEF)
        self.assertEqual(codes(diagnosed), {'JSON_DUPLICATE_KEY'})
        self.assertNotIn(secret, json.dumps(diagnose_model_response(outer, BRIEF)))

    def test_nonfinite_literals_and_exponents_fail_in_both_json_layers(self):
        for literal in ('NaN', 'Infinity', '-Infinity', '1e999'):
            with self.subTest(literal=literal):
                self.assertEqual(codes(diagnose_model_response('{"x":' + literal + '}', BRIEF)),
                                 {'JSON_NONFINITE_NUMBER'})
                value = response()
                value['workflow_json'] = '{"nodes":{},"x":' + literal + '}'
                self.assertEqual(codes(diagnose_model_response(json.dumps(value), BRIEF)),
                                 {'JSON_NONFINITE_NUMBER'})

    def test_raw_policy_quote_node_and_validator_secrets_are_never_emitted(self):
        secret = 'Bearer credentialSecretValueThatMustNotAppear'
        value = response()
        value['unresolved'] = [secret]
        value['limitations'] = [secret]
        value['coverage'][0].update(quote=secret, nodes=[secret], implementation=secret)
        def validator(unused):
            raise FoundationError('CONTEST_SECRET_TOKEN_' + secret)
        diagnosed = diagnose_model_response(json.dumps(value), BRIEF, validator)
        encoded = json.dumps(diagnosed)
        self.assertNotIn(secret, encoded)
        self.assertNotIn('CONTEST_SECRET_TOKEN_', encoded)
        self.assertEqual(diagnosed['issues'][-1]['validator_code'], 'UNCLASSIFIED_VALIDATOR_REJECTION')

    def test_no_issues_is_not_semantic_live_or_repair_acceptance(self):
        original = response()
        def validator(workflow):
            workflow['nodes'].clear()
            return {'static_valid': True, 'password': 'unreported secret'}
        diagnosed = diagnose_model_response(json.dumps(original), BRIEF, validator)
        self.assertEqual(diagnosed['issues'], [])
        self.assertEqual(diagnosed['validator_status'], 'RETURNED_WITHOUT_ERROR')
        self.assertIn('collect', json.loads(original['workflow_json'])['nodes'])
        for field in ('semantic_correctness_verified', 'live_success_verified', 'repair_performed',
                      'acceptance_authority'):
            self.assertFalse(diagnosed[field])
        self.assertNotIn('unreported secret', json.dumps(diagnosed))

    def test_bad_types_and_unknown_fields_emit_only_safe_shape_information(self):
        value = {'workflow_json': 4, 'unresolved': {}, 'coverage': 'secret',
                 'secret_field_name': 'secret_value'}
        diagnosed = diagnose_model_response(json.dumps(value), BRIEF)
        self.assertEqual(codes(diagnosed), {'RESPONSE_FIELDS_MISSING', 'RESPONSE_EXTRA_FIELDS',
                                          'UNRESOLVED_TYPE_INVALID', 'WORKFLOW_JSON_STRING_REQUIRED',
                                          'COVERAGE_TYPE_INVALID'})
        self.assertNotIn('secret_field_name', json.dumps(diagnosed))
        self.assertNotIn('secret_value', json.dumps(diagnosed))

    def test_validator_generic_crash_is_sanitized_and_does_not_escape(self):
        def validator(unused):
            raise RuntimeError('secret host path and credentials')
        diagnosed = diagnose_model_response(json.dumps(response()), BRIEF, validator)
        self.assertEqual(codes(diagnosed), {'VALIDATOR_CALLBACK_FAILED'})
        self.assertNotIn('secret host', json.dumps(diagnosed))

    def test_bad_outer_json_cannot_reach_validator(self):
        def validator(unused):
            self.fail('malformed response cannot reach static validator')
        diagnosed = diagnose_model_response(b'{"workflow_json":', BRIEF, validator)
        self.assertEqual(codes(diagnosed), {'JSON_SYNTAX_INVALID'})
        self.assertEqual(diagnosed['validator_status'], 'NOT_RUN')

    def test_oversized_integer_is_bounded_in_both_layers_without_raw_message(self):
        if not hasattr(sys, 'set_int_max_str_digits'):
            self.skipTest('Python runtime does not implement integer digit bounds')
        old_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(4300)
            digits = '9' * 5000
            outer = '{"secret_numeric_value":' + digits + '}'
            value = response()
            value['workflow_json'] = '{"nodes":{},"secret_numeric_value":' + digits + '}'
            for text, scope in ((outer, 'response'), (json.dumps(value), 'workflow')):
                with self.subTest(scope=scope):
                    diagnosed = diagnose_model_response(text, BRIEF)
                    self.assertEqual(diagnosed['issues'], [{'scope': scope, 'code': 'JSON_NUMERIC_LIMIT'}])
                    encoded = json.dumps(diagnosed)
                    self.assertNotIn(digits, encoded)
                    self.assertNotIn('secret_numeric_value', encoded)
                    self.assertNotIn('Exceeds the limit', encoded)
                    self.assertFalse(diagnosed['acceptance_authority'])
                    self.assertFalse(diagnosed['workflow_json_valid'])
        finally:
            sys.set_int_max_str_digits(old_limit)


if __name__ == '__main__':
    unittest.main()
