"""SQLite-backed persistence for Hermes Compras RFQ workflows.

This module stores the durable state for sourcing, RFQ approval, email
authorization, audit logs, and related commercial intelligence.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home

DEFAULT_DB_PATH = get_hermes_home() / "hermes_compras.db"
SCHEMA_VERSION = 5
SALES_EMBASSIES_JSONL = Path(__file__).resolve().parent / "data" / "hermes-mail" / "sales-embassies.jsonl"

DEFAULT_PRODUCT_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "Bicarbonato de Sódio",
        "description": "Aditivo alimentar e insumo industrial para aplicações alimentícias e sanitárias.",
        "application": "Alimentos, bebidas, limpeza e processos industriais",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "purity": ">= 99%",
                "moisture": "<= 0.25%",
                "particle_size": "micronizado ou padrão industrial",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Bicarbonato de Amônio",
        "description": "Agente de expansão e insumo para panificação e aplicações técnicas.",
        "application": "Panificação, biscoitos e formulações técnicas",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "purity": ">= 99%",
                "ammonia_residual": "controlado",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Bicarbonato de Potássio",
        "description": "Ingrediente funcional para formulações alimentícias e soluções técnicas.",
        "application": "Alimentos, bebidas e controle de pH",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "purity": ">= 99%",
                "potassium_content": "alto",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Sagu",
        "description": "Amido granulado para uso alimentício em sobremesas e misturas.",
        "application": "Alimentos e food service",
        "unit": "kg",
        "packaging": "Sacos de 20 kg",
        "technical_specs": json.dumps(
            {
                "starch_source": "mandioca",
                "granulometry": "uniforme",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Fécula de Batata",
        "description": "Amido refinado para uso em alimentos, indústria e aplicações funcionais.",
        "application": "Alimentos, molhos, snacks e indústria",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "source": "batata",
                "whiteness": "alta",
                "viscosity": "controlada",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Fécula de Mandioca",
        "description": "Amido de mandioca para alimentos e indústria de transformação.",
        "application": "Alimentos, doces, massa e formulações",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "source": "mandioca",
                "starch_content": "alto",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Polvilho Doce",
        "description": "Amido seco para panificação e aplicações alimentícias.",
        "application": "Panificação, confeitaria e alimentos",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "source": "mandioca",
                "acidity": "baixa",
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
    {
        "name": "Polvilho Azedo",
        "description": "Amido fermentado para biscoitos, pão de queijo e alimentos típicos.",
        "application": "Panificação, biscoitos e snacks",
        "unit": "kg",
        "packaging": "Sacos de 25 kg",
        "technical_specs": json.dumps(
            {
                "source": "mandioca",
                "acidity": "controlada",
                "fermented": True,
                "food_grade": True,
            },
            ensure_ascii=False,
        ),
    },
]

PRODUCT_SALES_GUIDES: dict[str, dict[str, Any]] = {
    "bicarbonato de sodio": {
        "family": "alcalinos",
        "mandatory_specs": [
            "grau alimenticio ou técnico",
            "pureza",
            "granulometria / malha",
            "umidade",
            "embalagem",
            "país de origem",
            "certificações aplicáveis",
        ],
        "market_differentiators": [
            "pureza consistente por lote",
            "baixo teor de umidade e boa fluidez",
            "documentação limpa para importação",
            "embalagem estável para transporte internacional",
        ],
        "transactional_angles": [
            "reduz risco operacional do comprador",
            "facilita padronização de lote em linha contínua",
            "melhora previsibilidade de custo total entregue",
        ],
        "buyer_questions": [
            "Qual a pureza mínima aceita?",
            "Qual a malha/granulometria desejada?",
            "Há exigência de grau alimentício?",
        ],
    },
    "bicarbonato de amonio": {
        "family": "levedantes",
        "mandatory_specs": [
            "grau alimenticio",
            "pureza",
            "umidade",
            "odor/resíduo",
            "embalagem",
            "certificado de análise",
        ],
        "market_differentiators": [
            "estabilidade de desempenho na panificação",
            "controle de umidade para shelf life",
            "qualidade constante para produção em escala",
        ],
        "transactional_angles": [
            "ajuda o comprador a reduzir perdas de produção",
            "melhora consistência em formulações sensíveis",
        ],
        "buyer_questions": [
            "O produto será usado em panificação ou aplicação técnica?",
            "Qual o limite de umidade aceitável?",
            "Exige laudo por lote?",
        ],
    },
    "bicarbonato de potassio": {
        "family": "alcalinos",
        "mandatory_specs": [
            "grau alimenticio",
            "teor de potássio",
            "pureza",
            "umidade",
            "embalagem",
            "certificações",
        ],
        "market_differentiators": [
            "alto teor ativo e consistência",
            "boas condições para formulações premium",
            "suporte técnico na adequação regulatória",
        ],
        "transactional_angles": [
            "bom argumento para compradores que buscam formulação premium",
            "ajuda a reduzir ajustes de processo na fábrica",
        ],
        "buyer_questions": [
            "Qual o teor mínimo de potássio?",
            "Há necessidade de certificação específica?",
            "Precisa de embalagem food grade?",
        ],
    },
    "sagu": {
        "family": "amidos",
        "mandatory_specs": [
            "origem do amido",
            "granulometria",
            "umidade",
            "cor / brancura",
            "embalagem",
            "aplicação prevista",
        ],
        "market_differentiators": [
            "uniformidade visual e de cozimento",
            "boa performance em sobremesas e food service",
            "padronização para redes e distribuidores",
        ],
        "transactional_angles": [
            "produto familiar e de giro recorrente",
            "boa porta de entrada para distribuição alimentar",
        ],
        "buyer_questions": [
            "A aplicação é food service ou varejo?",
            "Qual o padrão de granulação esperado?",
            "Há necessidade de marca própria?",
        ],
    },
    "fécula de batata": {
        "family": "amidos",
        "mandatory_specs": [
            "teor de amido",
            "viscosidade",
            "brancura",
            "umidade",
            "embalagem",
            "certificação alimentar",
        ],
        "market_differentiators": [
            "textura limpa e performance consistente",
            "boa performance em molhos e snacks",
            "qualidade estável por lote",
        ],
        "transactional_angles": [
            "ajuda o comprador a proteger formulação e rendimento",
            "facilita reposição de estoque com padrão consistente",
        ],
        "buyer_questions": [
            "Qual a viscosidade desejada?",
            "O uso é industrial ou alimentar?",
            "Existe requisito de não GMO ou alergênicos?",
        ],
    },
    "fécula de mandioca": {
        "family": "amidos",
        "mandatory_specs": [
            "origem da mandioca",
            "teor de amido",
            "umidade",
            "brancura",
            "embalagem",
            "uso alimentar",
        ],
        "market_differentiators": [
            "alta aceitação em múltiplos mercados",
            "bom custo-benefício transacional",
            "adequada para produtos com demanda recorrente",
        ],
        "transactional_angles": [
            "boa margem de negociação por volume",
            "produto com demanda consistente em food ingredients",
        ],
        "buyer_questions": [
            "Qual o teor mínimo de amido?",
            "Precisa de especificação para padaria ou indústria?",
            "Qual o volume mensal de consumo?",
        ],
    },
    "polvilho doce": {
        "family": "amidos",
        "mandatory_specs": [
            "origem da mandioca",
            "acidez",
            "umidade",
            "granulometria",
            "embalagem",
            "uso alimentar",
        ],
        "market_differentiators": [
            "perfil limpo para receitas tradicionais",
            "boa solubilidade e desempenho em panificação",
            "padronização para marcas próprias",
        ],
        "transactional_angles": [
            "produto de recorrência alta no canal food service",
            "simples de especificar e vender por padrão",
        ],
        "buyer_questions": [
            "Qual a acidez máxima tolerada?",
            "O destino é food service, varejo ou indústria?",
            "Há necessidade de private label?",
        ],
    },
    "polvilho azedo": {
        "family": "amidos",
        "mandatory_specs": [
            "origem da mandioca",
            "acidez",
            "umidade",
            "fermentação",
            "embalagem",
            "uso alimentar",
        ],
        "market_differentiators": [
            "resultado sensorial consistente em produtos típicos",
            "boa diferenciação em mercados latino-americanos",
            "padrão estável para produção em escala",
        ],
        "transactional_angles": [
            "forte apelo comercial para distribuidores de ingredientes",
            "bom produto de recorrência com ticket simples",
        ],
        "buyer_questions": [
            "Qual o nível de acidez esperado?",
            "A aplicação é pão de queijo, biscoito ou snack?",
            "Existe requisito de fermentação controlada?",
        ],
    },
}

SECOM_PRODUCT_REGION_HINTS: dict[str, list[str]] = {
    "alcalinos": ["Argentina", "Chile", "Colombia", "Peru", "Mexico", "United States", "Spain", "Portugal"],
    "levedantes": ["Argentina", "Chile", "Mexico", "Peru", "Colombia", "United States", "Spain"],
    "amidos": ["Chile", "Argentina", "Peru", "Colombia", "Mexico", "United States", "Spain", "Portugal", "United Arab Emirates"],
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    supplier_type TEXT NOT NULL DEFAULT 'unknown',
    country TEXT,
    state_province TEXT,
    city TEXT,
    full_address TEXT,
    street TEXT,
    number TEXT,
    district TEXT,
    postal_code TEXT,
    website TEXT,
    general_email TEXT,
    sales_email TEXT,
    phone TEXT,
    whatsapp TEXT,
    contact_page_url TEXT,
    source_url TEXT,
    source_name TEXT,
    source_type TEXT,
    manufacturer_flag INTEGER NOT NULL DEFAULT 0,
    trading_company_flag INTEGER NOT NULL DEFAULT 0,
    verified_status TEXT NOT NULL DEFAULT 'unverified',
    data_quality_status TEXT NOT NULL DEFAULT 'pending_validation',
    approved_from_rfq INTEGER NOT NULL DEFAULT 0,
    approved_rfq_batch_id TEXT,
    approved_by_user TEXT,
    approved_at REAL,
    user_authorized_email_sending INTEGER NOT NULL DEFAULT 0,
    email_sending_authorized_by TEXT,
    email_sending_authorized_at REAL,
    email_automation_scope TEXT NOT NULL DEFAULT 'this_rfq_only',
    email_automation_status TEXT NOT NULL DEFAULT 'manual_review_required',
    duplicate_check_status TEXT NOT NULL DEFAULT 'requires_user_review',
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    name TEXT,
    role TEXT,
    department TEXT,
    email TEXT,
    phone TEXT,
    whatsapp TEXT,
    wechat TEXT,
    linkedin TEXT,
    language TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    source_email_id TEXT,
    source_attachment_id TEXT,
    verified_status TEXT NOT NULL DEFAULT 'unverified',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    technical_specs TEXT,
    application TEXT,
    unit TEXT,
    ncm TEXT,
    hs_code TEXT,
    packaging TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS secom_offices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    office_name TEXT NOT NULL,
    city TEXT,
    language TEXT,
    email_primary TEXT,
    email_alternatives TEXT,
    phone TEXT,
    website TEXT,
    commercial_sector TEXT,
    product_focus TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    followup_interval_days INTEGER NOT NULL DEFAULT 45,
    report_interval_days INTEGER NOT NULL DEFAULT 45,
    last_request_at REAL,
    last_report_at REAL,
    source TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_product_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    match_status TEXT NOT NULL DEFAULT 'candidate',
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(supplier_id, product_id)
);

CREATE TABLE IF NOT EXISTS rfq_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    user_authorized INTEGER NOT NULL DEFAULT 0,
    authorized_by TEXT,
    authorized_at REAL,
    authorization_source TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    country TEXT,
    city TEXT,
    website TEXT,
    source_url TEXT,
    source_name TEXT,
    source_type TEXT,
    manufacturer_flag INTEGER NOT NULL DEFAULT 0,
    trading_company_flag INTEGER NOT NULL DEFAULT 0,
    verified_status TEXT NOT NULL DEFAULT 'unverified',
    data_quality_status TEXT NOT NULL DEFAULT 'pending_validation',
    selected_by_user INTEGER NOT NULL DEFAULT 0,
    approved_by_user INTEGER NOT NULL DEFAULT 0,
    rejected_by_user INTEGER NOT NULL DEFAULT 0,
    candidate_payload_json TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    selected_by_user INTEGER NOT NULL DEFAULT 0,
    approved_by_user INTEGER NOT NULL DEFAULT 0,
    user_authorized_email_sending INTEGER NOT NULL DEFAULT 0,
    email_sending_authorized_by TEXT,
    email_sending_authorized_at REAL,
    email_automation_scope TEXT NOT NULL DEFAULT 'this_rfq_only',
    send_status TEXT NOT NULL DEFAULT 'pending',
    sent_at REAL,
    error_message TEXT,
    followup_7_days_enabled INTEGER NOT NULL DEFAULT 0,
    followup_7_days_authorized_by TEXT,
    followup_7_days_authorized_at REAL,
    followup_7_days_sent INTEGER NOT NULL DEFAULT 0,
    followup_7_days_sent_at REAL,
    followup_7_days_email_log_id INTEGER,
    followup_count INTEGER NOT NULL DEFAULT 0,
    max_followups_allowed INTEGER NOT NULL DEFAULT 1,
    next_followup_due_at REAL,
    followup_status TEXT NOT NULL DEFAULT 'not_scheduled',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(rfq_batch_id, supplier_id, contact_id)
);

CREATE TABLE IF NOT EXISTS rfq_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    subject TEXT,
    body TEXT,
    signature_used TEXT,
    language TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    recipient_id INTEGER REFERENCES rfq_recipients(id) ON DELETE SET NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    to_email TEXT,
    cc TEXT,
    bcc TEXT,
    subject TEXT,
    body_snapshot TEXT,
    signature_snapshot TEXT,
    provider TEXT,
    provider_message_id TEXT,
    provider_thread_id TEXT,
    email_message_id TEXT,
    email_thread_id TEXT,
    reply_to_email TEXT,
    in_reply_to TEXT,
    email_references TEXT,
    correlation_token TEXT,
    subject_normalized TEXT,
    parent_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    followup_sequence INTEGER NOT NULL DEFAULT 0,
    response_status TEXT NOT NULL DEFAULT 'awaiting_response',
    first_response_email_id INTEGER,
    first_response_at REAL,
    last_response_email_id INTEGER,
    last_response_at REAL,
    next_followup_due_at REAL,
    followup_blocked_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at REAL,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_inbound_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    message_id TEXT,
    in_reply_to TEXT,
    email_references TEXT,
    from_email TEXT,
    from_name TEXT,
    to_email TEXT,
    cc TEXT,
    subject TEXT,
    received_at REAL NOT NULL,
    body_text TEXT,
    body_html TEXT,
    body_summary TEXT,
    detected_language TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    raw_payload_path TEXT,
    processing_status TEXT NOT NULL DEFAULT 'received',
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    linked_outbound_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    provider_thread_id TEXT,
    email_thread_id TEXT,
    correlation_token TEXT,
    matched_by TEXT,
    matching_confidence REAL,
    is_direct_reply INTEGER NOT NULL DEFAULT 0,
    is_followup_reply INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_email_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    inbound_email_id INTEGER NOT NULL REFERENCES rfq_inbound_emails(id) ON DELETE CASCADE,
    original_filename TEXT,
    stored_filename TEXT,
    file_path TEXT NOT NULL,
    file_extension TEXT,
    mime_type TEXT,
    file_size_bytes INTEGER,
    sha256_hash TEXT,
    document_type TEXT NOT NULL DEFAULT 'unknown',
    processing_status TEXT NOT NULL DEFAULT 'received',
    extracted_text TEXT,
    extracted_json TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL DEFAULT 'manual_entry',
    source_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    source_attachment_id INTEGER REFERENCES rfq_email_attachments(id) ON DELETE SET NULL,
    source_text_reference TEXT,
    confidence_score REAL,
    requires_user_review INTEGER NOT NULL DEFAULT 0,
    currency TEXT,
    unit_price REAL,
    quantity REAL,
    unit TEXT,
    moq REAL,
    incoterm TEXT,
    origin_port TEXT,
    origin_airport TEXT,
    destination_port TEXT,
    destination_airport TEXT,
    lead_time_days INTEGER,
    production_time_days INTEGER,
    validity_date TEXT,
    payment_terms TEXT,
    packaging TEXT,
    technical_specs TEXT,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_quote_specifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES supplier_quotes(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    specification_name TEXT NOT NULL,
    specification_value TEXT,
    unit TEXT,
    source_type TEXT,
    source_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    source_attachment_id INTEGER REFERENCES rfq_email_attachments(id) ON DELETE SET NULL,
    confidence_score REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_quote_commercial_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES supplier_quotes(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    incoterm TEXT,
    currency TEXT,
    unit_price REAL,
    moq REAL,
    payment_terms TEXT,
    validity_date TEXT,
    production_lead_time_days INTEGER,
    delivery_time_days INTEGER,
    warranty_terms TEXT,
    packing_terms TEXT,
    inspection_terms TEXT,
    certification_terms TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profit_margin_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_category TEXT,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    customer_id TEXT,
    incoterm TEXT,
    margin_type TEXT NOT NULL DEFAULT 'percentage',
    margin_value REAL NOT NULL,
    currency TEXT,
    valid_from TEXT,
    valid_until TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_price_calculations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    quote_id INTEGER REFERENCES supplier_quotes(id) ON DELETE SET NULL,
    margin_rule_id INTEGER REFERENCES profit_margin_rules(id) ON DELETE SET NULL,
    source_incoterm TEXT,
    sale_incoterm TEXT,
    purchase_currency TEXT,
    sale_currency TEXT,
    purchase_unit_price REAL,
    international_freight REAL,
    insurance REAL,
    origin_charges REAL,
    destination_charges REAL,
    customs_clearance_cost REAL,
    import_duties_estimated REAL,
    taxes_estimated REAL,
    inland_freight REAL,
    warehouse_cost REAL,
    financial_cost REAL,
    other_costs REAL,
    total_landed_cost REAL,
    margin_type TEXT,
    margin_value REAL,
    margin_amount REAL,
    sale_unit_price REAL,
    sale_total_price REAL,
    calculation_status TEXT NOT NULL DEFAULT 'draft',
    requires_user_approval INTEGER NOT NULL DEFAULT 1,
    approved_by TEXT,
    approved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logistics_news_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT,
    source_name TEXT,
    source_url TEXT,
    published_at REAL,
    collected_at REAL NOT NULL,
    modal TEXT,
    affected_routes TEXT,
    affected_countries TEXT,
    affected_ports TEXT,
    affected_airports TEXT,
    affected_carriers TEXT,
    risk_level TEXT,
    expected_impact TEXT,
    recommended_action TEXT,
    related_rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    related_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_market_intelligence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_country TEXT,
    origin_city TEXT,
    origin_port TEXT,
    destination_country TEXT,
    destination_city TEXT,
    destination_port TEXT,
    modal TEXT,
    route TEXT,
    carrier TEXT,
    freight_forwarder TEXT,
    currency TEXT,
    freight_value REAL,
    container_type TEXT,
    chargeable_weight REAL,
    volume_cbm REAL,
    gross_weight REAL,
    transit_time_days INTEGER,
    validity_date TEXT,
    source_type TEXT,
    source_name TEXT,
    source_url TEXT,
    source_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    source_attachment_id INTEGER REFERENCES rfq_email_attachments(id) ON DELETE SET NULL,
    market_trend TEXT,
    risk_level TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_timing_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    quote_id INTEGER REFERENCES supplier_quotes(id) ON DELETE SET NULL,
    stock_current_qty REAL,
    stock_reserved_qty REAL,
    stock_available_qty REAL,
    stock_in_transit_qty REAL,
    stock_min_qty REAL,
    monthly_average_sales_qty REAL,
    stock_coverage_days REAL,
    production_lead_time_days REAL,
    transit_time_days REAL,
    estimated_customs_clearance_days REAL,
    total_replenishment_days REAL,
    rupture_risk_level TEXT,
    freight_trend TEXT,
    freight_risk_level TEXT,
    product_price_trend TEXT,
    exchange_rate_risk_level TEXT,
    margin_status TEXT,
    recommendation TEXT,
    reasoning_summary TEXT,
    user_decision_required INTEGER NOT NULL DEFAULT 1,
    user_decision TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hermes_decision_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    quote_id INTEGER REFERENCES supplier_quotes(id) ON DELETE SET NULL,
    recommendation_type TEXT NOT NULL,
    recommendation_title TEXT NOT NULL,
    recommendation_text TEXT,
    reasoning_summary TEXT,
    risk_summary TEXT,
    suggested_action TEXT,
    user_decision_required INTEGER NOT NULL DEFAULT 1,
    user_decision TEXT,
    user_decision_by TEXT,
    user_decision_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_decision_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_context TEXT NOT NULL,
    related_entity_type TEXT,
    related_entity_id TEXT,
    rfq_batch_id INTEGER REFERENCES rfq_batches(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    quote_id INTEGER REFERENCES supplier_quotes(id) ON DELETE SET NULL,
    recommendation_id INTEGER REFERENCES hermes_decision_recommendations(id) ON DELETE SET NULL,
    decision TEXT NOT NULL,
    decision_label TEXT,
    decided_by TEXT,
    decided_at REAL NOT NULL,
    decision_source TEXT,
    notes TEXT,
    payload_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload_json TEXT,
    error_message TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_field_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source_type TEXT,
    source_url TEXT,
    source_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    source_attachment_id INTEGER REFERENCES rfq_email_attachments(id) ON DELETE SET NULL,
    confidence_score REAL,
    updated_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rfq_email_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    thread_key TEXT NOT NULL,
    provider_thread_id TEXT,
    first_outbound_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    latest_outbound_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    first_inbound_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    latest_inbound_email_id INTEGER REFERENCES rfq_inbound_emails(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_response',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(rfq_batch_id, supplier_id, thread_key)
);

CREATE TABLE IF NOT EXISTS rfq_followup_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_batch_id INTEGER NOT NULL REFERENCES rfq_batches(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES supplier_contacts(id) ON DELETE SET NULL,
    recipient_id INTEGER REFERENCES rfq_recipients(id) ON DELETE SET NULL,
    original_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    followup_email_log_id INTEGER REFERENCES rfq_email_logs(id) ON DELETE SET NULL,
    followup_sequence INTEGER NOT NULL DEFAULT 1,
    due_at REAL,
    sent_at REAL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    subject TEXT,
    body_snapshot TEXT,
    signature_snapshot TEXT,
    reason TEXT,
    blocked_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_suppliers_legal_name ON suppliers(legal_name);
CREATE INDEX IF NOT EXISTS idx_suppliers_source_url ON suppliers(source_url);
CREATE INDEX IF NOT EXISTS idx_supplier_contacts_supplier_primary ON supplier_contacts(supplier_id, is_primary);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_secom_offices_country ON secom_offices(country);
CREATE UNIQUE INDEX IF NOT EXISTS idx_secom_offices_unique ON secom_offices(country, office_name, COALESCE(city, ''));
CREATE INDEX IF NOT EXISTS idx_rfq_batches_batch_code ON rfq_batches(batch_code);
CREATE INDEX IF NOT EXISTS idx_rfq_candidates_batch ON rfq_candidates(rfq_batch_id, approved_by_user, rejected_by_user);
CREATE INDEX IF NOT EXISTS idx_rfq_recipients_batch_supplier ON rfq_recipients(rfq_batch_id, supplier_id);
CREATE INDEX IF NOT EXISTS idx_rfq_messages_batch ON rfq_messages(rfq_batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rfq_email_logs_batch_supplier ON rfq_email_logs(rfq_batch_id, supplier_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rfq_inbound_emails_batch_supplier ON rfq_inbound_emails(rfq_batch_id, supplier_id, received_at);
CREATE INDEX IF NOT EXISTS idx_rfq_email_attachments_inbound ON rfq_email_attachments(inbound_email_id);
CREATE INDEX IF NOT EXISTS idx_supplier_quotes_batch_supplier ON supplier_quotes(rfq_batch_id, supplier_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_entity_field_sources_entity ON entity_field_sources(entity_type, entity_id, field_name);
CREATE INDEX IF NOT EXISTS idx_user_decision_logs_batch ON user_decision_logs(rfq_batch_id, decided_at);
"""


