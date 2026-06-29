#!/usr/bin/env python3
"""IMAP dry-run connector skeleton for Hermes Mail.

This script only inspects local configuration. It does not connect to IMAP,
does not request passwords, and does not write secrets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
SETTINGS_PATH = ROOT / 'state' / 'settings.json'

REQUIRED_FIELDS = [
    'imap_host',
    'imap_port',
    'imap_ssl',
    'smtp_host',
    'smtp_port',
    'smtp_tls',
    'email_address',
    'allowed_senders',
    'poll_interval_seconds',
    'mode',
]

PENDING_HINTS = {
    'imap_host': 'preencher host IMAP quando houver servidor real',
    'imap_port': 'definir porta IMAP (ex.: 993 para SSL)',
    'imap_ssl': 'definir se IMAP usa SSL/TLS',
    'smtp_host': 'preencher host SMTP quando houver servidor real',
    'smtp_port': 'definir porta SMTP (ex.: 587 ou 465)',
    'smtp_tls': 'definir se SMTP usa TLS',
    'email_address': 'definir o endereço de e-mail da caixa de leitura',
    'allowed_senders': 'listar remetentes permitidos, se houver restrição',
    'poll_interval_seconds': 'ajustar intervalo de polling conforme necessidade',
    'mode': 'manter "dry_run" até a ativação controlada',
}


def load_settings() -> dict[str, Any]:
    with SETTINGS_PATH.open('r', encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError('settings.json must contain a JSON object')
    return data


def mask_value(value: Any) -> Any:
    if value in (None, ''):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return ['***' for _ in value]
    if isinstance(value, str):
        if '@' in value:
            local, _, domain = value.partition('@')
            return f'{local[:1]}***@{domain[:1]}***'
        if len(value) <= 4:
            return '***'
        return f'{value[:2]}***{value[-2:]}'
    return '***'


def validate_settings(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    email_config = settings.get('email_config')
    if not isinstance(email_config, dict):
        return ['missing email_config section in settings.json']

    for field in REQUIRED_FIELDS:
        if field not in email_config:
            errors.append(f'missing field: email_config.{field}')

    if email_config.get('mode') != 'dry_run':
        errors.append('email_config.mode must be "dry_run"')

    if not isinstance(email_config.get('allowed_senders', []), list):
        errors.append('email_config.allowed_senders must be a list')

    return errors


def pending_fields(settings: dict[str, Any]) -> list[str]:
    email_config = settings.get('email_config') if isinstance(settings, dict) else {}
    if not isinstance(email_config, dict):
        email_config = {}

    pending: list[str] = []
    for field in REQUIRED_FIELDS:
        value = email_config.get(field)
        if value in (None, '', [], {}):
            pending.append(f'{field}: {PENDING_HINTS[field]}')
    return pending


def show_config() -> None:
    settings = load_settings()
    email_config = settings.get('email_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(email_config, dict):
        email_config = {}

    safe = {
        'settings_path': str(SETTINGS_PATH),
        'email_config': {
            'imap_host': mask_value(email_config.get('imap_host')),
            'imap_port': email_config.get('imap_port'),
            'imap_ssl': email_config.get('imap_ssl'),
            'smtp_host': mask_value(email_config.get('smtp_host')),
            'smtp_port': email_config.get('smtp_port'),
            'smtp_tls': email_config.get('smtp_tls'),
            'email_address': mask_value(email_config.get('email_address')),
            'allowed_senders_count': len(email_config.get('allowed_senders') or []),
            'allowed_senders': ['***' for _ in (email_config.get('allowed_senders') or [])],
            'poll_interval_seconds': email_config.get('poll_interval_seconds'),
            'mode': email_config.get('mode'),
        },
        'pending_fields': pending_fields(settings),
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_validate(_: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except FileNotFoundError:
        print(f'VALIDATION FAILED\nmissing file: {SETTINGS_PATH}')
        return 1
    except json.JSONDecodeError as exc:
        print(f'VALIDATION FAILED\ninvalid JSON in {SETTINGS_PATH}: {exc}')
        return 1
    except Exception as exc:
        print(f'VALIDATION FAILED\n{exc}')
        return 1

    errors = validate_settings(settings)
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1

    email_config = settings.get('email_config', {})
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'mode={email_config.get("mode")}')
    print(f'pending_fields={len(pending_fields(settings))}')
    return 0


def cmd_show_config(_: argparse.Namespace) -> int:
    show_config()
    return 0


def cmd_plan(_: argparse.Namespace) -> int:
    plan = {
        'current_mode': 'dry_run',
        'future_flow': [
            'conectar IMAP',
            'listar mensagens não lidas',
            'baixar .eml',
            'salvar raw',
            'extrair body',
            'gravar emails.jsonl',
            'não mover nem apagar mensagens',
            'só depois ativar processamento',
        ],
        'safety_guards': [
            'não conectar em IMAP ainda',
            'não pedir senha no terminal',
            'não gravar senha real',
            'ler apenas state/settings.json',
        ],
        'pending_fields': pending_fields(load_settings()),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_test_no_connect(_: argparse.Namespace) -> int:
    settings = load_settings()
    errors = validate_settings(settings)
    if errors:
        print('TEST FAILED')
        for err in errors:
            print(err)
        return 1
    print('TEST NO CONNECT OK')
    print('No IMAP connection attempted')
    print('No SMTP connection attempted')
    print('No passwords requested')
    print(f'mode={settings["email_config"]["mode"]}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail IMAP dry-run connector skeleton')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate local dry-run configuration only')
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser('show-config', help='Show masked configuration and pending fields')
    p_show.set_defaults(func=cmd_show_config)

    p_plan = sub.add_parser('plan', help='Explain the future IMAP flow')
    p_plan.set_defaults(func=cmd_plan)

    p_test = sub.add_parser('test-no-connect', help='Confirm no network connection is attempted')
    p_test.set_defaults(func=cmd_test_no_connect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
