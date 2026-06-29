#!/usr/bin/env python3
"""Approval queue for controlled Hermes Mail task actions.

This queue is separate from the legacy RFQ/SMTP approval queue used by the
existing dry-run pipeline. It stores task-level approvals for actions derived
from incoming emails.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import re
from pathlib import Path
from typing import Any

from email_real_common import append_record, ensure_storage, now
from purchase_governance import record_user_decision

ROOT = Path('/opt/data/hermes-mail')
DATA_DIR = ROOT / 'data'
MANUAL_REVIEW_QUEUE_JSONL = DATA_DIR / 'manual-review-queue.jsonl'
TASK_APPROVALS_JSONL = DATA_DIR / 'task_approvals.jsonl'
BULK_APPROVAL_EVENTS_JSONL = DATA_DIR / 'bulk-approval-events.jsonl'
AUTOMATION_POLICY_JSON = ROOT / 'config' / 'automation_policy.json'

PENDING_STATUSES = {'pending', 'queued', 'awaiting_approval', 'needs_approval'}
APPROVED_STATUSES = {'approved', 'auto_approved'}
REJECTED_STATUSES = {'rejected'}
FINAL_STATUSES = APPROVED_STATUSES | REJECTED_STATUSES

REQUIRED_POLICY_KEYS = [
    'version',
    'imap',
    'telegram',
    'smtp',
    'task_types',
    'auto_after_first_approval',
]

TASK_MESSAGE_TEMPLATE = """Hermes Mail - Aprovação necessária

E-mail:
{subject}
Recebido em:
{received_at}
Remetente:
{from_email}
Tarefa detectada:
{task_type}
Resumo:
{summary}
Histórico vinculado:
{history_block}

Ação proposta:
{proposed_action}

