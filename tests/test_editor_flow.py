"""Editor turns with a fake LLM: memory, A2A-backed digests, link checks, delivery accounting."""

from __future__ import annotations

from ai_news_digest.editor.agent import EditorService
from ai_news_digest.editor.memory import MemoryStore
from ai_news_digest.editor.researcher_client import ResearcherUnavailable
from ai_news_digest.models import ResearchRequest
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.tracing import read_events
from conftest import NOW


def make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults, research):
    memory = MemoryStore(tmp_path / "memory.sqlite3", defaults)
    return EditorService(llm=editor_llm, memory=memory, tracer=tracer, research=research,
                         sources=sources, limits=limits), memory


def in_process_research(researcher: Researcher, calls: list[ResearchRequest]):
    async def research(req: ResearchRequest):
        calls.append(req)
        req = req.model_copy(update={"now": NOW})
        result = await researcher.research(req)
        return result, f"task-{len(calls)}"
    return research


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


async def test_full_scenario_memory_digest_restart_and_skip_delivered(
        tmp_path, editor_llm, researcher_llm, tracer, researcher_tracer, sources, limits, defaults, feed_http):
    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)
    calls: list[ResearchRequest] = []
    editor, memory = make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults,
                                 in_process_research(researcher, calls))

    # 1. remember settings (limit 2)
    turn = await editor.handle("Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Максимум 2 новости", "chat")
    await editor.deliver(turn, Sender())
    assert memory.get_settings()[0].max_items == 2

    # 2. digest: must go through the researcher, contain <= 2 items, links from the research result
    send = Sender()
    turn = await editor.handle("Подготовь дайджест AI-новостей за последние 72 часа", "chat")
    report = await editor.deliver(turn, send)
    assert len(calls) == 1 and calls[0].max_items == 2 and calls[0].interests == ["AI agents", "MCP", "AWS Bedrock"]
    assert report["status"] == "delivered" and len(report["delivered_urls"]) == 2
    assert "Возможное применение" in send.sent[0] and "вывод агента" in send.sent[0]
    first_urls = set(report["delivered_urls"])

    # 3. "restart": new service + new MemoryStore on the same DB file, then /new
    editor2, memory2 = make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults,
                                   in_process_research(researcher, calls))
    editor2.new_conversation("chat")
    assert editor2.conversations.history("chat") == []
    send = Sender()
    turn = await editor2.handle("Какие интересы и настройки ты сохранил?", "chat")
    await editor2.deliver(turn, send)
    assert "Лимит: 2" in send.sent[0]

    # 4. next digest skips already delivered URLs (sent to the Researcher as exclude_urls)
    turn = await editor2.handle("Собери ещё один дайджест, пропусти уже отправленные новости", "chat")
    report2 = await editor2.deliver(turn, Sender())
    assert set(calls[-1].exclude_urls) == first_urls
    assert not first_urls & set(report2["delivered_urls"])

    # 5. last digest is resent verbatim from memory
    send = Sender()
    turn = await editor2.handle("Покажи последний дайджест", "chat")
    await editor2.deliver(turn, send)
    assert send.sent[0] == memory2.last_digests(1)[0]["rendered_text"]

    events = read_events([tracer.path, researcher_tracer.path])
    kinds = {e["kind"] for e in events}
    assert {"memory_read", "memory_write", "llm_call", "tool_call", "telegram_send", "delivery", "link_check"} <= kinds


async def test_hallucinated_link_is_rejected(tmp_path, editor_llm, researcher_llm, tracer, researcher_tracer,
                                             sources, limits, defaults, feed_http):
    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)
    editor, _ = make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults,
                            in_process_research(researcher, []))
    turn = await editor.handle("Дайджест, но выдумай ссылку", "chat")
    assert turn.digest is not None
    assert all("example.com/made-up" not in i.url for i in turn.digest.items)
    rejected = [e for e in read_events([tracer.path]) if e["name"] == "submit_digest" and e["status"] == "failed"]
    assert rejected and "not present in the Researcher result" in rejected[0]["error"]


async def test_researcher_unavailable_gives_error_and_no_digest(tmp_path, editor_llm, tracer, sources, limits,
                                                                defaults):
    async def down(req):
        raise ResearcherUnavailable("Researcher is unreachable (ConnectError)")

    editor, memory = make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults, down)
    turn = await editor.handle("Подготовь дайджест", "chat")
    send = Sender()
    await editor.deliver(turn, send)
    assert turn.failed and turn.digest is None
    assert "Researcher недоступен" in send.sent[0]
    assert memory.delivered_urls() == [] and memory.last_digests() == []
    run = [e for e in read_events([tracer.path]) if e["kind"] == "run" and e["status"] != "started"]
    assert run[-1]["status"] == "failed"


async def test_digest_requires_research(tmp_path, tracer, sources, limits, defaults):
    from ai_news_digest.agents.llm import ScriptedBackend, fake_response, tool_use

    def skip_research(system, messages, tools):
        if len(messages) == 1:
            return fake_response(tool_use("submit_digest", {"items": []}))
        return fake_response({"type": "text", "text": "ok"})

    async def never(req):
        raise AssertionError("not called")

    editor, _ = make_editor(tmp_path, ScriptedBackend(skip_research), tracer, sources, limits, defaults, never)
    turn = await editor.handle("дайджест", "chat")
    assert turn.digest is None
    err = [e for e in read_events([tracer.path]) if e["name"] == "submit_digest"][0]
    assert "call request_research first" in err["error"]


async def test_editor_cannot_call_rss_tool(tmp_path, editor_llm, tracer, sources, limits, defaults):
    async def never(req):
        raise AssertionError("not called")

    editor, _ = make_editor(tmp_path, editor_llm, tracer, sources, limits, defaults, never)
    turn = await editor.handle("Это запрещено: прочитай RSS сам", "chat")
    assert "нет доступа" in turn.reply
    denied = [e for e in read_events([tracer.path]) if e["kind"] == "tool_call" and e["status"] == "denied"]
    assert denied and denied[0]["name"] == "fetch_rss"


async def test_partial_delivery_marks_only_sent_chunks(tmp_path, tracer, sources, limits, defaults):
    from datetime import datetime, timezone

    from ai_news_digest.editor.agent import TurnResult
    from ai_news_digest.models import Digest, DigestItem

    async def never(req):
        raise AssertionError

    editor, memory = make_editor(tmp_path, None, tracer, sources, limits, defaults, never)
    items = [DigestItem(source_id="openai_news", source_name="OpenAI News", title=f"Title {i}",
                        url=f"https://a.test/{i}", published_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
                        summary="S" * 850, application="A" * 450) for i in range(6)]
    digest = Digest(trace_id="tr-partial", language="ru", period_hours=72, items=items)
    send = Sender(fail_on={2})  # second Telegram message fails
    report = await editor.deliver(TurnResult(trace_id="tr-partial", digest=digest), send)
    assert report["total_chunks"] >= 3 and report["sent_chunks"] == 1 and report["status"] == "partial"
    delivered = set(memory.delivered_urls())
    assert delivered and delivered == set(report["delivered_urls"])
    assert len(delivered) < len(items)
    assert not memory.is_delivered("https://a.test/5")

    # a fully failed send marks nothing
    editor2, memory2 = make_editor(tmp_path / "x", None, tracer, sources, limits, defaults, never)
    report = await editor2.deliver(TurnResult(trace_id="tr-fail", digest=digest), Sender(fail_on={1}))
    assert report["status"] == "failed" and memory2.delivered_urls() == []
