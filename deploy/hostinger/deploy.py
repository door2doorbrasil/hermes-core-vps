#!/usr/bin/env python3
"""Recreate a Hostinger VPS Docker project from a GitHub repo."""

from __future__ import annotations

import json
import os
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
REPO_URL = os.getenv("HOSTINGER_REPO_URL")
DELETE_PROJECTS = [
    item.strip()
    for item in os.getenv("HOSTINGER_DELETE_PROJECTS", PROJECT_NAME).split(",")
    if item.strip()
]
SMOKE_URL = os.getenv("HOSTINGER_SMOKE_URL")
SMOKE_EXPECT = os.getenv("HOSTINGER_SMOKE_EXPECT", "Hermes")
SMOKE_TIMEOUT = int(os.getenv("HOSTINGER_SMOKE_TIMEOUT", "180"))
POLL_SECONDS = int(os.getenv("HOSTINGER_POLL_SECONDS", "10"))
POLL_ATTEMPTS = int(os.getenv("HOSTINGER_POLL_ATTEMPTS", "120"))
DEPLOY_ENV_KEYS = [
    "ACME_EMAIL",
    "OPENAI_API_KEY",
    "API_SERVER_KEY",
    "LITELLM_MASTER_KEY",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "HERMES_DASHBOARD_PUBLIC_URL",
    "HERMES_DASHBOARD_HOSTNAME",
    "HERMES_API_HOSTNAME",
    "OPEN_WEBUI_HOSTNAME",
]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


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
        with urlopen(req, timeout=120) as response:
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
    payload: dict[str, Any] = {
        "project_name": PROJECT_NAME,
        "content": REPO_URL,
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
            with urlopen(SMOKE_URL, timeout=15) as response:
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
    if not REPO_URL:
        fail("HOSTINGER_REPO_URL is required")

    print(f"Using Hostinger API at {API_BASE}")
    print(f"Target VPS: {VPS_ID}")
    print(f"Project: {PROJECT_NAME}")

    existing = {project.get('name') for project in get_projects() if project.get('name')}
    for name in DELETE_PROJECTS:
        if name in existing:
            print(f"Removing existing project: {name}")
            delete_project(name)

    print(f"Deploying {PROJECT_NAME} from {REPO_URL}")
    deploy_project()

    for attempt in range(1, POLL_ATTEMPTS + 1):
        details = wait_for_project()
        containers = project_containers()
        state = details.get("state")
        status = details.get("status")
        print(f"Poll {attempt}/{POLL_ATTEMPTS}: state={state} status={status}")
        if state == "running" and containers:
            healthy = all(c.get("state") == "running" for c in containers)
            if healthy:
                break
        time.sleep(POLL_SECONDS)
    else:
        fail("Project did not reach a running state in time")

    print(json.dumps(wait_for_project(), indent=2, ensure_ascii=False))
    print(json.dumps(project_containers(), indent=2, ensure_ascii=False))
    smoke_test()


if __name__ == "__main__":
    main()
