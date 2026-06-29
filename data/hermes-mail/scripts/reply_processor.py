#!/usr/bin/env python3
"""Controlled reply processor for Hermes Mail.

This module bridges incoming supplier replies into the analysis pipeline while
keeping uncertain links in a manual-review queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from email_real_common import EMAILS_JSONL, ROOT, append_record, ensure_storage, latest_record, load_jsonl_safe, now, notify_telegram
from reporting_utils import count_jsonl
from supplier_reply_analyzer import analyze_reply_email, write_analysis

MANUAL_REVIEW_QUEUE_JSONL = ROOT / "manual-review-queue.jsonl"


def validate_jsonl(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        errors.append(f"missing file: {path}")
        return errors
    try:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                json.loads(line)
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSONL in {path}: {exc}")
    except OSError as exc:
        errors.append(f"read error {path}: {exc}")
    return errors


def latest_pending_reply() -> dict[str, Any] | None:
    replies = [
        rec
        for rec in load_jsonl_safe(EMAILS_JSONL)
        if rec.get("direction") == "incoming"
        and rec.get("source") in {"imap_ingestor", "supplier_reply_simulator", "supplier_reply_received"}
        and rec.get("id")
    ]
    if not replies:
        return None
    replies = [rec for rec in replies if not latest_record(ROOT / "supplier-reply-analysis.jsonl", lambda a: a.get("supplier_reply_email_id") == rec.get("id"))]
    return replies[-1] if replies else None


def manual_review_item(reply_email: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    rfq_linked = bool(analysis.get("rfq_email_id")) and bool(analysis.get("rfq_quote_id"))
    reason_bits = []
    if not rfq_linked:
        reason_bits.append("unlinked_reply")
    if analysis.get("manual_review_reason"):
        reason_bits.append(str(analysis["manual_review_reason"]))
    if not analysis.get("translated_reply_ptbr"):
        reason_bits.append("missing_translation")
    reason = ", ".join(bit for bit in reason_bits if bit)
    digest_source = f"{reply_email.get('id')}|{analysis.get('id')}"
    return {
        "id": f"manual_review_{hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:12]}",
        "created_at": now(),
        "updated_at": now(),
        "status": "pending",
        "type": "supplier_reply_unlinked" if not rfq_linked else "supplier_reply_review",
        "reply_email_id": reply_email.get("id"),
        "reply_message_id": reply_email.get("message_id"),
        "thread_id": reply_email.get("thread_id"),
        "received_at": reply_email.get("received_at") or reply_email.get("created_at") or now(),
        "rfq_email_id": analysis.get("rfq_email_id"),
        "rfq_quote_id": analysis.get("rfq_quote_id"),
        "supplier_id": analysis.get("supplier_id") or reply_email.get("supplier_id"),
        "contact_id": analysis.get("contact_id") or reply_email.get("contact_id"),
        "product_name": analysis.get("product_name"),
        "reason": reason or "reply could not be safely linked to the original RFQ",
        "analysis_id": analysis.get("id"),
        "source": "reply_processor",
    }


def process_reply(reply_email: dict[str, Any]) -> dict[str, Any]:
    analysis = analyze_reply_email(reply_email)
    linked = bool(analysis.get("rfq_email_id")) and bool(analysis.get("rfq_quote_id"))
    needs_review = bool(analysis.get("manual_review")) or not linked
    result: dict[str, Any] = {
        "reply_email_id": reply_email.get("id"),
        "analysis_id": analysis.get("id"),
        "linked": linked,
        "needs_review": needs_review,
        "manual_review_reason": analysis.get("manual_review_reason"),
    }
    if needs_review:
        item = manual_review_item(reply_email, analysis)
        append_record(MANUAL_REVIEW_QUEUE_JSONL, item)
        notify_telegram(
            "supplier_reply_manual_review",
            f"Resposta de fornecedor enviada para revisão manual: {reply_email.get('id')}",
            metadata=item,
        )
        result["manual_review_item_id"] = item["id"]
        return result
    write_analysis(analysis)
    notify_telegram(
        "supplier_reply_received",
        f"Resposta de fornecedor processada e vinculada ao RFQ: {reply_email.get('id')}",
        metadata={
            "reply_email_id": reply_email.get("id"),
            "analysis_id": analysis.get("id"),
            "rfq_email_id": analysis.get("rfq_email_id"),
            "rfq_quote_id": analysis.get("rfq_quote_id"),
        },
    )
    result["analysis_written"] = True
    return result


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_storage()
    errors = validate_jsonl(MANUAL_REVIEW_QUEUE_JSONL)
    if errors:
        print("VALIDATION FAILED")
        for err in errors:
            print(err)
        return 1
    print("VALIDATION OK")
    print(f"manual_review_queue={MANUAL_REVIEW_QUEUE_JSONL}")
    return 0


def cmd_process_latest(args: argparse.Namespace) -> int:
    ensure_storage()
    reply = latest_pending_reply()
    if not reply:
        print(json.dumps({"ok": True, "processed": [], "message": "no pending replies"}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    processed = process_reply(reply)
    print(json.dumps({"ok": True, "reply": reply, "processed": processed}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    ensure_storage()
    replies = [
        rec
        for rec in load_jsonl_safe(EMAILS_JSONL)
        if rec.get("direction") == "incoming"
        and rec.get("source") in {"imap_ingestor", "supplier_reply_simulator", "supplier_reply_received"}
    ]
    manual = count_jsonl(MANUAL_REVIEW_QUEUE_JSONL)
    analyses = count_jsonl(ROOT / "supplier-reply-analysis.jsonl")
    print(json.dumps({
        "ok": True,
        "incoming_replies": len(replies),
        "analysis_records": analyses,
        "manual_review_items": manual,
        "queue_path": str(MANUAL_REVIEW_QUEUE_JSONL),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Mail reply processor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate the manual review queue store")
    p_validate.set_defaults(func=cmd_validate)

    p_process = sub.add_parser("process-latest", help="Process the latest pending reply")
    p_process.add_argument("--limit", type=int, default=1)
    p_process.set_defaults(func=cmd_process_latest)

    p_stats = sub.add_parser("stats", help="Show reply processor stats")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
