import json

import pytest

from ai_news_digest.agents.llm import ScriptedBackend, fake_response, tool_use
from ai_news_digest.agents.runtime import AgentLimitError, AgentSpec, ToolPermissionError, run_agent
from ai_news_digest.editor.agent import build_registry as editor_registry
from ai_news_digest.researcher.agent import build_registry as researcher_registry
from ai_news_digest.tracing import read_events


def test_registries_are_disjoint_and_minimal():
    editor = set(editor_registry().names)
    researcher = set(researcher_registry().names)
    assert researcher == {"fetch_rss", "submit_selection"}
    assert editor == {"get_user_settings", "update_user_settings", "get_digest_history",
                      "show_last_digest", "request_research", "submit_digest"}
    assert not editor & researcher
    assert not any("rss" in n or "fetch" in n or "web" in n for n in editor)
    assert not any("telegram" in n or "settings" in n or "memory" in n for n in researcher)


async def test_dispatch_rejects_other_roles_tools():
    with pytest.raises(ToolPermissionError):
        await editor_registry().dispatch("fetch_rss", {"source_id": "openai_news"}, ctx=None)
    with pytest.raises(ToolPermissionError):
        await researcher_registry().dispatch("update_user_settings", {"max_items": 10}, ctx=None)
    with pytest.raises(ToolPermissionError):
        await researcher_registry().dispatch("request_research", {}, ctx=None)


async def test_loop_denies_forbidden_tool_and_traces_it(tracer):
    calls = []

    def script(system, messages, tools):
        calls.append(list(messages))
        if len(calls) == 1:
            return fake_response(tool_use("update_user_settings", {"max_items": 10}))
        return fake_response({"type": "text", "text": "done"})

    spec = AgentSpec(role="researcher", system_prompt="s", registry=researcher_registry())
    result = await run_agent(spec, llm=ScriptedBackend(script), tracer=tracer, trace_id="tr-perm",
                             user_content="x", ctx=None)
    assert result.text == "done"
    denied = calls[1][-1]["content"][0]
    assert denied["is_error"] and "not allowed for role 'researcher'" in denied["content"]
    events = read_events([tracer.path])
    assert any(e["kind"] == "tool_call" and e["status"] == "denied" and e["name"] == "update_user_settings"
               for e in events)


async def test_iteration_and_tool_call_limits(tracer):
    def endless(system, messages, tools):
        return fake_response(*[tool_use("get_user_settings", {}) for _ in range(5)])

    class Ctx:
        memory = None

    async def fake_handler(args, ctx):
        return {"ok": True}

    from ai_news_digest.agents.runtime import ToolRegistry, ToolSpec

    reg = ToolRegistry("editor", [ToolSpec("get_user_settings", "d", {"type": "object", "properties": {}},
                                           fake_handler)])
    spec = AgentSpec(role="editor", system_prompt="s", registry=reg, max_iterations=4, max_tool_calls=8)
    llm = ScriptedBackend(endless)
    with pytest.raises(AgentLimitError):
        await run_agent(spec, llm=llm, tracer=tracer, trace_id="tr-limit", user_content="x", ctx=Ctx())
    assert len(llm.calls) == 4
    events = read_events([tracer.path])
    executed = [e for e in events if e["kind"] == "tool_call" and e["status"] == "completed"]
    denied = [e for e in events if e["kind"] == "tool_call" and e["status"] == "denied"]
    assert len(executed) == 8 and len(denied) == 12
    # every LLM request only advertises the role's tools
    assert all({t["name"] for t in c["tools"]} == {"get_user_settings"} for c in llm.calls)


def test_trace_redacts_secrets(tracer):
    fake_key = "sk-ant-" + "api03-" + "x" * 30  # assembled at runtime: not a real secret
    fake_bot_token = "123456789:" + "A" * 35
    tracer.event("tr-x", agent="editor", kind="test", name="n", status="completed",
                 input=f"key {fake_key} and {fake_bot_token}",
                 data={"headers": "Authorization: Bearer secret-token-value"})
    raw = tracer.path.read_text()
    assert fake_key not in raw and fake_bot_token not in raw and "secret-token-value" not in raw
    assert json.loads(raw.splitlines()[0])["input"].count("[REDACTED]") == 2
