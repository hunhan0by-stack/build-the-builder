import copy
import unittest
from builder.models import FoundationError
from builder.model_wire import ACTION, NODE, render_plan
from orchestration.contest_build import verify_provider_graph
from tests_orchestration.test_model_wire import empty


def terminal():
    return {'name':'end_call','handler':'__function__:end_call','parameters':{
        'type':'object','properties':{'message_mode':{'type':'string','default':'none'}}}}


class NativeDefaultsTests(unittest.TestCase):
    def graph(self):
        return {'initial_node':'work','system_prompt':'Keep source policy.', 'global_nodes':{},
            'nodes':{'work':{'node_type':'code','suppress_tool_results':True,
                'pre_actions':[{'function':{'handler':'__function__:code_exec',
                    'parameters':{'type':'object','properties':{'code':{'default':'result = 1'}}}}}]},
                'end':{'node_type':'end_call','suppress_tool_results':False,
                       'terminal':{'type':'function','function':terminal()}}}}

    def test_empty_conversation_and_end_emit_false_without_mutating_plan(self):
        for kind in ('conversation','end_call'):
            for requested in (False,True):
                row=empty(NODE); row.update(name='one',node_type=kind,suppress_tool_results=requested)
                plan={'initial_node':'one','system_prompt':'','nodes':[row]}
                original=copy.deepcopy(plan)
                graph=render_plan(plan,[],{'tools':{}})
                self.assertIs(graph['nodes']['one']['suppress_tool_results'],False)
                self.assertEqual(plan,original)

    def test_real_action_and_code_nodes_preserve_requested_suppression(self):
        for kind,placement in (('conversation','tools'),('conversation','pre_actions'),('code','pre_actions')):
            for requested in (False,True):
                row=empty(NODE);row.update(name='one',node_type=kind,suppress_tool_results=requested)
                action=empty(ACTION);action.update(kind='code_exec',code_ref='one')
                row[placement]=[action]
                result=render_plan({'initial_node':'one','system_prompt':'','nodes':[row]},
                    [{'id':'one','code':'result = 1'}],{'tools':{}})
                self.assertIs(result['nodes']['one']['suppress_tool_results'],requested)

    def test_only_added_empty_pre_action_and_silent_terminal_descriptions_accepted(self):
        expected=self.graph();actual=copy.deepcopy(expected)
        actual['nodes']['work']['pre_actions'][0]['function']['description']=''
        actual['nodes']['end']['terminal']['function']['description']=''
        original=copy.deepcopy(actual)
        self.assertTrue(verify_provider_graph(expected,actual))
        self.assertEqual(actual,original)
        self.assertNotIn('description',expected['nodes']['end']['terminal']['function'])

    def test_nonempty_wrong_type_or_changed_existing_description_rejected(self):
        for value in ('new text',None,False,[],{}):
            expected=self.graph();actual=copy.deepcopy(expected)
            actual['nodes']['work']['pre_actions'][0]['function']['description']=value
            with self.subTest(value=value),self.assertRaises(FoundationError):
                verify_provider_graph(expected,actual)
        expected=self.graph();expected['nodes']['work']['pre_actions'][0]['function']['description']='source'
        actual=copy.deepcopy(expected);actual['nodes']['work']['pre_actions'][0]['function']['description']=''
        with self.assertRaises(FoundationError):verify_provider_graph(expected,actual)

    def test_nested_parameter_tool_post_action_and_wrong_terminal_paths_rejected(self):
        for placement in ('tools','post_actions'):
            expected=self.graph();expected['nodes']['work'][placement]=copy.deepcopy(expected['nodes']['work']['pre_actions'])
            actual=copy.deepcopy(expected);actual['nodes']['work'][placement][0]['function']['description']=''
            with self.subTest(placement=placement),self.assertRaises(FoundationError):
                verify_provider_graph(expected,actual)
        expected=self.graph();actual=copy.deepcopy(expected)
        actual['nodes']['work']['pre_actions'][0]['function']['parameters']['description']=''
        with self.assertRaises(FoundationError):verify_provider_graph(expected,actual)
        for mode,kind,handler in (('custom','end_call','__function__:end_call'),
            ('none','conversation','__function__:end_call'),('none','end_call','unexpected')):
            expected=self.graph();node=expected['nodes']['end'];node['node_type']=kind
            node['terminal']['function']['handler']=handler
            node['terminal']['function']['parameters']['properties']['message_mode']['default']=mode
            actual=copy.deepcopy(expected);actual['nodes']['end']['terminal']['function']['description']=''
            with self.subTest(mode=mode,kind=kind,handler=handler),self.assertRaises(FoundationError):
                verify_provider_graph(expected,actual)

    def test_real_source_values_still_fail_with_harmless_metadata(self):
        for change in ('suppression','code','handler','terminal_mode','new_action','missing_field'):
            expected=self.graph();actual=copy.deepcopy(expected)
            work=actual['nodes']['work'];function=work['pre_actions'][0]['function']
            function['description']=''
            if change=='suppression':work['suppress_tool_results']=False
            if change=='code':function['parameters']['properties']['code']['default']='result = 2'
            if change=='handler':function['handler']='__function__:other'
            if change=='terminal_mode':actual['nodes']['end']['terminal']['function']['parameters']['properties']['message_mode']['default']='custom'
            if change=='new_action':work['pre_actions'].append(copy.deepcopy(work['pre_actions'][0]))
            if change=='missing_field':function.pop('handler')
            with self.subTest(change=change),self.assertRaises(FoundationError):
                verify_provider_graph(expected,actual)

    def test_canonical_emitter_then_readback_does_not_need_value_exceptions(self):
        row=empty(NODE);row.update(name='talk',node_type='conversation',suppress_tool_results=True)
        end=empty(NODE);end.update(name='end',node_type='end_call',suppress_tool_results=True)
        expected=render_plan({'initial_node':'talk','system_prompt':'','nodes':[row,end]},[],{'tools':{}})
        actual=copy.deepcopy(expected)
        actual['nodes']['end']['terminal']['function']['description']=''
        self.assertTrue(verify_provider_graph(expected,actual))
        actual['nodes']['talk']['suppress_tool_results']=True
        with self.assertRaises(FoundationError):verify_provider_graph(expected,actual)


if __name__=='__main__':unittest.main()
