#!/usr/bin/env python3
"""Hermes operational memory and diary.

Append-only logs for learned operational rules, user corrections, and recent
actions. Safe by default: if the diary store is absent it is created locally.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from reporting_utils import append_jsonl, count_jsonl, load_jsonl_records, make_id, normalize_text, utc_now

ROOT = Path('/opt/data/hermes-mail')
DATA_DIR = ROOT / 'data'
DIARY_JSONL = DATA_DIR / 'hermes_diary.jsonl'
MEMORY_JSONL = ROOT / 'state' / 'hermes_memory.jsonl'


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DIARY_JSONL.touch(exist_ok=True)
    MEMORY_JSONL.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_JSONL.touch(exist_ok=True)


def _json_arg(value: str | None, default: Any) -> Any:
    if value is None or value == '':
        return default
    try:
        return json.loads(value)
    except Exception:
        return value


def _suggest_learning(action_type: str, *, company: str, country: str, product: str, result: str, error: str, correction: str, next_action: str) -> str:
    parts: list[str] = []
    act = normalize_text(action_type).lower()
    res = normalize_text(result).lower()
    corr = normalize_text(correction).lower()
    nxt = normalize_text(next_action)

    if 'exclude' in act or 'bloque' in corr or 'não enviar' in corr:
        parts.append('adicionar/confirmar lista de exclusão comercial')
    if 'contact' in act and ('wrong' in corr or 'errado' in corr):
        parts.append('atualizar CRM com contato corrigido')
    if country and ('spanish' in corr or 'espanhol' in corr):
        parts.append(f'preferir espanhol para {country}')
    if 'embassy' in act or 'secom' in act:
        parts.append('ajustar janela de follow-up por país/embaixada')
    if 'supplier' in act or 'rfq' in act:
        parts.append('reforçar base de fornecedores/fabricantes homologados')
    if 'approved' in res:
        parts.append('manter padrão de template aprovado')
    if error:
        parts.append('registrar erro para reexecução assistida')
    if nxt:
        parts.append(f'próxima ação sugerida: {nxt}')
    if company or product:
        parts.append(f'contexto rastreado: {company or "sem empresa"} / {product or "sem produto"}')
    return '; '.join(parts) or 'sem aprendizado adicional'


def log_action(*, module: str, action: str, action_type: str, company: str = '', country: str = '', product: str = '', origin: str = '', result: Any = None, error: str = '', correction: Any = None, next_action_suggested: str = '', summary: str = '', learning: str = '') -> dict[str, Any]:
    ensure_storage()
    record = {
        'id': make_id('hermes_memory'),
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'module': normalize_text(module),
        'action': normalize_text(action),
        'action_type': normalize_text(action_type),
        'company': normalize_text(company),
        'country': normalize_text(country),
        'product': normalize_text(product),
        'origin': normalize_text(origin),
        'result': result if result is not None else {},
        'error': normalize_text(error),
        'correction': correction if correction is not None else {},
        'learning_generated': normalize_text(learning) or _suggest_learning(action_type, company=company, country=country, product=product, result=json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else '', error=error, correction=json.dumps(correction, ensure_ascii=False, sort_keys=True) if correction is not None else '', next_action=next_action_suggested),
        'next_action_suggested': normalize_text(next_action_suggested),
        'summary': normalize_text(summary) or normalize_text(action),
        'source': 'hermes_memory',
    }
    append_jsonl(MEMORY_JSONL, record)
    diary = {
        'id': record['id'],
        'timestamp': record['created_at'],
        'module': record['module'],
        'action': record['action'],
        'summary': record['summary'],
        'result': record['result'],
        'learning': record['learning_generated'],
        'source': 'hermes_diary',
    }
    append_jsonl(DIARY_JSONL, diary)
    return record


def cmd_log_action(args: argparse.Namespace) -> int:
    result = _json_arg(args.result, {})
    correction = _json_arg(args.correction, {})
    record = log_action(
        module=args.module,
        action=args.action_name,
        action_type=args.action_type,
        company=args.company,
        country=args.country,
        product=args.product,
        origin=args.origin,
        result=result,
        error=args.error,
        correction=correction,
        next_action_suggested=args.next_action,
        summary=args.summary,
        learning=args.learning,
    )
    print(json.dumps({'ok': True, 'record': record}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_list_recent(args: argparse.Namespace) -> int:
    ensure_storage()
    records = load_jsonl_records(MEMORY_JSONL)
    recent = records[-max(1, args.limit):]
    print(json.dumps({'ok': True, 'count': len(recent), 'records': recent}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_summarize(_: argparse.Namespace) -> int:
    ensure_storage()
    records = load_jsonl_records(MEMORY_JSONL)
    by_module = Counter(rec.get('module') or 'unknown' for rec in records)
    by_action_type = Counter(rec.get('action_type') or 'unknown' for rec in records)
    by_country = Counter(rec.get('country') or 'unknown' for rec in records)
    learnings = Counter(rec.get('learning_generated') or 'unknown' for rec in records)
    print(json.dumps({
        'ok': True,
        'memory_total': count_jsonl(MEMORY_JSONL),
        'diary_total': count_jsonl(DIARY_JSONL),
        'by_module': dict(by_module),
        'by_action_type': dict(by_action_type),
        'by_country': dict(by_country),
        'top_learnings': learnings.most_common(10),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes operational memory and diary')
    sub = parser.add_subparsers(dest='command', required=True)

    p_log = sub.add_parser('log-action', help='Log an operational action')
    p_log.add_argument('--module', required=True)
    p_log.add_argument('--action-name', required=True)
    p_log.add_argument('--action-type', required=True)
    p_log.add_argument('--company', default='')
    p_log.add_argument('--country', default='')
    p_log.add_argument('--product', default='')
    p_log.add_argument('--origin', default='')
    p_log.add_argument('--result', default='{}')
    p_log.add_argument('--error', default='')
    p_log.add_argument('--correction', default='{}')
    p_log.add_argument('--learning', default='')
    p_log.add_argument('--next-action', default='')
    p_log.add_argument('--summary', default='')
    p_log.set_defaults(func=cmd_log_action)

    p_recent = sub.add_parser('list-recent', help='List recent memory entries')
    p_recent.add_argument('--limit', type=int, default=10)
    p_recent.set_defaults(func=cmd_list_recent)

    p_sum = sub.add_parser('summarize', help='Summarize memory trends')
    p_sum.set_defaults(func=cmd_summarize)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
