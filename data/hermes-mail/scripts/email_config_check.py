#!/usr/bin/env python3
"""Safe e-mail configuration checker for Hermes Mail.

Validates the local configuration and prints a masked view of e-mail settings.
Does not connect to IMAP or SMTP.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
SETTINGS_PATH = ROOT / 'state' / 'settings.json'
SECRETS_EXAMPLE_PATH = ROOT / 'state' / 'secrets.example.json'


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
        return [f'***{str(item)[-2:]}' if str(item) else '***' for item in value]
    if isinstance(value, str):
        if '@' in value:
            local, _, domain = value.partition('@')
            return f'{local[:1]}***@{domain[:1]}***'
        if len(value) <= 4:
            return '***'
        return f'{value[:2]}***{value[-2:]}'
    return '***'


def validate() -> list[str]:
    errors: list[str] = []
    try:
        settings = load_json(SETTINGS_PATH)
    except FileNotFoundError:
        return [f'missing file: {SETTINGS_PATH}']
    except json.JSONDecodeError as exc:
        return [f'invalid JSON in {SETTINGS_PATH}: {exc}']

    email_config = settings.get('email_config')
    if not isinstance(email_config, dict):
        errors.append('missing or invalid email_config section in settings.json')
        return errors

    required_fields = [
        'imap_host', 'imap_port', 'imap_ssl', 'smtp_host', 'smtp_port',
        'smtp_tls', 'email_address', 'allowed_senders', 'poll_interval_seconds', 'mode',
    ]
    for field in required_fields:
        if field not in email_config:
            errors.append(f'missing field in email_config: {field}')

    if email_config.get('mode') != 'dry_run':
        errors.append('email_config.mode must be "dry_run"')

    if not isinstance(email_config.get('allowed_senders', []), list):
        errors.append('email_config.allowed_senders must be a list')

    if not SECRETS_EXAMPLE_PATH.exists():
        errors.append(f'missing file: {SECRETS_EXAMPLE_PATH}')
    else:
        try:
            secrets = load_json(SECRETS_EXAMPLE_PATH)
        except json.JSONDecodeError as exc:
            errors.append(f'invalid JSON in {SECRETS_EXAMPLE_PATH}: {exc}')
        else:
            for field in ['email_password', 'openai_api_key_reference']:
                if field not in secrets:
                    errors.append(f'missing field in secrets.example.json: {field}')
    return errors


def show_safe() -> None:
    settings = load_json(SETTINGS_PATH)
    email_config = settings.get('email_config', {})
    safe = {
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
        'secrets_example_present': SECRETS_EXAMPLE_PATH.exists(),
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_validate(_: argparse.Namespace) -> int:
    errors = validate()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Safe Hermes Mail e-mail configuration checker')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate safe email configuration files')
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser('show-safe', help='Display masked e-mail configuration')
    p_show.set_defaults(func=cmd_show_safe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
