#!/usr/bin/env python3
"""Hermes Mail dry-run outbox simulator.

Creates draft and send-dry-run artifacts without connecting to SMTP.
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
EMAILS_DIR = ROOT / 'emails'
OUTGOING_DIR = EMAILS_DIR / 'outgoing'
LOGS_DIR = ROOT / 'logs'
SETTINGS_PATH = STATE_DIR / 'settings.json'
EMAILS_JSONL = ROOT / 'emails.jsonl'
OUTBOX_LOG = LOGS_DIR / 'outbox.log'

REQUIRED_PATHS = [
    ROOT,
    STATE_DIR,
    EMAILS_DIR,
    OUTGOING_DIR,
    LOGS_DIR,
    ROOT / 'emails' / 'incoming',
    ROOT / 'emails' / 'raw',
    ROOT / 'attachments' / 'original',
    ROOT / 'attachments' / 'extracted-text',
    ROOT / 'attachments' / 'ocr',
    ROOT / 'backups',
    ROOT / 'schemas',
    ROOT / 'fornecedores',
    ROOT / 'contatos',
    ROOT / 'produtos',
    ROOT / 'cotacoes',
    ROOT / 'price-history',
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


def validate_structure() -> list[str]:
    errors: list[str] = []
    for path in REQUIRED_PATHS:
        if not path.exists():
            errors.append(f'missing path: {path}')
    if not EMAILS_JSONL.exists():
        errors.append(f'missing file: {EMAILS_JSONL}')
    return errors


def validate_settings() -> list[str]:
    errors: list[str] = []
    try:
        settings = load_json(SETTINGS_PATH)
    except FileNotFoundError:
        return [f'missing file: {SETTINGS_PATH}']
    except json.JSONDecodeError as exc:
        return [f'invalid JSON in {SETTINGS_PATH}: {exc}']

    email_config = settings.get('email_config')
    if not isinstance(email_config, dict):
        errors.append('missing email_config section')
        return errors
    if email_config.get('mode') != 'dry_run':
        errors.append('email_config.mode must be dry_run')
    if not isinstance(email_config.get('allowed_senders', []), list):
        errors.append('allowed_senders must be a list')
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


def latest_draft() -> dict[str, Any] | None:
    if not EMAILS_JSONL.exists():
        return None
    last = None
    with EMAILS_JSONL.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('direction') == 'outgoing' and rec.get('status') == 'draft':
                last = rec
    return last


def draft_test() -> dict[str, Any]:
    draft_id = f'out_draft_{uuid.uuid4().hex}'
    created = now()
    subject = 'TESTE - Resposta Simulada'
    recipient = 'contato.teste@example.com'
    body_text = """Este é apenas um teste de envio simulado do Hermes Mail.

Nenhum e-mail real foi enviado.
Modo: dry_run"""
    draft_path = OUTGOING_DIR / f'{draft_id}.eml'
    eml_text = f"""From: compras@simulado.local
To: {recipient}
Subject: {subject}
Message-ID: <{draft_id}@simulado.local>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

{body_text}
"""
    draft_path.write_text(eml_text, encoding='utf-8')
    record = {
        'id': draft_id,
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
        'direction': 'outgoing',
        'mode': 'dry_run',
        'status': 'draft',
        'event_type': 'draft',
        'from': 'compras@simulado.local',
        'to': [recipient],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'body_text': body_text,
        'raw_path': str(draft_path),
        'draft_path': str(draft_path),
        'source': 'email_outbox',
        'simulation': True,
    }
    append_jsonl(EMAILS_JSONL, record)
    OUTBOX_LOG.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX_LOG.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'timestamp': created, 'action': 'draft-test', 'id': draft_id, 'status': 'draft'}, ensure_ascii=False, sort_keys=True) + '\n')
    print('DRAFT CREATED')
    print(f'id={draft_id}')
    print(f'path={draft_path}')
    return record


def send_dry_run() -> dict[str, Any]:
    draft = latest_draft()
    if not draft:
        raise SystemExit('no draft found')
    sent_id = f'{draft["id"]}_sent_{uuid.uuid4().hex[:8]}'
    created = now()
    recipient = (draft.get('to') or ['contato.teste@example.com'])[0]
    subject = draft.get('subject', 'TESTE - Resposta Simulada')
    body_text = draft.get('body_text', 'Este é apenas um teste de envio simulado.')
    sent_path = OUTGOING_DIR / f'{sent_id}.eml'
    eml_text = f"""From: {draft.get('from', 'compras@simulado.local')}
To: {recipient}
Subject: {subject}
Message-ID: <{sent_id}@simulado.local>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

{body_text}
"""
    sent_path.write_text(eml_text, encoding='utf-8')
    event = {
        'id': sent_id,
        'version': '0.1.0',
        'created_at': created,
        'updated_at': created,
        'direction': 'outgoing',
        'mode': 'dry_run',
        'status': 'sent_dry_run',
        'event_type': 'send-dry-run',
        'parent_id': draft['id'],
        'draft_id': draft['id'],
        'from': draft.get('from', 'compras@simulado.local'),
        'to': draft.get('to', [recipient]),
        'cc': draft.get('cc', []),
        'bcc': draft.get('bcc', []),
        'subject': subject,
        'body_text': body_text,
        'raw_path': str(sent_path),
        'sent_path': str(sent_path),
        'source': 'email_outbox',
        'simulation': True,
    }
    append_jsonl(EMAILS_JSONL, event)
    with OUTBOX_LOG.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'timestamp': created, 'action': 'send-dry-run', 'draft_id': draft['id'], 'sent_id': sent_id, 'status': 'sent_dry_run'}, ensure_ascii=False, sort_keys=True) + '\n')
    print('DRY RUN SENT')
    print(f'draft_id={draft["id"]}')
    print(f'sent_id={sent_id}')
    print(f'path={sent_path}')
    return event


def stats() -> dict[str, int]:
    return {
        'emails.jsonl': count_jsonl(EMAILS_JSONL),
        'outgoing_eml_files': len(list(OUTGOING_DIR.glob('*.eml'))),
    }


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    errors.extend(validate_structure())
    errors.extend(validate_settings())
    errors.extend(validate_jsonl(EMAILS_JSONL))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('mode=dry_run')
    return 0


def cmd_draft_test(_: argparse.Namespace) -> int:
    draft_test()
    return 0


def cmd_send_dry_run(_: argparse.Namespace) -> int:
    send_dry_run()
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    s = stats()
    print('OUTBOX STATS')
    for key, value in s.items():
        print(f'{key}: {value}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail dry-run outbox simulator')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate dry-run outbox configuration and JSONL')
    p_validate.set_defaults(func=cmd_validate)

    p_draft = sub.add_parser('draft-test', help='Create a fake outgoing draft')
    p_draft.set_defaults(func=cmd_draft_test)

    p_send = sub.add_parser('send-dry-run', help='Create a simulated sent message from the latest draft')
    p_send.set_defaults(func=cmd_send_dry_run)

    p_stats = sub.add_parser('stats', help='Show draft/send counts')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
