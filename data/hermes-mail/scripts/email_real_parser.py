#!/usr/bin/env python3
"""Parse real incoming emails and materialize supplier/product/quote records.

The parser is intentionally conservative:
- it never deletes or mutates source e-mails;
- it appends analysis and entity records to JSONL stores;
- it translates supplier replies to pt-BR when possible;
- it links replies to RFQs when a matching thread/reference exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from email_real_common import (
    EMAILS_JSONL,
    ROOT,
    append_record,
    ensure_storage,
    latest_record,
    load_jsonl_safe,
    now,
    notify_telegram,
    parse_reply_insights,
    product_identity_from_email,
    supplier_identity_from_email,
)
from reporting_utils import count_jsonl, normalize_text
from supplier_reply_analyzer import analyze_reply_email

CONTACTS_JSONL = ROOT / 'contatos.jsonl'
PRODUCTS_JSONL = ROOT / 'produtos.jsonl'
SUPPLIERS_JSONL = ROOT / 'fornecedores.jsonl'
QUOTES_JSONL = ROOT / 'cotacoes.jsonl'
ANALYSIS_JSONL = ROOT / 'supplier-reply-analysis.jsonl'


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
    for path in [EMAILS_JSONL, CONTACTS_JSONL, PRODUCTS_JSONL, SUPPLIERS_JSONL, QUOTES_JSONL, ANALYSIS_JSONL]:
        errors.extend(validate_jsonl(path))
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('process=real-email-parser')
    return 0


def email_already_analyzed(email_id: str) -> bool:
    return latest_record(ANALYSIS_JSONL, lambda rec: rec.get('supplier_reply_email_id') == email_id) is not None


def email_lookup(email_id: str) -> dict[str, Any] | None:
    return latest_record(EMAILS_JSONL, lambda rec: rec.get('id') == email_id)


def latest_pending_email(limit: int = 1) -> list[dict[str, Any]]:
    emails = [rec for rec in load_jsonl_safe(EMAILS_JSONL) if rec.get('direction') == 'incoming' and rec.get('source') == 'imap_ingestor']
    pending = [rec for rec in emails if not email_already_analyzed(str(rec.get('id') or ''))]
    return pending[-limit:] if limit > 0 else []


def deterministic_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"


def ensure_record(path: Path, record_id: str, record: dict[str, Any]) -> bool:
    existing = latest_record(path, lambda rec: rec.get('id') == record_id)
    if existing:
        return False
    append_record(path, record)
    return True


def build_supplier_record(email_record: dict[str, Any]) -> dict[str, Any]:
    supplier_core = supplier_identity_from_email(str(email_record.get('from_name') or ''), str(email_record.get('from') or ''))
    supplier_id = deterministic_id('supplier_real', str(email_record.get('from') or '').lower())
    return {
        'id': supplier_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'status': supplier_core['status'],
        'source': 'email_real_parser',
        'source_email_id': email_record.get('id'),
        'source_message_id': email_record.get('message_id'),
        'name': supplier_core['name'],
        'legal_name': supplier_core['legal_name'],
        'emails': supplier_core['emails'],
        'website': supplier_core['website'],
        'domain': supplier_core['domain'],
        'language': 'en',
        'notes': normalize_text(str(email_record.get('subject') or '')),
    }


def build_contact_record(email_record: dict[str, Any], supplier_id: str) -> dict[str, Any]:
    contact_core = supplier_identity_from_email(str(email_record.get('from_name') or ''), str(email_record.get('from') or ''))
    contact_id = deterministic_id('contact_real', f"{email_record.get('from') or ''}|{email_record.get('from_name') or ''}")
    return {
        'id': contact_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'status': 'active',
        'source': 'email_real_parser',
        'source_email_id': email_record.get('id'),
        'supplier_id': supplier_id,
        'name': contact_core['name'],
        'role': 'Sales',
        'email': str(email_record.get('from') or '').lower(),
        'preferred_channel': 'email',
        'notes': normalize_text(str(email_record.get('subject') or '')),
    }


def build_product_record(email_record: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    product_core = product_identity_from_email(str(email_record.get('subject') or ''), str(email_record.get('body_text') or ''), analysis)
    product_id = deterministic_id('product_real', f"{email_record.get('subject') or ''}|{analysis.get('product_name') or analysis.get('description') or ''}")
    return {
        'id': product_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'source': 'email_real_parser',
        'source_email_id': email_record.get('id'),
        'name': product_core['name'],
        'sku': product_core['sku'],
        'description': product_core['description'],
        'unit': product_core['unit'],
        'category': product_core['category'],
        'brand': product_core['brand'],
        'active': True,
        'aliases': product_core['aliases'],
    }


def build_quote_record(email_record: dict[str, Any], analysis: dict[str, Any], supplier_id: str, contact_id: str, product_id: str) -> dict[str, Any]:
    extracted = analysis.get('extracted') or {}
    quote_id = deterministic_id('quote_real', str(email_record.get('id') or email_record.get('message_id') or 'quote'))
    price = extracted.get('price_usd')
    status = 'received' if price not in (None, '') else 'needs_review'
    product_name = analysis.get('product_name') or extracted.get('description') or email_record.get('subject') or 'Produto não identificado'
    item = {
        'product_id': product_id,
        'description': product_name,
        'quantity': 1,
        'unit': 'unit',
        'unit_price_usd': price,
        'notes': analysis.get('translated_reply_ptbr') or '',
    }
    rfq_email_id = analysis.get('rfq_email_id')
    rfq_id = analysis.get('rfq_quote_id') or analysis.get('rfq_email_id') or email_record.get('thread_id')
    return {
        'id': quote_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'source': 'email_real_parser',
        'source_email_id': email_record.get('id'),
        'message_id': email_record.get('message_id'),
        'thread_id': email_record.get('thread_id'),
        'rfq_id': rfq_id,
        'rfq_email_id': rfq_email_id,
        'supplier_id': supplier_id,
        'contact_id': contact_id,
        'product_id': product_id,
        'quote_number': f"Q-{quote_id[-6:].upper()}",
        'currency': 'USD' if price not in (None, '') else None,
        'items': [item],
        'subtotal_usd': price,
        'total_usd': price,
        'incoterm': extracted.get('incoterm'),
        'lead_time_days': extracted.get('lead_time_days'),
        'moq': extracted.get('moq'),
        'payment_terms': extracted.get('payment_terms'),
        'warranty': extracted.get('warranty'),
        'specifications': extracted.get('specifications'),
        'country': extracted.get('country'),
        'translated_reply_ptbr': analysis.get('translated_reply_ptbr'),
        'status': status,
        'analysis_id': analysis.get('id'),
        'manual_review': bool(analysis.get('manual_review')),
    }


def process_email(email_record: dict[str, Any]) -> dict[str, Any]:
    analysis = analyze_reply_email(email_record)
    insights = parse_reply_insights(str(email_record.get('body_text') or ''))
    analysis = {**analysis, 'insights': insights}

    supplier_record = build_supplier_record(email_record)
    supplier_created = ensure_record(SUPPLIERS_JSONL, supplier_record['id'], supplier_record)

    contact_record = build_contact_record(email_record, supplier_record['id'])
    contact_created = ensure_record(CONTACTS_JSONL, contact_record['id'], contact_record)

    product_record = build_product_record(email_record, analysis)
    product_created = ensure_record(PRODUCTS_JSONL, product_record['id'], product_record)

    quote_record = build_quote_record(email_record, analysis, supplier_record['id'], contact_record['id'], product_record['id'])
    quote_created = ensure_record(QUOTES_JSONL, quote_record['id'], quote_record)

    summary = {
        'email_id': email_record.get('id'),
        'message_id': email_record.get('message_id'),
        'thread_id': email_record.get('thread_id'),
        'supplier_id': supplier_record['id'],
        'contact_id': contact_record['id'],
        'product_id': product_record['id'],
        'quote_id': quote_record['id'],
        'analysis_id': analysis.get('id'),
        'translated_reply_ptbr': analysis.get('translated_reply_ptbr'),
        'created': {
            'supplier': supplier_created,
            'contact': contact_created,
            'product': product_created,
            'quote': quote_created,
        },
        'insights': {
            'price_usd': insights.get('supplier_price_usd'),
            'incoterm': insights.get('incoterm'),
            'lead_time_days': insights.get('lead_time_days'),
            'moq': insights.get('moq'),
        },
        'analysis': analysis,
    }
    append_record(ANALYSIS_JSONL, {
        'id': analysis.get('id'),
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'source': 'email_real_parser',
        'supplier_reply_email_id': email_record.get('id'),
        'supplier_reply_message_id': email_record.get('message_id'),
        'rfq_email_id': analysis.get('rfq_email_id'),
        'rfq_quote_id': analysis.get('rfq_quote_id'),
        'thread_id': email_record.get('thread_id'),
        'supplier_id': supplier_record['id'],
        'contact_id': contact_record['id'],
        'product_id': product_record['id'],
        'quote_id': quote_record['id'],
        'translated_reply_ptbr': analysis.get('translated_reply_ptbr'),
        'analysis': analysis,
    })
    return summary


def cmd_process_latest(args: argparse.Namespace) -> int:
    ensure_storage()
    pending = latest_pending_email(args.limit)
    if not pending:
        print(json.dumps({'ok': True, 'processed': [], 'message': 'no pending incoming emails'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    processed: list[dict[str, Any]] = []
    for email_record in pending:
        processed.append(process_email(email_record))
    notify_telegram('supplier_reply_processed', f"Processed {len(processed)} real incoming email(s)", metadata={'processed_count': len(processed), 'email_ids': [item['email_id'] for item in processed]})
    print(json.dumps({
        'ok': True,
        'mode': 'production_controlled',
        'processed_count': len(processed),
        'processed': processed,
        'counts': {
            'emails': count_jsonl(EMAILS_JSONL),
            'suppliers': count_jsonl(SUPPLIERS_JSONL),
            'contacts': count_jsonl(CONTACTS_JSONL),
            'products': count_jsonl(PRODUCTS_JSONL),
            'quotes': count_jsonl(QUOTES_JSONL),
            'analysis': count_jsonl(ANALYSIS_JSONL),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_storage()
    print(json.dumps({
        'ok': True,
        'emails': count_jsonl(EMAILS_JSONL),
        'suppliers': count_jsonl(SUPPLIERS_JSONL),
        'contacts': count_jsonl(CONTACTS_JSONL),
        'products': count_jsonl(PRODUCTS_JSONL),
        'quotes': count_jsonl(QUOTES_JSONL),
        'analysis': count_jsonl(ANALYSIS_JSONL),
        'pending_emails': len(latest_pending_email(limit=1000)),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes Mail real email parser')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate local JSONL stores')
    p_validate.set_defaults(func=cmd_validate)

    p_process = sub.add_parser('process-latest', help='Process the latest pending incoming email')
    p_process.add_argument('--limit', type=int, default=1)
    p_process.set_defaults(func=cmd_process_latest)

    p_stats = sub.add_parser('stats', help='Show record counts')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
