#!/usr/bin/env python3
"""Open WebUI bridge for Hermes Mail.

This bridge keeps Open WebUI as a dry-run, auditable entry point that can
create sourcing requests, trigger the existing intelligence + sourcing flow,
and record manual approval/rejection actions without sending real e-mail or
real RFQs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporting_utils import (
    OPEN_WEBUI_ACTIONS_JSONL,
    OPEN_WEBUI_REQUESTS_JSONL,
    SOURCING_PROJECTS_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    PRODUCT_INTELLIGENCE_JSONL,
    PRODUCT_CATEGORIES_JSONL,
    PRODUCT_COMPLIANCE_RULES_JSONL,
    PRODUCT_SOURCING_SOURCES_JSONL,
    PROCUREMENT_KB_JSONL,
    SOURCING_REPORTS_DIR,
    ROOT,
    append_jsonl,
    ensure_runtime_dirs,
    latest_jsonl_record,
    load_jsonl_records,
    make_id,
    normalize_text,
    utc_now,
    write_json,
)
from purchase_governance import (
    build_purchase_gate_message,
    build_purchase_gate_reply_markup,
    build_purchase_recommendation,
    latest_purchase_recommendation,
)

SCRIPTS = ROOT / 'scripts'
GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT = SCRIPTS / 'global_product_intelligence.py'
SOURCING_RESEARCH_SCRIPT = SCRIPTS / 'sourcing_research.py'
TELEGRAM_NOTIFIER_SCRIPT = SCRIPTS / 'telegram_notifier.py'
STATE_SETTINGS_PATH = ROOT / 'state' / 'settings.json'
OPEN_WEBUI_DOCS_DIR = ROOT / 'docs'
OPEN_WEBUI_BRIDGE_LOG = ROOT / 'logs' / 'open-webui-bridge.log'
OPEN_WEBUI_PROPOSALS_JSONL = ROOT / 'open-webui-proposals.jsonl'

REQUEST_REQUIRED_PATHS = [
    ROOT,
    ROOT / 'logs',
    ROOT / 'state',
    ROOT / 'sourcing-projects',
    SOURCING_REPORTS_DIR,
]

REQUIRED_JSONL_FILES = [
    OPEN_WEBUI_REQUESTS_JSONL,
    OPEN_WEBUI_ACTIONS_JSONL,
    PRODUCT_INTELLIGENCE_JSONL,
    PRODUCT_CATEGORIES_JSONL,
    PRODUCT_COMPLIANCE_RULES_JSONL,
    PRODUCT_SOURCING_SOURCES_JSONL,
    PROCUREMENT_KB_JSONL,
    SOURCING_PROJECTS_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
]

TEST_PRODUCT_TEXT = (
    'Semi-automatic wafer cone making machine, 1000-1200 pcs/hour, electric heating, '
    '380V 60Hz 3-phase, with mixer, dosing, baking and cone rolling unit.'
)

TEST_DESCRIPTION = (
    'Dry-run sourcing request created from Open WebUI. The request includes a '
    'semi-automatic wafer cone making machine, global sourcing scope, and '
    'manual review support for supplier qualification.'
)

TEST_ATTACHMENTS = [
    {
        'name': 'wafer-cone-catalog.pdf',
        'type': 'pdf',
        'source': 'open_webui_upload',
        'status': 'simulated',
    },
    {
        'name': 'machine-photo.jpg',
        'type': 'image',
        'source': 'open_webui_upload',
        'status': 'simulated',
    },
    {
        'name': 'production-line-brochure.pdf',
        'type': 'catalog',
        'source': 'open_webui_upload',
        'status': 'simulated',
    },
]


def now() -> str:
    return utc_now()


def touch_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def ensure_store() -> None:
    ensure_runtime_dirs()
    for path in REQUIRED_JSONL_FILES + [OPEN_WEBUI_PROPOSALS_JSONL]:
        touch_jsonl(path)
    OPEN_WEBUI_BRIDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if OPEN_WEBUI_DOCS_DIR.exists():
        OPEN_WEBUI_DOCS_DIR.mkdir(parents=True, exist_ok=True)


def append_action(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        'id': make_id('open_webui_action'),
        'created_at': now(),
        'updated_at': now(),
        'source_channel': 'open_webui',
        'mode': 'dry_run',
        'dry_run': True,
        **record,
    }
    append_jsonl(OPEN_WEBUI_ACTIONS_JSONL, payload)
    return payload


def append_request(record: dict[str, Any]) -> dict[str, Any]:
    request_id = record.get('request_id') or make_id('open_webui_request')
    payload = {
        'id': request_id,
        'request_id': request_id,
        'created_at': now(),
        'updated_at': now(),
        'source_channel': 'open_webui',
        'mode': 'dry_run',
        'dry_run': True,
        **record,
    }
    append_jsonl(OPEN_WEBUI_REQUESTS_JSONL, payload)
    return payload


def load_requests() -> list[dict[str, Any]]:
    return load_jsonl_records(OPEN_WEBUI_REQUESTS_JSONL)


def load_actions() -> list[dict[str, Any]]:
    return load_jsonl_records(OPEN_WEBUI_ACTIONS_JSONL)


def latest_request() -> dict[str, Any] | None:
    return latest_jsonl_record(OPEN_WEBUI_REQUESTS_JSONL)


def actions_for_request(request_id: str) -> list[dict[str, Any]]:
    return [rec for rec in load_actions() if rec.get('request_id') == request_id]


def latest_project_for_request(request_id: str) -> dict[str, Any] | None:
    matches = [rec for rec in load_jsonl_records(SOURCING_PROJECTS_JSONL) if rec.get('open_webui_request_id') == request_id or rec.get('request_id') == request_id]
    if matches:
        return matches[-1]
    projects = load_jsonl_records(SOURCING_PROJECTS_JSONL)
    if not projects:
        return None
    return projects[-1]


def latest_product_intelligence_for_request(request_id: str) -> dict[str, Any] | None:
    intel = [rec for rec in load_jsonl_records(PRODUCT_INTELLIGENCE_JSONL) if rec.get('open_webui_request_id') == request_id]
    return intel[-1] if intel else latest_jsonl_record(PRODUCT_INTELLIGENCE_JSONL)


def run_command(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return result


def parse_key_value(output: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}=(.+)$', output, flags=re.MULTILINE)
    return match.group(1).strip() if match else ''


def parse_run_output(output: str) -> dict[str, str]:
    values = {
        'project_id': parse_key_value(output, 'project_id'),
        'product_intelligence_id': parse_key_value(output, 'product_intelligence_id'),
        'category': parse_key_value(output, 'category'),
        'subcategory': parse_key_value(output, 'subcategory'),
        'report_json': parse_key_value(output, 'report_json'),
        'report_pdf': parse_key_value(output, 'report_pdf'),
    }
    return values


def summarize_candidates(project_id: str) -> list[dict[str, Any]]:
    records = [rec for rec in load_jsonl_records(MANUFACTURER_QUALIFICATION_JSONL) if rec.get('project_id') == project_id]
    return sorted(
        records,
        key=lambda rec: float(rec.get('final_score') or rec.get('manufacturer_score') or 0),
        reverse=True,
    )


def derive_available_actions(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return ['view_project_status', 'retry_processing']
    top_score = max(float(rec.get('final_score') or rec.get('manufacturer_score') or 0) for rec in records)
    if any(60 <= float(rec.get('final_score') or rec.get('manufacturer_score') or 0) < 80 for rec in records):
        return ['approve_supplier', 'reject_supplier', 'view_manufacturer_report', 'view_client_proposal_pdf', 'consult_history']
    if top_score >= 80:
        return ['approve_rfq', 'view_manufacturer_report', 'view_client_proposal_pdf', 'consult_history']
    return ['view_manufacturer_report', 'reject_supplier', 'consult_history']


def write_request_snapshot(project: dict[str, Any] | None, request: dict[str, Any], intel: dict[str, Any] | None, report_paths: dict[str, str], action: str) -> None:
    if not project:
        return
    enriched = dict(project)
    enriched.update({
        'open_webui_request_id': request['request_id'],
        'open_webui_source_channel': 'open_webui',
        'open_webui_request_title': request.get('title') or request.get('product_text') or '',
        'open_webui_product_text': request.get('product_text') or '',
        'open_webui_description': request.get('description') or '',
        'open_webui_region': request.get('region') or '',
        'open_webui_quantity': request.get('quantity'),
        'open_webui_quantity_unit': request.get('quantity_unit') or '',
        'open_webui_application': request.get('application') or '',
        'open_webui_end_customer': request.get('final_customer') or '',
        'open_webui_attachments': request.get('attachments') or [],
        'open_webui_action': action,
        'open_webui_product_intelligence_id': (intel or {}).get('id') or request.get('product_intelligence_id') or '',
        'open_webui_report_json': report_paths.get('report_json') or '',
        'open_webui_report_pdf': report_paths.get('report_pdf') or '',
        'open_webui_proposal_pdf': report_paths.get('proposal_pdf') or '',
        'updated_at': now(),
    })
    append_jsonl(SOURCING_PROJECTS_JSONL, enriched)
    project_dir = ROOT / 'sourcing-projects' / str(enriched['project_id'])
    project_dir.mkdir(parents=True, exist_ok=True)
    write_json(project_dir / 'project.json', enriched)


def build_purchase_proposal_payload(request: dict[str, Any], summary: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    message = build_purchase_gate_message(recommendation)
    reply_markup = build_purchase_gate_reply_markup(recommendation)
    return {
        'id': make_id('open_webui_purchase_proposal'),
        'created_at': now(),
        'updated_at': now(),
        'request_id': request['request_id'],
        'project_id': summary['sourcing_project_id'],
        'product_intelligence_id': summary['product_intelligence_id'],
        'proposal_type': 'purchase_decision_gate',
        'decision_gate': recommendation.get('decision_gate') or {'type': 'decision_gate', 'context': 'purchase_and_freight_timing'},
        'message': message,
        'reply_markup': reply_markup,
        'recommendation_id': recommendation.get('id') or recommendation.get('recommendation_id'),
        'recommended_action': recommendation.get('suggested_action') or 'manual_review_required',
        'recommended_title': recommendation.get('recommendation_title') or 'Revisão manual necessária',
        'reasoning_summary': recommendation.get('reasoning_summary') or '',
        'risk_summary': recommendation.get('risk_summary') or '',
        'available_actions': summary.get('available_actions') or [],
        'supplier_candidates': summary.get('premium_candidates') or [],
        'manual_review_candidates': summary.get('manual_review_candidates') or [],
        'blocked_candidates': summary.get('blocked_candidates') or [],
        'report_json': summary.get('report_json') or '',
        'report_pdf': summary.get('report_pdf') or '',
        'proposal_pdf': summary.get('proposal_pdf') or '',
        'source_channel': 'open_webui',
        'mode': 'dry_run',
    }


def request_summary_payload(request: dict[str, Any], project: dict[str, Any] | None, intel: dict[str, Any] | None, candidates: list[dict[str, Any]], report_paths: dict[str, str]) -> dict[str, Any]:
    manual_candidates = [rec for rec in candidates if 60 <= float(rec.get('final_score') or rec.get('manufacturer_score') or 0) < 80]
    premium_candidates = [rec for rec in candidates if float(rec.get('final_score') or rec.get('manufacturer_score') or 0) >= 80]
    blocked_candidates = [rec for rec in candidates if float(rec.get('final_score') or rec.get('manufacturer_score') or 0) < 60]
    return {
        'request_id': request['request_id'],
        'sourcing_project_id': (project or {}).get('project_id', 'Não verificado'),
        'product_intelligence_id': (intel or {}).get('id', 'Não verificado'),
        'category': (project or {}).get('category_label') or (intel or {}).get('category_label') or request.get('category') or 'Não verificado',
        'subcategory': (project or {}).get('subcategory') or (intel or {}).get('subcategory') or 'Não verificado',
        'manufacturer_count': len(candidates),
        'premium_candidates': [
            {
                'company_name': rec.get('company_name', 'Não verificado'),
                'final_score': rec.get('final_score', rec.get('manufacturer_score', 0)),
                'candidate_id': rec.get('id', 'Não verificado'),
            }
            for rec in premium_candidates[:5]
        ],
        'manual_review_candidates': [
            {
                'company_name': rec.get('company_name', 'Não verificado'),
                'final_score': rec.get('final_score', rec.get('manufacturer_score', 0)),
                'candidate_id': rec.get('id', 'Não verificado'),
            }
            for rec in manual_candidates[:5]
        ],
        'blocked_candidates': [
            {
                'company_name': rec.get('company_name', 'Não verificado'),
                'final_score': rec.get('final_score', rec.get('manufacturer_score', 0)),
                'candidate_id': rec.get('id', 'Não verificado'),
            }
            for rec in blocked_candidates[:5]
        ],
        'report_json': report_paths.get('report_json') or (project or {}).get('report_path') or 'Não verificado',
        'report_pdf': report_paths.get('report_pdf') or (project or {}).get('report_pdf_path') or 'Não verificado',
        'proposal_pdf': report_paths.get('proposal_pdf') or 'Não verificado',
        'available_actions': derive_available_actions(candidates),
        'status': (project or {}).get('status') or 'requested',
    }


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_store()
    errors: list[str] = []
    for path in REQUEST_REQUIRED_PATHS:
        if not path.exists():
            errors.append(f'missing path: {path}')
    for path in [GLOBAL_PRODUCT_INTELLIGENCE_SCRIPT, SOURCING_RESEARCH_SCRIPT, TELEGRAM_NOTIFIER_SCRIPT]:
        if not path.exists():
            errors.append(f'missing script: {path}')
    if STATE_SETTINGS_PATH.exists():
        try:
            settings = json.loads(STATE_SETTINGS_PATH.read_text(encoding='utf-8'))
            email_cfg = settings.get('email_config') if isinstance(settings, dict) else None
            if isinstance(email_cfg, dict) and email_cfg.get('mode') not in (None, 'dry_run'):
                errors.append('email_config.mode must remain dry_run')
        except Exception as exc:
            errors.append(f'cannot read settings.json: {exc}')
    for path in REQUIRED_JSONL_FILES:
        try:
            load_jsonl_records(path)
        except Exception as exc:
            errors.append(f'invalid jsonl {path}: {exc}')
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print(f'requests_jsonl={OPEN_WEBUI_REQUESTS_JSONL}')
    print(f'actions_jsonl={OPEN_WEBUI_ACTIONS_JSONL}')
    print('mode=dry_run')
    return 0


def cmd_create_test_request(_: argparse.Namespace) -> int:
    ensure_store()
    request = append_request({
        'request_id': make_id('open_webui_request'),
        'title': 'Wafer cone machine sourcing request',
        'source_channel': 'open_webui',
        'status': 'requested',
        'product_text': TEST_PRODUCT_TEXT,
        'description': TEST_DESCRIPTION,
        'region': 'global',
        'country': 'global',
        'quantity': 1200,
        'quantity_unit': 'pcs/hour',
        'application': 'ice cream cone manufacturing',
        'final_customer': 'Hermes Mail dry-run client',
        'language': 'pt-BR',
        'rfq_language': 'en',
        'attachments': TEST_ATTACHMENTS,
        'attachment_count': len(TEST_ATTACHMENTS),
        'requested_channels': ['open_webui', 'telegram', 'email', 'manual_upload', 'trade_fairs'],
        'notes': 'Open WebUI dry-run test request.',
        'project_id': '',
        'product_intelligence_id': '',
        'category_id': '',
        'category_label': '',
        'subcategory': '',
        'sourcing_strategy_id': '',
        'report_json': '',
        'report_pdf': '',
        'proposal_pdf': '',
    })
    append_action({
        'request_id': request['request_id'],
        'action_type': 'request_created',
        'status': 'queued',
        'title': request['title'],
        'product_text': request['product_text'],
        'region': request['region'],
        'quantity': request['quantity'],
        'application': request['application'],
        'final_customer': request['final_customer'],
        'attachments': request['attachments'],
    })
    print('OPEN WEBUI REQUEST CREATED')
    print(f'request_id={request["request_id"]}')
    print(f'product={request["product_text"]}')
    print(f'region={request["region"]}')
    print(f'quantity={request["quantity"]} {request["quantity_unit"]}')
    print(f'application={request["application"]}')
    print(f'final_customer={request["final_customer"]}')
    return 0


def cmd_process_latest(_: argparse.Namespace) -> int:
    ensure_store()
    request = latest_request()
    if not request:
        raise SystemExit('no open webui request found')
    request_id = str(request['request_id'])
    append_action({
        'request_id': request_id,
        'action_type': 'processing_started',
        'status': 'processing',
        'message': 'Starting dry-run sourcing workflow from Open WebUI.',
    })
    command = [
        sys.executable,
        str(SOURCING_RESEARCH_SCRIPT),
        'run-with-intelligence',
        '--product',
        str(request.get('product_text') or TEST_PRODUCT_TEXT),
        '--region',
        str(request.get('region') or 'global'),
        '--country',
        str(request.get('country') or 'global'),
    ]
    result = run_command(command, timeout=900)
    if result.returncode != 0:
        append_action({
            'request_id': request_id,
            'action_type': 'processing_failed',
            'status': 'error',
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
        })
        print('PROCESS FAILED')
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return result.returncode
    parsed = parse_run_output(result.stdout)
    project_id = parsed.get('project_id')
    intel = latest_product_intelligence_for_request(request_id)
    project = latest_project_for_request(request_id)
    if project_id and project and project.get('project_id') == project_id:
        # If the latest project record isn't already linked, attach the request metadata.
        write_request_snapshot(project, request, intel, parsed, 'processed')
        project = latest_project_for_request(request_id)
    candidates = summarize_candidates(project_id or (project or {}).get('project_id', ''))
    summary = request_summary_payload(request, project, intel, candidates, parsed)
    append_action({
        'request_id': request_id,
        'project_id': summary['sourcing_project_id'],
        'product_intelligence_id': summary['product_intelligence_id'],
        'action_type': 'workflow_completed',
        'status': summary['status'],
        'category': summary['category'],
        'subcategory': summary['subcategory'],
        'manufacturer_count': summary['manufacturer_count'],
        'available_actions': summary['available_actions'],
        'report_json': summary['report_json'],
        'report_pdf': summary['report_pdf'],
        'proposal_pdf': summary['proposal_pdf'],
    })
    recommendation = latest_purchase_recommendation()
    if recommendation is None:
        top_supplier = summary['premium_candidates'][0]['company_name'] if summary.get('premium_candidates') else 'Fornecedor'
        recommendation = build_purchase_recommendation(
            product_name=str(request.get('title') or request.get('product_text') or TEST_PRODUCT_TEXT),
            supplier_name=str(top_supplier),
            notes=[f'open_webui_request={request_id}', f'manufacturers={summary["manufacturer_count"]}'],
        )
    proposal = build_purchase_proposal_payload(request, summary, recommendation)
    append_jsonl(OPEN_WEBUI_PROPOSALS_JSONL, proposal)
    telegram_note = {
        'request_id': request_id,
        'project_id': summary['sourcing_project_id'],
        'action_type': 'telegram_notice_skipped',
        'status': 'dry_run',
        'message': 'Telegram notice is optional and was not sent automatically.',
    }
    append_action(telegram_note)
    print('OPEN WEBUI PROCESS COMPLETED')
    print(f'request_id={summary["request_id"]}')
    print(f'sourcing_project_id={summary["sourcing_project_id"]}')
    print(f'product_intelligence_id={summary["product_intelligence_id"]}')
    print(f'category={summary["category"]}')
    print(f'subcategory={summary["subcategory"]}')
    print(f'manufacturers={summary["manufacturer_count"]}')
    print(f'report_json={summary["report_json"]}')
    print(f'report_pdf={summary["report_pdf"]}')
    print(f'proposal_pdf={summary["proposal_pdf"]}')
    print('available_actions=' + ', '.join(summary['available_actions']))
    return 0


def pick_candidate_for_approval(project_id: str) -> dict[str, Any] | None:
    records = summarize_candidates(project_id)
    band = [rec for rec in records if 60 <= float(rec.get('final_score') or rec.get('manufacturer_score') or 0) < 80]
    if band:
        return band[0]
    return records[0] if records else None


def pick_candidate_for_rejection(project_id: str) -> dict[str, Any] | None:
    records = summarize_candidates(project_id)
    return records[-1] if records else None


def cmd_approve_test_supplier(_: argparse.Namespace) -> int:
    ensure_store()
    request = latest_request()
    if not request:
        raise SystemExit('no open webui request found')
    project = latest_project_for_request(str(request['request_id']))
    if not project or not project.get('project_id'):
        raise SystemExit('no sourcing project found for latest request')
    candidate = pick_candidate_for_approval(str(project['project_id']))
    if not candidate:
        raise SystemExit('no candidate available for approval')
    score = float(candidate.get('final_score') or candidate.get('manufacturer_score') or 0)
    if score < 60:
        raise SystemExit('no candidate in the 60-79 approval range')
    command = [
        sys.executable,
        str(SOURCING_RESEARCH_SCRIPT),
        'manual-approve',
        '--project-id',
        str(project['project_id']),
        '--candidate-id',
        str(candidate['id']),
    ]
    result = run_command(command, timeout=300)
    if result.returncode != 0:
        raise SystemExit(result.stdout + '\n' + result.stderr)
    append_action({
        'request_id': request['request_id'],
        'project_id': project['project_id'],
        'candidate_id': candidate['id'],
        'action_type': 'supplier_approved',
        'status': 'approved',
        'company_name': candidate.get('company_name', 'Não verificado'),
        'final_score': score,
        'message': 'Manual approval recorded from Open WebUI dry-run.',
    })
    write_request_snapshot(project, request, latest_product_intelligence_for_request(str(request['request_id'])), {
        'report_json': str(project.get('report_path') or ''),
        'report_pdf': str(project.get('report_pdf_path') or ''),
        'proposal_pdf': '',
    }, 'approved_supplier')
    print('SUPPLIER APPROVED')
    print(f'request_id={request["request_id"]}')
    print(f'project_id={project["project_id"]}')
    print(f'candidate_id={candidate["id"]}')
    print(f'company_name={candidate.get("company_name", "Não verificado")}')
    print(f'final_score={score}')
    return 0


def cmd_reject_test_supplier(_: argparse.Namespace) -> int:
    ensure_store()
    request = latest_request()
    if not request:
        raise SystemExit('no open webui request found')
    project = latest_project_for_request(str(request['request_id']))
    if not project or not project.get('project_id'):
        raise SystemExit('no sourcing project found for latest request')
    candidate = pick_candidate_for_rejection(str(project['project_id']))
    if not candidate:
        raise SystemExit('no candidate available for rejection')
    score = float(candidate.get('final_score') or candidate.get('manufacturer_score') or 0)
    append_action({
        'request_id': request['request_id'],
        'project_id': project['project_id'],
        'candidate_id': candidate['id'],
        'action_type': 'supplier_rejected',
        'status': 'rejected',
        'company_name': candidate.get('company_name', 'Não verificado'),
        'final_score': score,
        'message': 'Manual rejection recorded from Open WebUI dry-run.',
    })
    write_request_snapshot(project, request, latest_product_intelligence_for_request(str(request['request_id'])), {
        'report_json': str(project.get('report_path') or ''),
        'report_pdf': str(project.get('report_pdf_path') or ''),
        'proposal_pdf': '',
    }, 'rejected_supplier')
    print('SUPPLIER REJECTED')
    print(f'request_id={request["request_id"]}')
    print(f'project_id={project["project_id"]}')
    print(f'candidate_id={candidate["id"]}')
    print(f'company_name={candidate.get("company_name", "Não verificado")}')
    print(f'final_score={score}')
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_store()
    requests = load_requests()
    actions = load_actions()
    latest_req = requests[-1] if requests else None
    latest_project = latest_project_for_request(str(latest_req['request_id'])) if latest_req else None
    candidates = summarize_candidates(str((latest_project or {}).get('project_id') or '')) if latest_project else []
    summary = request_summary_payload(latest_req or {'request_id': 'Não verificado', 'status': 'unknown'}, latest_project, latest_product_intelligence_for_request(str((latest_req or {}).get('request_id') or '')), candidates, {
        'report_json': str((latest_project or {}).get('report_path') or ''),
        'report_pdf': str((latest_project or {}).get('report_pdf_path') or ''),
        'proposal_pdf': '',
    }) if latest_req else {
        'request_id': 'Não verificado',
        'sourcing_project_id': 'Não verificado',
        'category': 'Não verificado',
        'subcategory': 'Não verificado',
        'available_actions': [],
        'manufacturer_count': 0,
    }
    print('OPEN WEBUI BRIDGE STATS')
    print(f'requests_total={len(requests)}')
    print(f'actions_total={len(actions)}')
    print(f'latest_request_id={summary["request_id"]}')
    print(f'latest_sourcing_project_id={summary.get("sourcing_project_id", "Não verificado")}')
    print(f'latest_category={summary["category"]}')
    print(f'latest_subcategory={summary["subcategory"]}')
    print(f'latest_manufacturer_count={summary.get("manufacturer_count", 0)}')
    print('latest_available_actions=' + ', '.join(summary.get('available_actions', [])))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Open WebUI bridge for Hermes Mail')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate bridge files and dry-run prerequisites')
    p_validate.set_defaults(func=cmd_validate)

    p_create = sub.add_parser('create-test-request', help='Create a sample Open WebUI sourcing request')
    p_create.set_defaults(func=cmd_create_test_request)

    p_process = sub.add_parser('process-latest', help='Process the latest Open WebUI request')
    p_process.set_defaults(func=cmd_process_latest)

    p_approve = sub.add_parser('approve-test-supplier', help='Approve the latest test supplier in dry-run mode')
    p_approve.set_defaults(func=cmd_approve_test_supplier)

    p_reject = sub.add_parser('reject-test-supplier', help='Reject the latest test supplier in dry-run mode')
    p_reject.set_defaults(func=cmd_reject_test_supplier)

    p_stats = sub.add_parser('stats', help='Show Open WebUI bridge statistics')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
