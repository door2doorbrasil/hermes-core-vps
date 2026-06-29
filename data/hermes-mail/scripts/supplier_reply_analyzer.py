#!/usr/bin/env python3
"""Analyze supplier replies and link them to the originating RFQ.

Dry-run friendly: it never sends e-mail or leaves the local filesystem.
It can also create a simulated supplier reply so the end-to-end workflow can
be exercised without live mail traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdf_writer import A4_PORTRAIT, PdfDocument
from reporting_utils import (
    ASSETS_DIR,
    CLIENT_QUOTES_DIR,
    CLIENT_QUOTES_JSONL,
    COMPARISON_REPORTS_DIR,
    COMPARISON_REPORTS_JSONL,
    COTACOES_JSONL,
    EMAILS_JSONL,
    FORNECEDORES_JSONL,
    POLAR_SINERGY,
    SUPPLIER_REPLY_ANALYSIS_JSONL,
    append_jsonl,
    classify_missing_fields,
    compare_similarity,
    company_display_lines,
    control_number,
    count_jsonl,
    ensure_runtime_dirs,
    extract_first,
    format_percent,
    format_usd,
    heuristically_extract_supplier_reply,
    line_group_signature,
    load_json,
    load_jsonl_records,
    make_id,
    margin_policy,
    normalize_text,
    openai_json_completion,
    round_money,
    short_source_label,
    slugify,
    utc_now,
    vendor_display_name,
    write_json,
)

ROOT = Path('/opt/data/hermes-mail')
LOGS_DIR = ROOT / 'logs'
SCRIPTS_DIR = ROOT / 'scripts'
STATE_DIR = ROOT / 'state'

REQUIRED_PATHS = [
    ROOT,
    LOGS_DIR,
    STATE_DIR,
    SCRIPTS_DIR,
    ASSETS_DIR,
    CLIENT_QUOTES_DIR,
    COMPARISON_REPORTS_DIR,
    ROOT / 'emails' / 'incoming',
    ROOT / 'emails' / 'outgoing',
    ROOT / 'emails' / 'raw',
    ROOT / 'attachments' / 'original',
    ROOT / 'attachments' / 'extracted-text',
    ROOT / 'attachments' / 'ocr',
    ROOT / 'fornecedores',
    ROOT / 'contatos',
    ROOT / 'produtos',
    ROOT / 'cotacoes',
    ROOT / 'price-history',
    ROOT / 'reports',
]


def now() -> str:
    return utc_now()


def load_email_records() -> list[dict[str, Any]]:
    return load_jsonl_records(EMAILS_JSONL)


def load_quote_records() -> list[dict[str, Any]]:
    return load_jsonl_records(COTACOES_JSONL)


def load_analysis_records() -> list[dict[str, Any]]:
    return load_jsonl_records(SUPPLIER_REPLY_ANALYSIS_JSONL)


def lookup_latest_by_id(path: Path, field_name: str, value: Any) -> dict[str, Any] | None:
    if value in (None, ''):
        return None
    last = None
    for rec in load_jsonl_records(path):
        if rec.get(field_name) == value:
            last = rec
    return last


def latest_supplier_reply_email() -> dict[str, Any] | None:
    replies = [
        rec for rec in load_email_records()
        if rec.get('direction') == 'incoming' and (
            rec.get('reply_to_email_id')
            or rec.get('source') == 'supplier_reply_simulator'
            or rec.get('classification') == 'supplier_reply_simulated'
            or rec.get('status') in {'supplier_reply_simulated', 'supplier_reply_received'}
        )
    ]
    if replies:
        return replies[-1]
    return None


def latest_rfq_email_for_reply(reply_email: dict[str, Any]) -> dict[str, Any] | None:
    reply_to = reply_email.get('reply_to_email_id')
    if reply_to:
        for rec in load_email_records():
            if rec.get('id') == reply_to:
                return rec
    thread_id = reply_email.get('thread_id')
    if thread_id:
        candidate = None
        for rec in load_email_records():
            if rec.get('id') == reply_email.get('id'):
                continue
            if rec.get('thread_id') == thread_id or rec.get('message_id') == thread_id:
                candidate = rec
        if candidate:
            return candidate
    subject = normalize_text(str(reply_email.get('subject') or '')).lower()
    if subject.startswith('re:'):
        base = subject[3:].strip()
        for rec in reversed(load_email_records()):
            if rec.get('direction') != 'incoming':
                continue
            subj = normalize_text(str(rec.get('subject') or '')).lower()
            if subj == base or base in subj:
                return rec
    return None


def related_quote_for_rfq(rfq_email: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rfq_email:
        return None
    quote = None
    for rec in load_quote_records():
        if rec.get('email_id') == rfq_email.get('id'):
            quote = rec
    return quote


def existing_analysis_for_email(email_id: str) -> dict[str, Any] | None:
    match = None
    for rec in load_analysis_records():
        if rec.get('supplier_reply_email_id') == email_id:
            match = rec
    return match


def openai_extract(reply_email: dict[str, Any], rfq_email: dict[str, Any] | None) -> dict[str, Any] | None:
    system_prompt = (
        'Você extrai dados comerciais de respostas de fornecedores. '
        'Retorne apenas JSON válido.'
    )
    user_prompt = json.dumps(
        {
            'reply_email': reply_email,
            'rfq_email': rfq_email,
            'fields_required': [
                'supplier_price_usd', 'incoterm', 'lead_time_days', 'moq',
                'payment_terms', 'warranty', 'specifications', 'country',
                'description', 'translated_reply_ptbr', 'missing_fields'
            ],
            'instructions': [
                'Traduzir para português do Brasil.',
                'Extrair preço, incoterm, prazo, MOQ, pagamento, garantia e especificações.',
                'Se faltarem preço, incoterm ou descrição, incluir em missing_fields.',
                'Responder também com a fonte usada em source_notes.',
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    payload = openai_json_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.0,
    )
    if not isinstance(payload, dict):
        return None
    return payload


def normalize_openai_result(payload: dict[str, Any]) -> dict[str, Any]:
    extracted = {
        'supplier_price_usd': payload.get('supplier_price_usd'),
        'incoterm': payload.get('incoterm'),
        'lead_time_days': payload.get('lead_time_days'),
        'moq': payload.get('moq'),
        'payment_terms': payload.get('payment_terms'),
        'warranty': payload.get('warranty'),
        'specifications': payload.get('specifications'),
        'country': payload.get('country'),
        'description': payload.get('description'),
        'translated_reply_ptbr': payload.get('translated_reply_ptbr'),
        'source_notes': payload.get('source_notes') or payload.get('notes') or '',
    }
    if extracted['supplier_price_usd'] not in (None, ''):
        try:
            extracted['supplier_price_usd'] = round_money(float(extracted['supplier_price_usd']))
        except Exception:
            extracted['supplier_price_usd'] = None
    if extracted['lead_time_days'] not in (None, ''):
        try:
            extracted['lead_time_days'] = int(float(extracted['lead_time_days']))
        except Exception:
            extracted['lead_time_days'] = None
    if extracted['moq'] not in (None, ''):
        try:
            extracted['moq'] = int(float(extracted['moq']))
        except Exception:
            extracted['moq'] = None
    return extracted


def build_simulated_supplier_reply(rfq_email: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    product_name = ''
    product_desc = ''
    if quote:
        first_item = (quote.get('items') or [{}])[0]
        product_name = str(first_item.get('description') or '')
        product_desc = str(quote.get('notes') or first_item.get('notes') or '')
    if not product_name:
        product_name = str(rfq_email.get('subject') or 'Produto').replace('RFQ', '').strip() or 'PRODUTO TESTE'
    simulated_price = 9800.00
    body = '\n'.join([
        f'Product: {product_name}',
        f'Description: {product_desc or "Produto industrial com especificações equivalentes ao RFQ"}',
        'Country: China',
        'Price: USD 9,800.00',
        'Incoterm: FOB Shanghai',
        'Lead time: 35 days',
        'MOQ: 10',
        'Payment terms: 30% advance, 70% before shipment',
        'Warranty: 12 months',
        'Specifications: Conforme a solicitação original, com acabamento industrial padrão exportação.',
        '',
        'Best regards,',
        'Supplier Sales Team',
    ])
    reply_id = make_id('supplier_reply_sim')
    return {
        'id': reply_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'direction': 'incoming',
        'mode': 'dry_run',
        'status': 'supplier_reply_simulated',
        'classification': 'supplier_reply_simulated',
        'source': 'supplier_reply_simulator',
        'simulation': True,
        'reply_to_email_id': rfq_email.get('id'),
        'thread_id': rfq_email.get('thread_id') or rfq_email.get('message_id') or rfq_email.get('id'),
        'from': (lookup_latest_by_id(FORNECEDORES_JSONL, 'id', (quote or {}).get('supplier_id') or rfq_email.get('supplier_id')) or {}).get('emails', ['supplier@example.com'])[0],
        'to': [rfq_email.get('from') or POLAR_SINERGY['contact_email']],
        'subject': f"Re: {rfq_email.get('subject', 'RFQ')} - Supplier Reply",
        'body_text': body,
        'body_html_path': None,
        'supplier_id': (quote or {}).get('supplier_id') or rfq_email.get('supplier_id'),
        'contact_id': rfq_email.get('contact_id'),
        'product_ids': rfq_email.get('product_ids') or [],
        'simulation_price_usd': simulated_price,
        'raw_path': str(ROOT / 'emails' / 'incoming' / f'{reply_id}.eml'),
    }


def ensure_simulated_reply_exists() -> dict[str, Any]:
    latest_rfq = None
    for rec in load_email_records():
        if rec.get('direction') == 'incoming' and rec.get('status') == 'simulated' and rec.get('source') == 'simulator':
            latest_rfq = rec
    if not latest_rfq:
        raise SystemExit('no simulated RFQ e-mail found for supplier-reply simulation')
    quote = related_quote_for_rfq(latest_rfq)
    reply = build_simulated_supplier_reply(latest_rfq, quote)
    reply['reply_to_quote_id'] = quote.get('id') if quote else None
    Path(reply['raw_path']).write_text(reply['body_text'], encoding='utf-8')
    append_jsonl(EMAILS_JSONL, reply)
    return reply


def analyze_reply_email(reply_email: dict[str, Any]) -> dict[str, Any]:
    rfq_email = latest_rfq_email_for_reply(reply_email)
    rfq_quote = related_quote_for_rfq(rfq_email)
    body_text = str(reply_email.get('body_text') or '')

    openai_payload = openai_extract(reply_email, rfq_email)
    if openai_payload:
        extracted = normalize_openai_result(openai_payload)
        analysis_method = 'openai'
    else:
        extracted = heuristically_extract_supplier_reply(body_text)
        analysis_method = 'heuristic'

    missing_fields = classify_missing_fields(extracted)
    manual_review = bool(missing_fields)

    if not extracted.get('description') and rfq_quote:
        first_item = (rfq_quote.get('items') or [{}])[0]
        extracted['description'] = first_item.get('description') or rfq_quote.get('notes')
    if not extracted.get('incoterm') and rfq_quote:
        notes = str(rfq_quote.get('notes') or '')
        match = re.search(r'(?i)incoterm\s+([A-Z]{3}(?:\s+[A-Z][A-Za-z0-9-]+)?)', notes)
        if match:
            extracted['incoterm'] = match.group(1)
    if not extracted.get('supplier_price_usd'):
        extracted['supplier_price_usd'] = round_money(float((rfq_quote or {}).get('subtotal') or 0.0))
    if not extracted.get('lead_time_days'):
        extracted['lead_time_days'] = int((rfq_quote or {}).get('lead_time_days') or 0) or 35
    if not extracted.get('moq'):
        extracted['moq'] = 10
    if not extracted.get('payment_terms'):
        extracted['payment_terms'] = '30% antecipado, 70% antes do embarque'
    if not extracted.get('warranty'):
        extracted['warranty'] = '12 meses'
    if not extracted.get('specifications'):
        extracted['specifications'] = 'Conforme RFQ original, mantendo as características essenciais.'
    if not extracted.get('country'):
        extracted['country'] = 'China'

    if rfq_quote and rfq_quote.get('currency') == 'USD' and extracted.get('supplier_price_usd') is not None:
        extracted['supplier_price_usd'] = round_money(float(extracted['supplier_price_usd']))

    margin = margin_policy(float(extracted['supplier_price_usd'] or 0.0))
    summary_ptbr = extracted.get('translated_reply_ptbr') or (
        f"Fornecedor respondeu com preço {format_usd(extracted.get('supplier_price_usd'))}, "
        f"incoterm {extracted.get('incoterm')}, prazo {extracted.get('lead_time_days')} dias, "
        f"MOQ {extracted.get('moq')}, pagamento {extracted.get('payment_terms')}, "
        f"garantia {extracted.get('warranty')} e especificações {extracted.get('specifications')}."
    )

    supplier_record = lookup_latest_by_id(
        FORNECEDORES_JSONL,
        'id',
        (rfq_quote or {}).get('supplier_id') or (rfq_email or {}).get('supplier_id') or reply_email.get('supplier_id'),
    )
    supplier_name = vendor_display_name(
        (supplier_record or {}).get('name')
        or (supplier_record or {}).get('legal_name')
        or (reply_email.get('from') or ''),
        'Fornecedor não informado',
    )
    product_name = ''
    product_description = ''
    if rfq_quote:
        first_item = (rfq_quote.get('items') or [{}])[0]
        product_name = normalize_text(str(first_item.get('description') or ''))
        product_description = normalize_text(str(rfq_quote.get('notes') or first_item.get('notes') or ''))
    if not product_name:
        product_name = normalize_text(str(extracted.get('description') or 'PRODUTO'))

    analysis = {
        'id': make_id('reply_analysis'),
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'analysis_method': analysis_method,
        'analysis_model': os.environ.get('OPENAI_MODEL') if analysis_method == 'openai' else None,
        'source': 'supplier_reply_analyzer',
        'simulation': bool(reply_email.get('simulation')),
        'supplier_reply_email_id': reply_email.get('id'),
        'supplier_reply_source': reply_email.get('source'),
        'rfq_email_id': rfq_email.get('id') if rfq_email else None,
        'rfq_quote_id': rfq_quote.get('id') if rfq_quote else None,
        'thread_id': reply_email.get('thread_id'),
        'supplier_id': reply_email.get('supplier_id') or (rfq_email or {}).get('supplier_id'),
        'supplier_name': supplier_name,
        'contact_id': reply_email.get('contact_id') or (rfq_email or {}).get('contact_id'),
        'product_name': product_name,
        'product_description': product_description,
        'translated_reply_ptbr': summary_ptbr,
        'source_text': body_text,
        'source_notes': [
            short_source_label(reply_email),
            short_source_label(rfq_email or {}),
            short_source_label(rfq_quote or {}),
        ],
        'rfq_link_status': 'linked' if rfq_email else 'unlinked',
        'manual_review_reason': ', '.join(missing_fields) if missing_fields else None,
        'extracted': {
            'price_usd': extracted.get('supplier_price_usd'),
            'incoterm': extracted.get('incoterm'),
            'lead_time_days': extracted.get('lead_time_days'),
            'moq': extracted.get('moq'),
            'payment_terms': extracted.get('payment_terms'),
            'warranty': extracted.get('warranty'),
            'specifications': extracted.get('specifications'),
            'country': extracted.get('country'),
            'description': extracted.get('description'),
        },
        'missing_fields': missing_fields,
        'manual_review': manual_review,
        'analysis_status': 'manual_review' if manual_review else 'ready_for_quote',
        'margem_percentual': margin['margem_percentual'],
        'margem_valor': margin['margem_valor'],
        'valor_total_com_margem': margin['valor_total_com_margem'],
        'margem_regra': margin['margem_regra'],
        'currency': 'USD',
        'currency_quote': 'USD',
        'analysis_signature': line_group_signature({
            'product_name': product_name,
            'description': product_description,
            'incoterm': extracted.get('incoterm'),
            'specifications': extracted.get('specifications'),
        }),
    }
    return analysis


def write_analysis(analysis: dict[str, Any]) -> Path:
    append_jsonl(SUPPLIER_REPLY_ANALYSIS_JSONL, analysis)
    return SUPPLIER_REPLY_ANALYSIS_JSONL


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    for path in REQUIRED_PATHS:
        if not path.exists():
            errors.append(f'missing path: {path}')
    for path in [EMAILS_JSONL, COTACOES_JSONL]:
        if not path.exists():
            errors.append(f'missing file: {path}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'analysis_jsonl={SUPPLIER_REPLY_ANALYSIS_JSONL}')
    return 0


def cmd_simulate_analysis(_: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    reply = ensure_simulated_reply_exists()
    print('SIMULATED SUPPLIER REPLY CREATED')
    print(f'reply_email_id={reply["id"]}')
    print(f'reply_to_email_id={reply.get("reply_to_email_id")}')
    print(f'path={reply["raw_path"]}')
    return 0


def cmd_analyze_latest(_: argparse.Namespace) -> int:
    reply = latest_supplier_reply_email()
    if not reply:
        raise SystemExit('no supplier reply e-mail found; run simulate-analysis first or ingest a real reply')
    existing = existing_analysis_for_email(str(reply.get('id')))
    if existing:
        print('LATEST SUPPLIER REPLY ALREADY ANALYZED')
        print(f'analysis_id={existing.get("id")}')
        print(f'manual_review={existing.get("manual_review")}')
        print(f'margem_valor={existing.get("margem_valor")}')
        return 0
    analysis = analyze_reply_email(reply)
    write_analysis(analysis)
    print('ANALYSIS CREATED')
    print(f'analysis_id={analysis["id"]}')
    print(f'rfq_email_id={analysis.get("rfq_email_id")}')
    print(f'supplier_reply_email_id={analysis.get("supplier_reply_email_id")}')
    print(f'analysis_status={analysis["analysis_status"]}')
    print(f'missing_fields={analysis["missing_fields"]}')
    print(f'margem_regra={analysis["margem_regra"]}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    analyses = load_analysis_records()
    replies = [rec for rec in load_email_records() if rec.get('direction') == 'incoming' and (rec.get('reply_to_email_id') or rec.get('source') == 'supplier_reply_simulator' or rec.get('status') == 'supplier_reply_received')]
    rfqs = [rec for rec in load_email_records() if rec.get('direction') == 'incoming' and rec.get('status') == 'simulated' and rec.get('source') == 'simulator']
    responded = {rec.get('reply_to_email_id') for rec in replies if rec.get('reply_to_email_id')}
    sent = len(rfqs)
    received = len(replies)
    without = max(sent - len(responded), 0)
    rate = 0.0 if sent == 0 else round_money((len(responded) / sent) * 100.0)
    manual = sum(1 for rec in analyses if rec.get('manual_review'))
    print('SUPPLIER REPLY ANALYZER STATS')
    print(f'rfqs_sent: {sent}')
    print(f'replies_received: {received}')
    print(f'suppliers_without_response: {without}')
    print(f'response_rate_percent: {rate:.2f}')
    print(f'analyses: {len(analyses)}')
    print(f'manual_review: {manual}')
    print(f'analysis_jsonl: {SUPPLIER_REPLY_ANALYSIS_JSONL}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Analyze supplier reply e-mails')
    sub = parser.add_subparsers(dest='command', required=True)
    p_validate = sub.add_parser('validate', help='Validate directories and data stores')
    p_validate.set_defaults(func=cmd_validate)
    p_sim = sub.add_parser('simulate-analysis', help='Create a simulated supplier reply for dry-run testing')
    p_sim.set_defaults(func=cmd_simulate_analysis)
    p_latest = sub.add_parser('analyze-latest', help='Analyze the latest supplier reply e-mail')
    p_latest.set_defaults(func=cmd_analyze_latest)
    p_stats = sub.add_parser('stats', help='Show RFQ/reply analysis stats')
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
