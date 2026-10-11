import copy
import json
from pathlib import Path
import unittest
from builder.api_contract import BankContract
from builder.models import FoundationError
from builder.model_wire import NODE, ACTION, TRANSITION, CHECK, render_plan
from orchestration.contest_build import validate_workflow

def empty(schema):
    if schema['type']=='object': return {k:empty(v) for k,v in schema['properties'].items()}
    if schema['type']=='array': return []
    if schema['type']=='boolean': return False
    if schema['type'] in ('integer','number'): return 0
    return schema.get('enum',[''])[0]

class TypedWireTests(unittest.TestCase):
    def setUp(self):
        self.contract=BankContract.from_path(Path(__file__).parent/'inputs/openapi.json')
        self.plan={'initial_node':'verify_identity','system_prompt':'source policy','nodes':[]}
        for name in ('verify_identity','get_account','create_promise','close_call'):
            node=empty(NODE);node.update(name=name,node_type='api_request')
            action=empty(ACTION);action['kind']=name
            node['pre_actions']=[action]
            self.plan['nodes'].append(node)

    def test_tool_structure_generated_from_current_contract_and_schema_changes(self):
        contract=copy.deepcopy(self.contract)
        contract.operations['create_promise'].request['properties']['new_rule_code']={'type':'string','enum':['NEXT']}
        result=render_plan(self.plan,[],contract.freya_templates())
        self.assertTrue(validate_workflow(result,contract)['static_valid'])
        leaf=result['nodes']['create_promise']['pre_actions'][0]['function']['parameters']['properties']['body']['properties']['new_rule_code']
        self.assertEqual(leaf['enum'],['NEXT'])
        with self.assertRaisesRegex(FoundationError,'BODY_FIELDS'):
            validate_workflow(result,self.contract)

    def test_generated_code_roundtrips_without_nested_json_escaping(self):
        node=empty(NODE);node.update(name='compute',node_type='code')
        action=empty(ACTION);action.update(kind='code_exec',code_ref='controller_core')
        node['pre_actions']=[action]
        self.plan['nodes'].append(node)
        code='result = {"spoken": "Bank\'s rules"}\n'
        result=render_plan(self.plan,[{'id':'controller_core','code':code}],self.contract.freya_templates())
        self.assertEqual(json.loads(json.dumps(result))['nodes']['compute']['pre_actions'][0]['function']['parameters']['properties']['code']['default'],code)
        self.assertTrue(validate_workflow(result,self.contract)['static_valid'])

    def test_unknown_body_field_duplicate_node_and_unreferenced_code_refused(self):
        changed=copy.deepcopy(self.plan)
        changed['nodes'][0]['pre_actions'][0]['body_defaults']=[{'field':'not_in_contract','value_json':'"value"'}]
        with self.assertRaisesRegex(FoundationError,'BANK_BINDING'):
            render_plan(changed,[],self.contract.freya_templates())
        changed=copy.deepcopy(self.plan);changed['nodes'].append(changed['nodes'][0])
        with self.assertRaisesRegex(FoundationError,'PLAN_NODE'):
            render_plan(changed,[],self.contract.freya_templates())
        with self.assertRaisesRegex(FoundationError,'UNUSED_CODE'):
            render_plan(self.plan,[{'id':'unused','code':'result = {}'}],self.contract.freya_templates())

    def test_variable_and_conditions_have_documented_outer_or(self):
        transition=empty(TRANSITION);transition.update(target_node='get_account',description='verified',logic='and')
        check=empty(CHECK);check.update(operator='equals',variable='verified',operand_json='"yes"')
        transition['checks']=[check,copy.deepcopy(check)]
        self.plan['nodes'][0]['transitions']=[transition]
        result=render_plan(self.plan,[],self.contract.freya_templates())
        condition=result['nodes']['verify_identity']['transitions'][0]['condition']
        self.assertEqual(condition['logic'],'or')
        self.assertEqual(condition['conditions'][0]['logic'],'and')
        self.assertTrue(validate_workflow(result,self.contract)['static_valid'])

    def test_pre_action_mapping_requires_node_level_and_literal_defaults_match_schema(self):
        changed=copy.deepcopy(self.plan)
        changed['nodes'][0]['pre_actions'][0]['response_mapping']=[{'path':'data.ok','variable':'verified'}]
        with self.assertRaisesRegex(FoundationError,'REQUIRES_NODE_LEVEL'):
            render_plan(changed,[],self.contract.freya_templates())
        changed['nodes'][0]['pre_actions'][0]['response_mapping']=[]
        changed['nodes'][0]['response_mapping']=[{'path':'data.ok','variable':'verified'}]
        result=render_plan(changed,[],self.contract.freya_templates())
        self.assertIn('response_mapping',result['nodes']['verify_identity'])
        amount=copy.deepcopy(self.plan)
        amount['nodes'][2]['pre_actions'][0]['body_defaults']=[{'field':'amount','value_json':'"not-a-number"'}]
        with self.assertRaisesRegex(FoundationError,'LITERAL_DEFAULT'):
            render_plan(amount,[],self.contract.freya_templates())
        amount['nodes'][2]['pre_actions'][0]['body_defaults'][0]['value_json']=str(10**1000)
        with self.assertRaisesRegex(FoundationError,'LITERAL_DEFAULT'):
            render_plan(amount,[],self.contract.freya_templates())
        amount['nodes'][2]['pre_actions'][0]['body_defaults'][0]['value_json']='"{{amount}}"'
        self.assertTrue(validate_workflow(render_plan(amount,[],self.contract.freya_templates()),self.contract)['static_valid'])

    def test_nested_malformed_plan_fields_have_fixed_repair_diagnostic(self):
        changes = (
            lambda p: p['nodes'][0].pop('pre_actions'),
            lambda p: p['nodes'][0].update(pre_actions='not-an-array'),
            lambda p: p['nodes'][0].update(passthrough='false'),
            lambda p: p['nodes'][0].update(node_type='unsupported-node'),
            lambda p: p['nodes'][0]['pre_actions'][0].update(kind='unknown-action'),
            lambda p: p['nodes'][0]['pre_actions'][0].update(body_defaults=[{'field':'answer'}]),
            lambda p: p['nodes'][0].update(max_node_turns=True),
            lambda p: p['nodes'][0].update(max_node_turns=10**1000),
            lambda p: p.update(nodes=[None]),
        )
        for mutate in changes:
            changed=copy.deepcopy(self.plan); mutate(changed)
            with self.subTest(plan=changed), self.assertRaisesRegex(FoundationError,'^BUILDER_MODEL_PLAN_FIELDS_INVALID$'):
                render_plan(changed,[],self.contract.freya_templates())

    def test_malformed_code_and_template_collections_have_fixed_diagnostics(self):
        for blocks in (None, 'not-an-array', [None], [{'id':12,'code':'result = {}'}],
                [{'id':'one','code':None}], [{'id':'one','code':'result = {}','extra':'private-marker'}]):
            with self.subTest(blocks=blocks), self.assertRaisesRegex(FoundationError,'^BUILDER_MODEL_CODE_BLOCK_INVALID$'):
                render_plan(self.plan,blocks,self.contract.freya_templates())
        for templates in (None, {}, {'tools':[]}):
            with self.subTest(templates=templates), self.assertRaisesRegex(FoundationError,'^BUILDER_MODEL_BANK_TEMPLATES_INVALID$'):
                render_plan(self.plan,[],templates)

    def test_code_and_bank_hooks_refuse_template_delimiters_without_rewriting(self):
        controller=empty(NODE); controller.update(name='compute',node_type='code')
        action=empty(ACTION); action.update(kind='code_exec',code_ref='one')
        controller['pre_actions']=[action]
        for placement in ('code_exec','pre_process_ref','post_process_ref'):
            changed=copy.deepcopy(self.plan)
            if placement=='code_exec': changed['nodes'].append(controller)
            else: changed['nodes'][0]['pre_actions'][0][placement]='one'
            for code in ('result = {"outer": {"inner": 1}}', 'result = "{{unbound}}"'):
                blocks=[{'id':'one','code':code}]
                with self.subTest(placement=placement,code=code), self.assertRaisesRegex(FoundationError,'TEMPLATE_DELIMITERS_REFUSED'):
                    render_plan(changed,blocks,self.contract.freya_templates())
                self.assertEqual(blocks[0]['code'],code)
            safe='result = {"outer": {"inner": 1} }'
            built=render_plan(changed,[{'id':'one','code':safe}],self.contract.freya_templates())
            rendered_action=built['nodes']['compute' if placement=='code_exec' else 'verify_identity']['pre_actions'][0]
            actual=(rendered_action['function']['parameters']['properties']['code']['default'] if placement=='code_exec'
                else rendered_action[placement.replace('_ref','_code')])
            self.assertEqual(actual,safe)

if __name__=='__main__': unittest.main()
