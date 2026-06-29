#!/usr/bin/env python3
"""Simulated e-mail ingestor for Hermes Mail.

This utility is intentionally offline: it does not connect to IMAP/SMTP.
It can validate the provisional store, emit one clearly marked test e-mail,
and report basic JSONL counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
EMAILS_DIR = ROOT / 'emails'
INCOMING_DIR = EMAILS_DIR / 'incoming'
OUTGOING_DIR = EMAILS_DIR / 'outgoing'
RAW_DIR = EMAILS_DIR / 'raw'
STATE_DIR = ROOT / 'state'
SCHEMAS_DIR = ROOT / 'schemas'
LOGS_DIR = ROOT / 'logs'
JSONL_EMAILS = ROOT / 'emails.jsonl'

REQUIRED_DIRS = [
    ROOT,
    EMAILS_DIR,
    INCOMING_DIR,
    OUTGOING_DIR,
    RAW_DIR,
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

REQUIRED_FILES = [
    ROOT / 'fornecedores.jsonl',
    ROOT / 'contatos.jsonl',
    ROOT / 'produtos.jsonl',
    ROOT / 'emails.jsonl',
    ROOT / 'cotacoes.jsonl',
    ROOT / 'price-history.jsonl',
    ROOT / 'anexos.jsonl',
    STATE_DIR / 'settings.json',
    STATE_DIR / 'cursor.json',
    STATE_DIR / 'ingest-state.json',
    SCHEMAS_DIR / 'fornecedores.schema.json',
    SCHEMAS_DIR / 'contatos.schema.json',
    SCHEMAS_DIR / 'produtos.schema.json',
    SCHEMAS_DIR / 'emails.schema.json',
    SCHEMAS_DIR / 'cotacoes.schema.json',
    SCHEMAS_DIR / 'price-history.schema.json',
    SCHEMAS_DIR / 'anexos.schema.json',
]


def now() -> str:
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
    for f in REQUIRED_FILES:
        if not f.exists():
            errors.append(f'missing file: {f}')
    return errors


def validate_json_files() -> list[str]:
    errors: list[str] = []
    for path in [
        STATE_DIR / 'settings.json',
        STATE_DIR / 'cursor.json',
        STATE_DIR / 'ingest-state.json',
        *sorted(SCHEMAS_DIR.glob('*.schema.json')),
    ]:
        try:
            load_json(path)
        except FileNotFoundError:
            errors.append(f'missing file: {path}')
        except json.JSONDecodeError as exc:
            errors.append(f'invalid JSON in {path}: {exc}')
    return errors


def count_jsonl(path: Path) -> int:
    count = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    count += 1
    return count


def jsonl_stats() -> dict[str, int]:
    files = [
        ROOT / 'fornecedores.jsonl',
        ROOT / 'contatos.jsonl',
        ROOT / 'produtos.jsonl',
        ROOT / 'emails.jsonl',
        ROOT / 'cotacoes.jsonl',
        ROOT / 'price-history.jsonl',
        ROOT / 'anexos.jsonl',
    ]
    return {path.name: count_jsonl(path) for path in files}


def make_email_payload(email_id: str) -> tuple[dict[str, Any], str, str]:
    created = now()
    subject = 'TESTE - Cotação Simulada'
    body_text = """Este e-mail é um teste simulado do Hermes Mail.

Fornecedor fictício: Fornecedor Simulado LTDA
Contato fictício: Maria Teste
Produto fictício: Produto Simulado 1000

Nenhum dado real foi utilizado."""
    raw_eml = f"""From: teste@simulado.local
To: compras@simulado.local
Subject: {subject}
Message-ID: <{email_id}@simulado.local>
Date: Tue, 25 Jun 2026 15:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=\"utf-8\"

{body_text}
"""
    body_hash = hashlib.sha256(raw_eml.encode('utf-8')).hexdigest()
    record = {
        'id': email_id,
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
        'direction': 'incoming',
        'message_id': f'<{email_id}@simulado.local>',
        'thread_id': None,
        'from': 'teste@simulado.local',
        'to': ['compras@simulado.local'],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'sent_at': None,
        'received_at': created,
        'body_text': body_text,
        'body_html_path': None,
        'raw_path': str(RAW_DIR / f'{email_id}.eml'),
        'supplier_id': 'sup_test_0001',
        'contact_id': 'con_test_0001',
        'product_ids': ['prd_test_0001'],
        'attachment_ids': [],
        'classification': 'simulated_test',
        'status': 'simulated',
        'hash': body_hash,
        'source': 'simulator',
        'test_marker': True,
        'simulation': True,
    }
    return record, raw_eml, body_text


def simulate() -> dict[str, Path]:
    email_id = f'eml_test_{uuid.uuid4().hex}'
    record, raw_eml, body_text = make_email_payload(email_id)
    raw_path = RAW_DIR / f'{email_id}.eml'
    body_path = INCOMING_DIR / f'{email_id}.txt'

    raw_path.write_text(raw_eml, encoding='utf-8')
    body_path.write_text(body_text, encoding='utf-8')

    with JSONL_EMAILS.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')

    print('SIMULATED EMAIL CREATED')
    print(f'id={email_id}')
    print(f'jsonl={JSONL_EMAILS}')
    print(f'raw={raw_path}')
    print(f'body={body_path}')
    print(f'hash={record["hash"]}')
    return {'jsonl': JSONL_EMAILS, 'raw': raw_path, 'body': body_path}


def cmd_validate(_: argparse.Namespace) -> int:
    errors = []
    errors.extend(validate_structure())
    errors.extend(validate_json_files())
    if not JSONL_EMAILS.exists():
        errors.append(f'missing file: {JSONL_EMAILS}')
    else:
        try:
            with JSONL_EMAILS.open('r', encoding='utf-8') as fh:
                for lineno, raw in enumerate(fh, start=1):
                    if raw.strip():
                        json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f'invalid JSONL in {JSONL_EMAILS}: {exc}')
        except OSError as exc:
            errors.append(f'read error {JSONL_EMAILS}: {exc}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'jsonl={JSONL_EMAILS}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    print('JSONL COUNTS')
    total = 0
    for name, count in sorted(jsonl_stats().items()):
        print(f'{name}: {count}')
        total += count
    print(f'total: {total}')
    return 0


def cmd_simulate(_: argparse.Namespace) -> int:
    simulate()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail simulated ingestor')
    sub = parser.add_subparsers(dest='command', required=True)
    p_validate = sub.add_parser('validate', help='Validate structure and JSON files')
    p_validate.set_defaults(func=cmd_validate)
    p_stats = sub.add_parser('stats', help='Show counts for JSONL files')
    p_stats.set_defaults(func=cmd_stats)
    p_sim = sub.add_parser('simulate', help='Create one clearly marked test email')
    p_sim.set_defaults(func=cmd_simulate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