@dataclass
class ComprasRow:
    data: dict[str, Any]


def _candidate_text_blob(candidate: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in (
        "legal_name",
        "trade_name",
        "supplier_type",
        "country",
        "city",
        "website",
        "source_url",
        "source_name",
        "source_type",
        "general_email",
        "sales_email",
        "phone",
        "whatsapp",
        "contact_page_url",
        "notes",
    ):
        value = candidate.get(key)
        if value:
            pieces.append(str(value))
    payload = candidate.get("candidate_payload_json")
    if isinstance(payload, str) and payload.strip():
        pieces.append(payload)
    return " ".join(pieces).lower()


def _qualify_supplier_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Attach a deterministic qualification and ranking snapshot to a supplier candidate."""
    enriched = dict(candidate)
    text_blob = _candidate_text_blob(enriched)

    score = 0.0
    reasons: list[str] = []
    score_breakdown = {
        "manufacturer_signal": 0.0,
        "contact_quality": 0.0,
        "digital_presence": 0.0,
        "source_reliability": 0.0,
        "data_quality": 0.0,
        "product_signal": 0.0,
    }

    manufacturer_flag = bool(enriched.get("manufacturer_flag"))
    trading_company_flag = bool(enriched.get("trading_company_flag"))
    verified_status = str(enriched.get("verified_status") or "unverified").strip().lower()
    data_quality_status = str(enriched.get("data_quality_status") or "pending_validation").strip().lower()
    source_type = str(enriched.get("source_type") or "").strip().lower()

    if manufacturer_flag:
        score += 30.0
        score_breakdown["manufacturer_signal"] += 30.0
        reasons.append("sinal de fabricante declarado")
    if trading_company_flag:
        score -= 12.0
        score_breakdown["manufacturer_signal"] -= 12.0
        reasons.append("sinal de trading company")

    contact_hits = 0
    for field, weight in (
        ("general_email", 8.0),
        ("sales_email", 8.0),
        ("phone", 6.0),
        ("whatsapp", 4.0),
        ("contact_page_url", 4.0),
    ):
        if str(enriched.get(field) or "").strip():
            score += weight
            score_breakdown["contact_quality"] += weight
            contact_hits += 1
    if contact_hits:
        reasons.append("contato comercial localizado")

    if str(enriched.get("website") or "").strip():
        score += 8.0
        score_breakdown["digital_presence"] += 8.0
        reasons.append("website informado")
    if str(enriched.get("source_url") or "").strip():
        score += 6.0
        score_breakdown["digital_presence"] += 6.0
        reasons.append("fonte original rastreável")
    if str(enriched.get("country") or "").strip():
        score += 2.0
        score_breakdown["digital_presence"] += 2.0
    if str(enriched.get("city") or "").strip():
        score += 2.0
        score_breakdown["digital_presence"] += 2.0

    if verified_status in {"verified", "approved", "approved_for_rfq"}:
        score += 14.0
        score_breakdown["data_quality"] += 14.0
        reasons.append("status verificado")
    elif verified_status in {"partially_verified", "pending_review"}:
        score += 4.0
        score_breakdown["data_quality"] += 4.0
    elif verified_status in {"unverified", "unknown"}:
        score -= 4.0
        score_breakdown["data_quality"] -= 4.0
    elif verified_status in {"rejected", "blocked", "disqualified"}:
        score -= 18.0
        score_breakdown["data_quality"] -= 18.0
        reasons.append("status desqualificado")

    if data_quality_status in {"complete", "validated", "ready_for_rfq"}:
        score += 10.0
        score_breakdown["data_quality"] += 10.0
        reasons.append("dados completos")
    elif data_quality_status in {"needs_review", "pending_validation"}:
        score -= 4.0
        score_breakdown["data_quality"] -= 4.0
    elif data_quality_status in {"low", "poor", "incomplete"}:
        score -= 12.0
        score_breakdown["data_quality"] -= 12.0

    if source_type in {"official_site", "manufacturer_catalog", "trade_fair_exhibitor", "export_registry"}:
        score += 10.0
        score_breakdown["source_reliability"] += 10.0
        reasons.append("fonte industrial confiável")
    elif source_type in {"marketplace", "trading_platform", "directory"}:
        score -= 8.0
        score_breakdown["source_reliability"] -= 8.0
        reasons.append("fonte de marketplace/diretório")

    if "factory" in text_blob or "manufacturer" in text_blob or "fabrica" in text_blob or "fabricante" in text_blob:
        score += 4.0
        score_breakdown["product_signal"] += 4.0
    if "trading company" in text_blob or "trading" in text_blob:
        score -= 4.0
        score_breakdown["product_signal"] -= 4.0

    score = max(0.0, min(100.0, score))
    if score >= 70.0 and (manufacturer_flag or source_type in {"official_site", "manufacturer_catalog", "export_registry"}):
        qualification_status = "approved_for_rfq"
    elif score >= 45.0:
        qualification_status = "manual_review_required"
    else:
        qualification_status = "not_qualified"

    if qualification_status == "approved_for_rfq":
        reasons.append("apto para RFQ")
    elif qualification_status == "manual_review_required":
        reasons.append("requer revisão manual")
    else:
        reasons.append("não qualificado para RFQ")

    contact_quality_score = round(score_breakdown["contact_quality"], 1)
    manufacturer_score = round(score_breakdown["manufacturer_signal"], 1)
    product_match_score = round(score_breakdown["product_signal"], 1)

    enriched.update(
        {
            "qualification_score": round(score, 1),
            "qualification_status": qualification_status,
            "qualification_reasons": reasons,
            "qualification_summary": "; ".join(reasons),
            "qualification_rank": None,
            "approved_for_rfq": qualification_status == "approved_for_rfq",
            "trust_confidence": min(100.0, round(score + 10.0 if manufacturer_flag else score, 1)),
            "supplier_trust_score": round(score, 1),
            "supplier_trust_confidence": min(100.0, round(score + 10.0 if manufacturer_flag else score, 1)),
            "manufacturer_score": manufacturer_score,
            "contact_quality_score": contact_quality_score,
            "product_match_score": product_match_score,
            "score_breakdown": score_breakdown,
        }
    )
    return enriched


def _rank_qualified_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = [_qualify_supplier_candidate(candidate) for candidate in candidates]
    enriched.sort(
        key=lambda item: (
            -float(item.get("qualification_score") or 0.0),
            -int(bool(item.get("manufacturer_flag"))),
            -float(item.get("contact_quality_score") or 0.0),
            str(item.get("legal_name") or "").lower(),
        )
    )
    for index, item in enumerate(enriched, start=1):
        item["qualification_rank"] = index
    return enriched


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        return value
    return None


def _derive_unit_value(
    quantity_total: Any,
    total_value: Any,
    unit_value: Any = None,
) -> tuple[float | None, str | None]:
    explicit_unit_value = _coerce_float(unit_value)
    if explicit_unit_value is not None:
        return explicit_unit_value, "explicit"
    quantity = _coerce_float(quantity_total)
    total = _coerce_float(total_value)
    if quantity is not None and quantity > 0 and total is not None:
        return round(total / quantity, 6), "derived_from_total"
    return None, None


def _normalize_lookup_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("ç", "c").replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
    text = text.replace("é", "e").replace("ê", "e").replace("í", "i").replace("ó", "o").replace("ô", "o").replace("õ", "o")
    text = text.replace("ú", "u").replace("ü", "u")
    return text


def _product_sales_guide(name: str) -> dict[str, Any]:
    normalized = _normalize_lookup_text(name)
    for key, guide in PRODUCT_SALES_GUIDES.items():
        if key in normalized:
            return {"product_family": guide["family"], **guide}
    if "amido" in normalized or "polvilho" in normalized or "sagu" in normalized or "fecula" in normalized:
        guide = PRODUCT_SALES_GUIDES["sagu"]
        return {"product_family": guide["family"], **guide}
    if "bicarbonato" in normalized:
        guide = PRODUCT_SALES_GUIDES["bicarbonato de sodio"]
        return {"product_family": guide["family"], **guide}
    return {
        "product_family": "geral",
        "mandatory_specs": [
            "grau do produto",
            "pureza / composição",
            "embalagem",
            "quantidade mínima",
            "certificações",
            "país de origem",
        ],
        "market_differentiators": [
            "rastreamento de lote e documentação clara",
            "disponibilidade consistente",
            "capacidade de atender volumes recorrentes",
        ],
        "transactional_angles": [
            "reduz incerteza para o comprador",
            "melhora a velocidade de cotação",
        ],
        "buyer_questions": [
            "Qual a especificação mínima aceitável?",
            "Qual a embalagem e o volume por pedido?",
        ],
    }


def _default_secom_payload_from_embassy(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "country": record.get("country"),
        "office_name": record.get("embassy") or record.get("office_name") or "SECOM / Brazilian representation",
        "city": record.get("city"),
        "language": record.get("language") or "en",
        "email_primary": record.get("email_primary"),
        "email_alternatives": json.dumps(record.get("email_alternatives") or [], ensure_ascii=False),
        "phone": record.get("phone"),
        "website": record.get("website"),
        "commercial_sector": record.get("commercial_sector") or "Commercial Section",
        "product_focus": json.dumps(record.get("product_focus") or [], ensure_ascii=False),
        "active": 1 if record.get("active", True) else 0,
        "followup_interval_days": int(record.get("campaign_cycle_days_min") or 45),
        "report_interval_days": int(record.get("campaign_cycle_days_max") or 45),
        "last_request_at": None,
        "last_report_at": None,
        "source": record.get("source") or "hermes_sales",
        "notes": record.get("status"),
    }


class ComprasDB:
    def __init__(self, db_path: Path | None = None, read_only: bool = False):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.read_only = read_only
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.read_only:
            self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            return
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ComprasDB connection not initialized")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA_SQL)
            row = self.conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                self.conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            self._seed_default_catalogs()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, seq_of_params)

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[ComprasRow]:
        cursor = self.conn.execute(sql, tuple(params))
        return [ComprasRow(dict(row)) for row in cursor.fetchall()]

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[ComprasRow]:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return ComprasRow(dict(row)) if row is not None else None

    def table_exists(self, table_name: str) -> bool:
        row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
        return row is not None

    def list_tables(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [str(row[0]) for row in rows]

    def insert_audit_log(
        self,
        *,
        action: str,
        actor: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload_json: str | None = None,
        error_message: str | None = None,
        created_at: float,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO audit_logs (actor, action, entity_type, entity_id, payload_json, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (actor, action, entity_type, entity_id, payload_json, error_message, created_at),
        )
        return int(cursor.lastrowid)

    def create_user_decision_log(
        self,
        *,
        decision_context: str,
        decision: str,
        decided_by: str | None,
        decided_at: float,
        rfq_batch_id: int | None = None,
        supplier_id: int | None = None,
        product_id: int | None = None,
        quote_id: int | None = None,
        recommendation_id: int | None = None,
        decision_label: str | None = None,
        decision_source: str | None = None,
        notes: str | None = None,
        payload_json: str | None = None,
        related_entity_type: str | None = None,
        related_entity_id: str | None = None,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO user_decision_logs (
                decision_context, related_entity_type, related_entity_id,
                rfq_batch_id, supplier_id, product_id, quote_id, recommendation_id,
                decision, decision_label, decided_by, decided_at, decision_source,
                notes, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_context, related_entity_type, related_entity_id,
                rfq_batch_id, supplier_id, product_id, quote_id, recommendation_id,
                decision, decision_label, decided_by, decided_at, decision_source,
                notes, payload_json, decided_at,
            ),
        )
        return int(cursor.lastrowid)

    def insert_rfq_batch(
        self,
        *,
        batch_code: str,
        product_id: int | None,
        requested_by: str | None,
        status: str = "draft",
        user_authorized: bool = False,
        authorized_by: str | None = None,
        authorized_at: float | None = None,
        authorization_source: str | None = None,
        created_at: float,
        updated_at: float,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO rfq_batches (
                batch_code, product_id, requested_by, status, user_authorized,
                authorized_by, authorized_at, authorization_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (batch_code, product_id, requested_by, status, int(user_authorized), authorized_by, authorized_at, authorization_source, created_at, updated_at),
        )
        return int(cursor.lastrowid)

    def get_rfq_batch(self, rfq_batch_id: int) -> Optional[ComprasRow]:
        return self.fetchone("SELECT * FROM rfq_batches WHERE id = ?", (rfq_batch_id,))

    def list_rfq_batches(self, limit: int = 25, offset: int = 0) -> list[ComprasRow]:
        """Return recent RFQ batches with lightweight activity counters."""
        return self.fetchall(
            """
            SELECT
                b.*,
                (SELECT name FROM products p WHERE p.id = b.product_id) AS product_name,
                COALESCE((SELECT COUNT(*) FROM rfq_candidates c WHERE c.rfq_batch_id = b.id), 0) AS candidate_count,
                COALESCE((SELECT COUNT(*) FROM rfq_recipients r WHERE r.rfq_batch_id = b.id), 0) AS recipient_count,
                COALESCE((SELECT COUNT(*) FROM supplier_quotes q WHERE q.rfq_batch_id = b.id), 0) AS quote_count,
                COALESCE((SELECT COUNT(*) FROM rfq_inbound_emails e WHERE e.rfq_batch_id = b.id), 0) AS inbound_count,
                COALESCE((SELECT COUNT(*) FROM rfq_email_logs l WHERE l.rfq_batch_id = b.id), 0) AS email_log_count
            FROM rfq_batches b
            ORDER BY b.updated_at DESC, b.created_at DESC, b.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )

    def insert_product(
        self,
        *,
        name: str,
        created_at: float,
        updated_at: float,
        description: str | None = None,
        technical_specs: str | None = None,
        application: str | None = None,
        unit: str | None = None,
        ncm: str | None = None,
        hs_code: str | None = None,
        packaging: str | None = None,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO products (name, description, technical_specs, application, unit, ncm, hs_code, packaging, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, description, technical_specs, application, unit, ncm, hs_code, packaging, created_at, updated_at),
        )
        return int(cursor.lastrowid)

    def upsert_product(
        self,
        *,
        name: str,
        created_at: float,
        updated_at: float,
        description: str | None = None,
        technical_specs: str | None = None,
        application: str | None = None,
        unit: str | None = None,
        ncm: str | None = None,
        hs_code: str | None = None,
        packaging: str | None = None,
    ) -> int:
        existing = self.fetchone("SELECT * FROM products WHERE lower(name) = lower(?) ORDER BY id DESC LIMIT 1", (name,))
        if existing is not None:
            product_id = int(existing.data["id"])
            self.execute(
                """
                UPDATE products
                SET description = ?, technical_specs = ?, application = ?, unit = ?,
                    ncm = ?, hs_code = ?, packaging = ?, updated_at = ?
                WHERE id = ?
                """,
                (description, technical_specs, application, unit, ncm, hs_code, packaging, updated_at, product_id),
            )
            return product_id
        return self.insert_product(
            name=name,
            description=description,
            technical_specs=technical_specs,
            application=application,
            unit=unit,
            ncm=ncm,
            hs_code=hs_code,
            packaging=packaging,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_product_by_id(self, product_id: int) -> Optional[ComprasRow]:
        return self.fetchone("SELECT * FROM products WHERE id = ?", (product_id,))

    def get_product_by_name(self, name: str) -> Optional[ComprasRow]:
        name = name.strip()
        if not name:
            return None
        return self.fetchone("SELECT * FROM products WHERE lower(name) = lower(?) ORDER BY id DESC LIMIT 1", (name,))

    def list_products(self, search: str | None = None, limit: int = 100, offset: int = 0) -> list[ComprasRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            like = f"%{search.strip()}%"
            clauses.append("(name LIKE ? OR description LIKE ? OR application LIKE ? OR packaging LIKE ?)")
            params.extend([like, like, like, like])
        sql = "SELECT * FROM products"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.fetchall(sql, params)
        enriched: list[ComprasRow] = []
        for row in rows:
            data = dict(row.data)
            data["sales_brief"] = self.build_product_sales_brief(data)
            enriched.append(ComprasRow(data))
        return enriched

    def build_product_sales_brief(self, product: dict[str, Any] | str) -> dict[str, Any]:
        name = product if isinstance(product, str) else str(product.get("name") or "")
        guide = _product_sales_guide(name)
        if isinstance(product, dict):
            guide = {
                **guide,
                "product_name": name,
                "packaging": product.get("packaging"),
                "unit": product.get("unit"),
                "application": product.get("application"),
                "technical_specs": product.get("technical_specs"),
            }
        return guide

    def upsert_secom_office(
        self,
        *,
        country: str,
        office_name: str,
        created_at: float,
        updated_at: float,
        city: str | None = None,
        language: str | None = None,
        email_primary: str | None = None,
        email_alternatives: list[str] | str | None = None,
        phone: str | None = None,
        website: str | None = None,
        commercial_sector: str | None = None,
        product_focus: list[str] | str | None = None,
        active: bool = True,
        followup_interval_days: int = 45,
        report_interval_days: int = 45,
        last_request_at: float | None = None,
        last_report_at: float | None = None,
        source: str | None = None,
        notes: str | None = None,
    ) -> int:
        alt_text = email_alternatives if isinstance(email_alternatives, str) else json.dumps(email_alternatives or [], ensure_ascii=False)
        focus_text = product_focus if isinstance(product_focus, str) else json.dumps(product_focus or [], ensure_ascii=False)
        existing = self.fetchone(
            "SELECT * FROM secom_offices WHERE lower(country) = lower(?) AND lower(office_name) = lower(?) AND COALESCE(city, '') = COALESCE(?, '') ORDER BY id DESC LIMIT 1",
            (country, office_name, city),
        )
        if existing is not None:
            office_id = int(existing.data["id"])
            self.execute(
                """
                UPDATE secom_offices
                SET language = ?, email_primary = ?, email_alternatives = ?, phone = ?,
                    website = ?, commercial_sector = ?, product_focus = ?, active = ?,
                    followup_interval_days = ?, report_interval_days = ?, last_request_at = ?,
                    last_report_at = ?, source = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    language, email_primary, alt_text, phone, website, commercial_sector, focus_text,
                    int(bool(active)), followup_interval_days, report_interval_days, last_request_at,
                    last_report_at, source, notes, updated_at, office_id,
                ),
            )
            return office_id
        cursor = self.execute(
            """
            INSERT INTO secom_offices (
                country, office_name, city, language, email_primary, email_alternatives,
                phone, website, commercial_sector, product_focus, active,
                followup_interval_days, report_interval_days, last_request_at,
                last_report_at, source, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                country, office_name, city, language, email_primary, alt_text,
                phone, website, commercial_sector, focus_text, int(bool(active)),
                followup_interval_days, report_interval_days, last_request_at,
                last_report_at, source, notes, created_at, updated_at,
            ),
        )
        return int(cursor.lastrowid)

    def list_secom_offices(self, active_only: bool = True) -> list[ComprasRow]:
        sql = "SELECT * FROM secom_offices"
        params: list[Any] = []
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY country, office_name, id"
        rows = self.fetchall(sql, params)
        enriched: list[ComprasRow] = []
        for row in rows:
            data = dict(row.data)
            for key in ("email_alternatives", "product_focus"):
                raw = data.get(key)
                if isinstance(raw, str) and raw.strip():
                    try:
                        data[key] = json.loads(raw)
                    except Exception:
                        data[key] = raw
            enriched.append(ComprasRow(data))
        return enriched

    def list_secom_countries(self, active_only: bool = True) -> list[str]:
        sql = "SELECT DISTINCT country FROM secom_offices"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY country"
        rows = self.fetchall(sql)
        return [str(row.data.get("country") or "").strip() for row in rows if str(row.data.get("country") or "").strip()]

    def recommend_secom_offices_for_product(
        self,
        *,
        product_name: str,
        destination_country: str | None = None,
        limit: int = 8,
    ) -> list[ComprasRow]:
        offices = [row.data for row in self.list_secom_offices(active_only=True)]
        if not offices:
            return []
        guide = _product_sales_guide(product_name)
        family = str(guide.get("product_family") or "geral")
        country_hints = [country.casefold() for country in SECOM_PRODUCT_REGION_HINTS.get(family, [])]
        dest_norm = _normalize_lookup_text(destination_country) if destination_country else ""

        scored: list[tuple[float, dict[str, Any]]] = []
        for office in offices:
            score = 0.0
            country = str(office.get("country") or "")
            office_name = str(office.get("office_name") or "")
            text = f"{country} {office_name} {office.get('city') or ''} {office.get('commercial_sector') or ''}".casefold()
            if dest_norm and dest_norm in _normalize_lookup_text(country):
                score += 100.0
            if country.casefold() in country_hints:
                score += 20.0
            for hint in country_hints:
                if hint and hint in text:
                    score += 4.0
            if office.get("last_report_at"):
                score -= 0.1
            if office.get("active"):
                score += 1.0
            scored.append((score, office))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("country") or ""), str(item[1].get("office_name") or "")))
        return [ComprasRow(dict(office, recommendation_score=score)) for score, office in scored[:limit]]

    def _seed_default_catalogs(self) -> None:
        if self.read_only:
            return
        now = time.time()
        product_count = int(self.fetchone("SELECT COUNT(*) AS c FROM products").data.get("c") or 0)
        if product_count == 0:
            for product in DEFAULT_PRODUCT_REGISTRY:
                self.upsert_product(
                    name=str(product["name"]),
                    description=product.get("description"),
                    technical_specs=product.get("technical_specs"),
                    application=product.get("application"),
                    unit=product.get("unit"),
                    packaging=product.get("packaging"),
                    created_at=now,
                    updated_at=now,
                )
        secom_count = int(self.fetchone("SELECT COUNT(*) AS c FROM secom_offices").data.get("c") or 0)
        if secom_count == 0:
            self._seed_secom_from_sales_registry(now=now)

    def _seed_secom_from_sales_registry(self, *, now: float | None = None) -> None:
        if not SALES_EMBASSIES_JSONL.exists():
            return
        created_at = now if now is not None else time.time()
        try:
            for line in SALES_EMBASSIES_JSONL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                country = str(record.get("country") or "").strip()
                office_name = str(record.get("embassy") or record.get("office_name") or "").strip()
                if not country or not office_name:
                    continue
                self.upsert_secom_office(
                    country=country,
                    office_name=office_name,
                    city=str(record.get("city") or "").strip() or None,
                    language=str(record.get("language") or "").strip() or None,
                    email_primary=str(record.get("email_primary") or "").strip() or None,
                    email_alternatives=record.get("email_alternatives") or [],
                    phone=str(record.get("phone") or "").strip() or None,
                    website=str(record.get("website") or "").strip() or None,
                    commercial_sector=str(record.get("commercial_sector") or "").strip() or None,
                    product_focus=record.get("product_focus") or [],
                    active=bool(record.get("active", True)),
                    followup_interval_days=int(record.get("campaign_cycle_days_min") or 45),
                    report_interval_days=int(record.get("campaign_cycle_days_max") or 45),
                    last_request_at=None,
                    last_report_at=None,
                    source=str(record.get("source") or "hermes_sales").strip() or "hermes_sales",
                    notes=str(record.get("status") or "").strip() or None,
                    created_at=created_at,
                    updated_at=created_at,
                )
        except Exception:
            pass

    def _find_existing_supplier_id(self, candidate: dict[str, Any]) -> Optional[int]:
        legal_name = str(candidate.get("legal_name") or candidate.get("name") or candidate.get("company") or "").strip()
        source_url = candidate.get("source_url") or None
        website = candidate.get("website") or None
        emails = [candidate.get("sales_email"), candidate.get("general_email")]
        clauses = []
        params: list[Any] = []
        if source_url:
            clauses.append("source_url = ?")
            params.append(source_url)
        if website:
            clauses.append("website = ?")
            params.append(website)
        for email in emails:
            if email:
                clauses.append("general_email = ? OR sales_email = ?")
                params.extend([email, email])
        if legal_name:
            clauses.append("legal_name = ?")
            params.append(legal_name)
        if not clauses:
            return None
        sql = "SELECT id FROM suppliers WHERE " + " OR ".join(f"({c})" for c in clauses) + " ORDER BY id LIMIT 1"
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row is not None else None

    def insert_supplier_candidate(
        self,
        *,
        legal_name: str,
        created_at: float,
        updated_at: float,
        trade_name: str | None = None,
        supplier_type: str = "unknown",
        country: str | None = None,
        city: str | None = None,
        website: str | None = None,
        source_url: str | None = None,
        source_name: str | None = None,
        source_type: str | None = None,
        manufacturer_flag: bool = False,
        trading_company_flag: bool = False,
        verified_status: str = "unverified",
        data_quality_status: str = "pending_validation",
        notes: str | None = None,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO suppliers (
                legal_name, trade_name, supplier_type, country, city, website,
                source_url, source_name, source_type, manufacturer_flag,
                trading_company_flag, verified_status, data_quality_status,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (legal_name, trade_name, supplier_type, country, city, website, source_url, source_name, source_type, int(manufacturer_flag), int(trading_company_flag), verified_status, data_quality_status, notes, created_at, updated_at),
        )
        return int(cursor.lastrowid)

    def upsert_supplier_contact(
        self,
        *,
        supplier_id: int,
        name: str | None = None,
        role: str | None = None,
        department: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        whatsapp: str | None = None,
        wechat: str | None = None,
        linkedin: str | None = None,
        language: str | None = None,
        is_primary: bool = False,
        source: str | None = None,
        source_email_id: str | None = None,
        source_attachment_id: str | None = None,
        verified_status: str = "unverified",
        created_at: float,
        updated_at: float,
    ) -> int:
        existing = None
        if email:
            existing = self.conn.execute(
                "SELECT id FROM supplier_contacts WHERE supplier_id = ? AND lower(COALESCE(email, '')) = lower(?) ORDER BY id DESC LIMIT 1",
                (supplier_id, email),
            ).fetchone()
        elif name:
            existing = self.conn.execute(
                "SELECT id FROM supplier_contacts WHERE supplier_id = ? AND lower(COALESCE(name, '')) = lower(?) ORDER BY id DESC LIMIT 1",
                (supplier_id, name),
            ).fetchone()
        payload = (
            supplier_id, name, role, department, email, phone, whatsapp, wechat, linkedin,
            language, int(bool(is_primary)), source, source_email_id, source_attachment_id,
            verified_status, created_at, updated_at,
        )
        if existing is not None:
            contact_id = int(existing[0])
            self.execute(
                """
                UPDATE supplier_contacts
                SET name = ?, role = ?, department = ?, email = ?, phone = ?, whatsapp = ?,
                    wechat = ?, linkedin = ?, language = ?, is_primary = ?, source = ?,
                    source_email_id = ?, source_attachment_id = ?, verified_status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name, role, department, email, phone, whatsapp, wechat, linkedin,
                    language, int(bool(is_primary)), source, source_email_id,
                    source_attachment_id, verified_status, updated_at, contact_id,
                ),
            )
        else:
            cursor = self.execute(
                """
                INSERT INTO supplier_contacts (
                    supplier_id, name, role, department, email, phone, whatsapp,
                    wechat, linkedin, language, is_primary, source, source_email_id,
                    source_attachment_id, verified_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            contact_id = int(cursor.lastrowid)
        self.insert_audit_log(
            action="supplier_contact_upserted",
            entity_type="supplier_contact",
            entity_id=str(contact_id),
            payload_json=json.dumps({
                "supplier_id": supplier_id,
                "name": name,
                "email": email,
                "phone": phone,
                "is_primary": bool(is_primary),
            }, ensure_ascii=False),
            created_at=updated_at,
        )
        return contact_id

    def store_rfq_candidates(
        self,
        rfq_batch_id: int,
        candidates: list[dict[str, Any]],
        *,
        created_at: float,
        updated_at: float,
    ) -> list[int]:
        candidate_ids: list[int] = []
        ranked_candidates = _rank_qualified_candidates(list(candidates))
        for raw in ranked_candidates:
            legal_name = str(raw.get("legal_name") or raw.get("name") or raw.get("company") or "").strip()
            if not legal_name:
                continue
            source_url = raw.get("source_url") or None
            website = raw.get("website") or None
            existing = self.conn.execute(
                "SELECT id FROM rfq_candidates WHERE rfq_batch_id = ? AND legal_name = ? AND COALESCE(source_url, '') = COALESCE(?, '') AND COALESCE(website, '') = COALESCE(?, '') ORDER BY id DESC LIMIT 1",
                (rfq_batch_id, legal_name, source_url, website),
            ).fetchone()
            payload_json = json.dumps(raw, ensure_ascii=False)
            if existing is not None:
                candidate_id = int(existing[0])
                self.execute(
                    """
                    UPDATE rfq_candidates
                    SET supplier_id = ?, trade_name = ?, country = ?, city = ?, website = ?,
                        source_url = ?, source_name = ?, source_type = ?, manufacturer_flag = ?,
                        trading_company_flag = ?, verified_status = ?, data_quality_status = ?,
                        selected_by_user = ?, approved_by_user = ?, rejected_by_user = ?,
                        candidate_payload_json = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        raw.get("supplier_id"), raw.get("trade_name"), raw.get("country"), raw.get("city"), website, source_url, raw.get("source_name"), raw.get("source_type"), int(bool(raw.get("manufacturer_flag"))), int(bool(raw.get("trading_company_flag"))), str(raw.get("verified_status") or "unverified"), str(raw.get("data_quality_status") or "pending_validation"), int(bool(raw.get("selected_by_user"))), int(bool(raw.get("approved_by_user"))), int(bool(raw.get("rejected_by_user"))), payload_json, raw.get("notes"), updated_at, candidate_id,
                    ),
                )
            else:
                cursor = self.execute(
                    """
                    INSERT INTO rfq_candidates (
                        rfq_batch_id, supplier_id, legal_name, trade_name, country, city,
                        website, source_url, source_name, source_type, manufacturer_flag,
                        trading_company_flag, verified_status, data_quality_status,
                        selected_by_user, approved_by_user, rejected_by_user,
                        candidate_payload_json, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (rfq_batch_id, raw.get("supplier_id"), legal_name, raw.get("trade_name"), raw.get("country"), raw.get("city"), website, source_url, raw.get("source_name"), raw.get("source_type"), int(bool(raw.get("manufacturer_flag"))), int(bool(raw.get("trading_company_flag"))), str(raw.get("verified_status") or "unverified"), str(raw.get("data_quality_status") or "pending_validation"), int(bool(raw.get("selected_by_user"))), int(bool(raw.get("approved_by_user"))), int(bool(raw.get("rejected_by_user"))), payload_json, raw.get("notes"), created_at, updated_at),
                )
                candidate_id = int(cursor.lastrowid)
            candidate_ids.append(candidate_id)
        return candidate_ids

    def list_rfq_candidates(self, rfq_batch_id: int) -> list[ComprasRow]:
        return self.fetchall("SELECT * FROM rfq_candidates WHERE rfq_batch_id = ? ORDER BY id", (rfq_batch_id,))

    def reject_rfq_suppliers(
        self,
        *,
        rfq_batch_id: int,
        rejected_supplier_candidates: list[dict[str, Any]],
        rejected_by: str | None,
        rejection_notes: str | None = None,
        created_at: float,
        updated_at: float,
    ) -> list[int]:
        rejected_ids: list[int] = []
        for candidate in rejected_supplier_candidates:
            legal_name = str(candidate.get("legal_name") or candidate.get("name") or candidate.get("company") or "").strip()
            if not legal_name:
                continue
            row = self.conn.execute(
                "SELECT id FROM rfq_candidates WHERE rfq_batch_id = ? AND legal_name = ? ORDER BY id DESC LIMIT 1",
                (rfq_batch_id, legal_name),
            ).fetchone()
            if row is None:
                continue
            candidate_id = int(row[0])
            rejected_ids.append(candidate_id)
            self.execute(
                "UPDATE rfq_candidates SET rejected_by_user = 1, approved_by_user = 0, selected_by_user = 0, notes = COALESCE(?, notes), updated_at = ? WHERE id = ?",
                (rejection_notes, updated_at, candidate_id),
            )
            self.create_user_decision_log(
                decision_context="rfq_supplier_rejection",
                decision="rejected",
                decided_by=rejected_by,
                decided_at=created_at,
                rfq_batch_id=rfq_batch_id,
                decision_label=legal_name,
                decision_source="api_request",
                notes=rejection_notes,
                payload_json=json.dumps(candidate, ensure_ascii=False),
                related_entity_type="rfq_candidate",
                related_entity_id=str(candidate_id),
            )
            self.insert_audit_log(
                action="rfq_supplier_rejected_by_user",
                actor=rejected_by,
                entity_type="rfq_candidate",
                entity_id=str(candidate_id),
                payload_json=json.dumps(candidate, ensure_ascii=False),
                created_at=created_at,
            )
        return rejected_ids

    def create_email_authorization_log(
        self,
        *,
        rfq_batch_id: int,
        supplier_id: int,
        contact_id: int | None,
        recipient_id: int,
        subject: str,
        body_snapshot: str,
        signature_snapshot: str,
        authorized_by: str | None,
        dry_run: bool,
        created_at: float,
        next_followup_due_at: float | None = None,
    ) -> int:
        status = "dry_run_simulated" if dry_run else "pending"
        response_status = "not_required" if dry_run else "awaiting_response"
        cursor = self.execute(
            """
            INSERT INTO rfq_email_logs (
                rfq_batch_id, recipient_id, supplier_id, contact_id,
                subject, body_snapshot, signature_snapshot, provider,
                status, response_status, sent_at, created_at, updated_at,
                next_followup_due_at, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rfq_batch_id, recipient_id, supplier_id, contact_id, subject, body_snapshot, signature_snapshot, authorized_by, status, response_status, None, created_at, created_at, next_followup_due_at, None),
        )
        return int(cursor.lastrowid)

    def record_rfq_inbound_email(
        self,
        *,
        rfq_batch_id: int | None,
        supplier_id: int | None,
        contact_id: int | None,
        message_id: str | None,
        in_reply_to: str | None,
        email_references: str | None,
        from_email: str | None,
        from_name: str | None,
        to_email: str | None,
        cc: str | None,
        subject: str | None,
        received_at: float,
        body_text: str | None = None,
        body_html: str | None = None,
        body_summary: str | None = None,
        detected_language: str | None = None,
        has_attachments: bool = False,
        attachment_count: int = 0,
        raw_payload_path: str | None = None,
        processing_status: str = "received",
        extraction_status: str = "pending",
        linked_outbound_email_log_id: int | None = None,
        provider_thread_id: str | None = None,
        email_thread_id: str | None = None,
        correlation_token: str | None = None,
        matched_by: str | None = None,
        matching_confidence: float | None = None,
        is_direct_reply: bool = False,
        is_followup_reply: bool = False,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> int:
        now = updated_at if updated_at is not None else received_at
        created = created_at if created_at is not None else now
        existing = None
        if message_id:
            existing = self.conn.execute(
                "SELECT id FROM rfq_inbound_emails WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                (message_id,),
            ).fetchone()
        elif correlation_token:
            existing = self.conn.execute(
                "SELECT id FROM rfq_inbound_emails WHERE correlation_token = ? ORDER BY id DESC LIMIT 1",
                (correlation_token,),
            ).fetchone()

        payload = (
            rfq_batch_id, supplier_id, contact_id, message_id, in_reply_to, email_references,
            from_email, from_name, to_email, cc, subject, received_at, body_text, body_html,
            body_summary, detected_language, int(bool(has_attachments)), attachment_count,
            raw_payload_path, processing_status, extraction_status, linked_outbound_email_log_id,
            provider_thread_id, email_thread_id, correlation_token, matched_by,
            matching_confidence, int(bool(is_direct_reply)), int(bool(is_followup_reply)),
            created, now,
        )
        if existing is not None:
            inbound_id = int(existing[0])
            self.execute(
                """
                UPDATE rfq_inbound_emails
                SET rfq_batch_id = ?, supplier_id = ?, contact_id = ?, message_id = ?,
                    in_reply_to = ?, email_references = ?, from_email = ?, from_name = ?,
                    to_email = ?, cc = ?, subject = ?, received_at = ?, body_text = ?,
                    body_html = ?, body_summary = ?, detected_language = ?,
                    has_attachments = ?, attachment_count = ?, raw_payload_path = ?,
                    processing_status = ?, extraction_status = ?,
                    linked_outbound_email_log_id = ?, provider_thread_id = ?,
                    email_thread_id = ?, correlation_token = ?, matched_by = ?,
                    matching_confidence = ?, is_direct_reply = ?, is_followup_reply = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    rfq_batch_id, supplier_id, contact_id, message_id, in_reply_to, email_references,
                    from_email, from_name, to_email, cc, subject, received_at, body_text, body_html,
                    body_summary, detected_language, int(bool(has_attachments)), attachment_count,
                    raw_payload_path, processing_status, extraction_status, linked_outbound_email_log_id,
                    provider_thread_id, email_thread_id, correlation_token, matched_by,
                    matching_confidence, int(bool(is_direct_reply)), int(bool(is_followup_reply)),
                    now, inbound_id,
                ),
            )
        else:
            cursor = self.execute(
                """
                INSERT INTO rfq_inbound_emails (
                    rfq_batch_id, supplier_id, contact_id, message_id, in_reply_to,
                    email_references, from_email, from_name, to_email, cc, subject,
                    received_at, body_text, body_html, body_summary, detected_language,
                    has_attachments, attachment_count, raw_payload_path,
                    processing_status, extraction_status, linked_outbound_email_log_id,
                    provider_thread_id, email_thread_id, correlation_token, matched_by,
                    matching_confidence, is_direct_reply, is_followup_reply, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            inbound_id = int(cursor.lastrowid)
        self.insert_audit_log(
            action="rfq_inbound_email_recorded",
            entity_type="rfq_inbound_email",
            entity_id=str(inbound_id),
            payload_json=json.dumps({
                "message_id": message_id,
                "from_email": from_email,
                "subject": subject,
                "rfq_batch_id": rfq_batch_id,
                "supplier_id": supplier_id,
            }, ensure_ascii=False),
            created_at=now,
        )
        return inbound_id

    def upsert_supplier_quote(
        self,
        *,
        rfq_batch_id: int | None,
        supplier_id: int | None,
        contact_id: int | None = None,
        product_id: int | None = None,
        source_type: str = "manual_entry",
        source_email_id: int | None = None,
        source_attachment_id: int | None = None,
        source_text_reference: str | None = None,
        confidence_score: float | None = None,
        requires_user_review: bool = False,
        currency: str | None = None,
        unit_price: float | None = None,
        quantity: float | None = None,
        unit: str | None = None,
        moq: float | None = None,
        incoterm: str | None = None,
        origin_port: str | None = None,
        origin_airport: str | None = None,
        destination_port: str | None = None,
        destination_airport: str | None = None,
        lead_time_days: int | None = None,
        production_time_days: int | None = None,
        validity_date: str | None = None,
        payment_terms: str | None = None,
        packaging: str | None = None,
        technical_specs: str | None = None,
        raw_response: str | None = None,
        status: str = "draft",
        created_at: float,
        updated_at: float,
    ) -> int:
        existing = None
        if rfq_batch_id is not None and supplier_id is not None:
            existing = self.conn.execute(
                """
                SELECT id FROM supplier_quotes
                WHERE rfq_batch_id = ? AND supplier_id = ? AND COALESCE(product_id, -1) = COALESCE(?, -1)
                  AND COALESCE(source_email_id, -1) = COALESCE(?, -1)
                  AND COALESCE(source_attachment_id, -1) = COALESCE(?, -1)
                ORDER BY id DESC LIMIT 1
                """,
                (rfq_batch_id, supplier_id, product_id, source_email_id, source_attachment_id),
            ).fetchone()
        payload = (
            rfq_batch_id, supplier_id, contact_id, product_id, source_type, source_email_id,
            source_attachment_id, source_text_reference, confidence_score, int(bool(requires_user_review)),
            currency, unit_price, quantity, unit, moq, incoterm, origin_port, origin_airport,
            destination_port, destination_airport, lead_time_days, production_time_days,
            validity_date, payment_terms, packaging, technical_specs, raw_response, status,
            created_at, updated_at,
        )
        if existing is not None:
            quote_id = int(existing[0])
            self.execute(
                """
                UPDATE supplier_quotes
                SET contact_id = ?, product_id = ?, source_type = ?, source_email_id = ?,
                    source_attachment_id = ?, source_text_reference = ?, confidence_score = ?,
                    requires_user_review = ?, currency = ?, unit_price = ?, quantity = ?,
                    unit = ?, moq = ?, incoterm = ?, origin_port = ?, origin_airport = ?,
                    destination_port = ?, destination_airport = ?, lead_time_days = ?,
                    production_time_days = ?, validity_date = ?, payment_terms = ?,
                    packaging = ?, technical_specs = ?, raw_response = ?, status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    contact_id, product_id, source_type, source_email_id,
                    source_attachment_id, source_text_reference, confidence_score,
                    int(bool(requires_user_review)), currency, unit_price, quantity,
                    unit, moq, incoterm, origin_port, origin_airport,
                    destination_port, destination_airport, lead_time_days,
                    production_time_days, validity_date, payment_terms, packaging,
                    technical_specs, raw_response, status, updated_at, quote_id,
                ),
            )
        else:
            cursor = self.execute(
                """
                INSERT INTO supplier_quotes (
                    rfq_batch_id, supplier_id, contact_id, product_id, source_type,
                    source_email_id, source_attachment_id, source_text_reference,
                    confidence_score, requires_user_review, currency, unit_price,
                    quantity, unit, moq, incoterm, origin_port, origin_airport,
                    destination_port, destination_airport, lead_time_days,
                    production_time_days, validity_date, payment_terms, packaging,
                    technical_specs, raw_response, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            quote_id = int(cursor.lastrowid)
        self.insert_audit_log(
            action="supplier_quote_recorded",
            entity_type="supplier_quote",
            entity_id=str(quote_id),
            payload_json=json.dumps({
                "rfq_batch_id": rfq_batch_id,
                "supplier_id": supplier_id,
                "source_type": source_type,
                "status": status,
            }, ensure_ascii=False),
            created_at=updated_at,
        )
        return quote_id

    def list_supplier_quotes(self, rfq_batch_id: int | None = None, supplier_id: int | None = None) -> list[ComprasRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if rfq_batch_id is not None:
            clauses.append("rfq_batch_id = ?")
            params.append(rfq_batch_id)
        if supplier_id is not None:
            clauses.append("supplier_id = ?")
            params.append(supplier_id)
        sql = "SELECT * FROM supplier_quotes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        return self.fetchall(sql, params)

    def upsert_profit_margin_rule(
        self,
        *,
        margin_value: float,
        margin_type: str = "percentage",
        product_id: int | None = None,
        product_category: str | None = None,
        supplier_id: int | None = None,
        customer_id: str | None = None,
        incoterm: str | None = None,
        currency: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        is_active: bool = True,
        created_by: str | None = None,
        created_at: float,
        updated_at: float,
    ) -> int:
        cursor = self.execute(
            """
            INSERT INTO profit_margin_rules (
                product_id, product_category, supplier_id, customer_id, incoterm,
                margin_type, margin_value, currency, valid_from, valid_until,
                is_active, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id, product_category, supplier_id, customer_id, incoterm,
                margin_type, margin_value, currency, valid_from, valid_until,
                int(bool(is_active)), created_by, created_at, updated_at,
            ),
        )
        return int(cursor.lastrowid)

    def resolve_profit_margin_rule(
        self,
        *,
        product_id: int | None = None,
        supplier_id: int | None = None,
        incoterm: str | None = None,
        currency: str | None = None,
    ) -> Optional[ComprasRow]:
        clauses = ["is_active = 1"]
        params: list[Any] = []
        if product_id is not None:
            clauses.append("(product_id IS NULL OR product_id = ?)")
            params.append(product_id)
        if supplier_id is not None:
            clauses.append("(supplier_id IS NULL OR supplier_id = ?)")
            params.append(supplier_id)
        if incoterm:
            clauses.append("(incoterm IS NULL OR lower(incoterm) = lower(?))")
            params.append(incoterm)
        if currency:
            clauses.append("(currency IS NULL OR lower(currency) = lower(?))")
            params.append(currency)
        row = self.fetchone(
            f"""
            SELECT * FROM profit_margin_rules
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE WHEN product_id IS NOT NULL THEN 0 ELSE 1 END,
                CASE WHEN supplier_id IS NOT NULL THEN 0 ELSE 1 END,
                CASE WHEN incoterm IS NOT NULL THEN 0 ELSE 1 END,
                CASE WHEN currency IS NOT NULL THEN 0 ELSE 1 END,
                id DESC
            LIMIT 1
            """,
            params,
        )
        return row

    def calculate_sale_price(
        self,
        *,
        rfq_batch_id: int | None = None,
        supplier_id: int | None = None,
        product_id: int | None = None,
        quote_id: int | None = None,
        margin_rule_id: int | None = None,
        source_incoterm: str | None = None,
        sale_incoterm: str | None = None,
        purchase_currency: str | None = None,
        sale_currency: str | None = None,
        purchase_unit_price: float | None = None,
        quantity: float | None = None,
        international_freight: float | None = None,
        insurance: float | None = None,
        origin_charges: float | None = None,
        destination_charges: float | None = None,
        customs_clearance_cost: float | None = None,
        import_duties_estimated: float | None = None,
        taxes_estimated: float | None = None,
        inland_freight: float | None = None,
        warehouse_cost: float | None = None,
        financial_cost: float | None = None,
        other_costs: float | None = None,
        margin_type: str | None = None,
        margin_value: float | None = None,
        requires_user_approval: bool = True,
        approved_by: str | None = None,
        approved_at: float | None = None,
        created_at: float,
        updated_at: float,
    ) -> int:
        quote_row = self.fetchone("SELECT * FROM supplier_quotes WHERE id = ?", (quote_id,)) if quote_id is not None else None
        if quote_row is not None:
            quote = quote_row.data
            supplier_id = _first_non_empty(supplier_id, quote.get("supplier_id"))
            product_id = _first_non_empty(product_id, quote.get("product_id"))
            purchase_currency = _first_non_empty(purchase_currency, quote.get("currency"))
            purchase_unit_price = _first_non_empty(purchase_unit_price, quote.get("unit_price"))
            quantity = _first_non_empty(quantity, quote.get("quantity"), 1)
            source_incoterm = _first_non_empty(source_incoterm, quote.get("incoterm"))
        quantity_value = _coerce_float(quantity) or 1.0
        purchase_unit_value = _coerce_float(purchase_unit_price) or 0.0
        international_freight_value = _coerce_float(international_freight) or 0.0
        insurance_value = _coerce_float(insurance) or 0.0
        origin_charges_value = _coerce_float(origin_charges) or 0.0
        destination_charges_value = _coerce_float(destination_charges) or 0.0
        customs_clearance_value = _coerce_float(customs_clearance_cost) or 0.0
        duties_value = _coerce_float(import_duties_estimated) or 0.0
        taxes_value = _coerce_float(taxes_estimated) or 0.0
        inland_value = _coerce_float(inland_freight) or 0.0
        warehouse_value = _coerce_float(warehouse_cost) or 0.0
        financial_value = _coerce_float(financial_cost) or 0.0
        other_value = _coerce_float(other_costs) or 0.0
        total_landed_cost = sum((
            purchase_unit_value,
            international_freight_value,
            insurance_value,
            origin_charges_value,
            destination_charges_value,
            customs_clearance_value,
            duties_value,
            taxes_value,
            inland_value,
            warehouse_value,
            financial_value,
            other_value,
        ))

        rule = None
        if margin_rule_id is not None:
            rule = self.fetchone("SELECT * FROM profit_margin_rules WHERE id = ?", (margin_rule_id,))
        if rule is None:
            rule = self.resolve_profit_margin_rule(
                product_id=_coerce_int(product_id),
                supplier_id=_coerce_int(supplier_id),
                incoterm=source_incoterm,
                currency=purchase_currency,
            )
        if rule is not None:
            margin_rule_id = int(rule.data["id"])
            margin_type = margin_type or str(rule.data.get("margin_type") or "percentage")
            margin_value = _coerce_float(margin_value) if margin_value is not None else _coerce_float(rule.data.get("margin_value"))
        margin_type_normalized = str(margin_type or "percentage").strip().lower()
        margin_value_value = _coerce_float(margin_value) or 0.0
        if margin_type_normalized in {"percentage", "margin", "gross_margin"}:
            if margin_value_value > 1:
                margin_value_value = margin_value_value / 100.0
            denominator = max(1e-6, 1.0 - margin_value_value)
            sale_unit_price = total_landed_cost / denominator
            margin_amount = sale_unit_price - total_landed_cost
        else:
            sale_unit_price = total_landed_cost + margin_value_value
            margin_amount = margin_value_value
            margin_type_normalized = "fixed_amount"
        sale_total_price = sale_unit_price * quantity_value

        cursor = self.execute(
            """
            INSERT INTO sale_price_calculations (
                rfq_batch_id, supplier_id, product_id, quote_id, margin_rule_id,
                source_incoterm, sale_incoterm, purchase_currency, sale_currency,
                purchase_unit_price, international_freight, insurance, origin_charges,
                destination_charges, customs_clearance_cost, import_duties_estimated,
                taxes_estimated, inland_freight, warehouse_cost, financial_cost,
                other_costs, total_landed_cost, margin_type, margin_value,
                margin_amount, sale_unit_price, sale_total_price, calculation_status,
                requires_user_approval, approved_by, approved_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rfq_batch_id, supplier_id, product_id, quote_id, margin_rule_id,
                source_incoterm, sale_incoterm, purchase_currency, sale_currency or purchase_currency,
                purchase_unit_value, international_freight_value, insurance_value, origin_charges_value,
                destination_charges_value, customs_clearance_value, duties_value, taxes_value,
                inland_value, warehouse_value, financial_value, other_value, total_landed_cost,
                margin_type_normalized, margin_value_value, margin_amount, sale_unit_price,
                sale_total_price, "approved" if not requires_user_approval else "draft",
                int(bool(requires_user_approval)), approved_by, approved_at,
                created_at, updated_at,
            ),
        )
        calculation_id = int(cursor.lastrowid)
        self.insert_audit_log(
            action="sale_price_calculated",
            entity_type="sale_price_calculation",
            entity_id=str(calculation_id),
            payload_json=json.dumps({
                "rfq_batch_id": rfq_batch_id,
                "supplier_id": supplier_id,
                "product_id": product_id,
                "quote_id": quote_id,
                "margin_type": margin_type_normalized,
                "margin_value": margin_value_value,
                "total_landed_cost": total_landed_cost,
                "sale_unit_price": sale_unit_price,
            }, ensure_ascii=False),
            created_at=created_at,
        )
        return calculation_id

    def build_proposal_snapshot(
        self,
        *,
        rfq_batch_id: int,
        quote_id: int | None = None,
        sale_price_calculation_id: int | None = None,
        created_at: float,
    ) -> dict[str, Any]:
        batch = self.get_rfq_batch(rfq_batch_id)
        if batch is None:
            raise ValueError(f"RFQ batch not found: {rfq_batch_id}")
        quote = self.fetchone("SELECT * FROM supplier_quotes WHERE id = ?", (quote_id,)) if quote_id is not None else None
        pricing = self.fetchone("SELECT * FROM sale_price_calculations WHERE id = ?", (sale_price_calculation_id,)) if sale_price_calculation_id is not None else None
        supplier = self.fetchone("SELECT * FROM suppliers WHERE id = ?", (quote.data.get("supplier_id"),)) if quote is not None and quote.data.get("supplier_id") is not None else None
        contact = self.fetchone("SELECT * FROM supplier_contacts WHERE id = ?", (quote.data.get("contact_id"),)) if quote is not None and quote.data.get("contact_id") is not None else None

        proposal = {
            "rfq_batch": batch.data,
            "quote": quote.data if quote is not None else None,
            "pricing": pricing.data if pricing is not None else None,
            "supplier": supplier.data if supplier is not None else None,
            "contact": contact.data if contact is not None else None,
            "proposal_text": None,
        }
        if quote is not None:
            unit_price = quote.data.get("unit_price")
            currency = quote.data.get("currency") or (pricing.data.get("purchase_currency") if pricing is not None else None)
            incoterm = quote.data.get("incoterm")
            supplier_name = (supplier.data.get("trade_name") if supplier is not None else None) or (supplier.data.get("legal_name") if supplier is not None else None) or str(quote.data.get("supplier_id") or "supplier")
            total_text = ""
            if pricing is not None:
                total_text = f" Margem aplicada: {pricing.data.get('margin_type')} {pricing.data.get('margin_value')}."
            proposal["proposal_text"] = (
                f"Proposal for {supplier_name}: {unit_price} {currency or ''} {incoterm or ''}."
                f"{total_text}"
            ).strip()
        self.insert_audit_log(
            action="proposal_snapshot_built",
            entity_type="rfq_batch",
            entity_id=str(rfq_batch_id),
            payload_json=json.dumps({
                "quote_id": quote_id,
                "sale_price_calculation_id": sale_price_calculation_id,
                "has_quote": quote is not None,
                "has_pricing": pricing is not None,
            }, ensure_ascii=False),
            created_at=created_at,
        )
        return proposal

    def approve_rfq_suppliers(
        self,
        *,
        rfq_batch_id: int,
        approved_supplier_candidates: list[dict[str, Any]],
        rejected_supplier_candidates: list[dict[str, Any]] | None,
        approved_by: str | None,
        approval_notes: str | None = None,
        authorize_email_send: bool = False,
        dry_run: bool = True,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = created_at if created_at is not None else time.time()
        batch = self.get_rfq_batch(rfq_batch_id)
        if batch is None:
            raise ValueError(f"RFQ batch not found: {rfq_batch_id}")

        if rejected_supplier_candidates:
            self.reject_rfq_suppliers(
                rfq_batch_id=rfq_batch_id,
                rejected_supplier_candidates=rejected_supplier_candidates,
                rejected_by=approved_by,
                rejection_notes=approval_notes,
                created_at=now,
                updated_at=now,
            )

        approved_ids: list[int] = []
        recipient_ids: list[int] = []
        email_log_ids: list[int] = []
        audit_log_ids: list[int] = []
        decision_log_ids: list[int] = []
        created_supplier_ids: list[int] = []

        for candidate in approved_supplier_candidates:
            legal_name = str(candidate.get("legal_name") or candidate.get("name") or candidate.get("company") or "").strip()
            if not legal_name:
                continue
            existing_supplier_id = candidate.get("supplier_id") or self._find_existing_supplier_id(candidate)
            if existing_supplier_id is None:
                supplier_id = self.insert_supplier_candidate(
                    legal_name=legal_name,
                    trade_name=candidate.get("trade_name"),
                    supplier_type=str(candidate.get("supplier_type") or "unknown"),
                    country=candidate.get("country"),
                    city=candidate.get("city"),
                    website=candidate.get("website"),
                    source_url=candidate.get("source_url"),
                    source_name=candidate.get("source_name"),
                    source_type=candidate.get("source_type"),
                    manufacturer_flag=bool(candidate.get("manufacturer_flag")),
                    trading_company_flag=bool(candidate.get("trading_company_flag")),
                    verified_status=str(candidate.get("verified_status") or "unverified"),
                    data_quality_status=str(candidate.get("data_quality_status") or "pending_validation"),
                    notes=approval_notes or candidate.get("notes"),
                    created_at=now,
                    updated_at=now,
                )
                created_supplier_ids.append(supplier_id)
            else:
                supplier_id = int(existing_supplier_id)
            approved_ids.append(supplier_id)
            contact_id = candidate.get("contact_id")
            candidate_id = None
            candidate_row = self.conn.execute(
                "SELECT id FROM rfq_candidates WHERE rfq_batch_id = ? AND legal_name = ? ORDER BY id DESC LIMIT 1",
                (rfq_batch_id, legal_name),
            ).fetchone()
            if candidate_row is not None:
                candidate_id = int(candidate_row[0])
                self.execute(
                    "UPDATE rfq_candidates SET approved_by_user = 1, selected_by_user = 1, rejected_by_user = 0, supplier_id = ?, notes = COALESCE(?, notes), updated_at = ? WHERE id = ?",
                    (supplier_id, approval_notes, now, candidate_id),
                )
            recipient_row = self.conn.execute(
                "SELECT id FROM rfq_recipients WHERE rfq_batch_id = ? AND supplier_id = ? AND COALESCE(contact_id, -1) = COALESCE(?, -1) LIMIT 1",
                (rfq_batch_id, supplier_id, contact_id),
            ).fetchone()
            if recipient_row is None:
                recipient_cursor = self.execute(
                    """
                    INSERT INTO rfq_recipients (
                        rfq_batch_id, supplier_id, contact_id, selected_by_user,
                        approved_by_user, user_authorized_email_sending,
                        email_sending_authorized_by, email_sending_authorized_at,
                        email_automation_scope, send_status, sent_at, error_message,
                        followup_7_days_enabled, followup_7_days_authorized_by,
                        followup_7_days_authorized_at, followup_7_days_sent,
                        followup_7_days_sent_at, followup_7_days_email_log_id,
                        followup_count, max_followups_allowed, next_followup_due_at,
                        followup_status, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?, 0, NULL, NULL, 0, 1, ?, ?, ?, ?)
                    """,
                    (
                        rfq_batch_id,
                        supplier_id,
                        contact_id,
                        int(authorize_email_send),
                        approved_by,
                        now if authorize_email_send else None,
                        "this_rfq_only",
                        "authorized_pending_send" if authorize_email_send else "manual_review_required",
                        approved_by if authorize_email_send else None,
                        now if authorize_email_send else None,
                        now + 7 * 24 * 3600 if authorize_email_send else None,
                        "scheduled" if authorize_email_send else "not_scheduled",
                        now,
                        now,
                    ),
                )
                recipient_id = int(recipient_cursor.lastrowid)
            else:
                recipient_id = int(recipient_row[0])
                self.execute(
                    """
                    UPDATE rfq_recipients
                    SET selected_by_user = 1, approved_by_user = 1,
                        user_authorized_email_sending = ?, email_sending_authorized_by = ?,
                        email_sending_authorized_at = ?, email_automation_scope = ?,
                        send_status = ?, followup_7_days_enabled = 1,
                        followup_7_days_authorized_by = ?, followup_7_days_authorized_at = ?,
                        next_followup_due_at = ?, followup_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(authorize_email_send),
                        approved_by,
                        now if authorize_email_send else None,
                        "this_rfq_only",
                        "authorized_pending_send" if authorize_email_send else "manual_review_required",
                        approved_by if authorize_email_send else None,
                        now if authorize_email_send else None,
                        now + 7 * 24 * 3600 if authorize_email_send else None,
                        "scheduled" if authorize_email_send else "not_scheduled",
                        now,
                        recipient_id,
                    ),
                )
            recipient_ids.append(recipient_id)

            batch_code = batch.data.get("batch_code") if batch else str(rfq_batch_id)
            if authorize_email_send:
                subject = "RFQ approval: " + str(batch_code)
                body_snapshot = approval_notes or json.dumps(candidate, ensure_ascii=False)
                signature_snapshot = candidate.get("signature_snapshot") or "Polar Sinergy LLC\nAluizio Andreatta"
                email_log_id = self.create_email_authorization_log(
                    rfq_batch_id=rfq_batch_id,
                    supplier_id=supplier_id,
                    contact_id=contact_id,
                    recipient_id=recipient_id,
                    subject=subject,
                    body_snapshot=body_snapshot,
                    signature_snapshot=signature_snapshot,
                    authorized_by=approved_by,
                    dry_run=dry_run,
                    created_at=now,
                    next_followup_due_at=now + 7 * 24 * 3600,
                )
                email_log_ids.append(email_log_id)
                self.execute("UPDATE rfq_recipients SET followup_7_days_email_log_id = ?, updated_at = ? WHERE id = ?", (email_log_id, now, recipient_id))

            decision_id = self.create_user_decision_log(
                decision_context="rfq_supplier_approval",
                decision="approved",
                decided_by=approved_by,
                decided_at=now,
                rfq_batch_id=rfq_batch_id,
                supplier_id=supplier_id,
                decision_label=legal_name,
                decision_source="api_request",
                notes=approval_notes,
                payload_json=json.dumps(candidate, ensure_ascii=False),
                related_entity_type="supplier" if existing_supplier_id is not None else "rfq_candidate",
                related_entity_id=str(supplier_id if existing_supplier_id is not None else candidate_id or supplier_id),
            )
            audit_id = self.insert_audit_log(
                action="rfq_supplier_email_authorization_granted" if authorize_email_send else "approved_without_email_authorization",
                actor=approved_by,
                entity_type="supplier" if existing_supplier_id is not None else "rfq_candidate",
                entity_id=str(supplier_id if existing_supplier_id is not None else candidate_id or supplier_id),
                payload_json=json.dumps({
                    "rfq_batch_id": rfq_batch_id,
                    "authorize_email_send": authorize_email_send,
                    "dry_run": dry_run,
                    "candidate": candidate,
                }, ensure_ascii=False),
                created_at=now,
            )
            decision_log_ids.append(decision_id)
            audit_log_ids.append(audit_id)

        if approved_ids or rejected_supplier_candidates:
            self.execute(
                "UPDATE rfq_batches SET status = ?, user_authorized = ?, authorized_by = ?, authorized_at = ?, authorization_source = ? WHERE id = ?",
                (
                    "authorized" if authorize_email_send else "approved_without_email",
                    int(authorize_email_send),
                    approved_by,
                    now,
                    "api_request",
                    rfq_batch_id,
                ),
            )

        return {
            "rfq_batch_id": rfq_batch_id,
            "approved_count": len(approved_ids),
            "rejected_count": len(rejected_supplier_candidates or []),
            "email_authorized": bool(authorize_email_send),
            "dry_run": bool(dry_run),
            "next_action": "send_pending_email_logs" if authorize_email_send else "no_email_send",
            "audit_log_ids": audit_log_ids,
            "decision_log_ids": decision_log_ids,
            "email_log_ids": email_log_ids,
            "created_supplier_ids": created_supplier_ids,
            "recipient_ids": recipient_ids,
        }

    def insert_recipients(self, rows: list[tuple[Any, ...]]) -> None:
        self.executemany(
            """
            INSERT INTO rfq_recipients (
                rfq_batch_id, supplier_id, contact_id, selected_by_user,
                approved_by_user, user_authorized_email_sending,
                email_sending_authorized_by, email_sending_authorized_at,
                email_automation_scope, send_status, sent_at, error_message,
                followup_7_days_enabled, followup_7_days_authorized_by,
                followup_7_days_authorized_at, followup_7_days_sent,
                followup_7_days_sent_at, followup_7_days_email_log_id,
                followup_count, max_followups_allowed, next_followup_due_at,
                followup_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def __enter__(self) -> "ComprasDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def get_compras_db_path() -> Path:
    return DEFAULT_DB_PATH


def open_compras_db(db_path: Path | None = None, read_only: bool = False) -> ComprasDB:
    return ComprasDB(db_path=db_path, read_only=read_only)
