"""Explicit deterministic assembly continuation of an authentic recorded model draft."""
import hashlib
import json
from pathlib import Path

from builder.models import FoundationError, strict_json
from builder.model_workflow import checked_model_events
from builder.python_format import canonical_bytes

def sha(value):return hashlib.sha256(value).hexdigest()

class RecordedCaseResponse:
    is_recorded_response=True
    def __init__(self,directory,attempt,projection,mock_url,*,references):
        prior=Path(directory).resolve()
        if type(attempt) is not int or not 1<=attempt<=3:
            raise FoundationError('CASE_REPLAY_ATTEMPT_INVALID')
        trace_raw=(prior/'end-to-end-trace.json').read_bytes()
        trace=strict_json(trace_raw.decode('utf-8'))
        if trace.get('status')=='STARTED':
            raise FoundationError('CASE_REPLAY_SOURCE_RUN_STILL_ACTIVE')
        if trace.get('manifest_sha256')!=projection['manifest_sha256'] or trace.get('mock_url')!=mock_url:
            raise FoundationError('CASE_REPLAY_SOURCE_MISMATCH')
        if sorted(trace.get('reference_only_paths',[]))!=sorted(projection.get('reference_only_paths',[])):
            raise FoundationError('CASE_REPLAY_INPUT_ROLE_MISMATCH')
        records=[row for row in trace.get('attempts',[]) if row.get('attempt')==attempt]
        if len(records)!=1 or not records[0].get('response_sha256'):
            raise FoundationError('CASE_REPLAY_COMPLETE_RESPONSE_REQUIRED')
        original=prior/('model-attempt-'+str(attempt))
        names=('model-response.json','model-session.jsonl','model-input.txt',
               'model-response.schema.json','model-process-status.json')
        self.files={name:(original/name).read_bytes() for name in names}
        model_input=self.files['model-input.txt'].decode('utf-8').replace('\r\n','\n')
        try:
            provided=strict_json(model_input.split('\nNATIVE_FORMAT_REFERENCES\n',1)[1].split('\nCASE_SOURCE_PROJECTION\n',1)[0])
        except (IndexError,FoundationError):
            raise FoundationError('CASE_REPLAY_REFERENCE_INPUT_INVALID') from None
        if provided!=references:
            raise FoundationError('CASE_REPLAY_PLATFORM_REFERENCES_CHANGED')
        self.trace_raw=trace_raw
        raw=self.files['model-response.json']
        if sha(raw)!=records[0]['response_sha256']:
            raise FoundationError('CASE_REPLAY_RESPONSE_CHANGED')
        self.response=strict_json(raw.decode('utf-8'))
        session=self.files['model-session.jsonl'].decode('utf-8')
        checked_model_events(session)
        events=[strict_json(line) for line in session.splitlines() if line.strip()]
        messages=[row['item'].get('text') for row in events if row.get('type')=='item.completed'
            and isinstance(row.get('item'),dict) and row['item'].get('type')=='agent_message']
        if not messages or not isinstance(messages[-1],str) or canonical_bytes(strict_json(messages[-1]))!=canonical_bytes(self.response):
            raise FoundationError('CASE_REPLAY_SESSION_RESPONSE_MISMATCH')
        process=strict_json(self.files['model-process-status.json'].decode('utf-8'))
        if process.get('output_sha256_at_exit')!=sha(raw) or process.get('return_code')!=0:
            raise FoundationError('CASE_REPLAY_PROCESS_RESPONSE_MISMATCH')
        self.model=records[0].get('model_requested')
        self.reasoning_effort=records[0].get('reasoning_effort_requested')
        self.origin={'run_id':trace['run_id'],'attempt':attempt,
            'trace_sha256':sha(trace_raw),'original_run_status':trace['status'],
            'original_attempt_status':records[0]['status'],
            'response_sha256':sha(raw),'session_sha256':sha(self.files['model-session.jsonl']),
            'input_sha256':sha(self.files['model-input.txt']),
            'source_runtime_sha256':trace['runtime_sha256'],
            'new_model_calls':0,'raw_response_edited':False,'raw_session_edited':False}
        self.paths={prior/'end-to-end-trace.json':sha(trace_raw)}
        self.paths.update({original/name:sha(value) for name,value in self.files.items()})
    def structured_response(self,*,directory,**kwargs):
        if any(sha(path.read_bytes())!=value for path,value in self.paths.items()):
            raise FoundationError('CASE_REPLAY_ORIGIN_CHANGED')
        for name,value in self.files.items():
            path=Path(directory)/name
            if path.exists():raise FoundationError('CASE_REPLAY_FRESH_OUTPUT_REQUIRED')
            path.write_bytes(value)
        (Path(directory)/'response-origin.json').write_text(json.dumps(self.origin,indent=2),encoding='utf-8')
        (Path(directory)/'origin-run-trace.json').write_bytes(self.trace_raw)
        return self.response,self.files['model-response.json'],None
