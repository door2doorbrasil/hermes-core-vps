#!/usr/bin/env python3
"""Unified Hermes Mail pipeline.

Current implementation supports validate, run-simulated, and status.
The IMAP and single-file modes are present as stubs for later expansion.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/opt/data/hermes-mail')
SCRIPTS = ROOT / 'scripts'
LOGS = ROOT / 'logs'
REPORTS = ROOT / 'reports'
REPORT_PATH = LOGS / 'pipeline-report.json'
STATE_PATH = ROOT / 'state' / 'pipeline-state.json'
ERROR_LOG_PATH = LOGS / 'error.log'
AUDIT_REPORT_PATH = LOGS / 'data-audit-report.json'

CONFIG_CHECK = SCRIPTS / 'email_config_check.py'
STRUCTURE_CHECK = SCRIPTS / 'hermes_mail_store.py'
AUDIT = SCRIPTS / 'data_audit.py'
PARSER = SCRIPTS / 'email_parser.py'
OUTBOX = SCRIPTS / 'email_outbox.py'
AUTO_REPLY = SCRIPTS / 'auto_reply_simulator.py'
INGESTOR = SCRIPTS / 'email_ingestor.py'
TELEGRAM_NOTIFIER = SCRIPTS / 'telegram_notifier.py'

JSONL_FILES = [
    ROOT / 'fornecedores.jsonl',
    ROOT / 'contatos.jsonl',
    ROOT / 'produtos.jsonl',
    ROOT / 'emails.jsonl',
    ROOT / 'cotacoes.jsonl',
    ROOT / 'price-history.jsonl',
    ROOT / 'anexos.jsonl',
    ROOT / 'supplier-reply-analysis.jsonl',
    ROOT / 'client-quotes.jsonl',
    ROOT / 'comparison-reports.jsonl',
    ROOT / 'sourcing-projects.jsonl',
    ROOT / 'manufacturer-research.jsonl',
    ROOT / 'manufacturer-qualification.jsonl',
    ROOT / 'supplier-performance.jsonl',
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def count_jsonl(path: Path) -> int:
    total = 0
    if path.exists():
        with path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                if raw.strip():
                    total += 1
    return total


def stats() -> dict[str, int]:
    return {path.name: count_jsonl(path) for path in JSONL_FILES}


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f'command failed: {" ".join(args)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}'
        )
    return result


def telegram_notify(event_type: str, message: str, *, source_event: str = '', pipeline_run_id: str = '', write_report: bool = False) -> str:
    args = [
        sys.executable,
        str(TELEGRAM_NOTIFIER),
        'notify-event',
        '--event-type',
        event_type,
        '--message',
        message,
    ]
    if source_event:
        args.extend(['--source-event', source_event])
    if pipeline_run_id:
        args.extend(['--pipeline-run-id', pipeline_run_id])
    if write_report:
        args.append('--write-report')
    result = run_cmd(args)
    return result.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return data


def save_report(report: dict[str, Any]) -> Path:
    LOGS.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open('w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return REPORT_PATH


def save_state(state: dict[str, Any]) -> Path:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open('w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return STATE_PATH


def save_report_to(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
    return path


def append_error_log(record: dict[str, Any]) -> None:
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG_PATH.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    with path.open('r', encoding='utf-8') as fh:
        for raw in fh:
            if raw.strip():
                total += 1
    return total


def latest_audit_summary() -> dict[str, Any]:
    if not AUDIT_REPORT_PATH.exists():
        return {}
    try:
        return load_json(AUDIT_REPORT_PATH)
    except Exception:
        return {}


def write_pipeline_state(*, last_mode: str, last_result: str, last_report_path: str, run_id: str | None = None, error_message: str | None = None, audited_total_records: int | None = None, issues_count: int | None = None) -> Path:
    audit_summary = latest_audit_summary()
    state = {
        'last_mode': last_mode,
        'last_run_at': now(),
        'last_result': last_result,
        'last_report_path': last_report_path,
        'last_run_id': run_id,
        'telegram_notifications_total': count_lines(LOGS / 'telegram-notifications.jsonl'),
        'audited_total_records': int(audited_total_records if audited_total_records is not None else audit_summary.get('total_records', 0) or 0),
        'issues_count': int(issues_count if issues_count is not None else audit_summary.get('issues_count', 0) or 0),
    }
    if error_message:
        state['last_error'] = error_message
    return save_state(state)


def load_json(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return data


def validate_environment() -> list[str]:
    errors: list[str] = []
    expected_files = [CONFIG_CHECK, STRUCTURE_CHECK, AUDIT, PARSER, OUTBOX, AUTO_REPLY, TELEGRAM_NOTIFIER,
                      SCRIPTS / 'supplier_reply_analyzer.py', SCRIPTS / 'client_quote_generator.py',
                      SCRIPTS / 'comparison_report_generator.py', SCRIPTS / 'sourcing_research.py']
    for path in expected_files:
        if not path.exists():
            errors.append(f'missing script: {path}')

    try:
        settings = load_json(ROOT / 'state' / 'settings.json')
    except Exception as exc:
        errors.append(f'cannot load settings.json: {exc}')
        return errors

    email_config = settings.get('email_config')
    if not isinstance(email_config, dict):
        errors.append('missing email_config section')
    else:
        if email_config.get('mode') != 'dry_run':
            errors.append('pipeline currently requires email_config.mode == dry_run')

    try:
        secrets = load_json(ROOT / 'state' / 'secrets.json')
    except Exception as exc:
        errors.append(f'cannot load secrets.json: {exc}')
    else:
        if 'email_password' not in secrets:
            errors.append('secrets.json missing email_password key')
        # Placeholder password is allowed in simulated mode; real IMAP tests
        # are gated by the separate read-only connector.

    return errors


def classify_latest() -> dict[str, Any]:
    email_path = ROOT / 'emails.jsonl'
    last = None
    if email_path.exists():
        with email_path.open('r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                last = json.loads(line)

    category = 'unknown'
    confidence = 0.0
    reason = 'no email found'
    if isinstance(last, dict):
        subject = str(last.get('subject', '')).lower()
        body = str(last.get('body_text', '')).lower()
        if 'cotação' in subject or 'cotacao' in subject or 'quote' in subject or 'preço' in body or 'preco' in body:
            category = 'quotation'
            confidence = 0.92
            reason = 'subject/body indicate quotation'
        elif last.get('direction') == 'outgoing':
            category = 'reply'
            confidence = 0.88
            reason = 'outgoing message'
        else:
            category = 'incoming_business'
            confidence = 0.81
            reason = 'incoming structured test message'

    classification = {
        'timestamp': now(),
        'category': category,
        'confidence': confidence,
        'reason': reason,
        'email_id': last.get('id') if isinstance(last, dict) else None,
    }
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / 'pipeline-classification.log').open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(classification, ensure_ascii=False, sort_keys=True) + '\n')
    return classification


def generate_entities_summary() -> dict[str, Any]:
    counts = stats()
    latest_ids: dict[str, str | None] = {}
    for name in ['fornecedores.jsonl', 'contatos.jsonl', 'produtos.jsonl', 'cotacoes.jsonl', 'price-history.jsonl']:
        path = ROOT / name
        last = None
        if path.exists():
            with path.open('r', encoding='utf-8') as fh:
                for raw in fh:
                    line = raw.strip()
                    if line:
                        last = json.loads(line)
        latest_ids[name] = last.get('id') if isinstance(last, dict) else None
    return {'counts': counts, 'latest_ids': latest_ids}


def run_pipeline_simulated() -> dict[str, Any]:
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []
    errors: list[str] = []

    def step(name: str, func):
        step_started = time.perf_counter()
        try:
            result = func()
            duration = time.perf_counter() - step_started
            steps.append({'name': name, 'status': 'ok', 'duration_seconds': round(duration, 3), 'result': result})
            return result
        except Exception as exc:
            duration = time.perf_counter() - step_started
            message = str(exc)
            errors.append(f'{name}: {message}')
            steps.append({'name': name, 'status': 'error', 'duration_seconds': round(duration, 3), 'error': message})
            raise

    # 1. validar configuração
    step('validar configuração', lambda: run_cmd([sys.executable, str(CONFIG_CHECK), 'validate']).stdout)

    # 2. validar estrutura
    step('validar estrutura', lambda: run_cmd([sys.executable, str(STRUCTURE_CHECK), 'validate']).stdout)

    # 3. validar auditoria
    step('validar auditoria', lambda: run_cmd([sys.executable, str(AUDIT), 'audit']).stdout)

    # 4. executar parser
    step('executar parser', lambda: run_cmd([sys.executable, str(INGESTOR), 'simulate']).stdout + run_cmd([sys.executable, str(PARSER), 'parse-latest']).stdout)

    # 5. executar classificação
    classification = step('executar classificação', classify_latest)

    # 6. gerar entidades
    entities = step('gerar entidades', generate_entities_summary)

    # 7. gerar draft de resposta
    draft_output = step('gerar draft de resposta', lambda: run_cmd([sys.executable, str(AUTO_REPLY), 'generate-latest']).stdout)

    # 8. executar auditoria final
    final_audit = step('executar auditoria final', lambda: run_cmd([sys.executable, str(AUDIT), 'audit']).stdout)

    # 9. gerar relatório final
    report_step = {
        'name': 'gerar relatório final',
        'status': 'ok',
        'duration_seconds': 0.0,
        'result': str(REPORT_PATH),
    }
    final_report = {
        'pipeline': 'run-simulated',
        'started_at': datetime.fromtimestamp(time.time(), timezone.utc).isoformat().replace('+00:00', 'Z'),
        'finished_at': now(),
        'duration_seconds': round(time.perf_counter() - started, 3),
        'steps': steps + [report_step],
        'classification': classification,
        'entities': entities,
        'final_audit_output': final_audit,
        'errors': errors,
        'success_percentage': None,
    }
    successful_steps = sum(1 for s in final_report['steps'] if s['status'] == 'ok')
    final_report['success_percentage'] = round((successful_steps / 9) * 100, 1)
    report_path = save_report(final_report)
    state_path = save_state({
        'last_run': final_report['finished_at'],
        'report_path': str(report_path),
        'steps': [s['name'] for s in steps],
        'success_percentage': final_report['success_percentage'],
    })
    final_report['report_path'] = str(report_path)
    final_report['state_path'] = str(state_path)
    return final_report


def run_pipeline_simulated_with_telegram() -> dict[str, Any]:
    started = time.perf_counter()
    run_id = uuid.uuid4().hex
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    telegram_events: list[dict[str, Any]] = []

    def note(event_type: str, message: str, *, source_event: str = '') -> None:
        output = telegram_notify(
            event_type,
            message,
            source_event=source_event or event_type,
            pipeline_run_id=run_id,
        )
        telegram_events.append({
            'event_type': event_type,
            'message': message,
            'output': output,
        })

    def step(name: str, func):
        step_started = time.perf_counter()
        try:
            result = func()
            duration = time.perf_counter() - step_started
            steps.append({'name': name, 'status': 'ok', 'duration_seconds': round(duration, 3), 'result': result})
            return result
        except Exception as exc:
            duration = time.perf_counter() - step_started
            message = str(exc)
            errors.append(f'{name}: {message}')
            steps.append({'name': name, 'status': 'error', 'duration_seconds': round(duration, 3), 'error': message})
            note('error', f'Erro no pipeline: {name}', source_event='pipeline_error')
            raise

    note('pipeline_started', 'Pipeline iniciado em modo dry_run.', source_event='pipeline_started')

    # 1. validar estrutura
    step('validar estrutura', lambda: run_cmd([sys.executable, str(STRUCTURE_CHECK), 'validate']).stdout)

    # 2. simulação de e-mail
    step('simulação de e-mail', lambda: run_cmd([sys.executable, str(INGESTOR), 'simulate']).stdout)
    note('new_email_processed', 'Novo e-mail simulado processado.', source_event='simulated_email_processed')

    # 3. parser
    step('parser', lambda: run_cmd([sys.executable, str(PARSER), 'parse-latest']).stdout)
    entities = step('entidades extraídas', generate_entities_summary)
    note('entities_extracted', 'Entidades extraídas com sucesso.', source_event='entities_extracted')

    # 4. geração de resposta
    draft_output = step('geração de resposta', lambda: run_cmd([sys.executable, str(AUTO_REPLY), 'generate-latest']).stdout)
    note('draft_generated', 'Draft de resposta gerado em modo dry_run.', source_event='draft_generated')

    # 5. auditoria
    final_audit = step('auditoria final', lambda: run_cmd([sys.executable, str(AUDIT), 'audit']).stdout)
    note('audit_ok', 'Auditoria final OK.', source_event='audit_ok')
    audited_total_records = None
    issues_count = None
    match = re.search(r'total_records=(\d+)', final_audit)
    if match:
        audited_total_records = int(match.group(1))
    match = re.search(r'issues_count=(\d+)', final_audit)
    if match:
        issues_count = int(match.group(1))

    # relatório final
    report_step = {
        'name': 'gerar relatório final',
        'status': 'ok',
        'duration_seconds': 0.0,
        'result': str(REPORT_PATH),
    }
    final_report = {
        'pipeline': 'run-simulated-with-telegram',
        'run_id': run_id,
        'started_at': datetime.fromtimestamp(time.time(), timezone.utc).isoformat().replace('+00:00', 'Z'),
        'finished_at': now(),
        'duration_seconds': round(time.perf_counter() - started, 3),
        'steps': steps + [report_step],
        'entities': entities,
        'draft_output': draft_output,
        'final_audit_output': final_audit,
        'telegram_notifications': telegram_events,
        'errors': errors,
        'success_percentage': None,
    }
    successful_steps = sum(1 for s in final_report['steps'] if s['status'] == 'ok')
    final_report['success_percentage'] = round((successful_steps / 7) * 100, 1)
    report_path = save_report(final_report)
    state_path = write_pipeline_state(
        last_mode='normal',
        last_result='ok' if not final_report['errors'] else 'error',
        last_report_path=str(report_path),
        run_id=run_id,
        audited_total_records=audited_total_records,
        issues_count=issues_count,
        error_message='; '.join(errors) or None,
    )
    final_report['report_path'] = str(report_path)
    final_report['state_path'] = str(state_path)
    return final_report


def cmd_validate(_: argparse.Namespace) -> int:
    errors = validate_environment()
    if errors:
        print('VALIDATION FAILED')
        for err in errors:
            print(err)
        return 1
    print('VALIDATION OK')
    print(f'root={ROOT}')
    print('pipeline=ready')
    return 0


def cmd_run_simulated(_: argparse.Namespace) -> int:
    report = run_pipeline_simulated()
    print('PIPELINE SIMULATED COMPLETE')
    print(f'report={REPORT_PATH}')
    print(f'success_percentage={report["success_percentage"]}')
    return 0 if not report['errors'] else 1


def cmd_run_simulated_with_telegram(args: argparse.Namespace) -> int:
    if getattr(args, 'simulate_error', False):
        run_id = uuid.uuid4().hex
        validation_errors = validate_environment()
        report_path = REPORTS / f'pipeline-error-alert-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json'
        try:
            raise RuntimeError('simulated controlled pipeline error')
        except Exception as exc:
            timestamp = now()
            error_record = {
                'timestamp': timestamp,
                'run_id': run_id,
                'event': 'pipeline_error',
                'message': str(exc),
                'error_type': exc.__class__.__name__,
                'dry_run': True,
                'validation_errors': validation_errors,
            }
            append_error_log(error_record)
            telegram_output = telegram_notify(
                'pipeline_error',
                'Erro simulado controlado no pipeline.',
                source_event='simulate_error_alert',
                pipeline_run_id=run_id,
                write_report=True,
            )
            report = {
                'pipeline': 'run-simulated-with-telegram',
                'mode': 'simulate-error',
                'run_id': run_id,
                'timestamp': timestamp,
                'validation_errors': validation_errors,
                'success': False,
                'simulated_error': error_record,
                'telegram_notification': {
                    'event_type': 'pipeline_error',
                    'message': 'Erro simulado controlado no pipeline.',
                    'output': telegram_output,
                },
                'error_log_path': str(ERROR_LOG_PATH),
            }
            save_report_to(report_path, report)
            write_pipeline_state(
                last_mode='simulate-error',
                last_result='error',
                last_report_path=str(report_path),
                run_id=run_id,
                error_message=str(exc),
            )
            print('PIPELINE SIMULATED WITH TELEGRAM - SIMULATED ERROR MODE')
            print(f'error={error_record["message"]}')
            print(f'telegram_event=pipeline_error')
            print(f'report={report_path}')
            return 0

    report = run_pipeline_simulated_with_telegram()
    write_pipeline_state(
        last_mode='normal',
        last_result='ok' if not report['errors'] else 'error',
        last_report_path=str(REPORT_PATH),
        run_id=report.get('run_id'),
        error_message='; '.join(report.get('errors', [])) or None,
    )
    print('PIPELINE SIMULATED WITH TELEGRAM COMPLETE')
    print(f'report={REPORT_PATH}')
    print(f'success_percentage={report["success_percentage"]}')
    print(f'telegram_notifications={len(report.get("telegram_notifications", []))}')
    return 0 if not report['errors'] else 1


def cmd_simulate_error_alert(_: argparse.Namespace) -> int:
    return cmd_run_simulated_with_telegram(argparse.Namespace(simulate_error=True))


def cmd_status(_: argparse.Namespace) -> int:
    state: dict[str, Any] = {}
    if STATE_PATH.exists():
        try:
            state = load_json(STATE_PATH)
        except Exception as exc:
            state = {'state_error': str(exc)}

    audit_summary = latest_audit_summary()
    status = {
        'root': str(ROOT),
        'state_path': str(STATE_PATH),
        'last_mode': state.get('last_mode', 'unknown'),
        'last_run_at': state.get('last_run_at', 'unknown'),
        'last_result': state.get('last_result', 'unknown'),
        'last_report_path': state.get('last_report_path', str(REPORT_PATH)),
        'telegram_notifications_total': state.get('telegram_notifications_total', count_lines(LOGS / 'telegram-notifications.jsonl')),
    }
    audit_total = state.get('audited_total_records', None)
    audit_issues = state.get('issues_count', None)
    if REPORT_PATH.exists():
        try:
            report = load_json(REPORT_PATH)
            status['last_report_summary'] = {
                'finished_at': report.get('finished_at'),
                'success_percentage': report.get('success_percentage'),
                'steps': [s.get('name') for s in report.get('steps', [])],
            }
            audit_output = str(report.get('final_audit_output') or '')
            match = re.search(r'total_records=(\d+)', audit_output)
            if match:
                audit_total = int(match.group(1))
            match = re.search(r'issues_count=(\d+)', audit_output)
            if match:
                audit_issues = int(match.group(1))
        except Exception as exc:
            status['last_report_error'] = str(exc)
    status['audited_total_records'] = audit_total if audit_total is not None else int(audit_summary.get('total_records', 0) or 0)
    status['issues_count'] = audit_issues if audit_issues is not None else int(audit_summary.get('issues_count', 0) or 0)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_run_imap(_: argparse.Namespace) -> int:
    print('run-imap is not implemented yet')
    return 1


def cmd_run_single(args: argparse.Namespace) -> int:
    print(f'run-single is not implemented yet: {args.eml_path}')
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Unified Hermes Mail pipeline')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate prerequisites for the unified pipeline')
    p_validate.set_defaults(func=cmd_validate)

    p_run_sim = sub.add_parser('run-simulated', help='Run the simulated end-to-end pipeline')
    p_run_sim.set_defaults(func=cmd_run_simulated)

    p_run_sim_tg = sub.add_parser('run-simulated-with-telegram', help='Run the simulated pipeline and emit Telegram dry-run notifications')
    p_run_sim_tg.add_argument('--simulate-error', action='store_true', help='Trigger a controlled QA error instead of the normal flow')
    p_run_sim_tg.set_defaults(func=cmd_run_simulated_with_telegram)

    p_sim_error = sub.add_parser('simulate-error-alert', help='Simulate a controlled pipeline error and send a Telegram dry-run alert')
    p_sim_error.set_defaults(func=cmd_simulate_error_alert)

    p_run_imap = sub.add_parser('run-imap', help='Placeholder for real IMAP execution')
    p_run_imap.set_defaults(func=cmd_run_imap)

    p_run_single = sub.add_parser('run-single', help='Placeholder for processing a single .eml file')
    p_run_single.add_argument('eml_path')
    p_run_single.set_defaults(func=cmd_run_single)

    p_status = sub.add_parser('status', help='Show pipeline status and latest report summary')
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
