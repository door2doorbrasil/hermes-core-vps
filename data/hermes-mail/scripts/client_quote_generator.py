#!/usr/bin/env python3
"""Generate client-facing quotation PDFs from supplier-reply analyses.

Dry-run only: it never sends mail automatically.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pdf_writer import PdfDocument
from reporting_utils import (
    ASSETS_DIR,
    CLIENT_QUOTES_DIR,
    CLIENT_QUOTES_JSONL,
    COTACOES_JSONL,
    POLAR_SINERGY,
    SUPPLIER_REPLY_ANALYSIS_JSONL,
    append_jsonl,
    company_display_lines,
    control_number,
    count_jsonl,
    ensure_runtime_dirs,
    format_percent,
    format_usd,
    latest_jsonl_record,
    load_jsonl_records,
    make_id,
    normalize_text,
    round_money,
    slugify,
    utc_now,
    write_json,
)
from purchase_governance import (
    build_purchase_recommendation,
    record_purchase_recommendation,
    record_purchase_timing_analysis,
    record_supplier_quote,
)

ROOT = Path('/opt/data/hermes-mail')
LOGS_DIR = ROOT / 'logs'
TELEGRAM_NOTIFICATIONS_LOG = LOGS_DIR / 'telegram-notifications.jsonl'


def now() -> str:
    return utc_now()


def load_analysis_records() -> list[dict[str, Any]]:
    return load_jsonl_records(SUPPLIER_REPLY_ANALYSIS_JSONL)


def latest_analysis() -> dict[str, Any] | None:
    return latest_jsonl_record(SUPPLIER_REPLY_ANALYSIS_JSONL)


def build_pdf(quote: dict[str, Any]) -> Path:
    doc = PdfDocument(landscape=False)
    logo = ASSETS_DIR / 'logo.png'
    doc.set_logo(logo)
    page = doc.add_page()
    width, height = doc.width, doc.height
    margin = 40
    y = height - 48

    if doc.logo is not None:
        page.image('Im0', margin, height - 110, 96, 54)
        header_x = margin + 112
    else:
        header_x = margin
    for idx, line in enumerate(company_display_lines()):
        page.text(header_x, y - idx * 16, line, size=14 if idx == 0 else 10, font='F3' if idx == 0 else 'F1')
    page.text(header_x, y - 54, 'Cotação ao Cliente', size=16, font='F3')
    page.line(margin, height - 120, width - margin, height - 120, width=1.2)

    body_y = height - 150
    left = margin
    right = width / 2 + 10

    def add_kv(x: float, y_val: float, label: str, value: str, *, size: int = 10) -> float:
        page.text(x, y_val, f'{label}:', size=size, font='F3')
        page.text(x + 145, y_val, value, size=size, font='F1')
        return y_val - 18

    y_left = body_y
    y_left = add_kv(left, y_left, 'Número de controle', quote['control_number'])
    y_left = add_kv(left, y_left, 'Produto cotado', quote['product_name'])
    y_left = add_kv(left, y_left, 'Fornecedor de origem', quote['supplier_name'])
    y_left = add_kv(left, y_left, 'Incoterm', quote['incoterm'])
    y_left = add_kv(left, y_left, 'Preço de compra', format_usd(quote['purchase_price_usd']))
    y_left = add_kv(left, y_left, 'Moeda', 'USD')
    y_left = add_kv(left, y_left, 'Margem percentual', format_percent(quote['margem_percentual']))
    y_left = add_kv(left, y_left, 'Margem em valor', format_usd(quote['margem_valor']))
    y_left = add_kv(left, y_left, 'Valor total com margem', format_usd(quote['valor_total_com_margem']))
    y_left = add_kv(left, y_left, 'Situação', quote['status'])

    page.text(right, body_y, 'Descrição estruturada do produto', size=12, font='F3')
    desc_lines = quote.get('description_lines') or []
    if not desc_lines:
        desc_lines = [quote.get('product_description') or quote['product_name']]
    y_desc = body_y - 20
    for line in desc_lines:
        page.text(right, y_desc, f'• {line}', size=10, font='F1')
        y_desc -= 15
    y_desc -= 6
    page.text(right, y_desc, 'Observações comerciais', size=12, font='F3')
    y_desc -= 18
    for line in quote.get('commercial_notes_lines') or []:
        page.text(right, y_desc, f'• {line}', size=10, font='F1')
        y_desc -= 15
    y_desc -= 6
    page.text(right, y_desc, 'Fontes registradas', size=12, font='F3')
    y_desc -= 18
    for source in quote.get('sources', []):
        page.text(right, y_desc, f'• {source}', size=8, font='F2')
        y_desc -= 12

    footer_y = 36
    page.line(margin, footer_y + 12, width - margin, footer_y + 12, width=0.8)
    page.text(margin, footer_y, POLAR_SINERGY['footer'], size=9, font='F2')
    page.text(width - 180, footer_y, f'Gerado em {quote["created_at"]}', size=9, font='F2')

    pdf_name = f"{slugify(quote['control_number'])}.pdf"
    pdf_path = CLIENT_QUOTES_DIR / pdf_name
    doc.save(pdf_path)
    return pdf_path


def build_quote_record(analysis: dict[str, Any]) -> dict[str, Any]:
    price = float(analysis['extracted']['price_usd'] or 0.0)
    control = control_number('PSQ')
    margin = {
        'margem_percentual': analysis['margem_percentual'],
        'margem_valor': analysis['margem_valor'],
        'valor_total_com_margem': analysis['valor_total_com_margem'],
    }
    product_name = normalize_text(str(analysis.get('product_name') or 'Produto'))
    description = normalize_text(str(analysis.get('product_description') or analysis['extracted'].get('specifications') or product_name))
    description_lines = [line for line in [analysis['extracted'].get('description'), analysis['extracted'].get('specifications')] if line]
    commercial_notes_lines = [
        f"Incoterm mantido da proposta do fornecedor: {analysis['extracted'].get('incoterm')}",
        f"Prazo de entrega informado: {analysis['extracted'].get('lead_time_days')} dias",
        f"MOQ informado: {analysis['extracted'].get('moq')}",
        f"Condições de pagamento: {analysis['extracted'].get('payment_terms')}",
        f"Garantia: {analysis['extracted'].get('warranty')}",
        f"Fonte primária: e-mail {analysis.get('supplier_reply_email_id')}",
    ]
    quote = {
        'id': make_id('client_quote'),
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'status': 'dry_run',
        'manual_review': bool(analysis.get('manual_review')),
        'analysis_id': analysis.get('id'),
        'supplier_reply_email_id': analysis.get('supplier_reply_email_id'),
        'rfq_email_id': analysis.get('rfq_email_id'),
        'rfq_quote_id': analysis.get('rfq_quote_id'),
        'control_number': control,
        'customer_name': POLAR_SINERGY['name'],
        'customer_email': POLAR_SINERGY['contact_email'],
        'supplier_name': normalize_text(str(analysis.get('supplier_name') or analysis.get('supplier_id') or 'Fornecedor')),
        'product_name': product_name,
        'product_description': description,
        'description_lines': description_lines,
        'incoterm': analysis['extracted'].get('incoterm') or '',
        'purchase_price_usd': round_money(price),
        'currency': 'USD',
        'margem_percentual': margin['margem_percentual'],
        'margem_valor': margin['margem_valor'],
        'valor_total_com_margem': margin['valor_total_com_margem'],
        'commercial_notes_lines': commercial_notes_lines,
        'sources': [
            f"analysis:{analysis.get('id')}",
            f"supplier_reply_email:{analysis.get('supplier_reply_email_id')}",
            f"rfq_email:{analysis.get('rfq_email_id')}",
            f"rfq_quote:{analysis.get('rfq_quote_id')}",
        ],
    }
    return quote


def write_quote_record(quote: dict[str, Any]) -> Path:
    append_jsonl(CLIENT_QUOTES_JSONL, quote)
    return CLIENT_QUOTES_JSONL


def persist_purchase_governance_records(analysis: dict[str, Any], quote: dict[str, Any]) -> None:
    recommendation = build_purchase_recommendation(
        product_name=quote.get('product_name') or 'Produto',
        supplier_name=quote.get('supplier_name') or 'Fornecedor',
        supplier_country=str(analysis.get('supplier_country') or analysis.get('country') or ''),
        supplier_city=str(analysis.get('supplier_city') or analysis.get('city') or ''),
        unit_price_usd=quote.get('purchase_price_usd'),
        incoterm=quote.get('incoterm') or analysis.get('extracted', {}).get('incoterm') or '',
        lead_time_days=analysis.get('extracted', {}).get('lead_time_days'),
        freight_trend=str(analysis.get('freight_trend') or ''),
        stock_current_qty=analysis.get('stock_current_qty'),
        stock_min_qty=analysis.get('stock_min_qty'),
        stock_coverage_days=analysis.get('stock_coverage_days'),
        margin_status=str(analysis.get('margin_status') or ''),
        product_price_trend=str(analysis.get('product_price_trend') or ''),
        exchange_rate_risk_level=str(analysis.get('exchange_rate_risk_level') or ''),
        notes=[str(analysis.get('summary') or ''), str(analysis.get('risk_summary') or '')],
    )
    recommendation['analysis_id'] = analysis.get('id')
    recommendation['supplier_reply_email_id'] = analysis.get('supplier_reply_email_id')
    recommendation['rfq_quote_id'] = analysis.get('rfq_quote_id')
    recommendation['status'] = 'awaiting_user_decision' if quote.get('manual_review') or recommendation.get('user_decision_required') else 'ready_for_user_review'
    record_purchase_recommendation(recommendation)
    record_purchase_timing_analysis({
        'id': f"purchase_timing_{quote['control_number']}",
        'rfq_batch_id': quote.get('rfq_quote_id'),
        'supplier_id': analysis.get('supplier_id') or analysis.get('supplier_name'),
        'product_id': analysis.get('product_id'),
        'quote_id': quote.get('rfq_quote_id') or quote.get('analysis_id'),
        'stock_current_qty': analysis.get('stock_current_qty'),
        'stock_reserved_qty': analysis.get('stock_reserved_qty'),
        'stock_available_qty': analysis.get('stock_available_qty'),
        'stock_in_transit_qty': analysis.get('stock_in_transit_qty'),
        'stock_min_qty': analysis.get('stock_min_qty'),
        'monthly_average_sales_qty': analysis.get('monthly_average_sales_qty'),
        'stock_coverage_days': analysis.get('stock_coverage_days'),
        'production_lead_time_days': analysis.get('extracted', {}).get('lead_time_days'),
        'transit_time_days': analysis.get('transit_time_days'),
        'estimated_customs_clearance_days': analysis.get('estimated_customs_clearance_days'),
        'total_replenishment_days': analysis.get('total_replenishment_days'),
        'rupture_risk_level': analysis.get('rupture_risk_level') or ('high' if quote.get('manual_review') else 'medium'),
        'freight_trend': analysis.get('freight_trend'),
        'freight_risk_level': analysis.get('freight_risk_level'),
        'product_price_trend': analysis.get('product_price_trend'),
        'exchange_rate_risk_level': analysis.get('exchange_rate_risk_level'),
        'margin_status': analysis.get('margin_status') or quote.get('margem_percentual'),
        'recommendation': recommendation.get('suggested_action'),
        'reasoning_summary': recommendation.get('reasoning_summary'),
        'user_decision_required': recommendation.get('user_decision_required'),
        'user_decision': recommendation.get('user_decision'),
        'created_at': quote.get('created_at'),
        'updated_at': quote.get('updated_at'),
    })
    record_supplier_quote({
        'id': analysis.get('rfq_quote_id') or quote.get('control_number'),
        'rfq_batch_id': quote.get('rfq_quote_id'),
        'supplier_company_id': analysis.get('supplier_id') or analysis.get('supplier_name'),
        'product_id': analysis.get('product_id'),
        'quote_currency': quote.get('currency') or 'USD',
        'unit_price_usd': quote.get('purchase_price_usd'),
        'moq': analysis.get('extracted', {}).get('moq'),
        'production_lead_time_days': analysis.get('extracted', {}).get('lead_time_days'),
        'transit_time_days': analysis.get('transit_time_days'),
        'incoterm': quote.get('incoterm'),
        'payment_terms': analysis.get('extracted', {}).get('payment_terms'),
        'freight_estimate': analysis.get('freight_estimate'),
        'total_land_cost': quote.get('valor_total_com_margem'),
        'validity_date': analysis.get('validity_date'),
        'quote_received_at': analysis.get('received_at') or analysis.get('created_at'),
        'status': 'manual_review' if quote.get('manual_review') else 'generated_dry_run',
        'quote_payload_json': {**analysis, **quote},
        'notes': analysis.get('summary') or analysis.get('risk_summary'),
    })


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    for path in [ROOT, LOGS_DIR, CLIENT_QUOTES_DIR, ASSETS_DIR, ROOT / 'reports', ROOT / 'emails', ROOT / 'emails' / 'incoming']:
        if not path.exists():
            errors.append(f'missing path: {path}')
    if not SUPPLIER_REPLY_ANALYSIS_JSONL.exists():
        errors.append(f'missing file: {SUPPLIER_REPLY_ANALYSIS_JSONL}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'analysis_jsonl={SUPPLIER_REPLY_ANALYSIS_JSONL}')
    print(f'client_quotes_jsonl={CLIENT_QUOTES_JSONL}')
    return 0


def cmd_generate_dry_run(_: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    analysis = latest_analysis()
    if not analysis:
        raise SystemExit('no supplier reply analysis found; run analyze-latest first')
    if analysis.get('manual_review'):
        quote = build_quote_record(analysis)
        quote['status'] = 'manual_review'
        quote['review_reason'] = 'missing required fields: ' + ', '.join(analysis.get('missing_fields') or [])
        quote['quote_pdf_path'] = None
        write_quote_record(quote)
        persist_purchase_governance_records(analysis, quote)
        append_jsonl(TELEGRAM_NOTIFICATIONS_LOG, {
            'id': make_id('tg_client_quote_manual_review'),
            'created_at': now(),
            'event_type': 'client_quote_manual_review',
            'dry_run': True,
            'summary': f"Cotação {quote['control_number']} enviada para revisão manual. Motivo: {quote['review_reason']}",
            'pdf_path': None,
            'control_number': quote['control_number'],
            'source': 'client_quote_generator',
        })
        print('CLIENT QUOTE SENT TO MANUAL REVIEW')
        print(f'control_number={quote["control_number"]}')
        print(f'review_reason={quote["review_reason"]}')
        return 0

    quote = build_quote_record(analysis)
    pdf_path = build_pdf(quote)
    quote['quote_pdf_path'] = str(pdf_path)
    quote['status'] = 'generated_dry_run'
    write_quote_record(quote)
    persist_purchase_governance_records(analysis, quote)
    append_jsonl(TELEGRAM_NOTIFICATIONS_LOG, {
        'id': make_id('tg_client_quote_generated'),
        'created_at': now(),
        'event_type': 'client_quote_generated',
        'dry_run': True,
        'summary': (
            f"Cotação {quote['control_number']} gerada. "
            f"PDF: {pdf_path}. Margem aplicada: {analysis['margem_regra']}"
        ),
        'pdf_path': str(pdf_path),
        'control_number': quote['control_number'],
        'source': 'client_quote_generator',
    })
    print('CLIENT QUOTE GENERATED')
    print(f'control_number={quote["control_number"]}')
    print(f'pdf_path={pdf_path}')
    print(f'margem_regra={analysis["margem_regra"]}')
    return 0


def cmd_preview_latest(_: argparse.Namespace) -> int:
    quote = latest_jsonl_record(CLIENT_QUOTES_JSONL)
    if not quote:
        raise SystemExit('no client quote found yet')
    print('LATEST CLIENT QUOTE')
    print(f'control_number={quote.get("control_number")}')
    print(f'product={quote.get("product_name")}')
    print(f'supplier={quote.get("supplier_name")}')
    print(f'incoterm={quote.get("incoterm")}')
    print(f'purchase_price={format_usd(quote.get("purchase_price_usd"))}')
    print(f'margem_valor={format_usd(quote.get("margem_valor"))}')
    print(f'total={format_usd(quote.get("valor_total_com_margem"))}')
    print(f'status={quote.get("status")}')
    print(f'pdf_path={quote.get("quote_pdf_path") or "-"}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    quotes = load_jsonl_records(CLIENT_QUOTES_JSONL)
    manual = sum(1 for rec in quotes if rec.get('manual_review'))
    generated = sum(1 for rec in quotes if rec.get('status') == 'generated_dry_run')
    latest = quotes[-1] if quotes else {}
    print('CLIENT QUOTE GENERATOR STATS')
    print(f'quotes_total: {len(quotes)}')
    print(f'quotes_generated: {generated}')
    print(f'manual_review: {manual}')
    print(f'latest_control_number: {latest.get("control_number") or "-"}')
    print(f'client_quotes_jsonl: {CLIENT_QUOTES_JSONL}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Generate client quotation PDFs')
    sub = parser.add_subparsers(dest='command', required=True)
    p_validate = sub.add_parser('validate', help='Validate directories and required input data')
    p_validate.set_defaults(func=cmd_validate)
    p_generate = sub.add_parser('generate-dry-run', help='Generate a dry-run client quote PDF')
    p_generate.set_defaults(func=cmd_generate_dry_run)
    p_preview = sub.add_parser('preview-latest', help='Preview the most recent generated quote')
    p_preview.set_defaults(func=cmd_preview_latest)
    p_stats = sub.add_parser('stats', help='Show quote generation stats')
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
