#!/usr/bin/env python3
"""IMAP read-only connection test for Hermes Mail.

This script is safe by default: it only validates local configuration and,
when explicitly invoked with connect-check, performs a read-only IMAP login
and INBOX message count check. It never downloads, moves, marks, or deletes
messages.
"""

from __future__ import annotations

import argparse
import json
import imaplib
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
SETTINGS_PATH = ROOT / 'state' / 'settings.json'
SECRETS_PATH = ROOT / 'state' / 'secrets.json'
PLACEHOLDER_PASSWORD = 'PREENCHER_SENHA_REAL_AQUI'

REQUIRED_FIELDS = ['email_address', 'imap_host', 'imap_port', 'imap_ssl', 'mode']


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


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


def load_settings() -> dict[str, Any]:
    data = load_json(SETTINGS_PATH)
    if not isinstance(data, dict):
        raise ValueError('settings.json must contain a JSON object')
    return data


def load_secrets() -> dict[str, Any]:
    data = load_json(SECRETS_PATH)
    if not isinstance(data, dict):
        raise ValueError('secrets.json must contain a JSON object')
    return data


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

    if not isinstance(email_config.get('imap_port'), int):
        errors.append('email_config.imap_port must be an integer')
    if not isinstance(email_config.get('imap_ssl'), bool):
        errors.append('email_config.imap_ssl must be a boolean')

    return errors


def validate_secrets(secrets: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    password = secrets.get('email_password')
    if not isinstance(password, str):
        errors.append('missing email_password in secrets.json')
    return errors


def show_safe() -> None:
    settings = load_settings()
    email_config = settings.get('email_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(email_config, dict):
        email_config = {}
    secrets = load_secrets()
    safe = {
        'settings_path': str(SETTINGS_PATH),
        'secrets_path': str(SECRETS_PATH),
        'email_config': {
            'email_address': mask_value(email_config.get('email_address')),
            'imap_host': mask_value(email_config.get('imap_host')),
            'imap_port': email_config.get('imap_port'),
            'imap_ssl': email_config.get('imap_ssl'),
            'mode': email_config.get('mode'),
        },
        'secrets': {
            'email_password': '***' if secrets.get('email_password') else None,
        },
        'pending_fields': [
            'email_password (replace placeholder in local secrets.json before connect-check)'
            if secrets.get('email_password') == PLACEHOLDER_PASSWORD else None,
        ],
    }
    safe['pending_fields'] = [item for item in safe['pending_fields'] if item]
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

    try:
        secrets = load_secrets()
    except FileNotFoundError:
        print(f'VALIDATION FAILED\nmissing file: {SECRETS_PATH}')
        return 1
    except json.JSONDecodeError as exc:
        print(f'VALIDATION FAILED\ninvalid JSON in {SECRETS_PATH}: {exc}')
        return 1

    errors = validate_settings(settings)
    errors.extend(validate_secrets(secrets))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1

    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('mode=dry_run')
    return 0


def cmd_show_safe(_: argparse.Namespace) -> int:
    show_safe()
    return 0


def cmd_connect_check(_: argparse.Namespace) -> int:
    settings = load_settings()
    email_config = settings.get('email_config', {})
    if not isinstance(email_config, dict):
        raise SystemExit('missing email_config section in settings.json')
    if email_config.get('mode') != 'dry_run':
        raise SystemExit('connect-check is only allowed when email_config.mode == dry_run')

    secrets = load_secrets()
    password = secrets.get('email_password')
    if password == PLACEHOLDER_PASSWORD:
        raise SystemExit('connect-check aborted: replace placeholder in local secrets.json first')
    if not isinstance(password, str) or not password:
        raise SystemExit('connect-check aborted: missing email_password in local secrets.json')

    host = email_config.get('imap_host')
    port = email_config.get('imap_port', 993)
    email_address = email_config.get('email_address')
    if not host or not email_address:
        raise SystemExit('connect-check aborted: imap_host and email_address must be configured')

    print(f'CONNECT CHECK START host={host} port={port} email={mask_value(email_address)}')
    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(email_address, password)
        typ, data = client.select('INBOX', readonly=True)
        if typ != 'OK':
            raise SystemExit(f'connect-check failed: SELECT returned {typ}')
        message_count = int(data[0]) if data and data[0] else 0
        print('CONNECT CHECK OK')
        print(f'inbox_messages={message_count}')
        client.close()
        client.logout()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail IMAP read-only test')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate local configuration and secrets file')
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser('show-safe', help='Show masked configuration')
    p_show.set_defaults(func=cmd_show_safe)

    p_connect = sub.add_parser('connect-check', help='Perform a read-only IMAP connection test')
    p_connect.set_defaults(func=cmd_connect_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
