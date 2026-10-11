"""Bounded local case-document projection. No policy inference, translation or HTTP.

Public interface: read_case_folder(path, *, limits=None) -> JSON-compatible dict.
Original hashes and relative locators identify evidence. `complete` means no
extraction error, not semantic completeness or visual/OCR fidelity.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

VERSION = 'case-projection-1'
DEFAULT_LIMITS = dict(files=64, depth=12, file_bytes=8_000_000, total_bytes=32_000_000,
                      archive_bytes=32_000_000, archive_parts=2000, pages=200,
                      segments=40000, text_chars=2_000_000, output_bytes=24_000_000,
                      timeout_seconds=60)
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


class CaseReaderError(ValueError):
    pass


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _limits(supplied):
    values = dict(DEFAULT_LIMITS)
    if supplied is not None:
        if not isinstance(supplied, dict) or set(supplied) - set(values):
            raise CaseReaderError('INVALID_LIMITS')
        for key, value in supplied.items():
            if type(value) is not int or not 1 <= value <= DEFAULT_LIMITS[key]:
                raise CaseReaderError('INVALID_LIMITS')
            values[key] = value
    return values


def _xml(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise CaseReaderError('XML_ENTITIES_UNSUPPORTED')
    return ET.fromstring(raw)


def _archive(raw, limits):
    archive = zipfile.ZipFile(io.BytesIO(raw))
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(entries) > limits['archive_parts'] or len(set(names)) != len(names):
        archive.close()
        raise CaseReaderError('ARCHIVE_PART_LIMIT_OR_DUPLICATE')
    if sum(entry.file_size for entry in entries) > limits['archive_bytes']:
        archive.close()
        raise CaseReaderError('ARCHIVE_EXPANDED_LIMIT')
    if any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or ':' in name for name in names):
        archive.close()
        raise CaseReaderError('ARCHIVE_UNSAFE_PATH')
    if any(entry.flag_bits & 1 for entry in entries):
        archive.close()
        raise CaseReaderError('ENCRYPTED_ARCHIVE_UNSUPPORTED')
    return archive


class _Collector:
    def __init__(self, document, limits):
        self.document, self.limits, self.characters = document, limits, 0

    def diagnostic(self, code, severity='warning', locator=None, detail=None):
        item = dict(code=code, severity=severity, path=self.document['path'])
        if locator is not None:
            item['locator'] = locator
        if detail is not None:
            item['detail'] = detail
        if item not in self.document['diagnostics']:
            self.document['diagnostics'].append(item)

    def add(self, locator, text, **metadata):
        if not isinstance(text, str):
            raise CaseReaderError('NON_TEXT_SEGMENT')
        if not text.strip():
            return
        self.characters += len(text)
        if self.characters > self.limits['text_chars'] or len(self.document['segments']) >= self.limits['segments']:
            raise CaseReaderError('EXTRACTED_CONTENT_LIMIT')
        self.document['segments'].append(dict(locator=locator, text=text, metadata=metadata))


def _text(raw, kind, collector):
    encoding = 'utf-8-sig'
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    text = raw.decode(encoding)
    if '\x00' in text:
        raise CaseReaderError('TEXT_NUL')
    collector.document['encoding'] = encoding
    if kind == '.json':
        parsed = json.loads(text)
        if len(text) > 64000:
            # Large machine-readable attachments otherwise spend most of the
            # projection on repeated per-line provenance. Preserve every value
            # in one valid JSON segment; the original bytes remain hash-pinned.
            collector.add({'json': '$'}, json.dumps(parsed, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False),
                          representation='compact_json', source_executed=False)
            return
    for index, line in enumerate(text.splitlines(), 1):
        collector.add({'line': index}, line)
    if kind in {'.csv', '.tsv'}:
        for row_index, row in enumerate(csv.reader(io.StringIO(text), delimiter='\t' if kind == '.tsv' else ',', strict=True), 1):
            for column, value in enumerate(row, 1):
                collector.add({'table': 1, 'row': row_index, 'column': column}, value, alternate_view=True)


def _docx(raw, collector):
    with _archive(raw, collector.limits) as archive:
        names = archive.namelist()
        if 'word/document.xml' not in names:
            raise CaseReaderError('DOCX_MAIN_PART_MISSING')
        parts = [name for name in names if name == 'word/document.xml' or
                 re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml', name)]
        parts.sort(key=lambda name: (name != 'word/document.xml', name))
        for name in ('word/numbering.xml', 'word/styles.xml'):
            if name in names:
                collector.document.setdefault('format_definitions', {})[name] = archive.read(name).decode('utf-8-sig')
        for part in parts:
            root = _xml(archive.read(part))
            if list(root.iter(W + 'drawing')) or list(root.iter(W + 'pict')):
                collector.diagnostic('DOCX_IMAGES_NOT_OCR', locator={'part': part})
            if list(root.iter(W + 'altChunk')):
                collector.diagnostic('DOCX_EMBEDDED_CONTENT_UNREAD', 'error', {'part': part})
            if list(root.iter(W + 'ins')) or list(root.iter(W + 'del')):
                collector.diagnostic('DOCX_REVISIONS_REQUIRE_AUTHORITY_REVIEW', 'error', {'part': part})
            paragraph_number = 0
            table_number = 0

            def visit(node, context):
                nonlocal paragraph_number, table_number
                if node.tag == W + 'p':
                    paragraph_number += 1
                    tokens, fields, deleted = [], [], []
                    for child in node.iter():
                        if child.tag in {W + 't', W + 'delText'}:
                            tokens.append(child.text or '')
                            if child.tag == W + 'delText':
                                deleted.append(child.text or '')
                        elif child.tag == W + 'tab':
                            tokens.append('\t')
                        elif child.tag in {W + 'br', W + 'cr'}:
                            tokens.append('\n')
                        elif child.tag == W + 'instrText':
                            fields.append(child.text or '')
                    props = node.find(W + 'pPr')
                    metadata = {}
                    if props is not None:
                        metadata['paragraph_properties_xml'] = ET.tostring(props, encoding='unicode')
                        if props.find(W + 'numPr') is not None:
                            collector.diagnostic('DOCX_NUMBERING_METADATA_RETAINED', 'info', {'part': part})
                    if fields:
                        metadata['field_instructions_not_executed'] = fields
                    if deleted:
                        metadata['deleted_text_retained_for_review'] = deleted
                    collector.add(dict(part=part, paragraph=paragraph_number, **context), ''.join(tokens), **metadata)
                    return
                if node.tag == W + 'tbl':
                    table_number += 1
                    current = table_number
                    collector.document.setdefault('tables', []).append(dict(
                        part=part, table=current,
                        row_cell_counts=[len(row.findall(W + 'tc')) for row in node.findall(W + 'tr')]))
                    for ri, row in enumerate(node.findall(W + 'tr'), 1):
                        for ci, cell in enumerate(row.findall(W + 'tc'), 1):
                            props = cell.find(W + 'tcPr')
                            cell_context = dict(table=current, row=ri, cell=ci)
                            if props is not None:
                                span = props.find(W + 'gridSpan')
                                merge = props.find(W + 'vMerge')
                                if span is not None:
                                    cell_context['column_span'] = span.get(W + 'val')
                                if merge is not None:
                                    cell_context['vertical_merge'] = merge.get(W + 'val', 'continue')
                            for child in cell:
                                visit(child, cell_context)
                    return
                for child in node:
                    visit(child, context)

            visit(root, {})
        if any(name.startswith('word/embeddings/') for name in names):
            collector.diagnostic('DOCX_EMBEDDED_OBJECTS_UNREAD', 'error')
        collector.diagnostic('DOCX_VISUAL_LAYOUT_NOT_VERIFIED', 'info')


def _xlsx(raw, collector):
    try:
        from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format, is_datetime, is_timedelta_format
        from openpyxl.utils.datetime import from_excel, CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
    except ImportError:
        raise CaseReaderError('DEPENDENCY_MISSING_OPENPYXL') from None
    with _archive(raw, collector.limits) as archive:
        names = archive.namelist()
        workbook = _xml(archive.read('xl/workbook.xml'))
        relations = {row.attrib['Id']: row.attrib for row in _xml(archive.read('xl/_rels/workbook.xml.rels'))}
        props = workbook.find(S + 'workbookPr')
        epoch1904 = props is not None and props.get('date1904') in {'1', 'true'}
        epoch = CALENDAR_MAC_1904 if epoch1904 else CALENDAR_WINDOWS_1900
        collector.document['date_system'] = '1904' if epoch1904 else '1900'
        shared = []
        if 'xl/sharedStrings.xml' in names:
            shared = [''.join(t.text or '' for t in item.iter(S + 't')) for item in _xml(archive.read('xl/sharedStrings.xml')).findall(S + 'si')]
        formats, styles = dict(BUILTIN_FORMATS), [0]
        if 'xl/styles.xml' in names:
            definitions = _xml(archive.read('xl/styles.xml'))
            formats.update({int(n.attrib['numFmtId']): n.attrib['formatCode'] for n in definitions.findall(S + 'numFmts/' + S + 'numFmt')})
            xfs = definitions.findall(S + 'cellXfs/' + S + 'xf')
            if xfs:
                styles = [int(n.get('numFmtId', '0')) for n in xfs]
        collector.document['number_formats'] = {str(k): v for k, v in formats.items()}
        sheets = []
        for sheet in workbook.findall(S + 'sheets/' + S + 'sheet'):
            relation = relations[sheet.attrib[R + 'id']]
            if relation.get('TargetMode') == 'External':
                raise CaseReaderError('EXTERNAL_WORKSHEET_UNSUPPORTED')
            target = relation['Target']
            part = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            if '..' in PurePosixPath(part).parts or part not in names:
                raise CaseReaderError('WORKSHEET_TARGET_INVALID')
            page = _xml(archive.read(part))
            merges = [n.attrib['ref'] for n in page.findall(S + 'mergeCells/' + S + 'mergeCell')]
            columns = [dict(n.attrib) for n in page.findall(S + 'cols/' + S + 'col')]
            sheets.append(dict(name=sheet.attrib['name'], state=sheet.get('state', 'visible'), part=part,
                               merged_ranges=merges, columns=columns))
            for row in page.findall(S + 'sheetData/' + S + 'row'):
                for cell in row.findall(S + 'c'):
                    locator = dict(sheet=sheet.attrib['name'], cell=cell.attrib['r'], part=part)
                    raw_value = cell.findtext(S + 'v')
                    value = raw_value
                    typ = cell.get('t', 'n')
                    formula = cell.find(S + 'f')
                    style = int(cell.get('s', '0'))
                    if not 0 <= style < len(styles):
                        raise CaseReaderError('XLSX_STYLE_INDEX_INVALID')
                    fmt_id = styles[style]
                    number_format = formats.get(fmt_id)
                    if number_format is None:
                        collector.diagnostic('XLSX_UNKNOWN_NUMBER_FORMAT', 'error', locator)
                    metadata = dict(cell_type=typ, raw_value=raw_value, style_index=style,
                                    number_format_id=fmt_id, number_format=number_format,
                                    sheet_state=sheet.get('state', 'visible'), row_hidden=row.get('hidden', '0'),
                                    formula=None, cached_value_present=raw_value is not None)
                    if typ == 's' and raw_value is not None:
                        index = int(raw_value)
                        if not 0 <= index < len(shared):
                            raise CaseReaderError('XLSX_SHARED_STRING_INDEX_INVALID')
                        value = shared[index]
                    elif typ == 'inlineStr':
                        value = ''.join(n.text or '' for n in cell.iter(S + 't'))
                    elif typ == 'd' and raw_value is not None:
                        metadata['iso_date_from_explicit_cell_type'] = raw_value
                    elif typ == 'n' and raw_value is not None and number_format:
                        if is_timedelta_format(number_format):
                            duration = from_excel(float(raw_value), epoch, timedelta=True)
                            metadata['duration_seconds_from_explicit_number_format'] = duration.total_seconds()
                        elif is_date_format(number_format):
                            serial = float(raw_value)
                            if not epoch1904 and 60 <= serial < 61:
                                collector.diagnostic('XLSX_FICTITIOUS_1900_LEAP_DAY', 'error', locator)
                            else:
                                interpreted = from_excel(serial, epoch)
                                temporal_kind = is_datetime(number_format)
                                if isinstance(interpreted, dt.time) and temporal_kind in {'date', 'datetime'}:
                                    if not epoch1904:
                                        collector.diagnostic('XLSX_1900_ZERO_DAY_REQUIRES_REVIEW', 'error', locator)
                                    interpreted = dt.datetime.combine(epoch.date(), interpreted)
                                if temporal_kind == 'date' and isinstance(interpreted, dt.datetime):
                                    interpreted = interpreted.date()
                                metadata['iso_date_from_explicit_number_format'] = interpreted.isoformat()
                        elif re.fullmatch('0+', number_format) and re.fullmatch(r'\d+', raw_value):
                            metadata['zero_padded_display_from_explicit_number_format'] = raw_value.zfill(len(number_format))
                    if formula is not None:
                        metadata['formula'] = formula.text or ''
                        metadata['formula_attributes'] = dict(formula.attrib)
                        collector.diagnostic('XLSX_FORMULA_NOT_RECALCULATED', 'warning', locator)
                        if raw_value is None:
                            collector.diagnostic('XLSX_FORMULA_CACHE_MISSING', 'error', locator)
                            value = '=' + (formula.text or '[shared formula reference]')
                    if value is not None:
                        collector.add(locator, value, **metadata)
            if list(page.iter(S + 'drawing')):
                collector.diagnostic('XLSX_DRAWINGS_NOT_EXTRACTED', 'warning', {'sheet': sheet.attrib['name']})
        collector.document['sheets'] = sheets
        if any(name.startswith('xl/externalLinks/') for name in names):
            collector.diagnostic('XLSX_EXTERNAL_LINKS_NOT_RESOLVED', 'error')
        if any(name.endswith('vbaProject.bin') for name in names):
            collector.diagnostic('XLSX_MACROS_NOT_EXECUTED', 'warning')
        collector.diagnostic('XLSX_RAW_VALUES_AND_FORMATS_PRESERVED_NO_LOCALE_GUESS', 'info')


def _pdf(raw, collector):
    try:
        import pdfplumber
    except ImportError:
        raise CaseReaderError('DEPENDENCY_MISSING_PDFPLUMBER') from None
    with pdfplumber.open(io.BytesIO(raw)) as document:
        if len(document.pages) > collector.limits['pages']:
            raise CaseReaderError('PDF_PAGE_LIMIT')
        collector.document['page_count'] = len(document.pages)
        for number, page in enumerate(document.pages, 1):
            text = page.extract_text() or ''
            locator = dict(page=number, bbox=list(page.bbox))
            if not text.strip():
                collector.diagnostic('PDF_PAGE_REQUIRES_OCR_OR_IS_EMPTY', 'error', locator)
            collector.add(locator, text)
            if page.images:
                collector.diagnostic('PDF_IMAGES_NOT_OCR', 'warning', locator)
            for ti, table in enumerate(page.find_tables(), 1):
                for ri, row in enumerate(table.extract(), 1):
                    for ci, text in enumerate(row, 1):
                        if text:
                            collector.add(dict(page=number, table=ti, row=ri, cell=ci, bbox=list(table.bbox)),
                                          text, alternate_view=True, table_detection='geometric_lines')
    collector.diagnostic('PDF_TEXT_LAYER_ORDER_AND_TABLE_GEOMETRY_REQUIRE_REVIEW', 'warning')


def _document(path, relative, raw, limits):
    document = dict(path=relative, source_id='src_' + _sha(raw), sha256=_sha(raw), bytes=len(raw),
                    format=path.suffix.lower(), status='extracted', segments=[], diagnostics=[])
    collector = _Collector(document, limits)
    try:
        kind = document['format']
        if kind in {'.md', '.txt', '.json', '.csv', '.tsv', '.js', '.ts', '.py',
                    '.toml', '.yaml', '.yml', '.html', '.xml'}:
            _text(raw, kind, collector)
            if kind in {'.js', '.ts', '.py', '.toml', '.yaml', '.yml', '.html', '.xml'}:
                collector.diagnostic('LITERAL_SOURCE_TEXT_NOT_EXECUTED', 'info')
        elif kind == '.docx':
            _docx(raw, collector)
        elif kind == '.xlsx':
            _xlsx(raw, collector)
        elif kind == '.pdf':
            _pdf(raw, collector)
        else:
            collector.diagnostic('LEGACY_OFFICE_UNSUPPORTED' if kind in {'.doc', '.xls'} else 'UNSUPPORTED_FORMAT', 'error')
            document['status'] = 'unsupported'
    except Exception as error:
        code = str(error) if isinstance(error, CaseReaderError) else 'PARSE_FAILED_' + type(error).__name__
        collector.diagnostic(code, 'error')
        document['status'] = 'extraction_failed'
    if not document['segments'] and document['status'] == 'extracted':
        collector.diagnostic('EMPTY_DOCUMENT', 'error')
        document['status'] = 'empty'
    elif any(d['severity'] == 'error' for d in document['diagnostics']) and document['status'] == 'extracted':
        document['status'] = 'partial'
    document['text_sha256'] = _sha(_bytes(document['segments']))
    return document


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _project(root, limits):
    supplied = Path(root)
    if _linked(supplied) or not supplied.is_dir():
        raise CaseReaderError('CASE_FOLDER_NOT_REGULAR_DIRECTORY')
    root = supplied.resolve()
    entries, diagnostics = [], []

    def walk(folder, depth):
        if depth > limits['depth']:
            raise CaseReaderError('DIRECTORY_DEPTH_LIMIT')
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            relative = path.relative_to(root).as_posix()
            if _linked(path) or not path.resolve().is_relative_to(root):
                diagnostics.append(dict(code='LINK_SOURCE_REFUSED', severity='error', path=relative))
                continue
            if path.name.startswith('.') or path.name.lower() in {'credentials.json', 'secrets.json', 'token.json', 'env.sh', 'env.example'}:
                diagnostics.append(dict(code='CONFIG_OR_HIDDEN_SOURCE_REFUSED', severity='error', path=relative))
                continue
            if path.is_dir():
                walk(path, depth + 1)
            elif path.is_file():
                entries.append((relative, path))
                if len(entries) > limits['files']:
                    raise CaseReaderError('CASE_FILE_COUNT_LIMIT')
            else:
                diagnostics.append(dict(code='NON_FILE_SOURCE_REFUSED', severity='error', path=relative))

    walk(root, 0)
    entries.sort(key=lambda item: item[0])
    manifest, documents, total = [], [], 0
    for relative, path in entries:
        size = path.stat().st_size
        if size > limits['file_bytes'] or total + size > limits['total_bytes']:
            diagnostics.append(dict(code='SOURCE_BYTE_LIMIT', severity='error', path=relative))
            manifest.append(dict(path=relative, bytes=size, sha256=None, status='not_read_limit'))
            continue
        raw = path.read_bytes()
        if len(raw) != size or len(raw) > limits['file_bytes']:
            raise CaseReaderError('SOURCE_CHANGED_DURING_READ')
        total += len(raw)
        manifest.append(dict(path=relative, bytes=len(raw), sha256=_sha(raw), status='read'))
        document = _document(path, relative, raw, limits)
        documents.append(document)
        diagnostics.extend(document['diagnostics'])
    if not entries:
        diagnostics.append(dict(code='EMPTY_CASE_FOLDER', severity='error', path='.'))
    rendered = []
    for document in documents:
        context = {key: document[key] for key in ('path', 'format', 'encoding', 'date_system', 'sheets', 'tables', 'format_definitions') if key in document}
        rendered.append('DOCUMENT_METADATA ' + _bytes(context).decode('utf-8'))
        for segment in document['segments']:
            header = dict(source=document['path'], sha256=document['sha256'], locator=segment['locator'])
            rendered.append('SOURCE ' + _bytes(header).decode('utf-8') + '\n' + segment['text'])
            # Dates, formats, formulas and revision context remain available to
            # the model projection without replacing original raw text.
            if segment['metadata']:
                rendered.append('SOURCE_METADATA ' + _bytes(segment['metadata']).decode('utf-8'))
    text = '\n\n'.join(rendered)
    if len(text) > limits['text_chars']:
        raise CaseReaderError('CASE_TEXT_LIMIT')
    result = dict(kind=VERSION, text=text, documents=documents, diagnostics=diagnostics,
                  manifest=manifest, manifest_sha256=_sha(_bytes(manifest)),
                  complete=not any(d['severity'] == 'error' for d in diagnostics),
                  requires_review=any(d['severity'] in {'warning', 'error'} for d in diagnostics),
                  semantic_policy_verified=False, network_calls=0, model_calls=0)
    result['projection_sha256'] = _sha(_bytes(result))
    if len(_bytes(result)) > limits['output_bytes']:
        raise CaseReaderError('PROJECTION_BYTES_LIMIT')
    return result


def read_case_folder(case_folder, *, limits=None):
    """Extract in a separate process with a wall-clock timeout and output cap.

