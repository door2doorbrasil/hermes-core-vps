from __future__ import annotations

from pathlib import Path

from hermes_compras_state import ComprasDB
from tools.compras_rfq_approval import (
    RFQ_BUTTON_ALL,
    RFQ_BUTTON_APPROVE,
    RFQ_BUTTON_REJECT,
    parse_rfq_callback_data,
    render_rfq_approval_card,
    render_rfq_result_message,
    resolve_rfq_approval_action,
)


def _seed_rfq_batch(db: ComprasDB) -> int:
    product_id = db.insert_product(name="AMR Robot", created_at=1.0, updated_at=1.0)
    batch_id = db.insert_rfq_batch(
        batch_code="RFQ-TELEGRAM-001",
        product_id=product_id,
        requested_by="tester",
        created_at=1.0,
        updated_at=1.0,
    )
    db.store_rfq_candidates(
        batch_id,
        [
            {
                "legal_name": "Alpha Robotics",
                "country": "CN",
                "city": "Shenzhen",
                "website": "https://alpha.example",
                "source_url": "https://alpha.example",
            },
            {
                "legal_name": "Beta Trading",
                "country": "US",
                "city": "Miami",
                "website": "https://beta.example",
                "source_url": "https://beta.example",
            },
        ],
        created_at=1.0,
        updated_at=1.0,
    )
    return batch_id


def test_rfq_approval_card_payload_and_callback_parser(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        batch_id = _seed_rfq_batch(db)
        batch = db.get_rfq_batch(batch_id)
        candidates = [row.data for row in db.list_rfq_candidates(batch_id)]

    card = render_rfq_approval_card(batch.data, candidates)
    assert card["batch_id"] == batch_id
    assert card["buttons"][0][0]["callback_data"] == f"rfq:{batch_id}:a:0"
    assert card["buttons"][0][1]["callback_data"] == f"rfq:{batch_id}:r:0"
    assert card["buttons"][-1][0]["callback_data"] == f"rfq:{batch_id}:a:{RFQ_BUTTON_ALL}"
    assert card["buttons"][-1][1]["callback_data"] == f"rfq:{batch_id}:r:{RFQ_BUTTON_ALL}"
    assert "score" in card["text"].lower()
    assert parse_rfq_callback_data(f"rfq:{batch_id}:a:all").decision == RFQ_BUTTON_APPROVE
    assert parse_rfq_callback_data(f"rfq:{batch_id}:r:1").decision == RFQ_BUTTON_REJECT
    assert parse_rfq_callback_data(f"rfq:{batch_id}:r:1").selector == "1"
    assert parse_rfq_callback_data("bogus") is None


def test_resolve_rfq_approval_action_updates_backend_state(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes_compras.db"
    with ComprasDB(db_path=db_path) as db:
        batch_id = _seed_rfq_batch(db)

    result = resolve_rfq_approval_action(
        parse_rfq_callback_data(f"rfq:{batch_id}:a:0"),
        approved_by="telegram-user",
        approval_notes="approved from Telegram",
        authorize_email_send=False,
        dry_run=True,
        created_at=1.0,
        db_path=db_path,
    )
    assert result["approved_count"] == 1
    assert result["rejected_count"] == 0
    assert result["decision"] == RFQ_BUTTON_APPROVE
    assert result["selector"] == "0"
    assert "RFQ" in render_rfq_result_message(result)

    with ComprasDB(db_path=db_path) as db:
        assert db.fetchone("SELECT status FROM rfq_batches WHERE id = ?", (batch_id,)).data["status"] in {"approved_without_email", "authorized"}
        assert db.fetchone("SELECT COUNT(*) AS c FROM user_decision_logs WHERE rfq_batch_id = ?", (batch_id,)).data["c"] >= 1
        assert db.fetchone("SELECT COUNT(*) AS c FROM audit_logs WHERE entity_type IN ('rfq_candidate', 'supplier')", ()).data["c"] >= 1
        assert db.list_rfq_candidates(batch_id)
