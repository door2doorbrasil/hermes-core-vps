from pathlib import Path
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aivo_route_detector import (
    AivoRouteDetector,
    build_aivo_remarketing_message,
    select_aivo_remarketing_sessions,
    should_route_to_aivo_openai,
)


class _FakeAivoSessionStore:
    def __init__(self):
        self.data = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value):
        self.data[key] = value

    def clear(self, key: str):
        self.data.pop(key, None)


def _make_detector(now: float = 1_000.0):
    store = _FakeAivoSessionStore()
    clock = [now]
    detector = AivoRouteDetector(session_store=store, now_fn=lambda: clock[0])
    return detector, store, clock


def _make_whatsapp_source(sender: str = "5511999999999"):
    return SimpleNamespace(
        platform=SimpleNamespace(value="whatsapp"),
        user_id=sender,
        chat_id=f"{sender}@s.whatsapp.net",
        chat_type="dm",
    )


def test_aivo_route_detector_accepts_whatsapp_aivo_message():
    detector = AivoRouteDetector()
    event = SimpleNamespace(text='Quero saber mais sobre o AIVO Note')
    source = SimpleNamespace(platform=SimpleNamespace(value='whatsapp'))

    assert detector.should_route_to_openai(event, {'source': source}) is True
    assert should_route_to_aivo_openai(event, {'source': source}) is True


def test_aivo_route_detector_rejects_non_whatsapp_route():
    detector = AivoRouteDetector()
    event = SimpleNamespace(text='Quero saber mais sobre o AIVO Note')
    source = SimpleNamespace(platform=SimpleNamespace(value='telegram'))

    assert detector.should_route_to_openai(event, {'source': source}) is False


def test_aivo_route_detector_starts_sales_session_for_whatsapp_aivo_message():
    detector, store, _clock = _make_detector()
    source = _make_whatsapp_source()

    first = detector.decision(SimpleNamespace(text='quero comprar Aivo Note'), {'source': source})
    assert first.should_route is True
    assert first.reason == 'aivo_session_started'
    assert first.mode == 'sales'
    assert store.data


def test_aivo_route_detector_continues_sales_session_for_followups():
    detector, store, _clock = _make_detector()
    source = _make_whatsapp_source()

    detector.decision(SimpleNamespace(text='quero comprar Aivo Note'), {'source': source})

    second = detector.decision(SimpleNamespace(text='profissional'), {'source': source})
    assert second.should_route is True
    assert second.reason == 'aivo_session_continued'
    assert second.mode == 'sales'

    third = detector.decision(SimpleNamespace(text='sim'), {'source': source})
    assert third.should_route is True
    assert third.reason == 'aivo_session_continued'
    assert third.mode == 'sales'


def test_aivo_route_detector_uses_remarketing_mode_after_remarketing_was_sent():
    detector, store, _clock = _make_detector()
    source = _make_whatsapp_source()

    detector.decision(SimpleNamespace(text='Aivo Note'), {'source': source})
    session_key = next(iter(store.data))
    session = json.loads(store.data[session_key])
    session['mode'] = 'remarketing'
    session['remarketing_sent_at'] = 1234.0
    store.data[session_key] = session

    reply = detector.decision(SimpleNamespace(text='profissional'), {'source': source})
    assert reply.should_route is True
    assert reply.reason == 'aivo_session_continued'
    assert reply.mode == 'remarketing'


def test_aivo_route_detector_expires_session_after_ten_days():
    detector, store, clock = _make_detector()
    source = _make_whatsapp_source()

    detector.decision(SimpleNamespace(text='Aivo Note'), {'source': source})
    clock[0] += 10 * 24 * 60 * 60 + 1

    expired = detector.decision(SimpleNamespace(text='profissional'), {'source': source})
    assert expired.should_route is False
    assert expired.reason == 'missing_aivo_trigger'
    assert store.data


def test_aivo_remarketing_message_mentions_discount_link():
    vendedor = build_aivo_remarketing_message()
    direct = build_aivo_remarketing_message('direct')
    assert '10% de desconto' in vendedor
    assert 'AIVONOTE10' in vendedor
    assert '10% de desconto' in direct
    assert vendedor != direct


def test_aivo_route_detector_selects_due_remarketing_sessions(monkeypatch):
    now = 1_000.0
    monkeypatch.setattr(
        'aivo_route_detector.iter_aivo_sessions',
        lambda db_path=None: [
            ('aivo_whatsapp_session:5511999999999', {
                'active': True,
                'sender_id': '5511999999999',
                'chat_id': '5511999999999@s.whatsapp.net',
                'started_at': now - 4 * 24 * 60 * 60,
                'last_customer_at': now - 4 * 24 * 60 * 60,
                'updated_at': now - 4 * 24 * 60 * 60,
                'expires_at': now + 6 * 24 * 60 * 60,
                'remarketing_due_at': now - 1,
            })
        ],
    )

    due = select_aivo_remarketing_sessions(now=now)
    assert len(due) == 1
    assert due[0]['sender_id'] == '5511999999999'


def test_aivo_route_detector_ends_session_on_user_request():
    detector, store, _ = _make_detector()
    source = _make_whatsapp_source()

    started = detector.decision(SimpleNamespace(text='Aivo Note'), {'source': source})
    assert started.should_route is True

    ended = detector.decision(SimpleNamespace(text='sair'), {'source': source})
    assert ended.should_route is False
    assert ended.reason == 'aivo_session_ended_by_user'
    assert store.data


def test_aivo_route_detector_starts_from_recent_aivo_history_on_short_followup():
    detector, store, _ = _make_detector()
    source = _make_whatsapp_source()

    reply = detector.decision(
        SimpleNamespace(text='sim'),
        {
            'source': source,
            'history': [
                {'role': 'user', 'content': 'Quero ver os planos do AIVO Note'},
                {'role': 'assistant', 'content': 'Claro, temos Starter, Pro e Max.'},
                {'role': 'user', 'content': 'sim'},
            ],
        },
    )

    assert reply.should_route is True
    assert reply.reason == 'aivo_session_started_from_history'
    assert reply.mode == 'sales'
    assert store.data


def test_aivo_route_detector_routes_from_recent_aivo_context_even_without_trigger_word():
    detector, store, _ = _make_detector()
    source = _make_whatsapp_source()

    reply = detector.decision(
        SimpleNamespace(text='funciona no iPhone?'),
        {
            'source': source,
            'history': [
                {'role': 'user', 'content': 'Quero saber mais do AIVO Note'},
                {'role': 'assistant', 'content': 'Posso te mostrar como funciona.'},
                {'role': 'user', 'content': 'funciona no iPhone?'},
            ],
        },
    )

    assert reply.should_route is True
    assert reply.reason == 'aivo_session_started_from_context'
    assert reply.mode == 'sales'
    assert store.data
