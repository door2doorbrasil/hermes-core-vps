#!/usr/bin/env python3
"""Simulated parser for Hermes Mail.

Reads the latest email record from emails.jsonl. If it is a simulated test
message, appends fictitious structured records to the provisional JSONL stores.
No IMAP, SMTP, or OpenAI calls are made.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
STATE_DIR = ROOT / 'state'
SCHEMAS_DIR = ROOT / 'schemas'
LOGS_DIR = ROOT / 'logs'

EMAILS_JSONL = ROOT / 'emails.jsonl'
FORNECEDORES_JSONL = ROOT / 'fornecedores.jsonl'
CONTATOS_JSONL = ROOT / 'contatos.jsonl'
PRODUTOS_JSONL = ROOT / 'produtos.jsonl'
COTACOES_JSONL = ROOT / 'cotacoes.jsonl'
PRICE_HISTORY_JSONL = ROOT / 'price-history.jsonl'
INGEST_LOG = LOGS_DIR / 'ingest.log'

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
    ROOT / 'backups',
    LOGS_DIR,
    SCHEMAS_DIR,
    STATE_DIR,
]

REQUIRED_JSON_FILES = [
    STATE_DIR / 'settings.json',
    STATE_DIR / 'cursor.json',
    STATE_DIR / 'ingest-state.json',
    *sorted(SCHEMAS_DIR.glob('*.schema.json')),
]

JSONL_FILES = [
    FORNECEDORES_JSONL,
    CONTATOS_JSONL,
    PRODUTOS_JSONL,
    EMAILS_JSONL,
    COTACOES_JSONL,
    PRICE_HISTORY_JSONL,
    ROOT / 'anexos.jsonl',
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def count_jsonl(path: Path) -> int:
    total = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    total += 1
    return total


def validate_structure() -> list[str]:
    errors: list[str] = []
    for path in REQUIRED_DIRS:
        if not path.exists():
            errors.append(f'missing directory: {path}')
        elif not path.is_dir():
            errors.append(f'not a directory: {path}')
    for path in [EMAILS_JSONL, FORNECEDORES_JSONL, CONTATOS_JSONL, PRODUTOS_JSONL, COTACOES_JSONL, PRICE_HISTORY_JSONL]:
        if not path.exists():
            errors.append(f'missing file: {path}')
    return errors


def validate_json_files() -> list[str]:
    errors: list[str] = []
    for path in REQUIRED_JSON_FILES:
        try:
            load_json(path)
        except FileNotFoundError:
            errors.append(f'missing file: {path}')
        except json.JSONDecodeError as exc:
            errors.append(f'invalid JSON in {path}: {exc}')
    return errors


def validate_jsonl(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        errors.append(f'missing file: {path}')
        return errors
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


def latest_email() -> dict[str, Any] | None:
    if not EMAILS_JSONL.exists():
        return None
    last = None
    with EMAILS_JSONL.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            last = json.loads(line)
    return last


def stats() -> dict[str, int]:
    return {path.name: count_jsonl(path) for path in JSONL_FILES}


def process_latest() -> list[str]:
    email = latest_email()
    if not email:
        return ['no email records found']
    if email.get('status') != 'simulated':
        return [f'last email status is {email.get("status")!r}, skipping']

    created = now()
    batch = uuid.uuid4().hex[:12]
    supplier_id = f'sup_test_{batch}'
    contact_id = f'con_test_{batch}'
    product_id = f'prd_test_{batch}'
    quote_id = f'cot_test_{batch}'
    price_id = f'prh_test_{batch}'

    supplier = {
        'id': supplier_id,
        'name': 'FORNECEDOR TESTE LTDA',
        'legal_name': 'FORNECEDOR TESTE LTDA',
        'tax_id': None,
        'emails': ['fornecedor.teste@simulado.local'],
        'phones': ['+00 00 0000-0000'],
        'website': None,
        'status': 'prospect',
        'tags': ['teste', 'simulado'],
        'aliases': ['Fornecedor Teste'],
        'notes': 'Registro fictício gerado pelo parser simulado.',
        'source': 'simulated_parser',
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
    }

    contact = {
        'id': contact_id,
        'supplier_id': supplier_id,
        'name': 'CONTATO TESTE',
        'role': 'Vendas',
        'email': 'contato.teste@simulado.local',
        'phone': '+00 00 0000-0001',
        'preferred_channel': 'email',
        'status': 'active',
        'tags': ['teste', 'simulado'],
        'notes': 'Contato fictício gerado pelo parser simulado.',
        'source': 'simulated_parser',
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
    }

    product = {
        'id': product_id,
        'sku': 'PRD-TEST-0001',
        'name': 'PRODUTO TESTE',
        'description': 'Produto fictício para simulação.',
        'unit': 'un',
        'category': 'teste',
        'brand': 'SIMULADO',
        'aliases': ['Produto Simulado'],
        'active': True,
        'source': 'simulated_parser',
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
    }

    quote = {
        'id': quote_id,
        'supplier_id': supplier_id,
        'contact_id': contact_id,
        'email_id': email['id'],
        'quote_number': 'Q-TESTE-0001',
        'requested_at': email.get('received_at') or email.get('created_at'),
        'received_at': created,
        'currency': 'USD',
        'items': [
            {
                'product_id': product_id,
                'description': 'PRODUTO TESTE',
                'quantity': 1,
                'unit': 'un',
                'unit_price': 123.45,
                'subtotal': 123.45,
                'notes': 'FOB / TESTE / TESTE',
            }
        ],
        'subtotal': 123.45,
        'shipping': 0.0,
        'taxes': 0.0,
        'total': 123.45,
        'valid_until': '2026-12-31',
        'lead_time_days': 7,
        'status': 'received',
        'notes': 'Incoterm FOB, país TESTE, cidade TESTE.',
        'source': 'simulated_parser',
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
    }

    price_history = {
        'id': price_id,
        'product_id': product_id,
        'supplier_id': supplier_id,
        'quote_id': quote_id,
        'email_id': email['id'],
        'currency': 'USD',
        'unit_price': 123.45,
        'quantity': 1,
        'min_qty': 1,
        'effective_date': '2026-06-25',
        'captured_at': created,
        'source': 'simulated_parser',
        'attachment_ids': [],
        'notes': 'FOB / país TESTE / cidade TESTE.',
        'version': '0.1.0',
        'created_at': created,
    }

    append_jsonl(FORNECEDORES_JSONL, supplier)
    append_jsonl(CONTATOS_JSONL, contact)
    append_jsonl(PRODUTOS_JSONL, product)
    append_jsonl(COTACOES_JSONL, quote)
    append_jsonl(PRICE_HISTORY_JSONL, price_history)

    INGEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_line = json.dumps(
        {
            'timestamp': created,
            'action': 'parse-latest',
            'email_id': email['id'],
            'status': 'parsed',
            'supplier_id': supplier_id,
            'contact_id': contact_id,
            'product_id': product_id,
            'quote_id': quote_id,
            'price_id': price_id,
            'source': 'simulated_parser',
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    with INGEST_LOG.open('a', encoding='utf-8') as fh:
        fh.write(log_line + '\n')

    print('PARSE OK')
    print(f'email_id={email["id"]}')
    print(f'supplier_id={supplier_id}')
    print(f'contact_id={contact_id}')
    print(f'product_id={product_id}')
    print(f'quote_id={quote_id}')
    print(f'price_id={price_id}')
    return []


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    errors.extend(validate_structure())
    errors.extend(validate_json_files())
    errors.extend(validate_jsonl(EMAILS_JSONL))
    errors.extend(validate_jsonl(FORNECEDORES_JSONL))
    errors.extend(validate_jsonl(CONTATOS_JSONL))
    errors.extend(validate_jsonl(PRODUTOS_JSONL))
    errors.extend(validate_jsonl(COTACOES_JSONL))
    errors.extend(validate_jsonl(PRICE_HISTORY_JSONL))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'emails={EMAILS_JSONL}')
    return 0


def cmd_parse_latest(_: argparse.Namespace) -> int:
    errors = process_latest()
    if errors:
        print('PARSE SKIPPED')
        for err in errors:
            print(err)
        return 1
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    print('JSONL COUNTS')
    total = 0
    for name, count in sorted(stats().items()):
        print(f'{name}: {count}')
        total += count
    print(f'total: {total}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail simulated parser')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate structure and JSONL files')
    p_validate.set_defaults(func=cmd_validate)

    p_parse = sub.add_parser('parse-latest', help='Parse the latest simulated email into structured entities')
    p_parse.set_defaults(func=cmd_parse_latest)

    p_stats = sub.add_parser('stats', help='Show counts for JSONL files')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
