import copy
import json
import unittest
from builder.models import FoundationError
from builder import model_wire as wire
from tests_orchestration.test_model_wire import empty

class NestedBindingTests(unittest.TestCase):
    def setUp(self):
        self.body = {'type': 'object', 'properties': {
            'envelope': {'type': 'object', 'properties': {
                'identifier': {'type': 'string'}, 'amount': {'type': 'number', 'minimum': 0}}},
            'literal.dotted': {'type': 'string'}, 'top': {'type': 'string'}}}
        self.templates = {'tools': {'inspect': {'type': 'function', 'function': {
            'name': 'inspect', 'parameters': {'type': 'object', 'properties': {'body': self.body}}}}}}
        node = empty(wire.NODE)
        node.update(name='start', node_type='api_request')
        action = empty(wire.ACTION)
        action['kind'] = 'inspect'
        node['pre_actions'] = [action]
        self.plan = {'initial_node': 'start', 'system_prompt': 'neutral', 'nodes': [node]}

    def render(self, bindings):
        self.plan['nodes'][0]['pre_actions'][0]['body_defaults'] = bindings
        workflow = wire.render_plan(self.plan, [], self.templates, schema=wire.plan_schema(['inspect']))
        return workflow['nodes']['start']['pre_actions'][0]['function']['parameters']['properties']['body']

    def test_nested_literal_and_dynamic_bind_existing_leaves(self):
        before = copy.deepcopy(self.templates)
        body = self.render([{'field':'envelope.identifier','value_json':'"{{runtime_identifier}}"'},
                            {'field':'envelope.amount','value_json':'12.5'}])
        self.assertEqual(body['properties']['envelope']['properties']['identifier']['default'], '{{runtime_identifier}}')
        self.assertEqual(body['properties']['envelope']['properties']['amount']['default'], 12.5)
        self.assertEqual(self.templates, before)
        self.assertNotIn('envelope.identifier', body['properties'])

    def test_exact_dotted_key_and_flat_field_are_preserved(self):
        body = self.render([{'field':'literal.dotted','value_json':'"value"'}, {'field':'top','value_json':'"ok"'}])
        self.assertEqual(body['properties']['literal.dotted']['default'], 'value')
        self.assertEqual(body['properties']['top']['default'], 'ok')

    def test_unknown_path_and_nonobject_parent_refused(self):
        for name in ['envelope.missing','top.child','envelope..amount','missing.x','envelope.amount.x']:
            with self.subTest(name=name), self.assertRaisesRegex(FoundationError, 'BANK_BINDING_INVALID'):
                self.render([{'field':name,'value_json':'"x"'}])

    def test_literal_leaf_type_and_bounds_remain_enforced(self):
        for value in ['"wrong-type"', '-1']:
            with self.subTest(value=value), self.assertRaisesRegex(FoundationError, 'LITERAL_DEFAULT_MISMATCH'):
                self.render([{'field':'envelope.amount','value_json':value}])

    def test_duplicates_and_parent_child_overlap_refused_in_both_orders(self):
        leaf = {'field':'envelope.identifier','value_json':'"x"'}
        parent = {'field':'envelope','value_json':'{}'}
        for bindings in [[leaf,leaf],[parent,leaf],[leaf,parent]]:
            with self.subTest(bindings=bindings), self.assertRaisesRegex(FoundationError, 'BANK_BINDING_INVALID'):
                self.render(bindings)

    def test_exact_key_precedence_does_not_modify_sibling_nested_field(self):
        self.body['properties']['literal'] = {'type':'object','properties':{'dotted':{'type':'integer'}}}
        body = self.render([{'field':'literal.dotted','value_json':'"ok"'}])
        self.assertEqual(body['properties']['literal.dotted']['default'], 'ok')
        self.assertNotIn('default', body['properties']['literal']['properties']['dotted'])

