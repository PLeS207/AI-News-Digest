import pytest

from ai_news_digest import config
from ai_news_digest.telegram_bot import Gatekeeper, process_text
from ai_news_digest.testing.fakes import FakeGateway
from ai_news_digest.tracing import read_events
from conftest import GATEWAY_TOKEN, make_coordinator


async def never(req):
    raise AssertionError("not called")


def test_missing_allowlist_fails_with_clear_error(monkeypatch):
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    with pytest.raises(config.ConfigError, match="ALLOWED_TELEGRAM_USER_ID is not set"):
        config.allowed_telegram_user_id()
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "@me")
    with pytest.raises(config.ConfigError, match="numeric"):
        config.allowed_telegram_user_id()
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "123456")
    assert config.allowed_telegram_user_id() == 123456


def test_app_main_exits_without_allowlist_or_gateway_token(monkeypatch, tmp_path):
    from ai_news_digest import telegram_bot

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "t"))
    monkeypatch.setenv("EDITOR_DB_PATH", str(tmp_path / "m.sqlite3"))
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    with pytest.raises(SystemExit, match="ALLOWED_TELEGRAM_USER_ID"):
        telegram_bot.main()
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "123456")
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="OPENCLAW_GATEWAY_TOKEN"):
        telegram_bot.main()


async def test_unauthorized_user_never_reaches_runtime(tmp_path, tracer, sources, defaults):
    gateway = FakeGateway(GATEWAY_TOKEN)
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never, gateway)
    gate = Gatekeeper(allowed_user_id=111, tracer=tracer)
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    out = await process_text(gate=gate, coordinator=coord, user_id=222, chat_key="c", text="Подготовь дайджест",
                             send=send)
    assert out == {"rejected": True} and gateway.requests == []
    assert "Доступ запрещён" in sent[0]
    events = read_events([tracer.path])
    assert events[-1]["kind"] == "auth" and events[-1]["status"] == "denied"
    assert "222" not in tracer.path.read_text()  # rejected id is not logged

    out = await process_text(gate=gate, coordinator=coord, user_id=111, chat_key="c", text="привет", send=send)
    assert out["rejected"] is False and gateway.requests


class FakeBotAPI:
    """Fake Telegram Bot API transport: records outgoing calls, never touches the network."""

    def __init__(self):
        from telegram.request import BaseRequest

        outer = self

        class _Req(BaseRequest):
            @property
            def read_timeout(self):
                return None

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                import json as _json

                api_method = url.rsplit("/", 1)[-1]
                params = request_data.parameters if request_data else {}
                outer.calls.append((api_method, params))
                if api_method == "getMe":
                    result = {"id": 999, "is_bot": True, "first_name": "Digest", "username": "digest_test_bot"}
                elif api_method == "sendMessage":
                    result = {"message_id": len(outer.calls), "date": 0,
                              "chat": {"id": params["chat_id"], "type": "private"}, "text": params["text"]}
                else:
                    result = True
                return 200, _json.dumps({"ok": True, "result": result}).encode()

        self.calls: list[tuple[str, dict]] = []
        self.request = _Req()

    def sent_texts(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def _update(user_id: int, text: str, update_id: int = 1) -> dict:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": 0, "text": text, "entities": entities,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "U"}}}


async def test_ptb_application_wiring_with_fake_bot_api(tmp_path, tracer, sources, defaults, limits,
                                                        researcher_llm, researcher_tracer, feed_http):
    from telegram import Update

    from ai_news_digest.researcher.agent import Researcher
    from ai_news_digest.telegram_bot import build_application
    from conftest import NOW

    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)

    async def research(req):
        return await researcher.research(req.model_copy(update={"now": NOW})), "task-1"

    gateway = FakeGateway(GATEWAY_TOKEN)
    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, research, gateway)
    api = FakeBotAPI()
    app = build_application("123456:TEST-TOKEN", Gatekeeper(111, tracer), coord, memory, request=api.request)
    await app.initialize()
    try:
        await app.process_update(Update.de_json(_update(222, "/digest", 1), app.bot))
        assert gateway.requests == [] and "Доступ запрещён" in api.sent_texts()[-1]

        await app.process_update(Update.de_json(_update(111, "Запомни: максимум 2 новости", 2), app.bot))
        assert memory.get_settings()[0].max_items == 2
        assert any(m == "sendChatAction" for m, _ in api.calls)

        await app.process_update(Update.de_json(_update(111, "/digest", 3), app.bot))
        assert any("AI-дайджест" in t for t in api.sent_texts()) and len(memory.delivered_urls()) == 2
        first_digest_call = [r for r in gateway.requests if r["body"]["messages"][0]["role"] == "user"][-1]
        assert "routed_intent" in first_digest_call["body"]["messages"][0]["content"]

        before = (memory.stats(), memory.delivered_urls())
        n = len(api.sent_texts())
        await app.process_update(Update.de_json(_update(111, "/last", 4), app.bot))
        resent = api.sent_texts()[n:]
        assert resent and "Повтор сохранённого дайджеста" in resent[0]
        assert (memory.stats(), memory.delivered_urls()) == before

        old_session = coord.session_key("111")
        await app.process_update(Update.de_json(_update(111, "/new", 5), app.bot))
        assert "Новая беседа" in api.sent_texts()[-1] and coord.session_key("111") != old_session
        await app.process_update(Update.de_json(_update(111, "/settings", 6), app.bot))
        assert "Новостей в дайджесте: до 2" in api.sent_texts()[-1] and "SQLite" in api.sent_texts()[-1]
        sends = [p for m, p in api.calls if m == "sendMessage"]
        assert all(p.get("parse_mode") in (None, "HTML") for p in sends)
    finally:
        await app.shutdown()
