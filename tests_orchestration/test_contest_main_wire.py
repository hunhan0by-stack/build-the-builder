"""Normal main path, synthetic HTTP only. Protector is NOT real encryption."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from integration.contest_evidence import ContestEvidence,verify_bundle
from orchestration.contest_build import FreyaClient,KIT,OUTPUT,run

def uid(n): return '00000000-0000-4000-8000-'+str(n).zfill(12)
def protect(body): return b'UNIT_SYNTHETIC:'+base64.b64encode(body)
def recover(body): return base64.b64decode(body[len(b'UNIT_SYNTHETIC:'):],validate=True)
class Reply:
    status=200
    headers={'Content-Type':'application/json'}
    def __init__(self,body): self.body=json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,limit): return self.body[:limit]

class MainWireTests(unittest.TestCase):
    def test_normal_main_three_repeats_seal_actual_context_and_complete_wire(self):
        case=next(c for c in json.loads((KIT/'public-test-set.json').read_text()) if c['id']=='PUB-01')
        state={'graph':None,'session':0,'requests':[],'models':None}
        sinks=[]
        def network(request,**kwargs):
            url=urlsplit(request.full_url);path=url.path;params=parse_qs(url.query)
            body=json.loads(request.data) if request.data else None
            state['requests'].append((request.method,path))
            rep=state['session'];sim=uid(100+rep);iteration=uid(200+rep);call=uid(300+rep)
            bank=False
            if path=='/api/v2/workspaces': result={'workspaces':[{'id':uid(1)}]}
            elif path=='/api/v2/workspaces/'+uid(1)+'/credits': result={'creditsLeft':1000-rep,'creditUsdRate':0.002}
            elif path=='/api/v2/workflow' and request.method=='POST':
                if not params.get('dryRun'):
                    state['graph']=copy.deepcopy(body['workflow'])
                    state['graph']['agent_config']={'llmConfig':{'provider':'experimental-5','model':'experimental-5'}}
                result={'workflow':{'id':uid(2),'agentId':uid(3)}}
            elif path=='/api/v2/workflow/'+uid(2):
                if request.method=='PUT':
                    state['graph']=copy.deepcopy(body['workflow'])
                    state['graph']['agent_config']={'llmConfig':{'provider':'experimental-5','model':'experimental-5'}}
                result={'workflow':{'id':uid(2),'agentId':uid(3),'workflow':state['graph']}}
            elif path=='/api/v2/versions/workflow/'+uid(2):
                result={'versions':[{'id':uid(4),'versionNumber':1,'isPublished':True}]}
            elif path=='/api/v2/personas': result={'personas':[{'id':uid(5)}]}
            elif path=='/v1/practice/sessions':
                bank=True;state['session']+=1
                result={'variables':{**case['variables'],'customer_id':'UNIT_CUSTOMER_'+str(state['session'])}}
            elif path=='/api/v2/simulations' and request.method=='POST':
                state['models']=body
                result={'simulation':{'id':sim,'settings':{'maxRetries':0}}}
            elif path=='/api/v2/simulations': result={'simulations':[],'total':0}
            elif path=='/api/v2/simulations/'+sim: result={'simulation':{'id':sim,'status':'completed'}}
            elif path=='/api/v2/simulations/'+sim+'/iterations': result={'iterations':[{'id':iteration}]}
            elif path=='/api/v2/simulations/'+sim+'/iterations/'+iteration:
                result={'iteration':{'id':iteration,'agentVersionId':uid(4),'agentVersionNumber':1,'params':state['models']},
                    'runs':[{'id':'unit-run','personaId':uid(5),'callId':call,'outcome':'pass'}]}
            elif path.startswith('/v1/practice/ledger/'):
                bank=True
                result={key:copy.deepcopy(case['expected'][key]) for key in ('outcome','promises','notes','callbacks','transfers')}
                result.update(ok=True,reason=case['expected'].get('reason'),calls=[{'tool':tool,'result':{'ok':True}} for tool in ('verify_identity','verify_identity','get_account','create_promise','close_call')])
            elif path=='/api/v2/call/'+call:
                result={'call':{'id':call,'workspaceId':uid(1),'assistantWorkflowId':uid(2),'simulationId':sim,
                    'entityVersionId':uid(4),'entityVersionNumber':1,'startedAt':'2026-10-11T00:00:00Z','endedAt':'2026-10-11T00:01:00Z',
                    'messages':[{'role':'user','message':'UNIT synthetic transcript'}]}}
            else: raise AssertionError('unexpected synthetic request '+path)
            return Reply(result if bank else {'success':True,'data':{'result':result}})
        original=FreyaClient.__init__
        def initialize(client,*args):
            original(client,*args)
            client.opener=type('Opener',(),{'open':staticmethod(network)})()
        OUTPUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary:
            root=Path(temporary)
            def evidence_factory(path,**kwargs):
                sink=ContestEvidence(root/('evidence-'+str(len(sinks)+1)),protector=protect)
                sinks.append(sink)
                return sink
            with (patch.dict('os.environ',{'FREYA_API_KEY':'UNIT synthetic key','FREYA_API_URL':'https://synthetic.invalid',
                'FREYA_WORKSPACE_ID':uid(1),'FREYA_BANK_TEAM_KEY':'UNIT_SYNTHETIC_TEAM_KEY'}), patch.object(FreyaClient,'__init__',initialize),
                patch('orchestration.contest_practice.time.sleep'),patch('socket.socket',side_effect=AssertionError('NETWORK_FORBIDDEN'))):
                result=run(brief_path=KIT/'briefs/brief-a.md',tool_api_path=KIT/'tools/openapi.json',directory=root/'run',
                    provider='bounded',publish_live=True,slot='unit-'+root.name.replace('_',''),practice_case='PUB-01',
                    repetitions=3,evidence_factory=evidence_factory,deadline_seconds=60,record_credits=True)
            self.assertEqual(result['status'],'PRACTICE_CASE_PASSED',result.get('diagnostic'))
            self.assertEqual(len(sinks),3)
            self.assertEqual(state['session'],3)
            self.assertEqual([row['credit_observation']['observed_workspace_credit_decrease'] for row in result['tests'][0]['runs']],[1,1,1])
            self.assertEqual(len({row['customer_sha256'] for row in result['tests'][0]['runs']}),3)
            for index,sink in enumerate(sinks,1):
                seal=json.loads((root/'run'/('practice-evidence-seal-'+str(index)+'.json')).read_bytes())
                self.assertTrue(verify_bundle(sink.directory,seal['sha256'],decoder=recover)['wire_join_verified'])
                self.assertTrue(sink.sealed)
            self.assertFalse(result['final_submission_ready'])

if __name__=='__main__': unittest.main()
