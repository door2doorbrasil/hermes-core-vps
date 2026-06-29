#!/usr/bin/env python3
"""Hermes user identity, company, and permission resolver.

This module keeps a file-backed registry for user profiles, company defaults,
role permissions, and session identity resolution. It is intentionally
non-destructive: when no user profile exists, it falls back to company defaults
and finally to the controlled e-mail context resolver.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from email_real_common import resolve_email_context
from reporting_utils import (
    ROOT,
    append_jsonl,
    count_jsonl,
    get_env_secret,
    load_json,
    load_jsonl_records,
    make_id,
    normalize_text,
    utc_now,
    write_json,
)

DATA_DIR = ROOT / 'data'
CONFIG_DIR = ROOT / 'config'
USER_PROFILES_JSONL = DATA_DIR / 'user_profiles.jsonl'
USER_CREDENTIALS_JSONL = DATA_DIR / 'user_credentials.jsonl'
IDENTITY_AUDIT_JSONL = DATA_DIR / 'identity-audit.jsonl'
IDENTITY_DIARY_JSONL = DATA_DIR / 'identity-diary.jsonl'
IDENTITY_POLICY_PATH = CONFIG_DIR / 'identity_policy.json'
COMPANY_DEFAULTS_PATH = CONFIG_DIR / 'company_identity_defaults.json'

SENSITIVE_MARKERS = ('PASSWORD', 'TOKEN', 'SECRET', 'KEY')
DEFAULT_CONTEXT_TO_COMPANY = {
    'buy': 'Polar Sinergy LLC',
    'sales': 'D2D Representação Comercial Ltda',
}

DEFAULT_POLICY: dict[str, Any] = {
    'roles': {
        'admin': {
            'allowed_agents': ['*'],
            'allowed_tools': ['*'],
            'allowed_actions': ['*'],
        },
        'compras': {
            'allowed_agents': ['polar-sinergy-compras'],
            'allowed_tools': ['IMAP', 'SMTP', 'ERP', 'CRM', 'OCR', 'OpenAI', 'Upload de documentos', 'Aprovação de e-mails', 'Aprovação de RFQ', 'Aprovação de propostas', 'Integrações externas'],
            'allowed_actions': ['rfq', 'supplier_approval', 'email_ingest', 'email_send_approved'],
        },
        'vendas': {
            'allowed_agents': ['polar-sinergy-vendas', 'd2d-representacao-vendas'],
            'allowed_tools': ['IMAP', 'SMTP', 'ERP', 'CRM', 'OCR', 'OpenAI', 'Upload de documentos', 'Aprovação de campanhas', 'Aprovação de propostas', 'Integrações externas'],
            'allowed_actions': ['campaigns', 'lead_followup', 'proposal_approval', 'sales_email'],
        },
        'agenciamento_cargas': {
            'allowed_agents': ['hermes-agenciamento-de-cargas'],
            'allowed_tools': ['IMAP', 'SMTP', 'ERP', 'OCR', 'OpenAI', 'Upload de documentos', 'Integrações externas'],
            'allowed_actions': ['shipment_tracking', 'booking', 'bl_awb', 'ce_mercante', 'duimp', 'due'],
        },
        'comex': {
            'allowed_agents': ['hermes-comex'],
            'allowed_tools': ['ERP', 'OCR', 'OpenAI', 'Upload de documentos', 'Integrações externas'],
            'allowed_actions': ['duimp', 'due', 'lpco', 'classificacao_fiscal', 'document_review'],
        },
        'financeiro': {
            'allowed_agents': ['hermes-financeiro'],
            'allowed_tools': ['ERP', 'OpenAI', 'Upload de documentos', 'Integrações externas'],
            'allowed_actions': ['payables', 'receivables', 'cash_flow', 'fx', 'reconciliation'],
        },
        'supervisor': {
            'allowed_agents': ['*'],
            'allowed_tools': ['dashboard'],
            'allowed_actions': ['view_only'],
        },
    }
}

DEFAULT_COMPANY_DEFAULTS: dict[str, Any] = {
    'Polar Sinergy LLC': {
        'company_name': 'Polar Sinergy LLC',
        'default_profile': 'compras',
        'default_language': 'en',
        'timezone': 'America/New_York',
        'email_address': 'buyer@polarsinergy.com',
        'company_domain': 'polarsinergy.com',
        'company_logo': '',
        'default_signature_plain': 'Polar Sinergy LLC\nAluizio Andreatta\nE-mail: buyer@polarsinergy.com\nPhone: +1 321 948 9126\nWhatsApp: +55 44 99156 9673\nWebsite: https://www.polarsinergy.com',
        'default_agent': 'polar-sinergy-compras',
        'default_tools': ['IMAP', 'SMTP', 'ERP', 'CRM', 'OCR', 'OpenAI', 'Upload de documentos', 'Aprovação de e-mails', 'Aprovação de RFQ'],
    },
    'D2D Representação Comercial Ltda': {
        'company_name': 'D2D Representação Comercial Ltda',
        'default_profile': 'vendas',
        'default_language': 'pt-BR',
        'timezone': 'America/Sao_Paulo',
        'email_address': 'aluizio@door2doorbrasil.com.br',
        'company_domain': 'door2doorbrasil.com.br',
        'company_logo': '',
        'default_signature_plain': 'D2D Representação Comercial Ltda\nMaringá - Paraná - Brasil\n\nAluizio Andreatta\nVendas Internacionais\nE-mail: aluizio@door2doorbrasil.com.br\nPhone/WhatsApp: +55 44 99156-9673',
        'default_agent': 'd2d-representacao-vendas',
        'default_tools': ['IMAP', 'SMTP', 'ERP', 'CRM', 'OCR', 'OpenAI', 'Upload de documentos', 'Aprovação de campanhas', 'Aprovação de propostas'],
    },
}


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for path in [USER_PROFILES_JSONL, USER_CREDENTIALS_JSONL, IDENTITY_AUDIT_JSONL, IDENTITY_DIARY_JSONL]:
        path.touch(exist_ok=True)
    if not IDENTITY_POLICY_PATH.exists():
        write_json(IDENTITY_POLICY_PATH, DEFAULT_POLICY)
    if not COMPANY_DEFAULTS_PATH.exists():
        write_json(COMPANY_DEFAULTS_PATH, DEFAULT_COMPANY_DEFAULTS)


def load_policy() -> dict[str, Any]:
    ensure_storage()
    data = load_json(IDENTITY_POLICY_PATH)
    return data if isinstance(data, dict) else dict(DEFAULT_POLICY)


def load_company_defaults() -> dict[str, Any]:
    ensure_storage()
    data = load_json(COMPANY_DEFAULTS_PATH)
    return data if isinstance(data, dict) else dict(DEFAULT_COMPANY_DEFAULTS)


def load_user_profiles() -> list[dict[str, Any]]:
    ensure_storage()
    return load_jsonl_records(USER_PROFILES_JSONL)


def load_user_credentials() -> list[dict[str, Any]]:
    ensure_storage()
    return load_jsonl_records(USER_CREDENTIALS_JSONL)


def _mask_sensitive(name: str, value: str | None) -> str:
    if not value:
        return ''
    if any(marker in name.upper() for marker in SENSITIVE_MARKERS):
        return '***'
    if '@' in value:
        local, _, domain = value.partition('@')
        return f'{local[:1]}***@{domain[:1]}***'
    if len(value) <= 4:
        return '***'
    return f'{value[:2]}***{value[-2:]}'


def _header(headers: Any, *names: str) -> str | None:
    if headers is None:
        return None
    for name in names:
        value = None
        if isinstance(headers, dict):
            value = headers.get(name)
            if value is None:
                value = headers.get(name.lower())
        else:
            try:
                value = headers.get(name)
            except Exception:
                value = None
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def _company_from_email(email: str | None) -> str | None:
    if not email or '@' not in email:
        return None
    domain = email.split('@', 1)[1].casefold()
    for company, defaults in load_company_defaults().items():
        default_domain = str(defaults.get('company_domain') or '').casefold()
        if default_domain and (domain == default_domain or domain.endswith('.' + default_domain)):
            return company
    return None


def _profile_from_login_email(login_email: str | None) -> dict[str, Any] | None:
    if not login_email:
        return None
    login_email = login_email.casefold()
    for record in load_user_profiles():
        if str(record.get('login_email') or '').casefold() == login_email:
            return record
    return None


def _credential_from_login_email(login_email: str | None) -> dict[str, Any] | None:
    if not login_email:
        return None
    login_email = login_email.casefold()
    for record in load_user_credentials():
        if str(record.get('login_email') or '').casefold() == login_email:
            return record
    return None


def resolve_identity(*, headers: Any | None = None, context: str | None = None, login_email: str | None = None, company: str | None = None, profile: str | None = None) -> dict[str, Any]:
    ensure_storage()
    context_name = normalize_text(str(context or '')).lower() if context else 'buy'
    context_name = context_name if context_name in {'buy', 'sales'} else 'buy'
    header_email = _header(headers, 'X-Hermes-User-Email', 'X-OpenWebUI-User-Email', 'X-Open-WebUI-User-Email', 'X-User-Email')
    header_name = _header(headers, 'X-Hermes-User-Name', 'X-OpenWebUI-User-Name', 'X-Open-WebUI-User-Name', 'X-User-Name')
    header_company = _header(headers, 'X-Hermes-User-Company', 'X-User-Company')
    header_profile = _header(headers, 'X-Hermes-User-Profile', 'X-User-Profile', 'X-User-Role')
    header_language = _header(headers, 'X-Hermes-User-Language', 'X-User-Language')
    header_timezone = _header(headers, 'X-Hermes-User-Timezone', 'X-User-Timezone')
    resolved_login = login_email or header_email or get_env_secret('HERMES_AUTH_USER_EMAIL', 'OPEN_WEBUI_USER_EMAIL')
    user_profile = _profile_from_login_email(resolved_login)
    user_credentials = _credential_from_login_email(resolved_login)
    company_name = company or header_company or (user_profile.get('company') if user_profile else None) or _company_from_email(resolved_login) or DEFAULT_CONTEXT_TO_COMPANY.get(context_name)
    company_defaults = load_company_defaults().get(company_name or '', {}) if company_name else {}
    if not company_defaults and context_name == 'sales':
        company_name = 'D2D Representação Comercial Ltda' if company_name is None else company_name
        company_defaults = load_company_defaults().get(company_name, {})
    if not company_defaults and context_name == 'buy':
        company_name = 'Polar Sinergy LLC' if company_name is None else company_name
        company_defaults = load_company_defaults().get(company_name, {})

    role = profile or header_profile or (user_profile.get('profile') if user_profile else None) or str(company_defaults.get('default_profile') or ('compras' if context_name == 'buy' else 'vendas'))
    policy = load_policy().get('roles', {}) if isinstance(load_policy(), dict) else {}
    role_policy = policy.get(role, policy.get('compras' if context_name == 'buy' else 'vendas', {})) if isinstance(policy, dict) else {}
    login = resolved_login or (user_profile.get('login_email') if user_profile else '') or str(company_defaults.get('email_address') or '')
    account_company = company_name or (user_profile.get('company') if user_profile else '') or str(company_defaults.get('company_name') or '')
    allowed_tools = list(role_policy.get('allowed_tools') or company_defaults.get('default_tools') or [])
    allowed_agents = list(role_policy.get('allowed_agents') or [company_defaults.get('default_agent') or ''])
    signature = (user_profile or {}).get('signature_plain') or (user_credentials or {}).get('signature_plain') or company_defaults.get('default_signature_plain') or ''
    imap_cfg = resolve_email_context(context_name)['imap']
    smtp_cfg = resolve_email_context(context_name)['smtp']
    identity = {
        'user': {
            'name': header_name or (user_profile.get('name') if user_profile else '') or login.split('@', 1)[0],
            'login_email': login,
            'title': (user_profile or {}).get('title', ''),
            'department': (user_profile or {}).get('department', ''),
            'status': (user_profile or {}).get('status', 'active' if login else 'pending'),
            'created_at': (user_profile or {}).get('created_at', ''),
            'last_access_at': (user_profile or {}).get('last_access_at', ''),
            'preferred_language': header_language or (user_profile or {}).get('preferred_language') or company_defaults.get('default_language', 'pt-BR'),
            'timezone': header_timezone or (user_profile or {}).get('timezone') or company_defaults.get('timezone', 'America/Sao_Paulo'),
        },
        'company': {
            'name': account_company,
            'domain': company_defaults.get('company_domain', ''),
            'profile': role,
            'default_profile': company_defaults.get('default_profile', role),
            'default_language': company_defaults.get('default_language', 'pt-BR'),
            'signature_plain': signature,
            'logo': company_defaults.get('company_logo', ''),
        },
        'permissions': {
            'role': role,
            'allowed_agents': allowed_agents,
            'allowed_tools': allowed_tools,
            'allowed_actions': list(role_policy.get('allowed_actions') or []),
        },
        'credentials': {
            'context': context_name,
            'imap': imap_cfg,
            'smtp': smtp_cfg,
            'login_email': login,
            'company_name': account_company,
            'company_default': bool(company_defaults),
            'user_specific_profile': bool(user_profile),
            'user_specific_credentials': bool(user_credentials),
            'profile_source': 'user' if user_profile else ('company' if company_defaults else 'system'),
        },
        'resolved_from': {
            'headers': bool(headers),
            'user_profile': bool(user_profile),
            'user_credentials': bool(user_credentials),
            'company_defaults': bool(company_defaults),
            'system_defaults': True,
        },
        'configured': bool(login and account_company),
        'message': '' if login and account_company else 'Configuração de identidade pendente',
        'context': context_name,
    }
    if context_name == 'sales' and not resolve_email_context('sales')['configured']:
        identity['message'] = 'Vendas ainda não configurado'
    return identity


def permission_allowed(identity: dict[str, Any], capability: str) -> bool:
    perms = identity.get('permissions') if isinstance(identity, dict) else {}
    allowed_tools = set(str(item).lower() for item in (perms.get('allowed_tools') or []))
    allowed_actions = set(str(item).lower() for item in (perms.get('allowed_actions') or []))
    capability_key = normalize_text(str(capability)).lower()
    if '*' in allowed_tools or '*' in allowed_actions:
        return True
    return capability_key in allowed_tools or capability_key in allowed_actions


def audit_identity(identity: dict[str, Any], *, module: str, action: str, result: Any, tool: str = '') -> dict[str, Any]:
    ensure_storage()
    record = {
        'id': make_id('identity_audit'),
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'module': module,
        'action': action,
        'tool': tool,
        'user_email': identity.get('user', {}).get('login_email', ''),
        'user_name': identity.get('user', {}).get('name', ''),
        'profile': identity.get('permissions', {}).get('role', ''),
        'company': identity.get('company', {}).get('name', ''),
        'agent': (identity.get('permissions', {}).get('allowed_agents') or [''])[0],
        'result': result,
    }
    append_jsonl(IDENTITY_AUDIT_JSONL, record)
    diary = {
        'id': record['id'],
        'timestamp': record['created_at'],
        'module': module,
        'action': action,
        'summary': f"{action} by {record['user_email']}",
        'result': result,
        'learning': 'audit trail recorded',
    }
    append_jsonl(IDENTITY_DIARY_JSONL, diary)
    return record


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_storage()
    errors: list[str] = []
    for path in [USER_PROFILES_JSONL, USER_CREDENTIALS_JSONL, IDENTITY_AUDIT_JSONL, IDENTITY_DIARY_JSONL, IDENTITY_POLICY_PATH, COMPANY_DEFAULTS_PATH]:
        if not path.exists():
            errors.append(f'missing file: {path}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'user_profiles={count_jsonl(USER_PROFILES_JSONL)}')
    print(f'user_credentials={count_jsonl(USER_CREDENTIALS_JSONL)}')
    print(f'identity_audit={count_jsonl(IDENTITY_AUDIT_JSONL)}')
    return 0


def cmd_show_safe(args: argparse.Namespace) -> int:
    identity = resolve_identity(context=args.context, login_email=args.login_email, company=args.company, profile=args.profile)
    safe = {
        'ok': True,
        'context': args.context,
        'configured': identity.get('configured', False),
        'message': identity.get('message', ''),
        'user': {
            'name': identity['user'].get('name', ''),
            'login_email': _mask_sensitive('login_email', identity['user'].get('login_email', '')),
            'title': identity['user'].get('title', ''),
            'department': identity['user'].get('department', ''),
            'preferred_language': identity['user'].get('preferred_language', ''),
            'timezone': identity['user'].get('timezone', ''),
            'status': identity['user'].get('status', ''),
            'created_at': identity['user'].get('created_at', ''),
            'last_access_at': identity['user'].get('last_access_at', ''),
        },
        'company': identity['company'],
        'permissions': identity['permissions'],
        'credentials': {
            'context': identity['credentials']['context'],
            'imap': {
                'host': identity['credentials']['imap'].get('host'),
                'port': identity['credentials']['imap'].get('port'),
                'ssl': identity['credentials']['imap'].get('ssl'),
                'username': _mask_sensitive('imap_username', identity['credentials']['imap'].get('username', '')),
                'configured': identity['credentials']['imap'].get('configured'),
                'auth_type': identity['credentials']['imap'].get('auth_type'),
                'password_present': bool(identity['credentials']['imap'].get('password')),
                'password_source': identity['credentials']['imap'].get('password_source', ''),
            },
            'smtp': {
                'host': identity['credentials']['smtp'].get('host'),
                'port': identity['credentials']['smtp'].get('port'),
                'ssl': identity['credentials']['smtp'].get('ssl'),
                'username': _mask_sensitive('smtp_username', identity['credentials']['smtp'].get('username', '')),
                'configured': identity['credentials']['smtp'].get('configured'),
                'auth_type': identity['credentials']['smtp'].get('auth_type'),
                'password_present': bool(identity['credentials']['smtp'].get('password')),
                'password_source': identity['credentials']['smtp'].get('password_source', ''),
            },
            'profile_source': identity['credentials'].get('profile_source', ''),
        },
        'resolved_from': identity['resolved_from'],
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    identity = resolve_identity(context=args.context, login_email=args.login_email, company=args.company, profile=args.profile)
    print(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_list_users(_: argparse.Namespace) -> int:
    ensure_storage()
    print(json.dumps({'ok': True, 'count': count_jsonl(USER_PROFILES_JSONL), 'records': load_user_profiles()}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes identity resolver')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate identity registry and policy files')
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser('show-safe', help='Show masked identity snapshot')
    p_show.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_show.add_argument('--login-email', default='')
    p_show.add_argument('--company', default='')
    p_show.add_argument('--profile', default='')
    p_show.set_defaults(func=cmd_show_safe)

    p_resolve = sub.add_parser('resolve', help='Resolve identity snapshot as JSON')
    p_resolve.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_resolve.add_argument('--login-email', default='')
    p_resolve.add_argument('--company', default='')
    p_resolve.add_argument('--profile', default='')
    p_resolve.set_defaults(func=cmd_resolve)

    p_list = sub.add_parser('list-users', help='List registered user profiles')
    p_list.set_defaults(func=cmd_list_users)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
