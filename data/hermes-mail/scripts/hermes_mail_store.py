#!/usr/bin/env python3
"""Hermes Mail store utility.

Operates on the provisional filesystem-backed store under
/opt/data/hermes-mail. It validates structure, checks JSON/JSONL files,
reports record counts, appends JSONL records, and can create compressed
backups.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
STATE_DIR = ROOT / 'state'
SCHEMAS_DIR = ROOT / 'schemas'
BACKUPS_DIR = ROOT / 'backups'
SCRIPTS_DIR = ROOT / 'scripts'
CONFIG_DIR = ROOT / 'config'

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
    ROOT / 'manufacturer-discovery.jsonl',
    ROOT / 'manual-review-queue.jsonl',
    ROOT / 'rfq-drafts.jsonl',
    ROOT / 'open-webui-requests.jsonl',
    ROOT / 'open-webui-actions.jsonl',
    ROOT / 'supplier-performance.jsonl',
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
    ROOT / 'data' / 'hermes_diary.jsonl',
    ROOT / 'data' / 'user_profiles.jsonl',
    ROOT / 'data' / 'user_credentials.jsonl',
    ROOT / 'data' / 'identity-audit.jsonl',
    ROOT / 'data' / 'identity-diary.jsonl',
]

REQUIRED_DIRS = [
    ROOT,
    ROOT / 'emails',
    ROOT / 'emails' / 'incoming',
    ROOT / 'emails' / 'outgoing',
    ROOT / 'emails' / 'raw',
    ROOT / 'attachments',
    ROOT / 'attachments' / 'original',
    ROOT / 'attachments' / 'extracted-text',
    ROOT / 'attachments' / 'ocr',
    ROOT / 'fornecedores',
    ROOT / 'contatos',
    ROOT / 'produtos',
    ROOT / 'cotacoes',
    ROOT / 'price-history',
    ROOT / 'reports',
    ROOT / 'logs',
    ROOT / 'assets',
    ROOT / 'config',
    ROOT / 'client-quotes',
    ROOT / 'reports' / 'comparisons',
    ROOT / 'sourcing-projects',
    ROOT / 'rfq-drafts',
    ROOT / 'reports' / 'sourcing',
    BACKUPS_DIR,
    SCHEMAS_DIR,
    STATE_DIR,
    SCRIPTS_DIR,
]

ENTITY_TO_JSONL = {
    'fornecedores': ROOT / 'fornecedores.jsonl',
    'contatos': ROOT / 'contatos.jsonl',
    'produtos': ROOT / 'produtos.jsonl',
    'emails': ROOT / 'emails.jsonl',
    'cotacoes': ROOT / 'cotacoes.jsonl',
    'price-history': ROOT / 'price-history.jsonl',
    'anexos': ROOT / 'anexos.jsonl',
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def validate_structure() -> list[str]:
    errors: list[str] = []
    for d in REQUIRED_DIRS:
        if not d.exists():
            errors.append(f'missing directory: {d}')
        elif not d.is_dir():
            errors.append(f'not a directory: {d}')

    for f in JSON_FILES + SCHEMA_FILES + JSONL_FILES:
        if not f.exists():
            errors.append(f'missing file: {f}')

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


def make_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return f'{kind}_{stamp}'


def normalize_record(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    record = dict(data)
    record.setdefault('id', make_id(kind))
    record.setdefault('created_at', now)
    record.setdefault('version', '0.1.0')
    record.setdefault('updated_at', now)
    return record


def add_record(kind: str, payload: str | None, file_path: str | None) -> Path:
    if payload and file_path:
        raise SystemExit('use either --json or --file, not both')
    if not payload and not file_path:
        raise SystemExit('provide --json or --file')
    if file_path:
        data = load_json(Path(file_path))
    else:
        data = json.loads(payload or '{}')
    if not isinstance(data, dict):
        raise SystemExit('record must be a JSON object')
    path = ENTITY_TO_JSONL[kind]
    record = normalize_record(kind, data)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\\n')
    return path


def count_jsonl(path: Path) -> int:
    count = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    count += 1
    return count


def stats() -> dict[str, int]:
    return {path.name: count_jsonl(path) for path in JSONL_FILES}


def create_backup() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = BACKUPS_DIR / f'hermes-mail-{stamp}.tar.gz'
    with tarfile.open(out, 'w:gz') as tar:
        for path in [STATE_DIR, SCHEMAS_DIR, ROOT / 'logs', CONFIG_DIR, ROOT / 'data']:
            if path.exists():
                tar.add(path, arcname=path.relative_to(ROOT))
        for path in JSONL_FILES:
            if path.exists():
                tar.add(path, arcname=path.relative_to(ROOT))
    return out


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
    print(f'json_files={len(JSON_FILES)}')
    print(f'schema_files={len(SCHEMA_FILES)}')
    print(f'jsonl_files={len(JSONL_FILES)}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    counts = stats()
    print('JSONL COUNTS')
    total = 0
    for name in sorted(counts):
        print(f'{name}: {counts[name]}')
        total += counts[name]
    print(f'total: {total}')
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    path = add_record(args.entity, args.json, args.file)
    print(path)
    return 0


def cmd_backup(_: argparse.Namespace) -> int:
    path = create_backup()
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail store utility')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate structure, JSON, schemas, and JSONL files')
    p_validate.set_defaults(func=cmd_validate)

    p_stats = sub.add_parser('stats', help='Show counts per JSONL file')
    p_stats.set_defaults(func=cmd_stats)

    p_add = sub.add_parser('add', help='Append a JSON object to a JSONL store')
    p_add.add_argument('entity', choices=sorted(ENTITY_TO_JSONL))
    src = p_add.add_mutually_exclusive_group(required=True)
    src.add_argument('--json', help='JSON object string')
    src.add_argument('--file', help='Path to a JSON file containing a single object')
    p_add.set_defaults(func=cmd_add)

    p_backup = sub.add_parser('backup', help='Create a compressed backup in /opt/data/hermes-mail/backups')
    p_backup.set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
