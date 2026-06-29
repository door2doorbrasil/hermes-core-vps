from __future__ import annotations

"""RFQ approval card helpers shared by Telegram and the message tool.

This module keeps the callback payload format, card rendering, and backend
state transition logic in one place so the Telegram adapter and the message
sending tool stay in sync.
"""

from dataclasses import dataclass
import html
import json
import time
from typing import Any, Mapping, Sequence

from hermes_compras_state import ComprasRow, open_compras_db

RFQ_CALLBACK_PREFIX = "rfq"
RFQ_BUTTON_APPROVE = "a"
RFQ_BUTTON_REJECT = "r"
RFQ_BUTTON_ALL = "all"


@dataclass(frozen=True)
class RFQApprovalAction:
    batch_id: int
    decision: str
    selector: str


def parse_rfq_callback_data(data: str) -> RFQApprovalAction | None:
    """Parse ``rfq:<batch_id>:<decision>:<selector>`` callback payloads."""
    parts = (data or "").split(":", 3)
    if len(parts) != 4 or parts[0] != RFQ_CALLBACK_PREFIX:
        return None
    try:
        batch_id = int(parts[1])
    except (TypeError, ValueError):
        return None
    decision = parts[2].strip().lower()
    selector = parts[3].strip().lower()
    if decision not in {RFQ_BUTTON_APPROVE, RFQ_BUTTON_REJECT}:
        return None
    if selector != RFQ_BUTTON_ALL and not selector.isdigit():
        return None
    return RFQApprovalAction(batch_id=batch_id, decision=decision, selector=selector)


def _candidate_identity(candidate: Mapping[str, Any], index: int) -> str:
    label = str(candidate.get("legal_name") or candidate.get("trade_name") or candidate.get("name") or f"Fornecedor {index + 1}").strip()
    country = str(candidate.get("country") or "").strip()
    city = str(candidate.get("city") or "").strip()
    site = str(candidate.get("website") or candidate.get("source_url") or "").strip()
    bits = [label]
    if country or city:
        bits.append(" / ".join(bit for bit in (city, country) if bit))
    if site:
        bits.append(site)
    return " — ".join(bits)


def _candidate_qualification_label(candidate: Mapping[str, Any]) -> str:
    payload = candidate.get("candidate_payload_json")
    payload_data: dict[str, Any] = {}
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload_data = parsed

    raw_score = candidate.get("qualification_score", payload_data.get("qualification_score"))
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = -1.0
    status = str(candidate.get("qualification_status") or payload_data.get("qualification_status") or "").strip()
    rank = candidate.get("qualification_rank", payload_data.get("qualification_rank"))
    parts: list[str] = []
    if score >= 0:
        parts.append(f"score {score:.1f}")
    if status:
        parts.append(status.replace("_", " "))
    if rank not in (None, ""):
        parts.append(f"rank {rank}")
    return " | ".join(parts)


def load_rfq_batch_card_data(batch_id: int, *, db_path: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load batch + candidate data, enriching the batch with product_name."""
    with open_compras_db(db_path=db_path) as db:
        batch = db.get_rfq_batch(batch_id)
        if batch is None:
            raise ValueError(f"RFQ batch not found: {batch_id}")
        candidate_rows = db.list_rfq_candidates(batch_id)
        batch_data = dict(batch.data)
        product_id = batch_data.get("product_id")
        if product_id is not None:
            product_row = db.fetchone("SELECT name FROM products WHERE id = ?", (product_id,))
            if product_row is not None and product_row.data.get("name"):
                batch_data["product_name"] = product_row.data["name"]
        candidates = [row.data for row in candidate_rows]
    return batch_data, candidates



def render_rfq_approval_card(
    batch: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the text + inline keyboard rows for an RFQ approval card."""
    batch_id = int(batch.get("id") or batch.get("rfq_batch_id") or 0)
    batch_code = str(batch.get("batch_code") or batch_id or "RFQ").strip()
    product_name = str(batch.get("product_name") or batch.get("product") or "Produto sem nome").strip()
    requested_by = str(batch.get("requested_by") or batch.get("requested_by_user") or "-").strip()
    status = str(batch.get("status") or "awaiting_user_approval").strip()
    created_at = batch.get("created_at")
    created_at_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(created_at))) if created_at not in (None, "") else "-"

    lines = [
        "📬 <b>Aprovação RFQ pendente</b>",
        f"<b>Lote:</b> {html.escape(batch_code)}",
        f"<b>Produto:</b> {html.escape(product_name)}",
        f"<b>Solicitado por:</b> {html.escape(requested_by)}",
        f"<b>Status:</b> {html.escape(status)}",
        f"<b>Criado em:</b> {html.escape(created_at_text)}",
        "",
        f"<b>Fornecedores:</b> {len(candidates)}",
    ]

    for idx, candidate in enumerate(candidates):
        name = html.escape(_candidate_identity(candidate, idx))
        qual = _candidate_qualification_label(candidate)
        if qual:
            lines.append(f"{idx + 1}. {name} [{html.escape(qual)}]")
        else:
            lines.append(f"{idx + 1}. {name}")

    lines.append("")
    lines.append("Escolha aprovar ou rejeitar por fornecedor, ou use as ações em lote abaixo.")

    rows: list[list[dict[str, str]]] = []
    for idx, _candidate in enumerate(candidates):
        rows.append([
            {"text": f"✅ Aprovar {idx + 1}", "callback_data": f"rfq:{batch_id}:a:{idx}"},
            {"text": f"❌ Rejeitar {idx + 1}", "callback_data": f"rfq:{batch_id}:r:{idx}"},
        ])
    rows.append([
        {"text": "✅ Aprovar todos", "callback_data": f"rfq:{batch_id}:a:all"},
        {"text": "❌ Rejeitar todos", "callback_data": f"rfq:{batch_id}:r:all"},
    ])

    return {
        "batch_id": batch_id,
        "batch_code": batch_code,
        "text": "\n".join(lines),
        "buttons": rows,
    }


