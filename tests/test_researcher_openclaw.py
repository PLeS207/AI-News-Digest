"""Researcher model loop through the OpenClaw `researcher` agent (FAKE Gateway; real A2A over HTTP).

Proves the client side of the contract: per-task sessions, the Researcher's own two tools only,
budgets, and failures without any fallback. The real OpenClaw integration is checked by
scripts/openclaw_integration_check.py.
"""

from __future__ import annotations

import pytest

from ai_news_digest.agents.runtime import AgentLimitError, AgentLLMError
from ai_news_digest.coordinator.researcher_client import ResearcherClient
from ai_news_digest.openclaw_client import OpenClawClient, OpenClawUnavailable
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.testing.fakes import FakeGateway, ScriptedGateway, gateway_tool_call
from ai_news_digest.tracing import read_events
from conftest import GATEWAY_TOKEN
from test_a2a_roundtrip import _request, running_researcher


def _researcher(gateway_transport, sources, limits, feed_http, tracer) -> Researcher:
    client = OpenClawClient("http://openclaw.test:18789", GATEWAY_TOKEN, agent_id="researcher",
                            transport=gateway_transport)
    return Researcher(openclaw=client, sources=sources, limits=limits, tracer=tracer, http_client=feed_http)


async def test_researcher_runs_in_openclaw_agent_over_real_a2a(sources, limits, feed_http, tracer, researcher_tracer):
    gateway = FakeGateway(token=GATEWAY_TOKEN, agent_id="researcher")
    researcher = _researcher(gateway.transport(), sources, limits, feed_http, researcher_tracer)
    async with running_researcher(researcher, researcher_tracer) as url:
        client = ResearcherClient(url, tracer, timeout_seconds=30)
        first, task_id = await client.research(_request())
        second, _ = await client.research(_request(request_id="req-a2a-0002", trace_id="tr-a2a-0002"))

    assert task_id and first.articles and not first.incomplete
    assert {r["body"]["model"] for r in gateway.requests} == {"openclaw/researcher"}
    # one OpenClaw session per A2A task, never the Coordinator's session
    assert {r["session_key"] for r in gateway.requests} == {"agent:researcher:research-req-a2a-0001",
                                                           "agent:researcher:research-req-a2a-0002"}
    offered = {t["function"]["name"] for r in gateway.requests for t in r["body"]["tools"]}
    assert offered == {"fetch_rss", "submit_selection"}
    assert all(r["body"]["tool_choice"] == "required" for r in gateway.requests)
    # follow-ups carry only the assistant tool calls and tool results (the session holds the task)
    follow = [r for r in gateway.requests if r["body"]["messages"][0]["role"] == "assistant"]
    assert follow and all({m["role"] for m in r["body"]["messages"]} == {"assistant", "tool"} for r in follow)

    events = [e for e in read_events([researcher_tracer.path]) if e["trace_id"] == "tr-a2a-0001"]
    calls = [e for e in events if e["kind"] == "openclaw_call" and e["status"] == "completed"]
    assert calls and all(e["data"]["openclaw_run_id"].startswith("chatcmpl_") for e in calls)
    assert all(e["data"]["openclaw_session_key"] == "research-req-a2a-0001" for e in calls)
    tools = [e for e in events if e["kind"] == "tool_call"]
    assert {e["name"] for e in tools} == {"fetch_rss", "submit_selection"}
    assert all(e["data"]["executed_by"] == "researcher-service" for e in tools)
    run = next(e for e in events if e["kind"] == "run" and e["status"] == "completed")
    assert run["data"]["openclaw_session_key"] == "research-req-a2a-0001"
    assert not any(e["kind"] == "llm_call" for e in events)  # no in-process model call


async def test_forbidden_tool_and_invalid_args_are_returned_as_errors(sources, limits, feed_http, researcher_tracer):
    transport = ScriptedGateway(gateway_tool_call("send_telegram", {"text": "x"}), gateway_tool_call("fetch_rss", "{not json"),
                                 gateway_tool_call("submit_selection", {"selected": []}))
    result = await _researcher(transport, sources, limits, feed_http, researcher_tracer).research(_request())
    assert result.articles == []
    tool_msgs = [m for b in transport.bodies[1:] for m in b["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("ERROR: Tool 'send_telegram' is not allowed")
    assert tool_msgs[1]["content"].startswith("ERROR:")
    statuses = {(e["name"], e["status"]) for e in read_events([researcher_tracer.path]) if e["kind"] == "tool_call"}
    assert ("send_telegram", "denied") in statuses and ("fetch_rss", "failed") in statuses


@pytest.mark.parametrize("transport, error", [
    (ScriptedGateway({"role": "assistant", "content": "Here are the news: ..."}), AgentLLMError),
    (ScriptedGateway(status=429), OpenClawUnavailable),
    (ScriptedGateway(*[gateway_tool_call("fetch_rss", {"source_id": "openai_news"})] * 10), AgentLimitError),
])
async def test_loop_ends_with_error_and_no_fallback(transport, error, sources, limits, feed_http, researcher_tracer):
    with pytest.raises(error):
        await _researcher(transport, sources, limits, feed_http, researcher_tracer).research(_request())
    assert len(transport.bodies) <= limits.max_llm_iterations