Fatal inventory/timeout errors raise CaseReaderError; per-document errors remain
visible in diagnostics with complete=False. Limits can only tighten defaults.
"""
    bounds = _limits(limits)
    with tempfile.TemporaryDirectory(prefix='case-reader-') as directory:
        directory = Path(directory)
        request, output = directory / 'request.json', directory / 'projection.json'
        request.write_bytes(_bytes(dict(root=str(Path(case_folder).absolute()), limits=bounds)))
        environment = {'PYTHONDONTWRITEBYTECODE': '1'}
        if 'SYSTEMROOT' in os.environ:
            environment['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        try:
            process = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                      '--worker', str(request), str(output)], cwd=directory,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=environment,
                                     timeout=bounds['timeout_seconds'], check=False)
        except subprocess.TimeoutExpired:
            raise CaseReaderError('CASE_READER_TIMEOUT') from None
        if process.returncode != 0 or not output.is_file():
            raise CaseReaderError('CASE_READER_WORKER_FAILED')
        if output.stat().st_size > bounds['output_bytes']:
            raise CaseReaderError('PROJECTION_BYTES_LIMIT')
        result = json.loads(output.read_bytes())
        if 'fatal_error' in result:
            raise CaseReaderError(result['fatal_error'])
        return result


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    try:
        request = json.loads(Path(sys.argv[2]).read_bytes())
        result = _project(request['root'], _limits(request['limits']))
    except Exception as error:
        result = {'fatal_error': str(error) if isinstance(error, CaseReaderError) else 'CASE_READER_FAILED_' + type(error).__name__}
    Path(sys.argv[3]).write_bytes(_bytes(result))
