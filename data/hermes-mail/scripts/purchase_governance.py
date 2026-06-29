#!/usr/bin/env python3
"""Purchase governance persistence helpers for Hermes Mail.

This module stores the consultative sourcing workflow as append-only JSONL
records. It is intentionally filesystem-backed so it stays aligned with the
rest of Hermes Mail's provisional store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reporting_utils import (
    ROOT,
    append_jsonl,
    latest_jsonl_record,
    make_id,
    normalize_text,
    round_money,
    utc_now,
)

DATA_DIR = ROOT / 'data'
SCHEMAS_DIR = ROOT / 'schemas'

PURCHASE_COMPANIES_JSONL = ROOT / 'purchase-companies.jsonl'
PURCHASE_PRODUCTS_JSONL = ROOT / 'purchase-products.jsonl'
PURCHASE_CONTACTS_JSONL = ROOT / 'purchase-contacts.jsonl'
RFQ_BATCHES_JSONL = ROOT / 'rfq-batches.jsonl'
RFQ_BATCH_SUPPLIERS_JSONL = ROOT / 'rfq-batch-suppliers.jsonl'
SUPPLIER_QUOTES_JSONL = ROOT / 'supplier-quotes.jsonl'
HERMES_DECISION_RECOMMENDATIONS_JSONL = ROOT / 'hermes-decision-recommendations.jsonl'
FREIGHT_MARKET_INTELLIGENCE_JSONL = ROOT / 'freight-market-intelligence.jsonl'
LOGISTICS_NEWS_ALERTS_JSONL = ROOT / 'logistics-news-alerts.jsonl'
PURCHASE_TIMING_ANALYSIS_JSONL = ROOT / 'purchase-timing-analysis.jsonl'
USER_DECISION_LOGS_JSONL = ROOT / 'user-decision-logs.jsonl'

PURCHASE_JSONL_FILES = [
    PURCHASE_COMPANIES_JSONL,
    PURCHASE_PRODUCTS_JSONL,
    PURCHASE_CONTACTS_JSONL,
    RFQ_BATCHES_JSONL,
    RFQ_BATCH_SUPPLIERS_JSONL,
    SUPPLIER_QUOTES_JSONL,
    HERMES_DECISION_RECOMMENDATIONS_JSONL,
    FREIGHT_MARKET_INTELLIGENCE_JSONL,
    LOGISTICS_NEWS_ALERTS_JSONL,
    PURCHASE_TIMING_ANALYSIS_JSONL,
    USER_DECISION_LOGS_JSONL,
]


_DECISION_GATE_ACTIONS = [
    {'label': 'Aprovar compra agora', 'action': 'approve_purchase_now', 'requires_user_confirmation': True},
    {'label': 'Aprovar compra, mas aguardar frete', 'action': 'approve_purchase_wait_freight', 'requires_user_confirmation': True},
    {'label': 'Negociar com fornecedor', 'action': 'negotiate_supplier', 'requires_user_confirmation': True},
    {'label': 'Solicitar novas cotações', 'action': 'request_more_quotes', 'requires_user_confirmation': True},
    {'label': 'Embarcar parcial', 'action': 'approve_partial_shipment', 'requires_user_confirmation': True},
    {'label': 'Aguardar', 'action': 'wait_and_monitor', 'requires_user_confirmation': True},
    {'label': 'Cancelar', 'action': 'cancel_process', 'requires_user_confirmation': True},
]

DECISION_GATE_ACTION_MAP = {
    'approve_purchase_now': {'callback_action': 'approve_now', 'decision': 'approved', 'label': 'Aprovar compra agora'},
    'approve_purchase_wait_freight': {'callback_action': 'approve_wait_freight', 'decision': 'approved_with_hold', 'label': 'Aprovar compra, mas aguardar frete'},
    'negotiate_supplier': {'callback_action': 'negotiate', 'decision': 'needs_negotiation', 'label': 'Negociar com fornecedor'},
    'request_more_quotes': {'callback_action': 'request_more_quotes', 'decision': 'needs_more_quotes', 'label': 'Solicitar novas cotações'},
    'approve_partial_shipment': {'callback_action': 'partial_shipment', 'decision': 'approved_partial', 'label': 'Embarcar parcial'},
    'wait_and_monitor': {'callback_action': 'wait', 'decision': 'waiting', 'label': 'Aguardar'},
    'cancel_process': {'callback_action': 'cancel', 'decision': 'cancelled', 'label': 'Cancelar'},
}

PURCHASE_GATE_CALLBACK_ACTIONS = {value['callback_action']: key for key, value in DECISION_GATE_ACTION_MAP.items()}


def ensure_purchase_storage() -> None:
    for path in PURCHASE_JSONL_FILES:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


def _record_id(prefix: str, provided: str | None = None) -> str:
    return normalize_text(str(provided or '')).strip() or make_id(prefix)


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    return value


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    ensure_purchase_storage()
    append_jsonl(path, record)
    return record


def _build_base_record(prefix: str, record: dict[str, Any]) -> dict[str, Any]:
    data = dict(record)
    data['id'] = _record_id(prefix, data.get('id'))
    data.setdefault('created_at', utc_now())
    data['updated_at'] = utc_now()
    return data


def record_purchase_company(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('purchase_company', record)
    return _append(PURCHASE_COMPANIES_JSONL, data)


def record_purchase_product(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('purchase_product', record)
    return _append(PURCHASE_PRODUCTS_JSONL, data)


def record_purchase_contact(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('purchase_contact', record)
    return _append(PURCHASE_CONTACTS_JSONL, data)


def record_rfq_batch(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('rfq_batch', record)
    data.setdefault('status', 'pending_user_authorization')
    data.setdefault('user_decision_required', True)
    data.setdefault('user_decision', 'manual_review_pending')
    data.setdefault('decision_gate', {
        'type': 'decision_gate',
        'context': 'rfq_authorization',
        'actions': [
            {'label': 'Confirmar e autorizar envio para todos', 'action': 'authorize_all_suppliers', 'requires_user_confirmation': True},
            {'label': 'Autorizar apenas fornecedores selecionados', 'action': 'authorize_selected_suppliers', 'requires_user_confirmation': True},
            {'label': 'Revisar RFQ antes de enviar', 'action': 'review_rfq_before_send', 'requires_user_confirmation': True},
            {'label': 'Cancelar', 'action': 'cancel_process', 'requires_user_confirmation': True},
        ],
    })
    return _append(RFQ_BATCHES_JSONL, data)


def record_rfq_batch_supplier(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('rfq_batch_supplier', record)
    data.setdefault('status', 'pendente_envio')
    return _append(RFQ_BATCH_SUPPLIERS_JSONL, data)


def record_supplier_quote(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('supplier_quote', record)
    if isinstance(data.get('unit_price_usd'), (int, float)):
        data['unit_price_usd'] = round_money(float(data['unit_price_usd']))
    return _append(SUPPLIER_QUOTES_JSONL, data)


def build_recommendation_gate() -> dict[str, Any]:
    return {
        'type': 'decision_gate',
        'context': 'purchase_and_freight_timing',
        'actions': list(_DECISION_GATE_ACTIONS),
    }


def latest_purchase_recommendation() -> dict[str, Any] | None:
    return latest_jsonl_record(HERMES_DECISION_RECOMMENDATIONS_JSONL)


def build_purchase_gate_reply_markup(record: dict[str, Any]) -> dict[str, Any]:
    recommendation_id = str(record.get('id') or record.get('recommendation_id') or '').strip()
    keyboard: list[list[dict[str, Any]]] = []
    rows = [
        [
            {'text': '✅ Aprovar compra agora', 'callback_data': f'purchase_gate|approve_now|{recommendation_id}'},
            {'text': '🕒 Aprovar e aguardar frete', 'callback_data': f'purchase_gate|approve_wait_freight|{recommendation_id}'},
        ],
        [
            {'text': '🤝 Negociar com fornecedor', 'callback_data': f'purchase_gate|negotiate|{recommendation_id}'},
            {'text': '📩 Solicitar novas cotações', 'callback_data': f'purchase_gate|request_more_quotes|{recommendation_id}'},
        ],
        [
            {'text': '📦 Embarque parcial', 'callback_data': f'purchase_gate|partial_shipment|{recommendation_id}'},
            {'text': '⏳ Aguardar', 'callback_data': f'purchase_gate|wait|{recommendation_id}'},
        ],
        [
            {'text': '🛑 Cancelar', 'callback_data': f'purchase_gate|cancel|{recommendation_id}'},
        ],
    ]
    for row in rows:
        keyboard.append(row)
    return {'inline_keyboard': keyboard}


def build_purchase_gate_message(record: dict[str, Any]) -> str:
    decision_gate = record.get('decision_gate') if isinstance(record.get('decision_gate'), dict) else build_recommendation_gate()
    reasoning = str(record.get('reasoning_summary') or 'Sem explicação disponível.')
    risks = str(record.get('risk_summary') or 'Sem riscos explicitados.')
    product_name = str(record.get('product_name') or 'Produto')
    supplier_name = str(record.get('supplier_name') or 'Fornecedor')
    unit_price = record.get('unit_price_usd')
    lead_time_days = record.get('lead_time_days')
    incoterm = str(record.get('incoterm') or 'N/A')
    lines = [
        'Hermes Compras - Decisão necessária',
        f'Produto: {product_name}',
        f'Fornecedor: {supplier_name}',
        f'Preço unitário: USD {unit_price:.2f}' if isinstance(unit_price, (int, float)) else 'Preço unitário: N/A',
        f'Lead time: {lead_time_days} dias' if isinstance(lead_time_days, int) else 'Lead time: N/A',
        f'Incoterm: {incoterm}',
        '',
        f'Recomendação: {record.get("recommendation_title") or record.get("suggested_action") or "Revisão manual necessária"}',
        f'Motivo: {reasoning}',
        f'Riscos: {risks}',
        '',
        'Escolha uma ação abaixo para registrar a decisão humana.',
        f'Gate: {decision_gate.get("context") if isinstance(decision_gate, dict) else "purchase_and_freight_timing"}',
    ]
    return '\n'.join(lines)


def record_purchase_user_decision(record: dict[str, Any], *, callback_action: str, decided_by: str = 'telegram_inline_button', notes: str | None = None) -> dict[str, Any]:
    canonical_action = PURCHASE_GATE_CALLBACK_ACTIONS.get(callback_action, callback_action)
    meta = DECISION_GATE_ACTION_MAP.get(canonical_action, {})
    payload = {
        'id': make_id('user_decision'),
        'decision_context': str(record.get('recommendation_type') or 'purchase_and_freight_timing'),
        'related_entity_type': 'purchase_recommendation',
        'related_entity_id': record.get('id') or record.get('recommendation_id'),
        'rfq_batch_id': record.get('rfq_batch_id'),
        'supplier_id': record.get('supplier_id'),
        'product_id': record.get('product_id'),
        'quote_id': record.get('quote_id'),
        'recommendation_id': record.get('id') or record.get('recommendation_id'),
        'decision': str(meta.get('decision') or canonical_action),
        'decision_label': str(meta.get('label') or callback_action),
        'decided_by': decided_by,
        'decided_at': utc_now(),
        'decision_source': 'telegram_inline_button',
        'notes': notes or str(record.get('risk_summary') or ''),
        'payload_json': {
            'recommendation': record,
            'callback_action': callback_action,
            'canonical_action': canonical_action,
        },
        'created_at': utc_now(),
    }
    return record_user_decision(payload)


def build_purchase_gate_acknowledgement(record: dict[str, Any], *, callback_action: str) -> str:
    canonical_action = PURCHASE_GATE_CALLBACK_ACTIONS.get(callback_action, callback_action)
    meta = DECISION_GATE_ACTION_MAP.get(canonical_action, {})
    return '\n'.join([
        'Hermes Compras - decisão registrada',
        f'Produto: {record.get("product_name") or "Produto"}',
        f'Fornecedor: {record.get("supplier_name") or "Fornecedor"}',
        f'Decisão: {meta.get("label") or callback_action}',
        f'Status: {meta.get("decision") or canonical_action}',
    ])


def build_purchase_recommendation(*, product_name: str, supplier_name: str, supplier_country: str | None = None, supplier_city: str | None = None, unit_price_usd: float | None = None, incoterm: str | None = None, lead_time_days: int | None = None, freight_trend: str | None = None, stock_current_qty: int | None = None, stock_min_qty: int | None = None, stock_coverage_days: int | None = None, margin_status: str | None = None, product_price_trend: str | None = None, exchange_rate_risk_level: str | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    notes = [normalize_text(str(item)) for item in (notes or []) if normalize_text(str(item))]
    reasoning: list[str] = []
    risks: list[str] = []
    action = 'manual_review_required'
    recommendation_type = 'purchase_timing'
    title = 'Revisão manual necessária'

    if isinstance(stock_coverage_days, int) and stock_coverage_days <= 0:
        risks.append('Cobertura de estoque não informada')
    elif isinstance(stock_coverage_days, int) and stock_coverage_days < 30:
        risks.append(f'Cobertura de estoque baixa ({stock_coverage_days} dias)')
        reasoning.append('O estoque parece apertado para o lead time informado.')
        action = 'buy_due_to_stock_risk'
        title = 'Comprar por risco de ruptura'
    elif isinstance(stock_coverage_days, int) and stock_coverage_days > 120:
        risks.append(f'Estoque alto ({stock_coverage_days} dias de cobertura)')
        reasoning.append('O estoque está alto; vale evitar compra por impulso.')
        action = 'wait_due_to_high_stock'
        title = 'Aguardar por estoque alto'

    if isinstance(unit_price_usd, (int, float)):
        reasoning.append(f'Preço unitário informado: USD {unit_price_usd:.2f}')
    if incoterm:
        reasoning.append(f'Incoterm recebido: {incoterm}')
    if lead_time_days is not None:
        reasoning.append(f'Prazo de produção estimado: {lead_time_days} dias')
        if isinstance(stock_coverage_days, int) and stock_coverage_days and lead_time_days and lead_time_days > stock_coverage_days:
            risks.append('Lead time maior do que a cobertura de estoque')
            action = 'buy_due_to_stock_risk'
            title = 'Comprar para proteger estoque'
    if freight_trend:
        reasoning.append(f'Tendência de frete: {freight_trend}')
        if freight_trend in {'falling', 'stable'} and action == 'manual_review_required':
            action = 'wait_for_better_freight'
            title = 'Aguardar frete melhor'
    if product_price_trend:
        reasoning.append(f'Tendência de preço do produto: {product_price_trend}')
        if product_price_trend in {'rising', 'volatile'} and action == 'manual_review_required':
            action = 'negotiate_before_buying'
            title = 'Negociar antes de comprar'
    if exchange_rate_risk_level:
        reasoning.append(f'Risco cambial: {exchange_rate_risk_level}')
        if exchange_rate_risk_level in {'high', 'critical'}:
            risks.append('Câmbio pressionado')
    if margin_status:
        reasoning.append(f'Situação de margem: {margin_status}')
        if margin_status in {'baixo', 'warning', 'at_risk'} and action == 'manual_review_required':
            action = 'negotiate_before_buying'
            title = 'Negociar para proteger margem'

    if not reasoning:
        reasoning.append('Dados de estoque, frete e margem ainda não foram suficientes para decisão automática.')
    if not risks:
        risks.append('Estoque, frete ou margem podem estar incompletos; revisar antes de executar ação crítica.')

    return {
        'id': make_id('decision_recommendation'),
        'rfq_batch_id': None,
        'supplier_id': None,
        'product_id': None,
        'quote_id': None,
        'recommendation_type': recommendation_type,
        'recommendation_title': title,
        'recommendation_text': f'Recommendation for {normalize_text(product_name)} with {normalize_text(supplier_name)}',
        'reasoning_summary': ' | '.join(reasoning),
        'risk_summary': ' | '.join(risks),
        'suggested_action': action,
        'user_decision_required': True,
        'user_decision': 'manual_review_pending',
        'user_decision_by': None,
        'user_decision_at': None,
        'status': 'awaiting_user_decision',
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'product_name': normalize_text(product_name),
        'supplier_name': normalize_text(supplier_name),
        'supplier_country': normalize_text(supplier_country or ''),
        'supplier_city': normalize_text(supplier_city or ''),
        'unit_price_usd': unit_price_usd,
        'incoterm': normalize_text(incoterm or ''),
        'lead_time_days': lead_time_days,
        'notes': notes,
        'decision_gate': build_recommendation_gate(),
    }


def record_purchase_recommendation(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('decision_recommendation', record)
    data.setdefault('status', 'awaiting_user_decision')
    data.setdefault('user_decision_required', True)
    data.setdefault('user_decision', 'manual_review_pending')
    data.setdefault('decision_gate', build_recommendation_gate())
    return _append(HERMES_DECISION_RECOMMENDATIONS_JSONL, data)


def record_purchase_timing_analysis(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('purchase_timing', record)
    data.setdefault('user_decision_required', True)
    data.setdefault('user_decision', 'manual_review_pending')
    return _append(PURCHASE_TIMING_ANALYSIS_JSONL, data)


def record_freight_market_intelligence(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('freight_market', record)
    return _append(FREIGHT_MARKET_INTELLIGENCE_JSONL, data)


def record_logistics_news_alert(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('logistics_news', record)
    return _append(LOGISTICS_NEWS_ALERTS_JSONL, data)


def record_user_decision(record: dict[str, Any]) -> dict[str, Any]:
    data = _build_base_record('user_decision', record)
    data.setdefault('decision_source', 'manual_admin_action')
    return _append(USER_DECISION_LOGS_JSONL, data)
