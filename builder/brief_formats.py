"""Offline source-preserving format adapter for bounded new bank briefs.

Recognizes explicit prose variants and typed tables, not arbitrary language.
Canonical text is an internal parsing projection only. Published IR, wording,
hashes and evidence always refer to the caller's original documents. Unknown
requirements, table columns and conflicting memos retain the existing rejection.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import time

from compiler.core import make_request, validate_decisions
from compiler.typed_source import source_wording
from .api_contract import BankContract
from .brief_requirements import derive_brief_requirements
from .compiled_policy import lower_compilation
from .ingest import collect_sources, read_source
from .models import Document, FoundationError, strict_json
from .semantic_renderer import validate_wording_bundle


VERSION = 'source-preserving-brief-formats-1'
_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_CODE = re.compile(r'[a-z][a-z0-9_]*')


def _error(code):
    raise FoundationError('BRIEF_FORMAT_' + code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sentence(text, depth=0):
    """Return a finite syntactic equivalent; unknown sentences pass to rejection."""
    if len(text) > 16000 or depth > 1:
        _error('CLAUSE_BUDGET')
    amendment = re.fullmatch(r'(?:This memo replaces|Replace) rule ([1-9][0-9]*) (?:in|of) policy ([A-Za-z][A-Za-z0-9_.-]{0,79}) with:\s*(.+)', text)
    if amendment:
        return 'This memo overrides rule ' + amendment[1] + ' of policy ' + amendment[2] + ' with: ' + _sentence(amendment[3], depth+1)
    named_memo = re.fullmatch(r'This memo overrides rule ([1-9][0-9]*) of policy ([A-Za-z][A-Za-z0-9_.-]{0,79}) with: (.+)', text)
    if named_memo:
        return text[:named_memo.start(3)] + _sentence(named_memo[3], depth+1)
    patterns = (
        (r'Verify identity in this order: (.+)\.', 'Identity questions, in order: {}.'),
        (r'No more than ([0-9]+) incorrect identity answers are allowed\.', 'Maximum wrong identity answers: {}.'),
        (r'A customer may have up to ([0-9]+) products\.', 'Maximum products: {}.'),
        (r'Payment promises must not exceed ([0-9]+(?:\.[0-9]{1,2})?)\.', 'Maximum payment amount: {}.'),
        (r'Payment promises must be dated no later than ([0-9]{4}-[0-9]{2}-[0-9]{2})\.', 'Latest payment date: {}.'),
        (r'Payment promises must be dated no earlier than ([0-9]{4}-[0-9]{2}-[0-9]{2})\.', 'Earliest payment date: {}.'),
    )
    for pattern, template in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return template.format(match[1])
    synonyms = {
        'Only read accounts after all identity checks pass.': 'Read accounts only after all required identity questions pass.',
        'A payment promise requires explicit confirmation of the current readback.': 'Record promises only after an explicit current readback confirmation.',
        'The promise amount must be between the account floor and the past-due balance, inclusive.': 'Record promises only within the account floor and past-due amount.',
        'Payment dates must be business days.': 'Record promises only on business days.',
        'The call must be closed exactly once, after every other action.': 'Close each call once, after all other actions.',
        'Say goodbye before you close the call.': 'Say goodbye before closing the call.',
        'Retry a failed promise write once.': 'Promise write attempts: 2.',
    }
    if text in synonyms:
        return synonyms[text]
    days = re.fullmatch(r'Business days: (.+)\.', text)
    if days:
        values = [value.strip() for value in days[1].split(',')]
        if not values or any(value not in _DAYS for value in values) or len(set(values)) != len(values):
            _error('INVALID_WEEKDAY_LIST')
        return 'Parameter business_weekdays is ' + _json([_DAYS.index(value) for value in values]) + '.'
    holidays = re.fullmatch(r'Bank holidays: (none|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:;\s*[0-9]{4}-[0-9]{2}-[0-9]{2})*)\.', text)
    if holidays:
        return 'Bank holidays are ' + ', '.join(value.strip() for value in holidays[1].split(';')) + '.'
    return text


def _table(rows):
    """Only explicit field-name tables; filenames or guessed positions are unused."""
    # csv.reader represents an empty physical line as []; an actual row of
    # empty cells is still data and must pass normal field validation.
    rows = [row for row in rows if row]
    if not 2 <= len(rows) <= 129:
        _error('TABLE_SIZE')
    headers = [value.strip().lower().replace(' ', '_') for value in rows[0]]
    if len(set(headers)) != len(headers) or any(len(row) != len(headers) for row in rows[1:]):
        _error('AMBIGUOUS_TABLE_SHAPE')
    values = [{key: value.strip() for key, value in zip(headers, row)} for row in rows[1:]]
    if set(headers) == {'outcome', 'reasons', 'when'}:
        if len({row['outcome'] for row in values}) != len(values):
            _error('DUPLICATE_OUTCOME')
        for row in values:
            row['reasons'] = row['reasons'].split(';') if row['reasons'] else []
            row['reasons'] = [reason.strip() for reason in row['reasons']]
            if not _CODE.fullmatch(row['outcome']) or not row['when'] or any(not _CODE.fullmatch(reason) for reason in row['reasons']) or len(set(row['reasons'])) != len(row['reasons']):
                _error('INVALID_OUTCOME_ROW')
        return 'Closing outcomes: ' + _json(values) + '.'
    if set(headers) == {'product_type', 'floor_field'}:
        if len({row['product_type'] for row in values}) != len(values) or any(not _CODE.fullmatch(v) for row in values for v in row.values()):
            _error('INVALID_FLOOR_ROW')
        return 'Floor fields: ' + _json({row['product_type']: row['floor_field'] for row in values}) + '.'
    if set(headers) == {'name', 'min_days_late', 'max_days_late', 'deadline_business_days', 'persuasion_attempts'}:
        for row in values:
            if not row['name']:
                _error('MISSING_BAND_NAME')
            for key in set(headers) - {'name'}:
                if not re.fullmatch(r'[0-9]{1,6}', row[key]):
                    _error('INVALID_BAND_INTEGER')
                row[key] = int(row[key])
        return 'Delinquency bands: ' + _json(values) + '.'
    _error('UNKNOWN_TABLE_COLUMNS')


def _project(document):
    if document.status != 'processed' or document.kind not in {'.md', '.txt', '.eml', '.csv', '.tsv'}:
        _error('UNSUPPORTED_MODALITY')
    if any('attachment' in item.lower() for item in document.limitations):
        _error('UNREAD_ATTACHMENT')
    spans, emitted, cursor = [], [], 0
    def add(text, start, end):
        nonlocal cursor
        text += '\n'
        spans.append(dict(start=cursor, end=cursor+len(text), source_start=start, source_end=end))
        emitted.append(text)
        cursor += len(text)
    if document.kind in {'.csv', '.tsv'}:
        rows = list(csv.reader(io.StringIO(document.text), delimiter='\t' if document.kind == '.tsv' else ',', strict=True))
        add(_table(rows), 0, len(document.text))
    else:
        lines = document.text.splitlines(keepends=True)
        offsets, pos = [], 0
        for line in lines:
            offsets.append(pos); pos += len(line)
        i = 0
        while i < len(lines):
            raw = lines[i]; stripped = raw.strip()
            if not stripped:
                i += 1; continue
            start = offsets[i] + len(raw) - len(raw.lstrip())
            if stripped.startswith('|'):
                first, rows = i, []
                while i < len(lines) and lines[i].strip().startswith('|'):
                    line = lines[i].strip()
                    if not line.endswith('|'):
                        _error('MALFORMED_MARKDOWN_TABLE')
                    cells = [value.strip() for value in line[1:-1].split('|')]
                    if i == first + 1 and all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells):
                        if len(cells) != len(rows[0]):
                            _error('AMBIGUOUS_TABLE_SHAPE')
                    else:
                        rows.append(cells)
                    i += 1
                add(_table(rows), offsets[first], offsets[i-1]+len(lines[i-1]))
                continue
            number = re.fullmatch(r'([1-9][0-9]*)[.)]\s+(.+)', stripped)
            bullet = re.fullmatch(r'[-*]\s+(.+)', stripped)
            if number:
                canonical = number[1] + '. ' + _sentence(number[2])
            else:
                canonical = _sentence(bullet[1] if bullet else stripped)
            add(canonical, start, start+len(stripped))
            i += 1
    text = ''.join(emitted)
    if not text or len(text) > 1_000_000:
        _error('SOURCE_BUDGET')
    virtual = Document(document.source_id, 'projection:' + document.source_id, hashlib.sha256(text.encode()).hexdigest(), len(text.encode()), '.txt', text)
    return virtual, spans


def derive_formatted_requirements(documents, contract, *, evaluation_source_hashes=()):
    """Produce original-source IR for prose variants, Markdown and CSV/TSV lists."""
    documents = list(documents)
    if not 1 <= len(documents) <= 32 or sum(len(doc.text) for doc in documents) > 1_000_000:
        _error('SOURCE_BUDGET')
    if len({doc.source_id for doc in documents}) != len(documents):
        _error('DUPLICATE_SOURCE')
    excluded = set(evaluation_source_hashes)
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in excluded):
        _error('INVALID_EVALUATION_HASH')
    if any(doc.sha256 in excluded for doc in documents):
        _error('EVALUATION_DATA_NOT_POLICY')
    sources = {doc.source_id: doc for doc in documents}
    projections, mappings = [], {}
    for doc in documents:
        projected, mapping = _project(doc)
        projections.append(projected); mappings[doc.source_id] = mapping
    result = derive_brief_requirements(projections, contract)
    def remap(value):
        if isinstance(value, dict):
            if set(value) == {'source_id', 'start', 'end', 'quote'}:
                hits = [m for m in mappings[value['source_id']] if m['start'] <= value['start'] < value['end'] <= m['end']]
                if len(hits) != 1:
                    _error('UNMAPPED_SOURCE_SPAN')
                item = hits[0]; doc = sources[value['source_id']]
                value.update(start=item['source_start'], end=item['source_end'], quote=doc.text[item['source_start']:item['source_end']])
            else:
                for child in value.values(): remap(child)
        elif isinstance(value, list):
            for child in value: remap(child)
    remap(result['compilation']['decisions'])
    remap(result['derivation']['structural_blocks'])
    envelope = result['compilation']
    envelope['sources'] = [doc.inventory() for doc in documents]
    envelope['request_sha256'] = hashlib.sha256(_json(make_request(documents, contract)).encode()).hexdigest()
    validate_decisions(envelope['decisions'], documents, contract)
    result['kind'] = VERSION
    result['derivation'].update(original_sources=[doc.inventory() for doc in documents],
        grammar_scope='bounded prose equivalents; explicit floor/band/outcome tables; referenced memo overrides',
        parsing_projection_used=True, projection_text_claimed_as_original=False,
        full_source_semantic_coverage=False, model_calls=0, network_calls=0)
    return result


def compile_formatted_documents(documents, contract, *, evaluation_source_hashes=()):
    started = time.perf_counter(); documents = list(documents)
    result = derive_formatted_requirements(documents, contract, evaluation_source_hashes=evaluation_source_hashes)
    compiled = lower_compilation(result['compilation'], documents, contract)
    wording = validate_wording_bundle(source_wording(documents), documents)
    elapsed = time.perf_counter()-started
    if elapsed > 1800:
        _error('BRIEF_DEADLINE_EXCEEDED')
    return dict(compiled=compiled, documents=documents, wording_bundle=wording, compilation=result['compilation'],
        derivation=result['derivation'], scope=VERSION, publish_allowed=False, elapsed_seconds=elapsed,
        unsupported=['arbitrary_prose_interpretation', 'unknown_table_columns', 'unread_attachments',
                     'new_identity_normalization_formats', 'all_native_branches', 'live_validation'])


def compile_formatted_brief(policy_paths, tool_api_path, *, evaluation_paths=()):
    documents = collect_sources([Path(p) for p in policy_paths])
    excluded = [read_source(Path(p)).sha256 for p in evaluation_paths]
    api = read_source(Path(tool_api_path))
    contract = BankContract(strict_json(api.text), api.source_id, api.sha256)
    return compile_formatted_documents(documents, contract, evaluation_source_hashes=excluded)