def resolve_rfq_approval_action(
    action: RFQApprovalAction,
    *,
    approved_by: str | None,
    approval_notes: str | None = None,
    authorize_email_send: bool = False,
    dry_run: bool = True,
    created_at: float | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Apply an RFQ approval/rejection action against ComprasDB."""
    now = created_at if created_at is not None else time.time()
    with open_compras_db(db_path=db_path) as db:
        batch = db.get_rfq_batch(action.batch_id)
        if batch is None:
            raise ValueError(f"RFQ batch not found: {action.batch_id}")
        candidates = db.list_rfq_candidates(action.batch_id)
        if not candidates:
            raise ValueError(f"RFQ batch has no candidates: {action.batch_id}")

        if action.selector == RFQ_BUTTON_ALL:
            selected = [row.data for row in candidates]
        else:
            idx = int(action.selector)
            if idx < 0 or idx >= len(candidates):
                raise ValueError(f"RFQ candidate index out of range: {idx}")
            selected = [candidates[idx].data]

        if action.decision == RFQ_BUTTON_APPROVE:
            result = db.approve_rfq_suppliers(
                rfq_batch_id=action.batch_id,
                approved_supplier_candidates=selected,
                rejected_supplier_candidates=[],
                approved_by=approved_by,
                approval_notes=approval_notes,
                authorize_email_send=authorize_email_send,
                dry_run=dry_run,
                created_at=now,
            )
        else:
            rejected_ids = db.reject_rfq_suppliers(
                rfq_batch_id=action.batch_id,
                rejected_supplier_candidates=selected,
                rejected_by=approved_by,
                rejection_notes=approval_notes,
                created_at=now,
                updated_at=now,
            )
            db.execute(
                "UPDATE rfq_batches SET status = ?, user_authorized = ?, authorized_by = ?, authorized_at = ?, authorization_source = ? WHERE id = ?",
                (
                    "rejected",
                    0,
                    approved_by,
                    now,
                    "telegram_callback",
                    action.batch_id,
                ),
            )
            result = {
                "rfq_batch_id": action.batch_id,
                "approved_count": 0,
                "rejected_count": len(rejected_ids),
                "email_authorized": False,
                "dry_run": bool(dry_run),
                "next_action": "no_email_send",
                "audit_log_ids": [],
                "decision_log_ids": [],
                "email_log_ids": [],
                "created_supplier_ids": [],
                "recipient_ids": [],
            }

    result.update(
        {
            "batch_id": action.batch_id,
            "decision": action.decision,
            "selector": action.selector,
            "batch_code": str(batch.data.get("batch_code") if batch else action.batch_id),
        }
    )
    return result


def render_rfq_result_message(result: Mapping[str, Any]) -> str:
    """Render a concise outcome message for Telegram edits."""
    batch_code = html.escape(str(result.get("batch_code") or result.get("rfq_batch_id") or "RFQ"))
    approved_count = int(result.get("approved_count") or 0)
    rejected_count = int(result.get("rejected_count") or 0)
    decision = str(result.get("decision") or "").strip().lower()
    selector = str(result.get("selector") or "").strip()
    email_authorized = bool(result.get("email_authorized"))
    dry_run = bool(result.get("dry_run"))

    decision_label = "Aprovado" if decision == RFQ_BUTTON_APPROVE else "Rejeitado"
    target_label = "todos" if selector == RFQ_BUTTON_ALL else f"fornecedor {selector}"

    lines = [
        f"✅ <b>RFQ {html.escape(decision_label)}</b>",
        f"<b>Lote:</b> {batch_code}",
        f"<b>Alvo:</b> {html.escape(target_label)}",
        f"<b>Aprovados:</b> {approved_count}",
        f"<b>Rejeitados:</b> {rejected_count}",
        f"<b>E-mail autorizado:</b> {'sim' if email_authorized else 'não'}",
        f"<b>Dry-run:</b> {'sim' if dry_run else 'não'}",
    ]
    return "\n".join(lines)


def button_rows_to_keyboard(rows: Sequence[Sequence[Mapping[str, str]]]):
    """Convert transport-agnostic button rows into Telegram markup rows."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception as exc:  # pragma: no cover - import availability depends on runtime
        raise RuntimeError(f"Telegram inline keyboard support unavailable: {exc}") from exc

    keyboard = []
    for row in rows:
        keyboard.append([
            InlineKeyboardButton(button["text"], callback_data=button["callback_data"])
            for button in row
        ])
    return InlineKeyboardMarkup(keyboard)
