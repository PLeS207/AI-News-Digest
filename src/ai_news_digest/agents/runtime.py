"""Minimal bounded agent loop with per-role tool registries enforced at dispatch time."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ai_news_digest.agents.llm import LLMBackend, LLMResponse
from ai_news_digest.tracing import Tracer, clip


class ToolPermissionError(PermissionError):
    """Raised when an agent requests a tool outside its role's allowlist."""


class ToolInputError(ValueError):
    """Raised by a tool handler for invalid arguments; returned to the model as an error result."""


class AgentLimitError(RuntimeError):
    """Raised when the agent exceeds its LLM iteration budget without finishing."""


class AgentLLMError(RuntimeError):
    """Raised when the model stops for a reason the loop cannot continue from."""


ToolHandler = Callable[[dict[str, Any], Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False  # a successful call ends the loop (result is the agent output)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    """The only tools a role can see or execute. Dispatch re-checks the allowlist."""

    def __init__(self, role: str, tools: list[ToolSpec]):
        self.role = role
        self._tools = {t.name: t for t in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolPermissionError(f"Tool '{name}' is not allowed for role '{self.role}'")
        return self._tools[name]

    async def dispatch(self, name: str, args: dict[str, Any], ctx: Any) -> Any:
        spec = self.get(name)
        if not isinstance(args, dict):
            raise ToolInputError("tool input must be a JSON object")
        return await spec.handler(args, ctx)


@dataclass
class AgentRunResult:
    text: str
    terminal_tool: str | None = None
    terminal_output: Any = None
    iterations: int = 0
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class AgentSpec:
    role: str
    system_prompt: str
    registry: ToolRegistry
    max_iterations: int = 4
    max_tool_calls: int = 8


def _accumulate(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


async def run_agent(
    spec: AgentSpec,
    *,
    llm: LLMBackend,
    tracer: Tracer,
    trace_id: str,
    user_content: str,
    ctx: Any,
    history: list[dict[str, Any]] | None = None,
) -> AgentRunResult:
    messages: list[dict[str, Any]] = list(history or []) + [{"role": "user", "content": user_content}]
    tools = spec.registry.definitions()
    result = AgentRunResult(text="")

    for iteration in range(1, spec.max_iterations + 1):
        result.iterations = iteration
        with tracer.span(
            trace_id, agent=spec.role, kind="llm_call", name=f"{spec.role}.llm#{iteration}",
            input=messages[-1]["content"] if iteration == 1 else f"{len(messages)} messages",
            data={"model": llm.model, "iteration": iteration, "tools": spec.registry.names},
        ) as span:
            response: LLMResponse = await llm.create(system=spec.system_prompt, messages=messages, tools=tools)
            span.data.update({
                "stop_reason": response.stop_reason, "usage": response.usage,
                "response_model": response.model, "request_id": response.request_id,
                "tool_uses": [t["name"] for t in response.tool_uses()],
            })
            span.set_output(response.text() or None)
        _accumulate(result.usage, response.usage)

        if response.stop_reason == "refusal":
            raise AgentLLMError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise AgentLLMError("model hit max_tokens before finishing")

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = response.tool_uses()

        if response.stop_reason == "pause_turn":
            continue
        if not tool_uses:
            result.text = response.text()
            return result

        tool_results: list[dict[str, Any]] = []
        terminal_hit: tuple[str, Any] | None = None
        for call in tool_uses:
            name, args, call_id = call["name"], call.get("input") or {}, call["id"]
            if result.tool_calls >= spec.max_tool_calls:
                tracer.event(trace_id, agent=spec.role, kind="tool_call", name=name, status="denied",
                             input=args, error="tool call budget exhausted",
                             data={"limit": spec.max_tool_calls})
                tool_results.append({"type": "tool_result", "tool_use_id": call_id, "is_error": True,
                                     "content": f"Tool call budget ({spec.max_tool_calls}) exhausted."})
                continue
            result.tool_calls += 1
            started = time.perf_counter()
            try:
                output = await spec.registry.dispatch(name, args, ctx)
            except ToolPermissionError as exc:
                tracer.event(trace_id, agent=spec.role, kind="tool_call", name=name, status="denied",
                             input=args, error=str(exc),
                             duration_ms=(time.perf_counter() - started) * 1000,
                             data={"allowed": spec.registry.names})
                tool_results.append({"type": "tool_result", "tool_use_id": call_id, "is_error": True,
                                     "content": str(exc)})
                continue
            except (ToolInputError, ValueError) as exc:
                tracer.event(trace_id, agent=spec.role, kind="tool_call", name=name, status="failed",
                             input=args, error=str(exc),
                             duration_ms=(time.perf_counter() - started) * 1000)
                tool_results.append({"type": "tool_result", "tool_use_id": call_id, "is_error": True,
                                     "content": f"Error: {exc}"})
                continue
            tracer.event(trace_id, agent=spec.role, kind="tool_call", name=name, status="completed",
                         input=args, output=output, duration_ms=(time.perf_counter() - started) * 1000)
            tool_results.append({"type": "tool_result", "tool_use_id": call_id,
                                 "content": output if isinstance(output, str)
                                 else json.dumps(output, ensure_ascii=False, default=str)})
            if spec.registry.get(name).terminal and terminal_hit is None:
                terminal_hit = (name, output)

        if terminal_hit is not None:
            result.terminal_tool, result.terminal_output = terminal_hit
            result.text = response.text()
            return result
        messages.append({"role": "user", "content": tool_results})

    raise AgentLimitError(
        f"{spec.role} did not finish within {spec.max_iterations} LLM iterations "
        f"({result.tool_calls} tool calls)"
    )


def short(value: Any, limit: int = 300) -> str:
    return clip(value, limit) or ""