Responder:
APPROVE {task_id}
REJECT {task_id}
"""


def load_json(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return data


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')


def ensure_local_storage() -> None:
    ensure_storage()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_REVIEW_QUEUE_JSONL.touch(exist_ok=True)
    TASK_APPROVALS_JSONL.touch(exist_ok=True)
    BULK_APPROVAL_EVENTS_JSONL.touch(exist_ok=True)
    if not AUTOMATION_POLICY_JSON.exists():
        save_json(AUTOMATION_POLICY_JSON, default_policy())


def default_policy() -> dict[str, Any]:
    return {
        'version': '0.1.0',
        'imap': {
            'read_only': True,
            'case_insensitive_subject_filter': True,
            'initial_subject_terms': [
                'quotation',
                'quote',
                'rfq',
                'request for quotation',
                'cotação',
                'cotacao',
            ],
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


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_POLICY_KEYS:
        if key not in policy:
            errors.append(f'missing policy field: {key}')
    imap = policy.get('imap')
    if not isinstance(imap, dict):
        errors.append('policy.imap must be an object')
    else:
        if imap.get('read_only') is not True:
            errors.append('policy.imap.read_only must be true')
        if imap.get('case_insensitive_subject_filter') is not True:
            errors.append('policy.imap.case_insensitive_subject_filter must be true')
        terms = imap.get('initial_subject_terms')
        if not isinstance(terms, list) or not terms:
            errors.append('policy.imap.initial_subject_terms must be a non-empty list')
    telegram = policy.get('telegram')
    if not isinstance(telegram, dict):
        errors.append('policy.telegram must be an object')
    smtp = policy.get('smtp')
    if not isinstance(smtp, dict):
        errors.append('policy.smtp must be an object')
    if not isinstance(policy.get('task_types', {}), dict):
        errors.append('policy.task_types must be an object')
    if not isinstance(policy.get('auto_after_first_approval'), bool):
        errors.append('policy.auto_after_first_approval must be boolean')
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


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _normalize_sender_value(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def _rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def purge_records_by_sender(*, sender_email: str = '', sender_name: str = '', task_ids: list[str] | None = None) -> dict[str, int]:
    """Remove queued/approved records that belong to an excluded sender.

    This is used when an operator presses EXCLUIR in Telegram and wants the
    imported records to disappear from the local JSONL database too.
    """

    ensure_local_storage()
    sender_email_norm = _normalize_sender_value(sender_email)
    sender_name_norm = _normalize_sender_value(sender_name)
    task_id_set = {str(task_id).strip() for task_id in (task_ids or []) if str(task_id).strip()}

    def should_keep(record: dict[str, Any]) -> bool:
        task_id = str(record.get('task_id') or record.get('id') or '').strip()
        if task_id and task_id in task_id_set:
            return False
        rec_email = _normalize_sender_value(record.get('from_email') or record.get('from'))
        rec_name = _normalize_sender_value(record.get('from') or record.get('sender_name'))
        if sender_email_norm and rec_email and rec_email == sender_email_norm:
            return False
        if sender_name_norm and rec_name and rec_name == sender_name_norm:
            return False
        return True

    removed_counts: dict[str, int] = {}
    for path in (MANUAL_REVIEW_QUEUE_JSONL, TASK_APPROVALS_JSONL):
        records = load_jsonl_records(path)
        kept = [record for record in records if should_keep(record)]
        removed_counts[path.name] = len(records) - len(kept)
        if len(kept) != len(records):
            _rewrite_jsonl(path, kept)

    append_record(BULK_APPROVAL_EVENTS_JSONL, {
        'id': f"bulk_purge_{hashlib.sha1(f'{sender_email_norm}|{sender_name_norm}|{now()}'.encode('utf-8')).hexdigest()[:12]}",
        'action': 'purge_sender_records',
        'sender_email': sender_email,
        'sender_name': sender_name,
        'task_ids': sorted(task_id_set),
        'removed_counts': removed_counts,
        'created_at': now(),
        'source': 'approval_queue',
    })
    return removed_counts


def latest_by_task_id(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rec in load_jsonl_records(path):
        task_id = str(rec.get('task_id') or rec.get('id') or '').strip()
        if not task_id:
            continue
        current = latest.get(task_id)
        if not current or str(rec.get('created_at') or '') >= str(current.get('created_at') or ''):
            latest[task_id] = rec
    return latest


def latest_task(task_id: str) -> dict[str, Any] | None:
    return latest_by_task_id(MANUAL_REVIEW_QUEUE_JSONL).get(task_id)


def latest_decision(task_id: str) -> dict[str, Any] | None:
    return latest_by_task_id(TASK_APPROVALS_JSONL).get(task_id)


def build_pending_tasks() -> list[dict[str, Any]]:
    tasks = latest_by_task_id(MANUAL_REVIEW_QUEUE_JSONL)
    decisions = latest_by_task_id(TASK_APPROVALS_JSONL)
    pending: list[dict[str, Any]] = []
    for task_id, task in tasks.items():
        status = str(task.get('status') or 'pending').lower()
        decision = decisions.get(task_id)
        if decision and str(decision.get('status') or '').lower() in FINAL_STATUSES:
            continue
        if status in PENDING_STATUSES:
            merged = dict(task)
            merged['task_id'] = task_id
            pending.append(merged)
    pending.sort(key=lambda item: str(item.get('created_at') or ''))
    return pending


def build_approval_request_message(task: dict[str, Any]) -> str:
    received_at = str(task.get('received_at') or task.get('created_at') or now())
    related_messages = task.get('related_messages') if isinstance(task.get('related_messages'), list) else []

    received_lines: list[str] = []
    sent_lines: list[str] = []
    for item in related_messages[:8]:
        if not isinstance(item, dict):
            continue
        received_at_item = str(item.get('received_at') or item.get('sent_at') or item.get('created_at') or '-')
        sender = str(item.get('from_email') or item.get('from') or '-')
        subject = str(item.get('subject') or '-')
        role = str(item.get('conversation_role') or '').lower()
        folder = str(item.get('mailbox_folder') or '').lower()
        line = f"- {received_at_item} | {sender} | {subject}"
        if role == 'outbound' or folder == 'sent':
            sent_lines.append(line)
        else:
            received_lines.append(line)

    if not received_lines:
        received_lines = ['- (nenhuma mensagem recebida vinculada)']
    if not sent_lines:
        sent_lines = ['- (nenhuma resposta enviada vinculada)']

    history_block = (
        '📥 Mensagem recebida\n' + '\n'.join(received_lines) +
        '\n\n📤 Resposta vinculada\n' + '\n'.join(sent_lines)
    )
    return TASK_MESSAGE_TEMPLATE.format(
        subject=str(task.get('subject') or '(sem assunto)'),
        received_at=received_at,
        from_email=str(task.get('from') or task.get('from_email') or '(remetente não informado)'),
        task_type=str(task.get('task_type') or 'quotation_review'),
        summary=str(task.get('summary') or 'Sem resumo disponível.'),
        history_block=history_block,
        proposed_action=str(task.get('proposed_action') or 'Revisar manualmente antes de executar qualquer ação derivada.'),
        task_id=str(task.get('task_id') or task.get('id') or ''),
    )


def register_policy_after_approval(task: dict[str, Any]) -> None:
    policy = default_policy()
    if AUTOMATION_POLICY_JSON.exists():
        try:
            loaded = load_json(AUTOMATION_POLICY_JSON)
            if isinstance(loaded, dict):
                policy.update(loaded)
        except Exception:
            pass
    if not bool(policy.get('auto_after_first_approval')):
        return
    task_type = str(task.get('task_type') or '').strip() or 'quotation_review'
    task_types = policy.setdefault('task_types', {})
    if not isinstance(task_types, dict):
        task_types = {}
        policy['task_types'] = task_types
    entry = dict(task_types.get(task_type) or {})
    entry.update({
        'auto_after_first_approval': True,
        'approved_once': True,
        'auto_enabled': True,
        'last_approved_task_id': task.get('task_id') or task.get('id'),
        'last_approved_at': now(),
    })
    task_types[task_type] = entry
    save_json(AUTOMATION_POLICY_JSON, policy)


def append_task_state(task: dict[str, Any], status: str, *, reason: str | None = None, actor: str = 'approval_queue') -> dict[str, Any]:
    task_id = str(task.get('task_id') or task.get('id') or '').strip()
    if not task_id:
        raise ValueError('task_id is required')
    record = {
        'id': f"task_evt_{hashlib.sha1(f'{task_id}|{status}|{now()}'.encode('utf-8')).hexdigest()[:12]}",
        'task_id': task_id,
        'email_id': task.get('email_id'),
        'message_id': task.get('message_id'),
        'uid': task.get('uid'),
        'thread_id': task.get('thread_id'),
        'conversation_key': task.get('conversation_key'),
        'conversation_role': task.get('conversation_role'),
        'related_email_ids': task.get('related_email_ids'),
        'related_message_ids': task.get('related_message_ids'),
        'subject': task.get('subject'),
        'from': task.get('from'),
        'from_email': task.get('from_email'),
        'received_at': task.get('received_at') or task.get('created_at'),
        'task_type': task.get('task_type'),
        'summary': task.get('summary'),
        'proposed_action': task.get('proposed_action'),
        'status': status,
        'reason': reason,
        'created_at': now(),
        'updated_at': now(),
        'source': actor,
    }
    append_record(MANUAL_REVIEW_QUEUE_JSONL, record)
    return record


def append_decision(task: dict[str, Any], status: str, *, reason: str | None = None) -> dict[str, Any]:
    task_id = str(task.get('task_id') or task.get('id') or '').strip()
    record = {
        'id': f"approval_{hashlib.sha1(f'{task_id}|{status}|{now()}'.encode('utf-8')).hexdigest()[:12]}",
        'task_id': task_id,
        'email_id': task.get('email_id'),
        'message_id': task.get('message_id'),
        'uid': task.get('uid'),
        'thread_id': task.get('thread_id'),
        'conversation_key': task.get('conversation_key'),
        'conversation_role': task.get('conversation_role'),
        'related_email_ids': task.get('related_email_ids'),
        'related_message_ids': task.get('related_message_ids'),
        'task_type': task.get('task_type'),
        'received_at': task.get('received_at') or task.get('created_at'),
        'status': status,
        'reason': reason,
        'created_at': now(),
        'updated_at': now(),
        'source': 'approval_queue',
    }
    append_record(TASK_APPROVALS_JSONL, record)
    try:
        record_user_decision({
            'id': record['id'],
            'decision_context': task.get('task_type') or 'task_approval',
            'related_entity_type': 'email_task',
            'related_entity_id': task_id,
            'rfq_batch_id': task.get('rfq_batch_id'),
            'supplier_id': task.get('supplier_id'),
            'product_id': task.get('product_id'),
            'quote_id': task.get('quote_id'),
            'recommendation_id': task.get('recommendation_id'),
            'decision': status,
            'decision_label': status,
            'decided_by': actor,
            'decided_at': record['created_at'],
            'decision_source': 'manual_admin_action',
            'notes': reason,
            'payload_json': record,
        })
    except Exception:
        pass
    return record


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_local_storage()
    policy_errors: list[str] = []
    try:
        policy = load_json(AUTOMATION_POLICY_JSON)
        if not isinstance(policy, dict):
            policy_errors.append(f'{AUTOMATION_POLICY_JSON} must contain a JSON object')
        else:
            policy_errors.extend(validate_policy(policy))
    except FileNotFoundError:
        policy_errors.append(f'missing file: {AUTOMATION_POLICY_JSON}')
    except json.JSONDecodeError as exc:
        policy_errors.append(f'invalid JSON in {AUTOMATION_POLICY_JSON}: {exc}')
    errors = policy_errors
    errors.extend(validate_jsonl(MANUAL_REVIEW_QUEUE_JSONL))
    errors.extend(validate_jsonl(TASK_APPROVALS_JSONL))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'manual_review_queue={MANUAL_REVIEW_QUEUE_JSONL}')
    print(f'task_approvals={TASK_APPROVALS_JSONL}')
    print(f'automation_policy={AUTOMATION_POLICY_JSON}')
    return 0


def cmd_list_pending(_: argparse.Namespace) -> int:
    ensure_local_storage()
    pending = build_pending_tasks()
    print(json.dumps({
        'ok': True,
        'pending_count': len(pending),
        'pending': pending,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_approve_task(args: argparse.Namespace) -> int:
    ensure_local_storage()
    task = latest_task(args.task_id)
    if not task:
        print(json.dumps({'ok': False, 'error': f'task not found: {args.task_id}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    decision = append_decision(task, 'approved', reason=args.reason)
    state = append_task_state(task, 'approved', reason=args.reason, actor='approval_queue')
    register_policy_after_approval(task)
    print(json.dumps({'ok': True, 'action': 'approve-task', 'task': task, 'decision': decision, 'state': state}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_reject_task(args: argparse.Namespace) -> int:
    ensure_local_storage()
    task = latest_task(args.task_id)
    if not task:
        print(json.dumps({'ok': False, 'error': f'task not found: {args.task_id}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    decision = append_decision(task, 'rejected', reason=args.reason)
    state = append_task_state(task, 'rejected', reason=args.reason, actor='approval_queue')
    print(json.dumps({'ok': True, 'action': 'reject-task', 'task': task, 'decision': decision, 'state': state}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_local_storage()
    pending = build_pending_tasks()
    tasks = latest_by_task_id(MANUAL_REVIEW_QUEUE_JSONL)
    decisions = latest_by_task_id(TASK_APPROVALS_JSONL)
    counts: dict[str, int] = {}
    for task in tasks.values():
        status = str(task.get('status') or 'unknown').lower()
        counts[status] = counts.get(status, 0) + 1
    approved = sum(1 for rec in decisions.values() if str(rec.get('status') or '').lower() in APPROVED_STATUSES)
    rejected = sum(1 for rec in decisions.values() if str(rec.get('status') or '').lower() in REJECTED_STATUSES)
    print(json.dumps({
        'ok': True,
        'manual_review_queue_events': sum(1 for _ in MANUAL_REVIEW_QUEUE_JSONL.open('r', encoding='utf-8') if _.strip()) if MANUAL_REVIEW_QUEUE_JSONL.exists() else 0,
        'unique_tasks': len(tasks),
        'task_approval_events': sum(1 for _ in TASK_APPROVALS_JSONL.open('r', encoding='utf-8') if _.strip()) if TASK_APPROVALS_JSONL.exists() else 0,
        'pending_count': len(pending),
        'approved_count': approved,
        'rejected_count': rejected,
        'status_counts': counts,
        'automation_policy': str(AUTOMATION_POLICY_JSON),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail approval queue')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate approval queue stores')
    p_validate.set_defaults(func=cmd_validate)

    p_list = sub.add_parser('list-pending', help='List pending approval tasks')
    p_list.set_defaults(func=cmd_list_pending)

    p_approve = sub.add_parser('approve-task', help='Approve a task by task_id')
    p_approve.add_argument('task_id')
    p_approve.add_argument('--reason', default=None)
    p_approve.set_defaults(func=cmd_approve_task)

    p_reject = sub.add_parser('reject-task', help='Reject a task by task_id')
    p_reject.add_argument('task_id')
    p_reject.add_argument('--reason', default=None)
    p_reject.set_defaults(func=cmd_reject_task)

    # Backwards-compatible aliases.
    p_approve_alias = sub.add_parser('approve', help='Alias for approve-task')
    p_approve_alias.add_argument('task_id')
    p_approve_alias.add_argument('--reason', default=None)
    p_approve_alias.set_defaults(func=cmd_approve_task)

    p_reject_alias = sub.add_parser('reject', help='Alias for reject-task')
    p_reject_alias.add_argument('task_id')
    p_reject_alias.add_argument('--reason', default=None)
    p_reject_alias.set_defaults(func=cmd_reject_task)

    p_stats = sub.add_parser('stats', help='Show queue statistics')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
