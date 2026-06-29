#!/usr/bin/env python3
"""ERP Django API client for Hermes Mail.

This client talks to an external ERP through HTTP only. If the ERP endpoint is
not configured, every operation falls back to a clear dry_run response so the
Hermes workflow stays safe and operational.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reporting_utils import get_env_secret, normalize_text, utc_now

ROOT = Path('/opt/data/hermes-mail')
DEFAULT_TIMEOUT = 30


@dataclass(slots=True)
class ERPClient:
    base_url: str | None
    token: str | None
    timeout: int = DEFAULT_TIMEOUT

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    @property
    def dry_run(self) -> bool:
        return not self.configured

    def _dry_run(self, operation: str, *, payload: Any | None = None, path: str = '') -> dict[str, Any]:
        return {
            'ok': True,
            'dry_run': True,
            'configured': False,
            'message': 'ERP não configurado. Operação simulada em dry_run.',
            'operation': operation,
            'path': path,
            'payload': payload if payload is not None else {},
            'timestamp': utc_now(),
        }

    def _request(self, method: str, path: str, *, payload: Any | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            return self._dry_run(method.lower(), payload=payload, path=path)
        assert self.base_url and self.token
        url = self.base_url.rstrip('/') + '/' + path.lstrip('/')
        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        data = None
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'HermesMail/ERPClient',
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode('utf-8', errors='replace')
                content_type = response.headers.get('Content-Type', '')
                parsed: Any = body
                if 'application/json' in content_type:
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        parsed = body
                return {
                    'ok': True,
                    'dry_run': False,
                    'configured': True,
                    'method': method.upper(),
                    'url': url,
                    'status_code': getattr(response, 'status', 200),
                    'response': parsed,
                    'timestamp': utc_now(),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace') if exc.fp else ''
            return {
                'ok': False,
                'dry_run': False,
                'configured': True,
                'method': method.upper(),
                'url': url,
                'status_code': exc.code,
                'error': body or str(exc),
                'timestamp': utc_now(),
            }
        except Exception as exc:
            return {
                'ok': False,
                'dry_run': False,
                'configured': True,
                'method': method.upper(),
                'url': url,
                'error': str(exc),
                'timestamp': utc_now(),
            }

    def health_check(self) -> dict[str, Any]:
        if not self.configured:
            return self._dry_run('health_check', path='/health')
        result = self._request('GET', '/health')
        if result.get('ok'):
            return result
        alt = self._request('GET', '/api/health')
        if alt.get('ok'):
            alt['health_endpoint'] = '/api/health'
            return alt
        result['fallback_attempt'] = alt
        return result

    def search_company(self, company_name: str) -> dict[str, Any]:
        return self._request('GET', '/companies', params={'q': company_name})

    def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('POST', '/companies', payload=payload)

    def update_company(self, company_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('PATCH', f'/companies/{company_id}', payload=payload)

    def create_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('POST', '/contacts', payload=payload)

    def create_opportunity(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('POST', '/opportunities', payload=payload)

    def create_supplier(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('POST', '/suppliers', payload=payload)

    def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request('POST', '/products', payload=payload)

    def attach_document(self, entity_type: str, entity_id: str, document_path: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            'entity_type': entity_type,
            'entity_id': entity_id,
            'document_path': document_path,
            'metadata': metadata or {},
        }
        return self._request('POST', '/documents', payload=payload)


def _env_or_none(*names: str) -> str | None:
    value = get_env_secret(*names)
    if value:
        return value
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def load_client() -> ERPClient:
    base_url = _env_or_none('ERP_API_BASE_URL')
    token = _env_or_none('ERP_API_TOKEN')
    timeout_raw = _env_or_none('ERP_TIMEOUT')
    timeout = DEFAULT_TIMEOUT
    if timeout_raw:
        try:
            timeout = int(timeout_raw)
        except Exception:
            timeout = DEFAULT_TIMEOUT
    return ERPClient(base_url=base_url, token=token, timeout=timeout)


def cmd_validate(_: argparse.Namespace) -> int:
    client = load_client()
    print(json.dumps({
        'ok': True,
        'configured': client.configured,
        'dry_run': client.dry_run,
        'base_url': client.base_url or '',
        'base_url_present': bool(client.base_url),
        'token_present': bool(client.token),
        'timeout': client.timeout,
        'env_vars': {
            'ERP_API_BASE_URL': bool(_env_or_none('ERP_API_BASE_URL')),
            'ERP_API_TOKEN': bool(_env_or_none('ERP_API_TOKEN')),
            'ERP_TIMEOUT': bool(_env_or_none('ERP_TIMEOUT')),
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_health_check(_: argparse.Namespace) -> int:
    client = load_client()
    result = client.health_check()
    if client.dry_run and result.get('dry_run'):
        result['message'] = 'ERP não configurado. Operação simulada em dry_run.'
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get('ok') else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Hermes ERP API client')
    sub = parser.add_subparsers(dest='command', required=True)

    p_validate = sub.add_parser('validate', help='Validate ERP API client configuration')
    p_validate.set_defaults(func=cmd_validate)

    p_health = sub.add_parser('health-check', help='Check ERP API health or dry_run status')
    p_health.set_defaults(func=cmd_health_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
