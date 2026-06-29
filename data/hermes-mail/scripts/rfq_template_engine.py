#!/usr/bin/env python3
"""RFQ template engine for Polar Sinergy.

Creates English RFQ drafts only. No real SMTP send happens here; drafts are
stored locally and routed into the approval queue for future manual approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from email_real_common import APPROVAL_QUEUE_JSONL, EMAILS_JSONL, OUTGOING_DIR, ROOT, append_record, ensure_storage, now, notify_telegram
from reporting_utils import (
    BRAND_PROFILE_PATH,
    POLAR_SINERGY,
    CLIENT_QUOTES_JSONL,
    COTACOES_JSONL,
    FORNECEDORES_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    MANUFACTURER_RESEARCH_JSONL,
    MANUFACTURER_DISCOVERY_JSONL,
    PRODUTOS_JSONL,
    RFQ_DRAFTS_JSONL,
    SOURCING_PROJECTS_JSONL,
    brand_signature_lines,
    company_display_lines,
    control_number,
    count_jsonl,
    load_brand_profile,
    load_jsonl_records,
    make_id,
    normalize_text,
    slugify,
)
from purchase_governance import (
    record_purchase_company,
    record_purchase_contact,
    record_purchase_product,
    record_rfq_batch,
    record_rfq_batch_supplier,
)

TEMPLATE_NAMES = [
    'generic',
    'industrial_machinery',
    'chemicals',
    'food_ingredients',
    'medical_equipment',
    'electronics',
    'automotive_parts',
    'packaging',
    'cosmetics',
    'raw_materials',
    'oem_private_label',
]

TEMPLATES: dict[str, dict[str, Any]] = {
    'generic': {
        'focus': 'general sourcing',
        'questions': [
            'Please confirm your unit price and currency.',
            'Please confirm MOQ, lead time, incoterm, and payment terms.',
            'Please share product specifications, packaging details, warranty, and commercial validity.',
            'Please attach any datasheet, certification, and product photos when available.',
        ],
    },
    'industrial_machinery': {
        'focus': 'industrial equipment',
        'questions': [
            'Please confirm machine capacity, cycle time, and output per hour.',
            'Please confirm voltage, frequency, phase, power consumption, and control system.',
            'Please share materials, dimensions, weight, installation requirements, and spare parts list.',
            'Please confirm warranty, after-sales support, manuals, videos, and testing conditions.',
        ],
    },
    'chemicals': {
        'focus': 'chemical products',
        'questions': [
            'Please confirm CAS number, purity, grade, and formulation details.',
            'Please share SDS/MSDS, COA, shelf life, packaging, and transport restrictions.',
            'Please confirm regulatory compliance, origin, and storage conditions.',
            'Please share MOQ, lead time, payment terms, and commercial validity.',
        ],
    },
    'food_ingredients': {
        'focus': 'food ingredients',
        'questions': [
            'Please confirm ingredients, technical specification, and shelf life.',
            'Please share HACCP, Halal, Kosher, FDA, or other applicable documents.',
            'Please confirm packaging format, storage requirements, and country of origin.',
            'Please share MOQ, lead time, payment terms, and sample availability.',
        ],
    },
    'medical_equipment': {
        'focus': 'medical equipment',
        'questions': [
            'Please confirm device model, intended use, and technical specification.',
            'Please share ISO 13485, CE Medical, FDA, or local registration documents when applicable.',
            'Please confirm sterilization, packaging, warranty, and spare parts support.',
            'Please share MOQ, lead time, installation support, and after-sales terms.',
        ],
    },
    'electronics': {
        'focus': 'electronic components',
        'questions': [
            'Please confirm part number, datasheet, tolerance, and compliance status.',
            'Please share RoHS, REACH, UL, or other applicable certificates.',
            'Please confirm MOQ, lead time, packing format, and reel/tray details.',
            'Please share warranty, traceability, and authenticity assurance.',
        ],
    },
    'automotive_parts': {
        'focus': 'automotive parts',
        'questions': [
            'Please confirm OEM/aftermarket position, part numbers, and vehicle compatibility.',
            'Please share technical drawings, tests, certifications, and material grade.',
            'Please confirm MOQ, lead time, packaging standard, and warranty.',
            'Please share PPAP, IATF, or other qualification documents when available.',
        ],
    },
    'packaging': {
        'focus': 'packaging materials',
        'questions': [
            'Please confirm dimensions, material, thickness, printing, and finishing.',
            'Please share packaging format, carton details, and shipping configuration.',
            'Please confirm MOQ, lead time, and artwork approval process.',
            'Please share food-grade or compliance documents when applicable.',
        ],
    },
    'cosmetics': {
        'focus': 'cosmetics / private label',
        'questions': [
            'Please confirm formula, ingredients, shelf life, and stability data.',
            'Please share GMP, safety sheet, microbiological test, and regulatory documents.',
            'Please confirm packaging, label requirements, MOQ, and lead time.',
            'Please share OEM/private label capabilities and sample availability.',
        ],
    },
    'raw_materials': {
        'focus': 'raw materials',
        'questions': [
            'Please confirm grade, purity, specs, and country of origin.',
            'Please share COA, technical sheet, and any applicable compliance documents.',
            'Please confirm MOQ, lead time, packing, and transport conditions.',
            'Please share current availability and validity of the quotation.',
        ],
    },
    'oem_private_label': {
        'focus': 'OEM/private label',
        'questions': [
            'Please confirm OEM/private label capability, customization scope, and tooling support.',
            'Please share samples, branding options, compliance documents, and packaging choices.',
            'Please confirm MOQ, lead time, payment terms, and exclusivity options if applicable.',
            'Please share warranty, after-sales support, and product photos or videos.',
        ],
    },
}

CATEGORY_ALIAS = {
    'machines-industriais': 'industrial_machinery',
    'produtos-quimicos': 'chemicals',
    'alimentos-ingredientes': 'food_ingredients',
    'equipamentos-medicos': 'medical_equipment',
    'componentes-eletronicos': 'electronics',
    'autopecas': 'automotive_parts',
    'embalagens': 'packaging',
    'cosmeticos': 'cosmetics',
    'materias-primas-metálicas': 'raw_materials',
    'materias-primas-metalicas': 'raw_materials',
    'plásticos-resinas': 'raw_materials',
    'plasticos-resinas': 'raw_materials',
    'oem-private-label': 'oem_private_label',
    'oem_private_label': 'oem_private_label',
}

MANUAL_REVIEW_QUEUE_JSONL = ROOT / 'manual-review-queue.jsonl'


def template_questions(template_name: str) -> list[str]:
    return list(TEMPLATES.get(template_name, TEMPLATES['generic'])['questions'])


def select_template(category_id: str | None, explicit_template: str | None = None) -> str:
    if explicit_template and explicit_template in TEMPLATES:
        return explicit_template
    if category_id:
        normalized = CATEGORY_ALIAS.get(category_id) or CATEGORY_ALIAS.get(category_id.replace(' ', '_').casefold())
        if normalized in TEMPLATES:
            return normalized
    return 'generic'


def latest_record(path: Path) -> dict[str, Any] | None:
    records = load_jsonl_records(path)
    return records[-1] if records else None


def latest_source_context() -> dict[str, Any]:
    project = latest_record(SOURCING_PROJECTS_JSONL) or {}
    supplier = latest_record(FORNECEDORES_JSONL) or {}
    contact = latest_record(ROOT / 'contatos.jsonl') or {}
    product = latest_record(PRODUTOS_JSONL) or {}
    quote = latest_record(COTACOES_JSONL) or {}
    client_quote = latest_record(CLIENT_QUOTES_JSONL) or {}
    qualification = latest_record(MANUFACTURER_QUALIFICATION_JSONL) or {}
    discovery = latest_record(MANUFACTURER_DISCOVERY_JSONL) or {}
    research = latest_record(MANUFACTURER_RESEARCH_JSONL) or {}
    return {
        'project': project,
        'supplier': supplier,
        'contact': contact,
        'product': product,
        'quote': quote,
        'client_quote': client_quote,
        'qualification': qualification,
        'discovery': discovery,
        'research': research,
    }


def infer_product_name(context: dict[str, Any], template_name: str) -> str:
    for key in ['product', 'quote', 'client_quote']:
        record = context.get(key) or {}
        for field in ['name', 'product_name', 'title']:
            value = normalize_text(str(record.get(field) or ''))
            if value:
                return value
        if key == 'quote':
            items = record.get('items') or []
            if items:
                value = normalize_text(str((items[0] or {}).get('description') or ''))
                if value:
                    return value
    if context.get('project'):
        value = normalize_text(str(context['project'].get('product') or context['project'].get('product_description') or ''))
        if value:
            return value
    return {
        'industrial_machinery': 'Industrial machinery',
        'chemicals': 'Chemical product',
        'food_ingredients': 'Food ingredient',
        'medical_equipment': 'Medical equipment',
        'electronics': 'Electronic component',
        'automotive_parts': 'Automotive part',
        'packaging': 'Packaging material',
        'cosmetics': 'Cosmetic product',
        'raw_materials': 'Raw material',
        'oem_private_label': 'OEM/private label product',
        'generic': 'Product',
    }.get(template_name, 'Product')


def infer_supplier_name(context: dict[str, Any]) -> str:
    for key in ['supplier', 'qualification', 'research']:
        record = context.get(key) or {}
        value = normalize_text(str(record.get('company_name') or record.get('name') or ''))
        if value:
            return value
    return 'Supplier'


def build_draft(template_name: str, *, product_name: str | None = None, product_description: str | None = None, category_id: str | None = None) -> dict[str, Any]:
    context = latest_source_context()
    brand = load_brand_profile()
    template_name = select_template(category_id, template_name)
    product_name = normalize_text(product_name or infer_product_name(context, template_name))
    if not product_name:
        product_name = 'Product'
    product_description = normalize_text(product_description or '') or normalize_text(
        str((context.get('project') or {}).get('product_description') or (context.get('quote') or {}).get('notes') or product_name)
    )
    supplier_name = infer_supplier_name(context)
    supplier_email = ''
    for key in ['supplier', 'contact']:
        record = context.get(key) or {}
        for field in ['email', 'contact_email']:
            value = normalize_text(str(record.get(field) or ''))
            if value:
                supplier_email = value
                break
        if supplier_email:
            break
    if not supplier_email:
        supplier_email = 'supplier@example.com'

    rfq_id = make_id('rfq').split('_', 1)[-1]
    draft_id = f"rfq_draft_{hashlib.sha1(f'{template_name}|{product_name}|{now()}'.encode('utf-8')).hexdigest()[:12]}"
    control = control_number('RFQ')
    subject = f"RFQ-{rfq_id} - Quotation Request - {product_name}"
    questions = template_questions(template_name)
    signature = brand.get('signature_plain') or '\n'.join(brand_signature_lines())
    body_lines = [
        f"Hello {supplier_name},",
        "",
        f"Polar Sinergy LLC is requesting a quotation for {product_name}.",
        f"Product description: {product_description}",
        f"Template: {template_name}",
        "",
        "Please reply in English and include:",
    ]
    for idx, question in enumerate(questions, start=1):
        body_lines.append(f"{idx}. {question}")
    body_lines.extend([
        "",
        "Please make sure your reply includes unit price, MOQ, lead time, incoterm, payment terms, warranty, and any technical notes.",
        "",
        signature,
    ])
    body_text = '\n'.join(body_lines)
    raw_path = OUTGOING_DIR / f'{draft_id}.eml'
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        f"From: {brand.get('email', POLAR_SINERGY['contact_email'])}\n"
        f"To: {supplier_email}\n"
        f"Subject: {subject}\n"
        f"Message-ID: <{draft_id}@hermes-mail.local>\n"
        f"X-RFQ-ID: {rfq_id}\n"
        f"X-Template-Name: {template_name}\n"
        f"X-Control-Number: {control}\n"
        f"MIME-Version: 1.0\n"
        f"Content-Type: text/plain; charset=\"utf-8\"\n\n{body_text}\n",
        encoding='utf-8',
    )
    draft = {
        'id': draft_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'source': 'rfq_template_engine',
        'status': 'draft_pending_approval',
        'approval_required': True,
        'draft_type': 'rfq_template',
        'template_name': template_name,
        'template_focus': TEMPLATES.get(template_name, TEMPLATES['generic'])['focus'],
        'rfq_id': rfq_id,
        'control_number': control,
        'sourcing_project_id': (context.get('project') or {}).get('project_id'),
        'supplier_id': (context.get('supplier') or {}).get('id') or (context.get('qualification') or {}).get('supplier_id'),
        'contact_id': (context.get('contact') or {}).get('id'),
        'product_id': (context.get('product') or {}).get('id'),
        'quote_id': (context.get('quote') or {}).get('id'),
        'customer_name': brand.get('company_name', POLAR_SINERGY['name']),
        'customer_email': brand.get('email', POLAR_SINERGY['contact_email']),
        'from': brand.get('email', POLAR_SINERGY['contact_email']),
        'to': [supplier_email],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'body_text': body_text,
        'raw_path': str(raw_path),
        'message_id': f'<{draft_id}@hermes-mail.local>',
        'template_questions': questions,
        'brand_signature': signature,
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
        'source': 'rfq_template_engine',
        'approval_required': True,
        'from': draft['from'],
        'to': draft['to'],
        'cc': [],
        'bcc': [],
        'subject': subject,
        'body_text': body_text,
        'raw_path': str(raw_path),
        'message_id': draft['message_id'],
        'thread_id': f'<{rfq_id}@hermes-mail.local>',
        'rfq_id': rfq_id,
        'control_number': control,
        'sourcing_project_id': draft['sourcing_project_id'],
        'supplier_id': draft['supplier_id'],
        'contact_id': draft['contact_id'],
        'product_id': draft['product_id'],
        'quote_id': draft['quote_id'],
        'template_name': template_name,
    })
    queue_item = {
        'id': f"approval_evt_{hashlib.sha1(f'{draft_id}|approval'.encode('utf-8')).hexdigest()[:12]}",
        'queue_key': draft_id,
        'item_type': 'rfq_draft',
        'draft_id': draft_id,
        'rfq_id': rfq_id,
        'thread_id': f'<{rfq_id}@hermes-mail.local>',
        'control_number': control,
        'sourcing_project_id': draft['sourcing_project_id'],
        'supplier_id': draft['supplier_id'],
        'contact_id': draft['contact_id'],
        'product_id': draft['product_id'],
        'status': 'pending',
        'created_at': now(),
        'updated_at': now(),
        'source': 'rfq_template_engine',
        'template_name': template_name,
    }
    append_record(APPROVAL_QUEUE_JSONL, queue_item)
    record_purchase_company({
        'id': draft.get('supplier_id') or supplier_email,
        'legal_name': supplier_name or draft.get('supplier_id') or supplier_email,
        'trade_name': supplier_name or draft.get('supplier_id') or supplier_email,
        'country': 'Não verificado',
        'city': 'Não verificado',
        'website': '',
        'email': supplier_email,
        'phone': '',
        'status': 'contato_pendente',
        'notes': f'RFQ draft {draft_id}',
        'created_at': now(),
        'updated_at': now(),
    })
    record_purchase_product({
        'id': draft.get('product_id') or make_id('purchase_product'),
        'company_id': draft.get('sourcing_project_id') or '',
        'name': product_name,
        'technical_spec': product_description,
        'sku': '',
        'category': template_name,
        'unit': '',
        'target_price': None,
        'currency': 'USD',
        'incoterm': '',
        'moq': None,
        'created_at': now(),
        'updated_at': now(),
        'notes': f'RFQ product draft {draft_id}',
    })
    record_purchase_contact({
        'id': draft.get('contact_id') or supplier_email,
        'company_id': draft.get('supplier_id') or supplier_email,
        'name': supplier_name or supplier_email,
        'title': 'Sales contact',
        'email': supplier_email,
        'phone': '',
        'channel': 'email',
        'status': 'contato_pendente',
        'notes': f'RFQ draft {draft_id}',
        'created_at': now(),
        'updated_at': now(),
    })
    record_rfq_batch({
        'id': rfq_id,
        'product_id': draft.get('product_id') or '',
        'company_id': draft.get('supplier_id') or '',
        'authorized_at': now(),
        'authorized_by': 'manual_review',
        'authorization_scope': 'single_supplier_draft',
        'status': 'pending_user_authorization',
        'user_decision_required': True,
        'user_decision': 'manual_review_pending',
        'notes': f'RFQ draft generated for {product_name}',
        'decision_gate': {'actions': ['authorize_all_suppliers', 'authorize_selected_suppliers', 'review_rfq_before_send', 'cancel_process']},
        'created_at': now(),
        'updated_at': now(),
    })
    record_rfq_batch_supplier({
        'id': f'{rfq_id}:{slugify(supplier_name) or "supplier"}',
        'rfq_batch_id': rfq_id,
        'supplier_company_id': draft.get('supplier_id') or supplier_email,
        'contact_id': draft.get('contact_id') or supplier_email,
        'country': 'Não verificado',
        'city': 'Não verificado',
        'website': '',
        'contact_value': supplier_email,
        'contact_status': 'contato_pendente',
        'observation': f'RFQ draft {draft_id}',
        'email_status': 'pendente_envio',
        'notes': f'RFQ draft {draft_id}',
        'created_at': now(),
        'updated_at': now(),
    })
    notify_telegram(
        'rfq_draft_generated',
        f"RFQ draft generated for approval: {subject}",
        metadata={'draft_id': draft_id, 'rfq_id': rfq_id, 'template_name': template_name, 'control_number': control},
    )
    return {
        'draft': draft,
        'queue_item': queue_item,
    }


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_storage()
    errors: list[str] = []
    if not BRAND_PROFILE_PATH.exists():
        errors.append(f'missing file: {BRAND_PROFILE_PATH}')
    else:
        try:
            profile = load_brand_profile()
            for key in ['company_name', 'buyer_name', 'email', 'phone', 'whatsapp', 'website', 'default_language', 'communication_tone', 'signature_plain']:
                if not profile.get(key):
                    errors.append(f'missing brand profile field: {key}')
        except Exception as exc:
            errors.append(f'invalid brand profile: {exc}')
    if len(TEMPLATES) != len(TEMPLATE_NAMES):
        errors.append('template registry size mismatch')
    for path in [RFQ_DRAFTS_JSONL, APPROVAL_QUEUE_JSONL, EMAILS_JSONL]:
        if not path.exists():
            errors.append(f'missing file: {path}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'brand_profile={BRAND_PROFILE_PATH}')
    print(f'templates={len(TEMPLATES)}')
    print(f'company={POLAR_SINERGY["name"]}')
    return 0


def cmd_generate_test_rfq(args: argparse.Namespace) -> int:
    ensure_storage()
    template_name = select_template(args.category or None, args.template or None)
    result = build_draft(
        template_name,
        product_name=args.product_name or None,
        product_description=args.product_description or None,
        category_id=args.category or None,
    )
    draft = result['draft']
    queue_item = result['queue_item']
    print(json.dumps({
        'ok': True,
        'draft': draft,
        'approval_queue': queue_item,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_preview_latest(_: argparse.Namespace) -> int:
    ensure_storage()
    draft = latest_record(RFQ_DRAFTS_JSONL)
    if not draft:
        print(json.dumps({'ok': True, 'message': 'no RFQ draft found'}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    queue = latest_record(APPROVAL_QUEUE_JSONL)
    print(json.dumps({
        'ok': True,
        'draft': draft,
        'approval_queue': queue,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_storage()
    drafts = load_jsonl_records(RFQ_DRAFTS_JSONL)
    queued = load_jsonl_records(APPROVAL_QUEUE_JSONL)
    print(json.dumps({
        'ok': True,
        'drafts_total': len(drafts),
        'pending_approvals': sum(1 for rec in queued if rec.get('status') == 'pending'),
        'templates_supported': len(TEMPLATES),
        'brand_profile_loaded': BRAND_PROFILE_PATH.exists(),
        'company_name': POLAR_SINERGY['name'],
        'signature_ok': bool(load_brand_profile().get('signature_plain')),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Polar Sinergy RFQ template engine')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate templates, brand profile, and stores')
    p_validate.set_defaults(func=cmd_validate)

    p_generate = sub.add_parser('generate-test-rfq', help='Generate a test RFQ draft in English')
    p_generate.add_argument('--template', default='generic')
    p_generate.add_argument('--category', default='')
    p_generate.add_argument('--product-name', default='')
    p_generate.add_argument('--product-description', default='')
    p_generate.set_defaults(func=cmd_generate_test_rfq)

    p_preview = sub.add_parser('preview-latest', help='Preview the latest generated RFQ draft')
    p_preview.set_defaults(func=cmd_preview_latest)

    p_stats = sub.add_parser('stats', help='Show RFQ template engine stats')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
