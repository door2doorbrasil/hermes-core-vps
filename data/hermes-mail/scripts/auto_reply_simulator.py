#!/usr/bin/env python3
"""Auto-reply simulator for Hermes Mail.

Finds the latest simulated incoming email and generates a dry-run outgoing
reply. No SMTP calls are made.
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
OUTGOING_DIR = ROOT / 'emails' / 'outgoing'
INGEST_LOG = LOGS_DIR / 'ingest.log'

JSON_FILES = [
    STATE_DIR / 'settings.json',
    STATE_DIR / 'cursor.json',
    STATE_DIR / 'ingest-state.json',
    *sorted(SCHEMAS_DIR.glob('*.schema.json')),
]

JSONL_FILES = [
    ROOT / 'fornecedores.jsonl',
    ROOT / 'contatos.jsonl',
    ROOT / 'produtos.jsonl',
    ROOT / 'emails.jsonl',
    ROOT / 'cotacoes.jsonl',
    ROOT / 'price-history.jsonl',
    ROOT / 'anexos.jsonl',
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
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


def validate_json_files() -> list[str]:
    errors: list[str] = []
    for path in JSON_FILES:
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
                json.loads(line)
    except json.JSONDecodeError as exc:
        errors.append(f'invalid JSONL in {path}: {exc}')
    except OSError as exc:
        errors.append(f'read error {path}: {exc}')
    return errors


def latest_incoming_simulated() -> dict[str, Any] | None:
    last = None
    if not EMAILS_JSONL.exists():
        return None
    with EMAILS_JSONL.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('direction') == 'incoming' and rec.get('status') == 'simulated':
                last = rec
    return last


def lookup_last_by_id(path: Path, field_name: str, value: str | None) -> dict[str, Any] | None:
    if not value or not path.exists():
        return None
    last = None
    with path.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get(field_name) == value:
                last = rec
    return last


def related_entities(email: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    supplier = lookup_last_by_id(ROOT / 'fornecedores.jsonl', 'id', email.get('supplier_id'))
    contact = lookup_last_by_id(ROOT / 'contatos.jsonl', 'id', email.get('contact_id'))
    product = None
    quote = lookup_last_by_id(ROOT / 'cotacoes.jsonl', 'email_id', email.get('id'))
    if quote:
        first_item = (quote.get('items') or [{}])[0]
        product_id = first_item.get('product_id')
        product = lookup_last_by_id(ROOT / 'produtos.jsonl', 'id', product_id)
    return supplier, contact, product, quote


def generate_latest() -> dict[str, Any]:
    email = latest_incoming_simulated()
    if not email:
        raise SystemExit('no simulated incoming email found')

    supplier, contact, product, quote = related_entities(email)
    created = now()
    reply_id = f'auto_reply_{uuid.uuid4().hex}'
    subject = f'RE: {email.get("subject", "TESTE")}'
    to_addr = email.get('from') or (contact or {}).get('email') or 'contato.teste@example.com'
    from_addr = 'compras@simulado.local'
    body_lines = [
        'Olá,',
        '',
        'Esta é uma resposta automática de teste gerada pelo Hermes Mail.',
        'Nenhum e-mail real deve ser enviado.',
        'Modo: dry_run.',
        '',
        'Referência do teste:',
        f'- E-mail recebido: {email.get("id")}',
        f'- Fornecedor: {(supplier or {}).get("name", "FORNECEDOR TESTE LTDA")}',
        f'- Contato: {(contact or {}).get("name", "CONTATO TESTE")}',
        f'- Produto: {((product or {}).get("name") or "PRODUTO TESTE")}',
        '',
        'Atenciosamente,',
        'Hermes Mail (simulado)',
    ]
    if quote:
        body_lines.extend(['', f'Cotação relacionada: {quote.get("id")}'])
    body_text = '\\n'.join(body_lines)

    eml_path = OUTGOING_DIR / f'{reply_id}.eml'
    eml_text = f"""From: {from_addr}
To: {to_addr}
Subject: {subject}
Message-ID: <{reply_id}@simulado.local>
In-Reply-To: {email.get('message_id', '')}
References: {email.get('message_id', '')}
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

{body_text}
"""
    eml_path.write_text(eml_text, encoding='utf-8')

    event = {
        'id': reply_id,
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
        'direction': 'outgoing',
        'mode': 'dry_run',
        'status': 'auto_reply_draft',
        'event_type': 'auto-reply',
        'related_email_id': email.get('id'),
        'from': from_addr,
        'to': [to_addr],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'body_text': body_text,
        'raw_path': str(eml_path),
        'source': 'auto_reply_simulator',
        'simulation': True,
    }
    append_jsonl(EMAILS_JSONL, event)

    if INGEST_LOG.exists():
        with INGEST_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'timestamp': created, 'action': 'auto-reply-simulated', 'reply_id': reply_id, 'related_email_id': email.get('id')}, ensure_ascii=False, sort_keys=True) + '\n')

    print('AUTO REPLY GENERATED')
    print(f'email_id={email.get("id")}')
    print(f'reply_id={reply_id}')
    print(f'path={eml_path}')
    return {'email': email, 'reply_id': reply_id, 'path': eml_path}


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    errors.extend(validate_json_files())
    errors.extend(validate_jsonl(EMAILS_JSONL))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    return 0


def cmd_generate_latest(_: argparse.Namespace) -> int:
    generate_latest()
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    print('JSONL COUNTS')
    total = 0
    for path in JSONL_FILES:
        count = count_jsonl(path)
        print(f'{path.name}: {count}')
        total += count
    print(f'total: {total}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail auto-reply simulator')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate JSON and JSONL inputs')
    p_validate.set_defaults(func=cmd_validate)

    p_gen = sub.add_parser('generate-latest', help='Generate an auto-reply for the latest simulated incoming email')
    p_gen.set_defaults(func=cmd_generate_latest)

    p_stats = sub.add_parser('stats', help='Show JSONL counts')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
