#!/usr/bin/env python3
"""Telegram sender-approval workflow for Hermes Mail.

Behavior:
- Builds a unique sender list from pending approval tasks.
- Saves the sender snapshot locally.
- Sends one Telegram message per sender with "APROVADO" and "EXCLUIR" buttons.
- Approves or rejects all tasks for that sender when a button is clicked.
- Does not send any e-mail; first import/publish is Telegram-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from approval_queue import (  # noqa: E402
    append_decision,
    append_task_state,
    build_pending_tasks,
    ensure_local_storage,
    latest_decision,
    latest_task,
    purge_records_by_sender,
    register_policy_after_approval,
)
from email_real_common import append_record, load_secrets, load_settings, now  # noqa: E402
from reporting_utils import load_jsonl_records  # noqa: E402
from purchase_governance import (  # noqa: E402
    build_purchase_gate_acknowledgement,
    record_purchase_user_decision,
)

ROOT = Path('/opt/data/hermes-mail')
HERMES_DECISION_RECOMMENDATIONS_JSONL = ROOT / 'hermes-decision-recommendations.jsonl'
DATA_DIR = ROOT / 'data'
SENDER_EVENTS_JSONL = DATA_DIR / 'sender-approval-events.jsonl'
SESSION_JSON = DATA_DIR / 'telegram-sender-approval-session.json'
DEFAULT_POLL_INTERVAL = 3
DEFAULT_SESSION_PREFIX = 'tg_sender_approval'


# -----------------------------
# Helpers
# -----------------------------

def normalize_text(value: Any) -> str:
    text = str(value or '').strip()
    return re.sub(r'\s+', ' ', text)


def sender_key(task: dict[str, Any]) -> str:
    email = normalize_text(task.get('from_email') or task.get('from')).casefold()
    name = normalize_text(task.get('from')).casefold()
    if email:
        return email
    if name:
        return name
    raw = str(task.get('task_id') or task.get('id') or now())
    return f"sender_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def conversation_key(task: dict[str, Any]) -> str:
    key = normalize_text(task.get('conversation_key') or '').casefold()
    return key or sender_key(task)


def sender_label(task: dict[str, Any]) -> str:
    name = normalize_text(task.get('sender_name') or task.get('from') or task.get('name'))
    email = normalize_text(task.get('sender_email') or task.get('from_email') or task.get('from') or task.get('email'))
    key = normalize_text(task.get('sender_key'))
    if name and email and name.casefold() != email.casefold():
        return f'{name} <{email}>'
    return name or email or key or '(remetente não informado)'


def received_at_value(task: dict[str, Any]) -> str:
    return normalize_text(task.get('received_at') or task.get('created_at') or now())


def telegram_config() -> tuple[str, str]:
    settings = load_settings()
    telegram = settings.get('telegram_config') if isinstance(settings, dict) else {}
    if not isinstance(telegram, dict):
        telegram = {}
    secrets = load_secrets()
    token = str(secrets.get('telegram_bot_token') or secrets.get('telegram_token') or '').strip()
    chat_id = str(telegram.get('chat_id') or '').strip()
    if not token:
        raise SystemExit('telegram_bot_token ausente em state/secrets.json')
    if not chat_id:
        raise SystemExit('chat_id ausente em state/settings.json -> telegram_config.chat_id')
    return token, chat_id


def telegram_request(method: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = urllib.parse.urlencode(payload or {}).encode('utf-8') if payload else None
    request = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode('utf-8')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise RuntimeError('Telegram API returned unexpected payload')
    if not result.get('ok'):
        raise RuntimeError(result.get('description') or 'Telegram API request failed')
    return result


def telegram_send_message(token: str, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': 'true',
    }
    if reply_markup is not None:
        payload['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return telegram_request('sendMessage', token, payload)


def telegram_edit_message(token: str, chat_id: str, message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'disable_web_page_preview': 'true',
    }
    if reply_markup is not None:
        payload['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return telegram_request('editMessageText', token, payload)


def telegram_answer_callback(token: str, callback_query_id: str, text: str = '') -> dict[str, Any]:
    payload: dict[str, Any] = {'callback_query_id': callback_query_id}
    if text:
        payload['text'] = text
    return telegram_request('answerCallbackQuery', token, payload)


def telegram_get_updates(token: str, *, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {'timeout': timeout}
    if offset is not None:
        payload['offset'] = offset
    result = telegram_request('getUpdates', token, payload)
    updates = result.get('result') or []
    return updates if isinstance(updates, list) else []


def telegram_get_webhook_info(token: str) -> dict[str, Any]:
    result = telegram_request('getWebhookInfo', token)
    info = result.get('result') or {}
    return info if isinstance(info, dict) else {}


def schedule_sender_decision(*, action: str, session: dict[str, Any], sender: dict[str, Any], sender_id: str, callback: dict[str, Any], token: str, chat_id: str, pending_tasks: list[dict[str, Any]], callback_answered: bool) -> None:
    def _worker() -> None:
        try:
            result: dict[str, Any]
            decision_status: str
            decision_name: str
            callback_text: str
            edited_text: str
            if action == 'approve':
                result = approve_sender_tasks(sender, pending_tasks, reason='approved via Telegram inline button')
                decision_status = 'approved'
                decision_name = 'approve'
                callback_text = 'Conversa aprovada'
                edited_text = approved_sender_text(sender)
            else:
                sender_task_ids = {str(task_id).strip() for task_id in (sender.get('task_ids') or []) if str(task_id).strip()}
                if sender_task_ids:
                    sender_tasks = [task for task in pending_tasks if str(task.get('task_id') or task.get('id') or '').strip() in sender_task_ids]
                else:
                    sender_tasks = [task for task in pending_tasks if conversation_key(task) == sender['sender_key']]
                rejected_tasks: list[str] = []
                for task in sender_tasks:
                    task_id = str(task.get('task_id') or task.get('id') or '').strip()
                    if not task_id:
                        continue
                    current_decision = latest_decision(task_id)
                    if current_decision and str(current_decision.get('status') or '').lower() in {'approved', 'rejected'}:
                        continue
                    decision = append_decision(task, 'rejected', reason='excluded via Telegram inline button')
                    append_task_state(task, 'rejected', reason='excluded via Telegram inline button', actor='telegram_sender_approval')
                    rejected_tasks.append(str(decision.get('task_id') or task_id))
                result = {
                    'sender_key': sender['sender_key'],
                    'status': 'rejected',
                    'task_count': len(rejected_tasks),
                    'task_ids': rejected_tasks,
                    'purged_counts': purge_records_by_sender(sender_email='', sender_name='', task_ids=rejected_tasks),
                }
                decision_status = 'rejected'
                decision_name = 'reject'
                callback_text = 'Conversa excluída e registros apagados'
                edited_text = rejected_sender_text(sender)

            latest_session = load_session() or session
            latest_session.setdefault('approved_sender_ids', [])
            latest_session.setdefault('rejected_sender_ids', [])
            latest_session.setdefault('pending_sender_ids', [])
            latest_session.setdefault('processing_sender_ids', [])
            if sender_id in latest_session['pending_sender_ids']:
                latest_session['pending_sender_ids'].remove(sender_id)
            if sender_id in latest_session['processing_sender_ids']:
                latest_session['processing_sender_ids'].remove(sender_id)
            if action == 'approve':
                if sender_id not in latest_session['approved_sender_ids']:
                    latest_session['approved_sender_ids'].append(sender_id)
                if sender_id in latest_session['rejected_sender_ids']:
                    latest_session['rejected_sender_ids'].remove(sender_id)
            else:
                if sender_id not in latest_session['rejected_sender_ids']:
                    latest_session['rejected_sender_ids'].append(sender_id)
                if sender_id in latest_session['approved_sender_ids']:
                    latest_session['approved_sender_ids'].remove(sender_id)
            latest_session['updated_at'] = now()
            save_session(latest_session)

            message = callback.get('message') or {}
            message_id = message.get('message_id') if isinstance(message, dict) else None
            if isinstance(message_id, int):
                try:
                    telegram_edit_message(token, chat_id, message_id, edited_text)
                except Exception:
                    pass
            if not callback_answered:
                try:
                    telegram_answer_callback(token, str(callback.get('id') or ''), callback_text)
                except Exception:
                    pass
            save_sender_event(sender_event_record('decision', sender, session_id=str(latest_session.get('session_id') or session.get('session_id') or ''), status=decision_status, extra={
                'sender_id': sender_id,
                'decision': decision_name,
                'decision_result': result,
            }))
        except Exception as exc:
            try:
                latest_session = load_session() or session
                latest_session.setdefault('processing_sender_ids', [])
                if sender_id in latest_session['processing_sender_ids']:
                    latest_session['processing_sender_ids'].remove(sender_id)
                latest_session.setdefault('pending_sender_ids', [])
                if sender_id not in latest_session['pending_sender_ids']:
                    latest_session['pending_sender_ids'].append(sender_id)
                latest_session['updated_at'] = now()
                save_session(latest_session)
            except Exception:
                pass
            message = callback.get('message') or {}
            message_id = message.get('message_id') if isinstance(message, dict) else None
            if isinstance(message_id, int):
                try:
                    telegram_edit_message(token, chat_id, message_id, f'Hermes Compras\n\nFalha ao processar: {sender_label(sender)}')
                except Exception:
                    pass
            if not callback_answered:
                try:
                    telegram_answer_callback(token, str(callback.get('id') or ''), 'Falha ao processar')
                except Exception:
                    pass
            save_sender_event(sender_event_record('decision_error', sender, session_id=str(session.get('session_id') or ''), status='error', extra={'sender_id': sender_id, 'error': str(exc)}))

    thread = threading.Thread(target=_worker, name=f'tg_sender_decision_{sender_id}', daemon=True)
    thread.start()


def telegram_delete_webhook(token: str, *, drop_pending_updates: bool = False) -> dict[str, Any]:
    payload = {'drop_pending_updates': 'true' if drop_pending_updates else 'false'}
    return telegram_request('deleteWebhook', token, payload)


# -----------------------------
# Sender grouping / session state
# -----------------------------

def build_unique_senders(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for task in tasks:
        key = conversation_key(task)
        group = groups.setdefault(key, {
            'sender_key': key,
            'conversation_key': key,
            'sender_name': normalize_text(task.get('from')),
            'sender_email': normalize_text(task.get('from_email') or task.get('from')),
            'task_count': 0,
            'task_ids': [],
            'email_ids': [],
            'subjects': [],
            'recipient_summary': [],
            'latest_received_at': '',
            'latest_task_id': '',
            'mailbox_folders': [],
            'conversation_roles': [],
            'participants': [],
            'history_items': [],
            'related_email_ids': [],
        })
        group['task_count'] += 1

        task_id = normalize_text(task.get('task_id') or task.get('id'))
        if task_id:
            group['task_ids'].append(task_id)
            group['latest_task_id'] = task_id

        email_id = normalize_text(task.get('email_id'))
        if email_id:
            group['email_ids'].append(email_id)
            if email_id not in group['related_email_ids']:
                group['related_email_ids'].append(email_id)

        mailbox_folder = normalize_text(task.get('mailbox_folder'))
        if mailbox_folder and mailbox_folder not in group['mailbox_folders']:
            group['mailbox_folders'].append(mailbox_folder)

        conversation_role = normalize_text(task.get('conversation_role') or ('outbound' if mailbox_folder.casefold() == 'sent' else 'inbound'))
        if conversation_role and conversation_role not in group['conversation_roles']:
            group['conversation_roles'].append(conversation_role)

        participant = normalize_text(task.get('sender_email') or task.get('from_email') or task.get('from'))
        if participant and participant not in group['participants']:
            group['participants'].append(participant)

        history_item = {
            'email_id': email_id,
            'message_id': normalize_text(task.get('message_id')),
            'mailbox_folder': mailbox_folder,
            'conversation_role': conversation_role,
            'subject': normalize_text(task.get('subject')),
            'sender': normalize_text(task.get('sender_email') or task.get('from_email') or task.get('from')),
            'received_at': received_at_value(task),
        }
        history_key = history_item['email_id'] or history_item['message_id']
        if history_key and not any((item.get('email_id') or item.get('message_id')) == history_key for item in group['history_items']):
            group['history_items'].append(history_item)

        related_ids = task.get('related_email_ids')
        if isinstance(related_ids, list):
            for item in related_ids:
                item_text = normalize_text(item)
                if item_text and item_text not in group['related_email_ids']:
                    group['related_email_ids'].append(item_text)

        subject = normalize_text(task.get('subject'))
        if subject and subject not in group['subjects']:
            group['subjects'].append(subject)

        for field in ['to', 'cc', 'bcc']:
            value = task.get(field)
            if isinstance(value, list):
                for item in value:
                    text = normalize_text(item)
                    if text and text not in group['recipient_summary']:
                        group['recipient_summary'].append(text)
            elif isinstance(value, str):
                text = normalize_text(value)
                if text and text not in group['recipient_summary']:
                    group['recipient_summary'].append(text)

        received = received_at_value(task)
        if received and received > group['latest_received_at']:
            group['latest_received_at'] = received

        if not group['sender_name']:
            group['sender_name'] = normalize_text(task.get('from'))
        if not group['sender_email']:
            group['sender_email'] = normalize_text(task.get('from_email') or task.get('from'))

    senders = list(groups.values())
    senders.sort(key=lambda item: (str(item.get('latest_received_at') or ''), int(item.get('task_count') or 0)), reverse=True)
    return senders


def session_id_for(senders: list[dict[str, Any]]) -> str:
    digest_source = '|'.join(str(item.get('sender_key') or '') for item in senders)
    digest = hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:12]
    return f'{DEFAULT_SESSION_PREFIX}_{digest}'


def sender_id_for(session_id: str, sender_key_value: str) -> str:
    digest = hashlib.sha1(f'{session_id}|{sender_key_value}'.encode('utf-8')).hexdigest()[:12]
    return f's_{digest}'


def build_sender_message(sender: dict[str, Any], *, session_id: str, sender_id: str) -> tuple[str, dict[str, Any]]:
    subjects = sender.get('subjects') or []
    recipients = sender.get('recipient_summary') or []
    task_ids = sender.get('task_ids') or []
    participants = sender.get('participants') or []
    history_items = sender.get('history_items') or []
    mailbox_folders = sender.get('mailbox_folders') or []
    conversation_roles = sender.get('conversation_roles') or []
    subject_preview = '\n'.join(f'🗂 {item}' for item in subjects[:3]) or '🗂 (sem assunto)'
    participant_preview = '\n'.join(f'👥 {item}' for item in participants[:5]) or '👥 (sem participantes registrados)'
    recipient_preview = '\n'.join(f'🎯 {item}' for item in recipients[:3]) or '🎯 (sem destinatário registrado)'

    received_lines: list[str] = []
    sent_lines: list[str] = []
    for item in history_items[:8]:
        if not isinstance(item, dict):
            continue
        received_at_item = str(item.get('received_at') or '-')
        sender_label_item = str(item.get('sender') or '-')
        subject_item = str(item.get('subject') or '-')
        role_item = str(item.get('conversation_role') or '').lower()
        folder_item = str(item.get('mailbox_folder') or '').lower()
        line = f"- {received_at_item} | {sender_label_item} | {subject_item}"
        if role_item == 'outbound' or folder_item == 'sent':
            sent_lines.append(line)
        else:
            received_lines.append(line)

    if not received_lines:
        received_lines = ['- (nenhuma mensagem recebida vinculada)']
    if not sent_lines:
        sent_lines = ['- (nenhuma resposta enviada vinculada)']

    history_preview = (
        '🔽 Recebido\n' + '\n'.join(received_lines) +
        '\n\n🔼 Resposta enviada\n' + '\n'.join(sent_lines)
    )
    folders_preview = '\n'.join(f'🗃️ {item}' for item in mailbox_folders[:3]) or '🗃️ (sem pasta registrada)'
    roles_preview = '\n'.join(f'↕️ {item}' for item in conversation_roles[:3]) or '↕️ (sem direção registrada)'
    task_preview = '\n'.join(f'🧩 {item}' for item in task_ids[:5]) or '🧩 (sem tasks)'
    text = (
        'Hermes Compras - conversa vinculada\n\n'
        f'Conversa:\n{sender_label(sender)}\n'
        f'⏱ Última interação:\n{sender.get("latest_received_at") or "-"}\n'
        f'🧾 Pendências:\n{sender.get("task_count") or 0}\n\n'
        'Participantes:\n'
        f'{participant_preview}\n\n'
        'Assuntos associados:\n'
        f'{subject_preview}\n\n'
        'Linha do tempo da conversa:\n'
        f'{history_preview}\n\n'
        'Pastas associadas:\n'
        f'{folders_preview}\n\n'
        'Direções associadas:\n'
        f'{roles_preview}\n\n'
        'Destinatários associados:\n'
        f'{recipient_preview}\n\n'
        'Tasks associadas:\n'
        f'{task_preview}\n\n'
        'Clique em APROVADO para liberar esta conversa. Ou em EXCLUIR para rejeitá-la.'
    )

    markup = {
        'inline_keyboard': [[
            {
                'text': 'APROVADO',
                'callback_data': f'approve|{session_id}|{sender_id}',
            },
            {
                'text': 'EXCLUIR',
                'callback_data': f'reject|{session_id}|{sender_id}',
            },
        ]]
    }
    return text, markup


def sender_event_record(event_type: str, sender: dict[str, Any], *, session_id: str, status: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        'event_type': event_type,
        'status': status,
        'session_id': session_id,
        'sender_key': sender.get('sender_key'),
        'conversation_key': sender.get('conversation_key'),
        'sender_name': sender.get('sender_name'),
        'sender_email': sender.get('sender_email'),
        'task_count': sender.get('task_count', 0),
        'task_ids': sender.get('task_ids', []),
        'email_ids': sender.get('email_ids', []),
        'subjects': sender.get('subjects', []),
        'recipient_summary': sender.get('recipient_summary', []),
        'mailbox_folders': sender.get('mailbox_folders', []),
        'conversation_roles': sender.get('conversation_roles', []),
        'participants': sender.get('participants', []),
        'history_items': sender.get('history_items', []),
        'related_email_ids': sender.get('related_email_ids', []),
        'latest_received_at': sender.get('latest_received_at'),
        'latest_task_id': sender.get('latest_task_id'),
        'created_at': now(),
        'updated_at': now(),
    }
    if extra:
        payload.update(extra)
    return payload


def save_sender_event(record: dict[str, Any]) -> dict[str, Any]:
    SENDER_EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    SENDER_EVENTS_JSONL.touch(exist_ok=True)
    payload = dict(record)
    payload['id'] = payload.get('id') or f"sender_evt_{hashlib.sha1(f'{payload.get('session_id') or ''}|{payload.get('sender_key') or ''}|{payload.get('status') or ''}|{now()}'.encode('utf-8')).hexdigest()[:12]}"
    append_record(SENDER_EVENTS_JSONL, payload)
    return payload


def load_session() -> dict[str, Any] | None:
    if not SESSION_JSON.exists():
        return None
    try:
        with SESSION_JSON.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_session(session: dict[str, Any]) -> None:
    SESSION_JSON.parent.mkdir(parents=True, exist_ok=True)
    with SESSION_JSON.open('w', encoding='utf-8') as fh:
        json.dump(session, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')


def build_session(tasks: list[dict[str, Any]], existing_session: dict[str, Any] | None = None) -> dict[str, Any]:
    senders = build_unique_senders(tasks)
    if existing_session and isinstance(existing_session, dict):
        session = dict(existing_session)
        session_id = str(session.get('session_id') or '').strip() or session_id_for(senders)
        session.setdefault('created_at', now())
    else:
        session_id = session_id_for(senders)
        session = {
            'session_id': session_id,
            'status': 'created',
            'created_at': now(),
        }

    sender_map: dict[str, dict[str, Any]] = {}
    sender_order: list[str] = []
    existing_sender_map = existing_session.get('sender_map', {}) if isinstance(existing_session, dict) else {}
    key_to_sender_id: dict[str, str] = {}
    if isinstance(existing_sender_map, dict):
        for sender_id, sender in existing_sender_map.items():
            if not isinstance(sender, dict):
                continue
            key = str(sender.get('sender_key') or '').strip()
            if key:
                key_to_sender_id[key] = str(sender_id)
                sender_map[str(sender_id)] = sender

    approved_ids = {str(item) for item in session.get('approved_sender_ids', []) if str(item)}
    rejected_ids = {str(item) for item in session.get('rejected_sender_ids', []) if str(item)}
    sent_ids = {str(item) for item in session.get('sent_sender_ids', []) if str(item)}
    pending_sender_ids: list[str] = []
    for sender in senders:
        sid = key_to_sender_id.get(sender['sender_key']) or sender_id_for(session_id, sender['sender_key'])
        sender_map[sid] = sender
        if sid not in sender_order:
            sender_order.append(sid)
        if sid in approved_ids or sid in rejected_ids or sid in sent_ids:
            continue
        pending_sender_ids.append(sid)

    session.update({
        'session_id': session_id,
        'status': session.get('status') or 'created',
        'updated_at': now(),
        'operation_mode': 'production_controlled',
        'publish_mode': 'telegram_button_approval',
        'first_import_sends_email': False,
        'sender_map': sender_map,
        'chat_message_ids': dict(session.get('chat_message_ids', {}) or {}),
        'last_update_id': int(session.get('last_update_id') or 0),
        'pending_sender_ids': pending_sender_ids,
        'sent_sender_ids': list(session.get('sent_sender_ids', []) or []),
        'processing_sender_ids': list(session.get('processing_sender_ids', []) or []),
    })
    if not session.get('approved_sender_ids'):
        session['approved_sender_ids'] = []
    if not session.get('rejected_sender_ids'):
        session['rejected_sender_ids'] = []
    session.setdefault('processing_sender_ids', [])
    if not session.get('processing_sender_ids'):
        session['processing_sender_ids'] = []
    return session


# -----------------------------
# Task approval actions
# -----------------------------

def approve_sender_tasks(sender: dict[str, Any], tasks: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
    sender_task_ids = {str(task_id).strip() for task_id in (sender.get('task_ids') or []) if str(task_id).strip()}
    if sender_task_ids:
        sender_tasks = [task for task in tasks if str(task.get('task_id') or task.get('id') or '').strip() in sender_task_ids]
    else:
        sender_tasks = [task for task in tasks if conversation_key(task) == sender['sender_key']]
    approved_tasks: list[str] = []
    for task in sender_tasks:
        task_id = str(task.get('task_id') or task.get('id') or '').strip()
        if not task_id:
            continue
        current_decision = latest_decision(task_id)
        if current_decision and str(current_decision.get('status') or '').lower() in {'approved', 'rejected'}:
            continue
        decision = append_decision(task, 'approved', reason=reason)
        append_task_state(task, 'approved', reason=reason, actor='telegram_sender_approval')
        register_policy_after_approval(task)
        approved_tasks.append(str(decision.get('task_id') or task_id))
    return {
        'sender_key': sender['sender_key'],
        'status': 'approved',
        'task_count': len(approved_tasks),
        'task_ids': approved_tasks,
    }


def approved_sender_text(sender: dict[str, Any]) -> str:
    return (
        'Hermes Compras\n\n'
        f'Conversa aprovada:\n{sender_label(sender)}\n'
        f'Recebido em:\n{sender.get("latest_received_at") or "-"}\n'
        f'Tarefas liberadas:\n{sender.get("task_count") or 0}\n\n'
        'Status: APROVADO'
    )


def rejected_sender_text(sender: dict[str, Any]) -> str:
    return (
        'Hermes Compras\n\n'
        f'Conversa excluída:\n{sender_label(sender)}\n'
        f'Recebido em:\n{sender.get("latest_received_at") or "-"}\n'
        f'Tarefas excluídas:\n{sender.get("task_count") or 0}\n\n'
        'Status: EXCLUÍDO'
    )


def build_updates_offset(updates: list[dict[str, Any]], current_offset: int) -> int:
    max_update_id = current_offset
    for update in updates:
        update_id = update.get('update_id')
        if isinstance(update_id, int) and update_id >= max_update_id:
            max_update_id = update_id + 1
    return max_update_id


# -----------------------------
# Commands
# -----------------------------

def cmd_list(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    senders = build_unique_senders(tasks)
    print(json.dumps({'ok': True, 'count': len(senders), 'senders': senders}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    session = build_session(tasks)
    save_session(session)
    for sender_id, sender in session['sender_map'].items():
        save_sender_event(sender_event_record('snapshot', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id}))
    print(json.dumps({
        'ok': True,
        'session_id': session['session_id'],
        'saved': len(session['sender_map']),
        'session_file': str(SESSION_JSON),
        'events_file': str(SENDER_EVENTS_JSONL),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    ensure_local_storage()
    token, chat_id = telegram_config()
    tasks = build_pending_tasks()
    session = build_session(tasks)
    save_session(session)
    sent: list[dict[str, Any]] = []
    for sender_id in session['pending_sender_ids'][: args.limit or 10_000]:
        sender = session['sender_map'][sender_id]
        text, markup = build_sender_message(sender, session_id=session['session_id'], sender_id=sender_id)
        if args.dry_run:
            sent.append({'sender_id': sender_id, 'sender_key': sender['sender_key'], 'preview': text, 'reply_markup': markup})
            continue
        result = telegram_send_message(token, chat_id, text, reply_markup=markup)
        message = result.get('result', {}) if isinstance(result, dict) else {}
        message_id = message.get('message_id') if isinstance(message, dict) else None
        session['sent_sender_ids'].append(sender_id)
        session['chat_message_ids'][sender_id] = message_id
        session['updated_at'] = now()
        save_session(session)
        save_sender_event(sender_event_record('published', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id, 'message_id': message_id}))
        sent.append({'sender_id': sender_id, 'sender_key': sender['sender_key'], 'message_id': message_id})
    print(json.dumps({
        'ok': True,
        'operation_mode': 'preview_only' if args.dry_run else 'production_controlled',
        'session_id': session['session_id'],
        'sent_count': len(sent),
        'first_import_sends_email': False,
        'items': sent,
        'session_file': str(SESSION_JSON),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ensure_local_storage()
    token, chat_id = telegram_config()
    tasks = build_pending_tasks()
    session = build_session(tasks)
    save_session(session)
    for sender_id in session['pending_sender_ids']:
        sender = session['sender_map'][sender_id]
        text, markup = build_sender_message(sender, session_id=session['session_id'], sender_id=sender_id)
        if args.dry_run:
            save_sender_event(sender_event_record('dry_run_publish', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id}))
            continue
        result = telegram_send_message(token, chat_id, text, reply_markup=markup)
        message = result.get('result', {}) if isinstance(result, dict) else {}
        message_id = message.get('message_id') if isinstance(message, dict) else None
        session['sent_sender_ids'].append(sender_id)
        session['chat_message_ids'][sender_id] = message_id
        session['updated_at'] = now()
        save_session(session)
        save_sender_event(sender_event_record('published', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id, 'message_id': message_id}))
    if args.dry_run:
        print(json.dumps({
            'ok': True,
            'operation_mode': 'production_controlled',
            'session_id': session['session_id'],
            'message': 'No email send; Telegram publish only (production controlled).',
            'pending_sender_count': len(session['pending_sender_ids']),
            'first_import_sends_email': False,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(json.dumps({
        'ok': True,
        'session_id': session['session_id'],
        'published_count': len(session['pending_sender_ids']),
        'polling': True,
        'first_import_sends_email': False,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return poll_session(session, token, chat_id, args.poll_interval)


def cmd_resume(args: argparse.Namespace) -> int:
    ensure_local_storage()
    token, chat_id = telegram_config()
    session = load_session()
    if not session:
        print(json.dumps({'ok': False, 'error': 'no saved session'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps({'ok': True, 'session_id': session.get('session_id'), 'resuming': True}, ensure_ascii=False, indent=2, sort_keys=True))
    return poll_session(session, token, chat_id, args.poll_interval)


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    senders = build_unique_senders(tasks)
    session = load_session() or {}
    print(json.dumps({
        'ok': True,
        'pending_tasks': len(tasks),
        'unique_senders': len(senders),
        'session_id': session.get('session_id'),
        'session_file': str(SESSION_JSON),
        'events_file': str(SENDER_EVENTS_JSONL),
        'first_import_sends_email': False,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    ensure_local_storage()
    token, chat_id = telegram_config()
    session = load_session()
    cycles = 0
    while True:
        tasks = build_pending_tasks()
        session = build_session(tasks, existing_session=session)
        save_session(session)
        sent_ids = set(str(item) for item in session.get('sent_sender_ids', []) if str(item))
        pending_ids = [sender_id for sender_id in session.get('pending_sender_ids', []) if sender_id not in sent_ids]
        if pending_ids:
            if args.dry_run:
                preview_items: list[dict[str, Any]] = []
                for sender_id in pending_ids[: args.limit or 1000]:
                    sender = session['sender_map'][sender_id]
                    text, markup = build_sender_message(sender, session_id=session['session_id'], sender_id=sender_id)
                    preview_items.append({'sender_id': sender_id, 'sender_key': sender['sender_key'], 'preview': text, 'reply_markup': markup})
                    save_sender_event(sender_event_record('dry_run_publish', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id}))
                print(json.dumps({
                    'ok': True,
                    'operation_mode': 'production_controlled',
                    'watching': True,
                    'session_id': session['session_id'],
                    'pending_sender_count': len(pending_ids),
                    'items': preview_items,
                    'first_import_sends_email': False,
                }, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                for sender_id in pending_ids[: args.limit or 1000]:
                    sender = session['sender_map'][sender_id]
                    text, markup = build_sender_message(sender, session_id=session['session_id'], sender_id=sender_id)
                    result = telegram_send_message(token, chat_id, text, reply_markup=markup)
                    message = result.get('result', {}) if isinstance(result, dict) else {}
                    message_id = message.get('message_id') if isinstance(message, dict) else None
                    session['sent_sender_ids'].append(sender_id)
                    session['chat_message_ids'][sender_id] = message_id
                    session['updated_at'] = now()
                    save_session(session)
                    save_sender_event(sender_event_record('published', sender, session_id=session['session_id'], status='pending', extra={'sender_id': sender_id, 'message_id': message_id}))
                print(json.dumps({
                    'ok': True,
                    'operation_mode': 'production_controlled',
                    'watching': True,
                    'session_id': session['session_id'],
                    'published_count': len(pending_ids[: args.limit or 1000]),
                    'first_import_sends_email': False,
                }, ensure_ascii=False, indent=2, sort_keys=True))
                session = load_session() or session
                poll_session(session, token, chat_id, args.poll_interval)
        cycles += 1
        if args.cycles and cycles >= args.cycles:
            return 0
        time.sleep(max(1, args.poll_interval))


# -----------------------------
# Poll loop
# -----------------------------

def handle_callback_update(update: dict[str, Any], *, session: dict[str, Any], token: str, chat_id: str, pending_tasks: list[dict[str, Any]]) -> tuple[bool, dict[str, Any] | None]:
    callback = update.get('callback_query')
    if not isinstance(callback, dict):
        return False, None
    callback_id = str(callback.get('id') or '').strip()
    data = str(callback.get('data') or '').strip()
    if not data:
        return False, None

    if data.startswith('purchase_gate|'):
        parts = data.split('|', 2)
        if len(parts) != 3:
            return False, None
        _, callback_action, recommendation_id = parts
        recommendations = load_jsonl_records(HERMES_DECISION_RECOMMENDATIONS_JSONL)
        recommendation = None
        for rec in reversed(recommendations):
            rec_id = str(rec.get('id') or rec.get('recommendation_id') or '').strip()
            if recommendation_id and rec_id == recommendation_id:
                recommendation = rec
                break
        if recommendation is None and recommendations:
            recommendation = recommendations[-1]
        if not isinstance(recommendation, dict):
            telegram_answer_callback(token, callback_id, 'Recomendação não encontrada')
            return True, None
        decision = record_purchase_user_decision(recommendation, callback_action=callback_action, decided_by='telegram_sender_approval', notes='approved via Telegram purchase decision gate')
        ack = build_purchase_gate_acknowledgement(recommendation, callback_action=callback_action)
        try:
            message = callback.get('message') or {}
            message_id = message.get('message_id') if isinstance(message, dict) else None
            if isinstance(message_id, int):
                telegram_edit_message(token, chat_id, message_id, ack)
        except Exception:
            pass
        telegram_answer_callback(token, callback_id, 'Decisão registrada')
        save_sender_event(sender_event_record('purchase_gate_decision', {
            'sender_key': recommendation_id,
            'sender_name': str(recommendation.get('supplier_name') or recommendation.get('product_name') or 'purchase_gate'),
            'sender_email': str(recommendation.get('supplier_country') or ''),
            'task_count': 1,
            'task_ids': [recommendation_id],
            'email_ids': [],
            'subjects': [str(recommendation.get('recommendation_title') or recommendation.get('product_name') or '')],
            'recipient_summary': [],
            'latest_received_at': str(recommendation.get('created_at') or now()),
            'latest_task_id': recommendation_id,
        }, session_id=recommendation_id, status='decision_recorded', extra={'decision': decision, 'callback_action': callback_action}))
        return True, {'recommendation_id': recommendation_id, 'status': 'decision_recorded'}

    if data.startswith('task_approve|') or data.startswith('task_reject|'):
        action, task_id = data.split('|', 1)
        task = latest_task(task_id)
        if not isinstance(task, dict):
            telegram_answer_callback(token, callback_id, 'Tarefa não encontrada')
            return True, None
        approved = action == 'task_approve'
        decision_status = 'approved' if approved else 'rejected'
        decision_name = 'approve' if approved else 'reject'
        decision = append_decision(task, decision_status, reason='approved via Telegram button' if approved else 'rejected via Telegram button')
        append_task_state(task, decision_status, reason='approved via Telegram button' if approved else 'rejected via Telegram button', actor='telegram_task_approval')
        if approved:
            register_policy_after_approval(task)
        text = 'Aprovação registrada' if approved else 'Rejeição registrada'
        try:
            message = callback.get('message') or {}
            message_id = message.get('message_id') if isinstance(message, dict) else None
            if isinstance(message_id, int):
                telegram_edit_message(token, chat_id, message_id, f'Hermes Compras\n\nTarefa: {task.get("subject") or task_id}\nStatus: {decision_status.upper()}')
        except Exception:
            pass
        telegram_answer_callback(token, callback_id, text)
        save_sender_event(sender_event_record('task_decision', {
            'sender_key': str(task_id),
            'sender_name': str(task.get('subject') or task_id),
            'sender_email': str(task.get('from_email') or task.get('from') or ''),
            'task_count': 1,
            'task_ids': [str(task_id)],
            'email_ids': [str(task.get('email_id') or '')],
            'subjects': [str(task.get('subject') or '')],
            'recipient_summary': [],
            'latest_received_at': str(task.get('received_at') or task.get('created_at') or now()),
            'latest_task_id': str(task_id),
        }, session_id=str(task_id), status=decision_status, extra={'decision': decision_name, 'decision_result': decision}))
        return True, {'task_id': task_id, 'status': decision_status}

    parts = data.split('|', 2)
    if len(parts) != 3:
        return False, None
    action, session_id, sender_id = parts
    if action not in {'approve', 'reject'} or session_id != session.get('session_id'):
        return False, None
    sender_map = session.setdefault('sender_map', {})
    sender = sender_map.get(sender_id)
    if not isinstance(sender, dict):
        telegram_answer_callback(token, callback_id, 'Sessão inválida ou remetente não encontrado')
        return True, None

    session.setdefault('processing_sender_ids', [])
    if sender_id in session['processing_sender_ids']:
        telegram_answer_callback(token, callback_id, 'Já em processamento')
        return True, {'sender_id': sender_id, 'status': 'processing'}

    callback_answered = False
    try:
        telegram_answer_callback(token, callback_id, 'Processando...')
        callback_answered = True
    except Exception:
        pass
    try:
        message = callback.get('message') or {}
        message_id = message.get('message_id') if isinstance(message, dict) else None
        if isinstance(message_id, int):
            telegram_edit_message(
                token,
                chat_id,
                message_id,
                f'Hermes Compras\n\nProcessando {"aprovação" if action == "approve" else "exclusão"}...\n{sender_label(sender)}',
            )
    except Exception:
        pass

    session.setdefault('processing_sender_ids', [])
    if sender_id not in session['processing_sender_ids']:
        session['processing_sender_ids'].append(sender_id)
    session['updated_at'] = now()
    save_session(session)

    schedule_sender_decision(
        action=action,
        session=session,
        sender=sender,
        sender_id=sender_id,
        callback=callback,
        token=token,
        chat_id=chat_id,
        pending_tasks=pending_tasks,
        callback_answered=callback_answered,
    )
    return True, {'sender_id': sender_id, 'status': 'processing'}



def poll_session(session: dict[str, Any], token: str, chat_id: str, poll_interval: int) -> int:
    offset = int(session.get('last_update_id') or 0)
    while True:
        try:
            updates = telegram_get_updates(token, offset=offset, timeout=25)
        except urllib.error.HTTPError as exc:
            if getattr(exc, 'code', None) == 409:
                session['updated_at'] = now()
                save_session(session)
                time.sleep(max(1, poll_interval))
                continue
            raise
        if updates:
            offset = build_updates_offset(updates, offset)
            session['last_update_id'] = offset
            session['updated_at'] = now()
            save_session(session)
        changed = False
        pending_tasks = build_pending_tasks()
        for update in updates:
            handled, result = handle_callback_update(update, session=session, token=token, chat_id=chat_id, pending_tasks=pending_tasks)
            if handled:
                changed = True
        if changed:
            session = load_session() or session
        if not session.get('pending_sender_ids'):
            session['status'] = 'done'
            session['updated_at'] = now()
            save_session(session)
            return 0
        time.sleep(max(1, poll_interval))


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Telegram sender approval workflow for Hermes Mail')
    sub = parser.add_subparsers(dest='command', required=True)

    p_list = sub.add_parser('list', help='List unique pending senders')
    p_list.set_defaults(func=cmd_list)

    p_sync = sub.add_parser('sync', help='Import and save the sender snapshot')
    p_sync.set_defaults(func=cmd_sync)

    p_publish = sub.add_parser('publish', help='Publish one Telegram card per sender')
    p_publish.add_argument('--dry-run', action='store_true', help='Do not send Telegram messages; only generate the payload')
    p_publish.add_argument('--limit', type=int, default=1000, help='Maximum number of senders to publish')
    p_publish.set_defaults(func=cmd_publish)

    p_run = sub.add_parser('run', help='Publish sender cards and keep polling for Telegram approvals')
    p_run.add_argument('--dry-run', action='store_true', help='Do not send Telegram messages; only generate the payload')
    p_run.add_argument('--poll-interval', type=int, default=DEFAULT_POLL_INTERVAL)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser('resume', help='Resume polling an existing session')
    p_resume.add_argument('--poll-interval', type=int, default=DEFAULT_POLL_INTERVAL)
    p_resume.set_defaults(func=cmd_resume)

    p_stats = sub.add_parser('stats', help='Show sender approval stats')
    p_stats.set_defaults(func=cmd_stats)

    p_watch = sub.add_parser('watch', help='Continuously sync, publish, and poll for sender approvals')
    p_watch.add_argument('--dry-run', action='store_true', help='Do not send Telegram messages; only generate the payload')
    p_watch.add_argument('--limit', type=int, default=1000, help='Maximum number of senders to process per cycle')
    p_watch.add_argument('--cycles', type=int, default=0, help='Limit the number of watch cycles (0 = forever)')
    p_watch.add_argument('--poll-interval', type=int, default=DEFAULT_POLL_INTERVAL)
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
