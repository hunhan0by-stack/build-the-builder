import copy
import json
import unittest
from builder.models import FoundationError
from builder import api_contract as module

class ConditionalSchemaTests(unittest.TestCase):
    def response_schema(self):
        return module.resolve_schema({'type':'object',
            'properties':{'status':{'type':'string'},'payload':{},'error':{'type':'string'}},
            'required':['status'],'if':{'properties':{'status':{'const':'ok'}},'required':['status']},
            'then':{'required':['payload'],'not':{'required':['error']}},
            'else':{'required':['error'],'not':{'required':['payload']}}},{})
    def test_then_requires_its_fields_and_does_not_validate_else(self):
        schema=self.response_schema()
        module.validate_value({'status':'ok','payload':{}},schema,strict_fields=True)
        for value in ({'status':'ok'},{'status':'ok','payload':{},'error':'bad'}):
            with self.subTest(value=value),self.assertRaises(FoundationError):module.validate_value(value,schema,strict_fields=True)
    def test_else_requires_its_fields_and_forbids_then_payload(self):
        schema=self.response_schema()
        module.validate_value({'status':'failed','error':'bad'},schema,strict_fields=True)
        for value in ({'status':'failed'},{'status':'failed','payload':{},'error':'bad'}):
            with self.subTest(value=value),self.assertRaises(FoundationError):module.validate_value(value,schema,strict_fields=True)
    def test_parent_extra_field_gate_still_applies(self):
        with self.assertRaises(FoundationError):
            module.validate_value({'status':'ok','payload':{},'unexpected':1},self.response_schema(),strict_fields=True)
    def test_properties_condition_without_required_matches_missing_field(self):
        schema={'if':{'properties':{'flag':{'const':True}}},'then':{'required':['payload']}}
        with self.assertRaises(FoundationError):module.validate_value({},schema)
        module.validate_value({'flag':False},schema)
    def test_then_else_without_if_are_ignored(self):
        schema=module.resolve_schema({'type':'integer','then':{'type':'string'},'else':{'minimum':50}},{})
        module.validate_value(1,schema)
    def test_recursive_local_refs_in_predicate_and_branch_resolve(self):
        schema={'$defs':{'is_number':{'type':'number'},'positive':{'minimum':1}},
            'if':{'$ref':'#/$defs/is_number'},'then':{'$ref':'#/$defs/positive'},'else':{'type':'string'}}
        resolved=module.resolve_schema(schema,schema)
        module.validate_value(3,resolved);module.validate_value('three',resolved)
        for value in (0,False,None):
            with self.subTest(value=value),self.assertRaises(FoundationError):module.validate_value(value,resolved)
        with self.assertRaises(FoundationError):module.resolve_schema({'if':{'$ref':'https://remote.invalid/predicate'}},{})
    def test_branch_explicit_extra_field_restriction_is_honored(self):
        schema={'if':{'required':['a']},'then':{'properties':{'a':{}},'additionalProperties':False}}
        module.validate_value({'a':1},schema)
        with self.assertRaises(FoundationError):module.validate_value({'a':1,'b':2},schema)
