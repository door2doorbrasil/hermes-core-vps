from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.session import SessionSource


class DummyPairingStore:
    def is_approved(self, platform_name, user_id):
        return False


class DummyRunner(GatewayAuthorizationMixin):
    pass


def _make_runner(dm_policy: str = "open") -> DummyRunner:
    runner = DummyRunner()
    runner.pairing_store = DummyPairingStore()
    runner.adapters = {
        Platform.WHATSAPP: SimpleNamespace(
            enforces_own_access_policy=True,
            _dm_policy=dm_policy,
            _group_policy="open",
            _groups={},
        )
    }
    runner.config = SimpleNamespace(
        platforms={},
        unauthorized_dm_behavior="pair",
    )
    return runner


def _make_whatsapp_source() -> SessionSource:
    return SessionSource(
        platform=Platform.WHATSAPP,
        chat_id="5511999999999@s.whatsapp.net",
        chat_type="dm",
        user_id="5511999999999@s.whatsapp.net",
        user_name="5511999999999",
    )


def _stub_gateway_run(monkeypatch):
    module = ModuleType("gateway.run")
    module.logger = SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "gateway.run", module)


def test_whatsapp_open_dm_allows_any_sender(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("WHATSAPP_ALLOW_ALL_USERS", raising=False)
    _stub_gateway_run(monkeypatch)

    runner = _make_runner(dm_policy="open")
    assert runner._is_user_authorized(_make_whatsapp_source()) is True


def test_whatsapp_disabled_dm_still_blocks_sender(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("WHATSAPP_ALLOW_ALL_USERS", raising=False)
    _stub_gateway_run(monkeypatch)

    runner = _make_runner(dm_policy="disabled")
    assert runner._is_user_authorized(_make_whatsapp_source()) is False
