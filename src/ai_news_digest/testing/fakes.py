"""Deterministic fakes for OFFLINE checks. Never used in the normal (live) configuration.

* ``decide_research`` - scripted Researcher policy (fetch every source, select every item);
* ``ScriptedGateway`` - a Gateway transport returning given assistant messages (edge cases);
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


TERMINAL = {"submit_digest", "reply", "show_last_digest"}
FORBIDDEN_NATIVE_TOOL = "web_fetch"


# ---------------------------------------------------------------- researcher ----------------------
def decide_research(task_text: str, results: list[tuple[str, str]]) -> dict[str, Any]:
    """Scripted Researcher for the OpenClaw `researcher` agent: fetch every source, select every item."""
    if any(name == "submit_selection" and not str(c).startswith("ERROR") for name, c in results):
        return {"text": "Selection submitted."}
    fetched = [_parse(c) for name, c in results if name == "fetch_rss"]
    if not fetched:
        ids = re.findall(r"^- ([a-z0-9_]+):", task_text, flags=re.M)
        return {"tool_calls": [("fetch_rss", {"source_id": sid}) for sid in ids]}
    match = re.search(r"Select at most (\d+)", task_text)
    items = [i for r in fetched if isinstance(r, dict) for i in r.get("items", [])][: int(match.group(1)) if match else 10]
    return {"tool_calls": [("submit_selection", {"selected": [
        {"item_id": i["item_id"], "relevance_reason": f"matches interests: {i['title'][:40]}"} for i in items]})]}


# ---------------------------------------------------------------- coordinator policy ---------------
def _parse(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def _has(t: str, *words: str) -> bool:
    return any(w in t for w in words)


def decide(user_text: str, results: list[tuple[str, str]]) -> dict[str, Any]:
    """Scripted Coordinator: returns {"tool_calls": [(name, args)]} or {"text": "..."}.

    Understands English and Russian requests (the real agent is multilingual too)."""
    t = user_text.lower()
    if any(name in TERMINAL and not str(content).startswith("ERROR") for name, content in results):
        return {"text": "Done."}  # closing turn after a terminal action
    done = {name for name, _ in results}
    last_of = {name: _parse(content) for name, content in results}
    errors = [c for _, c in results if str(c).startswith("ERROR") or "not found" in str(c)]

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"tool_calls": [(name, args)]}

    if _has(t, "text only", "только текст"):
        return {"text": "Here is the news without research: all is well."}
    if _has(t, "forbidden", "запрещ"):
        if FORBIDDEN_NATIVE_TOOL not in done:
            return call(FORBIDDEN_NATIVE_TOOL, {"url": "https://example.com/feed"})
        return call("reply", {"text": "I do not have access to that tool.", "kind": "info"})
    if _has(t, "remember", "запомни"):
        if "update_user_settings" not in done:
            limit = int(m.group(1)) if (m := re.search(r"(\d+)", t)) else 2
            return call("update_user_settings", {"interests": ["AI agents", "MCP", "AWS Bedrock"],
                                                 "language": "en", "max_items": limit})
        return call("reply", {"text": "Saved: up to that many news items.", "kind": "confirmation"})
    if _has(t, "settings", "настройки"):
        settings = last_of.get("get_user_settings")
        if not isinstance(settings, dict):
            return call("get_user_settings", {})
        return call("reply", {"text": f"Limit: {settings['max_items']}, interests: {', '.join(settings['interests'])}",
                              "kind": "settings"})
    if _has(t, "last digest", "последний"):
        return call("show_last_digest", {})
    if _has(t, "digest", "дайджест"):
        if _has(t, "answer in text", "ответь текстом") and not errors:
            return call("reply", {"text": "Digest: nothing to report.", "kind": "info"})
        research = last_of.get("request_research")
        if research is None:
            return call("request_research", {})
        if not isinstance(research, dict) or "error" in research:
            return {"text": "The Researcher is unavailable."}
        if _has(t, "invent", "выдумай") and not errors:
            return call("submit_digest", {"items": [{"url": "https://example.com/made-up", "summary": "x",
                                                      "application": "y"}]})
        items = [{"url": a["url"], "summary": f"In short: {a['title']}. Based on the RSS announcement.",
                  "application": "Worth evaluating for the platform team."}
                 for a in research["articles"][: research["max_items"]]]
        return call("submit_digest", {"intro": "Fresh news", "items": items})
    return call("reply", {"text": "Ready to help with your digest.", "kind": "help"})


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
        if not session_key.startswith(f"agent:{self.agent_id}:"):  # real Gateway with several agents: HTTP 500
            return httpx.Response(500, json={"error": {"type": "api_error", "message": "internal error"}})
        self.requests.append({"session_key": session_key, "body": body})
        assert body["model"] == f"openclaw/{self.agent_id}"
        researcher = self.agent_id == "researcher"
        session = self.sessions.setdefault(session_key, {"user_text": "", "results": [], "turns": 0})
        msgs = body["messages"]
        id_to_name: dict[str, str] = {}
        for m in msgs:
            if m["role"] == "user":
                text = m["content"]
                match = None if researcher else re.search(r"<user_message>(.*)</user_message>", text, re.S)
                session.update(user_text=match.group(1) if match else text, results=[])
                session["turns"] += 1
            elif m["role"] == "assistant":
                for c in m.get("tool_calls") or []:
                    id_to_name[c["id"]] = c["function"]["name"]
            elif m["role"] == "tool":
                session["results"].append((id_to_name.get(m["tool_call_id"], "?"), m["content"]))
        decision = (decide_research if researcher else decide)(session["user_text"], session["results"])
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


def parse_openclaw_prompt(messages: list[dict[str, Any]], marker: str = "<user_message>") -> tuple[str, list[tuple[str, str]]]:
    """Recover (user request, tool results of the current turn) from the provider request OpenClaw sends.

    OpenClaw folds client-tool follow-ups into a user message:
    'Assistant: tool_call id=X name=Y arguments=…' and 'Tool:X: <result>'.
    Native tool results (e.g. a denied tool) arrive as role=tool messages.
    """
    start = 0
    for i, m in enumerate(messages):
        if m.get("role") == "user" and marker in _text_of(m.get("content")):
            start = i
    turn = messages[start:]
    first = _text_of(turn[0].get("content")) if turn else ""
    match = re.search(r"<user_message>(.*?)</user_message>", first, re.S) if marker == "<user_message>" else None
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
        if "researcher" in str(body.get("model")):  # OpenClaw `researcher` agent -> fake/scripted-researcher
            task_text, results = parse_openclaw_prompt(body.get("messages", []), marker="Research task")
            decision = decide_research(task_text, results)
        else:
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
        return JSONResponse({"object": "list", "data": [{"id": "scripted-coordinator", "object": "model"},
                                                        {"id": "scripted-researcher", "object": "model"}]})

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


# ---------------------------------------------------------------- scripted gateway (edge cases) -----
class ScriptedGateway(httpx.AsyncBaseTransport):
    """Returns the given assistant messages in order; a non-200 ``status`` fails every call."""

    def __init__(self, *messages: dict[str, Any], status: int = 200, error: str = "usage limit reached"):
        self.messages, self.status, self.error, self.bodies = list(messages), status, error, []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"type": "api_error", "message": self.error}})
        message = self.messages.pop(0) if self.messages else {"role": "assistant", "content": "done"}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return httpx.Response(200, json={"id": f"chatcmpl_{len(self.bodies)}", "choices": [
            {"index": 0, "message": message, "finish_reason": finish}], "usage": {"total_tokens": 7}})


def gateway_tool_call(name: str, args: Any) -> dict[str, Any]:
    """An assistant message requesting one client tool call (``args`` may be raw, even invalid, JSON)."""
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c_{name}", "type": "function", "function": {
        "name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}]}
