"""Model-only caller interface admission; synthetic CLI and no network."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from builder.models import FoundationError
from builder.model_workflow import (CodexWorkflowProvider, prompt_for,
    validate_extraction_interface)
from builder.model_wire import EXTRACT, NODE
from tests_orchestration.test_model_wire import empty


def listener(field='speech_event', kind='string'):
    node=empty(NODE)
    node.update(name='listen',node_type='conversation')
    extraction=empty(EXTRACT)
    extraction.update(name=field,type=kind,min_confidence=0.7)
    node['extraction_schema']=[extraction]
    return node


class ExtractionInterfaceTests(unittest.TestCase):
    def test_generation_prompt_pins_protocol_without_current_bank_values(self):
        prompt=prompt_for('Synthetic source only.',{}, {})
        instruction=prompt.split('INPUT_DATA\n',1)[0]
        self.assertIn('Mandatory caller extraction/controller interface:',instruction)
        self.assertIn('speech_event with type=string',instruction)
        self.assertIn('allowed extraction_schema names are speech_event and optional speech_date',instruction)
        self.assertIn('identity_answer is a string',instruction)
        self.assertIn('wrong_recipient, decline_identity, human, inconvenient,',instruction)
        self.assertIn('all_considered',instruction)
        self.assertIn('payment_claim',instruction)
        self.assertIn('not an exhaustive policy vocabulary',instruction)
        self.assertIn('source_values (a closed typed',instruction)
        self.assertIn('every source-required caller branch',instruction)
        for value in ('241.65','2026-11-13','pledged_amount','4826','1984-07-23'):
            self.assertNotIn(value,instruction)
        data=json.loads(prompt.split('INPUT_DATA\n',1)[1])
        self.assertEqual(data['policy'],'Synthetic source only.')
        self.assertNotIn('cases',data)

    def test_model_provider_rejects_wrong_field_and_accepts_stable_field(self):
        events='\n'.join(json.dumps({'type':t}) for t in ('thread.started','turn.started','turn.completed'))
        for field,kind,accepted in (('turn_json','string',False),('speech_event','boolean',False),('speech_event','string',True)):
            with self.subTest(field=field,kind=kind), tempfile.TemporaryDirectory() as temporary:
                response={'workflow_plan':{'initial_node':'listen','system_prompt':'','nodes':[listener(field,kind)]},
                    'code_blocks':[],'unresolved':[],
                    'coverage':[{'quote':'Synthetic source only.','nodes':['listen'],'implementation':'Unit only.'}],
                    'limitations':[]}
                def process(command,**kwargs):
                    (Path(temporary)/'model-response.json').write_text(json.dumps(response),encoding='utf-8')
                    return SimpleNamespace(returncode=0,stdout=events,stderr='')
                with patch('builder.model_workflow.subprocess.run',side_effect=process):
                    provider=CodexWorkflowProvider(executable='synthetic-unused')
                    arguments=dict(brief='Synthetic source only.',api={},references={},directory=temporary,
                                   remaining_seconds=10,tool_templates={'tools':{}})
                    if accepted:
                        workflow,metadata=provider.generate(**arguments)
                        self.assertEqual(workflow['nodes']['listen']['extraction_schema'][0]['name'],'speech_event')
                        self.assertFalse(metadata['semantic_correctness_verified'])
                    else:
                        with self.assertRaisesRegex(FoundationError,'^BUILDER_MODEL_EXTRACTION_INTERFACE_MISMATCH$'):
                            provider.generate(**arguments)
                        self.assertTrue((Path(temporary)/'assembled-response.json').is_file())

    def test_duplicate_split_identity_and_wrong_optional_date_are_rejected(self):
        row={'name':'speech_event','type':'string'}
        for fields in ([],[row,copy.deepcopy(row)],
                [{'name':[],'type':'string'}],
                [row,{'name':'family_code','type':'string'}],
                [row,{'name':'birth_date','type':'date'}],
                [row,{'name':'reply','type':'string'}],
                [row,{'name':'phone_last4','type':'string'}],
                [row,{'name':'loan_ref','type':'string'}],
                [row,{'name':'speech_date','type':'string'}]):
            with self.subTest(fields=fields), self.assertRaisesRegex(FoundationError,'EXTRACTION_INTERFACE_MISMATCH'):
                validate_extraction_interface({'nodes':{'listen':{'node_type':'conversation','extraction_schema':fields}}})
        validate_extraction_interface({'nodes':{'listen':{'node_type':'conversation','extraction_schema':
            [row,{'name':'speech_date','type':'date'}]}}})

    def test_passthrough_farewell_and_non_conversation_nodes_are_not_listeners(self):
        non_listeners={'farewell':{'node_type':'conversation','passthrough':True},
            'compute':{'node_type':'code'},'end':{'node_type':'end_call'}}
        with self.assertRaisesRegex(FoundationError,'EXTRACTION_INTERFACE_MISMATCH'):
            validate_extraction_interface({'nodes':non_listeners})
        non_listeners['listen']={'node_type':'conversation','extraction_schema':[{'name':'speech_event','type':'string'}]}
        validate_extraction_interface({'nodes':non_listeners})

    def test_custom_prompt_requires_every_native_input_placeholder(self):
        tokens = ('{{extractor.speech}}', '{{extractor.conversation.last-10}}',
                  '{{extractor.type_preamble}}', '{{extractor.variables_list}}')
        row = {'name': 'speech_event', 'type': 'string'}
        for prompt in ('Semantic instructions only', None, *(' '.join(t for t in tokens if t != omitted) for omitted in tokens)):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(FoundationError, 'EXTRACTION_INTERFACE_MISMATCH'):
                validate_extraction_interface({'nodes': {'listen': {'node_type': 'conversation',
                    'extraction_schema': [dict(row, extraction_prompt=prompt)]}}})
        for prompt in ('', ' '.join(tokens)):
            validate_extraction_interface({'nodes': {'listen': {'node_type': 'conversation',
                'extraction_schema': [dict(row, extraction_prompt=prompt)]}}})


if __name__=='__main__':
    unittest.main()
