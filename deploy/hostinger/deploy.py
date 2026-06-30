#!/usr/bin/env python3
"""Recreate a Hostinger VPS Docker project from a GitHub repo."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE = os.getenv("HOSTINGER_API_BASE_URL", "https://developers.hostinger.com").rstrip("/")
VPS_ID = os.getenv("HOSTINGER_VPS_ID")
TOKEN = os.getenv("HOSTINGER_API_TOKEN")
PROJECT_NAME = os.getenv("HOSTINGER_PROJECT_NAME", "hermes-core")
STOP_PROJECTS = [
    item.strip()
    for item in os.getenv("HOSTINGER_STOP_PROJECTS", "").split(",")
    if item.strip()
]
SMOKE_URL = os.getenv("HOSTINGER_SMOKE_URL")
SMOKE_EXPECT = os.getenv("HOSTINGER_SMOKE_EXPECT", "Hermes")
SMOKE_TIMEOUT = int(os.getenv("HOSTINGER_SMOKE_TIMEOUT", "180"))
POLL_SECONDS = int(os.getenv("HOSTINGER_POLL_SECONDS", "10"))
POLL_ATTEMPTS = int(os.getenv("HOSTINGER_POLL_ATTEMPTS", "120"))
CA_BUNDLE = os.getenv("HOSTINGER_CA_BUNDLE")
INSECURE_SSL = os.getenv("HOSTINGER_INSECURE_SSL", "").lower() in {
    "1",
    "true",
    "yes",
}
DEPLOY_ENV_KEYS = [
    "HERMES_IMAGE",
    "ACME_EMAIL",
    "OPENAI_API_KEY",
    "API_SERVER_KEY",
    "LITELLM_MASTER_KEY",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "HERMES_DASHBOARD_PUBLIC_URL",
    "HERMES_DASHBOARD_HOSTNAME",
    "OPEN_WEBUI_HOSTNAME",
    "OPEN_WEBUI_PUBLIC_URL",
    # WhatsApp bridge access control for the business number.
    "WHATSAPP_ALLOWED_USERS",
    "BUY_IMAP_HOST",
    "BUY_IMAP_PORT",
    "BUY_IMAP_SSL",
    "BUY_IMAP_USERNAME",
    "BUY_IMAP_PASSWORD",
    "BUY_SMTP_HOST",
    "BUY_SMTP_PORT",
    "BUY_SMTP_SSL",
    "BUY_SMTP_USERNAME",
    "BUY_SMTP_PASSWORD",
    "SALES_IMAP_HOST",
    "SALES_IMAP_PORT",
    "SALES_IMAP_SSL",
    "SALES_IMAP_USERNAME",
    "SALES_IMAP_PASSWORD",
    "SALES_SMTP_HOST",
    "SALES_SMTP_PORT",
    "SALES_SMTP_SSL",
    "SALES_SMTP_USERNAME",
    "SALES_SMTP_PASSWORD",
]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def ssl_context() -> ssl.SSLContext:
    if INSECURE_SSL:
        return ssl._create_unverified_context()
    if CA_BUNDLE:
        return ssl.create_default_context(cafile=CA_BUNDLE)
    return ssl.create_default_context()


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, */*",
        "User-Agent": "codex-hostinger-deploy/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120, context=ssl_context()) as response:
            body = response.read().decode("utf-8").strip()
            return json.loads(body) if body else None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"{method} {path} failed: HTTP {exc.code} {exc.reason}\n{body}")
    except URLError as exc:
        fail(f"{method} {path} failed: {exc.reason}")


def get_projects() -> list[dict[str, Any]]:
    data = request("GET", f"/api/vps/v1/virtual-machines/{VPS_ID}/docker")
    if not isinstance(data, list):
        fail(f"Unexpected project list response: {data!r}")
    return data


def delete_project(name: str) -> None:
    request("DELETE", f"/api/vps/v1/virtual-machines/{VPS_ID}/docker/{quote(name)}/down")


def build_environment_blob() -> str:
    lines: list[str] = []
    for key in DEPLOY_ENV_KEYS:
        value = os.getenv(key)
        if value:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def deploy_project() -> None:
    compose_path = os.path.join(os.getcwd(), "docker-compose.yml")
    try:
        content = open(compose_path, "r", encoding="utf-8").read()
    except OSError as exc:
        fail(f"Unable to read docker-compose.yml: {exc}")

    payload: dict[str, Any] = {
        "project_name": PROJECT_NAME,
        "content": content,
    }
    environment = build_environment_blob()
    if environment:
        payload["environment"] = environment
    request("POST", f"/api/vps/v1/virtual-machines/{VPS_ID}/docker", payload)


def project_containers() -> list[dict[str, Any]]:
    data = request(
        "GET",
        f"/api/vps/v1/virtual-machines/{VPS_ID}/docker/{quote(PROJECT_NAME)}/containers",
    )
    if not isinstance(data, list):
        fail(f"Unexpected container response: {data!r}")
    return data


def summarize_container_states(containers: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for container in containers:
        state = str(container.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return ", ".join(f"{state}({count})" for state, count in sorted(counts.items()))


def wait_for_project() -> dict[str, Any]:
    for _ in range(POLL_ATTEMPTS):
        for project in get_projects():
            if project.get("name") == PROJECT_NAME:
                return project
        time.sleep(POLL_SECONDS)
    fail(f"Project {PROJECT_NAME!r} not found after deployment")


def smoke_test() -> None:
    if not SMOKE_URL:
        return
    deadline = time.time() + SMOKE_TIMEOUT
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(SMOKE_URL, timeout=15, context=ssl_context()) as response:
                body = response.read().decode("utf-8", errors="replace")
                if SMOKE_EXPECT in body:
                    print(f"Smoke test passed: {SMOKE_URL}")
                    return
                raise RuntimeError(
                    f"Expected marker {SMOKE_EXPECT!r} not found in smoke response"
                )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(5)
    fail(f"Smoke test failed for {SMOKE_URL}: {last_error}")


def main() -> None:
    if not VPS_ID:
        fail("HOSTINGER_VPS_ID is required")
    if not TOKEN:
        fail("HOSTINGER_API_TOKEN is required")
    print(f"Using Hostinger API at {API_BASE}")
    print(f"Target VPS: {VPS_ID}")
    print(f"Project: {PROJECT_NAME}")

    existing = {project.get('name') for project in get_projects() if project.get('name')}
    for name in STOP_PROJECTS:
        if name in existing:
            print(f"Stopping existing project: {name}")
            request("POST", f"/api/vps/v1/virtual-machines/{VPS_ID}/docker/{quote(name)}/stop")

    print(f"Deploying {PROJECT_NAME} from raw docker-compose content")
    deploy_project()

    for attempt in range(1, POLL_ATTEMPTS + 1):
        details = wait_for_project()
        containers = project_containers()
        state = details.get("state")
        status = details.get("status")
        print(f"Poll {attempt}/{POLL_ATTEMPTS}: state={state} status={status}")
        if containers:
            print(f"Containers: {summarize_container_states(containers)}")
        if state in {"running", "mixed"} and containers:
            healthy = any(c.get("state") == "running" for c in containers)
            if healthy:
                break
        time.sleep(POLL_SECONDS)
    else:
        fail("Project did not reach a healthy state in time")

    print(json.dumps(wait_for_project(), indent=2, ensure_ascii=False))
    print(json.dumps(project_containers(), indent=2, ensure_ascii=False))
    smoke_test()


if __name__ == "__main__":
    main()
