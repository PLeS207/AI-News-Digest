import json

import pytest

from ai_news_digest.agents.runtime import AgentLimitError, AgentLLMError, AgentSpec, ToolPermissionError, ToolRegistry, ToolSpec
from ai_news_digest.coordinator.tools import ALLOWED_TOOL_NAMES, CoordinatorToolRegistry, ToolNotAllowed, TurnState
from ai_news_digest.researcher.agent import build_registry as researcher_registry
from ai_news_digest.researcher.openclaw_loop import run_via_openclaw
from ai_news_digest.testing.fakes import ScriptedGateway, gateway_tool_call
from ai_news_digest.tracing import read_events
from conftest import researcher_client


def test_registries_are_disjoint_and_minimal():
    coordinator = set(ALLOWED_TOOL_NAMES)
    researcher = set(researcher_registry().names)
    assert researcher == {"fetch_rss", "submit_selection"}
    assert coordinator == {"get_user_settings", "update_user_settings", "get_digest_history",
                           "show_last_digest", "request_research", "submit_digest", "reply"}
    assert not coordinator & researcher
    assert not any("rss" in n or "fetch" in n or "web" in n for n in coordinator)
    assert not any("telegram" in n or "settings" in n or "memory" in n for n in researcher)


async def test_dispatch_rejects_other_roles_tools():
    reg = CoordinatorToolRegistry(ctx=None)
    state = TurnState(trace_id="tr_perm00001", chat_key="c", intent=None)
    for name in ("fetch_rss", "web_fetch", "exec", "sessions_spawn"):
        with pytest.raises(ToolNotAllowed):
            await reg.dispatch(name, "{}", state)
    with pytest.raises(ToolPermissionError):
        await researcher_registry().dispatch("update_user_settings", {"max_items": 10}, ctx=None)
    with pytest.raises(ToolPermissionError):
        await researcher_registry().dispatch("request_research", {}, ctx=None)


async def test_loop_denies_forbidden_tool_and_traces_it(researcher_tracer):
    """The OpenClaw researcher model asks for a Coordinator tool: the service refuses it and says why."""
    gateway = ScriptedGateway(gateway_tool_call("update_user_settings", {"max_items": 10}),
                              {"role": "assistant", "content": "I cannot change settings."})
    spec = AgentSpec(role="researcher", registry=researcher_registry())
    with pytest.raises(AgentLLMError):  # free text afterwards ends the task as failed
        await run_via_openclaw(spec, client=researcher_client(gateway), tracer=researcher_tracer,
                               trace_id="tr-perm", request_id="req-perm", user_content="x", ctx=None)
    denied = next(m for m in gateway.bodies[1]["messages"] if m["role"] == "tool")
    assert "not allowed for role 'researcher'" in denied["content"]
    events = read_events([researcher_tracer.path])
    assert any(e["kind"] == "tool_call" and e["status"] == "denied" and e["name"] == "update_user_settings"
               for e in events)


async def test_gateway_call_and_tool_call_limits(tracer):
    async def fake_handler(args, ctx):
        return {"ok": True}

    reg = ToolRegistry("researcher", [ToolSpec("fetch_rss", "d", {"type": "object", "properties": {}},
                                               fake_handler)])
    spec = AgentSpec(role="researcher", registry=reg, max_iterations=4, max_tool_calls=8)
    five_calls = {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "fetch_rss", "arguments": "{}"}} for i in range(5)]}
    gateway = ScriptedGateway(*[five_calls] * 10)
    with pytest.raises(AgentLimitError):
        await run_via_openclaw(spec, client=researcher_client(gateway), tracer=tracer, trace_id="tr-limit",
                               request_id="req-limit", user_content="x", ctx=None)
    assert len(gateway.bodies) == 4
    events = read_events([tracer.path])
    executed = [e for e in events if e["kind"] == "tool_call" and e["status"] == "completed"]
    denied = [e for e in events if e["kind"] == "tool_call" and e["status"] == "denied"]
    assert len(executed) == 8 and len(denied) == 12
    # every Gateway request only advertises the role's tools
    assert all({t["function"]["name"] for t in b["tools"]} == {"fetch_rss"} for b in gateway.bodies)


def test_trace_redacts_secrets(tracer):
    fake_key = "sk-ant-" + "api03-" + "x" * 30  # assembled at runtime: not a real secret
    fake_bot_token = "123456789:" + "A" * 35
    tracer.event("tr-x", agent="editor", kind="test", name="n", status="completed",
                 input=f"key {fake_key} and {fake_bot_token}",
                 data={"headers": "Authorization: Bearer secret-token-value"})
    raw = tracer.path.read_text()
    assert fake_key not in raw and fake_bot_token not in raw and "secret-token-value" not in raw
    assert json.loads(raw.splitlines()[0])["input"].count("[REDACTED]") == 2
