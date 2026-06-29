from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin


class DummyWhatsAppBehavior(WhatsAppBehaviorMixin):
    def __init__(self):
        self.config = SimpleNamespace(extra={})
        self._reply_prefix = None
        self.name = "whatsapp"


def test_bridge_is_the_default_mode_for_reply_prefix(monkeypatch):
    monkeypatch.delenv("WHATSAPP_MODE", raising=False)
    dummy = DummyWhatsAppBehavior()
    assert dummy._effective_reply_prefix() == ""
