"""Client for the OpenClaw Gateway OpenAI-compatible endpoint (POST /v1/chat/completions).

Contract (OpenClaw 2026.9.9, docs/gateway/openai-http-api.md):
* ``model: "openclaw/<agentId>"`` selects the configured agent (``editor`` = Coordinator, ``researcher`` = Researcher);
* ``x-openclaw-session-key`` routes the run into an application-owned session; with more than one
  agent configured the key must be agent-scoped, so the client sends ``agent:<agentId>:<key>``;
* client function ``tools`` + ``tool_choice``; the agent answers with ``finish_reason="tool_calls"``
  and the client continues the same loop with the assistant tool-call message plus ``role:"tool"``
  results (only the new messages: the session already holds the earlier transcript);
* the response ``id`` is the Gateway run id (``chatcmpl_<uuid>``) that plugin hooks see as ``runId``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class OpenClawUnavailable(RuntimeError):
    """The Gateway could not be reached or failed the run (no fallback to any other agent)."""


class NoStructuredAction(RuntimeError):
    """The agent answered without the client tool call that tool_choice required (Gateway HTTP 502)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str

    def arguments(self) -> Any:
        return json.loads(self.arguments_raw or "{}")


@dataclass
class GatewayResponse:
    run_id: str
    content: str
    finish_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_message: dict[str, Any] = field(default_factory=dict)


class OpenClawClient:
    def __init__(self, base_url: str, token: str, *, agent_id: str = "editor", timeout_seconds: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._token = token
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.transport = transport

    @property
    def model(self) -> str:
        return f"openclaw/{self.agent_id}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                                 headers={"Authorization": f"Bearer {self._token}"})

    async def chat(self, *, session_key: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                   tool_choice: Any = "auto") -> GatewayResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        try:
            async with self._client() as http:
                response = await http.post(f"{self.base_url}/v1/chat/completions", json=body,
                                           headers={"x-openclaw-session-key": self.scoped_session_key(session_key)})
        except httpx.TimeoutException as exc:
            raise OpenClawUnavailable("OpenClaw Gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise OpenClawUnavailable(f"OpenClaw Gateway is unreachable ({type(exc).__name__})") from exc
        if response.status_code != 200:
            try:
                err = response.json().get("error", {})
                detail = f"{err.get('type', 'error')}: {err.get('message', '')}"
            except ValueError:
                detail = response.text[:200]
            if response.status_code == 502 and "tool_choice" in detail:
                raise NoStructuredAction(detail[:300])
            raise OpenClawUnavailable(f"OpenClaw Gateway HTTP {response.status_code} ({detail[:300]})")
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [ToolCall(id=c["id"], name=c["function"]["name"], arguments_raw=c["function"].get("arguments") or "{}")
                 for c in message.get("tool_calls") or []]
        return GatewayResponse(run_id=data.get("id", ""), content=message.get("content") or "",
                               finish_reason=choice.get("finish_reason") or "", tool_calls=calls,
                               usage=data.get("usage") or {}, raw_message=message)

    def scoped_session_key(self, key: str) -> str:
        return key if key.startswith("agent:") else f"agent:{self.agent_id}:{key}"

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as http:
            r = await http.get(f"{self.base_url}/healthz")
            r.raise_for_status()
            return r.json()

    async def list_agents(self) -> list[str]:
        async with self._client() as http:
            r = await http.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
