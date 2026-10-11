"""Dashboard renders merged traces (headless Streamlit AppTest).

App and Researcher traces come from a FAKE Gateway scenario with a real A2A exchange. The OpenClaw hook
events below are TEST DATA shaped like the digest-telemetry plugin output; they check the join logic
only. Real OpenClaw events come from scripts/openclaw_integration_check.py.
"""

import json
from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")

ROOT = Path(__file__).resolve().parents[1]


async def _generate_traces(out: Path) -> None:
    """Fake Gateway for both agents, REAL A2A over HTTP, fixture feeds (one of them broken)."""
    import httpx

    from ai_news_digest.config import DigestDefaults, Limits, SourceConfig
    from ai_news_digest.coordinator.memory import MemoryStore
    from ai_news_digest.coordinator.researcher_client import ResearcherClient
    from ai_news_digest.coordinator.service import CoordinatorService
    from ai_news_digest.openclaw_client import OpenClawClient
    from ai_news_digest.researcher.agent import Researcher
    from ai_news_digest.testing.fakes import FakeGateway
    from ai_news_digest.tracing import Tracer
    from conftest import GATEWAY_TOKEN, NOW, feed_handler, researcher_client
    from test_a2a_roundtrip import running_researcher

    sources = [
        SourceConfig(id="openai_news", name="OpenAI News", url="https://feeds.test/openai.xml"),
        SourceConfig(id="huggingface_blog", name="Hugging Face Blog", url="https://feeds.test/hf.xml"),
        SourceConfig(id="aws_whats_new", name="AWS What's New", url="https://feeds.test/aws.xml", interest_filter=True),
        SourceConfig(id="broken_source", name="Broken (demo)", url="https://feeds.test/broken.xml"),
    ]
    app_tracer, researcher_tracer = Tracer("app", out / "traces"), Tracer("researcher", out / "traces")
    researcher = Researcher(
        openclaw=researcher_client(FakeGateway(GATEWAY_TOKEN, agent_id="researcher").transport()),
        sources=sources, limits=Limits(), tracer=researcher_tracer,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(feed_handler)))
    editor = OpenClawClient("http://openclaw.test", GATEWAY_TOKEN, transport=FakeGateway(GATEWAY_TOKEN).transport())

    async def send(text: str) -> None:
        pass

    async with running_researcher(researcher, researcher_tracer) as url:
        client = ResearcherClient(url, app_tracer, timeout_seconds=30)

        async def research(req):
            return await client.research(req.model_copy(update={"now": NOW}))

        coord = CoordinatorService(openclaw=editor, memory=MemoryStore(out / "memory.sqlite3", DigestDefaults()),
                                   tracer=app_tracer, research=research, sources=sources)
        for msg in ["Remember: I'm interested in AI agents, MCP and AWS Bedrock. At most 2 news items",
                    "Prepare an AI news digest for the last 72 hours"]:
            await coord.deliver(await coord.handle(msg, "demo"), send)
        coord.new_conversation("demo")
        for msg in ["What interests and settings have you saved?",
                    "Build another digest and skip the news you already sent"]:
            await coord.deliver(await coord.handle(msg, "demo"), send)
    coord.registry.ctx.research = ResearcherClient(url, app_tracer, timeout_seconds=5, backoff_seconds=0.1).research
    await coord.deliver(await coord.handle("Prepare the digest again", "demo"), send)  # Researcher is down now


async def test_dashboard_shows_app_openclaw_and_researcher_in_one_trace(tmp_path, monkeypatch):
    await _generate_traces(tmp_path)

    app_file = tmp_path / "traces" / "app" / "events.jsonl"
    researcher_file = tmp_path / "traces" / "researcher" / "events.jsonl"
    app_events = [json.loads(line) for line in app_file.read_text().splitlines()]
    call = next(e for e in app_events if e["kind"] == "openclaw_call" and e["status"] == "completed"
                and e["data"].get("requested_tools") == ["request_research"])
    run_id, session = call["data"]["openclaw_run_id"], call["data"]["openclaw_session_key"]
    oc_file = tmp_path / "traces" / "openclaw" / "events.jsonl"
    oc_file.parent.mkdir(parents=True)
    base = {"trace_id": None, "service": "openclaw", "agent": "editor", "runtime": "openclaw", "input": None,
            "output": None, "error": None, "duration_ms": 12.0}
    oc_events = [
        {**base, "ts": call["ts"], "span_id": "a", "kind": "llm_input", "name": "openclaw.llm_input",
         "status": "completed", "data": {"openclaw_run_id": run_id, "openclaw_session_key": f"agent:editor:{session}",
                                         "native_tools_after_policy": [], "workspace_instructions_loaded": True}},
        {**base, "ts": call["ts"], "span_id": "b", "kind": "llm_call", "name": "openclaw.model_call c1",
         "status": "completed", "data": {"openclaw_run_id": run_id, "openclaw_session_key": f"agent:editor:{session}"}},
    ]
    oc_file.write_text("".join(json.dumps(e) + "\n" for e in oc_events))

    monkeypatch.setenv("TRACE_FILES", ",".join(str(f) for f in (app_file, researcher_file, oc_file)))
    at = streamlit_testing.AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    runs = at.dataframe[0].value
    assert len(runs) == 5
    assert set(runs["status"].str.split().str[-1]) >= {"completed", "failed", "completed_with_errors"}
    joined = runs[runs["trace_id"] == call["trace_id"]].iloc[0]
    assert "editor (openclaw)" in joined["agents (runtime)"] and "researcher (python)" in joined["agents (runtime)"]
    assert joined["a2a_task"] and joined["openclaw_runs"] >= 1

    at.selectbox[0].select(call["trace_id"]).run()
    assert not at.exception
    timeline = at.dataframe[1].value
    assert {"app", "editor", "researcher"} <= set(timeline["agent"])
    assert "Model calls · editor (OpenClaw)" in [m.label for m in at.metric]
    assert {"python", "openclaw"} <= set(timeline["runtime"])
    assert {"openclaw_call", "llm_input", "tool_call", "a2a_discovery", "a2a_send", "a2a_task", "rss_fetch",
            "memory_read", "telegram_send", "delivery"} <= set(timeline["kind"])
    assert (timeline["openclaw_run_id"] == run_id).sum() >= 3
