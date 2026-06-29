#!/usr/bin/env python3
"""Global Product Intelligence for Hermes Mail.

Classify a product, select compliance rules, choose source types, and build a
category-aware sourcing strategy with append-only intelligence history.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _resolve_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    opt_root = Path('/opt/data/hermes-mail')
    if local_root.exists():
        return local_root
    if opt_root.exists():
        return opt_root
    return local_root


ROOT = _resolve_root()
PRODUCT_INTELLIGENCE_JSONL = ROOT / 'product-intelligence.jsonl'
PRODUCT_CATEGORIES_JSONL = ROOT / 'product-categories.jsonl'
PRODUCT_COMPLIANCE_RULES_JSONL = ROOT / 'product-compliance-rules.jsonl'
PRODUCT_SOURCING_SOURCES_JSONL = ROOT / 'product-sourcing-sources.jsonl'
PROCUREMENT_KB_JSONL = ROOT / 'procurement-knowledge-base.jsonl'

TEST_PRODUCT_TEXT = (
    'Semi-automatic wafer cone making machine, 1000-1200 pcs/hour, electric heating, '
    '380V 60Hz 3-phase, with mixer, dosing, baking and cone rolling unit.'
)

PATTERN_RULES = [
    ('produtos-quimicos', ['cas', 'sds', 'msds', 'coa', 'purity', 'grade', 'solvent', 'reagent', 'chemical']),
    ('alimentos-ingredientes', ['haccp', 'halal', 'kosher', 'food', 'ingredient', 'ingredients', 'flour', 'starch', 'syrup', 'spice']),
    ('equipamentos-medicos', ['medical', 'device', 'iso 13485', 'ce medical', 'sterile', 'surgical', 'fda']),
    ('componentes-eletronicos', ['rohs', 'reach', 'datasheet', 'pcb', 'chip', 'sensor', 'module', 'electronics', 'electronic']),
    ('autopecas', ['auto part', 'auto parts', 'automotive', 'brake', 'filter', 'transmission', 'engine', 'oem']),
    ('embalagens', ['packaging', 'carton', 'box', 'pouch', 'bottle', 'label', 'film', 'wrap']),
    ('texteis', ['textile', 'textiles', 'fabric', 'yarn', 'cotton', 'polyester', 'garment']),
    ('cosmeticos', ['cosmetic', 'cosmetics', 'skincare', 'cream', 'lotion', 'serum', 'makeup', 'private label']),
    ('materias-primas-metálicas', ['steel', 'aluminum', 'copper', 'metal', 'ingot', 'sheet', 'coil', 'bar', 'alloy']),
    ('plásticos-resinas', ['plastic resin', 'resin', 'polymer', 'polyethylene', 'pp', 'pe', 'pvc', 'abs', 'plastic']),
    ('produtos-agricolas', ['agricultural', 'grain', 'seed', 'corn', 'soy', 'tea', 'coffee', 'spice']),
    ('moveis', ['furniture', 'chair', 'table', 'sofa', 'cabinet', 'wood']),
    ('servicos', ['service', 'services', 'consulting', 'maintenance', 'installation', 'outsourcing']),
    ('software', ['software', 'saas', 'platform', 'api', 'license', 'subscription', 'app']),
    ('oem-private-label', ['oem', 'private label', 'white label', 'contract manufacturing', 'custom brand']),
    ('machines-industriais', ['machine', 'machinery', 'equipment', 'production line', 'processing line', 'electric heating', 'baking', 'dosing', 'rolling', 'wafer cone', 'ice cream cone']),
]

DEFAULT_SEARCH_PRIORITY = {
    'machines-industriais': ['source_official_website', 'source_manufacturer_catalog', 'source_trade_fair_exhibitor', 'source_export_registry'],
    'produtos-quimicos': ['source_official_website', 'source_certification_database', 'source_government_registry', 'source_authorized_distributor'],
    'alimentos-ingredientes': ['source_official_website', 'source_certification_database', 'source_government_registry', 'source_association_directory'],
    'equipamentos-medicos': ['source_official_website', 'source_certification_database', 'source_government_registry', 'source_authorized_distributor'],
    'componentes-eletronicos': ['source_official_website', 'source_manufacturer_catalog', 'source_certification_database', 'source_authorized_distributor'],
    'autopecas': ['source_official_website', 'source_manufacturer_catalog', 'source_export_registry', 'source_association_directory'],
    'embalagens': ['source_official_website', 'source_manufacturer_catalog', 'source_trade_fair_exhibitor'],
    'texteis': ['source_official_website', 'source_manufacturer_catalog', 'source_trade_fair_exhibitor'],
    'cosmeticos': ['source_official_website', 'source_certification_database', 'source_government_registry'],
    'materias-primas-metálicas': ['source_official_website', 'source_export_registry', 'source_government_registry'],
    'plásticos-resinas': ['source_official_website', 'source_export_registry', 'source_certification_database'],
    'produtos-agricolas': ['source_official_website', 'source_government_registry', 'source_association_directory'],
    'moveis': ['source_official_website', 'source_manufacturer_catalog', 'source_trade_fair_exhibitor'],
    'servicos': ['source_official_website', 'source_government_registry', 'source_association_directory'],
    'software': ['source_official_website', 'source_authorized_distributor'],
    'oem-private-label': ['source_official_website', 'source_manufacturer_catalog', 'source_trade_fair_exhibitor'],
    'outros': ['source_official_website', 'source_manufacturer_catalog', 'source_association_directory'],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def slugify(text: str) -> str:
    chars = []
    prev_dash = False
    for ch in text.casefold():
        if ch.isalnum():
            chars.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                chars.append('-')
                prev_dash = True
    return ''.join(chars).strip('-') or 'item'


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
    return records


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def ensure_files() -> None:
    for path in [PRODUCT_INTELLIGENCE_JSONL, PRODUCT_CATEGORIES_JSONL, PRODUCT_COMPLIANCE_RULES_JSONL, PRODUCT_SOURCING_SOURCES_JSONL, PROCUREMENT_KB_JSONL]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


def load_indexed_catalog(path: Path, key: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(path):
        value = record.get(key)
        if isinstance(value, str) and value:
            index[value] = record
    return index


def classify_product(text: str) -> tuple[str, int]:
    normalized = text.casefold()
    best_id = 'outros'
    best_score = -1
    for category_id, keywords in PATTERN_RULES:
        score = sum(1 for kw in keywords if kw in normalized)
        if category_id == 'machines-industriais' and any(term in normalized for term in ['wafer cone', 'ice cream cone', 'electric heating', '3-phase', '380v', 'dosing', 'rolling']):
            score += 3
        if score > best_score:
            best_id = category_id
            best_score = score
    if best_score <= 0:
        return 'outros', 0
    return best_id, best_score


def derive_subcategory(category_id: str, text: str, taxonomy: dict[str, dict[str, Any]]) -> str:
    normalized = text.casefold()
    if category_id == 'machines-industriais' and any(term in normalized for term in ['wafer cone', 'ice cream cone', 'cone rolling', 'cone maker']):
        return 'food processing machinery / wafer cone machine'
    category = taxonomy.get(category_id) or taxonomy.get('outros') or {}
    return str(category.get('subcategory') or 'general / uncategorized')


def category_taxonomy() -> dict[str, dict[str, Any]]:
    return load_indexed_catalog(PRODUCT_CATEGORIES_JSONL, 'category_id')


def compliance_catalog() -> dict[str, dict[str, Any]]:
    return load_indexed_catalog(PRODUCT_COMPLIANCE_RULES_JSONL, 'category_id')


def source_catalog() -> dict[str, dict[str, Any]]:
    return load_indexed_catalog(PRODUCT_SOURCING_SOURCES_JSONL, 'id')


def build_intelligence_record(product_text: str, region: str, country: str) -> dict[str, Any]:
    taxonomy = category_taxonomy()
    compliance = compliance_catalog()
    sources = source_catalog()
    category_id, score = classify_product(product_text)
    category = taxonomy.get(category_id) or taxonomy.get('outros') or {}
    compliance_rule = compliance.get(category_id) or {}
    source_ids = list(category.get('source_profile_ids') or DEFAULT_SEARCH_PRIORITY.get(category_id, DEFAULT_SEARCH_PRIORITY['outros']))
    source_labels = [sources[sid]['label'] for sid in source_ids if sid in sources]
    return {
        'id': f'product_intelligence_{slugify(product_text)[:48]}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}',
        'created_at': now(),
        'updated_at': now(),
        'product_text': product_text,
        'region': region,
        'country': country,
        'category_id': category_id,
        'category_label': category.get('category_label') or category.get('label') or category_id,
        'subcategory': derive_subcategory(category_id, product_text, taxonomy),
        'application': category.get('application', 'general sourcing demand'),
        'ideal_supplier_types': category.get('ideal_supplier_types', ['depends on product']),
        'search_mode': category.get('search_mode', 'hybrid'),
        'risk_flags': category.get('risk_flags', []),
        'required_documents': compliance_rule.get('mandatory_documents') or category.get('required_documents', []),
        'recommended_certifications': compliance_rule.get('recommended_certifications') or category.get('recommended_certifications', []),
        'source_profile_ids': source_ids,
        'recommended_sources': source_labels,
        'qualification_criteria': category.get('qualification_criteria', []),
        'rfq_model': category.get('rfq_model', {}),
        'compliance_rule_id': compliance_rule.get('id') or f'compliance-{category_id}',
        'primary_source_profile_id': source_ids[0] if source_ids else 'source_official_website',
        'knowledge_base_id': 'procurement_global_principles',
        'confidence_score': min(100, 55 + score * 12),
        'matched_score': score,
    }


def build_category_profile(intel: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': f'category_profile_{intel["category_id"]}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}',
        'created_at': now(),
        'updated_at': now(),
        'product_intelligence_id': intel['id'],
        'category_id': intel['category_id'],
        'category_label': intel['category_label'],
        'subcategory': intel['subcategory'],
        'application': intel['application'],
        'ideal_supplier_types': intel['ideal_supplier_types'],
        'search_mode': intel['search_mode'],
        'risk_flags': intel['risk_flags'],
        'required_documents': intel['required_documents'],
        'recommended_certifications': intel['recommended_certifications'],
        'qualification_criteria': intel['qualification_criteria'],
        'rfq_model': intel['rfq_model'],
        'source_profile_ids': intel['source_profile_ids'],
        'recommended_sources': intel['recommended_sources'],
        'notes': 'Derived category profile from product intelligence run.',
    }


def build_sourcing_strategy(intel: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    compliance = compliance_catalog().get(intel['category_id'], {})
    return {
        'id': f'sourcing_strategy_{intel["category_id"]}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}',
        'created_at': now(),
        'updated_at': now(),
        'product_intelligence_id': intel['id'],
        'category_profile_id': profile['id'],
        'category_id': intel['category_id'],
        'category_label': intel['category_label'],
        'subcategory': intel['subcategory'],
        'sourcing_mode': intel['search_mode'],
        'target_supplier_types': intel['ideal_supplier_types'],
        'source_priority': profile['source_profile_ids'],
        'source_recommendations': profile['recommended_sources'],
        'mandatory_documents': compliance.get('mandatory_documents', intel['required_documents']),
        'recommended_certifications': compliance.get('recommended_certifications', intel['recommended_certifications']),
        'qualification_criteria': intel['qualification_criteria'],
        'risk_controls': intel['risk_flags'],
        'rfq_model': intel['rfq_model'],
        'search_phases': [
            'classify demand',
            'load category compliance rules',
            'search official and manufacturer sources first',
            'verify documents and regulatory fit',
            'qualify supplier type and commercial trust',
            'gate RFQ by the lower of manufacturing and trust scores',
        ],
        'next_actions': [
            'collect evidence URLs',
            'request mandatory documents',
            'verify technical compatibility',
            'record product intelligence history',
        ],
        'knowledge_base_id': 'procurement_global_principles',
        'compliance_rule_id': intel['compliance_rule_id'],
        'primary_source_profile_id': intel['primary_source_profile_id'],
    }


def latest_product_intelligence() -> dict[str, Any] | None:
    records = read_jsonl(PRODUCT_INTELLIGENCE_JSONL)
    return records[-1] if records else None


def latest_derived_category_profile() -> dict[str, Any] | None:
    records = [r for r in read_jsonl(PRODUCT_CATEGORIES_JSONL) if isinstance(r.get('product_intelligence_id'), str)]
    return records[-1] if records else None


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_files()
    for path in [PRODUCT_INTELLIGENCE_JSONL, PRODUCT_CATEGORIES_JSONL, PRODUCT_COMPLIANCE_RULES_JSONL, PRODUCT_SOURCING_SOURCES_JSONL, PROCUREMENT_KB_JSONL]:
        read_jsonl(path)
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('files=5')
    return 0


def cmd_classify_test_product(args: argparse.Namespace) -> int:
    ensure_files()
    intel = build_intelligence_record(args.product_text or TEST_PRODUCT_TEXT, args.region or 'global', args.country or 'global')
    append_jsonl(PRODUCT_INTELLIGENCE_JSONL, intel)
    print(json.dumps(intel, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_create_category_profile(args: argparse.Namespace) -> int:
    ensure_files()
    intel = latest_product_intelligence()
    if intel is None:
        intel = build_intelligence_record(TEST_PRODUCT_TEXT, args.region or 'global', args.country or 'global')
        append_jsonl(PRODUCT_INTELLIGENCE_JSONL, intel)
    profile = build_category_profile(intel)
    append_jsonl(PRODUCT_CATEGORIES_JSONL, profile)
    print(json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_generate_sourcing_strategy(args: argparse.Namespace) -> int:
    ensure_files()
    intel = latest_product_intelligence()
    if intel is None:
        intel = build_intelligence_record(TEST_PRODUCT_TEXT, args.region or 'global', args.country or 'global')
        append_jsonl(PRODUCT_INTELLIGENCE_JSONL, intel)
    profile = latest_derived_category_profile()
    if profile is None or profile.get('product_intelligence_id') != intel['id']:
        profile = build_category_profile(intel)
        append_jsonl(PRODUCT_CATEGORIES_JSONL, profile)
    strategy = build_sourcing_strategy(intel, profile)
    append_jsonl(PROCUREMENT_KB_JSONL, strategy)
    print(json.dumps(strategy, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_files()
    product_intel = read_jsonl(PRODUCT_INTELLIGENCE_JSONL)
    category_profiles = read_jsonl(PRODUCT_CATEGORIES_JSONL)
    compliance = read_jsonl(PRODUCT_COMPLIANCE_RULES_JSONL)
    sources = read_jsonl(PRODUCT_SOURCING_SOURCES_JSONL)
    kb = read_jsonl(PROCUREMENT_KB_JSONL)
    latest = product_intel[-1] if product_intel else None
    print('GLOBAL PRODUCT INTELLIGENCE STATS')
    print(f'product_intelligence_records: {len(product_intel)}')
    print(f'category_records: {len(category_profiles)}')
    print(f'compliance_rule_records: {len(compliance)}')
    print(f'source_profile_records: {len(sources)}')
    print(f'knowledge_base_records: {len(kb)}')
    if latest:
        print(f'latest_category: {latest.get("category_label")}')
        print(f'latest_subcategory: {latest.get("subcategory")}')
        print(f'latest_supplier_mode: {latest.get("search_mode")}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Global Product Intelligence module')
    parser.add_argument('--product-text', default=TEST_PRODUCT_TEXT, help='Optional product text override')
    parser.add_argument('--region', default='global')
    parser.add_argument('--country', default='global')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate required files and JSONL syntax')
    p_validate.set_defaults(func=cmd_validate)

    p_classify = sub.add_parser('classify-test-product', help='Classify the built-in initial test product')
    p_classify.set_defaults(func=cmd_classify_test_product)

    p_profile = sub.add_parser('create-category-profile', help='Create a category profile from the latest intelligence record')
    p_profile.set_defaults(func=cmd_create_category_profile)

    p_strategy = sub.add_parser('generate-sourcing-strategy', help='Generate a category-aware sourcing strategy')
    p_strategy.set_defaults(func=cmd_generate_sourcing_strategy)

    p_stats = sub.add_parser('stats', help='Show module statistics')
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
