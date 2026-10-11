"""Researcher model loop executed by the OpenClaw `researcher` agent.

The agent's instructions live in its OpenClaw workspace (openclaw/workspace/researcher/AGENTS.md);
OpenClaw calls the model and chooses the tool. This service only executes the two client tools of
its own registry (fetch_rss, submit_selection) and returns the results to the same OpenClaw
session. Every A2A task gets its own session (`research-<request_id>`), so tasks never share a
transcript, and the Researcher never sees the Coordinator's session, memory or tools.

The loop ends on a successful submit_selection, and also - with an exception that fails the A2A
task - when the Gateway or the model fails, the model answers without a tool call, or the call /
tool budgets are exhausted. There is no fallback to another model or backend.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ai_news_digest.agents.runtime import (
    AgentLimitError,
    AgentLLMError,
    AgentRunResult,
    AgentSpec,
    ToolInputError,
    ToolPermissionError,
)
from ai_news_digest.openclaw_client import GatewayResponse, NoStructuredAction, OpenClawClient
from ai_news_digest.tracing import Tracer

ROLE = "researcher"


def session_key(request_id: str) -> str:
    return f"research-{request_id}"


def gateway_tools(spec: AgentSpec) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": d["name"], "description": d["description"],
                                              "parameters": d["input_schema"]}}
            for d in spec.registry.definitions()]


async def run_via_openclaw(spec: AgentSpec, *, client: OpenClawClient, tracer: Tracer, trace_id: str,
                           request_id: str, user_content: str, ctx: Any) -> AgentRunResult:
    key = session_key(request_id)
    tools = gateway_tools(spec)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    result = AgentRunResult(text="")
    for call_no in range(1, spec.max_iterations + 1):
        result.iterations = call_no
        with tracer.span(trace_id, agent=ROLE, kind="openclaw_call", name=f"gateway /v1/chat/completions #{call_no}",
                         input=user_content if call_no == 1 else f"{len(messages)} follow-up messages",
                         data={"openclaw_session_key": key, "openclaw_agent": client.model, "tool_choice": "required",
                               "client_tools": spec.registry.names}) as span:
            try:
                response: GatewayResponse = await client.chat(session_key=key, messages=messages, tools=tools,
                                                              tool_choice="required")
            except NoStructuredAction as exc:
                raise AgentLLMError(f"researcher model answered without a tool call ({exc})") from exc
            span.data.update({"openclaw_run_id": response.run_id, "finish_reason": response.finish_reason,
                              "requested_tools": [c.name for c in response.tool_calls],
                              "gateway_usage": response.usage})
            span.set_output(response.content or f"tool_calls: {[c.name for c in response.tool_calls]}")
        _accumulate(result.usage, response.usage)
        if not response.tool_calls:
            raise AgentLLMError("researcher model answered with free text instead of submit_selection")

        followup: list[dict[str, Any]] = [{
            "role": "assistant", "content": response.content or "",
            "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_raw}}
                           for c in response.tool_calls]}]
        terminal: tuple[str, Any] | None = None
        for call in response.tool_calls:
            content, output, ok = await _execute(spec, call, ctx, tracer, trace_id, response.run_id, result)
            followup.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if ok and terminal is None and spec.registry.get(call.name).terminal:
                terminal = (call.name, output)
        if terminal is not None:
            # The session is single-use (one A2A task), so the loop is not closed with another model call.
            result.terminal_tool, result.terminal_output = terminal
            return result
        messages = followup
    raise AgentLimitError(f"researcher did not submit a selection within {spec.max_iterations} Gateway calls "
                          f"({result.tool_calls} tool calls)")


async def _execute(spec: AgentSpec, call, ctx: Any, tracer: Tracer, trace_id: str, run_id: str,
                   result: AgentRunResult) -> tuple[str, Any, bool]:
    data = {"openclaw_run_id": run_id, "tool_call_id": call.id, "executed_by": "researcher-service"}
    started = time.perf_counter()

    def event(status: str, error: str | None = None, output: Any = None, extra: dict[str, Any] | None = None) -> None:
        tracer.event(trace_id, agent=ROLE, kind="tool_call", name=call.name, status=status, input=call.arguments_raw,
                     output=output, error=error, duration_ms=(time.perf_counter() - started) * 1000,
                     data=data | (extra or {}))

    if result.tool_calls >= spec.max_tool_calls:
        event("denied", "tool call budget exhausted", extra={"limit": spec.max_tool_calls})
        return f"ERROR: tool call budget ({spec.max_tool_calls}) exhausted", None, False
    result.tool_calls += 1
    try:
        args = json.loads(call.arguments_raw or "{}")
        output = await spec.registry.dispatch(call.name, args, ctx)
    except ToolPermissionError as exc:
        event("denied", str(exc), extra={"allowed": spec.registry.names})
        return f"ERROR: {exc}", None, False
    except (ToolInputError, ValueError) as exc:  # includes invalid JSON arguments
        event("failed", str(exc))
        return f"ERROR: {exc}", None, False
    event("completed", output=output)
    return (output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)), output, True


def _accumulate(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
