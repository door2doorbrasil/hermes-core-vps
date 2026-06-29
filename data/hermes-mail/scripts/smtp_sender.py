#!/usr/bin/env python3
"""Controlled SMTP sender for Hermes Mail.

The sender creates English RFQ drafts, records them in the approval queue,
and only performs a real SMTP send when the explicit `approve-send` command
is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from email_real_common import (
    APPROVAL_QUEUE_JSONL,
    EMAILS_JSONL,
    OUTGOING_DIR,
    ROOT,
    append_record,
    connect_smtp,
    control_number,
    ensure_storage,
    get_email_context_snapshot,
    load_jsonl_safe,
    load_settings,
    mask_email,
    message_from_email_record,
    now,
    notify_telegram,
    smtp_state_snapshot,
)
from reporting_utils import count_jsonl, latest_jsonl_record, normalize_text, write_json

try:
    from hermes_memory import log_action as log_memory_action
except Exception:  # pragma: no cover - optional integration
    log_memory_action = None

RFQ_DRAFTS_JSONL = ROOT / 'rfq-drafts.jsonl'
FORNECEDORES_JSONL = ROOT / 'fornecedores.jsonl'
CONTATOS_JSONL = ROOT / 'contatos.jsonl'
PRODUCTS_JSONL = ROOT / 'produtos.jsonl'
COTACOES_JSONL = ROOT / 'cotacoes.jsonl'
SENT_DIR = OUTGOING_DIR / 'sent'


def validate_settings() -> list[str]:
    errors: list[str] = []
    buy = get_email_context_snapshot('buy')
    if not buy['smtp']['configured']:
        missing = ', '.join(buy['smtp']['missing']) or 'BUY_SMTP_*'
        errors.append(f'buy SMTP not configured: {missing}')
    sales = get_email_context_snapshot('sales')
    if sales['context'] == 'sales' and not sales['configured']:
        # Optional by design; do not block validation.
        pass
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


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_storage()
    errors: list[str] = []
    errors.extend(validate_settings())
    for path in [RFQ_DRAFTS_JSONL, APPROVAL_QUEUE_JSONL, EMAILS_JSONL]:
        errors.extend(validate_jsonl(path))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    return 0


def latest_record(path: Path) -> dict[str, Any] | None:
    return latest_jsonl_record(path, None)


def latest_nonempty(path: Path, predicate) -> dict[str, Any] | None:
    return latest_jsonl_record(path, predicate)


def build_draft_context() -> dict[str, Any]:
    supplier = latest_nonempty(FORNECEDORES_JSONL, lambda rec: True)
    product = latest_nonempty(PRODUCTS_JSONL, lambda rec: True)
    contact = latest_nonempty(CONTATOS_JSONL, lambda rec: True)
    quote = latest_nonempty(COTACOES_JSONL, lambda rec: True)
    return {
        'supplier': supplier,
        'product': product,
        'contact': contact,
        'quote': quote,
    }


def build_test_draft() -> dict[str, Any]:
    context = build_draft_context()
    supplier = context['supplier'] or {}
    product = context['product'] or {}
    quote = context['quote'] or {}
    supplier_name = normalize_text(str(supplier.get('name') or supplier.get('legal_name') or 'Supplier')) or 'Supplier'
    recipient = ''
    emails = supplier.get('emails') or []
    if emails:
        recipient = str(emails[0])
    if not recipient and context['contact'] and context['contact'].get('email'):
        recipient = str(context['contact']['email'])
    if not recipient:
        recipient = 'supplier@example.com'
    product_name = normalize_text(str(product.get('name') or 'Industrial product')) or 'Industrial product'
    product_desc = normalize_text(str(product.get('description') or product_name))
    quantity = '1 unit'
    region = 'Global'
    application = 'Industrial sourcing'
    customer = 'Polar Sinergy'
    notes = 'Please reply in English. This is a controlled sourcing request.'
    draft_seed = f"{recipient}|{product_name}|{product_desc}|{now()}"
    draft_id = f"smtp_draft_{hashlib.sha1(draft_seed.encode('utf-8')).hexdigest()[:12]}"
    rfq_id = f"rfq_{hashlib.sha1((draft_id + '|rfq').encode('utf-8')).hexdigest()[:12]}"
    thread_id = f"thread_{hashlib.sha1((draft_id + '|thread').encode('utf-8')).hexdigest()[:12]}"
    control = control_number('RFQ')
    subject = f"RFQ {control} - {product_name}"
    body = "\n".join([
        f"Hello {supplier_name},",
        "",
        f"We are sourcing: {product_name}",
        f"Description: {product_desc}",
        f"Quantity: {quantity}",
        f"Region: {region}",
        f"Application: {application}",
        f"Customer: {customer}",
        f"Notes: {notes}",
        "",
        "Please reply in English with:",
        "- Unit price",
        "- MOQ",
        "- Lead time",
        "- Incoterm",
        "- Payment terms",
        "- Warranty",
        "- Technical specifications",
        "- Packaging details",
        "",
        "Best regards,",
        "Polar Sinergy Purchasing Team",
    ])
    raw_path = OUTGOING_DIR / f'{draft_id}.eml'
    raw_path.write_text(
        f"From: {get_settings_email()}\nTo: {recipient}\nSubject: {subject}\nMessage-ID: <{draft_id}@hermes-mail.local>\nIn-Reply-To: <{thread_id}@hermes-mail.local>\nReferences: <{thread_id}@hermes-mail.local>\nX-RFQ-ID: {rfq_id}\nX-Thread-ID: {thread_id}\nX-Control-Number: {control}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=\"utf-8\"\n\n{body}\n",
        encoding='utf-8',
    )
    draft = {
        'id': draft_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'source': 'smtp_sender',
        'draft_type': 'test_rfq',
        'status': 'pending_manual_approval',
        'approval_required': True,
        'from': get_settings_email(),
        'to': [recipient],
        'cc': [],
        'bcc': [],
        'recipient_name': supplier_name,
        'subject': subject,
        'body_text': body,
        'raw_path': str(raw_path),
        'rfq_id': rfq_id,
        'thread_id': thread_id,
        'control_number': control,
        'supplier_id': supplier.get('id'),
        'contact_id': (context['contact'] or {}).get('id'),
        'product_id': product.get('id'),
        'quote_id': quote.get('id'),
        'quantity': quantity,
        'region': region,
        'application': application,
        'customer': customer,
        'notes': notes,
        'message_id': f'<{draft_id}@hermes-mail.local>',
        'language': 'en',
        'reply_translation_target': 'pt-BR',
    }
    append_record(RFQ_DRAFTS_JSONL, draft)
    append_record(EMAILS_JSONL, {
        'id': draft_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'direction': 'outgoing',
        'mode': 'production_controlled',
        'status': 'draft_pending_approval',
        'source': 'smtp_sender',
        'approval_required': True,
        'from': get_settings_email(),
        'to': [recipient],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'body_text': body,
        'raw_path': str(raw_path),
        'message_id': draft['message_id'],
        'thread_id': thread_id,
        'rfq_id': rfq_id,
        'control_number': control,
        'supplier_id': supplier.get('id'),
        'contact_id': (context['contact'] or {}).get('id'),
        'product_id': product.get('id'),
        'quote_id': quote.get('id'),
    })
    append_record(APPROVAL_QUEUE_JSONL, {
        'id': f'approval_evt_{hashlib.sha1((draft_id + "|pending").encode("utf-8")).hexdigest()[:12]}',
        'queue_key': draft_id,
        'item_type': 'smtp_send',
        'draft_id': draft_id,
        'rfq_id': rfq_id,
        'thread_id': thread_id,
        'control_number': control,
        'status': 'pending',
        'created_at': now(),
        'updated_at': now(),
        'source': 'smtp_sender',
    })
    smtp_state_snapshot(last_draft_id=draft_id, last_rfq_id=rfq_id, last_thread_id=thread_id, last_message_id=draft['message_id'], last_send_at=None)
    notify_telegram('smtp_draft_created', f"Created RFQ draft {draft_id} for {recipient}", metadata=draft)
    if log_memory_action:
        try:
            log_memory_action(
                module='smtp_sender',
                action='create-test-draft',
                action_type='draft_creation',
                company='Polar Sinergy LLC',
                country='',
                product=product_name,
                origin='smtp',
                result={'draft_id': draft_id, 'rfq_id': rfq_id, 'queue_status': 'pending'},
                summary='Created controlled RFQ draft awaiting manual approval',
                learning='Drafts require manual approval before SMTP send',
            )
        except Exception:
            pass
    return draft


def get_settings_email() -> str:
    settings = load_settings()
    email_cfg = settings.get('email_config', {}) if isinstance(settings, dict) else {}
    if isinstance(email_cfg, dict) and email_cfg.get('email_address'):
        return str(email_cfg.get('email_address'))
    return 'buyer@polarsinergy.com'


def cmd_create_test_draft(_: argparse.Namespace) -> int:
    ensure_storage()
    draft = build_test_draft()
    print(json.dumps({
        'ok': True,
        'action': 'create-test-draft',
        'draft': draft,
        'counts': {
            'drafts': count_jsonl(RFQ_DRAFTS_JSONL),
            'pending_approvals': len([rec for rec in load_jsonl_safe(APPROVAL_QUEUE_JSONL) if rec.get('status') == 'pending']),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_preview_latest(_: argparse.Namespace) -> int:
    ensure_storage()
    draft = latest_record(RFQ_DRAFTS_JSONL)
    if not draft:
        print(json.dumps({'ok': True, 'message': 'no draft found'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    queue = latest_jsonl_record(APPROVAL_QUEUE_JSONL, lambda rec: rec.get('queue_key') == draft.get('id'))
    print(json.dumps({
        'ok': True,
        'draft': draft,
        'approval_queue': queue,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _queue_status_for_draft(draft_id: str) -> dict[str, Any] | None:
    return latest_jsonl_record(APPROVAL_QUEUE_JSONL, lambda rec: rec.get('queue_key') == draft_id)


def cmd_approve_send(args: argparse.Namespace) -> int:
    ensure_storage()
    draft_id = getattr(args, 'draft_id', None) or getattr(args, 'id', None)
    draft = latest_jsonl_record(RFQ_DRAFTS_JSONL, lambda rec: rec.get('id') == draft_id)
    if not draft:
        print(json.dumps({'ok': False, 'error': f'draft not found: {draft_id}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    queue = _queue_status_for_draft(draft_id)
    if not queue or queue.get('status') != 'approved':
        print(json.dumps({'ok': False, 'error': 'draft must be approved in approval queue before sending', 'draft_id': draft_id, 'queue_status': queue.get('status') if queue else None}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    message = message_from_email_record(draft)
    message['Message-ID'] = draft.get('message_id') or f"<{draft['id']}@hermes-mail.local>"
    message['X-RFQ-ID'] = str(draft.get('rfq_id') or '')
    message['X-Thread-ID'] = str(draft.get('thread_id') or '')
    message['X-Control-Number'] = str(draft.get('control_number') or '')
    try:
        smtp = connect_smtp(context=args.context, tls=bool(load_settings().get('smtp_config', {}).get('tls', True)))
    except Exception as exc:
        print(json.dumps({'ok': False, 'draft_id': args.draft_id, 'error': f'smtp_connect_failed: {exc}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    sent_path = SENT_DIR / f"{draft['id']}.eml"
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        response = smtp.send_message(message)
        sent_path.write_bytes(message.as_bytes())
        sent_event = {
            'id': f"sent_evt_{hashlib.sha1((draft['id'] + '|sent').encode('utf-8')).hexdigest()[:12]}",
            'version': '0.1.0',
            'created_at': now(),
            'updated_at': now(),
            'direction': 'outgoing',
            'mode': 'production_controlled',
            'status': 'sent_real',
            'source': 'smtp_sender',
            'parent_draft_id': draft['id'],
            'draft_id': draft['id'],
            'from': draft.get('from'),
            'to': draft.get('to'),
            'cc': draft.get('cc', []),
            'bcc': draft.get('bcc', []),
            'subject': draft.get('subject'),
            'body_text': draft.get('body_text'),
            'raw_path': str(sent_path),
            'message_id': message['Message-ID'],
            'rfq_id': draft.get('rfq_id'),
            'thread_id': draft.get('thread_id'),
            'control_number': draft.get('control_number'),
            'smtp_response': response,
        }
        append_record(EMAILS_JSONL, sent_event)
        append_record(APPROVAL_QUEUE_JSONL, {
            'id': f"approval_evt_{hashlib.sha1((draft['id'] + '|sent').encode('utf-8')).hexdigest()[:12]}",
            'queue_key': draft['id'],
            'item_type': 'smtp_send',
            'draft_id': draft['id'],
            'rfq_id': draft.get('rfq_id'),
            'thread_id': draft.get('thread_id'),
            'control_number': draft.get('control_number'),
            'status': 'sent',
            'created_at': now(),
            'updated_at': now(),
            'source': 'smtp_sender',
            'smtp_response': response,
        })
        smtp_state_snapshot(last_draft_id=draft['id'], last_rfq_id=draft.get('rfq_id'), last_thread_id=draft.get('thread_id'), last_message_id=message['Message-ID'], last_send_at=now())
        notify_telegram('smtp_send_approved', f"Sent RFQ draft {draft['id']} to {draft.get('to', [''])[0] if draft.get('to') else 'unknown'}", metadata=sent_event)
        if log_memory_action:
            try:
                log_memory_action(
                    module='smtp_sender',
                    action='approve-send',
                    action_type='smtp_send',
                    company='Polar Sinergy LLC',
                    country='',
                    product=str(draft.get('subject') or ''),
                    origin='smtp',
                    result=sent_event,
                    summary='Approved SMTP send completed',
                    learning='SMTP real sends must stay manual-approved',
                )
            except Exception:
                pass
        print(json.dumps({'ok': True, 'draft_id': draft['id'], 'sent_path': str(sent_path), 'smtp_response': response, 'to': draft.get('to'), 'rfq_id': draft.get('rfq_id'), 'thread_id': draft.get('thread_id'), 'control_number': draft.get('control_number')}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'draft_id': draft['id'], 'error': f'smtp_send_failed: {exc}'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def cmd_stats(args: argparse.Namespace) -> int:
    ensure_storage()
    snapshot = get_email_context_snapshot(args.context)
    if snapshot['context'] == 'sales' and not snapshot['configured']:
        print('Vendas ainda não configurado')
        return 0
    print(json.dumps({
        'ok': True,
        'context': snapshot['context'],
        'drafts': count_jsonl(RFQ_DRAFTS_JSONL),
        'emails': count_jsonl(EMAILS_JSONL),
        'pending_approval_events': len([rec for rec in load_jsonl_safe(APPROVAL_QUEUE_JSONL) if rec.get('status') == 'pending']),
        'sent_events': len([rec for rec in load_jsonl_safe(APPROVAL_QUEUE_JSONL) if rec.get('status') == 'sent']),
        'identity': snapshot['identity'],
        'mode': 'production_controlled',
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail controlled SMTP sender')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate local SMTP configuration and stores')
    p_validate.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_validate.set_defaults(func=cmd_validate)

    p_create = sub.add_parser('create-test-draft', help='Create an English test RFQ draft')
    p_create.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_create.set_defaults(func=cmd_create_test_draft)

    p_preview = sub.add_parser('preview-latest', help='Preview the latest RFQ draft')
    p_preview.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_preview.set_defaults(func=cmd_preview_latest)

    p_send = sub.add_parser('approve-send', help='Approve and send a draft through real SMTP')
    p_send.add_argument('draft_id')
    p_send.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_send.set_defaults(func=cmd_approve_send)

    p_send_alias = sub.add_parser('send-approved', help='Alias for approve-send with --id')
    p_send_alias.add_argument('--id', dest='id', required=True)
    p_send_alias.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_send_alias.set_defaults(func=cmd_approve_send)

    p_stats = sub.add_parser('stats', help='Show sender stats')
    p_stats.add_argument('--context', default='buy', choices=['buy', 'sales'])
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
