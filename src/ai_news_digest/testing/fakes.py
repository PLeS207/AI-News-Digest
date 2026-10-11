"""Deterministic fakes for OFFLINE checks. Never used in the normal (live) configuration.

* ``researcher_script`` - fake Researcher LLM (fetch every source, select every item);
* ``decide`` - scripted Coordinator policy shared by:
  - ``FakeGateway``: an httpx transport imitating the OpenClaw Gateway (client-level tests only;
    it does NOT prove the OpenClaw integration);
  - ``fake_provider_app``: an OpenAI-compatible *model provider* plugged into the REAL OpenClaw
    Gateway for the offline integration check (OpenClaw still builds the prompt from its
    workspace, applies tool policy, runs hooks and manages the session).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ai_news_digest.agents.llm import fake_response, tool_use

TERMINAL = {"submit_digest", "reply", "show_last_digest"}
FORBIDDEN_NATIVE_TOOL = "web_fetch"


# ---------------------------------------------------------------- researcher ----------------------
def _tool_results_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_result" and not block.get("is_error"):
                    out.append(json.loads(block["content"]))
    return out


def researcher_script(system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """Fake Researcher LLM: fetch every listed source, then select every returned item."""
    results = _tool_results_anthropic(messages)
    if not results:
        ids = re.findall(r"^- ([a-z0-9_]+):", messages[0]["content"], flags=re.M)
        return fake_response({"type": "text", "text": "Fetching sources."},
                             *[tool_use("fetch_rss", {"source_id": sid}) for sid in ids])
    max_sel = int(re.search(r"Select at most (\d+)", messages[0]["content"]).group(1))
    items = [i for r in results for i in r.get("items", [])][:max_sel]
    return fake_response(tool_use("submit_selection", {
        "selected": [{"item_id": i["item_id"], "relevance_reason": f"matches interests: {i['title'][:40]}"}
                     for i in items],
    }))


# ---------------------------------------------------------------- coordinator policy ---------------
def _parse(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def decide(user_text: str, results: list[tuple[str, str]]) -> dict[str, Any]:
    """Scripted Coordinator: returns {"tool_calls": [(name, args)]} or {"text": "..."}."""
    t = user_text.lower()
    if any(name in TERMINAL and not str(content).startswith("ERROR") for name, content in results):
        return {"text": "Готово."}  # closing turn after a terminal action
    done = {name for name, _ in results}
    last_of = {name: _parse(content) for name, content in results}
    errors = [c for _, c in results if str(c).startswith("ERROR") or "not found" in str(c)]

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"tool_calls": [(name, args)]}

    if "только текст" in t:
        return {"text": "Вот новости без исследования: всё отлично."}
    if "запрещ" in t:
        if FORBIDDEN_NATIVE_TOOL not in done:
            return call(FORBIDDEN_NATIVE_TOOL, {"url": "https://example.com/feed"})
        return call("reply", {"text": "У меня нет доступа к этому инструменту.", "kind": "info"})
    if "запомни" in t:
        if "update_user_settings" not in done:
            return call("update_user_settings", {"interests": ["AI agents", "MCP", "AWS Bedrock"],
                                                 "language": "ru", "max_items": 2})
        return call("reply", {"text": "Запомнил: максимум 2 новости.", "kind": "confirmation"})
    if "настройки" in t:
        settings = last_of.get("get_user_settings")
        if not isinstance(settings, dict):
            return call("get_user_settings", {})
        return call("reply", {"text": f"Лимит: {settings['max_items']}, интересы: {', '.join(settings['interests'])}",
                              "kind": "settings"})
    if "последний" in t:
        return call("show_last_digest", {})
    if "дайджест" in t or "digest" in t:
        if "ответь текстом" in t and not errors:
            return call("reply", {"text": "Дайджест: всё спокойно.", "kind": "info"})
        research = last_of.get("request_research")
        if research is None:
            return call("request_research", {})
        if not isinstance(research, dict) or "error" in research:
            return {"text": "Researcher недоступен."}
        if "выдумай" in t and not errors:
            return call("submit_digest", {"items": [{"url": "https://example.com/made-up", "summary": "x",
                                                      "application": "y"}]})
        items = [{"url": a["url"], "summary": f"Кратко: {a['title']}. Основано на RSS-анонсе.",
                  "application": "Можно оценить для платформенной команды."}
                 for a in research["articles"][: research["max_items"]]]
        return call("submit_digest", {"intro": "Свежие новости", "items": items})
    return call("reply", {"text": "Готов помочь с дайджестом.", "kind": "help"})


# ---------------------------------------------------------------- fake gateway (client tests) ------
class FakeGateway:
    """Imitates the OpenClaw Gateway HTTP contract for client-level tests (not an integration)."""

    def __init__(self, token: str = "test-token", *, fail_status: int | None = None, agent_id: str = "editor"):
        self.token = token
        self.fail_status = fail_status
        self.agent_id = agent_id
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True, "status": "live"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": {"type": "unauthorized", "message": "bad token"}})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "openclaw"}, {"id": f"openclaw/{self.agent_id}"}]})
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"type": "api_error", "message": "agent run failed"}})
        body = json.loads(request.content)
        session_key = request.headers.get("x-openclaw-session-key", "")
        self.requests.append({"session_key": session_key, "body": body})
        assert body["model"] == f"openclaw/{self.agent_id}"
        session = self.sessions.setdefault(session_key, {"user_text": "", "results": [], "turns": 0})
        msgs = body["messages"]
        id_to_name: dict[str, str] = {}
        for m in msgs:
            if m["role"] == "user":
                text = m["content"]
                match = re.search(r"<user_message>(.*)</user_message>", text, re.S)
                session.update(user_text=match.group(1) if match else text, results=[])
                session["turns"] += 1
            elif m["role"] == "assistant":
                for c in m.get("tool_calls") or []:
                    id_to_name[c["id"]] = c["function"]["name"]
            elif m["role"] == "tool":
                session["results"].append((id_to_name.get(m["tool_call_id"], "?"), m["content"]))
        decision = decide(session["user_text"], session["results"])
        tool_choice = body.get("tool_choice", "auto")
        run_id = f"chatcmpl_{uuid.uuid4()}"
        pinned = tool_choice.get("function", {}).get("name") if isinstance(tool_choice, dict) else None
        if pinned and pinned not in [n for n, _ in decision.get("tool_calls", [])]:
            # OpenClaw exposes only the pinned tool; a model that wants something else fails the constraint
            return httpx.Response(502, json={"error": {"type": "api_error", "message":
                                  f"tool_choice required a {pinned} tool call, but the agent did not produce one"}})
        if "tool_calls" in decision and tool_choice != "none":
            calls = [{"id": uuid.uuid4().hex[:9], "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                     for n, a in decision["tool_calls"]]
            message = {"role": "assistant", "content": "", "tool_calls": calls}
            finish = "tool_calls"
        else:
            if tool_choice == "required":
                return httpx.Response(502, json={"error": {"type": "api_error",
                                      "message": "tool_choice=required was not satisfied by the agent response"}})
            message, finish = {"role": "assistant", "content": decision.get("text", "ok")}, "stop"
        return httpx.Response(200, json={"id": run_id, "object": "chat.completion", "model": body["model"],
                                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


# ---------------------------------------------------------------- fake provider for real OpenClaw ---
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def parse_openclaw_prompt(messages: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str]]]:
    """Recover (user request, tool results of the current turn) from the provider request OpenClaw sends.

    OpenClaw folds client-tool follow-ups into a user message:
    'Assistant: tool_call id=X name=Y arguments=…' and 'Tool:X: <result>'.
    Native tool results (e.g. a denied tool) arrive as role=tool messages.
    """
    start = 0
    for i, m in enumerate(messages):
        if m.get("role") == "user" and "<user_message>" in _text_of(m.get("content")):
            start = i
    turn = messages[start:]
    first = _text_of(turn[0].get("content")) if turn else ""
    match = re.search(r"<user_message>(.*?)</user_message>", first, re.S)
    user_text = match.group(1) if match else first
    names: dict[str, str] = {}
    results: list[tuple[str, str]] = []
    for m in turn:
        role, text = m.get("role"), _text_of(m.get("content"))
        if role == "assistant":
            for c in m.get("tool_calls") or []:
                names[c["id"]] = c["function"]["name"]
        elif role == "tool":
            if '"status": "pending"' in text or "delegated to client" in text:
                continue  # OpenClaw's placeholder for a client tool awaiting the app
            results.append((names.get(m.get("tool_call_id"), "?"), text))
        elif role == "user" and m is not turn[0]:
            for tid, name in re.findall(r"tool_call id=(\S+) name=(\S+)", text):
                names[tid] = name
            for tid, payload in re.findall(r"^Tool:([^:\s]+): (.*)$", text, flags=re.M):
                results.append((names.get(tid, "?"), payload))
    return user_text, results


def fake_provider_app(log_path: str | Path, feeds_dir: str | Path):
    """Starlette app: OpenAI-compatible chat completions (stream + non-stream) and fixture feeds."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.routing import Route

    log_path, feeds_dir = Path(log_path), Path(feeds_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    async def chat(request):
        body = await request.json()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "body": body}, ensure_ascii=False) + "\n")
        user_text, results = parse_openclaw_prompt(body.get("messages", []))
        decision = decide(user_text, results)
        offered = {t["function"]["name"] for t in body.get("tools", []) if "function" in t}
        cid = f"fake-{uuid.uuid4().hex[:8]}"
        if "tool_calls" in decision:
            calls = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                     for n, a in decision["tool_calls"]]
            # a model may hallucinate a native tool; OpenClaw must refuse it if it is not offered
            message, finish = {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
            if not all(c["function"]["name"] in offered or c["function"]["name"] == FORBIDDEN_NATIVE_TOOL
                       for c in calls):
                message, finish = {"role": "assistant", "content": "tool not offered"}, "stop"
        else:
            message, finish = {"role": "assistant", "content": decision["text"]}, "stop"
        usage = {"prompt_tokens": sum(len(_text_of(m.get("content"))) for m in body.get("messages", [])) // 4,
                 "completion_tokens": 20, "total_tokens": 0}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        if not body.get("stream"):
            return JSONResponse({"id": cid, "object": "chat.completion", "created": int(time.time()),
                                 "model": body.get("model"), "usage": usage,
                                 "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

        def sse():
            delta: dict[str, Any] = {"role": "assistant"}
            if message.get("tool_calls"):
                delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(message["tool_calls"])]
            else:
                delta["content"] = message["content"]
            base = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
            yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                                         "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    async def models(request):
        return JSONResponse({"object": "list", "data": [{"id": "scripted-coordinator", "object": "model"}]})

    async def feed(request):
        name = request.path_params["name"]
        path = feeds_dir / name
        if name == "broken.xml":
            return PlainTextResponse("unavailable", status_code=503)
        if not path.is_file() or path.parent != feeds_dir:
            return PlainTextResponse("not found", status_code=404)
        return Response(path.read_bytes(), media_type="application/rss+xml")

    return Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/v1/models", models),
                             Route("/feeds/{name}", feed)])
