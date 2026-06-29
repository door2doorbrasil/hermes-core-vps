#!/usr/bin/env python3
"""Shared utilities for Hermes Mail supplier-reply analysis and reporting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

def _resolve_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    opt_root = Path('/opt/data/hermes-mail')
    if local_root.exists():
        return local_root
    if opt_root.exists():
        return opt_root
    return local_root


ROOT = _resolve_root()
CREDENTIALS_ENV_PATH = Path('/opt/data/.env')
STATE_DIR = ROOT / 'state'
LOGS_DIR = ROOT / 'logs'
SCRIPTS_DIR = ROOT / 'scripts'
ASSETS_DIR = ROOT / 'assets'
CONFIG_DIR = ROOT / 'config'
CLIENT_QUOTES_DIR = ROOT / 'client-quotes'
COMPARISON_REPORTS_DIR = ROOT / 'reports' / 'comparisons'
SOURCING_PROJECTS_DIR = ROOT / 'sourcing-projects'
RFQ_DRAFTS_DIR = ROOT / 'rfq-drafts'
RFQ_DRAFTS_JSONL = ROOT / 'rfq-drafts.jsonl'
OPEN_WEBUI_REQUESTS_JSONL = ROOT / 'open-webui-requests.jsonl'
OPEN_WEBUI_ACTIONS_JSONL = ROOT / 'open-webui-actions.jsonl'
MANUFACTURER_DISCOVERY_JSONL = ROOT / 'manufacturer-discovery.jsonl'
PRODUCT_INTELLIGENCE_JSONL = ROOT / 'product-intelligence.jsonl'
PRODUCT_CATEGORIES_JSONL = ROOT / 'product-categories.jsonl'
PRODUCT_COMPLIANCE_RULES_JSONL = ROOT / 'product-compliance-rules.jsonl'
PRODUCT_SOURCING_SOURCES_JSONL = ROOT / 'product-sourcing-sources.jsonl'
PROCUREMENT_KB_JSONL = ROOT / 'procurement-knowledge-base.jsonl'
SOURCING_REPORTS_DIR = ROOT / 'reports' / 'sourcing'
ADAPTIVE_DISCOVERY_LOG = LOGS_DIR / 'adaptive-discovery.log'
ADAPTIVE_DISCOVERY_REPORT_JSON = SOURCING_REPORTS_DIR / 'adaptive-discovery-report.json'

EMAILS_JSONL = ROOT / 'emails.jsonl'
FORNECEDORES_JSONL = ROOT / 'fornecedores.jsonl'
CONTATOS_JSONL = ROOT / 'contatos.jsonl'
PRODUTOS_JSONL = ROOT / 'produtos.jsonl'
COTACOES_JSONL = ROOT / 'cotacoes.jsonl'
SUPPLIER_REPLY_ANALYSIS_JSONL = ROOT / 'supplier-reply-analysis.jsonl'
CLIENT_QUOTES_JSONL = ROOT / 'client-quotes.jsonl'
COMPARISON_REPORTS_JSONL = ROOT / 'comparison-reports.jsonl'
SOURCING_PROJECTS_JSONL = ROOT / 'sourcing-projects.jsonl'
MANUFACTURER_RESEARCH_JSONL = ROOT / 'manufacturer-research.jsonl'
MANUFACTURER_QUALIFICATION_JSONL = ROOT / 'manufacturer-qualification.jsonl'
SUPPLIER_PERFORMANCE_JSONL = ROOT / 'supplier-performance.jsonl'

BRAND_PROFILE_PATH = CONFIG_DIR / 'brand_profile.json'
SALES_BRAND_PROFILE_PATH = CONFIG_DIR / 'd2d_brand_profile.json'

DEFAULT_BRAND_PROFILE: dict[str, Any] = {
    'company_name': 'Polar Sinergy LLC',
    'buyer_name': 'Aluizio Andreatta',
    'email': 'buyer@polarsinergy.com',
    'phone': '+1 321 948 9126',
    'whatsapp': '+55 44 99156 9673',
    'website': 'https://www.polarsinergy.com',
    'default_language': 'en',
    'communication_tone': 'professional, technical, objective',
    'signature_plain': 'Polar Sinergy LLC\nAluizio Andreatta\nE-mail: buyer@polarsinergy.com\nPhone: +1 321 948 9126\nWhatsApp: +55 44 99156 9673\n\nWebsite: https://www.polarsinergy.com',
}

DEFAULT_SALES_BRAND_PROFILE: dict[str, Any] = {
    'company_name': 'D2D Representação Comercial Ltda',
    'location': 'Maringá - Paraná - Brasil',
    'buyer_name': 'Aluizio Andreatta',
    'role': 'Vendas Internacionais',
    'email': 'aluizio@door2doorbrasil.com.br',
    'phone': '+55 44 99156-9673',
    'whatsapp': '+55 44 99156-9673',
    'website': 'https://www.door2doorbrasil.com.br',
    'default_language': 'pt-BR',
    'communication_tone': 'professional, commercial, objective',
    'signature_plain': 'D2D Representação Comercial Ltda\nMaringá - Paraná - Brasil\n\nAluizio Andreatta\nVendas Internacionais\nE-mail: aluizio@door2doorbrasil.com.br\nPhone/WhatsApp: +55 44 99156-9673',
}


def load_brand_profile() -> dict[str, Any]:
    profile = dict(DEFAULT_BRAND_PROFILE)
    if BRAND_PROFILE_PATH.exists():
        try:
            loaded = load_json(BRAND_PROFILE_PATH)
            if isinstance(loaded, dict):
                profile.update(loaded)
        except Exception:
            pass
    return profile


def load_sales_brand_profile() -> dict[str, Any]:
    profile = dict(DEFAULT_SALES_BRAND_PROFILE)
    if SALES_BRAND_PROFILE_PATH.exists():
        try:
            loaded = load_json(SALES_BRAND_PROFILE_PATH)
            if isinstance(loaded, dict):
                profile.update(loaded)
        except Exception:
            pass
    return profile


def brand_line(value: str | None, fallback: str) -> str:
    return normalize_text(value or '') or fallback


def brand_signature_lines() -> list[str]:
    profile = load_brand_profile()
    return [
        brand_line(profile.get('company_name'), DEFAULT_BRAND_PROFILE['company_name']),
        brand_line(profile.get('buyer_name'), DEFAULT_BRAND_PROFILE['buyer_name']),
        f"E-mail: {brand_line(profile.get('email'), DEFAULT_BRAND_PROFILE['email'])}",
        f"Phone: {brand_line(profile.get('phone'), DEFAULT_BRAND_PROFILE['phone'])}",
        f"WhatsApp: {brand_line(profile.get('whatsapp'), DEFAULT_BRAND_PROFILE['whatsapp'])}",
        '',
        f"Website: {brand_line(profile.get('website'), DEFAULT_BRAND_PROFILE['website'])}",
    ]


def sales_brand_signature_lines() -> list[str]:
    profile = load_sales_brand_profile()
    return [
        brand_line(profile.get('company_name'), DEFAULT_SALES_BRAND_PROFILE['company_name']),
        brand_line(profile.get('location'), DEFAULT_SALES_BRAND_PROFILE['location']),
        '',
        brand_line(profile.get('buyer_name'), DEFAULT_SALES_BRAND_PROFILE['buyer_name']),
        brand_line(profile.get('role'), DEFAULT_SALES_BRAND_PROFILE['role']),
        f"E-mail: {brand_line(profile.get('email'), DEFAULT_SALES_BRAND_PROFILE['email'])}",
        f"Phone/WhatsApp: {brand_line(profile.get('phone'), DEFAULT_SALES_BRAND_PROFILE['phone'])}",
    ]


POLAR_SINERGY = {
    'name': load_brand_profile().get('company_name', DEFAULT_BRAND_PROFILE['company_name']),
    'legal_name': load_brand_profile().get('company_name', DEFAULT_BRAND_PROFILE['company_name']),
    'buyer_name': load_brand_profile().get('buyer_name', DEFAULT_BRAND_PROFILE['buyer_name']),
    'contact_email': load_brand_profile().get('email', DEFAULT_BRAND_PROFILE['email']),
    'phone': load_brand_profile().get('phone', DEFAULT_BRAND_PROFILE['phone']),
    'whatsapp': load_brand_profile().get('whatsapp', DEFAULT_BRAND_PROFILE['whatsapp']),
    'website': load_brand_profile().get('website', DEFAULT_BRAND_PROFILE['website']),
    'default_language': load_brand_profile().get('default_language', DEFAULT_BRAND_PROFILE['default_language']),
    'communication_tone': load_brand_profile().get('communication_tone', DEFAULT_BRAND_PROFILE['communication_tone']),
    'signature_plain': load_brand_profile().get('signature_plain', DEFAULT_BRAND_PROFILE['signature_plain']),
    'tagline': 'Comércio exterior e soluções industriais',
    'footer': 'Polar Sinergy LLC • Documento gerado em modo dry_run interno',
}

D2D_REPRESENTACAO = {
    'name': load_sales_brand_profile().get('company_name', DEFAULT_SALES_BRAND_PROFILE['company_name']),
    'legal_name': load_sales_brand_profile().get('company_name', DEFAULT_SALES_BRAND_PROFILE['company_name']),
    'buyer_name': load_sales_brand_profile().get('buyer_name', DEFAULT_SALES_BRAND_PROFILE['buyer_name']),
    'contact_email': load_sales_brand_profile().get('email', DEFAULT_SALES_BRAND_PROFILE['email']),
    'phone': load_sales_brand_profile().get('phone', DEFAULT_SALES_BRAND_PROFILE['phone']),
    'whatsapp': load_sales_brand_profile().get('whatsapp', DEFAULT_SALES_BRAND_PROFILE['whatsapp']),
    'website': load_sales_brand_profile().get('website', DEFAULT_SALES_BRAND_PROFILE['website']),
    'default_language': load_sales_brand_profile().get('default_language', DEFAULT_SALES_BRAND_PROFILE['default_language']),
    'communication_tone': load_sales_brand_profile().get('communication_tone', DEFAULT_SALES_BRAND_PROFILE['communication_tone']),
    'signature_plain': load_sales_brand_profile().get('signature_plain', DEFAULT_SALES_BRAND_PROFILE['signature_plain']),
    'footer': 'D2D Representação Comercial Ltda • Documento gerado em modo dry_run interno',
}

REQUIRED_DIRS = [
    ROOT,
    STATE_DIR,
    LOGS_DIR,
    ASSETS_DIR,
    CONFIG_DIR,
    CLIENT_QUOTES_DIR,
    COMPARISON_REPORTS_DIR,
    SOURCING_PROJECTS_DIR,
    RFQ_DRAFTS_DIR,
    SOURCING_REPORTS_DIR,
    ROOT / 'emails',
    ROOT / 'emails' / 'incoming',
    ROOT / 'emails' / 'outgoing',
    ROOT / 'emails' / 'raw',
    ROOT / 'attachments',
    ROOT / 'attachments' / 'original',
    ROOT / 'attachments' / 'extracted-text',
    ROOT / 'attachments' / 'ocr',
    ROOT / 'fornecedores',
    ROOT / 'contatos',
    ROOT / 'produtos',
    ROOT / 'cotacoes',
    ROOT / 'price-history',
    ROOT / 'reports',
    SOURCING_PROJECTS_DIR,
    RFQ_DRAFTS_DIR,
    SOURCING_REPORTS_DIR,
]

JSONL_FILES = [
    ROOT / 'fornecedores.jsonl',
    ROOT / 'contatos.jsonl',
    ROOT / 'produtos.jsonl',
    EMAILS_JSONL,
    COTACOES_JSONL,
    ROOT / 'price-history.jsonl',
    ROOT / 'anexos.jsonl',
    SUPPLIER_REPLY_ANALYSIS_JSONL,
    CLIENT_QUOTES_JSONL,
    COMPARISON_REPORTS_JSONL,
    SOURCING_PROJECTS_JSONL,
    MANUFACTURER_RESEARCH_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    SUPPLIER_PERFORMANCE_JSONL,
    RFQ_DRAFTS_JSONL,
    MANUFACTURER_DISCOVERY_JSONL,
]



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def ensure_runtime_dirs() -> None:
    for path in REQUIRED_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return path


def append_jsonl(path: Path, record: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return path


def _load_credentials_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not CREDENTIALS_ENV_PATH.exists():
        return values
    try:
        raw = CREDENTIALS_ENV_PATH.read_text(encoding='utf-8')
    except Exception:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key, value = stripped.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def get_env_secret(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    env_values = _load_credentials_env()
    for name in names:
        value = env_values.get(name)
        if value:
            os.environ.setdefault(name, value)
            return value
    return None


def count_jsonl(path: Path) -> int:
    total = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    total += 1
    return total


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open('r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
    return records


def latest_jsonl_record(path: Path, predicate: Any | None = None) -> dict[str, Any] | None:
    last = None
    for record in load_jsonl_records(path):
        if predicate is None or predicate(record):
            last = record
    return last


def make_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"


def control_number(prefix: str, *, timestamp: datetime | None = None) -> str:
    ts = (timestamp or datetime.now(timezone.utc)).strftime('%Y%m%d-%H%M%S')
    return f'{prefix}-{ts}-{uuid.uuid4().hex[:4].upper()}'


def slugify(value: str, max_length: int = 80) -> str:
    text = re.sub(r'[^A-Za-z0-9]+', '-', value.strip().lower()).strip('-')
    return text[:max_length] or 'documento'


def normalize_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


def round_money(value: float) -> float:
    return round(float(value) + 1e-12, 2)


def margin_policy(merchant_value_usd: float) -> dict[str, Any]:
    amount = float(merchant_value_usd)
    if amount < 10000:
        nominal_percent = 0.0
        nominal_margin = amount * nominal_percent / 100.0
        margin_value = max(2000.0, nominal_margin)
        rule = 'minimo_usd_2000'
    elif amount <= 20000:
        nominal_percent = 10.0
        nominal_margin = amount * nominal_percent / 100.0
        margin_value = max(2000.0, nominal_margin)
        rule = '10_percent_minimo_usd_2000'
    else:
        nominal_percent = 15.0
        nominal_margin = amount * nominal_percent / 100.0
        margin_value = max(3500.0, nominal_margin)
        rule = '15_percent_minimo_usd_3500'
    margin_value = round_money(margin_value)
    effective_percent = 0.0 if amount <= 0 else round_money((margin_value / amount) * 100.0)
    total = round_money(amount + margin_value)
    return {
        'margem_regra': rule,
        'margem_percentual': effective_percent,
        'margem_percentual_nominal': nominal_percent,
        'margem_valor': margin_value,
        'valor_total_com_margem': total,
    }


def line_group_signature(record: dict[str, Any]) -> str:
    parts = [
        normalize_text(str(record.get('product_name') or record.get('product') or '')),
        normalize_text(str(record.get('description') or '')),
        normalize_text(str(record.get('incoterm') or '')),
        normalize_text(str(record.get('specifications') or '')),
    ]
    digest = hashlib.sha1('||'.join(parts).encode('utf-8')).hexdigest()
    return digest[:12]


def normalize_pdf_text(text: str) -> str:
    return (
        text.replace('•', '-')
        .replace('↔', '<->')
        .replace('→', '->')
        .replace('—', '-')
        .replace('–', '-')
        .replace('…', '...')
        .replace('\\', '\\\\')
        .replace('(', '\\(')
        .replace(')', '\\)')
        .replace('\r', '')
    )


def wrap_text(text: str, width: int) -> list[str]:
    words = normalize_text(text).split(' ')
    if not words:
        return ['']
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f'{current} {word}'
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def extract_first(patterns: Iterable[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def parse_number(text: str) -> float | None:
    normalized = text.strip()
    match = re.search(r'(-?[0-9][0-9.,]*)', normalized)
    if not match:
        return None
    value = match.group(1)
    if ',' in value and '.' in value:
        if value.rfind(',') > value.rfind('.'):
            value = value.replace('.', '').replace(',', '.')
        else:
            value = value.replace(',', '')
    elif ',' in value:
        value = value.replace('.', '').replace(',', '.')
    try:
        return float(value)
    except ValueError:
        return None


def heuristically_extract_supplier_reply(body_text: str) -> dict[str, Any]:
    text = body_text or ''
    extracted = {
        'supplier_price_usd': None,
        'incoterm': None,
        'lead_time_days': None,
        'moq': None,
        'payment_terms': None,
        'warranty': None,
        'specifications': None,
        'country': None,
        'description': None,
        'translated_reply_ptbr': None,
    }
    patterns = {
        'supplier_price_usd': [r'(?im)^\s*(?:price|purchase price|unit price|valor|preco|preço)[:\-]\s*USD?\s*([0-9][0-9.,]*)', r'(?im)\bUSD\s*([0-9][0-9.,]*)'],
        'incoterm': [r'(?im)^\s*incoterm[:\-]\s*([^\n\r]+)', r'(?im)\b(FOB|CIF|CFR|EXW|DAP|DDP)\b'],
        'lead_time_days': [r'(?im)^\s*(?:lead time|prazo|prazo de entrega)[:\-]\s*([0-9]+)', r'(?im)\b([0-9]+)\s*(?:days|dias)\b'],
        'moq': [r'(?im)^\s*(?:moq|min(?:imum)? qty|quantidade mínima)[:\-]\s*([0-9]+)', r'(?im)\bMOQ\s*([0-9]+)\b'],
        'payment_terms': [r'(?im)^\s*(?:payment terms?|condições de pagamento)[:\-]\s*(.+)$'],
        'warranty': [r'(?im)^\s*warranty[:\-]\s*(.+)$', r'(?im)^\s*garantia[:\-]\s*(.+)$'],
        'specifications': [r'(?im)^\s*(?:specifications?|especifica(?:ç|c)ões?)[:\-]\s*(.+)$'],
        'country': [r'(?im)^\s*country[:\-]\s*(.+)$', r'(?im)^\s*pa[ií]s[:\-]\s*(.+)$'],
        'description': [r'(?im)^\s*(?:product|produto|description|descri(?:ç|c)ão)[:\-]\s*(.+)$'],
    }
    for key, pats in patterns.items():
        value = extract_first(pats, text)
        if value is not None:
            extracted[key] = value
    if extracted['supplier_price_usd'] is not None:
        extracted['supplier_price_usd'] = round_money(parse_number(str(extracted['supplier_price_usd'])) or 0.0)
    if extracted['lead_time_days'] is not None:
        extracted['lead_time_days'] = int(parse_number(str(extracted['lead_time_days'])) or 0)
    if extracted['moq'] is not None:
        extracted['moq'] = int(parse_number(str(extracted['moq'])) or 0)
    extracted['translated_reply_ptbr'] = build_ptbr_summary_from_extracted(extracted, text)
    return extracted


def build_ptbr_summary_from_extracted(extracted: dict[str, Any], raw_text: str) -> str:
    parts = []
    if extracted.get('description'):
        parts.append(f"Produto: {extracted['description']}")
    if extracted.get('supplier_price_usd') is not None:
        parts.append(f"Preço de compra: USD {float(extracted['supplier_price_usd']):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'))
    if extracted.get('incoterm'):
        parts.append(f"Incoterm: {extracted['incoterm']}")
    if extracted.get('lead_time_days') is not None:
        parts.append(f"Prazo: {extracted['lead_time_days']} dias")
    if extracted.get('moq') is not None:
        parts.append(f"MOQ: {extracted['moq']}")
    if extracted.get('payment_terms'):
        parts.append(f"Pagamento: {extracted['payment_terms']}")
    if extracted.get('warranty'):
        parts.append(f"Garantia: {extracted['warranty']}")
    if extracted.get('specifications'):
        parts.append(f"Especificações: {extracted['specifications']}")
    if not parts:
        return normalize_text(raw_text)
    return ' | '.join(parts)


def classify_missing_fields(extracted: dict[str, Any]) -> list[str]:
    missing = []
    if not extracted.get('supplier_price_usd'):
        missing.append('price')
    if not extracted.get('incoterm'):
        missing.append('incoterm')
    if not extracted.get('description'):
        missing.append('description')
    return missing


def openai_json_completion(*, system_prompt: str, user_prompt: str, model: str | None = None, temperature: float = 0.0) -> dict[str, Any] | None:
    api_key = get_env_secret('OPENAI_API_KEY', 'VOICE_TOOLS_OPENAI_KEY')
    if not api_key:
        return None
    model_name = model or os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
    payload = {
        'model': model_name,
        'temperature': temperature,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
    }
    request = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode('utf-8'))
    content = body['choices'][0]['message']['content']
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return None


def company_display_lines() -> list[str]:
    return [
        POLAR_SINERGY['name'],
        POLAR_SINERGY['buyer_name'],
        POLAR_SINERGY['contact_email'],
    ]


def vendor_display_name(value: str | None, fallback: str = 'Fornecedor não informado') -> str:
    text = normalize_text(value or '')
    return text or fallback


def compute_family_label(name: str, description: str) -> str:
    name = normalize_text(name)
    description = normalize_text(description)
    parts = [name[:50], description[:50]]
    return ' / '.join(part for part in parts if part) or 'Produto sem identificação'


def compare_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    a_sig = ' '.join(filter(None, [normalize_text(str(a.get('product_name') or '')), normalize_text(str(a.get('description') or '')), normalize_text(str(a.get('specifications') or ''))]))
    b_sig = ' '.join(filter(None, [normalize_text(str(b.get('product_name') or '')), normalize_text(str(b.get('description') or '')), normalize_text(str(b.get('specifications') or ''))]))
    if not a_sig or not b_sig:
        return 0.0
    a_tokens = set(re.findall(r'[\w\u00C0-\u017F]+', a_sig.lower()))
    b_tokens = set(re.findall(r'[\w\u00C0-\u017F]+', b_sig.lower()))
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def format_usd(value: float | None) -> str:
    if value is None:
        return '-'
    return f"USD {float(value):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


def format_percent(value: float | None) -> str:
    if value is None:
        return '-'
    return f'{float(value):.2f}%'


def short_source_label(record: dict[str, Any]) -> str:
    bits = []
    for key in ('id', 'email_id', 'rfq_email_id', 'supplier_reply_email_id'):
        if record.get(key):
            bits.append(f'{key}={record[key]}')
    return '; '.join(bits) if bits else 'source=unknown'
