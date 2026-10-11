"""Separate adjacent closing OP tokens only; never execute generated Python."""
import ast
import copy
import hashlib
import io
import json
import re
import sys
import tokenize

from .models import FoundationError

ALGORITHM = 'python-adjacent-closing-ops-1'
MAX_CODE_BYTES = 150_000


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FormatRefused(FoundationError):
    def __init__(self, receipt):
        super().__init__('BUILDER_MODEL_CODE_TEMPLATE_DELIMITERS_REFUSED')
        self.receipt = receipt


def _refuse(reason):
    raise ValueError(reason)


def normalize_python(code):
    """Return identical source or whitespace insertion with AST/compile proof.

    Delimiters inside strings, comments and f-strings are never rewritten.
    Opening delimiters and every unproven closing occurrence are refused.
    """
    if type(code) is not str:
        _refuse('CODE_TYPE_INVALID')
    encoded = code.encode('utf-8')
    if len(encoded) > MAX_CODE_BYTES:
        _refuse('CODE_SIZE_LIMIT')
    original_hash = digest(encoded)
    report = {'input_sha256': original_hash, 'output_sha256': original_hash,
              'status': 'UNCHANGED', 'inserted_spaces': 0,
              'code_executed': False, 'semantic_correctness_verified': False}
    if '{{' not in code and '}}' not in code:
        return code, report
    if '{{' in code:
        _refuse('OPENING_DELIMITER_REFUSED')
    try:
        before = ast.parse(code, mode='exec', type_comments=True)
        compile(code, '<builder-format-check>', 'exec', dont_inherit=True, optimize=0)
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (SyntaxError, tokenize.TokenError, ValueError, RecursionError, OverflowError):
        _refuse('ORIGINAL_PYTHON_NOT_PROVEN')
    offsets = [0]
    for line in code.split('\n')[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    def offset(position):
        return offsets[position[0] - 1] + position[1]
    eligible = {}
    previous = None
    depth = 0
    for token in tokens:
        kind = tokenize.tok_name.get(token.type, '')
        if kind in ('FSTRING_START', 'TSTRING_START'):
            depth += 1
        if (depth == 0 and token.type == tokenize.OP and token.string == '}'
                and previous is not None and previous.type == tokenize.OP
                and previous.string == '}' and previous.end == token.start):
            eligible[offset(token.start)] = {'line': token.start[0], 'column': token.start[1] + 1}
        if kind in ('FSTRING_END', 'TSTRING_END'):
            depth -= 1
            if depth < 0:
                _refuse('STRING_TOKEN_CONTEXT_UNPROVEN')
        previous = token if depth == 0 else None
    if depth:
        _refuse('STRING_TOKEN_CONTEXT_UNPROVEN')
    positions = [match.start() + 1 for match in re.finditer(r'(?=\}\})', code)]
    if not positions or any(position not in eligible for position in positions):
        _refuse('NON_OPERATOR_DELIMITER_REFUSED')
    pieces = []
    start = 0
    for position in positions:
        pieces.extend((code[start:position], ' '))
        start = position
    pieces.append(code[start:])
    normalized = ''.join(pieces)
    if '{{' in normalized or '}}' in normalized:
        _refuse('RESIDUAL_DELIMITER_REFUSED')
    try:
        after = ast.parse(normalized, mode='exec', type_comments=True)
        compile(normalized, '<builder-format-check>', 'exec', dont_inherit=True, optimize=0)
    except (SyntaxError, ValueError, RecursionError, OverflowError):
        _refuse('NORMALIZED_PYTHON_NOT_PROVEN')
    try:
        before_ast = ast.dump(before, include_attributes=False)
        after_ast = ast.dump(after, include_attributes=False)
    except (RecursionError, OverflowError):
        _refuse('AST_COMPARISON_NOT_PROVEN')
    if before_ast != after_ast:
        _refuse('AST_CHANGED_REFUSED')
    report.update(status='NORMALIZED', output_sha256=digest(normalized.encode('utf-8')),
        inserted_spaces=len(positions), insertion_positions=[eligible[p] for p in positions[:64]],
        insertion_positions_truncated=len(positions) > 64,
        ast_sha256=digest(before_ast.encode('utf-8')), ast_equivalent=True, compile_checked=True)
    return normalized, report


def normalize_code_blocks(blocks, raw_response_sha256):
    """Normalize atomically on a copy, preserving raw response and every value."""
    if (type(blocks) is not list or type(raw_response_sha256) is not str
            or not re.fullmatch(r'[0-9a-f]{64}', raw_response_sha256)):
        raise FoundationError('BUILDER_MODEL_CODE_BLOCK_INVALID')
    seen = set()
    for row in blocks:
        if (type(row) is not dict or set(row) != {'id', 'code'}
                or type(row['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', row['id'])
                or row['id'] in seen or type(row['code']) is not str):
            raise FoundationError('BUILDER_MODEL_CODE_BLOCK_INVALID')
        seen.add(row['id'])
    receipt = {'kind': 'python-code-format-normalization-1', 'algorithm': ALGORITHM,
        'python_version': list(sys.version_info[:3]), 'raw_response_sha256': raw_response_sha256,
        'input_code_blocks_sha256': digest(canonical_bytes(blocks)), 'status': 'UNCHANGED',
        'blocks': [], 'code_executed': False, 'semantic_correctness_verified': False}
    normalized = copy.deepcopy(blocks)
    for index, row in enumerate(blocks):
        try:
            code, report = normalize_python(row['code'])
        except (ValueError, UnicodeError) as error:
            # Only our finite reason vocabulary reaches the artifact. Never
            # include SyntaxError text, source snippets or arbitrary exceptions.
            reason = str(error)
            allowed = {'CODE_TYPE_INVALID', 'CODE_SIZE_LIMIT', 'OPENING_DELIMITER_REFUSED',
                'ORIGINAL_PYTHON_NOT_PROVEN', 'STRING_TOKEN_CONTEXT_UNPROVEN',
                'NON_OPERATOR_DELIMITER_REFUSED', 'RESIDUAL_DELIMITER_REFUSED',
                'NORMALIZED_PYTHON_NOT_PROVEN', 'AST_CHANGED_REFUSED', 'AST_COMPARISON_NOT_PROVEN'}
            receipt.update(status='REFUSED', reason=reason if reason in allowed else 'CODE_ENCODING_REFUSED',
                           refused_block_index=index, refused_block_id=row['id'])
            raise FormatRefused(receipt) from None
        normalized[index]['code'] = code
        receipt['blocks'].append(dict(index=index, code_block_id=row['id'], **report))
    receipt['output_code_blocks_sha256'] = digest(canonical_bytes(normalized))
    if any(row['status'] == 'NORMALIZED' for row in receipt['blocks']):
        receipt['status'] = 'NORMALIZED'
    return normalized, receipt
