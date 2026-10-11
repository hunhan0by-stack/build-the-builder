import hashlib
import json
import unittest
from pathlib import Path
from builder.case_model import verify_coverage_citation
from builder.case_contract import verify_citation
from builder.models import FoundationError
from orchestration.case_build import run
from tests_orchestration.test_case_build import CaseBuildTests, FakeProvider

class FormatReferenceTests(unittest.TestCase):
    def setUp(self):
        self.projection={'documents':[]}
        self.references={'docs/format.md':'# Intro\nA\n### The `result` object\nExact technical fact.\n### Another section\nDifferent fact.'}
        self.citation={'document':'docs/format.md','locator_json':json.dumps({'section':'The result object'}),'quote':'Exact technical fact.'}
    def test_exact_section_and_quote_admitted_only_for_coverage(self):
        self.assertEqual(verify_coverage_citation(self.citation,self.projection,self.references),'platform_format_reference_not_business_authority')
        with self.assertRaises(FoundationError):verify_citation(self.citation,self.projection)
    def test_wrong_section_quote_or_unknown_reference_refused(self):
        for change in ({'document':'outside.md'},{'quote':'Invented fact'},{'quote':'Different fact.'},{'locator_json':'{"section":"Another section"}'}):
            with self.subTest(change=change):
                with self.assertRaises(FoundationError):verify_coverage_citation(dict(self.citation,**change),self.projection,self.references)
        with self.assertRaises(FoundationError):verify_coverage_citation(self.citation,self.projection,{})

class ReplayTests(CaseBuildTests):
    def test_zero_new_model_calls_and_byte_identical_origin(self):
        class RecordedFixture(FakeProvider):
            def structured_response(self,**kwargs):
                response,raw,unused=super().structured_response(**kwargs)
                directory=kwargs['directory']
                events=[{'type':'thread.started'},{'type':'turn.started'},
                    {'type':'item.completed','item':{'type':'agent_message','text':raw.decode()}},
                    {'type':'turn.completed'}]
                (directory/'model-session.jsonl').write_text('\n'.join(json.dumps(x) for x in events)+'\n',encoding='utf-8')
                (directory/'model-input.txt').write_text(kwargs['prompt'],encoding='utf-8')
                (directory/'model-response.schema.json').write_text(json.dumps(kwargs['schema_value']),encoding='utf-8')
                (directory/'model-process-status.json').write_text(json.dumps({'return_code':0,'output_sha256_at_exit':hashlib.sha256(raw).hexdigest()}),encoding='utf-8')
                return response,raw,None
        original=self.build(RecordedFixture(self.projection))
        replay=run(self.case,'https://mock.example.test',output_directory=self.root/'replay',assemble_from=self.root/'out')
        self.assertEqual(replay['status'],'LOCAL_CASE_WORKFLOW_EXPORTED',replay)
        self.assertEqual(replay['model_calls'],0)
        self.assertEqual(replay['attempts'][0]['purpose'],'recorded_response_reassembly')
        self.assertEqual(replay['assembly_origin']['run_id'],original['run_id'])
        for name in ('model-response.json','model-session.jsonl','model-input.txt'):
            self.assertEqual((self.root/'out/model-attempt-1'/name).read_bytes(),(self.root/'replay/model-attempt-1'/name).read_bytes())
        original_path=self.root/'out/model-attempt-1/model-response.json'
        original_path.write_bytes(original_path.read_bytes()+b' ')
        refused=run(self.case,'https://mock.example.test',output_directory=self.root/'tampered',assemble_from=self.root/'out')
        self.assertEqual(refused['diagnostic'],'CASE_REPLAY_RESPONSE_CHANGED')
        self.assertEqual(refused['model_calls'],0)

if __name__=='__main__':unittest.main()
