#!/usr/bin/env python3
"""Continuous RFQ e-mail worker for Hermes Mail.

The worker reuses the existing IMAP ingestion and reply-processing pipeline,
but adds a single place to run repeated sync cycles, persist a heartbeat, and
expose a stable status file for the RFQ web UI.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from reporting_utils import count_jsonl

def _resolve_root() -> Path:
    local_root = Path(__file__).resolve().parents[1]
    opt_root = Path("/opt/data/hermes-mail")
    if local_root.exists():
        return local_root
    if opt_root.exists():
        return opt_root
    return local_root


ROOT = _resolve_root()
STATUS_PATH = ROOT / "state" / "rfq-email-worker.json"


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now()))


def load_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {
            "object": "hermes.compras.rfq.worker.status",
            "present": False,
            "state": "idle",
            "status_path": str(STATUS_PATH),
        }
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "object": "hermes.compras.rfq.worker.status",
            "present": True,
            "state": "error",
            "status_path": str(STATUS_PATH),
            "error": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "object": "hermes.compras.rfq.worker.status",
            "present": True,
            "state": "error",
            "status_path": str(STATUS_PATH),
            "error": "status file does not contain a JSON object",
        }
    payload.setdefault("object", "hermes.compras.rfq.worker.status")
    payload.setdefault("present", True)
    payload.setdefault("status_path", str(STATUS_PATH))
    return payload


def save_status(payload: dict[str, Any]) -> dict[str, Any]:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload.setdefault("object", "hermes.compras.rfq.worker.status")
    payload.setdefault("status_path", str(STATUS_PATH))
    payload.setdefault("updated_at", _now_iso())
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _sync_mailbox() -> dict[str, Any]:
    from imap_ingestor import MAILBOX_DEFAULT_FOLDERS, mailbox_by_name, primary_mailbox_name, scan_mailbox, ensure_local_storage, save_mailbox_state

    ensure_local_storage()
    mailbox = mailbox_by_name("buyer") or mailbox_by_name(primary_mailbox_name())
    if not mailbox:
        raise RuntimeError("buyer mailbox not configured")
    scan_result = scan_mailbox(mailbox, persist=True, folders=list(MAILBOX_DEFAULT_FOLDERS))
    save_mailbox_state(last_worker_sync_at=_now(), last_worker_sync_mailbox=str(mailbox.get("name") or "buyer").lower())
    return {
        "mailbox": str(mailbox.get("name") or "buyer").lower(),
        "scan_result": scan_result,
    }


def _drain_pending_replies(*, max_replies: int = 10) -> list[dict[str, Any]]:
    from reply_processor import latest_pending_reply, process_reply

    processed: list[dict[str, Any]] = []
    for _ in range(max_replies):
        reply = latest_pending_reply()
        if not reply:
            break
        processed.append(process_reply(reply))
    return processed


def run_worker_cycle(*, max_replies: int = 10) -> dict[str, Any]:
    started_at = _now()
    status = load_status()
    status.update({
        "state": "running",
        "cycle_started_at": started_at,
        "cycle_started_at_iso": _now_iso(),
    })
    save_status(status)

    try:
        sync_result = _sync_mailbox()
        reply_results = _drain_pending_replies(max_replies=max_replies)
        finished_at = _now()
        next_status = {
            "object": "hermes.compras.rfq.worker.status",
            "present": True,
            "state": "idle",
            "cycle_started_at": started_at,
            "cycle_finished_at": finished_at,
            "cycle_started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "cycle_finished_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_at)),
            "last_sync": sync_result,
            "replies_processed": len(reply_results),
            "reply_results": reply_results,
            "mail_counts": {
                "emails": count_jsonl(ROOT / "emails.jsonl"),
                "incoming_files": sum(1 for item in (ROOT / "emails" / "incoming").glob("*") if item.is_file()),
                "quotes": count_jsonl(ROOT / "cotacoes.jsonl"),
            },
            "last_error": None,
        }
        return save_status(next_status)
    except Exception as exc:
        failed = {
            "object": "hermes.compras.rfq.worker.status",
            "present": True,
            "state": "error",
            "cycle_started_at": started_at,
            "cycle_finished_at": _now(),
            "cycle_started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "cycle_finished_at_iso": _now_iso(),
            "last_error": str(exc),
            "last_sync": status.get("last_sync"),
            "replies_processed": 0,
        }
        save_status(failed)
        raise


def cmd_validate(_: argparse.Namespace) -> int:
    errors: list[str] = []
    if not ROOT.exists():
        errors.append(f"missing root: {ROOT}")
    if not STATUS_PATH.parent.exists():
        errors.append(f"missing state directory: {STATUS_PATH.parent}")
    if errors:
        print("VALIDATION FAILED")
        for err in errors:
            print(err)
        return 1
    print("VALIDATION OK")
    print(f"status_path={STATUS_PATH}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    print(json.dumps(load_status(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_run_once(_: argparse.Namespace) -> int:
    print(json.dumps(run_worker_cycle(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    interval = max(10, int(args.interval))
    max_replies = max(1, int(args.max_replies))
    while True:
        result = run_worker_cycle(max_replies=max_replies)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Mail RFQ e-mail worker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate worker paths and local status location")
    p_validate.set_defaults(func=cmd_validate)

    p_status = sub.add_parser("status", help="Show the current worker heartbeat")
    p_status.set_defaults(func=cmd_status)

    p_once = sub.add_parser("run-once", help="Run a single sync cycle")
    p_once.set_defaults(func=cmd_run_once)

    p_run = sub.add_parser("run", help="Run the worker continuously")
    p_run.add_argument("--interval", type=int, default=120)
    p_run.add_argument("--max-replies", type=int, default=10)
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
