"""Typed model wire plan; render structure only, without executing policy code."""
import copy
import re
from .models import FoundationError, strict_json
from .api_contract import validate_value

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

def render_plan(plan, code_blocks, templates):
    # The CLI output schema is not an acceptance gate. Validate the complete
    # wire plan here so malformed nested fields enter the repair path instead
    # of escaping as KeyError/TypeError or being silently omitted.
    try:
        validate_value(plan, PLAN, strict_fields=True)
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
            fields=tool['function']['parameters']['properties']['body']['properties']
            seen=set()
            for binding in row['body_defaults']:
                name=binding['field']
                if name not in fields or name in seen:
                    raise FoundationError('BUILDER_MODEL_BANK_BINDING_INVALID')
                seen.add(name)
                value=strict_json(binding['value_json'])
                dynamic=isinstance(value,str) and re.fullmatch(r'\{\{[A-Za-z0-9_.:-]+\}\}',value)
                if not dynamic:
                    try: validate_value(value,fields[name],strict_fields=True)
                    except (FoundationError,OverflowError):
                        raise FoundationError('BUILDER_MODEL_LITERAL_DEFAULT_MISMATCH') from None
                fields[name]['default']=value
            for key in ('pre_process','post_process'):
                if row[key+'_ref']: tool[key+'_code']=code(row[key+'_ref'])
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
            node['terminal']={'type':'function','function':{'name':'end_call','handler':'__function__:end_call',
                'parameters':{'type':'object','properties':{'message_mode':{'type':'string','default':'none'}}}}}
        nodes[row['name']]=node
    if used != set(blocks): raise FoundationError('BUILDER_MODEL_UNUSED_CODE_BLOCK')
    return {'initial_node':plan['initial_node'],'system_prompt':plan['system_prompt'],'nodes':nodes,'global_nodes':{}}
