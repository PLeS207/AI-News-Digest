"""LLM backends. The real backend uses the official Anthropic Python SDK; tests use scripted fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import anthropic

from ai_news_digest.config import LLMConfig

# Models that accept the server-side refusal fallback in its "default" form on the Claude API.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLMBackend(Protocol):
    model: str

    async def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


class AnthropicBackend:
    """Calls Claude through the official SDK (Messages API, client-side tools)."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.model = cfg.model
        self.client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key, timeout=cfg.timeout_seconds, max_retries=2
        )
        self.use_fallback = cfg.refusal_fallback and cfg.model in _FALLBACK_MODELS

    async def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto"},
            output_config={"effort": self.cfg.effort},
        )
        if self.use_fallback:
            response = await self.client.beta.messages.create(
                betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            response = await self.client.messages.create(**kwargs)
        usage = response.usage.to_dict() if response.usage else {}
        return LLMResponse(
            content=[block.to_dict() for block in response.content],
            stop_reason=response.stop_reason,
            model=response.model,
            usage={k: v for k, v in usage.items() if isinstance(v, (int, float, str)) and v is not None},
            request_id=getattr(response, "_request_id", None),
        )


ScriptFn = Callable[[str, list[dict[str, Any]], list[dict[str, Any]]], Awaitable[LLMResponse] | LLMResponse]


class ScriptedBackend:
    """Deterministic fake LLM for offline tests: a function decides the next response."""

    def __init__(self, script: ScriptFn, model: str = "fake-llm"):
        self.script = script
        self.model = model
        self.calls: list[dict[str, Any]] = []

    async def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        result = self.script(system, messages, tools)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return result  # type: ignore[return-value]


def tool_use(name: str, input: dict[str, Any], id: str | None = None) -> dict[str, Any]:
    import uuid

    return {"type": "tool_use", "id": id or f"toolu_{uuid.uuid4().hex[:12]}", "name": name, "input": input}


def fake_response(*blocks: dict[str, Any], stop_reason: str | None = None) -> LLMResponse:
    has_tools = any(b.get("type") == "tool_use" for b in blocks)
    return LLMResponse(
        content=list(blocks),
        stop_reason=stop_reason or ("tool_use" if has_tools else "end_turn"),
        model="fake-llm",
        usage={"input_tokens": 0, "output_tokens": 0},
    )
