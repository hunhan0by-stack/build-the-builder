import copy
import unittest
from builder.api_contract import resolve_schema, validate_value
from builder.case_contract import parse_schema_text
from builder.model_wire import NODE, render_plan, plan_schema
from builder.models import FoundationError
from tests_orchestration.test_case_build import empty

class SchemaShapes(unittest.TestCase):
    def test_negation_forbids_present_field_with_strict_outer_validation(self):
        schema=resolve_schema({'type':'object','properties':{'error':{'type':'string'},'payload':{}},
            'not':{'required':['payload']}},{})
        validate_value({'error':'unknown'},schema,strict_fields=True)
        with self.assertRaises(FoundationError):validate_value({'payload':None},schema,strict_fields=True)
    def test_negation_composes_and_resolves_local_reference(self):
        schema={'type':'integer','$defs':{'excluded':{'enum':[4,9]}},'not':{'$ref':'#/$defs/excluded'}}
        resolved=resolve_schema(schema,schema)
        validate_value(7,resolved)
        for value in (4,9,'seven'):
            with self.subTest(value=value),self.assertRaises(FoundationError):validate_value(value,resolved)
    def test_nested_unsupported_schema_is_not_silently_accepted(self):
        with self.assertRaises(FoundationError):resolve_schema({'not':{'unsupportedConstraint':True}},{})
        with self.assertRaises(FoundationError):validate_value(None,resolve_schema({'not':{}},{}))
    def test_invalid_inner_json_reports_operation_and_field(self):
        with self.assertRaisesRegex(FoundationError,'CASE_SCHEMA_JSON_INVALID lookup.response_schemas_json'):
            parse_schema_text('lookup','response_schemas_json','{"200":{')
    def test_generic_terminal_preserves_outcome_legacy_stays_unchanged(self):
        end=empty(NODE);end.update(name='finished_reading',node_type='end_call')
        plan={'initial_node':'finished_reading','system_prompt':'Synthetic test','nodes':[end]}
        for schema,expected in ((None,'end_call'),(plan_schema([]),'finished_reading')):
            graph=render_plan(copy.deepcopy(plan),[], {'tools':{}},schema=schema)
            self.assertEqual(graph['nodes']['finished_reading']['terminal']['function']['name'],expected)
            self.assertEqual(graph['nodes']['finished_reading']['terminal']['function']['handler'],'__function__:end_call')

if __name__=='__main__':unittest.main()
