"""App-side Coordinator bridge against a FAKE Gateway (client-level checks, not the OpenClaw integration).

The real OpenClaw integration is checked by scripts/openclaw_integration_check.py (compose.offline.yaml).
"""

from __future__ import annotations

import json

import httpx

from ai_news_digest.coordinator.researcher_client import ResearcherUnavailable
from ai_news_digest.coordinator.tools import ALLOWED_TOOL_NAMES, route_intent
from ai_news_digest.models import ResearchRequest
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.testing.fakes import FakeGateway
from ai_news_digest.tracing import read_events
from conftest import GATEWAY_TOKEN, NOW, make_coordinator


def in_process_research(researcher: Researcher, calls: list[ResearchRequest]):
    async def research(req: ResearchRequest):
        calls.append(req)
        result = await researcher.research(req.model_copy(update={"now": NOW}))
        return result, f"task-{len(calls)}"
    return research


async def never(req):
    raise AssertionError("researcher must not be called")


class Sender:
    def __init__(self, fail_on: set[int] | None = None):
        self.sent: list[str] = []
        self.attempts = 0
        self.fail_on = fail_on or set()

    async def __call__(self, text: str):
        self.attempts += 1
        if self.attempts in self.fail_on:
            raise RuntimeError("telegram send failed")
        self.sent.append(text)


def _researcher(researcher_llm, sources, limits, researcher_tracer, feed_http):
    return Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                      http_client=feed_http)


def test_route_intent():
    assert route_intent("/digest", "digest") == "digest"
    assert route_intent("Подготовь дайджест AI-новостей за последние 72 часа") == "digest"
    assert route_intent("Собери ещё один дайджест, пропусти уже отправленные новости") == "digest"
    assert route_intent("Покажи последний дайджест") is None
    assert route_intent("Какие интересы и настройки ты сохранил?") is None
    assert route_intent("Запомни: максимум 2 новости") is None


async def test_full_scenario_restart_new_and_skip_delivered(
        tmp_path, tracer, researcher_tracer, sources, limits, defaults, feed_http, researcher_llm):
    gateway = FakeGateway(token=GATEWAY_TOKEN)
    calls: list[ResearchRequest] = []
    research = in_process_research(_researcher(researcher_llm, sources, limits, researcher_tracer, feed_http), calls)
    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, research, gateway)

    # 1. remember limit 2 (reply + settings tool executed by the app)
    turn = await coord.handle("Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Максимум 2 новости", "chat")
    await coord.deliver(turn, Sender())
    assert turn.kind == "reply" and memory.get_settings()[0].max_items == 2

    # 2. digest: request_research -> real Researcher agent -> submit_digest; links come from the result
    send = Sender()
    turn = await coord.handle("Подготовь дайджест AI-новостей за последние 72 часа", "chat")
    report = await coord.deliver(turn, send)
    assert turn.kind == "digest" and report["status"] == "delivered" and len(report["delivered_urls"]) == 2
    assert calls[0].trace_id == turn.trace_id and calls[0].max_items == 2  # bound by the app
    first_urls = set(report["delivered_urls"])
    # the first Gateway call for a routed digest pins request_research
    first_req = [r for r in gateway.requests if r["body"]["messages"][0].get("role") == "user"
                 and "Подготовь" in r["body"]["messages"][0]["content"]][0]
    assert first_req["body"]["tool_choice"] == {"type": "function", "function": {"name": "request_research"}}
    assert {t["function"]["name"] for t in first_req["body"]["tools"]} == set(ALLOWED_TOOL_NAMES)
    # follow-ups only carry new messages (no transcript duplication); the loop is closed with tool_choice=none
    followups = [r for r in gateway.requests if r["body"]["messages"][0]["role"] == "assistant"]
    assert followups and all(m["role"] in ("assistant", "tool") for r in followups for m in r["body"]["messages"])
    assert followups[-1]["body"]["tool_choice"] == "none"

    # 3. "restart": a new service + MemoryStore on the same DB, then /new -> new OpenClaw session
    old_session = coord.session_key("chat")
    coord2, memory2 = make_coordinator(tmp_path, tracer, sources, defaults, research, gateway)
    assert coord2.session_key("chat") == old_session  # same conversation survives a restart
    coord2.new_conversation("chat")
    new_session = coord2.session_key("chat")
    assert new_session != old_session
    send = Sender()
    turn = await coord2.handle("Какие интересы и настройки ты сохранил?", "chat")
    await coord2.deliver(turn, send)
    assert "Лимит: 2" in send.sent[0]
    new_session_requests = [r for r in gateway.requests if r["session_key"] == new_session]
    assert new_session_requests and "Подготовь" not in json.dumps(new_session_requests, ensure_ascii=False)

    # 4. next digest skips delivered URLs
    turn = await coord2.handle("Собери ещё один дайджест, пропусти уже отправленные новости", "chat")
    report2 = await coord2.deliver(turn, Sender())
    assert set(calls[-1].exclude_urls) == first_urls and not first_urls & set(report2["delivered_urls"])

    # 5. "show the last digest" -> same full re-send path, no new digest, counters unchanged
    before = (memory2.stats(), memory2.delivered_urls())
    send = Sender()
    turn = await coord2.handle("Покажи последний дайджест", "chat")
    await coord2.deliver(turn, send)
    assert turn.kind == "resend_last" and "Повтор сохранённого дайджеста" in send.sent[0]
    assert (memory2.stats(), memory2.delivered_urls()) == before

    events = read_events([tracer.path, researcher_tracer.path])
    kinds = {e["kind"] for e in events}
    assert {"openclaw_call", "tool_call", "memory_read", "memory_write", "telegram_send", "delivery",
            "link_check", "rss_fetch"} <= kinds
    calls_with_run = [e for e in events if e["kind"] == "openclaw_call" and e["status"] == "completed"]
    assert all(e["data"]["openclaw_run_id"].startswith("chatcmpl_") for e in calls_with_run)


