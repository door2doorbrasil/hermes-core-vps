#!/usr/bin/env python3
"""Interactive sender-level approval workflow for Hermes Mail.

This tool builds a unique sender list from pending approval tasks, saves the
snapshot, sends one Telegram approval request per sender, and lets the operator
approve or reject each sender with a simple s/n prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
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
    register_policy_after_approval,
)
from email_real_common import append_record, now, notify_telegram  # noqa: E402

ROOT = Path('/opt/data/hermes-mail')
DATA_DIR = ROOT / 'data'
SENDER_EVENTS_JSONL = DATA_DIR / 'sender-approval-events.jsonl'


def normalize_text(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def sender_key(task: dict[str, Any]) -> str:
    email = normalize_text(task.get('from_email') or task.get('from')).casefold()
    name = normalize_text(task.get('from')).casefold()
    return email or name or f"sender_{hashlib.sha1(str(task.get('task_id') or task.get('id') or now()).encode('utf-8')).hexdigest()[:12]}"


def sender_label(task: dict[str, Any]) -> str:
    name = normalize_text(task.get('from'))
    email = normalize_text(task.get('from_email') or task.get('from'))
    if name and email and name.casefold() != email.casefold():
        return f'{name} <{email}>'
    return name or email or '(remetente não informado)'


def received_at_value(task: dict[str, Any]) -> str:
    return normalize_text(task.get('received_at') or task.get('created_at') or now())


def build_unique_senders(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for task in tasks:
        key = sender_key(task)
        group = groups.setdefault(key, {
            'sender_key': key,
            'sender_name': normalize_text(task.get('from')),
            'sender_email': normalize_text(task.get('from_email') or task.get('from')),
            'task_count': 0,
            'task_ids': [],
            'email_ids': [],
            'subjects': [],
            'recipient_summary': [],
            'latest_received_at': '',
            'pending_task_ids': [],
            'latest_task_id': '',
        })
        group['task_count'] += 1
        task_id = normalize_text(task.get('task_id') or task.get('id'))
        if task_id:
            group['task_ids'].append(task_id)
            group['pending_task_ids'].append(task_id)
            group['latest_task_id'] = task_id
        email_id = normalize_text(task.get('email_id'))
        if email_id:
            group['email_ids'].append(email_id)
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


def append_sender_event(record: dict[str, Any]) -> dict[str, Any]:
    SENDER_EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    SENDER_EVENTS_JSONL.touch(exist_ok=True)
    sender_component = normalize_text(record.get('sender_key'))
    event_component = normalize_text(record.get('event_type'))
    status_component = normalize_text(record.get('status'))
    digest_source = f'{sender_component}|{event_component}|{status_component}|{now()}'
    payload = {
        'id': f"sender_evt_{hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:12]}",
        'created_at': now(),
        'updated_at': now(),
        **record,
    }
    append_record(SENDER_EVENTS_JSONL, payload)
    return payload


def snapshot_unique_senders(senders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for sender in senders:
        payload = append_sender_event({
            'event_type': 'snapshot',
            'status': 'pending',
            'source': 'sender_approval_manager',
            'sender_key': sender['sender_key'],
            'sender_name': sender.get('sender_name'),
            'sender_email': sender.get('sender_email'),
            'task_count': sender.get('task_count', 0),
            'task_ids': sender.get('task_ids', []),
            'email_ids': sender.get('email_ids', []),
            'subjects': sender.get('subjects', []),
            'recipient_summary': sender.get('recipient_summary', []),
            'latest_received_at': sender.get('latest_received_at'),
            'latest_task_id': sender.get('latest_task_id'),
        })
        snapshots.append(payload)
    return snapshots


def latest_sender_state() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not SENDER_EVENTS_JSONL.exists():
        return latest
    with SENDER_EVENTS_JSONL.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            key = normalize_text(rec.get('sender_key')).casefold()
            if not key:
                continue
            current = latest.get(key)
            if not current or str(rec.get('created_at') or '') >= str(current.get('created_at') or ''):
                latest[key] = rec
    return latest


def build_review_queue(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    senders = build_unique_senders(tasks)
    latest = latest_sender_state()
    queue: list[dict[str, Any]] = []
    for sender in senders:
        state = latest.get(sender['sender_key'])
        if state and str(state.get('status') or '').lower() in {'approved', 'rejected', 'excluded'}:
            continue
        merged = dict(sender)
        if state:
            merged['previous_status'] = state.get('status')
            merged['previous_event_id'] = state.get('id')
        queue.append(merged)
    return queue


def render_sender_message(sender: dict[str, Any]) -> str:
    subjects = sender.get('subjects') or []
    subject_preview = '\n'.join(f'- {item}' for item in subjects[:3]) or '- (sem assunto)'
    recipients = sender.get('recipient_summary') or []
    recipient_preview = '\n'.join(f'- {item}' for item in recipients[:3]) or '- (sem destinatário registrado)'
    task_ids = sender.get('task_ids') or []
    task_preview = '\n'.join(f'- {item}' for item in task_ids[:5]) or '- (sem tasks)'
    return (
        'Hermes Compras - aprovação por remetente\n\n'
        f'Remetente:\n{sender_label(sender)}\n'
        f'Chave:\n{sender.get("sender_key") or "-"}\n'
        f'Quantidade de mensagens/tarefas pendentes:\n{sender.get("task_count") or 0}\n'
        f'Último recebimento:\n{sender.get("latest_received_at") or "-"}\n\n'
        'Assuntos associados:\n'
        f'{subject_preview}\n\n'
        'Destinatários associados:\n'
        f'{recipient_preview}\n\n'
        'Task IDs:\n'
        f'{task_preview}\n\n'
        'Responder no terminal com s para aprovar ou n para excluir este remetente.'
    )


def approve_sender(sender: dict[str, Any], tasks: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
    sender_tasks = [task for task in tasks if sender_key(task) == sender['sender_key']]
    approved_tasks: list[str] = []
    for task in sender_tasks:
        task_id = str(task.get('task_id') or task.get('id') or '').strip()
        if not task_id:
            continue
        if latest_decision(task_id) and str(latest_decision(task_id).get('status') or '').lower() in {'approved', 'rejected'}:
            continue
        decision = append_decision(task, 'approved', reason=reason)
        append_task_state(task, 'approved', reason=reason, actor='sender_approval_manager')
        register_policy_after_approval(task)
        approved_tasks.append(str(decision.get('task_id') or task_id))
    return {
        'sender_key': sender['sender_key'],
        'status': 'approved',
        'task_count': len(approved_tasks),
        'task_ids': approved_tasks,
    }


def reject_sender(sender: dict[str, Any], tasks: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
    sender_tasks = [task for task in tasks if sender_key(task) == sender['sender_key']]
    rejected_tasks: list[str] = []
    for task in sender_tasks:
        task_id = str(task.get('task_id') or task.get('id') or '').strip()
        if not task_id:
            continue
        if latest_decision(task_id) and str(latest_decision(task_id).get('status') or '').lower() in {'approved', 'rejected'}:
            continue
        decision = append_decision(task, 'rejected', reason=reason)
        append_task_state(task, 'rejected', reason=reason, actor='sender_approval_manager')
        rejected_tasks.append(str(decision.get('task_id') or task_id))
    return {
        'sender_key': sender['sender_key'],
        'status': 'rejected',
        'task_count': len(rejected_tasks),
        'task_ids': rejected_tasks,
    }


def cmd_list(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    senders = build_unique_senders(tasks)
    print(json.dumps({
        'ok': True,
        'count': len(senders),
        'senders': senders,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    senders = build_unique_senders(tasks)
    snapshots = snapshot_unique_senders(senders)
    print(json.dumps({
        'ok': True,
        'saved': len(snapshots),
        'file': str(SENDER_EVENTS_JSONL),
        'senders': senders,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_review(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    queue = build_review_queue(tasks)
    snapshots = snapshot_unique_senders(queue)
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not queue:
        print(json.dumps({'ok': True, 'message': 'no pending senders'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for sender in queue:
        message = render_sender_message(sender)
        notify_telegram('sender_approval_needed', message, metadata=sender)
        while True:
            answer = input(f"Aprovar remetente {sender_label(sender)}? [s/n]: ").strip().casefold()
            if answer in {'s', 'n'}:
                break
            print('Resposta inválida. Digite s ou n.')
        if answer == 's':
            result = approve_sender(sender, tasks, reason='approved interactively by sender review')
            append_sender_event({
                'event_type': 'decision',
                'status': 'approved',
                'source': 'sender_approval_manager',
                'sender_key': sender['sender_key'],
                'sender_name': sender.get('sender_name'),
                'sender_email': sender.get('sender_email'),
                'task_count': sender.get('task_count', 0),
                'task_ids': sender.get('task_ids', []),
                'latest_received_at': sender.get('latest_received_at'),
                'decision': 's',
                'decision_result': result,
            })
            approved.append({
                'sender_key': sender['sender_key'],
                'sender': sender_label(sender),
                'task_count': result['task_count'],
            })
        else:
            result = reject_sender(sender, tasks, reason='rejected interactively by sender review')
            append_sender_event({
                'event_type': 'decision',
                'status': 'rejected',
                'source': 'sender_approval_manager',
                'sender_key': sender['sender_key'],
                'sender_name': sender.get('sender_name'),
                'sender_email': sender.get('sender_email'),
                'task_count': sender.get('task_count', 0),
                'task_ids': sender.get('task_ids', []),
                'latest_received_at': sender.get('latest_received_at'),
                'decision': 'n',
                'decision_result': result,
            })
            rejected.append({
                'sender_key': sender['sender_key'],
                'sender': sender_label(sender),
                'task_count': result['task_count'],
            })
    print(json.dumps({
        'ok': True,
        'file': str(SENDER_EVENTS_JSONL),
        'snapshots_saved': len(snapshots),
        'approved': approved,
        'rejected': rejected,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_local_storage()
    tasks = build_pending_tasks()
    senders = build_unique_senders(tasks)
    print(json.dumps({
        'ok': True,
        'pending_tasks': len(tasks),
        'unique_senders': len(senders),
        'events_file': str(SENDER_EVENTS_JSONL),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Interactive sender approval manager')
    sub = parser.add_subparsers(dest='command', required=True)

    p_list = sub.add_parser('list', help='List unique pending senders')
    p_list.set_defaults(func=cmd_list)

    p_sync = sub.add_parser('sync', help='Import and save the unique sender snapshot')
    p_sync.set_defaults(func=cmd_sync)

    p_review = sub.add_parser('review', help='Send one Telegram message per sender and approve with s/n')
    p_review.set_defaults(func=cmd_review)

    p_stats = sub.add_parser('stats', help='Show sender queue stats')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
