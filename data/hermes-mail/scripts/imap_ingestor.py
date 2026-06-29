#!/usr/bin/env python3
"""Multi-mailbox IMAP ingestor for Hermes Mail.

Phase 2 behavior:
- supports multiple mailboxes from state/settings.json -> mailboxes
- buyer runs permanently; sales is initial_sync_only
- read-only IMAP only
- INBOX and Sent folders
- subject filter limited to quotation/RFQ-related subjects
- no flag changes, no deletes, no moves, no mark-as-read
- dedupe by Message-ID only
- saves raw .eml, attachments, and JSONL ledgers
- creates per-task approval requests before any downstream action
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from approval_queue import build_approval_request_message
from email_real_common import (
    EMAILS_JSONL as LEGACY_EMAILS_JSONL,
    MAILBOX_DEFAULT_FOLDERS,
    ROOT,
    append_record,
    connect_mailbox_imap,
    ensure_storage,
    fetch_uid_raw,
    get_email_context_snapshot,
    imap_state_snapshot,
    load_jsonl_safe,
    load_mailboxes_config,
    load_settings,
    mask_email,
    now,
    notify_telegram,
    parse_email_bytes,
    search_uids,
    settings_mode,
    update_cursor,
    update_ingest_state,
    normalize_message_id,
)
from reporting_utils import count_jsonl

try:
    from hermes_memory import log_action as log_memory_action
except Exception:  # pragma: no cover - optional integration
    log_memory_action = None

DATA_DIR = ROOT / 'data'
DATA_EMAILS_JSONL = DATA_DIR / 'emails.jsonl'
DATA_MANUAL_REVIEW_QUEUE_JSONL = DATA_DIR / 'manual-review-queue.jsonl'
DATA_TASK_APPROVALS_JSONL = DATA_DIR / 'task_approvals.jsonl'
AUTOMATION_POLICY_JSON = ROOT / 'config' / 'automation_policy.json'
MAILBOX_STATE_JSON = ROOT / 'state' / 'imap-state.json'
ANEXOS_JSONL = ROOT / 'anexos.jsonl'

SUBJECT_TERMS = [
    'quotation',
    'quote',
    'cotacao',
    'cotação',
    'rfq',
    'proposta',
    'offer',
    'price',
]

TASK_TYPE_PROPOSALS: dict[str, str] = {
    'quotation_review': 'Revisar o e-mail de cotação e confirmar os próximos passos.',
    'register_supplier': 'Cadastrar o fornecedor detectado no e-mail.',
    'register_contact': 'Cadastrar o contato detectado no e-mail.',
    'register_product': 'Cadastrar o produto detectado no e-mail.',
    'create_sourcing_project': 'Criar um projeto de sourcing a partir desta oportunidade.',
    'generate_rfq_draft': 'Gerar um RFQ draft em modo dry-run para aprovação.',
    'link_response_to_rfq': 'Vincular a resposta ao RFQ correspondente.',
    'generate_quote_analysis': 'Gerar uma análise de cotação para revisão humana.',
    'generate_client_quote_pdf': 'Gerar um PDF de cotação ao cliente em modo controlado.',
    'send_real_email': 'Enviar um e-mail real somente após aprovação explícita.',
}


def default_policy() -> dict[str, Any]:
    return {
        'version': '0.2.0',
        'imap': {
            'read_only': True,
            'mailboxes_enabled': True,
            'initial_subject_terms': SUBJECT_TERMS,
            'folders': list(MAILBOX_DEFAULT_FOLDERS),
        },
        'telegram': {
            'approval_requests_required': True,
            'approval_format': 'Hermes Mail - Aprovação necessária',
        },
        'smtp': {
            'real_send_blocked_by_default': True,
            'real_send_requires_explicit_approval': True,
            'approve_send_command': 'approve-send <draft_id>',
        },
        'task_types': {},
        'auto_after_first_approval': False,
    }


def ensure_local_storage() -> None:
    ensure_storage()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_EMAILS_JSONL.touch(exist_ok=True)
    DATA_MANUAL_REVIEW_QUEUE_JSONL.touch(exist_ok=True)
    DATA_TASK_APPROVALS_JSONL.touch(exist_ok=True)
    if not AUTOMATION_POLICY_JSON.exists():
        AUTOMATION_POLICY_JSON.write_text(json.dumps(default_policy(), ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def load_json(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return payload


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')


def validate_jsonl(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        errors.append(f'missing file: {path}')
        return errors
    try:
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                json.loads(line)
    except json.JSONDecodeError as exc:
        errors.append(f'invalid JSONL in {path}: {exc}')
    except OSError as exc:
        errors.append(f'read error {path}: {exc}')
    return errors


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(policy.get('imap'), dict):
        errors.append('policy.imap must be an object')
    else:
        imap = policy['imap']
        if imap.get('read_only') is not True:
            errors.append('policy.imap.read_only must be true')
        if not isinstance(imap.get('initial_subject_terms'), list) or not imap.get('initial_subject_terms'):
            errors.append('policy.imap.initial_subject_terms must be a non-empty list')
    if not isinstance(policy.get('telegram'), dict):
        errors.append('policy.telegram must be an object')
    if not isinstance(policy.get('smtp'), dict):
        errors.append('policy.smtp must be an object')
    if not isinstance(policy.get('task_types', {}), dict):
        errors.append('policy.task_types must be an object')
    if not isinstance(policy.get('auto_after_first_approval'), bool):
        errors.append('policy.auto_after_first_approval must be boolean')
    return errors


def fold_text(value: str | None) -> str:
    if not value:
        return ''
    text = unicodedata.normalize('NFKD', value)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_contact(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def canonical_subject(subject: str | None) -> str:
    text = fold_text(subject)
    text = re.sub(r'^(re|fw|fwd|res|rv|aw|tr|sv|enc)\s*:\s*', '', text)
    return text.strip()


def address_set_from_record(record: dict[str, Any]) -> set[str]:
    addresses: set[str] = set()
    for value in [record.get('from_email') or record.get('from')]:
        text = normalize_contact(value)
        if text:
            addresses.add(text)
    for field in ('to', 'cc', 'bcc'):
        value = record.get(field)
        if isinstance(value, list):
            for item in value:
                text = normalize_contact(item)
                if text:
                    addresses.add(text)
        elif isinstance(value, str):
            text = normalize_contact(value)
            if text:
                addresses.add(text)
    return addresses


def conversation_key_for_email(*, message_id: str, thread_id: str, subject: str, from_email: str, to: list[str], cc: list[str], bcc: list[str]) -> str:
    current_message_id = normalize_message_id(message_id)
    current_thread_key = normalize_message_id(thread_id)
    current_subject = canonical_subject(subject)
    current_addresses = {item for item in [normalize_contact(from_email), *{normalize_contact(item) for item in [*to, *cc, *bcc]}] if item}

    matches: list[dict[str, Any]] = []
    for candidate in _all_email_records():
        candidate_message_id = normalize_message_id(candidate.get('message_id'))
        candidate_thread_id = normalize_message_id(candidate.get('thread_id'))
        candidate_subject = canonical_subject(str(candidate.get('subject') or ''))
        candidate_addresses = address_set_from_record(candidate)
        candidate_conversation_key = normalize_contact(candidate.get('conversation_key') or '')

        matched = False
        if current_thread_key and candidate_message_id and current_thread_key == candidate_message_id:
            matched = True
        elif current_message_id and candidate_thread_id and current_message_id == candidate_thread_id:
            matched = True
        elif current_subject and candidate_subject and current_subject == candidate_subject and current_addresses.intersection(candidate_addresses):
            matched = True
        elif candidate_conversation_key and current_thread_key and candidate_conversation_key == f'thread:{current_thread_key}':
            matched = True

        if matched:
            matches.append(candidate)

    if matches:
        matches.sort(key=lambda item: str(item.get('received_at') or item.get('sent_at') or item.get('created_at') or ''))
        anchor = matches[0]
        anchor_key = normalize_contact(anchor.get('conversation_key') or '')
        if anchor_key:
            return anchor_key
        anchor_thread_key = normalize_message_id(anchor.get('thread_id'))
        if anchor_thread_key:
            return f'thread:{anchor_thread_key}'
        anchor_message_id = normalize_message_id(anchor.get('message_id'))
        if anchor_message_id:
            return f'thread:{anchor_message_id}'
        anchor_subject = canonical_subject(str(anchor.get('subject') or ''))
        if anchor_subject:
            anchor_addresses = address_set_from_record(anchor)
            participants = sorted({normalize_contact(from_email), *current_addresses, *anchor_addresses})
            participants = [item for item in participants if item]
            if participants:
                return f"subject:{anchor_subject}|participants:{'|'.join(participants)}"
            return f'subject:{anchor_subject}'

    if current_thread_key:
        return f'thread:{current_thread_key}'
    if current_subject:
        participants = sorted(current_addresses)
        if participants:
            return f"subject:{current_subject}|participants:{'|'.join(participants)}"
        return f'subject:{current_subject}'
    return current_message_id or ''


def _all_email_records() -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for path in (LEGACY_EMAILS_JSONL, DATA_EMAILS_JSONL):
        for rec in load_jsonl_safe(path):
            if not isinstance(rec, dict):
                continue
            rec_id = str(rec.get('id') or '').strip()
            if rec_id and rec_id in seen_ids:
                continue
            if rec_id:
                seen_ids.add(rec_id)
            records.append(rec)
    return records


def related_email_summaries(record: dict[str, Any]) -> list[dict[str, Any]]:
    current_id = str(record.get('id') or '').strip()
    current_message_id = normalize_message_id(record.get('message_id'))
    current_thread_id = normalize_message_id(record.get('thread_id'))
    current_subject = canonical_subject(str(record.get('subject') or ''))
    current_addresses = address_set_from_record(record)
    current_conversation_key = normalize_contact(str(record.get('conversation_key') or ''))

    related: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in _all_email_records():
        candidate_id = str(candidate.get('id') or '').strip()
        if not candidate_id or candidate_id == current_id or candidate_id in seen_ids:
            continue
        candidate_message_id = normalize_message_id(candidate.get('message_id'))
        candidate_thread_id = normalize_message_id(candidate.get('thread_id'))
        candidate_subject = canonical_subject(str(candidate.get('subject') or ''))
        candidate_addresses = address_set_from_record(candidate)
        candidate_conversation_key = normalize_contact(candidate.get('conversation_key') or '')

        matched = False
        if current_conversation_key and candidate_conversation_key and current_conversation_key == candidate_conversation_key:
            matched = True
        elif current_thread_id and candidate_message_id and current_thread_id == candidate_message_id:
            matched = True
        elif current_message_id and candidate_thread_id and current_message_id == candidate_thread_id:
            matched = True
        elif current_subject and candidate_subject and current_subject == candidate_subject and current_addresses.intersection(candidate_addresses):
            matched = True

        if not matched:
            continue

        seen_ids.add(candidate_id)
        related.append({
            'email_id': candidate_id,
            'message_id': str(candidate.get('message_id') or ''),
            'thread_id': str(candidate.get('thread_id') or ''),
            'mailbox': str(candidate.get('mailbox') or ''),
            'mailbox_folder': str(candidate.get('mailbox_folder') or ''),
            'conversation_role': str(candidate.get('conversation_role') or ('outbound' if str(candidate.get('mailbox_folder') or '').lower() == 'sent' else 'inbound')),
            'subject': str(candidate.get('subject') or ''),
            'from_email': str(candidate.get('from') or candidate.get('from_email') or ''),
            'received_at': str(candidate.get('received_at') or candidate.get('sent_at') or candidate.get('created_at') or ''),
        })

    related.sort(key=lambda item: str(item.get('received_at') or ''), reverse=True)
    return related[:12]


def subject_matches_filter(subject: str | None) -> bool:
    folded = fold_text(subject)
    return any(term in folded for term in SUBJECT_TERMS)


def mailbox_by_name(name: str) -> dict[str, Any] | None:
    name = fold_text(name)
    for mailbox in load_mailboxes_config():
        if fold_text(str(mailbox.get('name') or '')) == name:
            return mailbox
    return None


def primary_mailbox_name() -> str:
    for mailbox in load_mailboxes_config():
        if mailbox.get('primary'):
            return str(mailbox.get('name') or 'buyer').strip().lower() or 'buyer'
    return 'buyer'


def record_exists_by_message_id(message_id: str) -> bool:
    message_id = normalize_message_id(message_id)
    if not message_id:
        return False
    for path in (LEGACY_EMAILS_JSONL, DATA_EMAILS_JSONL):
        for rec in load_jsonl_safe(path):
            if normalize_message_id(str(rec.get('message_id') or '')) == message_id:
                return True
    return False


def preview_uid_headers(client: Any, uid: int) -> dict[str, str]:
    typ, data = client.uid('FETCH', str(uid), '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE IN-REPLY-TO REFERENCES)])')
    if typ != 'OK' or not data:
        raise RuntimeError(f'IMAP header fetch failed for UID {uid}')
    chunks: list[bytes] = []
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
            chunks.append(bytes(item[1]))
    if not chunks:
        raise RuntimeError(f'IMAP header fetch returned no bytes for UID {uid}')
    msg = BytesParser(policy=policy.default).parsebytes(b''.join(chunks))
    return {
        'subject': str(msg.get('Subject') or ''),
        'message_id': str(msg.get('Message-ID') or ''),
        'from': str(msg.get('From') or ''),
        'date': str(msg.get('Date') or ''),
        'in_reply_to': str(msg.get('In-Reply-To') or ''),
        'references': str(msg.get('References') or ''),
    }


def load_mailbox_state() -> dict[str, Any]:
    if not MAILBOX_STATE_JSON.exists():
        return {'version': '0.2.0', 'mailboxes': {}, 'updated_at': now()}
    try:
        data = load_json(MAILBOX_STATE_JSON)
    except Exception:
        return {'version': '0.2.0', 'mailboxes': {}, 'updated_at': now()}
    if not isinstance(data, dict):
        return {'version': '0.2.0', 'mailboxes': {}, 'updated_at': now()}
    data.setdefault('mailboxes', {})
    if not isinstance(data['mailboxes'], dict):
        data['mailboxes'] = {}
    return data


def save_mailbox_state(**updates: Any) -> dict[str, Any]:
    current = load_mailbox_state()
    current.update(updates)
    current['updated_at'] = now()
    save_json(MAILBOX_STATE_JSON, current)
    return current


def mark_mailbox_sync(mailbox_name: str, *, folder: str | None = None, initial_sync_complete: bool | None = None, last_uid: int | None = None, last_message_id: str | None = None, message_count: int | None = None) -> dict[str, Any]:
    state = load_mailbox_state()
    mailboxes = state.setdefault('mailboxes', {})
    mailbox_state = mailboxes.get(mailbox_name, {}) if isinstance(mailboxes, dict) else {}
    if not isinstance(mailbox_state, dict):
        mailbox_state = {}
    mailbox_state.setdefault('folders', {})
    if folder:
        folders = mailbox_state.setdefault('folders', {})
        if not isinstance(folders, dict):
            folders = {}
            mailbox_state['folders'] = folders
        folder_state = dict(folders.get(folder) or {})
        if last_uid is not None:
            folder_state['last_uid'] = last_uid
        if last_message_id is not None:
            folder_state['last_message_id'] = last_message_id
        if message_count is not None:
            folder_state['message_count'] = message_count
        folder_state['last_sync_at'] = now()
        folders[folder] = folder_state
    if initial_sync_complete is not None:
        mailbox_state['initial_sync_complete'] = initial_sync_complete
        mailbox_state['initial_sync_at'] = now() if initial_sync_complete else mailbox_state.get('initial_sync_at')
    mailbox_state['updated_at'] = now()
    mailboxes[mailbox_name] = mailbox_state
    state['mailboxes'] = mailboxes
    state['updated_at'] = now()
    save_json(MAILBOX_STATE_JSON, state)
    return state


def build_task_blueprints(subject: str, body_text: str) -> list[dict[str, str]]:
    text = fold_text(f'{subject}\n{body_text}')
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(task_type: str) -> None:
        if task_type in seen:
            return
        seen.add(task_type)
        tasks.append({
            'task_type': task_type,
            'summary': f'{subject or "E-mail sem assunto"} — tarefa: {task_type}',
            'proposed_action': TASK_TYPE_PROPOSALS.get(task_type, 'Revisar manualmente antes de executar qualquer ação derivada.'),
        })

    if subject_matches_filter(subject) or any(term in text for term in SUBJECT_TERMS):
        add('quotation_review')
        if 'rfq' in text or 'request for quotation' in text:
            add('create_sourcing_project')
            add('generate_rfq_draft')
        if any(term in text for term in ['supplier', 'fornecedor', 'vendor']):
            add('register_supplier')
        if any(term in text for term in ['contact', 'contato']):
            add('register_contact')
        if any(term in text for term in ['product', 'produto', 'item']):
            add('register_product')
        if any(term in text for term in ['reply', 're:']):
            add('link_response_to_rfq')
        if any(term in text for term in ['quote', 'quotation', 'cotacao', 'cotação', 'price', 'offer']):
            add('generate_quote_analysis')
    return tasks or [{
        'task_type': 'quotation_review',
        'summary': f'{subject or "E-mail sem assunto"} — revisão inicial',
        'proposed_action': TASK_TYPE_PROPOSALS['quotation_review'],
    }]


def task_id_for(email_id: str, task_type: str, mailbox_name: str) -> str:
    digest = hashlib.sha1(f'{email_id}|{mailbox_name}|{task_type}'.encode('utf-8')).hexdigest()[:12]
    return f'task_{digest}'


def append_task_request(email_record: dict[str, Any], task_blueprint: dict[str, str]) -> dict[str, Any]:
    task_id = task_id_for(str(email_record['id']), task_blueprint['task_type'], str(email_record.get('mailbox') or 'buyer'))
    existing = next((rec for rec in load_jsonl_safe(DATA_MANUAL_REVIEW_QUEUE_JSONL) if str(rec.get('task_id') or rec.get('id') or '') == task_id), None)
    if existing and str(existing.get('status') or '').lower() != 'pending':
        return existing
    record = {
        'id': task_id,
        'task_id': task_id,
        'email_id': email_record['id'],
        'message_id': email_record['message_id'],
        'uid': email_record['imap_uid'],
        'thread_id': email_record['thread_id'],
        'conversation_key': email_record.get('conversation_key'),
        'conversation_role': email_record.get('conversation_role'),
        'conversation_subject': email_record.get('conversation_subject'),
        'related_email_ids': email_record.get('related_email_ids', []),
        'related_message_ids': email_record.get('related_message_ids', []),
        'related_messages': email_record.get('related_messages', []),
        'subject': email_record['subject'],
        'from': email_record['from'],
        'from_email': email_record['from'],
        'mailbox': email_record.get('mailbox'),
        'mailbox_folder': email_record.get('mailbox_folder'),
        'task_type': task_blueprint['task_type'],
        'summary': task_blueprint['summary'],
        'proposed_action': task_blueprint['proposed_action'],
        'status': 'pending',
        'approval_required': True,
        'auto_after_first_approval': False,
        'received_at': email_record.get('received_at') or email_record.get('created_at') or now(),
        'created_at': now(),
        'updated_at': now(),
        'source': 'imap_ingestor',
    }
    append_record(DATA_MANUAL_REVIEW_QUEUE_JSONL, record)
    notify_telegram('approval_request_needed', build_approval_request_message(record), metadata=record)
    return record


def persist_email(mailbox: dict[str, Any], folder: str, parsed: Any, *, uid: int, raw_bytes: bytes) -> dict[str, Any] | None:
    if record_exists_by_message_id(parsed.message_id):
        return None

    mailbox_name = str(mailbox.get('name') or 'buyer').strip().lower() or 'buyer'
    email_id = f"imap_email_{hashlib.sha1(f'{parsed.message_id}|{mailbox_name}'.encode('utf-8')).hexdigest()[:12]}"
    attachment_ids: list[str] = []

    for index, attachment in enumerate(parsed.attachments, start=1):
        attachment_id = f"att_{hashlib.sha1(f'{parsed.message_id}|{attachment.filename}|{index}'.encode('utf-8')).hexdigest()[:12]}"
        attachment_ids.append(attachment_id)
        append_record(ROOT / 'anexos.jsonl', {
            'id': attachment_id,
            'version': '0.1.0',
            'created_at': now(),
            'updated_at': now(),
            'email_id': email_id,
            'message_id': parsed.message_id,
            'thread_id': parsed.thread_id,
            'filename': attachment.filename,
            'mime_type': attachment.mime_type,
            'size_bytes': attachment.size_bytes,
            'sha256': attachment.sha256,
            'storage_path': attachment.path,
            'content_id': attachment.content_id,
            'mailbox': mailbox_name,
            'mailbox_folder': folder,
            'source': 'imap_ingestor',
        })

    record = {
        'id': email_id,
        'version': '0.2.0',
        'created_at': now(),
        'updated_at': now(),
        'direction': 'incoming',
        'mode': settings_mode(),
        'status': 'fetched',
        'source': 'imap_ingestor',
        'system_mode': settings_mode(),
        'read_only': True,
        'mailbox': mailbox_name,
        'mailbox_folder': folder,
        'initial_sync_only': bool(mailbox.get('initial_sync_only', False)),
        'imap_host': str(mailbox.get('imap_host') or mailbox.get('host') or ''),
        'imap_port': mailbox.get('imap_port') or mailbox.get('port'),
        'imap_folder': folder,
        'imap_uid': uid,
        'message_id': parsed.message_id,
        'thread_id': parsed.thread_id,
        'from': parsed.from_email,
        'from_name': parsed.from_name,
        'to': parsed.to,
        'cc': parsed.cc,
        'bcc': parsed.bcc,
        'subject': parsed.subject,
        'sent_at': parsed.date_iso,
        'received_at': now(),
        'body_text': parsed.body_text,
        'body_html_path': parsed.html_path,
        'raw_path': parsed.raw_path,
        'raw_sha256': parsed.raw_sha256,
        'hash': parsed.raw_sha256,
        'message_size_bytes': len(raw_bytes),
        'attachment_ids': attachment_ids,
        'attachment_count': len(attachment_ids),
        'attachments': [
            {
                'filename': attachment.filename,
                'mime_type': attachment.mime_type,
                'size_bytes': attachment.size_bytes,
                'sha256': attachment.sha256,
                'path': attachment.path,
                'content_id': attachment.content_id,
            }
            for attachment in parsed.attachments
        ],
        'classification': 'quotation_incoming',
        'processed': False,
        'analysis_status': 'pending',
        'supplier_id': None,
        'contact_id': None,
        'product_ids': [],
        'quote_ids': [],
    }

    record['conversation_key'] = conversation_key_for_email(
        message_id=str(record['message_id']),
        thread_id=str(record['thread_id']),
        subject=str(record['subject'] or ''),
        from_email=str(record['from'] or ''),
        to=list(record.get('to') or []),
        cc=list(record.get('cc') or []),
        bcc=list(record.get('bcc') or []),
    )
    record['conversation_role'] = 'outbound' if folder.lower() == 'sent' else 'inbound'
    record['conversation_subject'] = canonical_subject(str(record.get('subject') or ''))
    record['conversation_participants'] = sorted({
        *address_set_from_record(record),
        normalize_contact(record.get('from_name') or ''),
    } - {''})
    related_messages = related_email_summaries(record)
    record['related_email_ids'] = [item['email_id'] for item in related_messages]
    record['related_message_ids'] = [item['message_id'] for item in related_messages if item.get('message_id')]
    record['related_messages'] = related_messages

    append_record(LEGACY_EMAILS_JSONL, record)
    append_record(DATA_EMAILS_JSONL, record)

    task_ids: list[str] = []
    for blueprint in build_task_blueprints(parsed.subject, parsed.body_text):
        task = append_task_request(record, blueprint)
        task_id = str(task.get('task_id') or task.get('id') or '')
        if task_id:
            task_ids.append(task_id)
    record['task_ids'] = task_ids

    update_cursor(
        last_processed_email_id=email_id,
        last_processed_message_id=parsed.message_id,
        last_ingest_at=now(),
        last_sync_at=now(),
        state='idle',
    )
    update_ingest_state(
        status='running',
        running=False,
        last_run_at=now(),
        last_success_at=now(),
        processed_counts={
            'emails': count_jsonl(DATA_EMAILS_JSONL),
            'attachments': count_jsonl(ROOT / 'anexos.jsonl'),
            'quotes': count_jsonl(ROOT / 'cotacoes.jsonl'),
            'suppliers': count_jsonl(ROOT / 'fornecedores.jsonl'),
            'contacts': count_jsonl(ROOT / 'contatos.jsonl'),
            'products': count_jsonl(ROOT / 'produtos.jsonl'),
            'tasks': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
        },
        metrics={
            'quotation_matches': count_jsonl(DATA_EMAILS_JSONL),
            'emails_seen': count_jsonl(DATA_EMAILS_JSONL),
            'emails_saved': count_jsonl(DATA_EMAILS_JSONL),
            'emails_failed': 0,
            'duplicates_ignored': 0,
            'attachments_saved': count_jsonl(ROOT / 'anexos.jsonl'),
            'attachments_failed': 0,
            'tasks_detected': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
            'tasks_created': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
            'tasks_completed': sum(1 for rec in load_jsonl_safe(DATA_TASK_APPROVALS_JSONL) if str(rec.get('status') or '').lower() == 'approved'),
            'tasks_rejected': sum(1 for rec in load_jsonl_safe(DATA_TASK_APPROVALS_JSONL) if str(rec.get('status') or '').lower() == 'rejected'),
        },
    )
    imap_state_snapshot(last_uid=uid, last_message_id=parsed.message_id, message_count=count_jsonl(DATA_EMAILS_JSONL), unread_count=0, last_sync_at=now(), readonly=True, mailbox=mailbox_name, folder=folder)
    mark_mailbox_sync(mailbox_name, folder=folder, last_uid=uid, last_message_id=parsed.message_id, message_count=count_jsonl(DATA_EMAILS_JSONL))
    return record


def scan_mailbox_folder(mailbox: dict[str, Any], folder: str, *, limit: int | None = None, persist: bool = True) -> dict[str, Any]:
    client, info = connect_mailbox_imap(mailbox, folder=folder, readonly=True)
    try:
        uids = search_uids(client, 'ALL')
        if limit is not None and limit > 0:
            uids = uids[-limit:]
        matched: list[dict[str, Any]] = []
        fetched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for uid in uids:
            header = preview_uid_headers(client, uid)
            if not subject_matches_filter(header['subject']):
                continue
            preview = {
                'uid': uid,
                'subject': header['subject'],
                'from': mask_email(re.sub(r'(?is)^.*<([^>]+)>.*$', r'\1', header['from']).strip() or header['from']),
                'message_id': header['message_id'],
                'thread_id': header['in_reply_to'] or header['references'] or header['message_id'],
                'folder': folder,
                'mailbox': info['mailbox'],
                'already_saved': record_exists_by_message_id(header['message_id']),
            }
            matched.append(preview)
            if persist:
                if record_exists_by_message_id(header['message_id']):
                    skipped.append({
                        'uid': uid,
                        'message_id': header['message_id'],
                        'folder': folder,
                        'mailbox': info['mailbox'],
                        'reason': 'duplicate_message_id',
                    })
                    continue
                raw = fetch_uid_raw(client, uid)
                parsed = parse_email_bytes(raw, uid=uid)
                record = persist_email(mailbox, folder, parsed, uid=uid, raw_bytes=raw)
                if record:
                    fetched.append({
                        'email_id': record['id'],
                        'uid': uid,
                        'message_id': record['message_id'],
                        'thread_id': record['thread_id'],
                        'subject': record['subject'],
                        'from': mask_email(record['from']),
                        'attachments': record['attachment_count'],
                        'task_ids': record.get('task_ids', []),
                        'raw_path': record['raw_path'],
                        'mailbox': record['mailbox'],
                        'mailbox_folder': record['mailbox_folder'],
                    })
        result = {
            'ok': True,
            'mailbox': info['mailbox'],
            'folder': folder,
            'message_count': info['message_count'],
            'readonly': True,
            'limit': limit,
            'matched_count': len(matched),
            'matches': matched,
            'fetched_count': len(fetched),
            'fetched': fetched,
            'duplicates_ignored': len(skipped),
            'skipped': skipped,
        }
        if persist:
            mark_mailbox_sync(info['mailbox'], folder=folder, last_uid=uids[-1] if uids else None, last_message_id=fetched[-1]['message_id'] if fetched else None, message_count=info['message_count'])
        return result
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def scan_mailbox(mailbox: dict[str, Any], *, limit: int | None = None, persist: bool = True, folders: list[str] | None = None) -> dict[str, Any]:
    folders = folders or [str(folder) for folder in mailbox.get('folders') or MAILBOX_DEFAULT_FOLDERS]
    folder_results: list[dict[str, Any]] = []
    totals = {'matched': 0, 'fetched': 0, 'duplicates': 0}
    for folder in folders:
        try:
            folder_result = scan_mailbox_folder(mailbox, folder, limit=limit, persist=persist)
            folder_results.append(folder_result)
            totals['matched'] += int(folder_result.get('matched_count') or 0)
            totals['fetched'] += int(folder_result.get('fetched_count') or 0)
            totals['duplicates'] += int(folder_result.get('duplicates_ignored') or 0)
        except Exception as exc:
            folder_name = str(folder or '').strip()
            folder_results.append({
                'ok': folder_name.lower() == 'sent',
                'skipped': folder_name.lower() == 'sent',
                'mailbox': str(mailbox.get('name') or 'unknown'),
                'folder': folder,
                'error': str(exc),
            })
    return {
        'ok': True,
        'mailbox': str(mailbox.get('name') or 'unknown').lower(),
        'folders': folders,
        'results': folder_results,
        'matched_count': totals['matched'],
        'fetched_count': totals['fetched'],
        'duplicates_ignored': totals['duplicates'],
    }


def sync_mailboxes(*, mailbox_names: list[str], limit: int | None = None, persist: bool = True, folders: list[str] | None = None) -> dict[str, Any]:
    mailboxes = load_mailboxes_config()
    wanted = {fold_text(name) for name in mailbox_names}
    selected = [mailbox for mailbox in mailboxes if fold_text(str(mailbox.get('name') or '')) in wanted]
    results = [scan_mailbox(mailbox, limit=limit, persist=persist, folders=folders) for mailbox in selected]
    return {
        'ok': True,
        'mailbox_names': [str(mailbox.get('name') or '') for mailbox in selected],
        'results': results,
        'matched_count': sum(int(item.get('matched_count') or 0) for item in results),
        'fetched_count': sum(int(item.get('fetched_count') or 0) for item in results),
        'duplicates_ignored': sum(int(item.get('duplicates_ignored') or 0) for item in results),
    }


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_local_storage()
    errors: list[str] = []
    try:
        policy = load_json(AUTOMATION_POLICY_JSON)
        errors.extend(validate_policy(policy))
    except FileNotFoundError:
        errors.append(f'missing file: {AUTOMATION_POLICY_JSON}')
    except json.JSONDecodeError as exc:
        errors.append(f'invalid JSON in {AUTOMATION_POLICY_JSON}: {exc}')

    try:
        mailboxes = load_mailboxes_config()
        if not mailboxes:
            errors.append('mailboxes must contain at least one mailbox or fallback IMAP config must be available')
        for mailbox in mailboxes:
            if not str(mailbox.get('name') or '').strip():
                errors.append('mailbox entry missing name')
            if not mailbox.get('username'):
                errors.append(f"mailbox {mailbox.get('name')}: missing username")
            if not (mailbox.get('imap_host') or mailbox.get('host')):
                errors.append(f"mailbox {mailbox.get('name')}: missing IMAP host")
            if not (mailbox.get('imap_port') or mailbox.get('port')):
                errors.append(f"mailbox {mailbox.get('name')}: missing IMAP port")
            if not (mailbox.get('password_key') or mailbox.get('password_env') or mailbox.get('password') or 'secrets.json' in str(mailbox.get('password_source') or '')):
                errors.append(f"mailbox {mailbox.get('name')}: missing password reference")
    except Exception as exc:
        errors.append(str(exc))

    for path in [LEGACY_EMAILS_JSONL, DATA_EMAILS_JSONL, DATA_MANUAL_REVIEW_QUEUE_JSONL, DATA_TASK_APPROVALS_JSONL, ANEXOS_JSONL]:
        errors.extend(validate_jsonl(path))

    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'policy={AUTOMATION_POLICY_JSON}')
    print(f'mailboxes={", ".join(str(m.get("name")) for m in load_mailboxes_config())}')
    return 0


def cmd_show_safe(_: argparse.Namespace) -> int:
    ensure_local_storage()
    mailboxes = []
    for mailbox in load_mailboxes_config():
        mailboxes.append({
            'name': mailbox.get('name'),
            'primary': mailbox.get('primary'),
            'folders': mailbox.get('folders'),
            'read_only': True,
            'initial_sync_only': mailbox.get('initial_sync_only', False),
            'imap_host': mailbox.get('imap_host') or mailbox.get('host'),
            'imap_port': mailbox.get('imap_port') or mailbox.get('port'),
            'imap_ssl': mailbox.get('imap_ssl', mailbox.get('ssl', True)),
            'username': mask_email(str(mailbox.get('username') or '')),
            'password_source': mailbox.get('password_source') or mailbox.get('password_key') or mailbox.get('password_env') or 'unknown',
        })
    print(json.dumps({
        'ok': True,
        'mode': settings_mode(),
        'policy': str(AUTOMATION_POLICY_JSON),
        'mailboxes': mailboxes,
        'smtp_blocked': True,
        'telegram_required': True,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_connect_check(_: argparse.Namespace) -> int:
    ensure_local_storage()
    mailbox = mailbox_by_name(primary_mailbox_name())
    if not mailbox:
        print(json.dumps({'ok': False, 'error': 'no primary mailbox configured'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    try:
        client, info = connect_mailbox_imap(mailbox, folder='INBOX', readonly=True)
    except Exception as exc:
        print(json.dumps({
            'ok': False,
            'service': 'imap_ingestor',
            'mailbox': mailbox.get('name'),
            'folder': 'INBOX',
            'mode': settings_mode(),
            'readonly': True,
            'error': str(exc),
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    try:
        print(json.dumps({
            'ok': True,
            'service': 'imap_ingestor',
            'mailbox': info['mailbox'],
            'folder': info['folder'],
            'message_count': info['message_count'],
            'mode': settings_mode(),
            'readonly': True,
            'identity': get_email_context_snapshot('buy')['identity'],
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def cmd_search_quotation(_: argparse.Namespace) -> int:
    ensure_local_storage()
    results = sync_mailboxes(mailbox_names=[primary_mailbox_name()], persist=False, folders=list(MAILBOX_DEFAULT_FOLDERS))
    print(json.dumps({
        'ok': True,
        'service': 'imap_ingestor',
        'mode': settings_mode(),
        'readonly': True,
        'subject_terms': SUBJECT_TERMS,
        **results,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_fetch_quotation(_: argparse.Namespace) -> int:
    ensure_local_storage()
    results = sync_mailboxes(mailbox_names=[primary_mailbox_name()], persist=True, folders=list(MAILBOX_DEFAULT_FOLDERS))
    notify_telegram('imap_quotation_fetch', f"Fetched {results['fetched_count']} quotation email(s) from buyer mailbox.", metadata=results)
    print(json.dumps({
        'ok': True,
        'service': 'imap_ingestor',
        'mode': settings_mode(),
        'readonly': True,
        'subject_terms': SUBJECT_TERMS,
        **results,
        'counts': {
            'emails': count_jsonl(DATA_EMAILS_JSONL),
            'manual_review_queue': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
            'task_approvals': count_jsonl(DATA_TASK_APPROVALS_JSONL),
            'attachments': count_jsonl(ANEXOS_JSONL),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_initial_sync(args: argparse.Namespace) -> int:
    ensure_local_storage()
    mailbox = mailbox_by_name(args.mailbox)
    if not mailbox:
        print(json.dumps({'ok': False, 'error': f'unknown mailbox: {args.mailbox}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    results = scan_mailbox(mailbox, limit=args.limit, persist=True, folders=list(MAILBOX_DEFAULT_FOLDERS))
    if bool(mailbox.get('initial_sync_only', False)):
        mark_mailbox_sync(str(mailbox.get('name') or '').strip().lower(), initial_sync_complete=True)
    notify_telegram('imap_initial_sync', f"Initial sync for mailbox {mailbox.get('name')} saved {results['fetched_count']} email(s).", metadata=results)
    print(json.dumps({
        'ok': True,
        'service': 'imap_ingestor',
        'mode': settings_mode(),
        'readonly': True,
        'mailbox': mailbox.get('name'),
        'initial_sync_only': bool(mailbox.get('initial_sync_only', False)),
        **results,
        'counts': {
            'emails': count_jsonl(DATA_EMAILS_JSONL),
            'manual_review_queue': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
            'task_approvals': count_jsonl(DATA_TASK_APPROVALS_JSONL),
            'attachments': count_jsonl(ANEXOS_JSONL),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_watch(_: argparse.Namespace) -> int:
    ensure_local_storage()
    mailbox = mailbox_by_name('buyer') or mailbox_by_name(primary_mailbox_name())
    if not mailbox:
        print(json.dumps({'ok': False, 'error': 'buyer mailbox not configured'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    results = scan_mailbox(mailbox, persist=True, folders=list(MAILBOX_DEFAULT_FOLDERS))
    save_mailbox_state(last_watch_at=now(), last_watch_mailbox=str(mailbox.get('name') or 'buyer').lower())
    notify_telegram('imap_watch', f"Buyer mailbox watch saved {results['fetched_count']} email(s).", metadata=results)
    print(json.dumps({
        'ok': True,
        'service': 'imap_ingestor',
        'mode': settings_mode(),
        'readonly': True,
        'watching': 'buyer',
        **results,
        'counts': {
            'emails': count_jsonl(DATA_EMAILS_JSONL),
            'manual_review_queue': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
            'task_approvals': count_jsonl(DATA_TASK_APPROVALS_JSONL),
            'attachments': count_jsonl(ANEXOS_JSONL),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_local_storage()
    task_approvals = load_jsonl_safe(DATA_TASK_APPROVALS_JSONL)
    pending_tasks = [rec for rec in load_jsonl_safe(DATA_MANUAL_REVIEW_QUEUE_JSONL) if str(rec.get('status') or '').lower() == 'pending']
    approved_tasks = [rec for rec in task_approvals if str(rec.get('status') or '').lower() == 'approved']
    rejected_tasks = [rec for rec in task_approvals if str(rec.get('status') or '').lower() == 'rejected']
    mailbox_state = load_mailbox_state()
    print(json.dumps({
        'ok': True,
        'service': 'imap_ingestor',
        'mode': settings_mode(),
        'readonly': True,
        'smtp_blocked': True,
        'telegram_operational': bool(load_settings().get('telegram_config', {}).get('enabled')) if (ROOT / 'state' / 'settings.json').exists() else False,
        'mailboxes': [mailbox.get('name') for mailbox in load_mailboxes_config()],
        'emails_total': count_jsonl(DATA_EMAILS_JSONL),
        'quotation_found': count_jsonl(DATA_EMAILS_JSONL),
        'new_processed': count_jsonl(DATA_EMAILS_JSONL),
        'ignored_by_dedup': 0,
        'tasks_detected': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
        'tasks_completed': len(approved_tasks),
        'tasks_pending_approval': len(pending_tasks),
        'tasks_rejected': len(rejected_tasks),
        'manual_review_queue_events': count_jsonl(DATA_MANUAL_REVIEW_QUEUE_JSONL),
        'task_approval_events': count_jsonl(DATA_TASK_APPROVALS_JSONL),
        'attachments_total': count_jsonl(ANEXOS_JSONL),
        'mailbox_state': mailbox_state,
        'next_safe_command': 'python3 /opt/data/hermes-mail/scripts/imap_ingestor.py watch',
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail multi-mailbox IMAP ingestor')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate settings, mailboxes and local JSONL files')
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser('show-safe', help='Show masked mailbox configuration')
    p_show.set_defaults(func=cmd_show_safe)

    p_connect = sub.add_parser('connect-check', help='Perform a read-only IMAP connection test for the primary mailbox')
    p_connect.set_defaults(func=cmd_connect_check)

    p_search_q = sub.add_parser('search-quotation', help='Search the primary mailbox for quotation-like subjects')
    p_search_q.set_defaults(func=cmd_search_quotation)

    p_fetch_q = sub.add_parser('fetch-quotation', help='Fetch quotation-like emails from the primary mailbox')
    p_fetch_q.set_defaults(func=cmd_fetch_quotation)

    p_initial = sub.add_parser('initial-sync', help='Initial historical sync for a specific mailbox')
    p_initial.add_argument('--mailbox', required=True, choices=['buyer', 'sales'])
    p_initial.add_argument('--limit', type=int, default=1000)
    p_initial.set_defaults(func=cmd_initial_sync)

    p_watch = sub.add_parser('watch', help='Watch the buyer mailbox only')
    p_watch.set_defaults(func=cmd_watch)

    p_stats = sub.add_parser('stats', help='Show local ingest statistics')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
