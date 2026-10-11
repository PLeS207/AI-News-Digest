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


def _researcher(researcher_openclaw, sources, limits, researcher_tracer, feed_http):
    return Researcher(openclaw=researcher_openclaw, sources=sources, limits=limits, tracer=researcher_tracer,
                      http_client=feed_http)


def test_route_intent():
    assert route_intent("/digest", "digest") == "digest"
    assert route_intent("Prepare an AI news digest for the last 72 hours") == "digest"
    assert route_intent("Build another digest and skip the news you already sent") == "digest"
    for not_new in ("Show the last digest", "Show me the latest digest", "Resend the previous digest",
                    "What interests and settings have you saved?", "Remember: at most 2 news items"):
        assert route_intent(not_new) is None, not_new
    # multilingual input: the same intents in Russian (intentional non-English test data)
    assert route_intent("Подготовь дайджест AI-новостей за последние 72 часа") == "digest"
    assert route_intent("Покажи последний дайджест") is None
    assert route_intent("Запомни: максимум 2 новости") is None


async def test_full_scenario_restart_new_and_skip_delivered(
        tmp_path, tracer, researcher_tracer, sources, limits, defaults, feed_http, researcher_openclaw):
    gateway = FakeGateway(token=GATEWAY_TOKEN)
    calls: list[ResearchRequest] = []
    research = in_process_research(_researcher(researcher_openclaw, sources, limits, researcher_tracer, feed_http), calls)
    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, research, gateway)

    # 1. remember limit 2 (reply + settings tool executed by the app)
    turn = await coord.handle("Remember: I'm interested in AI agents, MCP and AWS Bedrock. At most 2 news items", "chat")
    await coord.deliver(turn, Sender())
    assert turn.kind == "reply" and memory.get_settings()[0].max_items == 2

    # 2. digest: request_research -> real Researcher agent -> submit_digest; links come from the result
    send = Sender()
    turn = await coord.handle("Prepare an AI news digest for the last 72 hours", "chat")
    report = await coord.deliver(turn, send)
    assert turn.kind == "digest" and report["status"] == "delivered" and len(report["delivered_urls"]) == 2
    assert calls[0].trace_id == turn.trace_id and calls[0].max_items == 2  # bound by the app
    first_urls = set(report["delivered_urls"])
    # the first Gateway call for a routed digest pins request_research
    first_req = [r for r in gateway.requests if r["body"]["messages"][0].get("role") == "user"
                 and "Prepare an AI news digest" in r["body"]["messages"][0]["content"]][0]
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
    turn = await coord2.handle("What interests and settings have you saved?", "chat")
    await coord2.deliver(turn, send)
    assert "Limit: 2" in send.sent[0]
    new_session_requests = [r for r in gateway.requests if r["session_key"] == f"agent:editor:{new_session}"]
    assert new_session_requests and "Prepare an AI news digest" not in json.dumps(new_session_requests, ensure_ascii=False)

    # 4. next digest skips delivered URLs
    turn = await coord2.handle("Build another digest and skip the news you already sent", "chat")
    report2 = await coord2.deliver(turn, Sender())
    assert set(calls[-1].exclude_urls) == first_urls and not first_urls & set(report2["delivered_urls"])

    # 5. "show the last digest" -> same full re-send path, no new digest, counters unchanged
    before = (memory2.stats(), memory2.delivered_urls())
    send = Sender()
    turn = await coord2.handle("Show the last digest", "chat")
    await coord2.deliver(turn, send)
    assert turn.kind == "resend_last" and "Re-sending the saved digest" in send.sent[0]
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
    turn = await coord.handle("Digest, but answer in text", "chat")
    assert turn.kind == "error" and turn.failed and turn.digest is None
    assert "No digest was created" in turn.text
    # non-routed request where the model returns only free text: not delivered as a result
    turn = await coord.handle("Say text only", "chat")
    assert turn.kind == "error" and "Here is the news" not in turn.text
    assert memory.last_digests() == [] and memory.delivered_urls() == []
    checks = [e for e in read_events([tracer.path]) if e["kind"] == "action_check"]
    assert len(checks) == 2 and all(e["status"] == "failed" for e in checks)


async def test_reply_cannot_replace_a_digest(tmp_path, tracer, sources, defaults):
    from ai_news_digest.coordinator.tools import ToolRejected, TurnState, _reply, ReplyArgs

    state = TurnState(trace_id="tr_x1234567", chat_key="c", intent=None, research_calls=1)
    try:
        await _reply(ReplyArgs(text="News: all is well", kind="info"), state, None)
        raise AssertionError("reply in a digest flow must be rejected")
    except ToolRejected as exc:
        assert "not a digest" in str(exc)
    state = TurnState(trace_id="tr_x1234567", chat_key="c", intent=None)
    try:
        await _reply(ReplyArgs(text="see https://evil.example", kind="info"), state, None)
        raise AssertionError("links in replies must be rejected")
    except ToolRejected as exc:
        assert "links" in str(exc)


