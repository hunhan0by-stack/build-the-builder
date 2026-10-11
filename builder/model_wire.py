"""Typed model wire plan; render structure only, without executing policy code."""
import copy
import re
from .models import FoundationError, strict_json
from .api_contract import validate_value
from .hook_validation import validate_posthook_retention

def obj(properties):
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}
def arr(schema):
    return {'type':'array','items':schema}
def string(values=None):
    result={'type':'string'}
    if values is not None: result['enum']=values
    return result

MAPPING=obj({'path':string(),'variable':string()})
CHECK=obj({'operator':string(['always','matches_intent','equals','not_equals','greater_than','less_than','contains','is_set','is_not_set']),
    'variable':string(),'operand_json':string(),'description':string()})
TRANSITION=obj({'target_node':string(),'description':string(),'logic':string(['or','and']),'checks':arr(CHECK)})
ACTION=obj({'kind':string(['code_exec','verify_identity','get_account','create_promise','add_note','schedule_callback','transfer_to_agent','close_call']),
    'code_ref':string(),'pre_process_ref':string(),'post_process_ref':string(),
    'body_defaults':arr(obj({'field':string(),'value_json':string()})), 'response_mapping':arr(MAPPING)})
EXTRACT=obj({'name':string(),'type':string(['string','integer','float','boolean','date','datetime','name','phone_number']),
    'description':string(),'enum_values':arr(string()),'validation_pattern':string(),
    'min_confidence':{'type':'number'},'max_retries':{'type':'integer'},'extraction_prompt':string()})
NODE=obj({'name':string(),'node_type':string(['conversation','code','api_request','end_call']),
    'task_message':string(),'role_message':string(),'routing_prompt':string(),
    'loop_condition':string(),'loop_prompt':string(),
    'on_enter':obj({'type':string(['llm','silent','tts']),'text':string(),'prompt':string()}),
    'on_enter_first_entry_only':{'type':'boolean'},'passthrough':{'type':'boolean'},
    'passthrough_default_transition':string(),'max_node_turns':{'type':'integer'},
    'max_turn_transition':string(),'disable_inactivity_monitor':{'type':'boolean'},
    'suppress_tool_results':{'type':'boolean'},'extraction_schema':arr(EXTRACT),
    'tools':arr(ACTION),'pre_actions':arr(ACTION),'response_mapping':arr(MAPPING),
    'transitions':arr(TRANSITION)})
PLAN=obj({'initial_node':string(),'system_prompt':string(),'nodes':arr(NODE)})

def plan_schema(operation_names=None):
    schema=copy.deepcopy(PLAN)
    for key in ('tools','pre_actions'):
        field=schema['properties']['nodes']['items']['properties'][key]['items']['properties']['kind']
        field.clear()
        field.update(string(None if operation_names is None else ['code_exec', *operation_names]))
    return schema

def body_default_field(body, name):
    """Resolve an existing property only; prefer an exact literal dotted key."""
    remaining = name
    current = body
    path = []
    while True:
        properties = current.get('properties') if isinstance(current, dict) else None
        if not isinstance(properties, dict):
            raise FoundationError('BUILDER_MODEL_BANK_BINDING_INVALID')
        if remaining in properties:
            path.append(remaining)
            return properties[remaining], tuple(path)
        head, separator, tail = remaining.partition('.')
        if not separator or not head or not tail or head not in properties:
            raise FoundationError('BUILDER_MODEL_BANK_BINDING_INVALID')
        current = properties[head]
        path.append(head)
        remaining = tail


