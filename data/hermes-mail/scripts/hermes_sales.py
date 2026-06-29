#!/usr/bin/env python3
"""Hermes Sales: commercial CRM, exclusions, embassy/SECOM registry, imports, and operational learning.

This module is intentionally conservative:
- no automatic mass sending;
- exclusions are honored before campaign creation;
- imports are previewable before commit;
- all actions are logged append-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from reporting_utils import append_jsonl, count_jsonl, load_jsonl_records, make_id, normalize_text, slugify, load_sales_brand_profile, sales_brand_signature_lines

try:
    from hermes_memory import log_action as log_memory_action
except Exception:  # pragma: no cover - optional integration
    log_memory_action = None

ROOT = Path('/opt/data/hermes-mail')

EMBASSIES_JSONL = ROOT / 'sales-embassies.jsonl'
EXCLUSIONS_JSONL = ROOT / 'commercial-exclusions.jsonl'
IMPORTS_JSONL = ROOT / 'sales-imports.jsonl'
IMPORT_REVIEW_JSONL = ROOT / 'sales-import-review.jsonl'
COMPANIES_JSONL = ROOT / 'sales-companies.jsonl'
CONTACTS_JSONL = ROOT / 'sales-contacts.jsonl'
LEADS_JSONL = ROOT / 'sales-leads.jsonl'
CAMPAIGNS_JSONL = ROOT / 'sales-campaigns.jsonl'
EMAIL_EVENTS_JSONL = ROOT / 'sales-email-events.jsonl'
REPLY_EVENTS_JSONL = ROOT / 'sales-reply-events.jsonl'
HISTORY_JSONL = ROOT / 'sales-interactions.jsonl'
LEARNING_JSONL = ROOT / 'sales-learning.jsonl'
REPORT_QUEUE_JSONL = ROOT / 'sales-report-review-queue.jsonl'
STATUS_CATALOG_JSONL = ROOT / 'sales-status-catalog.jsonl'

STATUS_VALUES = [
    'novo',
    'importado',
    'excluído',
    'pendente de revisão',
    'prospectar',
    'e-mail enviado',
    'follow-up 1',
    'follow-up 2',
    'respondeu',
    'solicitou cotação',
    'solicitou especificação',
    'solicitou msds',
    'interessado',
    'sem interesse',
    'não respondeu',
    'cliente ativo',
]

DEFAULT_SIGNATURE_LINES = sales_brand_signature_lines()
SALES_BRAND_PROFILE = load_sales_brand_profile()

CSV_FIELD_ALIASES = {
    'company_name': ['empresa', 'company', 'company_name', 'name', 'nome', 'razao social', 'razão social', 'supplier', 'exporter', 'manufacturer'],
    'country': ['country', 'pais', 'país', 'origin_country', 'dest_country', 'destination_country', 'market'],
    'product': ['product', 'produto', 'item', 'description', 'mercadoria', 'goods'],
    'ncm': ['ncm', 'hs', 'hs_code', 'hscode', 'tariff', 'tariff_code'],
    'volume': ['volume', 'qty', 'quantity', 'quantidade', 'tons', 'tonnes', 'kg'],
    'value': ['value', 'valor', 'amount', 'price', 'fob', 'cif', 'usd'],
    'period': ['period', 'período', 'periodo', 'month', 'date', 'year', 'datas'],
    'notes': ['notes', 'observations', 'observações', 'observacoes', 'remarks', 'observação', 'obs'],
    'contact_name': ['contact', 'contato', 'contact_name', 'person', 'representative'],
    'email': ['email', 'e-mail', 'mail', 'commercial_email'],
    'website': ['website', 'site', 'url', 'homepage'],
    'phone': ['phone', 'telefone', 'tel'],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def ensure_storage() -> None:
    for path in [
        EMBASSIES_JSONL,
        EXCLUSIONS_JSONL,
        IMPORTS_JSONL,
        IMPORT_REVIEW_JSONL,
        COMPANIES_JSONL,
        CONTACTS_JSONL,
        LEADS_JSONL,
        CAMPAIGNS_JSONL,
        EMAIL_EVENTS_JSONL,
        REPLY_EVENTS_JSONL,
        HISTORY_JSONL,
        LEARNING_JSONL,
        REPORT_QUEUE_JSONL,
        STATUS_CATALOG_JSONL,
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


def _key(value: str | None) -> str:
    value = normalize_text(value or '')
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'[^a-z0-9]+', ' ', value.lower())
    return normalize_text(value)


def _pick(row: dict[str, Any], aliases: list[str]) -> str:
    if not row:
        return ''
    normalized = {_key(str(k)): v for k, v in row.items()}
    for alias in aliases:
        val = normalized.get(_key(alias))
        if val is not None and normalize_text(str(val)):
            return normalize_text(str(val))
    # fallback: partial match on header names
    for alias in aliases:
        alias_key = _key(alias)
        for header, val in row.items():
            if alias_key and alias_key in _key(str(header)) and normalize_text(str(val)):
                return normalize_text(str(val))
    return ''


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open('r', encoding='utf-8-sig', newline='') as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        except Exception:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        return [dict(row) for row in reader if any(normalize_text(str(v)) for v in row.values())]


def _col_to_index(ref: str) -> int:
    m = re.match(r'([A-Z]+)', ref.upper())
    if not m:
        return 0
    idx = 0
    for ch in m.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _read_xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read('xl/sharedStrings.xml')
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out: list[str] = []
    for si in root.iter():
        if not si.tag.endswith('si'):
            continue
        parts: list[str] = []
        for node in si.iter():
            if node.tag.endswith('t') and node.text:
                parts.append(node.text)
        out.append(''.join(parts))
    return out


def _first_sheet_target(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read('xl/workbook.xml'))
    rels = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
    rid_to_target: dict[str, str] = {}
    for rel in rels.iter():
        if rel.tag.endswith('Relationship'):
            rid_to_target[rel.attrib.get('Id', '')] = rel.attrib.get('Target', '')
    for sheet in workbook.iter():
        if sheet.tag.endswith('sheet'):
            rid = sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            target = rid_to_target.get(rid or '')
            if target:
                return target.lstrip('/')
    raise ValueError('xlsx workbook has no sheets')


def read_xlsx_rows(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        shared = _read_xlsx_shared_strings(zf)
        sheet_target = _first_sheet_target(zf)
        raw = zf.read(f'xl/{sheet_target}') if not sheet_target.startswith('xl/') else zf.read(sheet_target)
        root = ET.fromstring(raw)
        rows: list[list[str]] = []
        for row in root.iter():
            if not row.tag.endswith('row'):
                continue
            cells: dict[int, str] = {}
            for cell in row:
                if not cell.tag.endswith('c'):
                    continue
                ref = cell.attrib.get('r', '')
                idx = _col_to_index(ref)
                value = ''
                t = cell.attrib.get('t')
                if t == 's':
                    v = cell.find('.//{*}v')
                    if v is not None and v.text is not None:
                        try:
                            value = shared[int(v.text)]
                        except Exception:
                            value = v.text
                elif t == 'inlineStr':
                    texts = [n.text or '' for n in cell.iter() if n.tag.endswith('t')]
                    value = ''.join(texts)
                else:
                    v = cell.find('.//{*}v')
                    if v is not None and v.text is not None:
                        value = v.text
                cells[idx] = normalize_text(value)
            if cells:
                rows.append([cells.get(i, '') for i in range(1, max(cells) + 1)])
    if not rows:
        return []
    headers = [h or f'col_{i+1}' for i, h in enumerate(rows[0])]
    out: list[dict[str, Any]] = []
    for values in rows[1:]:
        row = {headers[i]: values[i] if i < len(values) else '' for i in range(len(headers))}
        if any(normalize_text(str(v)) for v in row.values()):
            out.append(row)
    return out


def read_report_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    suffix = path.suffix.lower()
    if suffix in {'.csv', '.txt'}:
        return read_csv_rows(path), []
    if suffix == '.xlsx':
        return read_xlsx_rows(path), []
    if suffix == '.xls':
        # No binary XLS parser is installed in this environment.
        # Treat as unsupported rather than silently corrupting data.
        return [], [f'unsupported XLS file without parser: {path}']
    if suffix == '.pdf':
        return [], [f'PDF OCR/text extraction not available in this environment: {path}']
    return [], [f'unsupported file type: {path.suffix}']


def normalize_company(row: dict[str, Any], source_file: Path, report_date: str) -> dict[str, Any]:
    company_name = _pick(row, CSV_FIELD_ALIASES['company_name'])
    country = _pick(row, CSV_FIELD_ALIASES['country'])
    product = _pick(row, CSV_FIELD_ALIASES['product'])
    ncm = _pick(row, CSV_FIELD_ALIASES['ncm'])
    volume = _pick(row, CSV_FIELD_ALIASES['volume'])
    value = _pick(row, CSV_FIELD_ALIASES['value'])
    period = _pick(row, CSV_FIELD_ALIASES['period'])
    notes = _pick(row, CSV_FIELD_ALIASES['notes'])
    contact_name = _pick(row, CSV_FIELD_ALIASES['contact_name'])
    email = _pick(row, CSV_FIELD_ALIASES['email'])
    website = _pick(row, CSV_FIELD_ALIASES['website'])
    phone = _pick(row, CSV_FIELD_ALIASES['phone'])
    return {
        'id': f"sales_company_{hashlib.sha1(f'{_key(company_name)}|{_key(country)}|{_key(product)}|{_key(ncm)}'.encode('utf-8')).hexdigest()[:12]}",
        'company_name': company_name,
        'country': country,
        'product': product,
        'ncm': ncm,
        'volume': volume,
        'value': value,
        'period': period,
        'notes': notes,
        'contact_name': contact_name,
        'email': email,
        'website': website,
        'phone': phone,
        'report_source_file': str(source_file),
        'report_source_date': report_date,
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'status': 'pendente de revisão' if not company_name else 'novo',
        'origin': 'report_import',
    }


def load_exclusions() -> list[dict[str, Any]]:
    return load_jsonl_records(EXCLUSIONS_JSONL)


def exclusion_match(company: dict[str, Any], exclusion: dict[str, Any]) -> bool:
    if not bool(exclusion.get('active', True)):
        return False
    company_name = _key(company.get('company_name'))
    company_country = _key(company.get('country'))
    exclusion_company = _key(exclusion.get('company'))
    exclusion_country = _key(exclusion.get('country'))
    if exclusion_company and exclusion_company in company_name:
        if not exclusion_country or exclusion_country == company_country:
            return True
    if exclusion_company == company_name and (not exclusion_country or exclusion_country == company_country):
        return True
    return False


def apply_exclusions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exclusions = load_exclusions()
    out: list[dict[str, Any]] = []
    for record in records:
        matched = next((ex for ex in exclusions if exclusion_match(record, ex)), None)
        enriched = dict(record)
        if matched:
            enriched['status'] = 'excluído'
            enriched['excluded'] = True
            enriched['exclusion_id'] = matched.get('id')
            enriched['exclusion_type'] = matched.get('exclusion_type')
            enriched['exclusion_reason'] = matched.get('reason')
        else:
            enriched['excluded'] = False
        out.append(enriched)
    return out


def dedupe_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    duplicates = 0
    for rec in records:
        key = (
            _key(rec.get('company_name')),
            _key(rec.get('country')),
            _key(rec.get('product')),
            _key(rec.get('ncm')),
        )
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(rec)
    return unique, duplicates


def schedule_followup(embassy: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    last_sent = embassy.get('last_sent_at')
    last_response = embassy.get('last_response_at')
    parsed_last_sent = parse_iso(last_sent) if last_sent else None
    parsed_last_response = parse_iso(last_response) if last_response else None
    status = 'novo'
    next_allowed = None
    cycle = embassy.get('campaign_cycle') or {}
    if parsed_last_response and parsed_last_sent and parsed_last_response >= parsed_last_sent:
        status = 'respondeu'
        next_allowed = parsed_last_response + timedelta(days=45)
    elif parsed_last_sent:
        delta = now - parsed_last_sent
        if delta.days < 7:
            status = 'e-mail enviado'
            next_allowed = parsed_last_sent + timedelta(days=7)
        elif delta.days < 14:
            status = 'follow-up 1'
            next_allowed = parsed_last_sent + timedelta(days=14)
        else:
            status = 'follow-up 2'
            next_allowed = parsed_last_sent + timedelta(days=45)
    return {
        'status': status,
        'next_send_allowed_at': iso(next_allowed) if next_allowed else '',
        'campaign_cycle_days_min': 45,
        'campaign_cycle_days_max': 60,
        'campaign_cycle': cycle,
    }


def iso(value: datetime | None) -> str:
    if not value:
        return ''
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def append_status_catalog() -> None:
    if STATUS_CATALOG_JSONL.exists() and count_jsonl(STATUS_CATALOG_JSONL) > 0:
        return
    for status in STATUS_VALUES:
        append_jsonl(STATUS_CATALOG_JSONL, {
            'id': f'status_{slugify(status)}',
            'status': status,
            'source': 'hermes_sales',
            'created_at': utc_now(),
        })


def upsert_review_queue(item: dict[str, Any]) -> None:
    append_jsonl(REPORT_QUEUE_JSONL, item)


def log_learning(action: str, *, reason: str, data_used: dict[str, Any] | list[dict[str, Any]] | None = None, result: dict[str, Any] | None = None, error: str = '', needs_human_review: bool = False, approval: str = 'pending', correction: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        'id': make_id('sales_learning'),
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'action': action,
        'reason': reason,
        'data_used': data_used or {},
        'result': result or {},
        'error': error,
        'needs_human_review': needs_human_review,
        'approval': approval,
        'correction': correction or {},
        'source': 'hermes_sales',
    }
    append_jsonl(LEARNING_JSONL, event)
    if log_memory_action:
        try:
            log_memory_action(
                module='hermes_sales',
                action=action,
                action_type=action,
                company=str((data_used or {}).get('company') or (data_used or {}).get('company_name') or (correction or {}).get('value') or ''),
                country=str((data_used or {}).get('country') or (correction or {}).get('country') or ''),
                product=str((data_used or {}).get('product') or ''),
                origin='hermes_sales',
                result=result or {},
                error=error,
                correction=correction or {},
                next_action_suggested=reason,
                summary=reason,
                learning=reason,
            )
        except Exception:
            pass
    return event


def load_embassies() -> list[dict[str, Any]]:
    return load_jsonl_records(EMBASSIES_JSONL)


def load_companies() -> list[dict[str, Any]]:
    return load_jsonl_records(COMPANIES_JSONL)


def latest_by_id(path: Path, item_id: str) -> dict[str, Any] | None:
    records = load_jsonl_records(path)
    for rec in reversed(records):
        if rec.get('id') == item_id:
            return rec
    return None


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_storage()
    append_status_catalog()
    errors: list[str] = []
    for path in [EMBASSIES_JSONL, EXCLUSIONS_JSONL, IMPORTS_JSONL, COMPANIES_JSONL, CONTACTS_JSONL, LEADS_JSONL, CAMPAIGNS_JSONL, EMAIL_EVENTS_JSONL, REPLY_EVENTS_JSONL, HISTORY_JSONL, LEARNING_JSONL]:
        if not path.exists():
            errors.append(f'missing file: {path}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'embassies_jsonl={EMBASSIES_JSONL}')
    print(f'exclusions_jsonl={EXCLUSIONS_JSONL}')
    print(f'imports_jsonl={IMPORTS_JSONL}')
    print(f'learning_jsonl={LEARNING_JSONL}')
    return 0


def cmd_add_embassy(args: argparse.Namespace) -> int:
    ensure_storage()
    record = {
        'id': make_id('embassy'),
        'country': normalize_text(args.country),
        'embassy': normalize_text(args.embassy),
        'commercial_sector': normalize_text(args.commercial_sector),
        'email_primary': normalize_text(args.email_primary),
        'email_alternatives': [normalize_text(x) for x in args.email_alternatives.split(',') if normalize_text(x)],
        'phone': normalize_text(args.phone),
        'contact_person': normalize_text(args.contact_person),
        'language': normalize_text(args.language) or 'en',
        'last_sent_at': normalize_text(args.last_sent_at),
        'last_response_at': normalize_text(args.last_response_at),
        'next_send_allowed_at': normalize_text(args.next_send_allowed_at),
        'status': normalize_text(args.status) or 'novo',
        'active': True,
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'source': 'hermes_sales',
        'type': 'embassy_secom',
    }
    record.update(schedule_followup(record))
    append_jsonl(EMBASSIES_JSONL, record)
    log_learning('add_embassy', reason='registered embassy/SECOM contact channel', data_used=record, result={'saved': True})
    print(json.dumps({'ok': True, 'embassy': record}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_add_exclusion(args: argparse.Namespace) -> int:
    ensure_storage()
    record = {
        'id': make_id('exclusion'),
        'company': normalize_text(args.company),
        'country': normalize_text(args.country),
        'reason': normalize_text(args.reason),
        'representative': normalize_text(args.representative),
        'exclusion_type': normalize_text(args.exclusion_type),
        'active': args.active,
        'notes': normalize_text(args.notes),
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'source': 'hermes_sales',
    }
    append_jsonl(EXCLUSIONS_JSONL, record)
    log_learning('add_exclusion', reason='do-not-contact rule stored', data_used=record, result={'saved': True})
    print(json.dumps({'ok': True, 'exclusion': record}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def preview_import(path: Path) -> dict[str, Any]:
    rows, warnings = read_report_rows(path)
    normalized = [normalize_company(row, path, utc_now()) for row in rows]
    normalized, duplicates = dedupe_records(normalized)
    normalized = apply_exclusions(normalized)
    excluded = sum(1 for row in normalized if row.get('excluded'))
    return {
        'id': make_id('import_preview'),
        'source_file': str(path),
        'source_date': utc_now(),
        'row_count': len(rows),
        'normalized_count': len(normalized),
        'duplicate_count': duplicates,
        'excluded_count': excluded,
        'warnings': warnings,
        'records': normalized,
    }


def commit_import(preview: dict[str, Any], *, campaign_name: str | None = None) -> dict[str, Any]:
    import_id = make_id('sales_import')
    imported_at = utc_now()
    committed: list[dict[str, Any]] = []
    for record in preview['records']:
        saved = dict(record)
        saved['import_id'] = import_id
        saved['imported_at'] = imported_at
        if saved.get('excluded'):
            saved['status'] = 'excluído'
        else:
            saved['status'] = 'importado'
        append_jsonl(COMPANIES_JSONL, saved)
        append_jsonl(LEADS_JSONL, {
            'id': make_id('lead'),
            'company_id': saved['id'],
            'company_name': saved.get('company_name', ''),
            'country': saved.get('country', ''),
            'product': saved.get('product', ''),
            'origin': 'report_import',
            'status_commercial': saved['status'],
            'lead_source': preview['source_file'],
            'import_id': import_id,
            'created_at': imported_at,
            'updated_at': imported_at,
        })
        append_jsonl(HISTORY_JSONL, {
            'id': make_id('interaction'),
            'company_id': saved['id'],
            'event_type': 'imported' if not saved.get('excluded') else 'excluded',
            'status': saved['status'],
            'reason': saved.get('exclusion_reason', ''),
            'source': 'hermes_sales',
            'created_at': imported_at,
            'updated_at': imported_at,
        })
        committed.append(saved)
    import_record = {
        'id': import_id,
        'source_file': preview['source_file'],
        'source_date': preview['source_date'],
        'imported_at': imported_at,
        'row_count': preview['row_count'],
        'normalized_count': preview['normalized_count'],
        'duplicate_count': preview['duplicate_count'],
        'excluded_count': preview['excluded_count'],
        'campaign_name': campaign_name or '',
        'status': 'committed',
        'source': 'hermes_sales',
    }
    append_jsonl(IMPORTS_JSONL, import_record)
    log_learning('import_report', reason='report committed after review', data_used={'source_file': preview['source_file'], 'warnings': preview['warnings']}, result=import_record, needs_human_review=bool(preview['warnings']))
    return {'import': import_record, 'records': committed}


def cmd_import_report(args: argparse.Namespace) -> int:
    ensure_storage()
    source = Path(args.file).expanduser()
    if not source.exists():
        print(json.dumps({'ok': False, 'error': f'file not found: {source}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    preview = preview_import(source)
    append_jsonl(IMPORT_REVIEW_JSONL, preview)
    append_jsonl(REPORT_QUEUE_JSONL, {
        'id': make_id('sales_review'),
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'status': 'pending',
        'source_file': preview['source_file'],
        'row_count': preview['row_count'],
        'normalized_count': preview['normalized_count'],
        'excluded_count': preview['excluded_count'],
        'duplicate_count': preview['duplicate_count'],
        'warnings': preview['warnings'],
        'source': 'hermes_sales',
    })
    if not args.commit:
        print(json.dumps({'ok': True, 'preview_only': True, 'review': preview}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    committed = commit_import(preview, campaign_name=args.campaign_name)
    print(json.dumps({'ok': True, 'review': preview, 'committed': committed}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_log_action(args: argparse.Namespace) -> int:
    ensure_storage()
    event = log_learning(
        args.action_name,
        reason=args.reason,
        data_used=json.loads(args.data_used) if args.data_used else {},
        result=json.loads(args.result) if args.result else {},
        error=args.error or '',
        needs_human_review=args.needs_human_review,
        approval=args.approval,
    )
    print(json.dumps({'ok': True, 'event': event}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_record_correction(args: argparse.Namespace) -> int:
    ensure_storage()
    correction = {
        'kind': args.kind,
        'target': args.target,
        'value': args.value,
        'notes': args.notes,
        'created_at': utc_now(),
    }
    if args.kind == 'do_not_send' and args.value:
        append_jsonl(EXCLUSIONS_JSONL, {
            'id': make_id('exclusion'),
            'company': normalize_text(args.value),
            'country': normalize_text(args.country),
            'reason': normalize_text(args.notes) or 'user correction',
            'representative': '',
            'exclusion_type': 'exclusao_temporaria',
            'active': True,
            'notes': normalize_text(args.notes),
            'created_at': utc_now(),
            'updated_at': utc_now(),
            'source': 'correction',
        })
    log_learning('record_correction', reason='user correction converted into operational rule', data_used=correction, result={'applied': True}, correction=correction)
    print(json.dumps({'ok': True, 'correction': correction}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def next_followups_for_embassies() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for embassy in load_embassies():
        scheduled = schedule_followup(embassy)
        results.append({
            'id': embassy.get('id'),
            'country': embassy.get('country', ''),
            'embassy': embassy.get('embassy', ''),
            'status': scheduled['status'],
            'next_send_allowed_at': scheduled['next_send_allowed_at'],
            'last_sent_at': embassy.get('last_sent_at', ''),
            'last_response_at': embassy.get('last_response_at', ''),
        })
    return results


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_storage()
    append_status_catalog()
    embassies = load_embassies()
    exclusions = load_exclusions()
    companies = load_companies()
    leads = load_jsonl_records(LEADS_JSONL)
    imported = sum(1 for c in companies if c.get('status') == 'importado')
    excluded = sum(1 for c in companies if c.get('status') == 'excluído')
    print(json.dumps({
        'ok': True,
        'embassies': len(embassies),
        'exclusions': len(exclusions),
        'companies': len(companies),
        'imports': count_jsonl(IMPORTS_JSONL),
        'leads': len(leads),
        'imported_companies': imported,
        'excluded_companies': excluded,
        'learning_events': count_jsonl(LEARNING_JSONL),
        'pending_review_items': count_jsonl(REPORT_QUEUE_JSONL),
        'next_followups': next_followups_for_embassies()[:10],
        'status_catalog': STATUS_VALUES,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_next_followups(_: argparse.Namespace) -> int:
    ensure_storage()
    print(json.dumps({'ok': True, 'items': next_followups_for_embassies()}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Sales CRM and commercial intelligence')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate sales stores')
    p_validate.set_defaults(func=cmd_validate)

    p_add_embassy = sub.add_parser('add-embassy', help='Register an embassy/SECOM contact')
    p_add_embassy.add_argument('--country', required=True)
    p_add_embassy.add_argument('--embassy', required=True)
    p_add_embassy.add_argument('--commercial-sector', default='')
    p_add_embassy.add_argument('--email-primary', default='')
    p_add_embassy.add_argument('--email-alternatives', default='')
    p_add_embassy.add_argument('--phone', default='')
    p_add_embassy.add_argument('--contact-person', default='')
    p_add_embassy.add_argument('--language', default='en')
    p_add_embassy.add_argument('--last-sent-at', default='')
    p_add_embassy.add_argument('--last-response-at', default='')
    p_add_embassy.add_argument('--next-send-allowed-at', default='')
    p_add_embassy.add_argument('--status', default='novo')
    p_add_embassy.set_defaults(func=cmd_add_embassy)

    p_add_exclusion = sub.add_parser('add-exclusion', help='Register a commercial exclusion')
    p_add_exclusion.add_argument('--company', required=True)
    p_add_exclusion.add_argument('--country', default='')
    p_add_exclusion.add_argument('--reason', default='')
    p_add_exclusion.add_argument('--representative', default='')
    p_add_exclusion.add_argument('--exclusion-type', default='exclusao_temporaria')
    p_add_exclusion.add_argument('--active', action='store_true', default=True)
    p_add_exclusion.add_argument('--notes', default='')
    p_add_exclusion.set_defaults(func=cmd_add_exclusion)

    p_import = sub.add_parser('import-report', help='Preview or commit a commercial report import')
    p_import.add_argument('file')
    p_import.add_argument('--commit', action='store_true')
    p_import.add_argument('--campaign-name', default='')
    p_import.set_defaults(func=cmd_import_report)

    p_log = sub.add_parser('log-action', help='Append a structured operational learning event')
    p_log.add_argument('--action-name', required=True)
    p_log.add_argument('--reason', required=True)
    p_log.add_argument('--data-used', default='{}')
    p_log.add_argument('--result', default='{}')
    p_log.add_argument('--error', default='')
    p_log.add_argument('--needs-human-review', action='store_true')
    p_log.add_argument('--approval', default='pending')
    p_log.set_defaults(func=cmd_log_action)

    p_corr = sub.add_parser('record-correction', help='Convert a user correction into an operational rule')
    p_corr.add_argument('--kind', required=True, choices=['do_not_send', 'wrong_contact', 'template_priority', 'country_language', 'embassy_delay'])
    p_corr.add_argument('--target', default='')
    p_corr.add_argument('--value', default='')
    p_corr.add_argument('--country', default='')
    p_corr.add_argument('--notes', default='')
    p_corr.set_defaults(func=cmd_record_correction)

    p_next = sub.add_parser('next-followups', help='Show embassy/SECOM follow-up schedule')
    p_next.set_defaults(func=cmd_next_followups)

    p_stats = sub.add_parser('stats', help='Show sales stats')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
