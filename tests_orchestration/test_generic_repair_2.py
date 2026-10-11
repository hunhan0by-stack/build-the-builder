import copy
import json
import unittest
from builder.models import FoundationError
from builder import hook_validation as module

class PosthookTests(unittest.TestCase):
    def test_direct_and_conditional_response_alias_are_refused(self):
        for value in ('result','result if isinstance(result, dict) else dict()'):
            with self.subTest(value=value),self.assertRaisesRegex(FoundationError,'RESPONSE_ALIAS_LOSS'):
                module.validate_posthook_retention("vars['saved']="+value+"\nresult.clear()")
    def test_local_alias_is_refused(self):
        with self.assertRaises(FoundationError):module.validate_posthook_retention("r=result\nvars['saved']=r\nr.clear()")
    def test_copied_snapshots_survive_actual_clear(self):
        code="vars['saved']=dict(result) if isinstance(result,dict) else dict()\nresult.clear()"
        module.validate_posthook_retention(code)
        response={'status_code':200,'data':{'value':7}}
        state={};exec(code,{'result':response,'vars':state})
        self.assertEqual(state['saved'],{'status_code':200,'data':{'value':7}})
    def test_mutually_exclusive_paths_do_not_raise(self):
        module.validate_posthook_retention("if flag:\n vars['saved']=result\nelse:\n result.clear()")
    def test_reassigned_or_deleted_saved_alias_does_not_raise(self):
        for update in ("vars['saved']=dict(result)","del vars['saved']"):
            module.validate_posthook_retention("vars['saved']=result\n"+update+"\nresult.clear()")
