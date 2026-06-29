from __future__ import annotations

import sys
from pathlib import Path


def _import_worker():
    scripts_dir = Path("/Users/aluizioandreatta/Documents/Polar Sinergy LLC/local-hermes/data/hermes-mail/scripts")
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import rfq_email_worker as worker  # type: ignore

    return worker


def test_rfq_worker_cycle_writes_status(tmp_path: Path, monkeypatch) -> None:
    worker = _import_worker()
    root = tmp_path / "hermes-mail"
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(worker, "STATUS_PATH", root / "state" / "rfq-email-worker.json")
    monkeypatch.setattr(worker, "_sync_mailbox", lambda: {"mailbox": "buyer", "scan_result": {"fetched_count": 1}})
    monkeypatch.setattr(worker, "_drain_pending_replies", lambda max_replies=10: [{"linked": True, "analysis_written": True}])

    result = worker.run_worker_cycle(max_replies=3)

    assert result["state"] == "idle"
    assert result["replies_processed"] == 1
    assert worker.STATUS_PATH.exists()
    status = worker.load_status()
    assert status["present"] is True
    assert status["state"] == "idle"
    assert status["last_sync"]["mailbox"] == "buyer"
    assert status["replies_processed"] == 1


def test_rfq_worker_validate_reports_status_path() -> None:
    worker = _import_worker()
    assert worker.cmd_validate.__name__ == "cmd_validate"