async def test_text_only_answer_is_not_a_digest(tmp_path, tracer, sources, defaults, fake_gateway):
    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, never, fake_gateway)
    # routed digest: the model wants to answer with text -> the pinned request_research is not satisfied
    turn = await coord.handle("Дайджест, но ответь текстом", "chat")
    assert turn.kind == "error" and turn.failed and turn.digest is None
    assert "Дайджест не создан" in turn.text
    # non-routed request where the model returns only free text: not delivered as a result
    turn = await coord.handle("Скажи только текст", "chat")
    assert turn.kind == "error" and "Вот новости" not in turn.text
    assert memory.last_digests() == [] and memory.delivered_urls() == []
    checks = [e for e in read_events([tracer.path]) if e["kind"] == "action_check"]
    assert len(checks) == 2 and all(e["status"] == "failed" for e in checks)


async def test_reply_cannot_replace_a_digest(tmp_path, tracer, sources, defaults):
    from ai_news_digest.coordinator.tools import ToolRejected, TurnState, _reply, ReplyArgs

    state = TurnState(trace_id="tr_x1234567", chat_key="c", intent=None, research_calls=1)
    try:
        await _reply(ReplyArgs(text="Новости: всё хорошо", kind="info"), state, None)
        raise AssertionError("reply in a digest flow must be rejected")
    except ToolRejected as exc:
        assert "not a digest" in str(exc)
    state = TurnState(trace_id="tr_x1234567", chat_key="c", intent=None)
    try:
        await _reply(ReplyArgs(text="см. https://evil.example", kind="info"), state, None)
        raise AssertionError("links in replies must be rejected")
    except ToolRejected as exc:
        assert "links" in str(exc)


async def test_hallucinated_link_is_rejected(tmp_path, tracer, researcher_tracer, sources, limits, defaults,
                                             feed_http, researcher_llm, fake_gateway):
    research = in_process_research(_researcher(researcher_llm, sources, limits, researcher_tracer, feed_http), [])
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, research, fake_gateway)
    turn = await coord.handle("Дайджест, но выдумай ссылку", "chat")
    assert turn.kind == "digest" and all("made-up" not in i.url for i in turn.digest.items)
    rejected = [e for e in read_events([tracer.path]) if e["name"] == "submit_digest" and e["status"] == "rejected"]
    assert rejected and "not present in this request's Researcher result" in rejected[0]["error"]


