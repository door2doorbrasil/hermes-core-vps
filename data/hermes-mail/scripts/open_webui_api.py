#!/usr/bin/env python3
"""Local Open WebUI API server for Hermes Mail.

Dry-run only. This API provides a simple local HTTP surface for Open WebUI
(or a local bridge) to create sourcing requests, inspect request state,
read reports, and approve/reject suppliers without sending real e-mail or
real RFQs.

FastAPI is optional. If it is not installed, the script falls back to the
stdlib http.server implementation.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import re
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from reporting_utils import (
    OPEN_WEBUI_ACTIONS_JSONL,
    OPEN_WEBUI_REQUESTS_JSONL,
    MANUFACTURER_DISCOVERY_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    PRODUCT_CATEGORIES_JSONL,
    PRODUCT_COMPLIANCE_RULES_JSONL,
    PRODUCT_INTELLIGENCE_JSONL,
    PRODUCT_SOURCING_SOURCES_JSONL,
    PROCUREMENT_KB_JSONL,
    SOURCING_PROJECTS_JSONL,
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

import global_product_intelligence as gpi
import sourcing_research as sr
from user_identity import audit_identity, permission_allowed, resolve_identity

FASTAPI_AVAILABLE = importlib.util.find_spec('fastapi') is not None
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8787
SERVICE_NAME = 'open_webui_api'
API_VERSION = '1.0.0'
REQUEST_SOURCE = 'open_webui_api'
DRY_RUN_MODE = True

REQUEST_JSONL_FILES = [
    OPEN_WEBUI_REQUESTS_JSONL,
    OPEN_WEBUI_ACTIONS_JSONL,
    PRODUCT_INTELLIGENCE_JSONL,
    PRODUCT_CATEGORIES_JSONL,
    PRODUCT_COMPLIANCE_RULES_JSONL,
    PRODUCT_SOURCING_SOURCES_JSONL,
    PROCUREMENT_KB_JSONL,
    SOURCING_PROJECTS_JSONL,
    MANUFACTURER_QUALIFICATION_JSONL,
    MANUFACTURER_DISCOVERY_JSONL,
]

TEST_REQUEST_PAYLOAD = {
    'product_description': 'Semi-automatic wafer cone making machine, 1000-1200 pcs/hour, electric heating, 380V 60Hz 3-phase, with mixer, dosing, baking and cone rolling unit.',
    'region': 'global',
    'quantity': '1200 pcs/hour',
    'application': 'ice cream cone manufacturing',
    'customer': 'Hermes Mail dry-run client',
    'notes': 'Dry-run API test request created from Open WebUI API server.',
    'attachments': [],
}


@dataclass(slots=True)
class ProcessResult:
    request: dict[str, Any]
    project: dict[str, Any] | None
    summary: dict[str, Any]
    raw_output: str
    status_code: int


def now() -> str:
    return utc_now()


def ensure_store() -> None:
    ensure_runtime_dirs()
    for path in REQUEST_JSONL_FILES:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


def framework_name() -> str:
    return 'fastapi' if FASTAPI_AVAILABLE else 'stdlib-http-server'


def json_response_payload(ok: bool, data: Any | None = None, error: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'ok': ok,
        'service': SERVICE_NAME,
        'framework': framework_name(),
        'dry_run': DRY_RUN_MODE,
        'timestamp': now(),
    }
    if ok:
        payload['data'] = data
    else:
        payload['error'] = error or 'unknown error'
    payload.update(extra)
    return payload


def resolve_actor(headers: Any, *, context: str = 'buy') -> dict[str, Any]:
    identity = resolve_identity(headers=headers, context=context)
    return identity


def append_request_state(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        'id': record.get('request_id') or make_id('open_webui_request'),
        'request_id': record.get('request_id') or make_id('open_webui_request'),
        'created_at': now(),
        'updated_at': now(),
        'source_channel': REQUEST_SOURCE,
        'mode': 'dry_run',
        'dry_run': True,
        **record,
    }
    append_jsonl(OPEN_WEBUI_REQUESTS_JSONL, payload)
    return payload


def append_action_state(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        'id': make_id('open_webui_action'),
        'created_at': now(),
        'updated_at': now(),
        'source_channel': REQUEST_SOURCE,
        'mode': 'dry_run',
        'dry_run': True,
        **record,
    }
    append_jsonl(OPEN_WEBUI_ACTIONS_JSONL, payload)
    return payload


def latest_request(request_id: str) -> dict[str, Any] | None:
    return latest_jsonl_record(OPEN_WEBUI_REQUESTS_JSONL, lambda rec: rec.get('request_id') == request_id)


def latest_action(request_id: str) -> dict[str, Any] | None:
    return latest_jsonl_record(OPEN_WEBUI_ACTIONS_JSONL, lambda rec: rec.get('request_id') == request_id)


def load_project_state(project: dict[str, Any] | None) -> dict[str, Any] | None:
    if not project:
        return None
    project_id = str(project.get('project_id') or '')
    if not project_id:
        return project
    state_path = sr.project_dir(project_id) / 'project.json'
    if not state_path.exists():
        return project
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:
        return project
    if isinstance(state, dict):
        merged = dict(project)
        merged.update(state)
        return merged
    return project


def latest_project(request_id: str) -> dict[str, Any] | None:
    project = latest_jsonl_record(
        SOURCING_PROJECTS_JSONL,
        lambda rec: rec.get('open_webui_request_id') == request_id or rec.get('request_id') == request_id,
    )
    return load_project_state(project)


def latest_intelligence(request_id: str) -> dict[str, Any] | None:
    return latest_jsonl_record(PRODUCT_INTELLIGENCE_JSONL, lambda rec: rec.get('open_webui_request_id') == request_id)


def load_project_report(project: dict[str, Any] | None) -> dict[str, Any] | None:
    if not project:
        return None
    report_path = str(project.get('report_path') or '')
    if not report_path:
        return None
    path = Path(report_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def project_candidates(project_id: str) -> list[dict[str, Any]]:
    records = [rec for rec in load_jsonl_records(MANUFACTURER_QUALIFICATION_JSONL) if rec.get('project_id') == project_id]
    return sorted(records, key=sr.final_supplier_score, reverse=True)


def serialize_candidate(record: dict[str, Any]) -> dict[str, Any]:
    profile = record.get('profile') if isinstance(record.get('profile'), dict) else {}
    return {
        'id': record.get('id'),
        'company_name': record.get('company_name') or profile.get('company_name') or 'Não verificado',
        'website': record.get('website') or profile.get('website') or 'Não verificado',
        'manufacturer_score': record.get('manufacturer_score', 'Não verificado'),
        'supplier_trust_score': record.get('supplier_trust_score', 'Não verificado'),
        'final_score': sr.final_supplier_score(record),
        'approved_for_rfq': bool(record.get('approved_for_rfq')),
        'manual_review_required': bool(record.get('manual_review_required')),
        'qualification_status': record.get('qualification_status', 'Não verificado'),
        'country': profile.get('country') or record.get('project_country') or 'Não verificado',
        'contact_person': profile.get('contact_person', 'Não verificado'),
        'email': profile.get('email', 'Não verificado'),
        'telephone': profile.get('telephone', 'Não verificado'),
    }


def available_actions(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return ['retry_processing', 'consult_history']
    premium = any(sr.final_supplier_score(rec) >= 80 for rec in records)
    manual = any(60 <= sr.final_supplier_score(rec) < 80 for rec in records)
    if premium:
        return ['approve_rfq_dry_run', 'reject_supplier', 'view_report', 'consult_history']
    if manual:
        return ['approve_supplier', 'reject_supplier', 'view_report', 'consult_history']
    return ['reject_supplier', 'view_report', 'consult_history']


def derive_status(project: dict[str, Any] | None, records: list[dict[str, Any]]) -> str:
    if project:
        status = str(project.get('status') or '')
        if status in {'rfq_approved', 'approved', 'manual_approved'}:
            return 'rfq_ready'
        if status in {'reported', 'qualified', 'discovery_qualified'}:
            if any(sr.final_supplier_score(rec) >= 80 for rec in records):
                return 'rfq_ready'
            if any(60 <= sr.final_supplier_score(rec) < 80 for rec in records):
                return 'manual_review'
            return 'blocked'
    if any(sr.final_supplier_score(rec) >= 80 for rec in records):
        return 'rfq_ready'
    if any(60 <= sr.final_supplier_score(rec) < 80 for rec in records):
        return 'manual_review'
    if records:
        return 'blocked'
    return 'processing'


def summarize_request(request_id: str) -> dict[str, Any]:
    request = latest_request(request_id)
    project = latest_project(request_id)
    records = project_candidates(str((project or {}).get('project_id') or '')) if project else []
    report = load_project_report(project)
    top_candidates = [serialize_candidate(rec) for rec in records[:5]]
    status = derive_status(project, records)
    premium_count = sum(1 for rec in records if sr.final_supplier_score(rec) >= 80)
    manual_count = sum(1 for rec in records if 60 <= sr.final_supplier_score(rec) < 80)
    blocked_count = sum(1 for rec in records if sr.final_supplier_score(rec) < 60)
    return {
        'request': request,
        'project': project,
        'status': status,
        'request_id': request_id,
        'sourcing_project_id': (project or {}).get('project_id') or 'Não verificado',
        'product_intelligence_id': (project or {}).get('product_intelligence_id') or (request or {}).get('product_intelligence_id') or 'Não verificado',
        'category': (project or {}).get('category_label') or (request or {}).get('category_label') or 'Não verificado',
        'subcategory': (project or {}).get('subcategory') or (request or {}).get('subcategory') or 'Não verificado',
        'candidate_count': len(records),
        'premium_count': premium_count,
        'manual_review_count': manual_count,
        'blocked_count': blocked_count,
        'available_actions': available_actions(records),
        'top_candidates': top_candidates,
        'report_json': (project or {}).get('report_path') or 'Não verificado',
        'report_pdf': (project or {}).get('report_pdf_path') or 'Não verificado',
        'report': report,
    }


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    product_description = normalize_text(str(payload.get('product_description') or ''))
    if not product_description:
        raise ValueError('product_description is required')
    attachments = payload.get('attachments')
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        raise ValueError('attachments must be an array')
    return {
        'product_description': product_description,
        'region': normalize_text(str(payload.get('region') or 'global')) or 'global',
        'quantity': payload.get('quantity', ''),
        'application': normalize_text(str(payload.get('application') or '')),
        'customer': normalize_text(str(payload.get('customer') or '')),
        'notes': normalize_text(str(payload.get('notes') or '')),
        'attachments': attachments,
        'country': normalize_text(str(payload.get('country') or 'global')) or 'global',
    }


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get('Content-Length') or '0')
    raw = handler.rfile.read(length) if length > 0 else b''
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        raise ValueError(f'invalid JSON body: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('request body must be a JSON object')
    return parsed


def parse_key_value(output: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}=(.+)$', output, flags=re.MULTILINE)
    return match.group(1).strip() if match else ''


def run_sourcing_workflow(request: dict[str, Any]) -> ProcessResult:
    args = argparse.Namespace(
        product=request['product_description'],
        region=request.get('region') or 'global',
        country=request.get('country') or 'global',
        category='',
        telegram_text='',
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        rc = sr.cmd_run_with_intelligence(args)
    raw_output = buffer.getvalue()
    if rc != 0:
        raise RuntimeError(raw_output.strip() or 'sourcing workflow failed')
    project_id = parse_key_value(raw_output, 'project_id')
    if not project_id:
        project = latest_project(request['request_id'])
        project_id = str((project or {}).get('project_id') or '')
    project = sr.lookup_project(project_id) if project_id else latest_project(request['request_id'])
    if project:
        project = load_project_state(project)
        enriched = dict(project)
        enriched.update({
            'open_webui_request_id': request['request_id'],
            'open_webui_source_channel': REQUEST_SOURCE,
            'open_webui_product_description': request['product_description'],
            'open_webui_region': request.get('region') or 'global',
            'open_webui_country': request.get('country') or 'global',
            'open_webui_quantity': request.get('quantity'),
            'open_webui_application': request.get('application') or '',
            'open_webui_customer': request.get('customer') or '',
            'open_webui_notes': request.get('notes') or '',
            'open_webui_attachments': request.get('attachments') or [],
            'updated_at': now(),
        })
        sr.save_project_state(enriched)
        append_jsonl(SOURCING_PROJECTS_JSONL, enriched)
        project = enriched
    summary = summarize_request(request['request_id'])
    return ProcessResult(request=request, project=project, summary=summary, raw_output=raw_output, status_code=200)


def persist_request_start(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    actor = payload.get('actor') if isinstance(payload.get('actor'), dict) else {}
    request = append_request_state({
        'request_id': request_id,
        'status': 'requested',
        'stage': 'received',
        'source_channel': REQUEST_SOURCE,
        'mode': 'dry_run',
        'dry_run': True,
        'actor': actor,
        'actor_email': (actor.get('user', {}) or {}).get('login_email', '') if isinstance(actor, dict) else '',
        'actor_profile': (actor.get('permissions', {}) or {}).get('role', '') if isinstance(actor, dict) else '',
        'actor_company': (actor.get('company', {}) or {}).get('name', '') if isinstance(actor, dict) else '',
        'actor_agent': ((actor.get('permissions', {}) or {}).get('allowed_agents') or [''])[0] if isinstance(actor, dict) else '',
        'product_description': payload['product_description'],
        'region': payload['region'],
        'country': payload['country'],
        'quantity': payload.get('quantity', ''),
        'application': payload.get('application', ''),
        'customer': payload.get('customer', ''),
        'notes': payload.get('notes', ''),
        'attachments': payload.get('attachments', []),
        'attachment_count': len(payload.get('attachments', [])),
        'language': 'pt-BR',
        'rfq_language': 'en',
        'project_id': '',
        'product_intelligence_id': '',
        'category_id': '',
        'category_label': '',
        'subcategory': '',
        'report_json': '',
        'report_pdf': '',
    })
    append_action_state({
        'request_id': request_id,
        'actor': actor,
        'actor_email': request.get('actor_email', ''),
        'actor_profile': request.get('actor_profile', ''),
        'actor_company': request.get('actor_company', ''),
        'actor_agent': request.get('actor_agent', ''),
        'action_type': 'request_received',
        'status': 'queued',
        'product_description': payload['product_description'],
        'region': payload['region'],
        'quantity': payload.get('quantity', ''),
        'application': payload.get('application', ''),
        'customer': payload.get('customer', ''),
        'attachments': payload.get('attachments', []),
    })
    return request


def update_request(request_id: str, **updates: Any) -> dict[str, Any]:
    current = latest_request(request_id) or {'request_id': request_id}
    current.update(updates)
    current['updated_at'] = now()
    current['mode'] = 'dry_run'
    current['dry_run'] = True
    append_jsonl(OPEN_WEBUI_REQUESTS_JSONL, current)
    return current


def process_request_payload(payload: dict[str, Any]) -> ProcessResult:
    ensure_store()
    actor = payload.get('actor') if isinstance(payload.get('actor'), dict) else resolve_identity(context='buy')
    if not permission_allowed(actor, 'rfq'):
        raise PermissionError('perfil sem permissão para sourcing/RFQ')
    normalized = normalize_payload(payload)
    normalized['actor'] = actor
    request_id = make_id('open_webui_request')
    request = persist_request_start(normalized, request_id)
    update_request(request_id, status='processing', stage='processing_started')
    append_action_state({
        'request_id': request_id,
        'actor': actor,
        'actor_email': request.get('actor_email', ''),
        'actor_profile': request.get('actor_profile', ''),
        'actor_company': request.get('actor_company', ''),
        'actor_agent': request.get('actor_agent', ''),
        'action_type': 'processing_started',
        'status': 'processing',
        'message': 'Global Product Intelligence will run before sourcing.',
    })
    try:
        result = run_sourcing_workflow(request)
        summary = result.summary
        project = result.project or {}
        request_update = update_request(
            request_id,
            status=summary['status'],
            stage='completed',
            project_id=summary['sourcing_project_id'],
            product_intelligence_id=summary['product_intelligence_id'],
            category=summary['category'],
            category_label=summary['category'],
            subcategory=summary['subcategory'],
            report_json=summary['report_json'],
            report_pdf=summary['report_pdf'],
            available_actions=summary['available_actions'],
            candidate_count=summary['candidate_count'],
            premium_count=summary['premium_count'],
            manual_review_count=summary['manual_review_count'],
            blocked_count=summary['blocked_count'],
        )
        append_action_state({
            'request_id': request_id,
            'actor': actor,
            'actor_email': request.get('actor_email', ''),
            'actor_profile': request.get('actor_profile', ''),
            'actor_company': request.get('actor_company', ''),
            'actor_agent': request.get('actor_agent', ''),
            'project_id': summary['sourcing_project_id'],
            'product_intelligence_id': summary['product_intelligence_id'],
            'action_type': 'workflow_completed',
            'status': summary['status'],
            'category': summary['category'],
            'subcategory': summary['subcategory'],
            'candidate_count': summary['candidate_count'],
            'premium_count': summary['premium_count'],
            'manual_review_count': summary['manual_review_count'],
            'blocked_count': summary['blocked_count'],
            'available_actions': summary['available_actions'],
            'report_json': summary['report_json'],
            'report_pdf': summary['report_pdf'],
        })
        if summary['status'] == 'rfq_ready':
            append_action_state({
                'request_id': request_id,
                'actor': actor,
                'actor_email': request.get('actor_email', ''),
                'actor_profile': request.get('actor_profile', ''),
                'actor_company': request.get('actor_company', ''),
                'actor_agent': request.get('actor_agent', ''),
                'project_id': summary['sourcing_project_id'],
                'action_type': 'rfq_ready_dry_run',
                'status': 'approved_for_rfq',
                'message': 'At least one supplier scored >= 80; RFQ is approved only in dry-run.',
            })
        return ProcessResult(request=request_update, project=project, summary=summary, raw_output=result.raw_output, status_code=200)
    except Exception as exc:
        update_request(request_id, status='error', stage='failed', error=str(exc))
        append_action_state({
            'request_id': request_id,
            'actor': actor,
            'actor_email': request.get('actor_email', ''),
            'actor_profile': request.get('actor_profile', ''),
            'actor_company': request.get('actor_company', ''),
            'actor_agent': request.get('actor_agent', ''),
            'action_type': 'processing_failed',
            'status': 'error',
            'message': str(exc),
        })
        raise


def select_candidate(project_id: str, candidate_id: str | None = None, *, mode: str = 'approve') -> dict[str, Any] | None:
    records = project_candidates(project_id)
    if candidate_id:
        target = next((rec for rec in records if rec.get('id') == candidate_id), None)
        if target:
            return target
    if mode == 'approve':
        band = [rec for rec in records if 60 <= sr.final_supplier_score(rec) < 80]
        if band:
            return band[0]
        premium = [rec for rec in records if sr.final_supplier_score(rec) >= 80]
        if premium:
            return premium[0]
    return records[-1] if records else None


def approve_supplier(request_id: str, candidate_id: str | None = None, *, actor: dict[str, Any] | None = None) -> dict[str, Any]:
    actor = actor or resolve_identity(context='buy')
    if not permission_allowed(actor, 'supplier_approval') and not permission_allowed(actor, 'rfq'):
        raise PermissionError('perfil sem permissão para aprovar RFQ/proposta')
    project = latest_project(request_id)
    if not project or not project.get('project_id'):
        raise ValueError('no sourcing project found for request')
    candidate = select_candidate(str(project['project_id']), candidate_id, mode='approve')
    if not candidate:
        raise ValueError('no candidate found')
    score = sr.final_supplier_score(candidate)
    if score < 60:
        raise ValueError('candidate is below approval threshold')
    if score >= 80:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rc = sr.cmd_approve_for_rfq(argparse.Namespace(project_id=project['project_id']))
        if rc != 0:
            raise RuntimeError(buffer.getvalue().strip() or 'approve-for-rfq failed')
        action_type = 'supplier_approved_for_rfq'
        status = 'approved_for_rfq'
    else:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rc = sr.cmd_manual_approve(argparse.Namespace(project_id=project['project_id'], candidate_id=candidate['id']))
        if rc != 0:
            raise RuntimeError(buffer.getvalue().strip() or 'manual-approve failed')
        action_type = 'supplier_manual_approved'
        status = 'manual_approved'
    append_action_state({
        'request_id': request_id,
        'actor': actor,
        'actor_email': actor.get('user', {}).get('login_email', ''),
        'actor_profile': actor.get('permissions', {}).get('role', ''),
        'actor_company': actor.get('company', {}).get('name', ''),
        'actor_agent': (actor.get('permissions', {}).get('allowed_agents') or [''])[0],
        'project_id': project['project_id'],
        'candidate_id': candidate['id'],
        'action_type': action_type,
        'status': status,
        'company_name': candidate.get('company_name', 'Não verificado'),
        'final_score': score,
    })
    update_request(request_id, status=status, stage='supplier_approved', approved_candidate_id=candidate['id'], approved_candidate_score=score)
    summary = summarize_request(request_id)
    return {
        'request_id': request_id,
        'project_id': project['project_id'],
        'candidate': serialize_candidate(candidate),
        'status': status,
        'summary': summary,
    }


def reject_supplier(request_id: str, candidate_id: str | None = None, *, actor: dict[str, Any] | None = None) -> dict[str, Any]:
    actor = actor or resolve_identity(context='buy')
    if not permission_allowed(actor, 'supplier_approval') and not permission_allowed(actor, 'rfq'):
        raise PermissionError('perfil sem permissão para rejeitar RFQ/proposta')
    project = latest_project(request_id)
    if not project or not project.get('project_id'):
        raise ValueError('no sourcing project found for request')
    candidate = select_candidate(str(project['project_id']), candidate_id, mode='reject')
    if not candidate:
        raise ValueError('no candidate found')
    score = sr.final_supplier_score(candidate)
    append_action_state({
        'request_id': request_id,
        'actor': actor,
        'actor_email': actor.get('user', {}).get('login_email', ''),
        'actor_profile': actor.get('permissions', {}).get('role', ''),
        'actor_company': actor.get('company', {}).get('name', ''),
        'actor_agent': (actor.get('permissions', {}).get('allowed_agents') or [''])[0],
        'project_id': project['project_id'],
        'candidate_id': candidate['id'],
        'action_type': 'supplier_rejected',
        'status': 'rejected',
        'company_name': candidate.get('company_name', 'Não verificado'),
        'final_score': score,
    })
    update_request(request_id, status='rejected', stage='supplier_rejected', rejected_candidate_id=candidate['id'], rejected_candidate_score=score)
    summary = summarize_request(request_id)
    return {
        'request_id': request_id,
        'project_id': project['project_id'],
        'candidate': serialize_candidate(candidate),
        'status': 'rejected',
        'summary': summary,
    }


def get_request_detail(request_id: str) -> dict[str, Any]:
    ensure_store()
    summary = summarize_request(request_id)
    request = summary['request']
    if not request:
        raise KeyError(request_id)
    return {
        'request_id': request_id,
        'request': request,
        'project': summary['project'],
        'summary': {
            'status': summary['status'],
            'candidate_count': summary['candidate_count'],
            'premium_count': summary['premium_count'],
            'manual_review_count': summary['manual_review_count'],
            'blocked_count': summary['blocked_count'],
            'available_actions': summary['available_actions'],
            'report_json': summary['report_json'],
            'report_pdf': summary['report_pdf'],
        },
        'top_candidates': summary['top_candidates'],
        'report_available': bool(summary['report']),
    }


def get_request_status(request_id: str) -> dict[str, Any]:
    ensure_store()
    summary = summarize_request(request_id)
    if not summary['request']:
        raise KeyError(request_id)
    return {
        'request_id': request_id,
        'status': summary['status'],
        'sourcing_project_id': summary['sourcing_project_id'],
        'product_intelligence_id': summary['product_intelligence_id'],
        'category': summary['category'],
        'subcategory': summary['subcategory'],
        'candidate_count': summary['candidate_count'],
        'premium_count': summary['premium_count'],
        'manual_review_count': summary['manual_review_count'],
        'blocked_count': summary['blocked_count'],
        'available_actions': summary['available_actions'],
        'top_candidate': summary['top_candidates'][0] if summary['top_candidates'] else None,
        'report_json': summary['report_json'],
        'report_pdf': summary['report_pdf'],
    }


def get_request_report(request_id: str) -> dict[str, Any]:
    ensure_store()
    summary = summarize_request(request_id)
    if not summary['request']:
        raise KeyError(request_id)
    if not summary['report']:
        raise FileNotFoundError('report not available yet')
    return {
        'request_id': request_id,
        'report_json': summary['report_json'],
        'report_pdf': summary['report_pdf'],
        'report': summary['report'],
    }


def stats_payload() -> dict[str, Any]:
    ensure_store()
    requests = load_jsonl_records(OPEN_WEBUI_REQUESTS_JSONL)
    actions = load_jsonl_records(OPEN_WEBUI_ACTIONS_JSONL)
    latest = requests[-1] if requests else None
    latest_summary = summarize_request(str(latest.get('request_id') or '')) if latest else None
    return {
        'service': SERVICE_NAME,
        'framework': framework_name(),
        'dry_run': DRY_RUN_MODE,
        'fastapi_available': FASTAPI_AVAILABLE,
        'requests_total': len(requests),
        'actions_total': len(actions),
        'latest_request_id': (latest or {}).get('request_id', 'Não verificado'),
        'latest_status': (latest_summary or {}).get('status', 'Não verificado'),
        'latest_sourcing_project_id': (latest_summary or {}).get('sourcing_project_id', 'Não verificado'),
        'latest_category': (latest_summary or {}).get('category', 'Não verificado'),
        'latest_subcategory': (latest_summary or {}).get('subcategory', 'Não verificado'),
        'latest_candidate_count': (latest_summary or {}).get('candidate_count', 0),
        'latest_available_actions': (latest_summary or {}).get('available_actions', []),
    }


def simulate_post_request() -> dict[str, Any]:
    result = process_request_payload(TEST_REQUEST_PAYLOAD)
    return {
        'request_id': result.request['request_id'],
        'status': result.summary['status'],
        'sourcing_project_id': result.summary['sourcing_project_id'],
        'category': result.summary['category'],
        'subcategory': result.summary['subcategory'],
        'candidate_count': result.summary['candidate_count'],
        'available_actions': result.summary['available_actions'],
        'top_candidates': result.summary['top_candidates'],
        'report_json': result.summary['report_json'],
        'report_pdf': result.summary['report_pdf'],
        'fastapi_available': FASTAPI_AVAILABLE,
    }


class OpenWebUIAPIHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, message: str = 'not found') -> None:
        self._write_json(404, json_response_payload(False, error=message))

    def _bad_request(self, message: str) -> None:
        self._write_json(400, json_response_payload(False, error=message))

    def _server_error(self, message: str) -> None:
        self._write_json(500, json_response_payload(False, error=message))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        try:
            if path == '/health':
                self._write_json(200, json_response_payload(True, data={
                    'service': SERVICE_NAME,
                    'framework': framework_name(),
                    'dry_run': DRY_RUN_MODE,
                    'fastapi_available': FASTAPI_AVAILABLE,
                    'message': 'ok',
                }))
                return
            if path.startswith('/sourcing/request/'):
                parts = path.split('/')
                if len(parts) == 4:
                    request_id = unquote(parts[3])
                    self._write_json(200, json_response_payload(True, data=get_request_detail(request_id)))
                    return
                if len(parts) == 5 and parts[4] == 'status':
                    request_id = unquote(parts[3])
                    self._write_json(200, json_response_payload(True, data=get_request_status(request_id)))
                    return
                if len(parts) == 5 and parts[4] == 'report':
                    request_id = unquote(parts[3])
                    self._write_json(200, json_response_payload(True, data=get_request_report(request_id)))
                    return
            self._not_found()
        except KeyError:
            self._write_json(404, json_response_payload(False, error='request not found'))
        except FileNotFoundError as exc:
            self._write_json(404, json_response_payload(False, error=str(exc)))
        except Exception as exc:
            self._server_error(str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        try:
            body = read_json_body(self)
            if path == '/sourcing/request':
                actor = resolve_actor(self.headers, context='buy')
                result = process_request_payload({**body, 'actor': actor})
                self._write_json(200, json_response_payload(True, data=result.summary, actor=result.request.get('actor', actor)))
                return
            if path.startswith('/sourcing/request/'):
                parts = path.split('/')
                if len(parts) == 5 and parts[4] in {'approve-supplier', 'reject-supplier'}:
                    request_id = unquote(parts[3])
                    candidate_id = normalize_text(str(body.get('candidate_id') or '')) or None
                    actor = resolve_actor(self.headers, context='buy')
                    if parts[4] == 'approve-supplier':
                        data = approve_supplier(request_id, candidate_id=candidate_id, actor=actor)
                        self._write_json(200, json_response_payload(True, data=data, actor=actor))
                        return
                    data = reject_supplier(request_id, candidate_id=candidate_id, actor=actor)
                    self._write_json(200, json_response_payload(True, data=data, actor=actor))
                    return
            self._not_found()
        except ValueError as exc:
            self._bad_request(str(exc))
        except KeyError:
            self._write_json(404, json_response_payload(False, error='request not found'))
        except Exception as exc:
            self._server_error(str(exc))


def run_test_server(host: str, port: int) -> None:
    ensure_store()
    server = ThreadingHTTPServer((host, port), OpenWebUIAPIHandler)
    print(f'OPEN WEBUI API SERVER STARTED host={host} port={port} framework={framework_name()} dry_run={DRY_RUN_MODE}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_store()
    errors: list[str] = []
    for path in REQUEST_JSONL_FILES:
        if not path.exists():
            errors.append(f'missing file: {path}')
        else:
            try:
                load_jsonl_records(path)
            except Exception as exc:
                errors.append(f'invalid jsonl {path}: {exc}')
    for path in [
        ROOT / 'scripts' / 'sourcing_research.py',
        ROOT / 'scripts' / 'global_product_intelligence.py',
    ]:
        if not path.exists():
            errors.append(f'missing script: {path}')
    if errors:
        payload = json_response_payload(False, error='validation failed', details=errors)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    payload = json_response_payload(True, data={
        'root': str(ROOT),
        'requests_jsonl': str(OPEN_WEBUI_REQUESTS_JSONL),
        'actions_jsonl': str(OPEN_WEBUI_ACTIONS_JSONL),
        'framework': framework_name(),
        'fastapi_available': FASTAPI_AVAILABLE,
        'dry_run': DRY_RUN_MODE,
        'endpoints': [
            'GET /health',
            'POST /sourcing/request',
            'GET /sourcing/request/{request_id}',
            'GET /sourcing/request/{request_id}/status',
            'GET /sourcing/request/{request_id}/report',
            'POST /sourcing/request/{request_id}/approve-supplier',
            'POST /sourcing/request/{request_id}/reject-supplier',
        ],
    })
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_simulate_post_request(_: argparse.Namespace) -> int:
    try:
        data = simulate_post_request()
        print(json.dumps(json_response_payload(True, data=data), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(json_response_payload(False, error=str(exc)), ensure_ascii=False, indent=2, sort_keys=True))
        return 1


def cmd_stats(_: argparse.Namespace) -> int:
    print(json.dumps(json_response_payload(True, data=stats_payload()), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Local Open WebUI API server for Hermes Mail')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate files, imports, and endpoint wiring')
    p_validate.set_defaults(func=cmd_validate)

    p_run = sub.add_parser('run-test-server', help='Start the local HTTP server for Open WebUI integration')
    p_run.add_argument('--host', default=DEFAULT_HOST)
    p_run.add_argument('--port', type=int, default=DEFAULT_PORT)
    p_run.set_defaults(func=lambda args: run_test_server(args.host, args.port) or 0)

    p_sim = sub.add_parser('simulate-post-request', help='Run a full dry-run request without HTTP')
    p_sim.set_defaults(func=cmd_simulate_post_request)

    p_stats = sub.add_parser('stats', help='Show request/action statistics as JSON')
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result or 0)


if __name__ == '__main__':
    raise SystemExit(main())
