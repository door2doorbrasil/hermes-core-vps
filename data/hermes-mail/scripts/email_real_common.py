#!/usr/bin/env python3
"""Shared helpers for the controlled real-email Hermes Mail flow.

This module centralizes settings loading, IMAP/SMTP connection helpers, MIME
parsing, local persistence, and light-weight classification/translation hooks.
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime, parseaddr
from pathlib import Path
from typing import Any

from reporting_utils import (
    CLIENT_QUOTES_JSONL,
    COTACOES_JSONL,
    EMAILS_JSONL,
    FORNECEDORES_JSONL,
    OPEN_WEBUI_ACTIONS_JSONL,
    OPEN_WEBUI_REQUESTS_JSONL,
    PRODUTOS_JSONL,
    RFQ_DRAFTS_JSONL,
    ROOT,
    append_jsonl,
    control_number,
    ensure_runtime_dirs,
    heuristically_extract_supplier_reply,
    latest_jsonl_record,
    load_json,
    load_jsonl_records,
    make_id,
    normalize_text,
    openai_json_completion,
    round_money,
    slugify,
    utc_now,
    write_json,
    get_env_secret,
)

STATE_DIR = ROOT / 'state'
LOGS_DIR = ROOT / 'logs'
EMAILS_DIR = ROOT / 'emails'
RAW_DIR = EMAILS_DIR / 'raw'
INCOMING_DIR = EMAILS_DIR / 'incoming'
OUTGOING_DIR = EMAILS_DIR / 'outgoing'
ATTACHMENTS_DIR = ROOT / 'attachments'
ATTACHMENTS_ORIGINAL_DIR = ATTACHMENTS_DIR / 'original'
ATTACHMENTS_EXTRACTED_DIR = ATTACHMENTS_DIR / 'extracted-text'
ATTACHMENTS_OCR_DIR = ATTACHMENTS_DIR / 'ocr'
APPROVAL_QUEUE_JSONL = STATE_DIR / 'approval-queue.jsonl'
IMAP_STATE_JSON = STATE_DIR / 'imap-state.json'
SMTP_STATE_JSON = STATE_DIR / 'smtp-state.json'

DEFAULT_IMAP_HOST = 'imap.hostinger.com'
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_HOST = 'smtp.hostinger.com'
DEFAULT_SMTP_PORT = 587
DEFAULT_EMAIL_ADDRESS = 'buyer@polarsinergy.com'
PLACEHOLDER_PASSWORD = 'PREENCHER_SENHA_REAL_AQUI'
SYSTEM_MODE = 'production_controlled'
MAILBOX_DEFAULT_FOLDERS = ('Inbox', 'Sent')

EMAIL_CONTEXTS: dict[str, dict[str, Any]] = {
    'buy': {
        'label': 'Compras',
        'company_name': 'Polar Sinergy LLC',
        'legal_name': 'Polar Sinergy LLC',
        'contact_name': 'Aluizio Andreatta',
        'email_address': 'buyer@polarsinergy.com',
        'phone': '+1 321 948 9126',
        'whatsapp': '+55 44 99156 9673',
        'website': 'https://www.polarsinergy.com',
        'identity_role': 'comprador internacional',
        'imap': {
            'host': 'BUY_IMAP_HOST',
            'port': 'BUY_IMAP_PORT',
            'ssl': 'BUY_IMAP_SSL',
            'username': 'BUY_IMAP_USERNAME',
            'password': 'BUY_IMAP_PASSWORD',
        },
        'smtp': {
            'host': 'BUY_SMTP_HOST',
            'port': 'BUY_SMTP_PORT',
            'ssl': 'BUY_SMTP_SSL',
            'username': 'BUY_SMTP_USERNAME',
            'password': 'BUY_SMTP_PASSWORD',
        },
        'legacy_password_fallbacks': ['email_password'],
    },
    'sales': {
        'label': 'Vendas',
        'company_name': 'D2D Representação Comercial Ltda',
        'legal_name': 'D2D Representação Comercial Ltda',
        'contact_name': 'Aluizio Andreatta',
        'email_address': 'aluizio@door2doorbrasil.com.br',
        'phone': '+55 44 99156-9673',
        'whatsapp': '+55 44 99156-9673',
        'website': '',
        'identity_role': 'vendas internacionais / representação comercial',
        'imap': {
            'host': 'SALES_IMAP_HOST',
            'port': 'SALES_IMAP_PORT',
            'ssl': 'SALES_IMAP_SSL',
            'username': 'SALES_IMAP_USERNAME',
            'password': 'SALES_IMAP_PASSWORD',
        },
        'smtp': {
            'host': 'SALES_SMTP_HOST',
            'port': 'SALES_SMTP_PORT',
            'ssl': 'SALES_SMTP_SSL',
            'username': 'SALES_SMTP_USERNAME',
            'password': 'SALES_SMTP_PASSWORD',
        },
        'legacy_password_fallbacks': [],
    },
}

SENSITIVE_ENV_MARKERS = ('PASSWORD', 'TOKEN', 'SECRET', 'KEY')


@dataclass(slots=True)
class ParsedAttachment:
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    path: str
    content_id: str | None = None


@dataclass(slots=True)
class ParsedEmail:
    message_id: str
    thread_id: str
    subject: str
    from_name: str
    from_email: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    date_iso: str | None
    body_text: str
    body_html: str | None
    attachments: list[ParsedAttachment]
    raw_sha256: str
    raw_path: str
    html_path: str | None


def now() -> str:
    return utc_now()


def load_settings() -> dict[str, Any]:
    data = load_json(STATE_DIR / 'settings.json')
    if not isinstance(data, dict):
        raise ValueError('settings.json must contain a JSON object')
    return data


def load_secrets() -> dict[str, Any]:
    data = load_json(STATE_DIR / 'secrets.json')
    if not isinstance(data, dict):
        raise ValueError('secrets.json must contain a JSON object')
    return data


def settings_mode() -> str:
    settings = load_settings()
    return str(settings.get('system_mode') or settings.get('email_config', {}).get('mode') or 'unknown')


def get_email_address() -> str:
    settings = load_settings()
    email_config = settings.get('email_config') if isinstance(settings, dict) else {}
    if isinstance(email_config, dict):
        return str(email_config.get('email_address') or DEFAULT_EMAIL_ADDRESS)
    return DEFAULT_EMAIL_ADDRESS


def get_imap_config() -> dict[str, Any]:
    settings = load_settings()
    imap = settings.get('imap_config')
    if isinstance(imap, dict):
        return imap
    email_config = settings.get('email_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(email_config, dict):
        email_config = {}
    return {
        'host': email_config.get('imap_host', DEFAULT_IMAP_HOST),
        'port': email_config.get('imap_port', DEFAULT_IMAP_PORT),
        'ssl': bool(email_config.get('imap_ssl', True)),
        'username': email_config.get('email_address', DEFAULT_EMAIL_ADDRESS),
        'read_only': True,
        'mode': email_config.get('mode', SYSTEM_MODE),
    }


def get_smtp_config() -> dict[str, Any]:
    settings = load_settings()
    smtp = settings.get('smtp_config')
    if isinstance(smtp, dict):
        return smtp
    email_config = settings.get('email_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(email_config, dict):
        email_config = {}
    return {
        'host': email_config.get('smtp_host', DEFAULT_SMTP_HOST),
        'port': email_config.get('smtp_port', DEFAULT_SMTP_PORT),
        'tls': bool(email_config.get('smtp_tls', True)),
        'username': email_config.get('email_address', DEFAULT_EMAIL_ADDRESS),
        'require_manual_approval': True,
        'mode': email_config.get('mode', SYSTEM_MODE),
    }


def get_rfq_config() -> dict[str, Any]:
    settings = load_settings()
    rfq = settings.get('rfq_config')
    if isinstance(rfq, dict):
        return rfq
    return {
        'language': 'en',
        'reply_translation_target': 'pt-BR',
        'require_manual_approval': True,
        'mode': SYSTEM_MODE,
    }


def normalize_email_context(context: str | None) -> str:
    value = normalize_text(str(context or '')).lower()
    if value in {'sales', 'sale', 'vendas', 'venda', 'd2d'}:
        return 'sales'
    return 'buy'


def _env_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(name: str) -> int | None:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _env_bool(name: str) -> bool | None:
    raw = _env_str(name)
    if raw is None:
        return None
    value = raw.lower()
    if value in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'n', 'off'}:
        return False
    return None


def _mask_sensitive_env(name: str, value: str | None) -> str:
    if not value:
        return ''
    upper = name.upper()
    if any(marker in upper for marker in SENSITIVE_ENV_MARKERS):
        return '***'
    if '@' in value:
        local, _, domain = value.partition('@')
        return f"{local[:1]}***@{domain[:1]}***"
    if len(value) <= 4:
        return '***'
    return f"{value[:2]}***{value[-2:]}"


def resolve_email_context(context: str | None = None) -> dict[str, Any]:
    context_name = normalize_email_context(context)
    profile = EMAIL_CONTEXTS[context_name]
    imap_env = profile['imap']
    smtp_env = profile['smtp']
    identity = {
        'company_name': profile['company_name'],
        'legal_name': profile['legal_name'],
        'contact_name': profile['contact_name'],
        'email_address': profile['email_address'],
        'phone': profile['phone'],
        'whatsapp': profile['whatsapp'],
        'website': profile['website'],
        'identity_role': profile['identity_role'],
    }

    imap_host = _env_str(imap_env['host']) or (DEFAULT_IMAP_HOST if context_name == 'buy' else None)
    imap_port = _env_int(imap_env['port']) or (DEFAULT_IMAP_PORT if context_name == 'buy' else None)
    imap_ssl = _env_bool(imap_env['ssl'])
    imap_username = _env_str(imap_env['username']) or (identity['email_address'] if context_name == 'buy' else None)
    imap_password = _env_str(imap_env['password'])
    imap_password_source = imap_env['password']
    if context_name == 'buy' and not imap_password:
        secrets = load_secrets()
        for legacy_key in profile.get('legacy_password_fallbacks', []):
            legacy_value = _env_str(legacy_key)
            if legacy_value:
                imap_password = legacy_value
                imap_password_source = legacy_key
                break
        if not imap_password:
            legacy_value = secrets.get('email_password')
            if isinstance(legacy_value, str) and legacy_value.strip():
                imap_password = legacy_value.strip()
                imap_password_source = 'state/secrets.json:email_password'
    smtp_host = _env_str(smtp_env['host']) or (DEFAULT_SMTP_HOST if context_name == 'buy' else None)
    smtp_port = _env_int(smtp_env['port']) or (DEFAULT_SMTP_PORT if context_name == 'buy' else None)
    smtp_ssl = _env_bool(smtp_env['ssl'])
    smtp_username = _env_str(smtp_env['username']) or (identity['email_address'] if context_name == 'buy' else None)
    smtp_password = _env_str(smtp_env['password'])
    smtp_password_source = smtp_env['password']
    if context_name == 'buy' and not smtp_password:
        secrets = load_secrets()
        for legacy_key in profile.get('legacy_password_fallbacks', []):
            legacy_value = _env_str(legacy_key)
            if legacy_value:
                smtp_password = legacy_value
                smtp_password_source = legacy_key
                break
        if not smtp_password:
            legacy_value = secrets.get('email_password')
            if isinstance(legacy_value, str) and legacy_value.strip():
                smtp_password = legacy_value.strip()
                smtp_password_source = 'state/secrets.json:email_password'

    imap_configured = bool(imap_host and imap_port and imap_username and imap_password)
    smtp_configured = bool(smtp_host and smtp_port and smtp_username and smtp_password)
    sales_unconfigured = context_name == 'sales' and not (imap_configured or smtp_configured)
    configured = bool(imap_configured or smtp_configured) and not sales_unconfigured

    missing_imap = [name for name, value in [('host', imap_host), ('port', imap_port), ('username', imap_username), ('password', imap_password)] if value in (None, '')]
    missing_smtp = [name for name, value in [('host', smtp_host), ('port', smtp_port), ('username', smtp_username), ('password', smtp_password)] if value in (None, '')]

    return {
        'context': context_name,
        'configured': configured,
        'not_configured_message': 'Vendas ainda não configurado' if sales_unconfigured else '',
        'identity': identity,
        'imap': {
            'host': imap_host,
            'port': imap_port,
            'ssl': bool(imap_ssl if imap_ssl is not None else True),
            'username': imap_username,
            'password': imap_password,
            'password_source': imap_password_source if imap_password else '',
            'configured': imap_configured,
            'missing': missing_imap,
            'auth_type': 'IMAP4_SSL + LOGIN' if bool(imap_ssl if imap_ssl is not None else True) else 'IMAP4 + LOGIN',
        },
        'smtp': {
            'host': smtp_host,
            'port': smtp_port,
            'ssl': bool(smtp_ssl if smtp_ssl is not None else True),
            'username': smtp_username,
            'password': smtp_password,
            'password_source': smtp_password_source if smtp_password else '',
            'configured': smtp_configured,
            'missing': missing_smtp,
            'auth_type': 'SMTP_SSL' if bool(smtp_ssl if smtp_ssl is not None else True) else 'SMTP + STARTTLS',
        },
        'sensitive_env': {
            name: _mask_sensitive_env(name, value)
            for name, value in sorted(os.environ.items())
            if any(marker in name.upper() for marker in SENSITIVE_ENV_MARKERS)
        },
    }


def get_email_context_snapshot(context: str | None = None) -> dict[str, Any]:
    return resolve_email_context(context)


def get_email_context_message(context: str | None = None) -> str:
    snapshot = resolve_email_context(context)
    if snapshot['context'] == 'sales' and not snapshot['configured']:
        return 'Vendas ainda não configurado'
    return ''


def load_mailboxes_config() -> list[dict[str, Any]]:
    settings = load_settings()
    mailboxes = settings.get('mailboxes')
    normalized: list[dict[str, Any]] = []
    if isinstance(mailboxes, list):
        for item in mailboxes:
            if not isinstance(item, dict):
                continue
            name = normalize_text(str(item.get('name') or '')).strip().lower()
            if not name:
                continue
            record = dict(item)
            record['name'] = name
            folders = item.get('folders')
            if isinstance(folders, list) and folders:
                record['folders'] = [str(folder).strip() for folder in folders if str(folder).strip()]
            else:
                record['folders'] = list(MAILBOX_DEFAULT_FOLDERS)
            record['read_only'] = bool(item.get('read_only', True))
            record['initial_sync_only'] = bool(item.get('initial_sync_only', False))
            record['primary'] = bool(item.get('primary', name == 'buyer'))
            normalized.append(record)
    if normalized:
        return normalized

    legacy = resolve_email_context('buy')
    return [{
        'name': 'buyer',
        'primary': True,
        'folders': list(MAILBOX_DEFAULT_FOLDERS),
        'read_only': True,
        'initial_sync_only': False,
        'imap_host': legacy['imap']['host'],
        'imap_port': legacy['imap']['port'],
        'imap_ssl': legacy['imap']['ssl'],
        'username': legacy['imap']['username'],
        'password_key': 'email_password',
        'password_source': 'state/secrets.json',
    }]


def resolve_mailbox_password(mailbox: dict[str, Any]) -> tuple[str, str]:
    password = mailbox.get('password')
    if isinstance(password, str) and password.strip():
        return password.strip(), 'mailboxes[].password'

    password_key = str(mailbox.get('password_key') or '').strip()
    password_source = str(mailbox.get('password_source') or '').strip()
    if password_key or 'secrets.json' in password_source:
        key = password_key or 'email_password'
        secrets = load_secrets()
        value = secrets.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), f'state/secrets.json:{key}'

    password_env = str(mailbox.get('password_env') or '').strip()
    if password_env:
        env_value = _env_str(password_env)
        if env_value:
            return env_value, f'env:{password_env}'
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', password_env):
            return password_env, 'mailboxes[].password_env'

    return '', ''


def resolve_imap_folder_name(client: imaplib.IMAP4_SSL | imaplib.IMAP4, desired_folder: str) -> str:
    desired = str(desired_folder or '').strip()
    if not desired:
        return desired_folder
    try:
        typ, data = client.list()
    except Exception:
        return desired
    if typ != 'OK' or not data:
        return desired
    candidates: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            line = item.decode('utf-8', errors='ignore')
        else:
            line = str(item)
        line = line.strip()
        if not line:
            continue
        match = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line)
        if match:
            candidates.append(match.group(1).replace('\\"', '"').replace('\\\\', '\\'))
            continue
        parts = line.split()
        if parts:
            candidates.append(parts[-1].strip('"'))
    desired_folded = desired.casefold()
    exact_matches = [candidate for candidate in candidates if candidate.casefold() == desired_folded]
    if exact_matches:
        return exact_matches[0]
    suffix_matches: list[str] = []
    for candidate in candidates:
        normalized_candidate = candidate.replace('/', '.').replace('\\', '.')
        tail = normalized_candidate.rsplit('.', 1)[-1].casefold()
        if tail == desired_folded:
            suffix_matches.append(candidate)
    if suffix_matches:
        suffix_matches.sort(key=len)
        return suffix_matches[0]
    return desired


def connect_mailbox_imap(mailbox: dict[str, Any], *, folder: str = 'INBOX', readonly: bool = True) -> tuple[imaplib.IMAP4_SSL, dict[str, Any]]:
    host = str(mailbox.get('imap_host') or mailbox.get('host') or DEFAULT_IMAP_HOST).strip()
    port = int(mailbox.get('imap_port') or mailbox.get('port') or DEFAULT_IMAP_PORT)
    ssl_enabled = bool(mailbox.get('imap_ssl', mailbox.get('ssl', True)))
    username = str(mailbox.get('username') or mailbox.get('email_address') or '').strip()
    password, password_source = resolve_mailbox_password(mailbox)
    if not host or not port or not username or not password:
        raise RuntimeError(f"missing IMAP configuration for mailbox '{mailbox.get('name') or 'unknown'}'")
    client: imaplib.IMAP4_SSL | imaplib.IMAP4
    if ssl_enabled:
        client = imaplib.IMAP4_SSL(host, port)
    else:
        client = imaplib.IMAP4(host, port)
    client.login(username, password)
    resolved_folder = resolve_imap_folder_name(client, folder)
    typ, data = client.select(resolved_folder, readonly=readonly)
    if typ != 'OK':
        try:
            client.logout()
        except Exception:
            pass
        raise RuntimeError(f'IMAP select failed for {resolved_folder}: {typ}')
    info = {
        'mailbox': str(mailbox.get('name') or '').strip().lower() or 'buyer',
        'folder': resolved_folder,
        'folder_requested': folder,
        'host': host,
        'port': port,
        'username': username,
        'readonly': readonly,
        'message_count': int(data[0]) if data and data[0] else 0,
        'ssl': ssl_enabled,
        'password_source': password_source,
        'initial_sync_only': bool(mailbox.get('initial_sync_only', False)),
        'read_only': bool(mailbox.get('read_only', True)),
    }
    return client, info


def ensure_storage() -> None:
    ensure_runtime_dirs()
    for path in [
        RAW_DIR,
        INCOMING_DIR,
        OUTGOING_DIR,
        ATTACHMENTS_ORIGINAL_DIR,
        ATTACHMENTS_EXTRACTED_DIR,
        ATTACHMENTS_OCR_DIR,
        LOGS_DIR,
        STATE_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    for path in [
        EMAILS_JSONL,
        FORNECEDORES_JSONL,
        ROOT / 'contatos.jsonl',
        PRODUTOS_JSONL,
        COTACOES_JSONL,
        RFQ_DRAFTS_JSONL,
        APPROVAL_QUEUE_JSONL,
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


def load_jsonl_safe(path: Path) -> list[dict[str, Any]]:
    return load_jsonl_records(path)


def latest_record(path: Path, predicate: Any | None = None) -> dict[str, Any] | None:
    return latest_jsonl_record(path, predicate)


def append_record(path: Path, record: dict[str, Any]) -> Path:
    return append_jsonl(path, record)


def write_state_json(path: Path, payload: dict[str, Any]) -> Path:
    return write_json(path, payload)


def mask_email(value: str | None) -> str:
    if not value:
        return ''
    local, _, domain = value.partition('@')
    if not domain:
        return f'{value[:2]}***'
    return f'{local[:1]}***@{domain[:1]}***'


def normalize_message_id(value: str | None) -> str:
    text = normalize_text(value or '')
    if not text:
        return ''
    if text.startswith('<') and text.endswith('>'):
        return text
    return f'<{text}>'


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ''
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return normalize_text(value)


def split_addresses(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: list[str] = []
    for name, addr in getaddresses(values):
        addr = normalize_text(addr)
        if addr and addr not in seen:
            seen.append(addr)
    return seen


def parsedate_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    try:
        return dt.astimezone().isoformat().replace('+00:00', 'Z')
    except Exception:
        return dt.isoformat().replace('+00:00', 'Z')


def message_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _html_to_text(html_body: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', html_body)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'&amp;', '&', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return normalize_text(text)


def _decode_bytes(payload: bytes, charset: str | None) -> str:
    for encoding in filter(None, [charset, 'utf-8', 'latin-1']):
        try:
            return payload.decode(encoding, errors='replace')
        except Exception:
            continue
    return payload.decode('utf-8', errors='replace')


def _safe_filename(name: str, fallback: str) -> str:
    text = normalize_text(name or '')
    if not text:
        text = fallback
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('_')
    return text or fallback


def _first_reference(values: str | None) -> str:
    if not values:
        return ''
    parts = re.findall(r'<[^>]+>', values)
    if parts:
        return parts[0]
    first = normalize_message_id(values)
    return first


def parse_email_bytes(raw_bytes: bytes, *, uid: int | str | None = None) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    message_id = normalize_message_id(msg.get('Message-ID')) or normalize_message_id(f'{message_hash(raw_bytes)}@local')
    thread_id = _first_reference(msg.get('In-Reply-To')) or _first_reference(msg.get('References')) or message_id
    subject = decode_mime_header(msg.get('Subject'))
    from_name, from_email = parseaddr(msg.get('From') or '')
    from_name = decode_mime_header(from_name) or normalize_text(from_name)
    from_email = normalize_text(from_email)
    to = split_addresses(msg.get_all('To', []))
    cc = split_addresses(msg.get_all('Cc', []))
    bcc = split_addresses(msg.get_all('Bcc', []))
    date_iso = parsedate_iso(msg.get('Date'))

    body_text_parts: list[str] = []
    body_html_parts: list[str] = []
    attachments: list[ParsedAttachment] = []
    raw_sha = message_hash(raw_bytes)
    message_key = slugify(message_id.strip('<>'), 48) or slugify(raw_sha[:16], 48)
    raw_path = RAW_DIR / f'{message_key}.eml'
    raw_path.write_bytes(raw_bytes)
    html_path: str | None = None
    text_path = INCOMING_DIR / f'{message_key}.txt'

    if msg.is_multipart():
        for index, part in enumerate(msg.walk()):
            if part.is_multipart():
                continue
            content_disposition = part.get_content_disposition()
            content_type = part.get_content_type()
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b''
            charset = part.get_content_charset()
            if content_disposition == 'attachment' or filename:
                safe_name = _safe_filename(decode_mime_header(filename) or filename or f'attachment-{index}', f'attachment-{index}')
                attachment_dir = ATTACHMENTS_ORIGINAL_DIR / message_key
                attachment_dir.mkdir(parents=True, exist_ok=True)
                attachment_path = attachment_dir / safe_name
                attachment_path.write_bytes(payload)
                attachments.append(ParsedAttachment(
                    filename=safe_name,
                    mime_type=content_type,
                    size_bytes=len(payload),
                    sha256=message_hash(payload),
                    path=str(attachment_path),
                    content_id=normalize_text(part.get('Content-ID') or '') or None,
                ))
                continue
            if content_type == 'text/plain':
                body_text_parts.append(_decode_bytes(payload, charset))
            elif content_type == 'text/html':
                html_text = _decode_bytes(payload, charset)
                body_html_parts.append(html_text)
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset()
            content_type = msg.get_content_type()
            if content_type == 'text/html':
                body_html_parts.append(_decode_bytes(payload, charset))
            else:
                body_text_parts.append(_decode_bytes(payload, charset))
        else:
            text = str(payload or '')
            if msg.get_content_type() == 'text/html':
                body_html_parts.append(text)
            else:
                body_text_parts.append(text)

    body_html = '\n\n'.join(part for part in body_html_parts if part).strip() or None
    if body_html and not body_text_parts:
        body_text_parts.append(_html_to_text(body_html))
    body_text = normalize_text('\n'.join(part.strip() for part in body_text_parts if part).strip())
    if not body_text and body_html:
        body_text = _html_to_text(body_html)

    if body_text:
        text_path.write_text(body_text, encoding='utf-8')
    if body_html:
        html_path = str(INCOMING_DIR / f'{message_key}.html')
        Path(html_path).write_text(body_html, encoding='utf-8')

    return ParsedEmail(
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        from_name=from_name,
        from_email=from_email,
        to=to,
        cc=cc,
        bcc=bcc,
        date_iso=date_iso,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        raw_sha256=raw_sha,
        raw_path=str(raw_path),
        html_path=html_path,
    )


def connect_imap(*, context: str | None = None, host: str | None = None, port: int | None = None, username: str | None = None, password: str | None = None, readonly: bool = True) -> tuple[imaplib.IMAP4_SSL, dict[str, Any]]:
    ctx = resolve_email_context(context)
    imap_cfg = ctx['imap']
    if ctx['context'] == 'sales' and not ctx['configured']:
        raise RuntimeError('Vendas ainda não configurado')
    host = host or str(imap_cfg.get('host') or '')
    port = int(port or imap_cfg.get('port') or 0)
    username = username or str(imap_cfg.get('username') or '')
    password = password or str(imap_cfg.get('password') or '')
    if not host or not port or not username or not password:
        if ctx['context'] == 'sales':
            raise RuntimeError('Vendas ainda não configurado')
        raise RuntimeError('missing BUY_* IMAP configuration or BUY_IMAP_PASSWORD')
    client = imaplib.IMAP4_SSL(host, port)
    client.login(username, password)
    typ, data = client.select('INBOX', readonly=readonly)
    if typ != 'OK':
        client.logout()
        raise RuntimeError(f'IMAP select failed: {typ}')
    info = {
        'context': ctx['context'],
        'identity': ctx['identity'],
        'host': host,
        'port': port,
        'username': username,
        'readonly': readonly,
        'mailbox': 'INBOX',
        'message_count': int(data[0]) if data and data[0] else 0,
        'mode': settings_mode(),
        'auth_type': imap_cfg.get('auth_type') or 'IMAP4_SSL + LOGIN',
        'configured': bool(imap_cfg.get('configured')),
        'password_source': imap_cfg.get('password_source') or '',
    }
    return client, info


def search_uids(client: imaplib.IMAP4_SSL, query: str = 'ALL') -> list[int]:
    typ, data = client.uid('SEARCH', None, query)
    if typ != 'OK' or not data or not data[0]:
        return []
    raw = data[0]
    if isinstance(raw, bytes):
        items = raw.decode('utf-8', errors='ignore').split()
    else:
        items = str(raw).split()
    uids: list[int] = []
    for item in items:
        try:
            uids.append(int(item))
        except ValueError:
            continue
    return uids


def fetch_uid_raw(client: imaplib.IMAP4_SSL, uid: int) -> bytes:
    typ, data = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
    if typ != 'OK' or not data:
        raise RuntimeError(f'IMAP fetch failed for UID {uid}')
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    raise RuntimeError(f'IMAP fetch returned no message bytes for UID {uid}')


def existing_email_by_message_id(message_id: str) -> dict[str, Any] | None:
    message_id = normalize_message_id(message_id)
    if not message_id:
        return None
    return latest_record(EMAILS_JSONL, lambda rec: normalize_message_id(str(rec.get('message_id') or '')) == message_id)


def existing_email_by_uid(uid: int) -> dict[str, Any] | None:
    return latest_record(EMAILS_JSONL, lambda rec: int(rec.get('imap_uid') or -1) == uid)


def ensure_translation_ptbr(text: str, *, source_hint: str = '') -> str:
    text = normalize_text(text)
    if not text:
        return ''
    if not re.search(r'[A-Za-z]', text):
        return text
    payload = openai_json_completion(
        system_prompt='Traduza para pt-BR sem adicionar explicações. Retorne JSON com a chave translated_text.',
        user_prompt=json.dumps({'text': text, 'source_hint': source_hint}, ensure_ascii=False),
        temperature=0.0,
    )
    if isinstance(payload, dict):
        translated = normalize_text(str(payload.get('translated_text') or payload.get('translation') or ''))
        if translated:
            return translated
    return text


def supplier_identity_from_email(from_name: str, from_email: str) -> dict[str, Any]:
    from_name = normalize_text(from_name)
    from_email = normalize_text(from_email).lower()
    local, _, domain = from_email.partition('@')
    supplier_name = from_name or (domain.split('.', 1)[0].replace('-', ' ').title() if domain else local.replace('.', ' ').title()) or 'Fornecedor não informado'
    website = f'https://{domain}' if domain and not any(domain.endswith(f'@{free}') for free in {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com'}) else None
    return {
        'name': supplier_name,
        'legal_name': supplier_name,
        'emails': [from_email] if from_email else [],
        'website': website,
        'domain': domain or None,
        'status': 'prospect',
    }


def contact_identity_from_email(from_name: str, from_email: str, supplier_id: str) -> dict[str, Any]:
    contact_name = normalize_text(from_name) or normalize_text(from_email.split('@', 1)[0].replace('.', ' ').title()) or 'Contato não informado'
    return {
        'name': contact_name,
        'role': 'Sales',
        'email': normalize_text(from_email).lower(),
        'phone': None,
        'preferred_channel': 'email',
        'status': 'active',
        'supplier_id': supplier_id,
    }


def product_identity_from_email(subject: str, body_text: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    analysis = analysis or {}
    product_name = normalize_text(str(analysis.get('product_name') or analysis.get('description') or ''))
    if not product_name:
        subject_clean = normalize_text(subject)
        subject_clean = re.sub(r'^(re|fw|fwd):\s*', '', subject_clean, flags=re.IGNORECASE)
        product_name = subject_clean[:120] if subject_clean else 'Produto não identificado'
    description = normalize_text(str(analysis.get('product_description') or body_text[:400] or ''))
    return {
        'name': product_name,
        'sku': slugify(product_name, 24).upper(),
        'description': description or product_name,
        'unit': 'un',
        'category': analysis.get('category') or 'email-import',
        'brand': analysis.get('brand') or 'N/A',
        'aliases': [product_name] if product_name else [],
        'active': True,
    }


def parse_reply_insights(body_text: str) -> dict[str, Any]:
    extracted = heuristically_extract_supplier_reply(body_text)
    translated = extracted.get('translated_reply_ptbr') or ensure_translation_ptbr(body_text, source_hint='supplier_reply')
    extracted['translated_reply_ptbr'] = translated
    return extracted


def recent_incoming_emails() -> list[dict[str, Any]]:
    return [rec for rec in load_jsonl_safe(EMAILS_JSONL) if rec.get('direction') == 'incoming']


def recent_outgoing_drafts() -> list[dict[str, Any]]:
    return [rec for rec in load_jsonl_safe(EMAILS_JSONL) if rec.get('direction') == 'outgoing' and rec.get('status') in {'draft', 'draft_pending_approval', 'draft_queued'}]


def list_pending_queue() -> list[dict[str, Any]]:
    return [rec for rec in load_jsonl_safe(APPROVAL_QUEUE_JSONL) if rec.get('status') in {'pending', 'pending_approval', 'queued'}]


def upsert_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    record = dict(item)
    record.setdefault('id', make_id('approval_item'))
    record.setdefault('created_at', now())
    record['updated_at'] = now()
    append_record(APPROVAL_QUEUE_JSONL, record)
    return record


def update_cursor(**updates: Any) -> dict[str, Any]:
    current = {
        'version': '0.1.0',
        'last_processed_email_id': None,
        'last_processed_message_id': None,
        'last_ingest_at': None,
        'last_sync_at': None,
        'last_attachment_id': None,
        'state': 'idle',
        'updated_at': now(),
    }
    if (STATE_DIR / 'cursor.json').exists():
        try:
            loaded = load_json(STATE_DIR / 'cursor.json')
            if isinstance(loaded, dict):
                current.update(loaded)
        except Exception:
            pass
    current.update(updates)
    current['updated_at'] = now()
    write_state_json(STATE_DIR / 'cursor.json', current)
    return current


def update_ingest_state(**updates: Any) -> dict[str, Any]:
    current = {
        'version': '0.1.0',
        'status': 'idle',
        'running': False,
        'last_run_at': None,
        'last_success_at': None,
        'last_error_at': None,
        'last_error': None,
        'processed_counts': {'emails': 0, 'attachments': 0, 'quotes': 0, 'suppliers': 0, 'contacts': 0, 'products': 0},
        'queue': {'pending_emails': 0, 'pending_attachments': 0, 'pending_quotes': 0},
        'locks': {'ingest': False, 'email_poll': False, 'attachment_extract': False},
        'metrics': {'emails_seen': 0, 'emails_saved': 0, 'emails_failed': 0, 'attachments_saved': 0, 'attachments_failed': 0, 'quotes_extracted': 0},
        'updated_at': now(),
    }
    if (STATE_DIR / 'ingest-state.json').exists():
        try:
            loaded = load_json(STATE_DIR / 'ingest-state.json')
            if isinstance(loaded, dict):
                current.update(loaded)
        except Exception:
            pass
    current.update(updates)
    current['updated_at'] = now()
    write_state_json(STATE_DIR / 'ingest-state.json', current)
    return current


def imap_state_snapshot(**updates: Any) -> dict[str, Any]:
    current = {
        'version': '0.1.0',
        'host': DEFAULT_IMAP_HOST,
        'port': DEFAULT_IMAP_PORT,
        'mailbox': 'INBOX',
        'readonly': True,
        'last_uid': None,
        'last_message_id': None,
        'message_count': 0,
        'unread_count': 0,
        'last_sync_at': None,
        'updated_at': now(),
    }
    if IMAP_STATE_JSON.exists():
        try:
            loaded = load_json(IMAP_STATE_JSON)
            if isinstance(loaded, dict):
                current.update(loaded)
        except Exception:
            pass
    current.update(updates)
    current['updated_at'] = now()
    write_state_json(IMAP_STATE_JSON, current)
    return current


def smtp_state_snapshot(**updates: Any) -> dict[str, Any]:
    current = {
        'version': '0.1.0',
        'host': DEFAULT_SMTP_HOST,
        'port': DEFAULT_SMTP_PORT,
        'tls': True,
        'last_draft_id': None,
        'last_rfq_id': None,
        'last_thread_id': None,
        'last_message_id': None,
        'last_send_at': None,
        'updated_at': now(),
    }
    if SMTP_STATE_JSON.exists():
        try:
            loaded = load_json(SMTP_STATE_JSON)
            if isinstance(loaded, dict):
                current.update(loaded)
        except Exception:
            pass
    current.update(updates)
    current['updated_at'] = now()
    write_state_json(SMTP_STATE_JSON, current)
    return current


def connect_smtp(*, context: str | None = None, host: str | None = None, port: int | None = None, username: str | None = None, password: str | None = None, tls: bool = True) -> smtplib.SMTP:
    ctx = resolve_email_context(context)
    smtp_cfg = ctx['smtp']
    if ctx['context'] == 'sales' and not ctx['configured']:
        raise RuntimeError('Vendas ainda não configurado')
    host = host or str(smtp_cfg.get('host') or '')
    port = int(port or smtp_cfg.get('port') or 0)
    username = username or str(smtp_cfg.get('username') or '')
    password = password or str(smtp_cfg.get('password') or '')
    if not host or not port or not username or not password:
        if ctx['context'] == 'sales':
            raise RuntimeError('Vendas ainda não configurado')
        raise RuntimeError('missing BUY_* SMTP configuration or BUY_SMTP_PASSWORD')
    client = smtplib.SMTP(host, port, timeout=60)
    client.ehlo()
    if tls and not bool(smtp_cfg.get('ssl') is False):
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
    client.login(username, password)
    return client


def message_from_email_record(record: dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    message['From'] = record.get('from') or DEFAULT_EMAIL_ADDRESS
    if record.get('to'):
        message['To'] = ', '.join(record.get('to') or [])
    if record.get('cc'):
        message['Cc'] = ', '.join(record.get('cc') or [])
    if record.get('subject'):
        message['Subject'] = str(record.get('subject'))
    if record.get('message_id'):
        message['Message-ID'] = normalize_message_id(str(record.get('message_id'))) or str(record.get('message_id'))
    if record.get('thread_id'):
        thread_id = normalize_message_id(str(record.get('thread_id'))) or str(record.get('thread_id'))
        message['In-Reply-To'] = thread_id
        message['References'] = thread_id
    message.set_content(record.get('body_text') or '')
    return message


def email_record_exists(message_id: str) -> bool:
    return latest_record(EMAILS_JSONL, lambda rec: normalize_message_id(str(rec.get('message_id') or '')) == normalize_message_id(message_id)) is not None


def queue_item_exists(item_id: str) -> dict[str, Any] | None:
    return latest_record(APPROVAL_QUEUE_JSONL, lambda rec: rec.get('id') == item_id or rec.get('draft_id') == item_id or rec.get('rfq_id') == item_id)


def notify_telegram(event_type: str, text: str, *, metadata: dict[str, Any] | None = None, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    telegram_cfg = settings.get('telegram_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(telegram_cfg, dict):
        telegram_cfg = {}
    secrets = load_secrets()
    token = str(secrets.get('telegram_bot_token') or secrets.get('telegram_token') or '').strip()
    chat_id = str(telegram_cfg.get('chat_id') or '').strip()
    mode = str(telegram_cfg.get('mode') or 'log_only').strip().casefold()
    payload = {
        'timestamp': now(),
        'event_type': event_type,
        'message': normalize_text(text),
        'metadata': metadata or {},
        'sent': False,
        'mode': mode or 'log_only',
        'operation_mode': 'production_controlled',
    }
    log_path = ROOT / 'logs' / 'telegram-notifications.jsonl'
    append_record(log_path, payload)
    allow_real_send = bool(telegram_cfg.get('enabled')) and bool(token) and bool(chat_id) and mode in {'production', 'real'} and bool(telegram_cfg.get('allow_real_send'))
    if not allow_real_send:
        payload['delivery'] = 'log_only'
        return payload
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data_fields = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': 'true'}
        if reply_markup is not None:
            data_fields['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
        body = urllib.parse.urlencode(data_fields).encode('utf-8')
        request = urllib.request.Request(url, data=body, method='POST')
        with urllib.request.urlopen(request, timeout=30) as response:
            payload['sent'] = True
            payload['delivery'] = 'telegram'
            payload['telegram_status'] = getattr(response, 'status', 200)
            payload['telegram_body'] = response.read().decode('utf-8', errors='replace')
    except Exception as exc:
        payload['error'] = str(exc)
    return payload
