"""Source-cited, case-specific tool contracts. No practice operation inventory."""
import copy
import json
import re
from urllib.parse import urlsplit

from .api_contract import Operation, resolve_schema
from .models import FoundationError, strict_json


def mock_base(value):
    if not isinstance(value,str) or len(value)>2048:
        raise FoundationError('CASE_MOCK_URL_INVALID')
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None,443)
            or re.search(r'[\s\\%{}]',value)
            or any(part in ('.','..') for part in parsed.path.split('/'))):
        raise FoundationError('CASE_MOCK_URL_INVALID')
    return value.rstrip('/')


def verify_citation(citation, projection):
    docs=[d for d in projection['documents'] if d['path']==citation['document']]
    if len(docs)!=1 or not citation['quote'].strip():
        raise FoundationError('CASE_SOURCE_CITATION_INVALID')
    locator=strict_json(citation['locator_json'])
    segments=[s for s in docs[0]['segments'] if s['locator']==locator]
    if not any(citation['quote'] in s['text'] for s in segments):
        raise FoundationError('CASE_SOURCE_CITATION_INVALID')
    return citation['quote']


def cited_route(path, quotes):
    """A route may be written literally or as a documented base plus operation."""
    boundary=r'(?<![\w/%.-])'+re.escape(path)+r'(?![\w/%?.-])'
    if re.search(boundary,quotes):
        return True
    for address in re.findall(r'https?://[^\s<>`"\)]+',quotes):
        base=urlsplit(address).path
        if base==path:
            return True
        if base.endswith('/') and path.startswith(base):
            suffix=path[len(base):]
            if suffix and re.search(r'(?<![\w/.-])'+re.escape(suffix)+r'(?![\w/.-])',quotes):
                return True
    return False


def parse_schema_text(operation, field, value):
    try:
        return strict_json(value)
    except FoundationError as error:
        raise FoundationError(f'CASE_SCHEMA_JSON_INVALID {operation}.{field}: {error}') from None


class CaseContract:
    """Literal JSON endpoints supported; unsupported binding styles fail explicitly.

    Citation matching proves source location, not correctness of model interpretation.
    """
    def __init__(self, rows, projection, mock_url):
        self.server=mock_base(mock_url)
        self.operations={}
        self.body_required={}
        self.rows=copy.deepcopy(rows)
        seen=set()
        if not isinstance(rows,list) or not 1<=len(rows)<=64:
            raise FoundationError('CASE_OPERATION_COUNT_INVALID')
        for row in rows:
            name,path,method=row['name'],row['path'],row['method']
            if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',name)
                    or name in ('code_exec','end_call') or name in self.operations):
                raise FoundationError('CASE_OPERATION_NAME_INVALID')
            if (method not in ('GET','POST','PUT','PATCH','DELETE') or not path.startswith('/')
                    or path.startswith('//') or re.search(r'[\s\\%{}?#]',path)
                    or any(part in ('.','..') for part in path.split('/'))):
                raise FoundationError('CASE_LITERAL_ROUTE_REQUIRED')
            if (method,path) in seen:
                raise FoundationError('CASE_DUPLICATE_ROUTE')
            seen.add((method,path))
            if not row['citations']:
                raise FoundationError('CASE_OPERATION_CITATION_REQUIRED')
            quotes='\n'.join(verify_citation(c,projection) for c in row['citations'])
            if not cited_route(path,quotes) or not re.search(r'\b'+method+r'\b',quotes,re.I):
                raise FoundationError('CASE_ROUTE_SOURCE_MISMATCH')
            request=parse_schema_text(name,'request_schema_json',row['request_schema_json'])
            request=resolve_schema(request,request)
            if request.get('type')!='object' or 'properties' not in request:
                raise FoundationError('CASE_REQUEST_OBJECT_REQUIRED')
            if not row['has_body'] and (request['properties'] or request.get('required')):
                raise FoundationError('CASE_BODYLESS_FIELDS_REFUSED')
            if any(key not in request['properties'] for key in request.get('required',[])):
                raise FoundationError('CASE_REQUIRED_FIELD_UNDECLARED')
            missing=[key for key in request['properties']
                     if not re.search(r'(?<!\w)'+re.escape(key)+r'(?!\w)',quotes)]
            if missing:
                raise FoundationError('CASE_REQUEST_FIELD_SOURCE_MISMATCH '+name+': '+','.join(missing))
            responses=parse_schema_text(name,'response_schemas_json',row['response_schemas_json'])
            if (not isinstance(responses,dict) or not responses or not any(re.fullmatch(r'2\d\d',str(k)) for k in responses)
                    or any(not re.fullmatch(r'[1-5]\d\d',str(k)) for k in responses)):
                raise FoundationError('CASE_EXPLICIT_RESPONSE_STATUSES_REQUIRED')
            for schema in responses.values():
                if schema is not None:
                    resolve_schema(schema,schema)
            self.operations[name]=Operation(name,method,path,self.server+path,row['description'],
                request,responses,{status:'Source-defined response; do not infer success from HTTP alone.' for status in responses})
            self.body_required[name]=row['has_body']
        self.required_operations=set(self.operations)

    def freya_templates(self):
        tools={}
        for name,op in self.operations.items():
            body=copy.deepcopy(op.request)
            for field,schema in body['properties'].items():
                schema['default']='{{input_'+name+'_'+field+'}}'
            tools[name]={'type':'function','config':{'timeoutMs':30000,'maxRetries':0},
                'function':{'name':name,'handler':'__function__:api_call','description':op.summary,
                    'parameters':{'type':'object','properties':{
                        'url':{'type':'string','default':op.url},
                        'method':{'type':'string','default':op.method},
                        'headers':{'type':'object','properties':{
                            'Content-Type':{'type':'string','default':'application/json'},
                            'User-Agent':{'type':'string','default':'BuildTheBuilderMockClient/1.0'}}},
                        'body':body}}}}
        return {'tools':tools}

    def guard_tools(self, workflow):
        def walk(value):
            if isinstance(value,list):
                for item in value: walk(item)
            if not isinstance(value,dict): return
            function=value.get('function',{})
            if function.get('handler')=='__function__:api_call':
                name=function['name']
                op=self.operations[name]
                original=value.get('pre_process_code','')
                if '_case_' in original:
                    raise FoundationError('CASE_RESERVED_GUARD_NAME')
                # Hook code executes inside a function so early return cannot skip
                # the final route check. The documented hook mutates config.
                wrapped='def _case_hook(vars, config):\n'+''.join('    '+line+'\n' for line in (original or 'pass').splitlines())
                wrapped+='try:\n    _case_hook(vars, config)\nexcept Exception:\n    config["url"] = ""\n'
                wrapped+='if config.get("url") and (config.get("url") != '+repr(op.url)+' or config.get("method") != '+repr(op.method)+'):\n    config["url"] = ""\n'
                wrapped+='config["connect_retries"] = 0\n'
                if not self.body_required[name]:
                    wrapped+='config.pop("body", None)\n'
                value['pre_process_code']=wrapped
            for child in list(value.values()): walk(child)
        walk(workflow)
