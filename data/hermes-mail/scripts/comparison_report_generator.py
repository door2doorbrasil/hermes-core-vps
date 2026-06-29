#!/usr/bin/env python3
"""Generate landscape comparison reports from supplier-reply analyses."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from pdf_writer import PdfDocument
from reporting_utils import (
    ASSETS_DIR,
    COMPARISON_REPORTS_DIR,
    COMPARISON_REPORTS_JSONL,
    CLIENT_QUOTES_JSONL,
    EMAILS_JSONL,
    POLAR_SINERGY,
    SUPPLIER_REPLY_ANALYSIS_JSONL,
    append_jsonl,
    compare_similarity,
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
    slugify,
    utc_now,
)

ROOT = Path('/opt/data/hermes-mail')
TELEGRAM_NOTIFICATIONS_LOG = ROOT / 'logs' / 'telegram-notifications.jsonl'


def now() -> str:
    return utc_now()


def analyses() -> list[dict[str, Any]]:
    return load_jsonl_records(SUPPLIER_REPLY_ANALYSIS_JSONL)


def email_records() -> list[dict[str, Any]]:
    return load_jsonl_records(EMAILS_JSONL)


def group_exact(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = '|'.join([
            normalize_text(str(record.get('product_name') or '')),
            normalize_text(str(record.get('product_description') or '')),
            normalize_text(str((record.get('extracted') or {}).get('incoterm') or '')),
            normalize_text(str((record.get('extracted') or {}).get('specifications') or '')),
        ])
        grouped[key].append(record)
    return grouped


def compute_email_summary() -> dict[str, Any]:
    emails = email_records()
    rfqs = [rec for rec in emails if rec.get('direction') == 'incoming' and rec.get('status') == 'simulated' and rec.get('source') == 'simulator']
    replies = [rec for rec in emails if rec.get('direction') == 'incoming' and (rec.get('reply_to_email_id') or rec.get('source') == 'supplier_reply_simulator' or rec.get('status') == 'supplier_reply_received')]
    responded_rfqs = {rec.get('reply_to_email_id') for rec in replies if rec.get('reply_to_email_id')}
    without = [rec for rec in rfqs if rec.get('id') not in responded_rfqs]
    return {
        'rfqs_sent': len(rfqs),
        'responses_received': len(replies),
        'suppliers_without_response': len(without),
        'response_rate_percent': 0.0 if not rfqs else round((len(responded_rfqs) / len(rfqs)) * 100.0, 2),
    }


def build_pdf(report: dict[str, Any], rows: list[dict[str, Any]], exact_groups: dict[str, list[dict[str, Any]]], similarity_pairs: list[tuple[str, str, float]]) -> Path:
    doc = PdfDocument(landscape=True)
    doc.set_logo(ASSETS_DIR / 'logo.png')
    page = doc.add_page()
    width, height = doc.width, doc.height
    margin = 30
    y = height - 40

    if doc.logo is not None:
        page.image('Im0', margin, height - 90, 84, 48)
        header_x = margin + 96
    else:
        header_x = margin
    for idx, line in enumerate(company_display_lines()):
        page.text(header_x, y - idx * 14, line, size=13 if idx == 0 else 9, font='F3' if idx == 0 else 'F1')
    page.text(header_x, y - 44, 'Relatório Comparativo de Fornecedores', size=15, font='F3')
    page.line(margin, height - 102, width - margin, height - 102, width=1.1)

    summary = report['email_summary']
    y_summary = height - 128
    summary_lines = [
        f"RFQs enviados: {summary['rfqs_sent']}",
        f"Respostas recebidas: {summary['responses_received']}",
        f"Fornecedores sem resposta: {summary['suppliers_without_response']}",
        f"Taxa de resposta: {summary['response_rate_percent']:.2f}%",
        f"Gerado em: {report['created_at']}",
    ]
    for idx, item in enumerate(summary_lines):
        page.text(margin, y_summary - idx * 13, item, size=9, font='F2')

    table_top = height - 208
    headers = ['Produto', 'Fornecedor', 'País', 'Preço compra', 'Incoterm', 'Margem %', 'Margem USD', 'Total USD', 'MOQ', 'Lead time', 'Pagamento', 'Garantia']
    col_x = [30, 116, 236, 320, 382, 444, 490, 548, 612, 656, 708, 785]
    page.line(margin, table_top + 6, width - margin, table_top + 6, width=0.8)
    for idx, head in enumerate(headers):
        page.text(col_x[idx], table_top, head, size=8, font='F3')

    y_row = table_top - 16
    row_height = 14
    for row in rows:
        if y_row < 120:
            break
        page.text(col_x[0], y_row, row['product_name'][:22], size=8, font='F2')
        page.text(col_x[1], y_row, row['supplier'][:16], size=8, font='F2')
        page.text(col_x[2], y_row, row['country'][:12], size=8, font='F2')
        page.text(col_x[3], y_row, row['purchase_price'], size=8, font='F2')
        page.text(col_x[4], y_row, row['incoterm'][:16], size=8, font='F2')
        page.text(col_x[5], y_row, row['margin_percent'], size=8, font='F2')
        page.text(col_x[6], y_row, row['margin_value'], size=8, font='F2')
        page.text(col_x[7], y_row, row['total_value'], size=8, font='F2')
        page.text(col_x[8], y_row, str(row['moq']), size=8, font='F2')
        page.text(col_x[9], y_row, f"{row['lead_time_days']}d", size=8, font='F2')
        page.text(col_x[10], y_row, row['payment_terms'][:18], size=8, font='F2')
        page.text(col_x[11], y_row, row['warranty'][:14], size=8, font='F2')
        y_row -= row_height

    section_y = 112
    page.line(margin, section_y + 10, width - margin, section_y + 10, width=0.8)
    page.text(margin, section_y, 'Agrupamentos exatos', size=11, font='F3')
    section_y -= 14
    for key, group in list(exact_groups.items())[:4]:
        sample = group[0]
        page.text(margin, section_y, f"• {sample['product_name']} — {len(group)} ocorrência(s) exata(s)", size=8, font='F1')
        section_y -= 11

    page.text(360, 112, 'Produtos parecidos', size=11, font='F3')
    sim_y = 98
    if similarity_pairs:
        for left, right, score in similarity_pairs[:4]:
            page.text(360, sim_y, f'• {left[:18]} ↔ {right[:18]} ({score:.2f})', size=8, font='F1')
            sim_y -= 11
    else:
        page.text(360, sim_y, '• Nenhum par similar não idêntico identificado.', size=8, font='F1')

    page.text(margin, 20, POLAR_SINERGY['footer'], size=8, font='F2')
    page.text(width - 220, 20, f'Arquivo: {report["control_number"]}', size=8, font='F2')

    pdf_path = COMPARISON_REPORTS_DIR / f"{slugify(report['control_number'])}.pdf"
    doc.save(pdf_path)
    return pdf_path


def row_from_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    extracted = analysis.get('extracted') or {}
    return {
        'analysis_id': analysis.get('id'),
        'product_name': normalize_text(str(analysis.get('product_name') or extracted.get('description') or 'Produto')),
        'supplier': normalize_text(str(analysis.get('supplier_name') or analysis.get('supplier_id') or 'Fornecedor')),
        'country': normalize_text(str(extracted.get('country') or '')),
        'purchase_price': format_usd(extracted.get('price_usd')),
        'incoterm': normalize_text(str(extracted.get('incoterm') or '')),
        'margin_percent': format_percent(analysis.get('margem_percentual')),
        'margin_value': format_usd(analysis.get('margem_valor')),
        'total_value': format_usd(analysis.get('valor_total_com_margem')),
        'moq': extracted.get('moq') or '-',
        'lead_time_days': extracted.get('lead_time_days') or '-',
        'payment_terms': normalize_text(str(extracted.get('payment_terms') or '')),
        'warranty': normalize_text(str(extracted.get('warranty') or '')),
        'specifications': normalize_text(str(extracted.get('specifications') or '')),
    }


def build_report() -> dict[str, Any]:
    analysis_records = analyses()
    rows = [row_from_analysis(rec) for rec in analysis_records]
    exact_groups = group_exact(analysis_records)
    similarity_pairs: list[tuple[str, str, float]] = []
    for i, left in enumerate(analysis_records):
        for right in analysis_records[i + 1:]:
            score = compare_similarity(left, right)
            if 0.45 <= score < 1.0:
                similarity_pairs.append((str(left.get('product_name') or 'Produto'), str(right.get('product_name') or 'Produto'), score))
    email_summary = compute_email_summary()
    report = {
        'id': make_id('comparison_report'),
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'status': 'dry_run',
        'control_number': control_number('PSR'),
        'analysis_count': len(analysis_records),
        'rows_count': len(rows),
        'exact_group_count': len(exact_groups),
        'similar_pair_count': len(similarity_pairs),
        'email_summary': email_summary,
        'analysis_ids': [rec.get('id') for rec in analysis_records],
        'sources': [f"analysis:{rec.get('id')}" for rec in analysis_records],
        'rows': rows,
        'exact_groups': [
            {
                'signature': key,
                'count': len(group),
                'product_name': group[0].get('product_name'),
            }
            for key, group in exact_groups.items()
        ],
        'similar_pairs': [
            {'left': left, 'right': right, 'score': score}
            for left, right, score in similarity_pairs
        ],
    }
    return report


def write_report_record(report: dict[str, Any], pdf_path: Path) -> Path:
    record = dict(report)
    record['pdf_path'] = str(pdf_path)
    append_jsonl(COMPARISON_REPORTS_JSONL, record)
    return COMPARISON_REPORTS_JSONL


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    for path in [ROOT, COMPARISON_REPORTS_DIR, ASSETS_DIR, ROOT / 'reports', ROOT / 'emails']:
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
    print(f'comparison_reports_jsonl={COMPARISON_REPORTS_JSONL}')
    return 0


def cmd_generate_landscape_dry_run(_: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    report = build_report()
    pdf_path = build_pdf(report, report['rows'], group_exact(analyses()), [(pair['left'], pair['right'], pair['score']) for pair in report['similar_pairs']])
    report['pdf_path'] = str(pdf_path)
    write_report_record(report, pdf_path)
    append_jsonl(TELEGRAM_NOTIFICATIONS_LOG, {
        'id': make_id('tg_comparison_report_generated'),
        'created_at': now(),
        'event_type': 'comparison_report_generated',
        'dry_run': True,
        'summary': (
            f"Relatório comparativo {report['control_number']} gerado. "
            f"PDF: {pdf_path}. RFQs: {report['email_summary']['rfqs_sent']}, "
            f"Respostas: {report['email_summary']['responses_received']}"
        ),
        'pdf_path': str(pdf_path),
        'control_number': report['control_number'],
        'source': 'comparison_report_generator',
    })
    print('COMPARISON REPORT GENERATED')
    print(f'control_number={report["control_number"]}')
    print(f'pdf_path={pdf_path}')
    print(f'rfq_sent={report["email_summary"]["rfqs_sent"]}')
    print(f'responses_received={report["email_summary"]["responses_received"]}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    records = load_jsonl_records(COMPARISON_REPORTS_JSONL)
    latest = records[-1] if records else {}
    print('COMPARISON REPORT STATS')
    print(f'reports_total: {len(records)}')
    print(f'latest_control_number: {latest.get("control_number") or "-"}')
    print(f'exact_group_count: {latest.get("exact_group_count") or 0}')
    print(f'similar_pair_count: {latest.get("similar_pair_count") or 0}')
    print(f'comparison_reports_jsonl: {COMPARISON_REPORTS_JSONL}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Generate comparison reports from supplier analyses')
    sub = parser.add_subparsers(dest='command', required=True)
    p_validate = sub.add_parser('validate', help='Validate directories and required inputs')
    p_validate.set_defaults(func=cmd_validate)
    p_gen = sub.add_parser('generate-landscape-dry-run', help='Generate a landscape comparison PDF in dry-run mode')
    p_gen.set_defaults(func=cmd_generate_landscape_dry_run)
    p_stats = sub.add_parser('stats', help='Show comparison report stats')
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
