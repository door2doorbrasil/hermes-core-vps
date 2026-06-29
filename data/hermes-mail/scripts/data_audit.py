#!/usr/bin/env python3
"""Hermes Mail data integrity audit.

Checks the provisional filesystem-backed store under /opt/data/hermes-mail.
It validates JSON/JSONL structure, checks duplicates, verifies cross-file
references, and can emit a JSON report.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
STATE_DIR = ROOT / 'state'
SCHEMAS_DIR = ROOT / 'schemas'
LOGS_DIR = ROOT / 'logs'
REPORT_PATH = LOGS_DIR / 'data-audit-report.json'

JSON_FILES = [
    STATE_DIR / 'settings.json',
    STATE_DIR / 'cursor.json',
    STATE_DIR / 'ingest-state.json',
]

SCHEMA_FILES = [
    SCHEMAS_DIR / 'fornecedores.schema.json',
    SCHEMAS_DIR / 'contatos.schema.json',
    SCHEMAS_DIR / 'produtos.schema.json',
    SCHEMAS_DIR / 'emails.schema.json',
    SCHEMAS_DIR / 'cotacoes.schema.json',
    SCHEMAS_DIR / 'price-history.schema.json',
    SCHEMAS_DIR / 'anexos.schema.json',
    SCHEMAS_DIR / 'purchase-governance.schema.json',
]

JSONL_FILES = [
    ROOT / 'fornecedores.jsonl',
    ROOT / 'contatos.jsonl',
    ROOT / 'produtos.jsonl',
    ROOT / 'emails.jsonl',
    ROOT / 'cotacoes.jsonl',
    ROOT / 'price-history.jsonl',
    ROOT / 'anexos.jsonl',
    ROOT / 'supplier-reply-analysis.jsonl',
    ROOT / 'client-quotes.jsonl',
    ROOT / 'comparison-reports.jsonl',
    ROOT / 'sourcing-projects.jsonl',
    ROOT / 'manufacturer-research.jsonl',
    ROOT / 'manufacturer-qualification.jsonl',
    ROOT / 'supplier-performance.jsonl',
    ROOT / 'manufacturer-discovery.jsonl',
    ROOT / 'open-webui-requests.jsonl',
    ROOT / 'open-webui-actions.jsonl',
    ROOT / 'product-intelligence.jsonl',
    ROOT / 'product-categories.jsonl',
    ROOT / 'product-compliance-rules.jsonl',
    ROOT / 'product-sourcing-sources.jsonl',
    ROOT / 'procurement-knowledge-base.jsonl',
    ROOT / 'rfq-drafts.jsonl',
    ROOT / 'manual-review-queue.jsonl',
    ROOT / 'sales-embassies.jsonl',
    ROOT / 'commercial-exclusions.jsonl',
    ROOT / 'sales-imports.jsonl',
    ROOT / 'sales-import-review.jsonl',
    ROOT / 'sales-companies.jsonl',
    ROOT / 'sales-contacts.jsonl',
    ROOT / 'sales-leads.jsonl',
    ROOT / 'sales-campaigns.jsonl',
    ROOT / 'sales-email-events.jsonl',
    ROOT / 'sales-reply-events.jsonl',
    ROOT / 'sales-interactions.jsonl',
    ROOT / 'sales-learning.jsonl',
    ROOT / 'sales-report-review-queue.jsonl',
    ROOT / 'sales-status-catalog.jsonl',
    ROOT / 'purchase-companies.jsonl',
    ROOT / 'purchase-products.jsonl',
    ROOT / 'purchase-contacts.jsonl',
    ROOT / 'rfq-batches.jsonl',
    ROOT / 'rfq-batch-suppliers.jsonl',
    ROOT / 'supplier-quotes.jsonl',
    ROOT / 'hermes-decision-recommendations.jsonl',
    ROOT / 'freight-market-intelligence.jsonl',
    ROOT / 'logistics-news-alerts.jsonl',
    ROOT / 'purchase-timing-analysis.jsonl',
    ROOT / 'user-decision-logs.jsonl',
    ROOT / 'state' / 'hermes_memory.jsonl',
    ROOT / 'data' / 'hermes_diary.jsonl',
    ROOT / 'data' / 'user_profiles.jsonl',
    ROOT / 'data' / 'user_credentials.jsonl',
    ROOT / 'data' / 'identity-audit.jsonl',
    ROOT / 'data' / 'identity-diary.jsonl',
]

REFERENCE_FIELDS = {
    'cotacoes.jsonl': {
        'supplier_id': 'fornecedores.jsonl',
        'contact_id': 'contatos.jsonl',
        'email_id': 'emails.jsonl',
    },
    'price-history.jsonl': {
        'product_id': 'produtos.jsonl',
        'supplier_id': 'fornecedores.jsonl',
        'quote_id': 'cotacoes.jsonl',
    },
    'anexos.jsonl': {
        'email_id': 'emails.jsonl',
    },
    'manufacturer-discovery.jsonl': {
        'project_id': 'sourcing-projects.jsonl',
        'product_intelligence_id': 'product-intelligence.jsonl',
    },
    'open-webui-requests.jsonl': {
        'project_id': 'sourcing-projects.jsonl',
        'product_intelligence_id': 'product-intelligence.jsonl',
    },
    'open-webui-actions.jsonl': {
        'request_id': 'open-webui-requests.jsonl',
        'project_id': 'sourcing-projects.jsonl',
        'product_intelligence_id': 'product-intelligence.jsonl',
        'candidate_id': 'manufacturer-qualification.jsonl',
    },
    'product-intelligence.jsonl': {
        'category_id': 'product-categories.jsonl',
        'compliance_rule_id': 'product-compliance-rules.jsonl',
        'primary_source_profile_id': 'product-sourcing-sources.jsonl',
        'knowledge_base_id': 'procurement-knowledge-base.jsonl',
    },
    'product-compliance-rules.jsonl': {
        'category_id': 'product-categories.jsonl',
    },
    'product-sourcing-sources.jsonl': {
        'category_id': 'product-categories.jsonl',
    },
    'procurement-knowledge-base.jsonl': {
        'category_id': 'product-categories.jsonl',
        'compliance_rule_id': 'product-compliance-rules.jsonl',
        'category_profile_id': 'product-categories.jsonl',
        'product_intelligence_id': 'product-intelligence.jsonl',
        'primary_source_profile_id': 'product-sourcing-sources.jsonl',
    },
    'rfq-drafts.jsonl': {
        'project_id': 'sourcing-projects.jsonl',
        'product_intelligence_id': 'product-intelligence.jsonl',
        'manufacturer_id': 'manufacturer-research.jsonl',
    },
}

PATH_FIELDS = ['raw_path', 'body_html_path', 'storage_path', 'extracted_text_path', 'quote_pdf_path', 'report_pdf_path']


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def count_jsonl(path: Path) -> int:
    count = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    count += 1
    return count


def validate_structure() -> list[str]:
    errors: list[str] = []
    for path in [ROOT, STATE_DIR, SCHEMAS_DIR, LOGS_DIR]:
        if not path.exists():
            errors.append(f'missing directory: {path}')
        elif not path.is_dir():
            errors.append(f'not a directory: {path}')
    for path in JSON_FILES + SCHEMA_FILES + JSONL_FILES:
        if not path.exists():
            errors.append(f'missing file: {path}')
    return errors


def validate_json_files() -> list[str]:
    errors: list[str] = []
    for path in JSON_FILES + SCHEMA_FILES:
        try:
            load_json(path)
        except FileNotFoundError:
            errors.append(f'missing file: {path}')
        except json.JSONDecodeError as exc:
            errors.append(f'invalid JSON in {path}: {exc}')
    return errors


def validate_jsonl_files() -> list[str]:
    errors: list[str] = []
    for path in JSONL_FILES:
        if not path.exists():
            errors.append(f'missing file: {path}')
            continue
        try:
            with path.open('r', encoding='utf-8') as fh:
                for lineno, raw in enumerate(fh, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        errors.append(f'invalid JSONL in {path}:{lineno}: {exc}')
                        break
        except OSError as exc:
            errors.append(f'read error {path}: {exc}')
    return errors


def load_jsonl_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        errors.append(f'missing file: {path}')
        return records, errors
    try:
        with path.open('r', encoding='utf-8') as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f'invalid JSONL in {path}:{lineno}: {exc}')
                    continue
                if not isinstance(record, dict):
                    errors.append(f'non-object JSONL record in {path}:{lineno}')
                    continue
                records.append(record)
    except OSError as exc:
        errors.append(f'read error {path}: {exc}')
    return records, errors


def index_by_id(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        rid = record.get('id')
        if isinstance(rid, str) and rid:
            counts[rid] += 1
    return counts


def audit() -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    record_counts: dict[str, int] = {}
    records_by_file: dict[str, list[dict[str, Any]]] = {}
    ids_by_file: dict[str, dict[str, int]] = {}
    total_records = 0

    for path in JSONL_FILES:
        file_key = path.name
        records, errors = load_jsonl_records(path)
        records_by_file[file_key] = records
        record_counts[file_key] = len(records)
        total_records += len(records)
        ids_by_file[file_key] = index_by_id(records)
        for err in errors:
            issues.append({'type': 'jsonl_error', 'file': file_key, 'message': err})
        duplicate_ids = sorted([rid for rid, count in ids_by_file[file_key].items() if count > 1])
        if duplicate_ids and file_key not in ('sourcing-projects.jsonl', 'open-webui-requests.jsonl'):
            issues.append({'type': 'duplicate_id', 'file': file_key, 'ids': duplicate_ids})

    id_sets = {
        file_key: {
            rid
            for record in records_by_file.get(file_key, [])
            for rid in [record.get('id'), record.get('category_id')]
            if isinstance(rid, str) and rid
        }
        for file_key, ids in ids_by_file.items()
    }

    for file_key, mappings in REFERENCE_FIELDS.items():
        for idx, record in enumerate(records_by_file.get(file_key, []), start=1):
            for field, target_file in mappings.items():
                value = record.get(field)
                if value is None or value == '':
                    continue
                if value not in id_sets.get(target_file, set()):
                    issues.append({
                        'type': 'missing_reference',
                        'file': file_key,
                        'record_index': idx,
                        'field': field,
                        'value': value,
                        'target_file': target_file,
                    })
            for field in PATH_FIELDS:
                value = record.get(field)
                if isinstance(value, str) and value:
                    if not Path(value).exists():
                        issues.append({
                            'type': 'missing_path',
                            'file': file_key,
                            'record_index': idx,
                            'field': field,
                            'value': value,
                        })

    summary = {
        'root': str(ROOT),
        'audited_at': now(),
        'total_records': total_records,
        'record_counts': record_counts,
        'issues_count': len(issues),
        'issues': issues,
    }
    return summary


def write_report(summary: dict[str, Any]) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open('w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\\n')
    return REPORT_PATH


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    errors.extend(validate_structure())
    errors.extend(validate_json_files())
    errors.extend(validate_jsonl_files())
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'jsonl_files={len(JSONL_FILES)}')
    return 0


def cmd_audit(_: argparse.Namespace) -> int:
    summary = audit()
    print('AUDIT COMPLETE')
    print(f'total_records={summary["total_records"]}')
    print(f'issues_count={summary["issues_count"]}')
    for issue in summary['issues']:
        print(json.dumps(issue, ensure_ascii=False, sort_keys=True))
    return 1 if summary['issues_count'] else 0


def cmd_report(_: argparse.Namespace) -> int:
    summary = audit()
    path = write_report(summary)
    print(path)
    return 1 if summary['issues_count'] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail data integrity audit')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate structure, JSON and JSONL files')
    p_validate.set_defaults(func=cmd_validate)

    p_audit = sub.add_parser('audit', help='Audit referential integrity and duplicates')
    p_audit.set_defaults(func=cmd_audit)

    p_report = sub.add_parser('report', help='Write audit report JSON to logs/data-audit-report.json')
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
