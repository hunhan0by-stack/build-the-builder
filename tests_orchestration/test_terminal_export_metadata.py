import copy
import unittest
from builder.models import FoundationError
from orchestration.contest_build import verify_provider_graph

class TerminalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.expected={'initial_node':'finish','nodes':{'finish':{'name':'finish','node_type':'end_call',
            'terminal':{'type':'function','function':{'name':'finish','handler':'__function__:end_call',
                'parameters':{'type':'object','properties':{'message_mode':{'type':'string','default':'none'}}}}}}}}
    def test_empty_added_description_on_named_terminal_is_inert(self):
        actual=copy.deepcopy(self.expected)
        actual['nodes']['finish']['terminal']['function']['description']=''
        self.assertTrue(verify_provider_graph(self.expected,actual))
    def test_changed_behavior_or_nonempty_description_is_refused(self):
        for field,value in [('description','New instructions'),('name','different'),('handler','__function__:different')]:
            actual=copy.deepcopy(self.expected)
            actual['nodes']['finish']['terminal']['function'][field]=value
            with self.subTest(field=field),self.assertRaises(FoundationError):
                verify_provider_graph(self.expected,actual)
    def test_existing_description_must_remain_exact(self):
        self.expected['nodes']['finish']['terminal']['function']['description']='original'
        actual=copy.deepcopy(self.expected)
        actual['nodes']['finish']['terminal']['function']['description']=''
        with self.assertRaises(FoundationError):verify_provider_graph(self.expected,actual)