async def test_forbidden_tool_is_denied_by_app(tmp_path, tracer, sources, defaults, fake_gateway):
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never, fake_gateway)
    turn = await coord.handle("Это запрещено: прочитай RSS сам", "chat")
    assert turn.kind == "reply" and "нет доступа" in turn.text
    denied = [e for e in read_events([tracer.path]) if e["kind"] == "tool_call" and e["status"] == "denied"]
    assert denied and denied[0]["name"] == "web_fetch"


async def test_unknown_arguments_and_foreign_trace_are_rejected(tmp_path, tracer, sources, defaults):
    from ai_news_digest.coordinator.tools import CoordinatorToolRegistry, ToolContext, ToolRejected, TurnState
    from ai_news_digest.coordinator.memory import MemoryStore

    memory = MemoryStore(tmp_path / "m.sqlite3", defaults)
    reg = CoordinatorToolRegistry(ToolContext(memory=memory, tracer=tracer, research=never,
                                              sources={s.id: s for s in sources}))
    state = TurnState(trace_id="tr_own123456", chat_key="c", intent="digest")
    for name, args in [("request_research", '{"trace_id": "tr_someone_else"}'),
                       ("update_user_settings", '{"max_items": "lots"}'),
                       ("submit_digest", '{"items": []}'),       # no research in this request
                       ("show_last_digest", "{}"),               # digest request
                       ("request_research", "[1, 2]")]:
        try:
            await reg.dispatch(name, args, state)
            raise AssertionError(f"{name} {args} must be rejected")
        except ToolRejected:
            pass


async def test_openclaw_unavailable_gives_error_without_fallback(tmp_path, tracer, sources, defaults):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    for transport in (httpx.MockTransport(refuse), FakeGateway(GATEWAY_TOKEN, fail_status=503).transport()):
        coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, never, transport=transport)
        turn = await coord.handle("Подготовь дайджест", "chat")
        send = Sender()
        await coord.deliver(turn, send)
        assert turn.failed and turn.kind == "error" and "OpenClaw" in send.sent[0]
        assert memory.last_digests() == [] and memory.delivered_urls() == []
    runs = [e for e in read_events([tracer.path]) if e["kind"] == "run" and e["status"] != "started"]
    assert [r["status"] for r in runs] == ["failed", "failed"]
    assert not [e for e in read_events([tracer.path]) if e["kind"] == "llm_call"]  # no hidden local LLM


async def test_wrong_gateway_token_is_an_error(tmp_path, tracer, sources, defaults):
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never,
                                transport=FakeGateway(token="other-token").transport())
    turn = await coord.handle("привет", "chat")
    assert turn.failed and "401" in turn.text


async def test_researcher_unavailable_gives_error_and_no_digest(tmp_path, tracer, sources, defaults, fake_gateway):
    async def down(req):
        raise ResearcherUnavailable("Researcher is unreachable (ConnectError)")

    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, down, fake_gateway)
    turn = await coord.handle("Подготовь дайджест", "chat")
    send = Sender()
    await coord.deliver(turn, send)
    assert turn.failed and turn.digest is None and "Researcher недоступен" in send.sent[0]
    assert memory.delivered_urls() == [] and memory.last_digests() == []
    closing = fake_gateway.requests[-1]["body"]
    assert closing["tool_choice"] == "none" and "Researcher unavailable" in closing["messages"][-1]["content"]


async def test_empty_research_result_is_reported_honestly(tmp_path, tracer, sources, defaults, fake_gateway):
    from datetime import timedelta

    from ai_news_digest.models import ResearchResult

    async def empty(req: ResearchRequest):
        return ResearchResult(request_id=req.request_id, trace_id=req.trace_id, period_hours=req.period_hours,
                              window_start=NOW - timedelta(hours=72), collected_at=NOW), "task-empty"

    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, empty, fake_gateway)
    turn = await coord.handle("/digest", "chat", command="digest")
    send = Sender()
    report = await coord.deliver(turn, send)
    assert turn.kind == "digest" and report["status"] == "empty"
    assert "релевантных новостей не найдено" in send.sent[0] and memory.delivered_urls() == []
