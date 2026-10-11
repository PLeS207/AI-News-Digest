"""Real A2A round trip over HTTP: official a2a-sdk server (uvicorn) + official a2a-sdk client.

The LLM is fake (scripted) and RSS comes from local fixtures; the transport, Agent Card discovery,
JSON-RPC calls, task lifecycle and result validation are real.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from ai_news_digest.agents.llm import ScriptedBackend
from ai_news_digest.editor.researcher_client import ResearcherClient, ResearcherUnavailable
from ai_news_digest.models import ResearchRequest
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.researcher.server import build_app
from ai_news_digest.tracing import read_events
from conftest import NOW


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def running_researcher(researcher: Researcher, tracer):
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(build_app(researcher, tracer, url), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield url
    finally:
        server.should_exit = True
        await task


def _request(trace_id: str = "tr-a2a-0001", **kw) -> ResearchRequest:
    data = dict(request_id="req-a2a-0001", trace_id=trace_id, period_hours=72,
                interests=["AI agents", "MCP", "AWS Bedrock"], max_items=3, now=NOW)
    data.update(kw)
    return ResearchRequest(**data)


async def test_real_a2a_round_trip(sources, limits, researcher_llm, feed_http, tracer, researcher_tracer):
    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)
    async with running_researcher(researcher, researcher_tracer) as url:
        # Agent Card is published at the well-known path
        async with httpx.AsyncClient() as http:
            card = (await http.get(f"{url}/.well-known/agent-card.json")).json()
        assert card["name"] == "AI News Researcher"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"

        client = ResearcherClient(url, tracer, timeout_seconds=30)
        request = _request(exclude_urls=["https://openai.com/index/incident-triage"])
        result, task_id = await client.research(request)

    assert task_id and result.request_id == request.request_id and result.trace_id == request.trace_id
    assert result.collected_at == NOW and result.period_hours == 72
    urls = [a.url for a in result.articles]
    assert "https://openai.com/index/agent-tooling" in urls
    assert "https://openai.com/index/incident-triage" not in urls  # already delivered -> excluded
    assert "https://aws.amazon.com/about-aws/whats-new/2026/10/bedrock-agentcore-mcp" in urls
    assert not any("ec2-r8gd" in u for u in urls)  # AWS feed filtered by interests
    assert len(urls) == len(set(urls))
    assert all(a.relevance_reason for a in result.articles)
    assert result.incomplete is False

    editor_events = read_events([tracer.path])
    researcher_events = read_events([researcher_tracer.path])
    kinds = {(e["kind"], e["status"]) for e in editor_events}
    assert ("a2a_discovery", "completed") in kinds and ("a2a_send", "completed") in kinds
    assert ("a2a_result", "completed") in kinds
    send = next(e for e in editor_events if e["kind"] == "a2a_send" and e["status"] == "completed")
    assert send["data"]["task_id"] == task_id and send["data"]["task_state"] == "TASK_STATE_COMPLETED"
    # both processes trace under the same trace_id, including the researcher's LLM and RSS tool calls
    assert {e["trace_id"] for e in researcher_events} == {request.trace_id}
    rkinds = {(e["kind"], e["name"]) for e in researcher_events}
    assert ("tool_call", "fetch_rss") in rkinds and ("tool_call", "submit_selection") in rkinds
    assert any(e["kind"] == "llm_call" for e in researcher_events)
    assert any(e["kind"] == "a2a_task" and e["data"].get("task_id") == task_id for e in researcher_events)


async def test_researcher_unavailable_is_retried_then_reported(tracer):
    port = _free_port()  # nothing listens here
    client = ResearcherClient(f"http://127.0.0.1:{port}", tracer, timeout_seconds=5, max_attempts=2,
                              backoff_seconds=0.01)
    with pytest.raises(ResearcherUnavailable, match="unreachable"):
        await client.research(_request(trace_id="tr-down-0001"))
    retries = [e for e in read_events([tracer.path]) if e["kind"] == "a2a_retry"]
    assert [e["data"]["attempt"] for e in retries] == [1, 2]
    assert all(e["data"]["transient"] for e in retries)


async def test_failed_researcher_task_is_not_retried(sources, limits, feed_http, tracer, researcher_tracer):
    def broken_llm(system, messages, tools):
        raise RuntimeError("simulated model outage")

    researcher = Researcher(llm=ScriptedBackend(broken_llm), sources=sources, limits=limits,
                            tracer=researcher_tracer, http_client=feed_http)
    async with running_researcher(researcher, researcher_tracer) as url:
        client = ResearcherClient(url, tracer, timeout_seconds=30, max_attempts=2, backoff_seconds=0.01)
        with pytest.raises(ResearcherUnavailable, match="TASK_STATE_FAILED"):
            await client.research(_request(trace_id="tr-fail-0001"))
    editor_events = read_events([tracer.path])
    assert not [e for e in editor_events if e["kind"] == "a2a_retry"]
    task_status = next(e for e in editor_events if e["kind"] == "a2a_task")
    assert task_status["status"] == "failed" and task_status["data"]["task_state"] == "TASK_STATE_FAILED"
    assert any(e["kind"] == "run" and e["status"] == "failed" for e in read_events([researcher_tracer.path]))


async def test_invalid_request_is_rejected_by_server(sources, limits, researcher_llm, feed_http, researcher_tracer):
    from a2a.client import ClientConfig, create_client
    from a2a.helpers import new_data_part, new_message
    from a2a.types import Role, SendMessageRequest, TaskState

    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)
    async with running_researcher(researcher, researcher_tracer) as url:
        client = await create_client(url, client_config=ClientConfig(streaming=False))
        msg = new_message([new_data_part({"period_hours": 9999})], role=Role.ROLE_USER)
        events = [e async for e in client.send_message(SendMessageRequest(message=msg))]
        await client.close()
    assert events[-1].task.status.state == TaskState.TASK_STATE_REJECTED
