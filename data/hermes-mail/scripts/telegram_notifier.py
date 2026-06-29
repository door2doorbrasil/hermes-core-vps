#!/usr/bin/env python3
"""Dry-run and real-test Telegram notifier for Hermes Mail."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from approval_queue import (
    MANUAL_REVIEW_QUEUE_JSONL,
    TASK_APPROVALS_JSONL,
    build_approval_request_message,
    build_pending_tasks,
    latest_task,
)
from reporting_utils import load_jsonl_records
from purchase_governance import (
    build_purchase_gate_acknowledgement,
    build_purchase_gate_message,
    build_purchase_gate_reply_markup,
    latest_purchase_recommendation,
)

def _resolve_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    opt_root = Path('/opt/data/hermes-mail')
    if local_root.exists():
        return local_root
    if opt_root.exists():
        return opt_root
    return local_root


ROOT = _resolve_root()
SETTINGS_PATH = ROOT / 'state' / 'settings.json'
SECRETS_PATH = ROOT / 'state' / 'secrets.json'
SECRETS_EXAMPLE_PATH = ROOT / 'state' / 'secrets.example.json'
LOG_PATH = ROOT / 'logs' / 'telegram-notifications.jsonl'
REPORT_DIR = ROOT / 'reports'
HERMES_DECISION_RECOMMENDATIONS_JSONL = ROOT / 'hermes-decision-recommendations.jsonl'

REQUIRED_SETTINGS_FIELDS = ['enabled', 'mode', 'chat_id', 'bot_token_source']
REQUIRED_SECRET_FIELDS = ['telegram_bot_token']
REAL_TEST_MESSAGE = 'Teste Hermes Telegram OK'
API_BASE = 'https://api.telegram.org'


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def load_settings() -> dict[str, Any]:
    data = load_json(SETTINGS_PATH)
    if not isinstance(data, dict):
        raise ValueError('settings.json must contain an object')
    return data


def load_secrets() -> dict[str, Any]:
    data = load_json(SECRETS_PATH)
    if not isinstance(data, dict):
        raise ValueError('secrets.json must contain an object')
    return data


def get_telegram_config() -> dict[str, Any]:
    settings = load_settings()
    telegram = settings.get('telegram_config')
    if not isinstance(telegram, dict):
        raise ValueError('missing telegram_config section in settings.json')
    return telegram


def mask(value: Any) -> Any:
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
        if len(value) <= 6:
            return '***'
        return f'{value[:3]}***{value[-3:]}'
    return '***'


def validate_settings(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    telegram = settings.get('telegram_config')
    if not isinstance(telegram, dict):
        return ['missing telegram_config section in settings.json']
    for field in REQUIRED_SETTINGS_FIELDS:
        if field not in telegram:
            errors.append(f'missing field: telegram_config.{field}')
    if telegram.get('mode') not in ('dry_run', 'real_test', 'production'):
        errors.append('telegram_config.mode must be "dry_run", "real_test", or "production"')
    if not isinstance(telegram.get('enabled'), bool):
        errors.append('telegram_config.enabled must be boolean')
    if telegram.get('bot_token_source') != 'secrets.json':
        errors.append('telegram_config.bot_token_source must be "secrets.json"')
    return errors


def validate_secrets_example() -> list[str]:
    errors: list[str] = []
    if not SECRETS_EXAMPLE_PATH.exists():
        return [f'missing file: {SECRETS_EXAMPLE_PATH}']
    try:
        secrets = load_json(SECRETS_EXAMPLE_PATH)
    except json.JSONDecodeError as exc:
        return [f'invalid JSON in {SECRETS_EXAMPLE_PATH}: {exc}']
    for field in REQUIRED_SECRET_FIELDS:
        if field not in secrets:
            errors.append(f'missing field in secrets.example.json: {field}')
    return errors


def show_safe() -> None:
    settings = load_settings()
    telegram = settings.get('telegram_config', {}) if isinstance(settings, dict) else {}
    if not isinstance(telegram, dict):
        telegram = {}
    secrets = {}
    if SECRETS_PATH.exists():
        try:
            secrets = load_secrets()
        except Exception:
            secrets = {}
    safe = {
        'settings_path': str(SETTINGS_PATH),
        'secrets_path': str(SECRETS_PATH),
        'telegram_config': {
            'enabled': telegram.get('enabled'),
            'mode': telegram.get('mode'),
            'chat_id': mask(telegram.get('chat_id')),
            'bot_token_source': telegram.get('bot_token_source'),
        },
        'token_present': bool(secrets.get('telegram_bot_token')),
        'masked_token': mask(secrets.get('telegram_bot_token')),
        'pending_fields': [item for item in [
            'chat_id' if not telegram.get('chat_id') else None,
            'telegram_bot_token' if not secrets.get('telegram_bot_token') else None,
        ] if item],
        'secrets_example_present': SECRETS_EXAMPLE_PATH.exists(),
        'queue_paths': {
            'manual_review': str(MANUAL_REVIEW_QUEUE_JSONL),
            'task_approvals': str(TASK_APPROVALS_JSONL),
        },
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))


def append_log(record: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def simulated_notifications() -> list[dict[str, Any]]:
    return [
        {'id': f'tg_new_email_{uuid.uuid4().hex[:8]}', 'type': 'new_email', 'message': 'Novo e-mail recebido: cotação simulada disponível.', 'source_event': 'incoming_email'},
        {'id': f'tg_supplier_reply_{uuid.uuid4().hex[:8]}', 'type': 'supplier_reply', 'message': 'Resposta de fornecedor recebida para acompanhamento.', 'source_event': 'supplier_reply'},
        {'id': f'tg_quote_extracted_{uuid.uuid4().hex[:8]}', 'type': 'quote_extracted', 'message': 'Cotação extraída com sucesso e pronta para validação.', 'source_event': 'quote_extracted'},
        {'id': f'tg_draft_generated_{uuid.uuid4().hex[:8]}', 'type': 'draft_generated', 'message': 'Draft de resposta gerado em modo dry_run.', 'source_event': 'draft_generated'},
        {'id': f'tg_send_registered_{uuid.uuid4().hex[:8]}', 'type': 'send_dry_run_registered', 'message': 'Envio dry_run registrado; nenhum envio real foi feito.', 'source_event': 'send_dry_run'},
        {'id': f'tg_error_{uuid.uuid4().hex[:8]}', 'type': 'error', 'message': 'Erro simulado para validar o canal de alerta.', 'source_event': 'pipeline_error'},
    ]


def create_report(kind: str, notifications: list[dict[str, Any]]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f'telegram-{kind}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json'
    payload = {
        'created_at': now(),
        'kind': kind,
        'notifications_count': len(notifications),
        'notifications': notifications,
        'log_path': str(LOG_PATH),
        'settings_path': str(SETTINGS_PATH),
    }
    with path.open('w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return path


def require_real_test_ready() -> tuple[dict[str, Any], dict[str, Any]]:
    settings = load_settings()
    telegram = settings.get('telegram_config')
    if not isinstance(telegram, dict):
        raise SystemExit('missing telegram_config section in settings.json')
    if telegram.get('enabled') is not True:
        raise SystemExit('safe abort: telegram_config.enabled must be true for real Telegram tests')
    if telegram.get('mode') not in ('real_test', 'production'):
        raise SystemExit('safe abort: telegram_config.mode must be "real_test" or "production" for real Telegram tests')
    secrets = load_secrets()
    token = str(secrets.get('telegram_bot_token', '') or '').strip()
    chat_id = str(telegram.get('chat_id', '') or '').strip()
    if not token:
        raise SystemExit('safe abort: telegram_bot_token is empty in secrets.json')
    if not chat_id:
        raise SystemExit('safe abort: telegram_config.chat_id is empty in settings.json')
    return telegram, secrets


def telegram_api_call(method: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    url = f'{API_BASE}/bot{token}/{method}'
    data = urllib.parse.urlencode(payload).encode('utf-8') if payload else None
    request = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode('utf-8')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise RuntimeError('Telegram API returned unexpected payload')
    if not result.get('ok'):
        raise RuntimeError(result.get('description') or 'Telegram API request failed')
    return result


def send_telegram_text(text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    telegram = settings.get('telegram_config', {})
    if not isinstance(telegram, dict):
        telegram = {}
    secrets = {}
    if SECRETS_PATH.exists():
        try:
            secrets = load_secrets()
        except Exception:
            secrets = {}
    token = str(secrets.get('telegram_bot_token') or secrets.get('telegram_token') or '').strip()
    chat_id = str(telegram.get('chat_id') or '').strip()
    payload = {
        'timestamp': now(),
        'telegram_config_enabled': telegram.get('enabled'),
        'mode': telegram.get('mode'),
        'message': text,
        'sent': False,
    }
    if not telegram.get('enabled') or not token or not chat_id:
        append_log(payload)
        return payload
    try:
        payload_fields = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': 'true'}
        if reply_markup is not None:
            payload_fields['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
        result = telegram_api_call('sendMessage', token, payload_fields)
        payload['sent'] = True
        payload['telegram_status'] = result.get('result', {}).get('message_id') if isinstance(result.get('result'), dict) else True
        payload['telegram_response_ok'] = True
    except Exception as exc:
        payload['error'] = str(exc)
    append_log(payload)
    return payload


def latest_by_task_id(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    with path.open('r', encoding='utf-8') as fh:
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
            task_id = str(rec.get('task_id') or rec.get('id') or '').strip()
            if not task_id:
                continue
            current = latest.get(task_id)
            if not current or str(rec.get('created_at') or '') >= str(current.get('created_at') or ''):
                latest[task_id] = rec
    return latest


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    try:
        settings = load_settings()
    except FileNotFoundError:
        errors.append(f'missing file: {SETTINGS_PATH}')
        settings = {}
    except json.JSONDecodeError as exc:
        errors.append(f'invalid JSON in {SETTINGS_PATH}: {exc}')
        settings = {}
    if isinstance(settings, dict):
        errors.extend(validate_settings(settings))
    errors.extend(validate_secrets_example())
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('mode=production')
    print(f'manual_review_queue={MANUAL_REVIEW_QUEUE_JSONL}')
    print(f'task_approvals={TASK_APPROVALS_JSONL}')
    return 0


def cmd_show_safe(_: argparse.Namespace) -> int:
    show_safe()
    return 0


def cmd_get_me(_: argparse.Namespace) -> int:
    telegram, secrets = require_real_test_ready()
    token = str(secrets.get('telegram_bot_token', '') or '').strip()
    safe_token = mask(token)
    result = telegram_api_call('getMe', token)
    user = result.get('result', {}) if isinstance(result, dict) else {}
    print('TELEGRAM GET_ME OK')
    print(f'token={safe_token}')
    print(f'bot_username={user.get("username") if isinstance(user, dict) else "unknown"}')
    print(f'bot_id={user.get("id") if isinstance(user, dict) else "unknown"}')
    print(f'mode={telegram.get("mode")}')
    return 0


def cmd_send_test_real(_: argparse.Namespace) -> int:
    telegram, secrets = require_real_test_ready()
    token = str(secrets.get('telegram_bot_token', '') or '').strip()
    chat_id = str(telegram.get('chat_id', '') or '').strip()
    safe_token = mask(token)
    result = telegram_api_call('sendMessage', token, {'chat_id': chat_id, 'text': REAL_TEST_MESSAGE})
    message = result.get('result', {}) if isinstance(result, dict) else {}
    print('TELEGRAM REAL TEST OK')
    print(f'token={safe_token}')
    print(f'chat_id={mask(chat_id)}')
    print(f'message_id={message.get("message_id") if isinstance(message, dict) else "unknown"}')
    print('message=Teste Hermes Telegram OK')
    return 0


def cmd_notify_dry_run(_: argparse.Namespace) -> int:
    settings = load_settings()
    telegram = settings.get('telegram_config', {})
    if not isinstance(telegram, dict):
        raise SystemExit('missing telegram_config section')
    notifications = simulated_notifications()
    for n in notifications:
        append_log({
            'timestamp': now(),
            'telegram_config_enabled': telegram.get('enabled'),
            'mode': telegram.get('mode'),
            'notification_id': n['id'],
            'type': n['type'],
            'message': n['message'],
            'source_event': n['source_event'],
            'dry_run': True,
        })
    report_path = create_report('notify', notifications)
    print('NOTIFY DRY RUN OK')
    print(f'example_message={notifications[0]["message"]}')
    print(f'report={report_path}')
    return 0


def cmd_notify_event(args: argparse.Namespace) -> int:
    settings = load_settings()
    telegram = settings.get('telegram_config', {})
    if not isinstance(telegram, dict):
        raise SystemExit('missing telegram_config section')
    notification_id = f'tg_{args.event_type}_{uuid.uuid4().hex[:8]}'
    record = {
        'timestamp': now(),
        'telegram_config_enabled': telegram.get('enabled'),
        'mode': telegram.get('mode'),
        'notification_id': notification_id,
        'type': args.event_type,
        'message': args.message,
        'source_event': args.source_event or args.event_type,
        'pipeline_run_id': args.pipeline_run_id,
        'dry_run': True,
    }
    append_log(record)
    if args.write_report:
        create_report('event', [record])
    print('NOTIFY EVENT DRY RUN OK')
    print(f'event_type={args.event_type}')
    print(f'message={args.message}')
    return 0


def cmd_report_dry_run(_: argparse.Namespace) -> int:
    report_path = create_report('report', simulated_notifications())
    print(report_path)
    return 0


def cmd_send_approval_request(args: argparse.Namespace) -> int:
    task = latest_task(args.task_id)
    if not task:
        print(json.dumps({'ok': False, 'error': f'task not found: {args.task_id}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    message = build_approval_request_message(task)
    reply_markup = {
        'inline_keyboard': [[
            {'text': 'APROVAR', 'callback_data': f'task_approve|{task["task_id"]}'},
            {'text': 'REJEITAR', 'callback_data': f'task_reject|{task["task_id"]}'},
        ]]
    }
    result = send_telegram_text(message, reply_markup=reply_markup)
    payload = {
        'ok': True,
        'task_id': args.task_id,
        'task': task,
        'message': message,
        'sent': bool(result.get('sent')),
        'mode': load_settings().get('telegram_config', {}).get('mode'),
        'transport': 'telegram' if result.get('sent') else 'log_only',
        'log_result': result,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0



def _load_purchase_recommendation(recommendation_id: str | None) -> dict[str, Any] | None:
    recommendations = load_jsonl_records(HERMES_DECISION_RECOMMENDATIONS_JSONL)
    if recommendation_id:
        for record in reversed(recommendations):
            if str(record.get('id') or record.get('recommendation_id') or '').strip() == recommendation_id:
                return record
    return latest_purchase_recommendation()


def cmd_send_purchase_decision(args: argparse.Namespace) -> int:
    recommendation = _load_purchase_recommendation(args.recommendation_id)
    if not recommendation:
        print(json.dumps({'ok': False, 'error': 'purchase recommendation not found'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    message = build_purchase_gate_message(recommendation)
    reply_markup = build_purchase_gate_reply_markup(recommendation)
    result = send_telegram_text(message, reply_markup=reply_markup)
    payload = {
        'ok': True,
        'recommendation_id': recommendation.get('id') or recommendation.get('recommendation_id'),
        'recommendation': recommendation,
        'message': message,
        'reply_markup': reply_markup,
        'sent': bool(result.get('sent')),
        'mode': load_settings().get('telegram_config', {}).get('mode'),
        'transport': 'telegram' if result.get('sent') else 'log_only',
        'log_result': result,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_poll_approvals(_: argparse.Namespace) -> int:
    pending = build_pending_tasks()
    decisions = latest_by_task_id(TASK_APPROVALS_JSONL)
    approved = [rec for rec in decisions.values() if str(rec.get('status') or '').lower() == 'approved']
    rejected = [rec for rec in decisions.values() if str(rec.get('status') or '').lower() == 'rejected']
    result = {
        'ok': True,
        'pending_count': len(pending),
        'approved_count': len(approved),
        'rejected_count': len(rejected),
        'pending': [
            {
                'task_id': task.get('task_id'),
                'task_type': task.get('task_type'),
                'subject': task.get('subject'),
                'from': task.get('from') or task.get('from_email'),
                'summary': task.get('summary'),
                'proposed_action': task.get('proposed_action'),
            }
            for task in pending
        ],
        'next_messages': [build_approval_request_message(task) for task in pending],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    log_count = 0
    if LOG_PATH.exists():
        with LOG_PATH.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    log_count += 1
    report_count = len(list(REPORT_DIR.glob('telegram-*.json')))
    pending = build_pending_tasks()
    print('TELEGRAM STATS')
    print(f'notifications_logged: {log_count}')
    print(f'reports: {report_count}')
    print(f'pending_approval_tasks: {len(pending)}')
    print(f'log_path: {LOG_PATH}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Dry-run Telegram notifier')
    sub = parser.add_subparsers(dest='command', required=True)
    p_validate = sub.add_parser('validate', help='Validate Telegram dry-run configuration')
    p_validate.set_defaults(func=cmd_validate)
    p_show = sub.add_parser('show-safe', help='Show masked Telegram config')
    p_show.set_defaults(func=cmd_show_safe)
    p_get_me = sub.add_parser('get-me', help='Call Telegram getMe in real_test mode')
    p_get_me.set_defaults(func=cmd_get_me)
    p_send_test = sub.add_parser('send-test-real', help='Send a single short Telegram message in real_test mode')
    p_send_test.set_defaults(func=cmd_send_test_real)
    p_approval = sub.add_parser('send-approval-request', help='Send an approval request for one task')
    p_approval.add_argument('task_id')
    p_approval.set_defaults(func=cmd_send_approval_request)
    p_purchase = sub.add_parser('send-purchase-decision', help='Send a purchase recommendation decision gate')
    p_purchase.add_argument('--recommendation-id', default='')
    p_purchase.set_defaults(func=cmd_send_purchase_decision)
    p_poll = sub.add_parser('poll-approvals', help='Show pending approvals and formatted next messages')
    p_poll.set_defaults(func=cmd_poll_approvals)
    p_notify = sub.add_parser('notify-dry-run', help='Write simulated Telegram notifications to the log')
    p_notify.set_defaults(func=cmd_notify_dry_run)
    p_event = sub.add_parser('notify-event', help='Write a single dry-run Telegram event notification')
    p_event.add_argument('--event-type', required=True)
    p_event.add_argument('--message', required=True)
    p_event.add_argument('--source-event', default='')
    p_event.add_argument('--pipeline-run-id', default='')
    p_event.add_argument('--write-report', action='store_true')
    p_event.set_defaults(func=cmd_notify_event)
    p_report = sub.add_parser('report-dry-run', help='Create a dry-run Telegram report')
    p_report.set_defaults(func=cmd_report_dry_run)
    p_stats = sub.add_parser('stats', help='Show log and report counts')
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
