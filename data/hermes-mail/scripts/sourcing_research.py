#!/usr/bin/env python3
"""Module 3 — pesquisa mundial e qualificação inteligente de fabricantes.

Dry-run only. The script can:
- create a sourcing project from a Telegram-style product request,
- search public web sources for manufacturer candidates,
- enrich candidates with optional OpenAI extraction,
- score and classify each company,
- generate an audit-friendly report and PDF,
- approve only qualified manufacturers for RFQ,
- generate RFQ drafts,
- record supplier performance updates.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdf_writer import PdfDocument
from reporting_utils import (
    ASSETS_DIR,
    CLIENT_QUOTES_DIR,
    COTACOES_JSONL,
    FORNECEDORES_JSONL,
    MANUFACTURER_DISCOVERY_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    MANUFACTURER_RESEARCH_JSONL,
    POLAR_SINERGY,
    RFQ_DRAFTS_DIR,
    RFQ_DRAFTS_JSONL,
    SOURCING_PROJECTS_DIR,
    SOURCING_PROJECTS_JSONL,
    SOURCING_REPORTS_DIR,
    SUPPLIER_PERFORMANCE_JSONL,
    append_jsonl,
    company_display_lines,
    control_number,
    count_jsonl,
    ensure_runtime_dirs,
    format_percent,
    format_usd,
    load_jsonl_records,
    make_id,
    normalize_pdf_text,
    normalize_text,
    openai_json_completion,
    round_money,
    slugify,
    utc_now,
    write_json,
)
from purchase_governance import (
    build_purchase_recommendation,
    record_purchase_company,
    record_purchase_contact,
    record_purchase_product,
    record_purchase_recommendation,
    record_rfq_batch,
    record_rfq_batch_supplier,
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
GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT = ROOT / 'scripts' / 'global_product_intelligence.py'
TELEGRAM_NOTIFIER_SCRIPT = ROOT / 'scripts' / 'telegram_notifier.py'
ADAPTIVE_DISCOVERY_LOG = ROOT / 'logs' / 'adaptive-discovery.log'
ADAPTIVE_DISCOVERY_REPORT_JSON = SOURCING_REPORTS_DIR / 'adaptive-discovery-report.json'
PRODUCT_INTELLIGENCE_JSONL = ROOT / 'product-intelligence.jsonl'
PRODUCT_CATEGORIES_JSONL = ROOT / 'product-categories.jsonl'
PRODUCT_COMPLIANCE_RULES_JSONL = ROOT / 'product-compliance-rules.jsonl'
PRODUCT_SOURCING_SOURCES_JSONL = ROOT / 'product-sourcing-sources.jsonl'
PROCUREMENT_KB_JSONL = ROOT / 'procurement-knowledge-base.jsonl'
SOURCE_TEMPLATES = [
    'official website',
    'factory page',
    'about us',
    'company profile',
    'contact page',
    'certifications',
    'export',
]
B2B_SOURCES = [
    'alibaba.com',
    'made-in-china.com',
    'globalsources.com',
    'thomasnet.com',
    'europages.com',
    'kompass.com',
]
BAD_DOMAINS = {
    'wikipedia.org', 'wikidata.org', 'britannica.com', 'investopedia.com',
    'ryerson.com', 'metalsupermarkets.com', 'encyclopedia.com', 'howstuffworks.com',
}
MARKETPLACE_HINTS = {'marketplace', 'amazon', 'aliexpress', 'ebay', 'etsy', 'walmart', 'temu'}
TRADING_HINTS = {'trading', 'trade company', 'exporter', 'importer', 'distributor'}
MANUFACTURER_HINTS = {'manufacturer', 'factory', 'manufacturing', 'production', 'made by', 'oem', 'odm', 'r&d'}
POSITIVE_REPUTATION_HINTS = {'trusted', 'verified', 'top supplier', 'gold supplier', 'certified'}
NEGATIVE_REPUTATION_HINTS = {'scam', 'complaint', 'lawsuit', 'fraud', 'fake', 'warning'}
CERT_KEYWORDS = ['ISO', 'CE', 'FDA', 'SGS', 'UL', 'TUV', 'TÜV', 'RoHS', 'CSA', 'BSCI', 'FSC']
FREE_EMAIL_DOMAINS = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com', 'qq.com', '163.com'}
PROJECTS_JSONL = SOURCING_PROJECTS_JSONL
RESEARCH_JSONL = MANUFACTURER_RESEARCH_JSONL
QUALIFICATION_JSONL = MANUFACTURER_QUALIFICATION_JSONL
PERFORMANCE_JSONL = SUPPLIER_PERFORMANCE_JSONL
PDF_OUTPUT_DIR = SOURCING_REPORTS_DIR
RFQ_OUTPUT_DIR = RFQ_DRAFTS_DIR


@dataclass
class Evidence:
    url: str
    source: str
    checked_at: str
    note: str
    confidence: float = 0.5
    capture_path: str | None = None


@dataclass
class Candidate:
    project_id: str
    candidate_id: str
    company_name: str
    website: str
    source_urls: list[str] = field(default_factory=list)
    evidence_notes: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    raw_text: str = ''
    profile: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def now() -> str:
    return utc_now()


def project_dir(project_id: str) -> Path:
    return SOURCING_PROJECTS_DIR / project_id


def project_data_dir(project_id: str) -> Path:
    return project_dir(project_id) / 'data'


def project_evidence_dir(project_id: str) -> Path:
    return project_dir(project_id) / 'evidence'


def project_report_dir(project_id: str) -> Path:
    return project_dir(project_id) / 'reports'


def project_rfq_dir(project_id: str) -> Path:
    return RFQ_OUTPUT_DIR / project_id


def ensure_project_dirs(project_id: str) -> None:
    for path in [
        project_dir(project_id),
        project_data_dir(project_id),
        project_evidence_dir(project_id),
        project_report_dir(project_id),
        project_rfq_dir(project_id),
    ]:
        path.mkdir(parents=True, exist_ok=True)


def load_records(path: Path) -> list[dict[str, Any]]:
    return load_jsonl_records(path)


def latest_project() -> dict[str, Any] | None:
    records = load_records(PROJECTS_JSONL)
    return records[-1] if records else None


def lookup_project(project_id: str) -> dict[str, Any] | None:
    last = None
    for record in load_records(PROJECTS_JSONL):
        if record.get('project_id') == project_id or record.get('id') == project_id:
            last = record
    return last


def write_project(project: dict[str, Any]) -> None:
    append_jsonl(PROJECTS_JSONL, project)
    ensure_project_dirs(project['project_id'])
    write_json(project_dir(project['project_id']) / 'project.json', project)


def save_project_state(project: dict[str, Any]) -> None:
    write_json(project_dir(project['project_id']) / 'project.json', project)


def load_latest_by_field(path: Path, field: str, value: str) -> dict[str, Any] | None:
    last = None
    for record in load_records(path):
        if str(record.get(field) or '') == value:
            last = record
    return last


def load_intelligence_by_id(product_intelligence_id: str | None) -> dict[str, Any] | None:
    if not product_intelligence_id:
        return None
    return load_latest_by_field(PRODUCT_INTELLIGENCE_JSONL, 'id', product_intelligence_id)


def load_category_profile_by_intelligence_id(product_intelligence_id: str | None) -> dict[str, Any] | None:
    if not product_intelligence_id:
        return None
    return load_latest_by_field(PRODUCT_CATEGORIES_JSONL, 'product_intelligence_id', product_intelligence_id)


def load_strategy_by_intelligence_id(product_intelligence_id: str | None) -> dict[str, Any] | None:
    if not product_intelligence_id:
        return None
    return load_latest_by_field(PROCUREMENT_KB_JSONL, 'product_intelligence_id', product_intelligence_id)


def project_intelligence(project: dict[str, Any]) -> dict[str, Any]:
    product_intelligence_id = str(project.get('product_intelligence_id') or '')
    intel = load_intelligence_by_id(product_intelligence_id) if product_intelligence_id else None
    profile = load_category_profile_by_intelligence_id(product_intelligence_id) if product_intelligence_id else None
    strategy = load_strategy_by_intelligence_id(product_intelligence_id) if product_intelligence_id else None
    return {
        'product_intelligence': intel or {},
        'category_profile': profile or {},
        'sourcing_strategy': strategy or {},
    }


def apply_intelligence_to_project(project: dict[str, Any], intel_bundle: dict[str, Any]) -> dict[str, Any]:
    product_intelligence = intel_bundle.get('product_intelligence') or {}
    category_profile = intel_bundle.get('category_profile') or {}
    sourcing_strategy = intel_bundle.get('sourcing_strategy') or {}
    project['product_intelligence_id'] = product_intelligence.get('id') or project.get('product_intelligence_id') or ''
    project['category_id'] = product_intelligence.get('category_id') or project.get('category_id') or ''
    project['category_label'] = product_intelligence.get('category_label') or project.get('category_label') or ''
    project['category'] = product_intelligence.get('category_label') or project.get('category') or ''
    project['subcategory'] = product_intelligence.get('subcategory') or project.get('subcategory') or ''
    project['compliance_rule_id'] = product_intelligence.get('compliance_rule_id') or project.get('compliance_rule_id') or ''
    project['source_profile_ids'] = list(product_intelligence.get('source_profile_ids') or project.get('source_profile_ids') or [])
    project['recommended_sources'] = list(product_intelligence.get('recommended_sources') or project.get('recommended_sources') or [])
    project['required_documents'] = list(product_intelligence.get('required_documents') or project.get('required_documents') or [])
    project['recommended_certifications'] = list(product_intelligence.get('recommended_certifications') or project.get('recommended_certifications') or [])
    project['qualification_criteria'] = list(product_intelligence.get('qualification_criteria') or project.get('qualification_criteria') or [])
    project['risk_flags'] = list(product_intelligence.get('risk_flags') or project.get('risk_flags') or [])
    project['rfq_model'] = product_intelligence.get('rfq_model') or project.get('rfq_model') or {}
    project['sourcing_strategy_id'] = sourcing_strategy.get('id') or project.get('sourcing_strategy_id') or ''
    project['search_mode'] = product_intelligence.get('search_mode') or project.get('search_mode') or ''
    project['product_intelligence_snapshot'] = product_intelligence
    project['category_profile_snapshot'] = category_profile
    project['sourcing_strategy_snapshot'] = sourcing_strategy
    return project


def run_external_command(command: list[str]) -> str:
    completed = subprocess.run(command, cwd=str(ROOT), check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def run_global_product_intelligence(product: str, region: str, country: str) -> dict[str, Any]:
    classify_output = run_external_command([
        sys.executable,
        str(GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT),
        '--product-text',
        product,
        '--region',
        region or 'global',
        '--country',
        country or 'global',
        'classify-test-product',
    ])
    intel = json.loads(classify_output)
    run_external_command([sys.executable, str(GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT), 'create-category-profile'])
    strategy_output = run_external_command([sys.executable, str(GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT), 'generate-sourcing-strategy'])
    strategy = json.loads(strategy_output)
    category_profile = load_category_profile_by_intelligence_id(str(intel.get('id') or '')) or {}
    return {
        'product_intelligence': intel,
        'category_profile': category_profile,
        'sourcing_strategy': strategy,
    }


def send_telegram_dry_run(message: str, project_id: str, product_intelligence_id: str) -> str:
    output = run_external_command([
        sys.executable,
        str(TELEGRAM_NOTIFIER_SCRIPT),
        'notify-event',
        '--event-type',
        'sourcing_run_with_intelligence',
        '--message',
        message,
        '--source-event',
        'run-with-intelligence',
        '--pipeline-run-id',
        f'{project_id}:{product_intelligence_id}',
        '--write-report',
    ])
    return output


def seed_simulated_candidates(project: dict[str, Any]) -> list[dict[str, Any]]:
    simulated_profile = {
        'company_name': f"Simulated {project.get('category_label') or 'Manufacturer'} Factory (dry-run)",
        'legal_name': f"Simulated {project.get('category_label') or 'Manufacturer'} Factory (dry-run)",
        'website': 'https://simulated.example/factory',
        'telephone': '+0000000000',
        'whatsapp': 'Não verificado',
        'wechat': 'Não verificado',
        'email': 'simulated.factory@gmail.com',
        'address': 'Simulated Industrial Park',
        'city': 'Simulated City',
        'state': 'Simulated State',
        'country': project.get('country') or 'Simulated Country',
        'contact_person': 'Simulated Sales',
        'contact_title': 'Sales Manager',
        'founded_year': 2021,
        'company_age_years': 3,
        'company_type': 'Manufacturer',
        'product_match_level': 'exact',
        'products': [project.get('product') or 'Simulated product'],
        'markets': 'International',
        'international_clients': 'Sim',
        'exports': 'Regular',
        'certifications': ['ISO 9001', 'CE'],
        'catalogs': ['Simulated catalog'],
        'photos': ['Simulated photos'],
        'videos': ['Simulated videos'],
        'notes': (
            'dry-run simulated manufacturer factory production line export FOB CIF '
            'technical manual capacity voltage frequency power materials spare parts HS Code '
            'category compliance documents sourcing strategy recommended sources'
        ),
        'source_urls': [
            'dry-run://global-product-intelligence',
            'https://simulated.example/factory',
        ],
        'confidence_score': 72.0,
        'risk_flags': ['Dry-run simulated'],
    }
    evidence_urls = ['dry-run://global-product-intelligence', 'https://simulated.example/factory']
    evidence_notes = [
        'dry-run simulated manufacturer from category intelligence',
        'category compliance and RFQ criteria applied',
        'official source profiles recommended by category',
    ]
    score = score_profile(project, simulated_profile, evidence_urls, evidence_notes)
    candidate = Candidate(
        project_id=project['project_id'],
        candidate_id=make_id('manufacturer'),
        company_name=simulated_profile['company_name'],
        website=simulated_profile['website'],
        source_urls=evidence_urls,
        evidence_notes=evidence_notes,
        evidence_paths=[],
        evidence=[
            {
                'url': 'dry-run://global-product-intelligence',
                'source': 'simulated_intelligence',
                'checked_at': now(),
                'note': 'Simulated manufacturer created because live search returned no candidates.',
                'confidence': 0.9,
            }
        ],
        raw_text='simulated dry-run candidate',
        profile=simulated_profile,
        score=score,
    )
    return [save_candidate(project, candidate)]


def safe_slug(value: str) -> str:
    text = slugify(value, max_length=64)
    return text or 'sourcing-project'


def strip_html(value: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', value)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = html.unescape(text)
    return normalize_text(text)


def fetch_url(url: str, timeout: int = 20) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        content_type = response.headers.get_content_type()
        raw = response.read()
    if content_type.startswith('text/') or b'<html' in raw[:500].lower():
        try:
            text = raw.decode('utf-8', errors='replace')
        except Exception:
            text = raw.decode('latin-1', errors='replace')
        return text, final_url
    return raw.decode('utf-8', errors='replace'), final_url


def ddg_search(
    query: str,
    max_results: int = 5,
    *,
    skip_b2b_domains: bool = True,
    blocked_domains: set[str] | None = None,
    required_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    url = 'https://search.brave.com/search?q=' + urllib.parse.quote_plus(query) + '&source=web'
    try:
        html_text, _ = fetch_url(url, timeout=20)
    except Exception:
        html_text = ''
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    candidate_urls = re.findall(r'https?://[^\s"\']+', html_text)
    skip_domains = {'search.brave.com', 'cdn.search.brave.com', 'imgs.search.brave.com', 'tiles.search.brave.com'}
    if skip_b2b_domains:
        skip_domains |= {'alibaba.com', 'made-in-china.com', 'globalsources.com', 'thomasnet.com', 'europages.com', 'kompass.com'}
    blocked = blocked_domains or BAD_DOMAINS
    terms = required_terms or ['cone', 'wafer', 'machine', 'food', 'baking', 'icecream']
    for raw in candidate_urls:
        if not raw.startswith('http'):
            continue
        parsed = urllib.parse.urlparse(raw)
        domain = parsed.netloc.lower().replace('www.', '')
        path = parsed.path.lower()
        if not domain or any(domain.endswith(bad) for bad in skip_domains):
            continue
        if domain in seen_urls:
            continue
        if any(domain.endswith(bad) for bad in blocked):
            continue
        if terms and not any(term in domain or term in path for term in terms):
            continue
        seen_urls.add(domain)
        title = normalize_text(parsed.path.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')) or domain
        results.append({'title': title, 'url': raw, 'snippet': '', 'query': query, 'engine': 'brave'})
        if len(results) >= max_results:
            break
    if results:
        return results
    fallback = yahoo_search(query, max_results=max_results, skip_b2b_domains=skip_b2b_domains, blocked_domains=blocked_domains, required_terms=required_terms)
    if fallback:
        return fallback
    return bing_search(query, max_results=max_results, skip_b2b_domains=skip_b2b_domains, blocked_domains=blocked_domains, required_terms=required_terms)


def decode_bing_redirect(url: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(url))
    qs = urllib.parse.parse_qs(parsed.query)
    encoded = qs.get('u', [''])[0]
    if encoded.startswith('a1'):
        payload = encoded[2:]
        pad = '=' * (-len(payload) % 4)
        try:
            return base64.urlsafe_b64decode(payload + pad).decode('utf-8', errors='replace')
        except Exception:
            return url
    return url


def yahoo_search(
    query: str,
    max_results: int = 5,
    *,
    skip_b2b_domains: bool = True,
    blocked_domains: set[str] | None = None,
    required_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    url = 'https://search.yahoo.com/search?p=' + urllib.parse.quote_plus(query)
    try:
        html_text, _ = fetch_url(url, timeout=20)
    except Exception:
        return []
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    blocked = blocked_domains or BAD_DOMAINS
    terms = required_terms or ['cone', 'wafer', 'machine', 'food', 'baking', 'icecream']
    for raw in re.findall(r'RU=([^/]+)', html_text):
        target = urllib.parse.unquote(raw)
        if not target.startswith('http'):
            continue
        parsed = urllib.parse.urlparse(target)
        domain = parsed.netloc.lower().replace('www.', '')
        path = parsed.path.lower()
        if not domain or 'yahoo.com' in domain or domain in seen_urls:
            continue
        if skip_b2b_domains and domain in B2B_SOURCES:
            continue
        if any(domain.endswith(bad) for bad in blocked):
            continue
        if terms and not any(term in domain or term in path for term in terms):
            continue
        seen_urls.add(domain)
        title = normalize_text(parsed.path.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')) or domain
        results.append({'title': title, 'url': target, 'snippet': '', 'query': query, 'engine': 'yahoo'})
        if len(results) >= max_results:
            break
    return results


def bing_search(
    query: str,
    max_results: int = 5,
    *,
    skip_b2b_domains: bool = True,
    blocked_domains: set[str] | None = None,
    required_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    url = 'https://www.bing.com/search?q=' + urllib.parse.quote_plus(query)
    try:
        html_text, _ = fetch_url(url, timeout=20)
    except Exception:
        return []
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    blocked = blocked_domains or BAD_DOMAINS
    terms = required_terms or ['cone', 'wafer', 'machine', 'food', 'baking', 'icecream']
    for block in re.findall(r'<li class="b_algo".*?</li>', html_text, flags=re.S):
        m = re.search(r'<a class="tilk"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
        if not m:
            m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        final_url = decode_bing_redirect(href)
        parsed = urllib.parse.urlparse(final_url)
        domain = parsed.netloc.lower().replace('www.', '')
        path = parsed.path.lower()
        if not domain or domain in seen_urls:
            continue
        if skip_b2b_domains and domain in B2B_SOURCES:
            continue
        if any(domain.endswith(bad) for bad in blocked):
            continue
        if terms and not any(term in domain or term in path for term in terms):
            continue
        title = normalize_text(re.sub(r'<.*?>', ' ', html.unescape(m.group(2))))
        snippet_match = re.search(r'<div class="b_caption"><p>(.*?)</p>', block, flags=re.S)
        snippet = normalize_text(re.sub(r'<.*?>', ' ', html.unescape(snippet_match.group(1)))) if snippet_match else ''
        seen_urls.add(domain)
        results.append({'title': title or domain, 'url': final_url, 'snippet': snippet, 'query': query, 'engine': 'bing'})
        if len(results) >= max_results:
            break
    return results


def extract_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower().replace('www.', '')


def company_name_from_result(result: dict[str, Any]) -> str:
    title = normalize_text(str(result.get('title') or ''))
    if title:
        for sep in ['|', '-', '–', '—']:
            if sep in title:
                left = normalize_text(title.split(sep)[0])
                if left:
                    return left
        return title[:120]
    domain = extract_domain(str(result.get('url') or ''))
    return domain.split('.')[0].replace('-', ' ').title() if domain else 'Empresa não identificada'


def extract_emails(text: str) -> list[str]:
    return sorted(set(re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text)))


def extract_phones(text: str) -> list[str]:
    phones = re.findall(r'(?:\+?\d[\d\s().-]{7,}\d)', text)
    cleaned = []
    for phone in phones:
        norm = normalize_text(phone)
        if len(re.sub(r'\D', '', norm)) >= 8:
            cleaned.append(norm)
    return sorted(set(cleaned))


def extract_years(text: str) -> list[int]:
    years = []
    for match in re.findall(r'(?i)(?:established|founded|since|since\s+)(?:in\s+)?(19\d{2}|20\d{2})', text):
        try:
            years.append(int(match))
        except ValueError:
            continue
    return years


def detect_company_type(text: str) -> str:
    lowered = text.lower()
    if any(hint in lowered for hint in MARKETPLACE_HINTS):
        return 'Marketplace'
    if 'trading company' in lowered or 'trader' in lowered:
        return 'Trading Company'
    if any(hint in lowered for hint in MANUFACTURER_HINTS):
        return 'Fabricante'
    if 'integrator' in lowered or 'system integrator' in lowered:
        return 'Integrador'
    if 'odm' in lowered and 'oem' in lowered:
        return 'OEM/ODM'
    if 'distributor' in lowered or 'dealer' in lowered:
        return 'Distribuidor'
    return 'Não verificado'


def detect_certifications(text: str) -> list[str]:
    found = []
    upper = text.upper()
    for cert in CERT_KEYWORDS:
        if cert.upper() in upper:
            found.append(cert)
    return sorted(set(found))


def detect_contact_channels(text: str) -> dict[str, list[str]]:
    return {
        'emails': extract_emails(text),
        'phones': extract_phones(text),
        'websites': sorted(set(re.findall(r'https?://[^\s"\']+', text))),
    }


def normalize_country(value: str | None) -> str:
    return normalize_text(value or '') or 'Não verificado'


def is_result_candidate(result: dict[str, Any]) -> bool:
    url = str(result.get('url') or '')
    domain = extract_domain(url)
    haystack = ' '.join([str(result.get('title') or ''), str(result.get('snippet') or ''), domain]).lower()
    if not domain:
        return False
    if any(domain.endswith(bad) for bad in BAD_DOMAINS):
        return False
    if any(term in haystack for term in ['what is', 'guide', 'definition', 'history', 'encyclopedia']):
        return False
    if domain in B2B_SOURCES or any(hint in haystack for hint in MANUFACTURER_HINTS):
        return True
    if '.' in domain and len(domain.split('.')) >= 2 and len(domain) > 6:
        return True
    return False


def openai_profile(project: dict[str, Any], evidence_text: str, search_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    prompt = {
        'project': {
            'product': project.get('product'),
            'region': project.get('region'),
            'country': project.get('country'),
            'category': project.get('category'),
            'telegram_text': project.get('telegram_text'),
        },
        'evidence_text': evidence_text[:12000],
        'search_results': search_results[:10],
        'requirements': {
            'return_json_only': True,
            'do_not_invent': True,
            'fill_missing_with': 'Não verificado',
            'fields': [
                'company_name', 'legal_name', 'website', 'telephone', 'whatsapp', 'wechat', 'email',
                'address', 'city', 'state', 'country', 'contact_person', 'contact_title', 'founded_year',
                'company_type', 'product_match_level', 'products', 'markets', 'international_clients',
                'exports', 'certifications', 'catalogs', 'photos', 'videos', 'notes', 'source_urls',
                'confidence_score', 'risk_flags'
            ],
        },
    }
    result = openai_json_completion(
        system_prompt='Você qualifica fabricantes reais com base apenas em evidência fornecida. Não invente dados.',
        user_prompt=json.dumps(prompt, ensure_ascii=False, indent=2),
        temperature=0.0,
    )
    return result if isinstance(result, dict) else None


def build_profile_from_evidence(project: dict[str, Any], result: dict[str, Any], page_text: str, extra_texts: list[str]) -> dict[str, Any]:
    text = '\n'.join([result.get('title', ''), result.get('snippet', ''), page_text, *extra_texts])
    domain = extract_domain(str(result.get('url') or ''))
    company_name = company_name_from_result(result)
    company_type = detect_company_type(text)
    certs = detect_certifications(text)
    years = extract_years(text)
    founded_year = min(years) if years else None
    age = max(datetime.now(timezone.utc).year - founded_year, 0) if founded_year else None
    contact = detect_contact_channels(text)
    website = str(result.get('url') or '')
    website_domain = extract_domain(website)
    contact_person = 'Não verificado'
    contact_title = 'Não verificado'
    for pat in [r'(?i)(?:contact|sales|business development|manager|director|engineer)[:\-]\s*([A-Z][A-Za-z ._-]{2,80})', r'(?i)\b([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[,\-]\s*(?:sales|manager|director|engineer)']:
        m = re.search(pat, text)
        if m:
            contact_person = normalize_text(m.group(1))
            break
    for pat in [r'(?i)(sales manager|business development|export manager|general manager|director|engineer|president|ceo)']:
        m = re.search(pat, text)
        if m:
            contact_title = normalize_text(m.group(1).title())
            break
    product = normalize_text(str(project.get('product') or ''))
    product_tokens = set(re.findall(r'[\w\u00C0-\u017F]+', product.lower()))
    text_tokens = set(re.findall(r'[\w\u00C0-\u017F]+', text.lower()))
    overlap = len(product_tokens & text_tokens)
    if overlap >= max(2, min(4, len(product_tokens))):
        match_level = 'exact'
    elif overlap >= 1:
        match_level = 'family'
    else:
        match_level = 'related'
    markets = 'International' if any(word in text.lower() for word in ['export', 'international', 'global', 'worldwide']) else 'Não verificado'
    exports = 'Regular' if 'export' in text.lower() else 'Não verificado'
    international_clients = 'Sim' if 'client' in text.lower() or 'customer' in text.lower() else 'Não verificado'
    catalogs = ['Não verificado']
    photos = ['Não verificado']
    videos = ['Não verificado']
    if any(word in text.lower() for word in ['catalog', 'brochure', 'pdf']):
        catalogs = ['Catálogo mencionado']
    if 'photo' in text.lower() or 'image' in text.lower():
        photos = ['Fotos mencionadas']
    if 'video' in text.lower():
        videos = ['Vídeo mencionado']
    risk_flags = []
    if any(domain.endswith(bad) for bad in ['aliexpress.com', 'amazon.com', 'ebay.com', 'temu.com']):
        risk_flags.append('Marketplace')
    if not website_domain:
        risk_flags.append('Sem website')
    if any(word in text.lower() for word in ['free email', 'gmail.com', 'yahoo.com', 'hotmail.com']):
        risk_flags.append('E-mail gratuito')
    if any(word in text.lower() for word in NEGATIVE_REPUTATION_HINTS):
        risk_flags.append('Reputação negativa')

    profile = {
        'company_name': company_name,
        'legal_name': company_name,
        'website': website or 'Não verificado',
        'telephone': contact['phones'][0] if contact['phones'] else 'Não verificado',
        'whatsapp': 'Não verificado',
        'wechat': 'Não verificado',
        'email': contact['emails'][0] if contact['emails'] else 'Não verificado',
        'address': 'Não verificado',
        'city': 'Não verificado',
        'state': 'Não verificado',
        'country': normalize_country(project.get('country')),
        'contact_person': contact_person,
        'contact_title': contact_title,
        'founded_year': founded_year if founded_year is not None else 'Não verificado',
        'company_age_years': age if age is not None else 'Não verificado',
        'company_type': company_type,
        'product_match_level': match_level,
        'products': [product] if product else ['Não verificado'],
        'markets': markets,
        'international_clients': international_clients,
        'exports': exports,
        'certifications': certs or ['Não verificado'],
        'catalogs': catalogs,
        'photos': photos,
        'videos': videos,
        'notes': normalize_text(f"{result.get('snippet', '')} {page_text[:500]}") or 'Não verificado',
        'source_urls': [result.get('url') or ''],
        'confidence_score': 65.0 if text else 20.0,
        'risk_flags': risk_flags,
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'recommended_sources': project.get('recommended_sources') or [],
        'required_documents': project.get('required_documents') or [],
        'qualification_criteria': project.get('qualification_criteria') or [],
        'source_profile_ids': project.get('source_profile_ids') or [],
    }
    if contact['emails']:
        profile['whatsapp'] = 'Não verificado'
    return profile


def company_display(profile: dict[str, Any]) -> str:
    return normalize_text(str(profile.get('company_name') or profile.get('legal_name') or 'Empresa sem nome'))


def normalize_score_input(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    if number < 0:
        return 0.0
    if number <= 1:
        return round(number * 100.0, 2)
    if number <= 5:
        return round((number / 5.0) * 100.0, 2)
    if number <= 10:
        return round((number / 10.0) * 100.0, 2)
    return round(min(number, 100.0), 2)


def score_response_time(hours: Any) -> float | None:
    if hours is None:
        return None
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return 100.0
    if value <= 1:
        return 98.0
    if value <= 24:
        return round(max(80.0, 98.0 - ((value - 1.0) / 23.0) * 18.0), 2)
    if value <= 72:
        return round(max(55.0, 80.0 - ((value - 24.0) / 48.0) * 25.0), 2)
    if value <= 168:
        return round(max(25.0, 55.0 - ((value - 72.0) / 96.0) * 30.0), 2)
    return 0.0


def performance_history_for_supplier(profile: dict[str, Any], supplier_id: str | None = None) -> list[dict[str, Any]]:
    company_name = normalize_text(str(profile.get('company_name') or profile.get('legal_name') or ''))
    website = normalize_text(str(profile.get('website') or ''))
    domain = extract_domain(website)
    records = load_records(PERFORMANCE_JSONL)
    matches: list[dict[str, Any]] = []
    for record in records:
        if supplier_id and str(record.get('supplier_id') or '') == str(supplier_id):
            matches.append(record)
            continue
        rec_name = normalize_text(str(record.get('company_name') or ''))
        rec_domain = extract_domain(str(record.get('website') or '')) if record.get('website') else ''
        if company_name and rec_name and company_name.casefold() == rec_name.casefold():
            matches.append(record)
            continue
        if domain and rec_domain and domain == rec_domain:
            matches.append(record)
            continue
    return matches


def build_performance_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {'count': 0, 'average': None, 'latest': None, 'timeline': []}
    ordered = sorted(records, key=lambda r: str(r.get('created_at') or r.get('updated_at') or ''))
    numeric_fields = [
        'response_time_hours', 'rfq_response_rate', 'technical_quality', 'spec_accuracy',
        'price_competitiveness', 'lead_time_compliance', 'service_quality', 'communication_ease',
        'purchase_success_rate', 'reorder_rate', 'manual_rating',
    ]
    totals: dict[str, list[float]] = {field: [] for field in numeric_fields}
    timeline: list[dict[str, Any]] = []
    for record in ordered:
        snapshot: dict[str, Any] = {
            'created_at': record.get('created_at'),
            'updated_at': record.get('updated_at'),
            'supplier_id': record.get('supplier_id'),
            'company_name': record.get('company_name'),
            'supplier_trust_score': record.get('supplier_trust_score'),
            'supplier_performance_score': record.get('supplier_performance_score'),
        }
        for field in numeric_fields:
            value = normalize_score_input(record.get(field))
            if value is not None:
                totals[field].append(value)
        timeline.append(snapshot)
    averages = {field: round(sum(values) / len(values), 2) for field, values in totals.items() if values}
    return {
        'count': len(records),
        'average': averages,
        'latest': ordered[-1],
        'timeline': timeline,
    }


def calculate_supplier_trust_score(project: dict[str, Any], profile: dict[str, Any], evidence_urls: list[str], evidence_notes: list[str], *, supplier_id: str | None = None, history_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    text_blob = ' '.join(evidence_notes + [profile.get('notes', ''), profile.get('company_type', ''), profile.get('products', ['']) and ' '.join(profile.get('products') or [])]).lower()
    history = history_records if history_records is not None else performance_history_for_supplier(profile, supplier_id=supplier_id)
    snapshot = build_performance_snapshot(history)
    company_name = normalize_text(str(profile.get('company_name') or profile.get('legal_name') or ''))
    legal_name = normalize_text(str(profile.get('legal_name') or ''))
    website = str(profile.get('website') or '')
    email = str(profile.get('email') or '')
    telephone = str(profile.get('telephone') or '')
    contact_person = str(profile.get('contact_person') or '')
    age = profile.get('company_age_years')
    certs = profile.get('certifications') or []

    score_breakdown: dict[str, float] = {}

    market_age = 0.0
    if isinstance(age, int):
        if age >= 15:
            market_age = 15.0
        elif age >= 10:
            market_age = 13.0
        elif age >= 5:
            market_age = 10.0
        elif age >= 2:
            market_age = 6.0
        elif age > 0:
            market_age = 2.0
    score_breakdown['tempo_mercado'] = market_age

    reputation = 0.0
    if any(h in text_blob for h in POSITIVE_REPUTATION_HINTS):
        reputation += 8.0
    if any(h in text_blob for h in NEGATIVE_REPUTATION_HINTS):
        reputation -= 8.0
    if any(term in text_blob for term in ['review', 'reviews', 'testimonial', 'testimonials', 'case study', 'case studies']):
        reputation += 3.0
    if any(domain in '\n'.join(evidence_urls) for domain in B2B_SOURCES):
        reputation += 2.0
    score_breakdown['reputacao'] = max(min(reputation + 5.0, 15.0), 0.0)

    export_history = 0.0
    if 'export' in text_blob:
        export_history += 6.0
    if any(term in text_blob for term in ['international', 'global', 'worldwide']):
        export_history += 3.0
    if any(term in text_blob for term in ['fob', 'cif', 'exw', 'shipping', 'customs']):
        export_history += 3.0
    if any(term in text_blob for term in ['clients', 'customers', 'countries', 'regions']):
        export_history += 3.0
    score_breakdown['historico_exportacao'] = min(export_history, 15.0)

    contact_score = 0.0
    if contact_person and contact_person != 'Não verificado':
        contact_score += 3.0
    if email and email != 'Não verificado':
        contact_score += 3.0
    if telephone and telephone != 'Não verificado':
        contact_score += 2.0
    if profile.get('whatsapp') not in ('Não verificado', None, '') or profile.get('wechat') not in ('Não verificado', None, ''):
        contact_score += 2.0
    score_breakdown['qualidade_contato'] = min(contact_score, 10.0)

    stability = 0.0
    if website.startswith('http'):
        stability += 3.0
    if profile.get('address') != 'Não verificado':
        stability += 3.0
    if profile.get('city') != 'Não verificado':
        stability += 2.0
    if profile.get('country') != 'Não verificado':
        stability += 2.0
    if profile.get('legal_name') not in ('Não verificado', None, ''):
        stability += 2.0
    if profile.get('catalogs') and profile.get('catalogs') != ['Não verificado']:
        stability += 1.0
    if profile.get('products') and profile.get('products') != ['Não verificado']:
        stability += 1.0
    score_breakdown['estabilidade_empresa'] = min(stability, 15.0)

    consistency = 0.0
    if company_name and legal_name and company_name.casefold() == legal_name.casefold():
        consistency += 3.0
    if profile.get('product_match_level') == 'exact':
        consistency += 3.0
    if len({extract_domain(u) for u in evidence_urls if u}) <= 2 and evidence_urls:
        consistency += 2.0
    if profile.get('confidence_score') and isinstance(profile.get('confidence_score'), (int, float)):
        consistency += min(float(profile['confidence_score']) / 20.0, 2.0)
    score_breakdown['consistencia_informacoes'] = min(consistency, 10.0)

    history_score = 0.0
    if snapshot['count']:
        average = snapshot.get('average') or {}
        weighted_parts = []
        for key in ['rfq_response_rate', 'technical_quality', 'spec_accuracy', 'price_competitiveness', 'lead_time_compliance', 'service_quality', 'communication_ease', 'purchase_success_rate', 'reorder_rate', 'manual_rating']:
            value = average.get(key)
            if value is not None:
                weighted_parts.append(float(value))
        if weighted_parts:
            history_score = round(sum(weighted_parts) / len(weighted_parts) * 0.15, 2)
        if snapshot['count'] >= 5:
            history_score += 3.0
        elif snapshot['count'] >= 2:
            history_score += 1.5
    score_breakdown['historico_interno_polar'] = min(history_score, 15.0)

    performance_score = 0.0
    if snapshot['count']:
        latest = snapshot.get('latest') or {}
        values = []
        for field in ['response_time_hours', 'rfq_response_rate', 'technical_quality', 'spec_accuracy', 'price_competitiveness', 'lead_time_compliance', 'service_quality', 'communication_ease', 'purchase_success_rate', 'reorder_rate', 'manual_rating']:
            raw = latest.get(field)
            if field == 'response_time_hours':
                normalized = score_response_time(raw)
            else:
                normalized = normalize_score_input(raw)
            if normalized is not None:
                values.append(normalized)
        if values:
            performance_score = round(sum(values) / len(values) * 0.2, 2)
    score_breakdown['desempenho_negociacao'] = min(performance_score, 20.0)

    total = round(sum(score_breakdown.values()), 2)
    total = max(min(total, 100.0), 0.0)
    trust_total = total
    trust_status = 'Fornecedor Premium' if trust_total >= 80 else 'Fornecedor Promissor' if trust_total >= 60 else 'Fornecedor não qualificado'
    trust_confidence = 0.0
    if evidence_urls:
        trust_confidence += 20.0
    if history:
        trust_confidence += 20.0
    if profile.get('website') != 'Não verificado':
        trust_confidence += 20.0
    if profile.get('telephone') != 'Não verificado':
        trust_confidence += 20.0
    if profile.get('email') != 'Não verificado':
        trust_confidence += 20.0
    trust_confidence = min(trust_confidence, 100.0)

    return {
        'supplier_trust_score': trust_total,
        'supplier_trust_status': trust_status,
        'supplier_trust_confidence': trust_confidence,
        'supplier_trust_score_breakdown': score_breakdown,
        'supplier_history_count': snapshot['count'],
        'supplier_history_average': snapshot['average'],
        'supplier_history_timeline': snapshot['timeline'],
        'supplier_history_latest': snapshot['latest'],
    }


def score_profile(project: dict[str, Any], profile: dict[str, Any], evidence_urls: list[str], evidence_notes: list[str]) -> dict[str, Any]:
    text_blob = ' '.join(evidence_notes + [profile.get('notes', ''), profile.get('company_type', '')]).lower()
    company_type = str(profile.get('company_type') or 'Não verificado')
    website = str(profile.get('website') or '')
    email = str(profile.get('email') or '')
    contact_person = str(profile.get('contact_person') or '')
    products = ' '.join(profile.get('products') or [])
    product_match_level = str(profile.get('product_match_level') or 'related')
    age = profile.get('company_age_years')
    certs = profile.get('certifications') or []
    risk_flags = list(profile.get('risk_flags') or [])
    intelligence = project_intelligence(project)
    category_profile = intelligence.get('category_profile') or {}
    product_intelligence = intelligence.get('product_intelligence') or {}
    mandatory_documents = list(category_profile.get('required_documents') or product_intelligence.get('required_documents') or [])
    qualification_criteria = list(category_profile.get('qualification_criteria') or product_intelligence.get('qualification_criteria') or [])
    source_profile_ids = list(category_profile.get('source_profile_ids') or product_intelligence.get('source_profile_ids') or [])

    score_breakdown = {}
    fabrication = 0
    if any(h in text_blob for h in ['manufacturer', 'factory', 'manufacturing', 'oem', 'odm']):
        fabrication += 6
    if any(h in text_blob for h in ['factory', 'manufacturing plant', 'factory tour', 'production line']):
        fabrication += 5
    if any(h in text_blob for h in ['photo', 'image', 'gallery', 'plant']):
        fabrication += 5
    if any(h in text_blob for h in ['production', 'engineering', 'r&d', 'research and development']):
        fabrication += 4
    score_breakdown['evidencia_fabricacao_propria'] = min(fabrication, 20)

    compatibility = {'exact': 10, 'family': 4, 'related': 1}.get(product_match_level, 0)
    score_breakdown['compatibilidade_produto'] = compatibility

    age_score = 0
    if isinstance(age, int):
        if age > 10:
            age_score = 10
        elif age >= 5:
            age_score = 7
        elif age >= 2:
            age_score = 4
        elif age > 0:
            age_score = 1
    score_breakdown['tempo_existencia'] = age_score

    corp = 0
    if profile.get('address') != 'Não verificado':
        corp += 3
    if profile.get('telephone') != 'Não verificado':
        corp += 2
    if website and not any(d in website.lower() for d in FREE_EMAIL_DOMAINS):
        corp += 2
    if profile.get('legal_name') not in ('Não verificado', None, ''):
        corp += 2
    if profile.get('city') != 'Não verificado' and profile.get('country') != 'Não verificado':
        corp += 1
    score_breakdown['dados_corporativos'] = corp

    digital = 0
    if website.startswith('http'):
        digital += 3
    if any(domain in '\n'.join(evidence_urls) for domain in B2B_SOURCES):
        digital += 2
    if any(h in text_blob for h in POSITIVE_REPUTATION_HINTS):
        digital += 2
    if any(h in text_blob for h in ['export', 'international', 'global']):
        digital += 2
    if 'negative' not in text_blob and 'warning' not in text_blob:
        digital += 1
    score_breakdown['presenca_digital_reputacao'] = min(digital, 10)

    cert_score = min(len([c for c in certs if c != 'Não verificado']) * 2, 10)
    if any(c in text_blob.upper() for c in ['ISO', 'CE', 'FDA', 'SGS', 'UL', 'TÜV', 'TUV']):
        cert_score = max(cert_score, 4)
    score_breakdown['certificacoes'] = cert_score

    compliance_score = 0
    matched_documents: list[str] = []
    missing_documents: list[str] = []
    compliance_text = ' '.join([text_blob, website.lower(), products.lower(), ' '.join(evidence_notes).lower()])
    for item in mandatory_documents:
        token = normalize_text(str(item)).lower()
        if not token:
            continue
        if token in compliance_text or any(part and part in compliance_text for part in token.replace('/', ' ').split()):
            matched_documents.append(item)
        else:
            missing_documents.append(item)
    if mandatory_documents:
        compliance_score = round((len(matched_documents) / max(len(mandatory_documents), 1)) * 10)
    score_breakdown['compliance_categoria'] = compliance_score
    score_breakdown['criterios_categoria'] = min(len([item for item in qualification_criteria if normalize_text(str(item)).lower() in compliance_text]), 5)

    source_signal_score = 0
    if source_profile_ids:
        source_signal_score = min(len([sid for sid in source_profile_ids if sid]) * 1, 5)
    score_breakdown['fontes_categoria'] = source_signal_score

    if missing_documents:
        risk_flags.append(f'Campos categoria ausentes: {", ".join(missing_documents[:3])}')
    export_score = 0
    if 'export' in text_blob:
        export_score += 3
    if any(term in text_blob for term in ['fob', 'cif', 'exw']):
        export_score += 2
    if any(term in text_blob for term in ['international clients', 'global customers', 'clients worldwide']):
        export_score += 2
    if any(term in text_blob for term in ['english', 'english speaking', 'sales team']):
        export_score += 2
    if any(term in text_blob for term in ['customs', 'shipping documents', 'export documents']):
        export_score += 1
    score_breakdown['capacidade_exportadora'] = min(export_score, 10)

    contact_score = 0
    if contact_person and contact_person != 'Não verificado':
        contact_score += 2
    if email and email != 'Não verificado':
        contact_score += 1
    if profile.get('telephone') != 'Não verificado' or profile.get('whatsapp') != 'Não verificado' or profile.get('wechat') != 'Não verificado':
        contact_score += 1
    if email and not any(email.lower().endswith(f'@{d}') for d in FREE_EMAIL_DOMAINS):
        contact_score += 1
    score_breakdown['qualidade_contato'] = min(contact_score, 5)

    penalties = 0
    applied = []
    if company_type == 'Marketplace' or any('Marketplace' in x for x in risk_flags):
        penalties -= 10
        applied.append('Marketplace')
    if company_type == 'Trading Company' and fabrication < 6:
        penalties -= 8
        applied.append('Trading sem evidência de fábrica')
    if not website or website == 'Não verificado':
        penalties -= 5
        applied.append('Sem website')
    if profile.get('address') == 'Não verificado':
        applied.append('Endereço não verificado')
    if email and any(email.lower().endswith(f'@{d}') for d in FREE_EMAIL_DOMAINS):
        penalties -= 3
        applied.append('E-mail gratuito')
    if age is not None and isinstance(age, int) and age < 2:
        penalties -= 5
        applied.append('Empresa muito recente sem histórico')
    if any(flag in risk_flags for flag in ['Reputação negativa']):
        penalties -= 10
        applied.append('Reputação negativa')
    if 'inconsistente' in text_blob:
        penalties -= 7
        applied.append('Informações contraditórias')
    score_breakdown['penalidades'] = penalties

    total = sum(score_breakdown.values())
    total = max(min(total, 100), 0)
    trust_data = calculate_supplier_trust_score(project, profile, evidence_urls, evidence_notes)
    supplier_trust_score = trust_data['supplier_trust_score']
    final_score = min(total, supplier_trust_score)
    confidence = 0.0
    if evidence_urls:
        confidence = min(100.0, 25.0 + len(evidence_urls) * 12.5)
    if any(v == 'Não verificado' for v in [profile.get('website'), profile.get('telephone'), profile.get('email')]):
        confidence = max(10.0, confidence - 10.0)
    status = classify_status(final_score, profile, applied)
    approved = status == 'Fornecedor Premium' and final_score >= 80 and profile.get('company_type') not in {'Marketplace', 'Trading Company'}
    manual = status == 'Fornecedor Promissor'
    if profile.get('company_type') in {'Marketplace'}:
        approved = False
        status = 'Marketplace'
    if profile.get('company_type') == 'Trading Company' and final_score < 80:
        approved = False
    return {
        'manufacturer_score': total,
        'supplier_trust_score': supplier_trust_score,
        'final_score': final_score,
        'qualification_status': status,
        'approved_for_rfq': approved,
        'confidence_score': round_money(confidence),
        'risk_flags': sorted(set(risk_flags)),
        'penalties_applied': applied,
        'product_match_score': compatibility,
        'company_age_years': age if isinstance(age, int) else 'Não verificado',
        'company_age_score': age_score,
        'reputation_rating': profile.get('reputation_rating', 'Não verificado'),
        'reputation_source': profile.get('reputation_source', 'Não verificado'),
        'certifications': certs,
        'export_capability_score': score_breakdown['capacidade_exportadora'],
        'contact_quality_score': score_breakdown['qualidade_contato'],
        'evidence_urls': evidence_urls,
        'evidence_notes': evidence_notes,
        'checked_at': now(),
        'checked_by': 'sourcing_research.py',
        'manual_review_required': manual,
        'score_breakdown': score_breakdown,
        'supplier_trust_score_breakdown': trust_data['supplier_trust_score_breakdown'],
        'supplier_trust_status': trust_data['supplier_trust_status'],
        'supplier_trust_confidence': trust_data['supplier_trust_confidence'],
        'supplier_history_count': trust_data['supplier_history_count'],
        'supplier_history_average': trust_data['supplier_history_average'],
        'supplier_history_timeline': trust_data['supplier_history_timeline'],
        'supplier_history_latest': trust_data['supplier_history_latest'],
        'category_compliance_score': score_breakdown['compliance_categoria'],
        'category_criteria_score': score_breakdown['criterios_categoria'],
        'category_source_signal_score': score_breakdown['fontes_categoria'],
        'category_missing_documents': missing_documents,
        'category_matched_documents': matched_documents,
        'category_qualification_criteria': qualification_criteria,
        'category_source_profile_ids': source_profile_ids,
    }


def classify_status(score: int, profile: dict[str, Any], applied_penalties: list[str]) -> str:
    ctype = str(profile.get('company_type') or 'Não verificado')
    if ctype == 'Marketplace' or 'Marketplace' in applied_penalties:
        return 'Marketplace'
    if ctype == 'Trading Company' and score < 80:
        return 'Fornecedor não qualificado'
    if score >= 80:
        return 'Fornecedor Premium'
    if 60 <= score <= 79:
        return 'Fornecedor Promissor'
    return 'Fornecedor não qualificado'


def parse_telegram_text(text: str) -> dict[str, str]:
    product = ''
    region = ''
    country = ''
    category = ''
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, sep, value = line.partition(':')
        if sep:
            norm_key = normalize_text(key).lower()
            value = normalize_text(value)
            if norm_key in {'produto', 'product', 'item', 'need', 'solicitação'}:
                product = value
            elif norm_key in {'região', 'region', 'area', 'área'}:
                region = value
            elif norm_key in {'país', 'country'}:
                country = value
            elif norm_key in {'categoria', 'category', 'segmento'}:
                category = value
    if not product:
        product = normalize_text(text.splitlines()[0] if text.strip() else text)
    return {'product': product, 'region': region, 'country': country, 'category': category}


# ---------------------------------------------------------------------------
# Project and search workflow
# ---------------------------------------------------------------------------


def create_project(args: argparse.Namespace) -> dict[str, Any]:
    ensure_runtime_dirs()
    if args.telegram_text:
        parsed = parse_telegram_text(args.telegram_text)
        product = args.product or parsed['product']
        region = args.region or parsed['region']
        country = args.country or parsed['country']
        category = args.category or parsed['category']
    else:
        product = args.product
        region = args.region
        country = args.country
        category = args.category
    if not product:
        raise SystemExit('product is required')
    project_id = make_id('sourcing_project')
    project = {
        'id': project_id,
        'project_id': project_id,
        'version': '0.1.0',
        'created_at': now(),
        'updated_at': now(),
        'status': 'created',
        'source': 'telegram' if args.telegram_text else 'manual',
        'telegram_text': args.telegram_text or '',
        'product': normalize_text(product),
        'region': normalize_text(region or ''),
        'country': normalize_text(country or ''),
        'category': normalize_text(category or ''),
        'product_intelligence_id': getattr(args, 'product_intelligence_id', '') or '',
        'category_id': getattr(args, 'category_id', '') or normalize_text(category or ''),
        'category_label': getattr(args, 'category_label', '') or '',
        'subcategory': getattr(args, 'subcategory', '') or '',
        'compliance_rule_id': getattr(args, 'compliance_rule_id', '') or '',
        'source_profile_ids': getattr(args, 'source_profile_ids', []) or [],
        'recommended_sources': getattr(args, 'recommended_sources', []) or [],
        'required_documents': getattr(args, 'required_documents', []) or [],
        'recommended_certifications': getattr(args, 'recommended_certifications', []) or [],
        'qualification_criteria': getattr(args, 'qualification_criteria', []) or [],
        'risk_flags': getattr(args, 'risk_flags', []) or [],
        'rfq_model': getattr(args, 'rfq_model', {}) or {},
        'search_mode': getattr(args, 'search_mode', '') or '',
        'sourcing_strategy_id': getattr(args, 'sourcing_strategy_id', '') or '',
        'search_queries': [],
        'candidate_ids': [],
        'approved_candidate_ids': [],
        'rfq_draft_paths': [],
        'notes': 'Projeto de sourcing criado em modo dry_run',
    }
    write_project(project)
    print('PROJECT CREATED')
    print(f'project_id={project_id}')
    print(f'product={project["product"]}')
    print(f'region={project["region"] or "-"}')
    print(f'country={project["country"] or "-"}')
    return project


def cmd_create_project(args: argparse.Namespace) -> int:
    create_project(args)
    return 0


def cmd_run_with_intelligence(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    if args.telegram_text:
        parsed = parse_telegram_text(args.telegram_text)
        product = args.product or parsed['product']
        region = args.region or parsed['region']
        country = args.country or parsed['country']
        category = args.category or parsed['category']
    else:
        product = args.product or 'Semi-automatic wafer cone making machine, 1000-1200 pcs/hour, electric heating, 380V 60Hz 3-phase, with mixer, dosing, baking and cone rolling unit.'
        region = args.region or 'global'
        country = args.country or 'global'
        category = args.category or ''
    intel_bundle = run_global_product_intelligence(product, region, country)
    product_intelligence = intel_bundle.get('product_intelligence') or {}
    category_profile = intel_bundle.get('category_profile') or {}
    sourcing_strategy = intel_bundle.get('sourcing_strategy') or {}
    create_args = argparse.Namespace(
        product=product,
        region=region,
        country=country,
        category=category,
        telegram_text=args.telegram_text or '',
        product_intelligence_id=product_intelligence.get('id', ''),
        category_id=product_intelligence.get('category_id', ''),
        category_label=product_intelligence.get('category_label', ''),
        subcategory=product_intelligence.get('subcategory', ''),
        compliance_rule_id=product_intelligence.get('compliance_rule_id', ''),
        source_profile_ids=product_intelligence.get('source_profile_ids', []),
        recommended_sources=product_intelligence.get('recommended_sources', []),
        required_documents=product_intelligence.get('required_documents', []),
        recommended_certifications=product_intelligence.get('recommended_certifications', []),
        qualification_criteria=product_intelligence.get('qualification_criteria', []),
        risk_flags=product_intelligence.get('risk_flags', []),
        rfq_model=product_intelligence.get('rfq_model', {}),
        search_mode=product_intelligence.get('search_mode', ''),
        sourcing_strategy_id=sourcing_strategy.get('id', ''),
    )
    project = create_project(create_args)
    project = apply_intelligence_to_project(project, intel_bundle)
    project['updated_at'] = now()
    append_jsonl(PROJECTS_JSONL, project)
    save_project_state(project)
    search_args = argparse.Namespace(project_id=project['project_id'])
    cmd_search_manufacturers(search_args)
    candidate_records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not candidate_records:
        adaptive_report = run_adaptive_discovery(project)
        project = lookup_project(project['project_id']) or project
        candidate_records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
        if not candidate_records:
            candidate_records = load_discovered_records(project['project_id'])
        print(f'adaptive_report={ADAPTIVE_DISCOVERY_REPORT_JSON}')
    cmd_qualify(search_args)
    cmd_report(search_args)
    cmd_approve_for_rfq(search_args)
    latest_records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    status_counts = {
        'premium': sum(1 for rec in latest_records if final_supplier_score(rec) >= 80),
        'manual': sum(1 for rec in latest_records if 60 <= final_supplier_score(rec) < 80),
        'blocked': sum(1 for rec in latest_records if final_supplier_score(rec) < 60),
    }
    telegram_message = (
        f'Run-with-intelligence dry_run concluído: {project["project_id"]} | '
        f'categoria={project.get("category_label") or project.get("category") or "Não verificado"} | '
        f'final_count premium={status_counts["premium"]} manual={status_counts["manual"]} blocked={status_counts["blocked"]}'
    )
    telegram_output = send_telegram_dry_run(telegram_message, project['project_id'], project.get('product_intelligence_id') or '')
    print('RUN WITH INTELLIGENCE COMPLETED')
    print(f'project_id={project["project_id"]}')
    print(f'product_intelligence_id={project.get("product_intelligence_id") or "Não verificado"}')
    print(f'category={project.get("category_label") or project.get("category") or "Não verificado"}')
    print(f'subcategory={project.get("subcategory") or "Não verificado"}')
    print(f'telegram={telegram_output.splitlines()[0] if telegram_output else "dry_run"}')
    return 0


def cmd_adaptive_discovery(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    if getattr(args, 'project_id', ''):
        project = lookup_project(args.project_id)
    else:
        project = None
    if project is None:
        if args.telegram_text:
            parsed = parse_telegram_text(args.telegram_text)
            product = args.product or parsed['product']
            region = args.region or parsed['region']
            country = args.country or parsed['country']
            category = args.category or parsed['category']
        else:
            product = args.product or 'Semi-automatic wafer cone making machine, 1000-1200 pcs/hour, electric heating, 380V 60Hz 3-phase, with mixer, dosing, baking and cone rolling unit.'
            region = args.region or 'global'
            country = args.country or 'global'
            category = args.category or ''
        intel_bundle = run_global_product_intelligence(product, region, country)
        product_intelligence = intel_bundle.get('product_intelligence') or {}
        category_profile = intel_bundle.get('category_profile') or {}
        sourcing_strategy = intel_bundle.get('sourcing_strategy') or {}
        create_args = argparse.Namespace(
            product=product,
            region=region,
            country=country,
            category=category,
            telegram_text=args.telegram_text or '',
            product_intelligence_id=product_intelligence.get('id', ''),
            category_id=product_intelligence.get('category_id', ''),
            category_label=product_intelligence.get('category_label', ''),
            subcategory=product_intelligence.get('subcategory', ''),
            compliance_rule_id=product_intelligence.get('compliance_rule_id', ''),
            source_profile_ids=product_intelligence.get('source_profile_ids', []),
            recommended_sources=product_intelligence.get('recommended_sources', []),
            required_documents=product_intelligence.get('required_documents', []),
            recommended_certifications=product_intelligence.get('recommended_certifications', []),
            qualification_criteria=product_intelligence.get('qualification_criteria', []),
            risk_flags=product_intelligence.get('risk_flags', []),
            rfq_model=product_intelligence.get('rfq_model', {}),
            search_mode=product_intelligence.get('search_mode', ''),
            sourcing_strategy_id=sourcing_strategy.get('id', ''),
        )
        project = create_project(create_args)
        project = apply_intelligence_to_project(project, intel_bundle)
        project['updated_at'] = now()
        append_jsonl(PROJECTS_JSONL, project)
        save_project_state(project)
    report = run_adaptive_discovery(project)
    project = lookup_project(project['project_id']) or project
    print('ADAPTIVE DISCOVERY COMPLETED')
    print(f'project_id={project["project_id"]}')
    print(f'rounds={len(report["rounds"])}')
    print(f'queries_total={report["queries_total"]}')
    print(f'candidates_found={report["candidates_found"]}')
    print(f'candidates_above_80={report["candidates_above_80"]}')
    print(f'candidates_between_60_and_79={report["candidates_between_60_and_79"]}')
    print(f'candidates_blocked={report["candidates_blocked"]}')
    print(f'report_json={ADAPTIVE_DISCOVERY_REPORT_JSON}')
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not records:
        raise SystemExit('no candidate records available')
    intelligence = project_intelligence(project)
    category_profile = intelligence.get('category_profile') or {}
    product_intelligence = intelligence.get('product_intelligence') or {}
    sourcing_strategy = intelligence.get('sourcing_strategy') or {}
    report = {
        'id': make_id('sourcing_report'),
        'project_id': project['project_id'],
        'created_at': now(),
        'updated_at': now(),
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'sourcing_strategy_id': project.get('sourcing_strategy_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'mandatory_documents': project.get('required_documents') or [],
        'recommended_sources': project.get('recommended_sources') or [],
        'qualification_criteria': project.get('qualification_criteria') or [],
        'source_profile_ids': project.get('source_profile_ids') or [],
        'recommended_certifications': project.get('recommended_certifications') or [],
        'risk_flags': project.get('risk_flags') or [],
        'category_profile': category_profile,
        'product_intelligence': product_intelligence,
        'sourcing_strategy': sourcing_strategy,
        'reason_summary': {
            'gate_rule': 'final_score = min(manufacturer_score, supplier_trust_score)',
            'auto_rfq_threshold': '>= 80',
            'manual_review_threshold': '60-79',
            'block_threshold': '< 60',
            'selected_sources': project.get('recommended_sources') or [],
            'required_documents': project.get('required_documents') or [],
            'qualification_criteria': project.get('qualification_criteria') or [],
        },
        'search_mode': project.get('search_mode') or 'Não verificado',
        'product': project.get('product'),
        'region': project.get('region'),
        'country': project.get('country'),
        'candidate_count': len(records),
        'qualified_count': sum(1 for rec in records if rec.get('approved_for_rfq')),
        'manual_review_count': sum(1 for rec in records if rec.get('manual_review_required')),
        'premium_count': sum(1 for rec in records if (final_supplier_score(rec)) >= 80),
        'promising_count': sum(1 for rec in records if 60 <= (final_supplier_score(rec)) < 80),
        'rejected_count': sum(1 for rec in records if (final_supplier_score(rec)) < 60),
        'top_candidates': sorted(records, key=lambda r: final_supplier_score(r), reverse=True)[:10],
        'telegram_review_queue': [build_telegram_review_payload(rec) for rec in records if 60 <= (final_supplier_score(rec)) < 80],
    }
    report_path = project_report_dir(project['project_id']) / f'{safe_slug(project["project_id"])}.json'
    try:
        recommendation = build_purchase_recommendation(
            product_name=project.get('product') or 'Produto',
            supplier_name=report['top_candidates'][0].get('company_name') if report['top_candidates'] else 'Fornecedor',
            supplier_country=project.get('country'),
            supplier_city=project.get('region'),
            notes=[
                f'qualified={report["qualified_count"]}',
                f'manual_review={report["manual_review_count"]}',
                f'premium={report["premium_count"]}',
                f'blocked={report["rejected_count"]}',
            ],
        )
        if report['qualified_count'] and report['manual_review_count'] == 0 and report['rejected_count'] == 0:
            recommendation['suggested_action'] = 'authorize_all_suppliers'
            recommendation['recommendation_title'] = 'Autorizar envio para fornecedores qualificados'
        elif report['manual_review_count']:
            recommendation['suggested_action'] = 'review_rfq_before_send'
            recommendation['recommendation_title'] = 'Revisar RFQ antes de enviar'
        else:
            recommendation['suggested_action'] = 'request_more_quotes'
            recommendation['recommendation_title'] = 'Solicitar mais cotações'
        recommendation['rfq_batch_id'] = project['project_id']
        recommendation['product_id'] = project.get('product_intelligence_id')
        recommendation['status'] = 'awaiting_user_decision'
        recommendation['reasoning_summary'] = report['reason_summary']
        recommendation['risk_summary'] = ', '.join(project.get('risk_flags') or []) or 'Sem riscos adicionais explicitados'
        record_purchase_recommendation(recommendation)
    except Exception:
        pass
    write_json(report_path, report)
    pdf_path = render_pdf_report(project, records)
    report['pdf_path'] = str(pdf_path)
    write_json(report_path, report)
    project['updated_at'] = now()
    project['status'] = 'reported'
    project['report_path'] = str(report_path)
    project['report_pdf_path'] = str(pdf_path)
    save_project_state(project)
    print('REPORT GENERATED')
    print(f'project_id={project["project_id"]}')
    print(f'report_json={report_path}')
    print(f'report_pdf={pdf_path}')
    return 0


def build_search_queries(project: dict[str, Any]) -> list[str]:
    product = project.get('product') or ''
    region = project.get('region') or ''
    country = project.get('country') or ''
    source_recommendations = project.get('recommended_sources') or []
    queries: list[str] = []
    for source in B2B_SOURCES:
        queries.append(f'site:{source} {product} {country}')
        queries.append(f'site:{source} {product} manufacturer {country}')
    if source_recommendations:
        source_modifiers = {
            'Official website': 'official website',
            'Manufacturer catalog / PDF': 'catalog PDF',
            'Trade fair exhibitor list': 'trade fair exhibitor',
            'Export registry / customs signal': 'export registry',
            'Certification / compliance database': 'certification database',
            'Government / business registry': 'business registry',
            'Authorized distributor / reseller': 'authorized distributor',
            'Association / industry directory': 'industry directory',
        }
        for label in source_recommendations:
            modifier = source_modifiers.get(str(label), str(label).lower())
            queries.append(f'{product} {modifier} {country}')
            queries.append(f'{product} {modifier} manufacturer {country}')
    queries.extend([
        f'{product} manufacturer {region} {country}'.strip(),
        f'{product} factory {country}'.strip(),
        f'{product} OEM ODM manufacturer'.strip(),
        f'{product} -wiki -wikipedia -britannica -guide'.strip(),
    ])
    return [normalize_text(q) for q in queries if normalize_text(q)]


def collect_search_results(project: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen = set()
    for query in build_search_queries(project):
        for hit in ddg_search(query, max_results=5):
            if not is_result_candidate(hit):
                continue
            url = hit.get('url') or ''
            domain = extract_domain(url)
            key = (domain, normalize_text(str(hit.get('title') or '')))
            if key in seen:
                continue
            seen.add(key)
            results.append(hit)
    return results



ADAPTIVE_REGIONS = ['China', 'India', 'Turkey', 'Europe', 'Italy', 'Germany', 'Taiwan']


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = normalize_text(str(value))
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def adaptive_query_bank(product: str) -> dict[str, list[str]]:
    normalized = normalize_text(product)
    lower = normalized.lower()
    exact = normalized or 'industrial cone machine'
    technical = [
        f'{exact} manufacturer',
        f'{exact} factory',
        f'{exact} OEM',
        f'{exact} ODM',
        'semi automatic wafer cone making machine manufacturer',
        'ice cream cone machine factory',
        'wafer cone baking machine OEM',
        'sugar cone production line manufacturer',
        'waffle cone baking machine factory',
        'cone rolling machine manufacturer',
        'wafer cone oven manufacturer',
        'industrial ice cream cone equipment',
        'food processing cone baking equipment',
        'commercial wafer cone production machine',
    ]
    if 'wafer' in lower or 'cone' in lower:
        technical.extend([
            'wafer cone making machine manufacturer',
            'wafer cone production machine factory',
            'wafer cone machine supplier',
            'cone baking machine manufacturer',
        ])
    regional = [f'{query} {region}' for query in technical[:8] for region in ADAPTIVE_REGIONS]
    marketplaces = [
        f'site:{domain} {query}'
        for domain in B2B_SOURCES
        for query in [
            'semi automatic wafer cone making machine',
            'wafer cone machine manufacturer',
            'ice cream cone machine factory',
            'commercial wafer cone production machine',
        ]
    ]
    trade_fair = [
        f'{query} trade fair exhibitor' for query in [
            'semi automatic wafer cone making machine',
            'wafer cone baking machine',
            'ice cream cone machine',
            'cone rolling machine',
        ]
    ] + [
        f'{query} exhibitor list' for query in [
            'food machinery',
            'baking machine',
            'industrial machinery',
            'packaging machinery',
        ]
    ]
    trade_fair += [f'{query} {region} expo' for query in ['wafer cone machine', 'ice cream cone equipment'] for region in ADAPTIVE_REGIONS[:5]]
    broad = [
        'industrial ice cream cone equipment',
        'food processing cone baking equipment',
        'commercial wafer cone production machine',
        'wafer cone production line supplier',
        'cone baking equipment manufacturer',
        'wafer cone machine supplier',
        'ice cream cone production machine',
        'sugar cone equipment manufacturer',
    ]
    broad = broad + [f'{query} {region}' for query in broad[:4] for region in ADAPTIVE_REGIONS]
    return {
        'technical': unique_preserve(technical),
        'regional': unique_preserve(regional),
        'marketplace': unique_preserve(marketplaces),
        'trade_fair': unique_preserve(trade_fair),
        'broad': unique_preserve(broad),
    }


def build_adaptive_round_plan(project: dict[str, Any]) -> list[dict[str, Any]]:
    bank = adaptive_query_bank(project.get('product') or '')
    return [
        {
            'round_number': 1,
            'name': 'official-directories-technical',
            'queries': bank['technical'][:12],
            'sources_used': ['official website', 'industrial directories'],
            'skip_b2b_domains': True,
            'required_terms': ['manufacturer', 'factory', 'oem', 'odm', 'official', 'directory', 'catalog', 'pdf'],
            'reason_for_next_round': 'expand to regional technical variants',
        },
        {
            'round_number': 2,
            'name': 'official-directories-regional',
            'queries': bank['regional'][:12],
            'sources_used': ['official website', 'industrial directories'],
            'skip_b2b_domains': True,
            'required_terms': ['manufacturer', 'factory', 'oem', 'odm', 'official', 'directory', 'catalog', 'pdf'],
            'reason_for_next_round': 'expand to B2B marketplaces',
        },
        {
            'round_number': 3,
            'name': 'marketplace-b2b',
            'queries': bank['marketplace'][:12],
            'sources_used': ['B2B marketplaces'],
            'skip_b2b_domains': False,
            'required_terms': ['manufacturer', 'factory', 'machine', 'cone', 'wafer', 'baking', 'production'],
            'reason_for_next_round': 'expand to trade fair exhibitor lists',
        },
        {
            'round_number': 4,
            'name': 'trade-fair-exhibitors',
            'queries': bank['trade_fair'][:12],
            'sources_used': ['trade fair exhibitor lists'],
            'skip_b2b_domains': False,
            'required_terms': ['exhibitor', 'fair', 'expo', 'trade', 'manufacturer', 'machine'],
            'reason_for_next_round': 'expand to broader web search',
        },
        {
            'round_number': 5,
            'name': 'broad-web-search',
            'queries': bank['broad'][:12],
            'sources_used': ['broad web search'],
            'skip_b2b_domains': False,
            'required_terms': ['cone', 'wafer', 'machine', 'food', 'industrial', 'production'],
            'reason_for_next_round': 'stop: maximum rounds reached',
        },
    ]


def append_adaptive_log(entry: dict[str, Any]) -> None:
    append_jsonl(ADAPTIVE_DISCOVERY_LOG, entry)


def discovery_record_from_candidate(project: dict[str, Any], round_info: dict[str, Any], result: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    profile = candidate.profile if isinstance(candidate.profile, dict) else {}
    evaluation = candidate.score if isinstance(candidate.score, dict) else {}
    return {
        'id': candidate.candidate_id,
        'project_id': project['project_id'],
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'sourcing_strategy_id': project.get('sourcing_strategy_id') or 'Não verificado',
        'round_number': round_info['round_number'],
        'round_name': round_info['name'],
        'source_tier': round_info['name'],
        'sources_used': round_info['sources_used'],
        'query': result.get('query') or '',
        'query_title': result.get('title') or '',
        'query_url': result.get('url') or '',
        'company_name': candidate.company_name,
        'website': candidate.website,
        'profile': profile,
        'evidence': candidate.evidence,
        'source_urls': candidate.source_urls,
        'evidence_paths': candidate.evidence_paths,
        'evaluation': evaluation,
        'manufacturer_score': evaluation.get('manufacturer_score'),
        'supplier_trust_score': evaluation.get('supplier_trust_score'),
        'final_score': evaluation.get('final_score'),
        'qualification_status': evaluation.get('qualification_status'),
        'approved_for_rfq': evaluation.get('approved_for_rfq'),
        'manual_review_required': evaluation.get('manual_review_required'),
        'risk_flags': evaluation.get('risk_flags', []),
        'category_missing_documents': evaluation.get('category_missing_documents', []),
        'category_matched_documents': evaluation.get('category_matched_documents', []),
        'created_at': now(),
        'checked_at': evaluation.get('checked_at') or now(),
        'search_mode': 'adaptive-discovery',
        'discovery_channel': round_info['name'],
    }


def load_discovered_records(project_id: str) -> list[dict[str, Any]]:
    return [record for record in load_records(MANUFACTURER_DISCOVERY_JSONL) if record.get('project_id') == project_id]


def qualify_discovered_candidates(project: dict[str, Any]) -> list[dict[str, Any]]:
    discovered = load_discovered_records(project['project_id'])
    if not discovered:
        return []
    existing_ids = {rec.get('id') for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']}
    qualified: list[dict[str, Any]] = []
    for record in discovered:
        source_tier = str(record.get('source_tier') or '')
        score = float(record.get('final_score') or 0)
        rfq_eligible = score >= 80 and source_tier != 'marketplace-b2b'
        qualification_record = {
            **record,
            'id': record.get('id') or make_id('manufacturer'),
            'updated_at': now(),
            'qualification_status': record.get('qualification_status') or ('Fornecedor Premium' if score >= 80 else 'Fornecedor Promissor' if score >= 60 else 'Fornecedor não qualificado'),
            'approved_for_rfq': rfq_eligible,
            'manual_review_required': bool(record.get('manual_review_required')) or (60 <= score < 80) or source_tier == 'marketplace-b2b',
            'rfq_block_reason': 'marketplace discovery requires manual review' if source_tier == 'marketplace-b2b' and score >= 80 else '',
            'discovery_channel': source_tier,
        }
        if source_tier == 'marketplace-b2b' and score >= 80:
            qualification_record['risk_flags'] = sorted(set((qualification_record.get('risk_flags') or []) + ['Marketplace discovery - RFQ manual only']))
        if qualification_record['id'] not in existing_ids:
            append_jsonl(QUALIFICATION_JSONL, qualification_record)
            existing_ids.add(qualification_record['id'])
        qualified.append(qualification_record)
    return qualified


def run_adaptive_discovery(project: dict[str, Any]) -> dict[str, Any]:
    ensure_project_dirs(project['project_id'])
    MANUFACTURER_DISCOVERY_JSONL.parent.mkdir(parents=True, exist_ok=True)
    MANUFACTURER_DISCOVERY_JSONL.touch(exist_ok=True)
    ADAPTIVE_DISCOVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    ADAPTIVE_DISCOVERY_LOG.touch(exist_ok=True)
    start = time.monotonic()
    seen_keys: set[tuple[str, str]] = set()
    query_hits: dict[str, int] = {}
    source_hits: dict[str, int] = {}
    rounds: list[dict[str, Any]] = []
    discovered_records: list[dict[str, Any]] = []
    all_queries: list[str] = []
    total_queries = 0
    stop_reason = 'completed'
    plan = build_adaptive_round_plan(project)
    for round_info in plan:
        if total_queries >= 60:
            stop_reason = 'stop: maximum query limit reached'
            break
        if time.monotonic() - start >= 20 * 60:
            stop_reason = 'stop: 20 minute execution limit reached'
            break
        round_queries = round_info['queries']
        round_hits_raw = 0
        round_unique_results: list[dict[str, Any]] = []
        round_queries_used: list[str] = []
        for query in round_queries:
            if total_queries >= 60 or time.monotonic() - start >= 20 * 60:
                break
            hits = ddg_search(
                query,
                max_results=5,
                skip_b2b_domains=round_info['skip_b2b_domains'],
                blocked_domains=BAD_DOMAINS,
                required_terms=round_info['required_terms'],
            )
            round_queries_used.append(query)
            all_queries.append(query)
            total_queries += 1
            round_hits_raw += len(hits)
            query_new = 0
            for hit in hits:
                url = hit.get('url') or ''
                key = (extract_domain(url), normalize_text(str(hit.get('title') or '')))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                enriched_hit = dict(hit)
                enriched_hit['adaptive_round'] = round_info['round_number']
                enriched_hit['adaptive_round_name'] = round_info['name']
                enriched_hit['adaptive_source_tier'] = round_info['name']
                enriched_hit['adaptive_sources_used'] = round_info['sources_used']
                round_unique_results.append(enriched_hit)
                query_new += 1
            if query_new:
                query_hits[query] = query_hits.get(query, 0) + query_new
                source_key = ', '.join(round_info['sources_used'])
                source_hits[source_key] = source_hits.get(source_key, 0) + query_new
        pages = fetch_candidate_pages(round_unique_results)
        round_scores: list[float] = []
        for hit in round_unique_results:
            candidate = candidate_from_result(project, hit, pages)
            discovery_record = discovery_record_from_candidate(project, round_info, hit, candidate)
            append_jsonl(MANUFACTURER_DISCOVERY_JSONL, discovery_record)
            discovered_records.append(discovery_record)
            round_scores.append(float(discovery_record.get('final_score') or 0))
        best_candidate_score = max(round_scores) if round_scores else 0.0
        round_summary = {
            'round_number': round_info['round_number'],
            'round_name': round_info['name'],
            'queries_used': round_queries_used,
            'sources_used': round_info['sources_used'],
            'candidates_found': round_hits_raw,
            'candidates_deduplicated': len(round_unique_results),
            'best_candidate_score': best_candidate_score,
            'reason_for_next_round': round_info['reason_for_next_round'],
        }
        rounds.append(round_summary)
        append_adaptive_log({
            'project_id': project['project_id'],
            'created_at': now(),
            **round_summary,
        })
        high_score_count = sum(1 for record in discovered_records if float(record.get('final_score') or 0) > 70)
        if len(discovered_records) >= 10:
            stop_reason = 'stop: at least 10 candidates found'
            break
        if high_score_count >= 3:
            stop_reason = 'stop: at least 3 candidates above 70'
            break
    qualified_records = qualify_discovered_candidates(project)
    high_80 = [rec for rec in discovered_records if float(rec.get('final_score') or 0) >= 80]
    manual_60_79 = [rec for rec in discovered_records if 60 <= float(rec.get('final_score') or 0) < 80]
    blocked = [rec for rec in discovered_records if float(rec.get('final_score') or 0) < 60]
    report = {
        'id': make_id('adaptive_discovery_report'),
        'project_id': project['project_id'],
        'created_at': now(),
        'updated_at': now(),
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'sources_that_worked': sorted(source_hits.items(), key=lambda item: item[1], reverse=True),
        'queries_that_worked': sorted(query_hits.items(), key=lambda item: item[1], reverse=True),
        'rounds': rounds,
        'queries_total': total_queries,
        'candidates_found': len(discovered_records),
        'candidates_deduplicated': len(discovered_records),
        'candidates_above_80': len(high_80),
        'candidates_between_60_and_79': len(manual_60_79),
        'candidates_blocked': len(blocked),
        'qualified_candidates': len(qualified_records),
        'stop_reason': stop_reason,
        'report_file': str(ADAPTIVE_DISCOVERY_REPORT_JSON),
        'log_file': str(ADAPTIVE_DISCOVERY_LOG),
        'discovery_jsonl': str(MANUFACTURER_DISCOVERY_JSONL),
        'query_limit_reached': total_queries >= 60,
        'time_limit_reached': (time.monotonic() - start) >= 20 * 60,
        'round_limit_reached': len(rounds) >= 5,
    }
    write_json(ADAPTIVE_DISCOVERY_REPORT_JSON, report)
    project['updated_at'] = now()
    project['status'] = 'adaptive_discovered'
    project['search_queries'] = all_queries
    project['discovery_rounds'] = rounds
    project['discovery_candidate_ids'] = [record['id'] for record in discovered_records]
    project['qualified_candidate_ids'] = [record['id'] for record in qualified_records if record.get('approved_for_rfq')]
    project['manual_review_candidate_ids'] = [record['id'] for record in qualified_records if record.get('manual_review_required')]
    project['blocked_candidate_ids'] = [record['id'] for record in qualified_records if float(record.get('final_score') or 0) < 60]
    project['adaptive_discovery_report_path'] = str(ADAPTIVE_DISCOVERY_REPORT_JSON)
    save_project_state(project)
    append_jsonl(PROJECTS_JSONL, {
        'id': make_id('adaptive_discovery_summary'),
        'project_id': project['project_id'],
        'created_at': now(),
        'status': 'adaptive_discovered',
        'discovered_count': len(discovered_records),
        'qualified_count': len(qualified_records),
        'stop_reason': stop_reason,
    })
    return report


def fetch_candidate_pages(results: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    pages: dict[str, tuple[str, str]] = {}
    for hit in results[:20]:
        url = str(hit.get('url') or '')
        if not url.startswith('http'):
            continue
        try:
            raw, final_url = fetch_url(url, timeout=25)
            pages[final_url] = (raw, url)
        except Exception:
            continue
    return pages


def candidate_from_result(project: dict[str, Any], result: dict[str, Any], pages: dict[str, tuple[str, str]]) -> Candidate:
    url = str(result.get('url') or '')
    domain = extract_domain(url)
    company_name = company_name_from_result(result)
    candidate_id = make_id('manufacturer')
    page_text = ''
    capture_paths: list[str] = []
    extra_texts: list[str] = []
    final_url = None
    if url and url in pages:
        page_html, original_url = pages[url]
        page_text = strip_html(page_html)
        extra_texts.append(page_text[:2000])
        final_url = original_url
    elif url:
        try:
            page_html, final_url = fetch_url(url, timeout=25)
            page_text = strip_html(page_html)
            extra_texts.append(page_text[:2000])
        except Exception:
            page_text = ''
    evidence_list = []
    evidence_notes: list[str] = []
    if result.get('snippet'):
        evidence_list.append({'url': url, 'source': 'search_snippet', 'checked_at': now(), 'note': result['snippet'], 'confidence': 0.5})
        evidence_notes.append(str(result['snippet']))
    if page_text:
        cap = project_evidence_dir(project['project_id']) / f'{candidate_id}.txt'
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_text(page_text[:20000], encoding='utf-8')
        capture_paths.append(str(cap))
        evidence_list.append({'url': final_url or url, 'source': 'official_site', 'checked_at': now(), 'note': 'Page text captured', 'confidence': 0.8, 'capture_path': str(cap)})
        evidence_notes.append(page_text[:4000])
    openai_data = None
    combined_text = '\n'.join([result.get('title', ''), result.get('snippet', ''), page_text])
    if combined_text.strip():
        try:
            openai_data = openai_profile(project, combined_text, [result])
        except Exception:
            openai_data = None
    profile = build_profile_from_evidence(project, result, page_text, extra_texts)
    if openai_data:
        for key in ['company_name', 'legal_name', 'website', 'telephone', 'whatsapp', 'wechat', 'email', 'address', 'city', 'state', 'country', 'contact_person', 'contact_title', 'company_type', 'products', 'markets', 'international_clients', 'exports', 'certifications', 'catalogs', 'photos', 'videos', 'notes', 'risk_flags']:
            if openai_data.get(key) not in (None, '', [], {}):
                profile[key] = openai_data[key]
        if openai_data.get('confidence_score') is not None:
            profile['confidence_score'] = openai_data['confidence_score']
        if openai_data.get('source_urls'):
            profile['source_urls'] = list(dict.fromkeys([str(u) for u in openai_data['source_urls'] if u]))
    sources = [url] if url else []
    if final_url and final_url not in sources:
        sources.append(final_url)
    qualification = score_profile(project, profile, sources, evidence_notes)
    candidate = Candidate(
        project_id=project['project_id'],
        candidate_id=candidate_id,
        company_name=company_display(profile),
        website=profile.get('website') or url or 'Não verificado',
        source_urls=sources,
        evidence_notes=evidence_notes,
        evidence_paths=capture_paths,
        evidence=evidence_list,
        raw_text=page_text,
        profile=profile,
        score=qualification,
    )
    return candidate


def save_candidate(project: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    record = {
        'id': candidate.candidate_id,
        'project_id': project['project_id'],
        'created_at': now(),
        'updated_at': now(),
        'project_product': project.get('product'),
        'project_region': project.get('region'),
        'project_country': project.get('country'),
        'company_name': candidate.company_name,
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'recommended_sources': project.get('recommended_sources') or [],
        'required_documents': project.get('required_documents') or [],
        'recommended_certifications': project.get('recommended_certifications') or [],
        'qualification_criteria': project.get('qualification_criteria') or [],
        'source_profile_ids': project.get('source_profile_ids') or [],
        'risk_flags': project.get('risk_flags') or [],
        'sourcing_strategy_id': project.get('sourcing_strategy_id') or 'Não verificado',
        'website': candidate.website,
        'profile': candidate.profile,
        'evidence': candidate.evidence,
        'source_urls': candidate.source_urls,
        'evidence_paths': candidate.evidence_paths,
        'checked_at': candidate.score['checked_at'],
        'checked_by': candidate.score['checked_by'],
        'evaluation': candidate.score,
        'manufacturer_score': candidate.score['manufacturer_score'],
        'supplier_trust_score': candidate.score['supplier_trust_score'],
        'final_score': candidate.score['final_score'],
        'qualification_status': candidate.score['qualification_status'],
        'approved_for_rfq': candidate.score['approved_for_rfq'],
        'confidence_score': candidate.score['confidence_score'],
        'risk_flags': candidate.score['risk_flags'],
        'penalties_applied': candidate.score['penalties_applied'],
        'product_match_score': candidate.score['product_match_score'],
        'company_age_years': candidate.score['company_age_years'],
        'company_age_score': candidate.score['company_age_score'],
        'reputation_rating': candidate.score['reputation_rating'],
        'reputation_source': candidate.score['reputation_source'],
        'certifications': candidate.score['certifications'],
        'export_capability_score': candidate.score['export_capability_score'],
        'contact_quality_score': candidate.score['contact_quality_score'],
        'evidence_urls': candidate.score['evidence_urls'],
        'evidence_notes': candidate.score['evidence_notes'],
        'manual_review_required': candidate.score['manual_review_required'],
        'supplier_trust_score_breakdown': candidate.score['supplier_trust_score_breakdown'],
        'supplier_trust_status': candidate.score['supplier_trust_status'],
        'supplier_trust_confidence': candidate.score['supplier_trust_confidence'],
        'supplier_history_count': candidate.score['supplier_history_count'],
    }
    append_jsonl(RESEARCH_JSONL, record)
    append_jsonl(QUALIFICATION_JSONL, record)
    profile = candidate.profile or {}
    try:
        record_purchase_company({
            'id': candidate.candidate_id,
            'legal_name': profile.get('legal_name') or candidate.company_name,
            'trade_name': profile.get('company_name') or candidate.company_name,
            'country': profile.get('country') or project.get('country') or '',
            'city': profile.get('city') or '',
            'website': candidate.website or profile.get('website') or '',
            'email': profile.get('email') or '',
            'phone': profile.get('telephone') or '',
            'status': 'contact_validated' if profile.get('email') or profile.get('telephone') else 'contact_pending',
            'notes': profile.get('notes') or '',
        })
        record_purchase_product({
            'id': project.get('product_intelligence_id') or project['project_id'],
            'company_id': candidate.candidate_id,
            'name': project.get('product') or profile.get('products', [''])[0] or 'Produto',
            'technical_spec': project.get('product') or profile.get('notes') or '',
            'sku': '',
            'category': project.get('category_label') or project.get('category') or '',
            'unit': '',
            'target_price': None,
            'currency': 'USD',
            'incoterm': '',
            'moq': None,
            'notes': f'Candidate saved from {project["project_id"]}',
        })
        record_purchase_contact({
            'id': candidate.candidate_id + ':contact',
            'company_id': candidate.candidate_id,
            'name': profile.get('contact_person') or profile.get('contact_name') or candidate.company_name,
            'title': profile.get('contact_title') or '',
            'email': profile.get('email') or '',
            'phone': profile.get('telephone') or '',
            'channel': 'email',
            'status': 'contact_validated' if profile.get('email') else 'contact_pending',
            'notes': profile.get('notes') or '',
        })
    except Exception:
        pass
    return record


def cmd_search_manufacturers(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found; create one first')
    ensure_project_dirs(project['project_id'])
    intel_bundle = project_intelligence(project)
    if not project.get('product_intelligence_id'):
        intel_bundle = run_global_product_intelligence(project.get('product') or '', project.get('region') or 'global', project.get('country') or 'global')
        project = apply_intelligence_to_project(project, intel_bundle)
        project['updated_at'] = now()
        append_jsonl(PROJECTS_JSONL, project)
        save_project_state(project)
    elif intel_bundle.get('product_intelligence'):
        project = apply_intelligence_to_project(project, intel_bundle)
        project['updated_at'] = now()
        append_jsonl(PROJECTS_JSONL, project)
        save_project_state(project)
    results = collect_search_results(project)
    pages = fetch_candidate_pages(results)
    candidate_records: list[dict[str, Any]] = []
    for result in results:
        candidate = candidate_from_result(project, result, pages)
        candidate_records.append(save_candidate(project, candidate))
    project['updated_at'] = now()
    project['status'] = 'searched'
    project['search_queries'] = build_search_queries(project)
    project['candidate_ids'] = [rec['id'] for rec in candidate_records]
    save_project_state(project)
    print('SEARCH COMPLETED')
    print(f'project_id={project["project_id"]}')
    print(f'candidates={len(candidate_records)}')
    print(f'queries={len(project["search_queries"])}')
    return 0


def qualify_candidates(project: dict[str, Any]) -> list[dict[str, Any]]:
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not records:
        return []
    qualified = []
    for record in records:
        if record.get('approved_for_rfq'):
            qualified.append(record)
    return qualified


def render_pdf_report(project: dict[str, Any], records: list[dict[str, Any]]) -> Path:
    doc = PdfDocument(landscape=True)
    doc.set_logo(ASSETS_DIR / 'logo.png')
    page = doc.add_page()
    width, height = doc.width, doc.height
    margin = 28
    y = height - 36
    if doc.logo is not None:
        page.image('Im0', margin, height - 86, 82, 46)
        x0 = margin + 94
    else:
        x0 = margin
    for idx, line in enumerate(company_display_lines()):
        page.text(x0, y - idx * 14, line, size=12 if idx == 0 else 9, font='F3' if idx == 0 else 'F1')
    page.text(x0, y - 42, 'Relatório de Qualificação de Fabricantes', size=15, font='F3')
    page.line(margin, height - 98, width - margin, height - 98, width=1.0)
    summary_y = height - 120
    project_lines = [
        f'Projeto: {project["project_id"]}',
        f'Product Intelligence: {project.get("product_intelligence_id") or "Não verificado"}',
        f'Categoria: {project.get("category_label") or project.get("category") or "Não verificado"}',
        f'Subcategoria: {project.get("subcategory") or "Não verificado"}',
        f'Compliance: {project.get("compliance_rule_id") or "Não verificado"}',
        f'Documentos obrigatórios: {", ".join((project.get("required_documents") or [])[:4]) or "Não verificado"}',
        f'Fontes recomendadas: {", ".join((project.get("recommended_sources") or [])[:3]) or "Não verificado"}',
        f'Critérios aplicados: {", ".join((project.get("qualification_criteria") or [])[:3]) or "Não verificado"}',
        f'Produto: {project.get("product")}',
        f'Região: {project.get("region") or "Não verificado"}',
        f'País: {project.get("country") or "Não verificado"}',
        f'Candidados avaliados: {len(records)}',
    ]
    for idx, line in enumerate(project_lines):
        page.text(margin, summary_y - idx * 13, line, size=9, font='F2')
    headers = ['Empresa', 'Mfg', 'Trust', 'Final', 'Status', 'Riscos']
    x_positions = [28, 300, 370, 440, 520, 640]
    table_y = height - 206
    for idx, h in enumerate(headers):
        page.text(x_positions[idx], table_y, h, size=8, font='F3')
    table_y -= 14
    for record in sorted(records, key=lambda r: r.get('manufacturer_score', 0), reverse=True):
        if table_y < 92:
            break
        page.text(x_positions[0], table_y, company_display(record.get('profile', record))[:42], size=8, font='F2')
        page.text(x_positions[1], table_y, str(record.get('manufacturer_score', '-')), size=8, font='F2')
        page.text(x_positions[2], table_y, str(record.get('supplier_trust_score', '-')), size=8, font='F2')
        page.text(x_positions[3], table_y, str(final_supplier_score(record)), size=8, font='F2')
        page.text(x_positions[4], table_y, str(record.get('qualification_status', '-'))[:15], size=8, font='F2')
        risks = ', '.join((record.get('risk_flags') or [])[:2]) or '-'
        page.text(x_positions[5], table_y, risks[:22], size=8, font='F2')
        table_y -= 12
    footer_y = 20
    page.line(margin, footer_y + 10, width - margin, footer_y + 10, width=0.8)
    page.text(margin, footer_y, POLAR_SINERGY['footer'], size=8, font='F2')
    report_path = project_report_dir(project['project_id']) / f'{safe_slug(project["project_id"])}.pdf'
    doc.save(report_path)
    return report_path


def cmd_qualify(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not records:
        raise SystemExit('no candidates found; run search-manufacturers first')
    qualified = [rec for rec in records if rec.get('approved_for_rfq')]
    project['updated_at'] = now()
    project['status'] = 'qualified'
    project['approved_candidate_ids'] = [rec['id'] for rec in qualified]
    save_project_state(project)
    print('QUALIFICATION COMPLETED')
    print(f'project_id={project["project_id"]}')
    print(f'qualified={len(qualified)}')
    print(f'needs_review={len(records) - len(qualified)}')
    return 0


def cmd_qualify_discovered_candidates(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    qualified = qualify_discovered_candidates(project)
    if not qualified:
        print('NO DISCOVERED CANDIDATES TO QUALIFY')
        return 0
    qualified_ids = [rec['id'] for rec in qualified if rec.get('approved_for_rfq')]
    manual_ids = [rec['id'] for rec in qualified if rec.get('manual_review_required')]
    blocked_ids = [rec['id'] for rec in qualified if float(rec.get('final_score') or 0) < 60]
    project['updated_at'] = now()
    project['status'] = 'discovery_qualified'
    project['qualified_candidate_ids'] = qualified_ids
    project['manual_review_candidate_ids'] = manual_ids
    project['blocked_candidate_ids'] = blocked_ids
    save_project_state(project)
    print('DISCOVERED CANDIDATES QUALIFIED')
    print(f'project_id={project["project_id"]}')
    print(f'qualified={len(qualified_ids)}')
    print(f'manual_review={len(manual_ids)}')
    print(f'blocked={len(blocked_ids)}')
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not records:
        raise SystemExit('no candidate records available')
    intelligence = project_intelligence(project)
    category_profile = intelligence.get('category_profile') or {}
    product_intelligence = intelligence.get('product_intelligence') or {}
    sourcing_strategy = intelligence.get('sourcing_strategy') or {}
    report = {
        'id': make_id('sourcing_report'),
        'project_id': project['project_id'],
        'created_at': now(),
        'updated_at': now(),
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'sourcing_strategy_id': project.get('sourcing_strategy_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'mandatory_documents': project.get('required_documents') or [],
        'recommended_sources': project.get('recommended_sources') or [],
        'qualification_criteria': project.get('qualification_criteria') or [],
        'source_profile_ids': project.get('source_profile_ids') or [],
        'recommended_certifications': project.get('recommended_certifications') or [],
        'risk_flags': project.get('risk_flags') or [],
        'category_profile': category_profile,
        'product_intelligence': product_intelligence,
        'sourcing_strategy': sourcing_strategy,
        'reason_summary': {
            'gate_rule': 'final_score = min(manufacturer_score, supplier_trust_score)',
            'auto_rfq_threshold': '>= 80',
            'manual_review_threshold': '60-79',
            'block_threshold': '< 60',
            'selected_sources': project.get('recommended_sources') or [],
            'required_documents': project.get('required_documents') or [],
            'qualification_criteria': project.get('qualification_criteria') or [],
        },
        'search_mode': project.get('search_mode') or 'Não verificado',
        'product': project.get('product'),
        'region': project.get('region'),
        'country': project.get('country'),
        'candidate_count': len(records),
        'qualified_count': sum(1 for rec in records if rec.get('approved_for_rfq')),
        'manual_review_count': sum(1 for rec in records if rec.get('manual_review_required')),
        'premium_count': sum(1 for rec in records if (final_supplier_score(rec)) >= 80),
        'promising_count': sum(1 for rec in records if 60 <= (final_supplier_score(rec)) < 80),
        'rejected_count': sum(1 for rec in records if (final_supplier_score(rec)) < 60),
        'top_candidates': sorted(records, key=lambda r: final_supplier_score(r), reverse=True)[:10],
        'telegram_review_queue': [build_telegram_review_payload(rec) for rec in records if 60 <= (final_supplier_score(rec)) < 80],
    }
    report_path = project_report_dir(project['project_id']) / f'{safe_slug(project["project_id"])}.json'
    try:
        recommendation = build_purchase_recommendation(
            product_name=project.get('product') or 'Produto',
            supplier_name=report['top_candidates'][0].get('company_name') if report['top_candidates'] else 'Fornecedor',
            supplier_country=project.get('country'),
            supplier_city=project.get('region'),
            notes=[
                f'qualified={report["qualified_count"]}',
                f'manual_review={report["manual_review_count"]}',
                f'premium={report["premium_count"]}',
                f'blocked={report["rejected_count"]}',
            ],
        )
        if report['qualified_count'] and report['manual_review_count'] == 0 and report['rejected_count'] == 0:
            recommendation['suggested_action'] = 'authorize_all_suppliers'
            recommendation['recommendation_title'] = 'Autorizar envio para fornecedores qualificados'
        elif report['manual_review_count']:
            recommendation['suggested_action'] = 'review_rfq_before_send'
            recommendation['recommendation_title'] = 'Revisar RFQ antes de enviar'
        else:
            recommendation['suggested_action'] = 'request_more_quotes'
            recommendation['recommendation_title'] = 'Solicitar mais cotações'
        recommendation['rfq_batch_id'] = project['project_id']
        recommendation['product_id'] = project.get('product_intelligence_id')
        recommendation['status'] = 'awaiting_user_decision'
        recommendation['reasoning_summary'] = report['reason_summary']
        recommendation['risk_summary'] = ', '.join(project.get('risk_flags') or []) or 'Sem riscos adicionais explicitados'
        record_purchase_recommendation(recommendation)
    except Exception:
        pass
    write_json(report_path, report)
    pdf_path = render_pdf_report(project, records)
    report['pdf_path'] = str(pdf_path)
    write_json(report_path, report)
    project['updated_at'] = now()
    project['status'] = 'reported'
    project['report_path'] = str(report_path)
    project['report_pdf_path'] = str(pdf_path)
    save_project_state(project)
    print('REPORT GENERATED')
    print(f'project_id={project["project_id"]}')
    print(f'report_json={report_path}')
    print(f'report_pdf={pdf_path}')
    return 0


def build_rfq_draft(project: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    profile = record.get('profile', {})
    return {
        'id': make_id('rfq_draft'),
        'project_id': project['project_id'],
        'product_intelligence_id': project.get('product_intelligence_id') or 'Não verificado',
        'category_id': project.get('category_id') or 'Não verificado',
        'category_label': project.get('category_label') or 'Não verificado',
        'subcategory': project.get('subcategory') or 'Não verificado',
        'compliance_rule_id': project.get('compliance_rule_id') or 'Não verificado',
        'source_profile_ids': project.get('source_profile_ids') or [],
        'recommended_sources': project.get('recommended_sources') or [],
        'required_documents': project.get('required_documents') or [],
        'recommended_certifications': project.get('recommended_certifications') or [],
        'qualification_criteria': project.get('qualification_criteria') or [],
        'sourcing_strategy_id': project.get('sourcing_strategy_id') or 'Não verificado',
        'manufacturer_id': record.get('id'),
        'created_at': now(),
        'product': project.get('product'),
        'region': project.get('region'),
        'country': project.get('country'),
        'company_name': profile.get('company_name', record.get('company_name')),
        'website': profile.get('website', record.get('website')),
        'contact_person': profile.get('contact_person', 'Não verificado'),
        'contact_title': profile.get('contact_title', 'Não verificado'),
        'email': profile.get('email', 'Não verificado'),
        'telephone': profile.get('telephone', 'Não verificado'),
        'approved_for_rfq': record.get('approved_for_rfq'),
        'manufacturer_score': record.get('manufacturer_score'),
        'qualification_status': record.get('qualification_status'),
        'rfq_subject': f'RFQ Request - {project.get("product")} ',
        'rfq_body': (
            f'Hello {profile.get("contact_person", "sales team")},\n\n'
            f'We would like to request a quotation for: {project.get("product")}\n'
            f'Category: {project.get("category_label") or project.get("category") or "Not verified"}\n'
            f'Subcategory: {project.get("subcategory") or "Not verified"}\n'
            f'Sourcing region: {project.get("region") or "Not verified"}\n'
            f'Preferred country: {project.get("country") or "Not verified"}\n\n'
            f'Please confirm price, MOQ, lead time, Incoterm, payment terms, warranty, and certifications.\n\n'
            f'This draft was generated in dry_run mode.'
        ),
        'risk_flags': record.get('risk_flags', []),
        'product_intelligence_snapshot': project.get('product_intelligence_snapshot') or {},
        'category_profile_snapshot': project.get('category_profile_snapshot') or {},
        'sourcing_strategy_snapshot': project.get('sourcing_strategy_snapshot') or {},
    }


def cmd_approve_for_rfq(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    eligible = [rec for rec in records if rec.get('approved_for_rfq') and final_supplier_score(rec) >= 80]
    if not eligible:
        print('NO MANUFACTURERS APPROVED FOR RFQ')
        return 0
    drafts = []
    for rec in eligible:
        draft = build_rfq_draft(project, rec)
        path = project_rfq_dir(project['project_id']) / f"{safe_slug(draft['company_name'])}-{draft['id']}.json"
        write_json(path, draft)
        append_jsonl(RFQ_DRAFTS_JSONL, {**draft, 'draft_path': str(path), 'mode': 'auto_approved'})
        drafts.append({**draft, 'draft_path': str(path)})
    project['updated_at'] = now()
    project['status'] = 'rfq_approved'
    project['approved_candidate_ids'] = [rec['id'] for rec in eligible]
    project['rfq_draft_paths'] = [draft['draft_path'] for draft in drafts]
    save_project_state(project)
    try:
        batch_id = make_id('rfq_batch')
        record_rfq_batch({
            'id': batch_id,
            'project_id': project['project_id'],
            'product_id': project.get('product_intelligence_id') or project.get('product') or '',
            'company_id': project.get('project_id'),
            'authorized_at': now(),
            'authorized_by': 'approval_queue',
            'authorization_scope': 'all_eligible_suppliers',
            'status': 'authorized',
            'user_decision_required': True,
            'user_decision': 'approved',
            'supplier_count': len(drafts),
            'authorized_supplier_ids': [rec['id'] for rec in eligible],
            'rfq_draft_paths': [draft['draft_path'] for draft in drafts],
            'notes': f'RFQ approval for project {project["project_id"]}',
        })
        for draft, rec in zip(drafts, eligible, strict=False):
            record_rfq_batch_supplier({
                'id': f"{batch_id}:{draft['id']}",
                'rfq_batch_id': batch_id,
                'supplier_company_id': rec.get('id') or draft.get('supplier_id') or '',
                'contact_id': draft.get('contact_id') or '',
                'country': rec.get('profile', {}).get('country') or project.get('country') or '',
                'city': rec.get('profile', {}).get('city') or '',
                'website': rec.get('website') or '',
                'contact_value': rec.get('profile', {}).get('email') or '',
                'contact_status': 'contato_validado' if rec.get('profile', {}).get('email') else 'contato_pendente',
                'observation': f'Authorized for RFQ: {draft["id"]}',
                'email_status': 'pendente_envio',
                'notes': rec.get('qualification_status') or '',
            })
    except Exception:
        pass
    append_jsonl(PROJECTS_JSONL, {
        'id': make_id('sourcing_approval'),
        'project_id': project['project_id'],
        'created_at': now(),
        'approved_candidate_ids': project['approved_candidate_ids'],
        'rfq_draft_paths': project['rfq_draft_paths'],
        'status': 'rfq_approved',
    })
    print('RFQ APPROVALS CREATED')
    print(f'project_id={project["project_id"]}')
    print(f'approved={len(drafts)}')
    for draft in drafts[:5]:
        print(f'draft={draft["draft_path"]}')
    return 0


def cmd_generate_rfq(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    manual_ids = set(project.get('manual_approved_candidate_ids', []))
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id'] and (rec.get('approved_for_rfq') or rec.get('id') in manual_ids)]
    if not records:
        raise SystemExit('no approved manufacturers to generate RFQ drafts')
    generated = []
    for rec in records:
        draft = build_rfq_draft(project, rec)
        path = project_rfq_dir(project['project_id']) / f"{safe_slug(draft['company_name'])}-rfq.json"
        write_json(path, draft)
        append_jsonl(RFQ_DRAFTS_JSONL, {**draft, 'draft_path': str(path), 'mode': 'manual_review' if not rec.get('approved_for_rfq') else 'auto_approved'})
        generated.append(str(path))
    project['updated_at'] = now()
    project['status'] = 'rfq_generated'
    project['rfq_draft_paths'] = generated
    save_project_state(project)
    print('RFQ DRAFTS GENERATED')
    print(f'project_id={project["project_id"]}')
    print(f'count={len(generated)}')
    for path in generated[:5]:
        print(path)
    return 0


def cmd_record_performance(args: argparse.Namespace) -> int:
    record_base = {
        'id': make_id('supplier_perf'),
        'created_at': now(),
        'updated_at': now(),
        'supplier_id': args.supplier_id,
        'company_name': args.company_name or 'Não verificado',
        'website': args.website or 'Não verificado',
        'response_time_hours': args.response_time_hours,
        'rfq_response_rate': args.rfq_response_rate,
        'technical_quality': args.technical_quality,
        'spec_accuracy': args.spec_accuracy,
        'price_competitiveness': args.price_competitiveness,
        'lead_time_compliance': args.lead_time_compliance,
        'service_quality': args.service_quality,
        'communication_ease': args.communication_ease,
        'purchase_success_rate': args.purchase_success_rate,
        'reorder_rate': args.reorder_rate,
        'manual_rating': args.manual_rating,
    }
    history_profile = {
        'company_name': record_base['company_name'],
        'legal_name': record_base['company_name'],
        'website': record_base['website'],
        'telephone': 'Não verificado',
        'email': 'Não verificado',
        'company_type': 'Fabricante',
        'products': ['Não verificado'],
        'notes': 'Histórico interno de desempenho do fornecedor',
    }
    history = performance_history_for_supplier(history_profile, supplier_id=args.supplier_id)
    history_with_current = history + [record_base]
    trust_data = calculate_supplier_trust_score({}, history_profile, [], [], supplier_id=args.supplier_id, history_records=history_with_current)
    record = {**record_base}
    record['supplier_performance_score'] = round_money(
        sum([v for v in [args.technical_quality, args.spec_accuracy, args.price_competitiveness, args.lead_time_compliance, args.service_quality, args.communication_ease, args.purchase_success_rate, args.reorder_rate, args.manual_rating] if isinstance(v, (int, float))])
        / max(len([v for v in [args.technical_quality, args.spec_accuracy, args.price_competitiveness, args.lead_time_compliance, args.service_quality, args.communication_ease, args.purchase_success_rate, args.reorder_rate, args.manual_rating] if isinstance(v, (int, float))]), 1)
    ) if any(isinstance(v, (int, float)) for v in [args.technical_quality, args.spec_accuracy, args.price_competitiveness, args.lead_time_compliance, args.service_quality, args.communication_ease, args.purchase_success_rate, args.reorder_rate, args.manual_rating]) else 'Não verificado'
    record['supplier_trust_score'] = trust_data['supplier_trust_score']
    record['supplier_trust_status'] = trust_data['supplier_trust_status']
    record['supplier_trust_confidence'] = trust_data['supplier_trust_confidence']
    record['supplier_trust_score_breakdown'] = trust_data['supplier_trust_score_breakdown']
    record['supplier_history_count'] = trust_data['supplier_history_count']
    record['supplier_history_average'] = trust_data['supplier_history_average']
    record['supplier_history_timeline'] = trust_data['supplier_history_timeline']
    record['supplier_history_latest'] = trust_data['supplier_history_latest']
    append_jsonl(PERFORMANCE_JSONL, record)
    print('SUPPLIER PERFORMANCE RECORDED')
    print(f'supplier_id={args.supplier_id}')
    print(f'performance_score={record["supplier_performance_score"]}')
    print(f'trust_score={record["supplier_trust_score"]}')
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    for path in [
        ROOT, SOURCING_PROJECTS_DIR, SOURCING_REPORTS_DIR, RFQ_OUTPUT_DIR,
        PROJECTS_JSONL, RESEARCH_JSONL, QUALIFICATION_JSONL, PERFORMANCE_JSONL,
        PRODUCT_INTELLIGENCE_JSONL, PRODUCT_CATEGORIES_JSONL, PRODUCT_COMPLIANCE_RULES_JSONL,
        PRODUCT_SOURCING_SOURCES_JSONL, PROCUREMENT_KB_JSONL, RFQ_DRAFTS_JSONL,
    ]:
        if isinstance(path, Path):
            if not path.exists():
                errors.append(f'missing path: {path}')
        else:
            if not Path(path).exists():
                errors.append(f'missing file: {path}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'projects_jsonl={PROJECTS_JSONL}')
    print(f'research_jsonl={RESEARCH_JSONL}')
    print(f'qualification_jsonl={QUALIFICATION_JSONL}')
    print(f'performance_jsonl={PERFORMANCE_JSONL}')
    return 0


def build_telegram_review_payload(record: dict[str, Any]) -> dict[str, Any]:
    profile = record.get('profile', {}) if isinstance(record.get('profile'), dict) else {}
    company_name = profile.get('company_name') or record.get('company_name') or 'Não verificado'
    buttons = [
        {'text': '✅ Aprovar RFQ', 'action': 'approve_rfq'},
        {'text': '❌ Rejeitar', 'action': 'reject_rfq'},
        {'text': '🔎 Ver relatório completo', 'action': 'view_report'},
        {'text': '🌐 Abrir website', 'action': 'open_website', 'url': profile.get('website') or record.get('website') or ''},
        {'text': '📂 Ver catálogo', 'action': 'view_catalog', 'catalogs': profile.get('catalogs') or []},
        {'text': '👤 Ver contato', 'action': 'view_contact', 'contact_person': profile.get('contact_person', 'Não verificado'), 'email': profile.get('email', 'Não verificado'), 'telephone': profile.get('telephone', 'Não verificado')},
    ]
    return {
        'company_name': company_name,
        'product_intelligence_id': record.get('product_intelligence_id', 'Não verificado'),
        'category_label': record.get('category_label', 'Não verificado'),
        'subcategory': record.get('subcategory', 'Não verificado'),
        'mandatory_documents': record.get('required_documents', []),
        'recommended_sources': record.get('recommended_sources', []),
        'qualification_criteria': record.get('qualification_criteria', []),
        'country': profile.get('country', record.get('project_country', 'Não verificado')),
        'city': profile.get('city', 'Não verificado'),
        'website': profile.get('website', record.get('website', 'Não verificado')),
        'company_age_years': record.get('company_age_years', profile.get('company_age_years', 'Não verificado')),
        'products': profile.get('products', ['Não verificado']),
        'evidence_urls': record.get('evidence_urls', []),
        'certifications': record.get('certifications', []),
        'manufacturer_score': record.get('manufacturer_score', 0),
        'supplier_trust_score': effective_supplier_trust_score(record),
        'final_score': final_supplier_score(record),
        'qualification_status': record.get('qualification_status', 'Não verificado'),
        'reason': record.get('reason_summary') or {
            'gate_rule': 'final_score = min(manufacturer_score, supplier_trust_score)',
            'category_compliance_score': record.get('category_compliance_score', 'Não verificado'),
            'category_missing_documents': record.get('category_missing_documents', []),
        },
        'score_breakdown': record.get('score_breakdown', {}),
        'supplier_trust_score_breakdown': record.get('supplier_trust_score_breakdown', {}),
        'strengths': [
            'Evidência de fabricante' if record.get('score_breakdown', {}).get('evidencia_fabricacao_propria', 0) >= 10 else 'Evidência limitada',
            'Contato verificável' if record.get('contact_quality_score', 0) >= 3 else 'Contato limitado',
        ],
        'weaknesses': record.get('penalties_applied', []) or ['Sem penalidades explícitas registradas'],
        'risk_flags': record.get('risk_flags', []),
        'buttons': buttons,
        'report_json': record.get('report_json', 'Não verificado'),
        'report_pdf': record.get('report_pdf', 'Não verificado'),
    }


def build_supplier_dashboard(record: dict[str, Any]) -> dict[str, Any]:
    profile = record.get('profile', {}) if isinstance(record.get('profile'), dict) else {}
    final_score = final_supplier_score(record)
    if final_score >= 80:
        status = '🟢 Premium'
    elif final_score >= 60:
        status = '🟡 Revisão Manual'
    else:
        status = '🔴 Bloqueado'
    return {
        'company_name': profile.get('company_name', record.get('company_name', 'Não verificado')),
        'manufacturer_score': record.get('manufacturer_score', 'Não verificado'),
        'supplier_trust_score': effective_supplier_trust_score(record),
        'final_score': final_supplier_score(record),
        'last_updated': record.get('updated_at', 'Não verificado'),
        'confidence_level': record.get('confidence_score', 'Não verificado'),
        'purchase_history': {
            'total_value': record.get('value_total_negotiated', 'Não verificado'),
            'rfqs_sent': record.get('rfqs_sent', 'Não verificado'),
            'responses': record.get('responses_count', 'Não verificado'),
            'response_rate': record.get('response_rate', 'Não verificado'),
            'avg_response_time': record.get('avg_response_time_hours', 'Não verificado'),
            'avg_production_time': record.get('avg_production_time_days', 'Não verificado'),
            'avg_delivery_time': record.get('avg_delivery_time_days', 'Não verificado'),
            'orders_placed': record.get('orders_placed', 'Não verificado'),
            'orders_completed': record.get('orders_completed', 'Não verificado'),
            'orders_cancelled': record.get('orders_cancelled', 'Não verificado'),
            'last_trade_fair': record.get('last_trade_fair', 'Não verificado'),
            'last_meeting': record.get('last_meeting', 'Não verificado'),
            'last_contact': record.get('last_contact', 'Não verificado'),
        },
        'status': status,
        'history_timeline': record.get('supplier_history_timeline', []),
        'history_count': record.get('supplier_history_count', 0),
    }


def effective_supplier_trust_score(record: dict[str, Any]) -> float:
    trust = record.get('supplier_trust_score')
    if isinstance(trust, (int, float)):
        return float(trust)
    profile = record.get('profile', {}) if isinstance(record.get('profile'), dict) else {}
    evidence_urls = record.get('evidence_urls') or record.get('source_urls') or []
    evidence_notes = record.get('evidence_notes') or []
    try:
        trust_data = calculate_supplier_trust_score({}, profile, list(evidence_urls), list(evidence_notes))
        return float(trust_data.get('supplier_trust_score', 0.0))
    except Exception:
        manufacturer = record.get('manufacturer_score', 0)
        return float(manufacturer if isinstance(manufacturer, (int, float)) else 0.0)


def final_supplier_score(record: dict[str, Any]) -> float:
    if 'final_score' in record and isinstance(record.get('final_score'), (int, float)):
        return float(record['final_score'])
    manufacturer = record.get('manufacturer_score', 0)
    if not isinstance(manufacturer, (int, float)):
        try:
            manufacturer = float(manufacturer)
        except Exception:
            manufacturer = 0.0
    trust = effective_supplier_trust_score(record)
    return float(min(float(manufacturer), float(trust)))


def cmd_dashboard(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if getattr(args, 'project_id', '') else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    if not records:
        raise SystemExit('no candidate records available')
    if args.company_name:
        target = next((rec for rec in records if normalize_text(str(rec.get('company_name') or '')).casefold() == normalize_text(args.company_name).casefold()), None)
        if not target:
            raise SystemExit('company not found in project records')
    else:
        target = max(records, key=lambda r: (final_supplier_score(r), r.get('manufacturer_score', 0), effective_supplier_trust_score(r)))
    dashboard = build_supplier_dashboard(target)
    print(json.dumps(dashboard, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_manual_approve(args: argparse.Namespace) -> int:
    project = lookup_project(args.project_id) if args.project_id else latest_project()
    if not project:
        raise SystemExit('no sourcing project found')
    records = [rec for rec in load_records(QUALIFICATION_JSONL) if rec.get('project_id') == project['project_id']]
    target = next((rec for rec in records if rec.get('id') == args.candidate_id), None)
    if not target:
        raise SystemExit('candidate not found')
    final_score = final_supplier_score(target)
    if final_score < 60:
        raise SystemExit('candidate is below manual-approval threshold')
    manual_ids = set(project.get('manual_approved_candidate_ids', []))
    manual_ids.add(args.candidate_id)
    project['manual_approved_candidate_ids'] = sorted(manual_ids)
    project['updated_at'] = now()
    save_project_state(project)
    append_jsonl(PROJECTS_JSONL, {
        'id': make_id('sourcing_manual_approval'),
        'project_id': project['project_id'],
        'created_at': now(),
        'candidate_id': args.candidate_id,
        'final_score': final_score,
        'status': 'manual_approved',
    })
    print('MANUAL APPROVAL RECORDED')
    print(f'project_id={project["project_id"]}')
    print(f'candidate_id={args.candidate_id}')
    print(f'final_score={final_score}')
    return 0

def cmd_stats(_: argparse.Namespace) -> int:
    projects = load_records(PROJECTS_JSONL)
    research = load_records(RESEARCH_JSONL)
    qual = load_records(QUALIFICATION_JSONL)
    perf = load_records(PERFORMANCE_JSONL)
    approved = sum(1 for rec in qual if rec.get('approved_for_rfq'))
    premium = sum(1 for rec in qual if (final_supplier_score(rec)) >= 80)
    promising = sum(1 for rec in qual if 60 <= (final_supplier_score(rec)) < 80)
    rejected = sum(1 for rec in qual if (final_supplier_score(rec)) < 60)
    print('SOURCING MODULE STATS')
    print(f'projects_total: {len(projects)}')
    print(f'research_records: {len(research)}')
    print(f'qualification_records: {len(qual)}')
    print(f'approved_for_rfq: {approved}')
    print(f'premium_suppliers: {premium}')
    print(f'promising_suppliers: {promising}')
    print(f'rejected_suppliers: {rejected}')
    print(f'supplier_performance_records: {len(perf)}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Module 3 sourcing and qualification workflow')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate directories and JSONL stores')
    p_validate.set_defaults(func=cmd_validate)

    p_create = sub.add_parser('create-project', help='Create a sourcing project')
    p_create.add_argument('--product')
    p_create.add_argument('--region', default='')
    p_create.add_argument('--country', default='')
    p_create.add_argument('--category', default='')
    p_create.add_argument('--telegram-text', default='')
    p_create.set_defaults(func=cmd_create_project)

    p_run = sub.add_parser('run-with-intelligence', help='Run the full sourcing workflow with Global Product Intelligence')
    p_run.add_argument('--product', default='')
    p_run.add_argument('--region', default='')
    p_run.add_argument('--country', default='')
    p_run.add_argument('--category', default='')
    p_run.add_argument('--telegram-text', default='')
    p_run.set_defaults(func=cmd_run_with_intelligence)

    p_adaptive = sub.add_parser('adaptive-discovery', help='Run adaptive sourcing discovery until candidates are found or limits are reached')
    p_adaptive.add_argument('--project-id', default='')
    p_adaptive.add_argument('--product', default='')
    p_adaptive.add_argument('--region', default='')
    p_adaptive.add_argument('--country', default='')
    p_adaptive.add_argument('--category', default='')
    p_adaptive.add_argument('--telegram-text', default='')
    p_adaptive.set_defaults(func=cmd_adaptive_discovery)

    p_search = sub.add_parser('search-manufacturers', help='Search public web sources for candidate manufacturers')
    p_search.add_argument('--project-id', default='')
    p_search.set_defaults(func=cmd_search_manufacturers)

    p_qualify = sub.add_parser('qualify', help='Qualify stored manufacturer candidates')
    p_qualify.add_argument('--project-id', default='')
    p_qualify.set_defaults(func=cmd_qualify)

    p_qualify_discovered = sub.add_parser('qualify-discovered-candidates', help='Qualify candidates discovered by adaptive-discovery')
    p_qualify_discovered.add_argument('--project-id', default='')
    p_qualify_discovered.set_defaults(func=cmd_qualify_discovered_candidates)

    p_report = sub.add_parser('report', help='Generate audit report and PDF')
    p_report.add_argument('--project-id', default='')
    p_report.set_defaults(func=cmd_report)

    p_approve = sub.add_parser('approve-for-rfq', help='Approve qualified manufacturers for RFQ')
    p_approve.add_argument('--project-id', default='')
    p_approve.set_defaults(func=cmd_approve_for_rfq)

    p_rfq = sub.add_parser('generate-rfq', help='Generate RFQ drafts for approved manufacturers')
    p_rfq.add_argument('--project-id', default='')
    p_rfq.set_defaults(func=cmd_generate_rfq)

    p_perf = sub.add_parser('record-performance', help='Record supplier performance metrics')
    p_perf.add_argument('--supplier-id', required=True)
    p_perf.add_argument('--company-name', default='')
    p_perf.add_argument('--website', default='')
    p_perf.add_argument('--response-time-hours', type=float, default=None)
    p_perf.add_argument('--rfq-response-rate', type=float, default=None)
    p_perf.add_argument('--technical-quality', type=float, default=None)
    p_perf.add_argument('--spec-accuracy', type=float, default=None)
    p_perf.add_argument('--price-competitiveness', type=float, default=None)
    p_perf.add_argument('--lead-time-compliance', type=float, default=None)
    p_perf.add_argument('--service-quality', type=float, default=None)
    p_perf.add_argument('--communication-ease', type=float, default=None)
    p_perf.add_argument('--purchase-success-rate', type=float, default=None)
    p_perf.add_argument('--reorder-rate', type=float, default=None)
    p_perf.add_argument('--manual-rating', type=float, default=None)
    p_perf.set_defaults(func=cmd_record_performance)

    p_manual = sub.add_parser('manual-approve', help='Manually approve a promising supplier for RFQ')
    p_manual.add_argument('--project-id', default='')
    p_manual.add_argument('--candidate-id', required=True)
    p_manual.set_defaults(func=cmd_manual_approve)

    p_dash = sub.add_parser('dashboard', help='Show supplier dashboard summary')
    p_dash.add_argument('--project-id', default='')
    p_dash.add_argument('--company-name', default='')
    p_dash.set_defaults(func=cmd_dashboard)

    p_stats = sub.add_parser('stats', help='Show sourcing module stats')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result or 0)


if __name__ == '__main__':
    raise SystemExit(main())
