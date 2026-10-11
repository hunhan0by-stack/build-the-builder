"""Aggregate safe model-output diagnostics without repair or acceptance authority.

Only saved output and the supplied brief are read. A caller may provide its
existing static validator; successful diagnostics never establish semantics or
live behavior. No policy, workflow, or unresolved entry is changed here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Callable

from builder.models import FoundationError


# An arbitrary exception string, even an uppercase identifier, is not safe to
# emit. Only the existing static gate's fixed diagnostic vocabulary may pass.
_VALIDATOR_CODES = frozenset({
    'CONTEST_WORKFLOW_INVALID', 'CONTEST_GRAPH_INITIAL_NODE_INVALID',
    'CONTEST_DYNAMIC_CODE_REFUSED', 'CONTEST_CODE_SYNTAX_INVALID',
    'CONTEST_CODE_IMPORT_REFUSED', 'CONTEST_CODE_HOST_ACCESS_REFUSED',
    'CONTEST_WORKFLOW_NULL_REFUSED', 'CONTEST_TOOL_ENDPOINT_MISMATCH',
    'CONTEST_TOOL_BLIND_RETRY_REFUSED', 'CONTEST_TOOL_BODY_FIELDS_MISMATCH',
    'CONTEST_TOOL_REQUIRED_FIELDS_MISMATCH', 'CONTEST_TOOL_SCHEMA_MISMATCH',
    'CONTEST_HANDLER_UNSUPPORTED', 'CONTEST_STRIPPED_PRE_ACTIONS',
    'CONTEST_STRIPPED_TOOLS', 'CONTEST_TRANSITION_TARGET_INVALID',
    'CONTEST_REQUIRED_BANK_TOOLS_MISSING', 'CONTEST_PUBLICATION_BODY_TOO_LARGE',
    'CONTEST_CODE_REQUEST_TOO_LARGE', 'CONTEST_CODE_TEMPLATE_DELIMITERS_REFUSED',
})
_FIELDS = frozenset({'workflow_json', 'unresolved', 'coverage', 'limitations'})


class _ParseIssue(ValueError):
    def __init__(self, code):
        self.code = code


def _parse(text, scope, issues):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _ParseIssue('JSON_DUPLICATE_KEY')
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise _ParseIssue('JSON_NONFINITE_NUMBER')
        return number

    def constant(unused):
        raise _ParseIssue('JSON_NONFINITE_NUMBER')

    try:
        return json.loads(text, object_pairs_hook=unique, parse_float=finite,
                          parse_constant=constant), True
    except _ParseIssue as error:
        issues.append({'scope': scope, 'code': error.code})
    except json.JSONDecodeError as error:
        issues.append({'scope': scope, 'code': 'JSON_SYNTAX_INVALID',
                       'line': error.lineno, 'column': error.colno})
    except (RecursionError, UnicodeError):
        issues.append({'scope': scope, 'code': 'JSON_DECODING_LIMIT'})
    except ValueError:
        # Python bounds integer digit conversion. Its exception message is an
        # implementation detail; emit only a fixed resource-limit diagnostic.
        issues.append({'scope': scope, 'code': 'JSON_NUMERIC_LIMIT'})
    return None, False


def diagnose_model_response(response_json: str | bytes, brief: str,
                            validator: Callable[[dict], object] | None = None) -> dict:
    """Return code-only diagnostics for an exact response, never a product PASS.

    The optional validator receives an isolated copy. Its response payload and
    error text are discarded. Existing allowlisted FoundationError codes alone
    are retained. Source quotation checks continue when workflow JSON is bad;
    unresolved issues never hide syntax, coverage, or contract issues.
    """
    issues = []
    result = {'kind': 'model-output-diagnostics-1', 'issues': issues,
              'response_json_valid': False, 'workflow_json_valid': False,
              'unresolved_count': 0, 'coverage_count': 0,
              'coverage_node_binding': 'NOT_CHECKED',
              'validator_status': 'NOT_REQUESTED' if validator is None else 'NOT_RUN',
              'semantic_correctness_verified': False, 'live_success_verified': False,
              'repair_performed': False, 'acceptance_authority': False}
    if type(response_json) not in (str, bytes) or type(brief) is not str:
        issues.append({'scope': 'response', 'code': 'INPUT_TYPE_INVALID'})
        result['issue_count'] = len(issues)
        return result
    try:
        encoded = response_json.encode('utf-8') if isinstance(response_json, str) else response_json
        brief_encoded = brief.encode('utf-8')
    except UnicodeError:
        issues.append({'scope': 'response', 'code': 'JSON_DECODING_LIMIT'})
        result['issue_count'] = len(issues)
        return result
    result['response_sha256'] = hashlib.sha256(encoded).hexdigest()
    result['brief_sha256'] = hashlib.sha256(brief_encoded).hexdigest()
    if len(encoded) > 2_000_000:
        issues.append({'scope': 'response', 'code': 'RESPONSE_SIZE_LIMIT'})
        result['issue_count'] = len(issues)
        return result
    response, outer_valid = _parse(response_json, 'response', issues)
    result['response_json_valid'] = outer_valid
    if not outer_valid or not isinstance(response, dict):
        if outer_valid:
            issues.append({'scope': 'response', 'code': 'RESPONSE_OBJECT_REQUIRED'})
        result['issue_count'] = len(issues)
        return result

    missing = _FIELDS - set(response)
    extra = set(response) - _FIELDS
    if missing:
        issues.append({'scope': 'response', 'code': 'RESPONSE_FIELDS_MISSING',
                       'fields': sorted(missing)})
    if extra:
        issues.append({'scope': 'response', 'code': 'RESPONSE_EXTRA_FIELDS', 'count': len(extra)})

    unresolved = response.get('unresolved')
    if isinstance(unresolved, list):
        result['unresolved_count'] = len(unresolved)
        if unresolved:
            issues.append({'scope': 'policy', 'code': 'UNRESOLVED_POLICY', 'count': len(unresolved)})
        if any(not isinstance(item, str) for item in unresolved):
            issues.append({'scope': 'response', 'code': 'UNRESOLVED_TYPE_INVALID'})
    elif 'unresolved' in response:
        issues.append({'scope': 'response', 'code': 'UNRESOLVED_TYPE_INVALID'})
    if 'limitations' in response and (not isinstance(response['limitations'], list)
            or any(not isinstance(item, str) for item in response['limitations'])):
        issues.append({'scope': 'response', 'code': 'LIMITATIONS_TYPE_INVALID'})

    workflow = None
    if isinstance(response.get('workflow_json'), str):
        workflow, inner_valid = _parse(response['workflow_json'], 'workflow', issues)
        result['workflow_json_valid'] = inner_valid
        if inner_valid and not isinstance(workflow, dict):
            issues.append({'scope': 'workflow', 'code': 'WORKFLOW_OBJECT_REQUIRED'})
            workflow = None
    elif 'workflow_json' in response:
        issues.append({'scope': 'workflow', 'code': 'WORKFLOW_JSON_STRING_REQUIRED'})

    nodes = workflow.get('nodes') if isinstance(workflow, dict) else None
    if isinstance(nodes, dict):
        result['coverage_node_binding'] = 'CHECKED'
    elif workflow is not None:
        issues.append({'scope': 'workflow', 'code': 'WORKFLOW_NODES_OBJECT_REQUIRED'})

    coverage = response.get('coverage')
    if isinstance(coverage, list):
        result['coverage_count'] = len(coverage)
        if not coverage:
            issues.append({'scope': 'coverage', 'code': 'COVERAGE_EMPTY'})
        for index, row in enumerate(coverage):
            if not isinstance(row, dict) or set(row) != {'quote', 'nodes', 'implementation'}:
                issues.append({'scope': 'coverage', 'code': 'COVERAGE_ROW_INVALID', 'row': index})
                continue
            quote = row['quote']
            identifiers = row['nodes']
            implementation = row['implementation']
            if not isinstance(quote, str) or not quote or quote not in brief:
                issues.append({'scope': 'coverage', 'code': 'COVERAGE_SOURCE_MISMATCH', 'row': index})
            if not isinstance(implementation, str) or not implementation.strip():
                issues.append({'scope': 'coverage', 'code': 'COVERAGE_IMPLEMENTATION_INVALID', 'row': index})
            identifiers_valid = isinstance(identifiers, list) and bool(identifiers) and all(
                isinstance(name, str) for name in identifiers)
            if not identifiers_valid:
                issues.append({'scope': 'coverage', 'code': 'COVERAGE_NODES_INVALID', 'row': index})
            elif isinstance(nodes, dict) and any(name not in nodes for name in identifiers):
                issues.append({'scope': 'coverage', 'code': 'COVERAGE_NODE_MISMATCH', 'row': index})
    elif 'coverage' in response:
        issues.append({'scope': 'coverage', 'code': 'COVERAGE_TYPE_INVALID'})

    if validator is not None and isinstance(workflow, dict):
        try:
            returned = validator(copy.deepcopy(workflow))
            if returned is False or (isinstance(returned, dict) and returned.get('static_valid') is False):
                result['validator_status'] = 'REJECTED'
                issues.append({'scope': 'contract', 'code': 'VALIDATOR_REJECTED'})
            else:
                result['validator_status'] = 'RETURNED_WITHOUT_ERROR'
        except FoundationError as error:
            result['validator_status'] = 'REJECTED'
            # No raw exception text is returned, including credential-like text.
            code = str(error)
            issues.append({'scope': 'contract', 'code': 'VALIDATOR_REJECTED',
                           'validator_code': code if code in _VALIDATOR_CODES else 'UNCLASSIFIED_VALIDATOR_REJECTION'})
        except Exception:
            result['validator_status'] = 'CALLBACK_FAILED'
            issues.append({'scope': 'contract', 'code': 'VALIDATOR_CALLBACK_FAILED'})
    result['issue_count'] = len(issues)
    return result