async def test_hallucinated_link_is_rejected(tmp_path, tracer, researcher_tracer, sources, limits, defaults,
                                             feed_http, researcher_openclaw, fake_gateway):
    research = in_process_research(_researcher(researcher_openclaw, sources, limits, researcher_tracer, feed_http), [])
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, research, fake_gateway)
    turn = await coord.handle("Digest, but invent a link", "chat")
    assert turn.kind == "digest" and all("made-up" not in i.url for i in turn.digest.items)
    rejected = [e for e in read_events([tracer.path]) if e["name"] == "submit_digest" and e["status"] == "rejected"]
    assert rejected and "not present in this request's Researcher result" in rejected[0]["error"]


async def test_forbidden_tool_is_denied_by_app(tmp_path, tracer, sources, defaults, fake_gateway):
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never, fake_gateway)
    turn = await coord.handle("This is forbidden: read the RSS feed yourself", "chat")
    assert turn.kind == "reply" and "do not have access" in turn.text
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
        turn = await coord.handle("Prepare a digest", "chat")
        send = Sender()
        await coord.deliver(turn, send)
        assert turn.failed and turn.kind == "error" and "OpenClaw" in send.sent[0]
        assert memory.last_digests() == [] and memory.delivered_urls() == []
    runs = [e for e in read_events([tracer.path]) if e["kind"] == "run" and e["status"] != "started"]
    assert [r["status"] for r in runs] == ["failed", "failed"]
    assert not [e for e in read_events([tracer.path]) if e["kind"] == "llm_call"]  # no hidden local LLM


async def test_subscription_limit_is_a_clear_error_without_paid_fallback(tmp_path, tracer, sources, defaults):
    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error",
                                                   "message": "You've hit your usage limit. Try again later."}})

    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, never, transport=httpx.MockTransport(limited))
    turn = await coord.handle("/digest", "chat", command="digest")
    send = Sender()
    await coord.deliver(turn, send)
    assert turn.failed and "subscription limit" in send.sent[0] and "no paid API" in send.sent[0]
    assert memory.last_digests() == [] and not [e for e in read_events([tracer.path]) if e["kind"] == "llm_call"]


async def test_wrong_gateway_token_is_an_error(tmp_path, tracer, sources, defaults):
    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never,
                                transport=FakeGateway(token="other-token").transport())
    turn = await coord.handle("hello", "chat")
    assert turn.failed and "401" in turn.text


async def test_researcher_unavailable_gives_error_and_no_digest(tmp_path, tracer, sources, defaults, fake_gateway):
    async def down(req):
        raise ResearcherUnavailable("Researcher is unreachable (ConnectError)")

    coord, memory = make_coordinator(tmp_path, tracer, sources, defaults, down, fake_gateway)
    turn = await coord.handle("Prepare a digest", "chat")
    send = Sender()
    await coord.deliver(turn, send)
    assert turn.failed and turn.digest is None and "Researcher is unavailable" in send.sent[0]
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
    assert "No relevant news found" in send.sent[0] and memory.delivered_urls() == []


async def test_parallel_requests_are_isolated(tmp_path, tracer, sources, defaults, fake_gateway):
    import asyncio

    from ai_news_digest.telegram_bot import Gatekeeper, process_text

    coord, _ = make_coordinator(tmp_path, tracer, sources, defaults, never, gateway=fake_gateway)
    gate = Gatekeeper(1, tracer)
    sent: dict[str, list[str]] = {"a": [], "b": []}

    async def run(chat: str, text: str):
        async def send(t: str) -> None:
            sent[chat].append(t)
        return await process_text(gate=gate, coordinator=coord, user_id=1, chat_key=chat, text=text, send=send)

    results = await asyncio.gather(run("a", "What interests and settings have you saved?"), run("b", "hello"),
                                   run("a", "hello"))
    assert len({r["trace_id"] for r in results}) == 3 and not any(r["failed"] for r in results)
    keys = [r["session_key"] for r in fake_gateway.requests]
    assert len(set(keys)) == 2  # one OpenClaw session per chat
    # requests of chat "a" are not interleaved: each turn finishes (incl. loop closure) before the next starts
    a_users = [i for i, r in enumerate(fake_gateway.requests)
               if r["session_key"].endswith(coord.session_key("a")) and r["body"]["messages"][0]["role"] == "user"]
    assert len(a_users) == 2 and len(sent["a"]) == 2 and len(sent["b"]) == 1