def render_plan(plan, code_blocks, templates, *, schema=None):
    # The CLI output schema is not an acceptance gate. Validate the complete
    # wire plan here so malformed nested fields enter the repair path instead
    # of escaping as KeyError/TypeError or being silently omitted.
    try:
        validate_value(plan, schema or PLAN, strict_fields=True)
    except (FoundationError, OverflowError, RecursionError, TypeError, ValueError):
        raise FoundationError('BUILDER_MODEL_PLAN_FIELDS_INVALID') from None
    try:
        validate_value(code_blocks, arr(obj({'id':string(),'code':string()})), strict_fields=True)
    except (FoundationError, OverflowError, RecursionError, TypeError, ValueError):
        raise FoundationError('BUILDER_MODEL_CODE_BLOCK_INVALID') from None
    if not isinstance(templates,dict) or not isinstance(templates.get('tools'),dict):
        raise FoundationError('BUILDER_MODEL_BANK_TEMPLATES_INVALID')
    blocks={}
    for row in code_blocks:
        if not isinstance(row,dict) or set(row) != {'id','code'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',row['id']) or row['id'] in blocks or not isinstance(row['code'],str):
            raise FoundationError('BUILDER_MODEL_CODE_BLOCK_INVALID')
        blocks[row['id']]=row['code']
    used=set()
    def code(identifier):
        if identifier not in blocks: raise FoundationError('BUILDER_MODEL_CODE_REFERENCE_INVALID')
        # tools-and-code.md forbids template delimiters in Python, including
        # adjacent closing braces in literals: the platform may substitute the
        # source before execution. Preserve the draft and request a repair.
        if '{{' in blocks[identifier] or '}}' in blocks[identifier]:
            raise FoundationError('BUILDER_MODEL_CODE_TEMPLATE_DELIMITERS_REFUSED')
        used.add(identifier)
        return blocks[identifier]
    def action(row):
        kind=row['kind']
        if kind=='code_exec':
            if row['body_defaults'] or row['pre_process_ref'] or row['post_process_ref']:
                raise FoundationError('BUILDER_MODEL_CODE_ACTION_FIELDS_INVALID')
            tool={'type':'function','function':{'name':'code_exec','handler':'__function__:code_exec',
                'parameters':{'type':'object','properties':{'code':{'type':'string','default':code(row['code_ref'])},
                'timeout_ms':{'type':'number','default':10000}}}}}
        else:
            if row['code_ref'] or kind not in templates['tools']:
                raise FoundationError('BUILDER_MODEL_BANK_ACTION_INVALID')
            tool=copy.deepcopy(templates['tools'][kind])
            tool['function']['parameters']['properties']['connect_retries']={'type':'number','default':0}
            body=tool['function']['parameters']['properties']['body']
            seen=set()
            for binding in row['body_defaults']:
                name=binding['field']
                field, path=body_default_field(body, name)
                if any(path[:len(previous)] == previous or previous[:len(path)] == path for previous in seen):
                    raise FoundationError('BUILDER_MODEL_BANK_BINDING_INVALID')
                seen.add(path)
                value=strict_json(binding['value_json'])
                dynamic=isinstance(value,str) and re.fullmatch(r'\{\{[A-Za-z0-9_.:-]+\}\}',value)
                if not dynamic:
                    try: validate_value(value,field,strict_fields=True)
                    except (FoundationError,OverflowError):
                        raise FoundationError('BUILDER_MODEL_LITERAL_DEFAULT_MISMATCH') from None
                # An inherited whole-object default shadows defaults on its
                # leaves. Explicit nested bindings must be resolved from the
                # child schema, while preserving sibling schemas and unrelated
                # top-level defaults. This edits only the copied template.
                ancestor = body
                for part in path[:-1]:
                    ancestor = ancestor['properties'][part]
                    ancestor.pop('default', None)
                field['default']=value
            for key in ('pre_process','post_process'):
                if row[key+'_ref']:
                    hook=code(row[key+'_ref'])
                    if key=='post_process':
                        try:
                            validate_posthook_retention(hook)
                        except FoundationError as error:
                            raise FoundationError(str(error)+':'+row[key+'_ref']) from None
                    tool[key+'_code']=hook
        if row['response_mapping']: tool['response_mapping']=copy.deepcopy(row['response_mapping'])
        return tool
    def condition(row):
        checks=[]
        for item in row['checks']:
            op=item['operator']
            check={'operator':op}
            if op=='matches_intent': check['description']=item['description']
            elif op!='always':
                check['variable']=item['variable']
                if op not in ('is_set','is_not_set'): check['operand']=strict_json(item['operand_json'])
            checks.append(check)
        if not checks: raise FoundationError('BUILDER_MODEL_TRANSITION_CHECKS_EMPTY')
        if len(checks)==1 and checks[0]['operator'] in ('always','matches_intent'): return checks[0]
        if row['logic']=='and': return {'logic':'or','conditions':[{'logic':'and','conditions':checks}]}
        return {'logic':'or','conditions':checks}
    nodes={}
    for row in plan['nodes']:
        if not isinstance(row,dict) or set(row)!=set(NODE['properties']) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}',row['name']) or row['name'] in nodes:
            raise FoundationError('BUILDER_MODEL_PLAN_NODE_INVALID')
        node={key:row[key] for key in ('name','node_type','on_enter_first_entry_only','passthrough','disable_inactivity_monitor','suppress_tool_results')}
        # Native suppression applies to tool results. Empty conversation/end
        # nodes have no such result; emit the observed platform default before
        # the graph is pinned, without relaxing readback value comparison.
        if row['node_type'] in ('conversation', 'end_call') and not row['tools'] and not row['pre_actions']:
            node['suppress_tool_results'] = False
        for key in ('task_message','role_message','routing_prompt','loop_condition','loop_prompt','passthrough_default_transition','max_turn_transition'):
            if row[key]: node[key]=row[key]
        if row['max_node_turns']>0: node['max_node_turns']=row['max_node_turns']
        node['on_enter']={'type':row['on_enter']['type']}
        for key in ('text','prompt'):
            if row['on_enter'][key]: node['on_enter'][key]=row['on_enter'][key]
        for key in ('tools','pre_actions'):
            if key=='pre_actions' and any(item['response_mapping'] for item in row[key]):
                raise FoundationError('BUILDER_MODEL_PRE_ACTION_MAPPING_REQUIRES_NODE_LEVEL')
            if row[key]: node[key]=[action(item) for item in row[key]]
        if row['response_mapping']: node['response_mapping']=copy.deepcopy(row['response_mapping'])
        if row['extraction_schema']:
            node['extraction_schema']=[]
            for extraction in row['extraction_schema']:
                entry={k:v for k,v in extraction.items() if v not in ('',[]) or k in ('min_confidence','max_retries')}
                if not 0<=entry['min_confidence']<=1 or not 0<=entry['max_retries']<=3:
                    raise FoundationError('BUILDER_MODEL_EXTRACTION_BOUNDS_INVALID')
                node['extraction_schema'].append(entry)
        node['transitions']=[{'target_node':t['target_node'],'description':t['description'],'condition':condition(t)} for t in row['transitions']]
        if row['node_type']=='end_call':
            if row['tools'] or row['pre_actions'] or row['transitions']:
                raise FoundationError('BUILDER_MODEL_END_NODE_ACTIONS_REFUSED')
            # A distinct native end reason preserves outcome intent; it is not a transfer API.
            terminal_name=row['name'] if schema is not None else 'end_call'
            node['terminal']={'type':'function','function':{'name':terminal_name,'handler':'__function__:end_call',
                'parameters':{'type':'object','properties':{'message_mode':{'type':'string','default':'none'}}}}}
        nodes[row['name']]=node
    if used != set(blocks): raise FoundationError('BUILDER_MODEL_UNUSED_CODE_BLOCK')
    return {'initial_node':plan['initial_node'],'system_prompt':plan['system_prompt'],'nodes':nodes,'global_nodes':{}}
