"""One Telegram request = one Coordinator turn executed by the OpenClaw `editor` agent.

Flow: app builds the request context (trace id, saved settings, routed intent) -> OpenClaw Gateway
runs the agent (instructions from its workspace, model call, tool choice) -> the app executes the
returned client tool calls -> results go back to the same OpenClaw session -> a terminal action
(submit_digest / reply / show_last_digest) is delivered by the app -> the tool loop is closed in
the OpenClaw session so the next request starts from a consistent transcript.

There is no fallback: if OpenClaw fails, the user gets an error and the trace is marked failed.
A free-text answer without a confirmed action is never delivered as a result.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ai_news_digest.config import SourceConfig
from ai_news_digest.coordinator.delivery import SendFn, deliver_digest, deliver_text, resend_last_digest
from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.openclaw_client import (
    GatewayResponse,
    NoStructuredAction,
    OpenClawClient,
    OpenClawUnavailable,
)
from ai_news_digest.coordinator.tools import (
    CoordinatorToolRegistry,
    ToolContext,
    ToolNotAllowed,
    ToolRejected,
    TurnState,
    route_intent,
)
from ai_news_digest.models import Digest, ResearchRequest, ResearchResult
from ai_news_digest.tracing import Tracer, new_id

AGENT = "app"
MAX_GATEWAY_CALLS = 5
MAX_TOOL_CALLS = 8


@dataclass
class TurnResult:
    trace_id: str
    kind: str  # "digest" | "reply" | "resend_last" | "error"
    text: str | None = None
    digest: Digest | None = None
    failed: bool = False
    session_key: str | None = None
    openclaw_run_ids: list[str] = field(default_factory=list)
    pending_closure: list[dict[str, Any]] | None = None


class CoordinatorService:
    def __init__(self, *, openclaw: OpenClawClient, memory: MemoryStore, tracer: Tracer,
                 research: Callable[[ResearchRequest], Awaitable[tuple[ResearchResult, str]]],
                 sources: list[SourceConfig]):
        self.openclaw = openclaw
        self.memory = memory
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self.tracer = tracer
        self.registry = CoordinatorToolRegistry(ToolContext(memory=memory, tracer=tracer, research=research,
                                                            sources={s.id: s for s in sources}))

    def chat_lock(self, chat_key: str) -> asyncio.Lock:
        """One turn (handle + deliver + loop closure) per chat at a time; different chats run in parallel,
        each in its own OpenClaw session, and every turn has its own TurnState and trace id."""
        return self._chat_locks.setdefault(chat_key, asyncio.Lock())

    # ---- sessions -------------------------------------------------------------------------------
    def session_key(self, chat_key: str) -> str:
        return f"digest-{self.memory.current_conversation(chat_key)}"

    def new_conversation(self, chat_key: str) -> str:
        conversation_id = self.memory.new_conversation(chat_key)
        self.tracer.event(new_id("tr_"), agent=AGENT, kind="session", name="/new", status="completed",
                          output="new OpenClaw session; persistent memory (SQLite) kept",
                          data={"openclaw_session_key": f"digest-{conversation_id}"})
        return conversation_id

    # ---- one request ------------------------------------------------------------------------------
    async def handle(self, text: str, chat_key: str, *, command: str | None = None) -> TurnResult:
        trace_id = new_id("tr_")
        session_key = self.session_key(chat_key)
        state = TurnState(trace_id=trace_id, chat_key=chat_key, intent=route_intent(text, command))
        turn = TurnResult(trace_id=trace_id, kind="error", session_key=session_key)
        with self.tracer.span(trace_id, agent=AGENT, kind="run", name="telegram_message", input=text,
                              data={"intent": state.intent, "command": command, "openclaw_session_key": session_key,
                                    "openclaw_agent": self.openclaw.model}) as run:
            settings, stored = self.memory.get_settings()
            self.tracer.event(trace_id, agent=AGENT, kind="memory_read", name="settings", status="completed",
                              output={"source": "sqlite" if stored else "defaults", **settings.model_dump(mode="json")})
            messages: list[dict[str, Any]] = [{"role": "user", "content": self._first_message(text, settings, state)}]
            tools = self.registry.definitions()
            tool_choice: Any = ({"type": "function", "function": {"name": "request_research"}}
                                if state.intent == "digest" else "required")
            error: str | None = None
            for call_no in range(1, MAX_GATEWAY_CALLS + 1):
                try:
                    response = await self._gateway_call(trace_id, session_key, messages, tools, tool_choice, call_no)
                except NoStructuredAction as exc:
                    error = f"coordinator returned no structured action ({exc})"
                    self.tracer.event(trace_id, agent=AGENT, kind="action_check", name="no_structured_action",
                                      status="failed", error=error)
                    break
                except OpenClawUnavailable as exc:
                    error = f"openclaw: {exc}"
                    break
                turn.openclaw_run_ids.append(response.run_id)
                if not response.tool_calls:
                    error = "coordinator returned free text without a confirmed action"
                    self.tracer.event(trace_id, agent=AGENT, kind="action_check", name="no_structured_action",
                                      status="failed", output=response.content, error=error,
                                      data={"openclaw_run_id": response.run_id})
                    break
                followup = [self._assistant_message(response)]
                for call in response.tool_calls:
                    followup.append(await self._execute(call, state, response.run_id))
                if state.outcome or state.research_error:
                    turn.pending_closure = followup
                    break
                messages = followup
                tool_choice = "required"
            else:
                error = f"no terminal action within {MAX_GATEWAY_CALLS} Gateway calls"

            if state.research_error:
                turn.kind, turn.failed = "error", True
                turn.text = _researcher_error_text(state.research_error)
                run.fail(f"researcher unavailable: {state.research_error}")
            elif state.outcome == "digest":
                turn.kind, turn.digest = "digest", state.digest
                run.set_output(f"digest with {len(state.digest.items)} items")
            elif state.outcome == "resend_last":
                turn.kind = "resend_last"
                run.set_output("resend last digest")
            elif state.outcome == "reply":
                turn.kind, turn.text = "reply", state.reply_text
                run.set_output(state.reply_text)
            else:
                turn.kind, turn.failed = "error", True
                error = error or "no confirmed action"
                turn.text = _coordinator_error_text(error, state.digest_flow)
                run.fail(error)
            run.data.update({"openclaw_run_ids": turn.openclaw_run_ids, "tool_calls": state.tool_calls,
                             "a2a_task_id": state.research_task_id, "outcome": state.outcome})
        return turn

    def _first_message(self, text: str, settings, state: TurnState) -> str:
        intent = ('\n<routed_intent>digest: this request must produce a NEW digest via request_research and '
                  'submit_digest</routed_intent>') if state.intent == "digest" else ""
        return (f"<saved_settings source=\"sqlite\">{json.dumps(settings.model_dump(mode='json'), ensure_ascii=False)}"
                f"</saved_settings>\n<now>{datetime.now(timezone.utc).isoformat(timespec='minutes')}</now>{intent}\n"
                f"<user_message>{text[:2000]}</user_message>")

    @staticmethod
    def _assistant_message(response: GatewayResponse) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments_raw}}
                               for c in response.tool_calls]}

    async def _gateway_call(self, trace_id: str, session_key: str, messages: list[dict[str, Any]],
                            tools: list[dict[str, Any]], tool_choice: Any, call_no: int) -> GatewayResponse:
        with self.tracer.span(trace_id, agent=AGENT, kind="openclaw_call", name=f"gateway /v1/chat/completions #{call_no}",
                              input=messages[-1].get("content") if call_no == 1 else f"{len(messages)} follow-up messages",
                              data={"openclaw_session_key": session_key, "openclaw_agent": self.openclaw.model,
                                    "tool_choice": tool_choice, "client_tools": [t["function"]["name"] for t in tools]}) as span:
            response = await self.openclaw.chat(session_key=session_key, messages=messages, tools=tools,
                                                tool_choice=tool_choice)
            span.data.update({"openclaw_run_id": response.run_id, "finish_reason": response.finish_reason,
                              "requested_tools": [c.name for c in response.tool_calls],
                              "gateway_usage": response.usage})
            span.set_output(response.content or f"tool_calls: {[c.name for c in response.tool_calls]}")
        return response

    async def _execute(self, call, state: TurnState, run_id: str) -> dict[str, Any]:
        data = {"openclaw_run_id": run_id, "tool_call_id": call.id, "executed_by": "app"}
        if state.tool_calls >= MAX_TOOL_CALLS:
            self.tracer.event(state.trace_id, agent=AGENT, kind="tool_call", name=call.name, status="denied",
                              input=call.arguments_raw, error="tool call budget exhausted", data=data)
            return {"role": "tool", "tool_call_id": call.id, "content": "ERROR: tool call budget exhausted"}
        state.tool_calls += 1
        with self.tracer.span(state.trace_id, agent=AGENT, kind="tool_call", name=call.name,
                              input=call.arguments_raw, data=data) as span:
            try:
                content, _ = await self.registry.dispatch(call.name, call.arguments_raw, state)
                span.set_output(content)
                if call.name == "request_research" and state.research_error:
                    span.fail(f"researcher unavailable: {state.research_error}")
            except ToolNotAllowed as exc:
                span.fail(str(exc), status="denied")
                content = f"ERROR: {exc}"
            except ToolRejected as exc:
                span.fail(str(exc), status="rejected")
                content = f"ERROR: {exc}"
        return {"role": "tool", "tool_call_id": call.id, "content": content}

    # ---- delivery ------------------------------------------------------------------------------------
    async def deliver(self, turn: TurnResult, send: SendFn) -> dict[str, Any]:
        try:
            if turn.kind == "digest" and turn.digest is not None:
                report = await deliver_digest(turn.digest, send=send, memory=self.memory, tracer=self.tracer,
                                              trace_id=turn.trace_id)
            elif turn.kind == "resend_last":
                report = await resend_last_digest(send=send, memory=self.memory, tracer=self.tracer,
                                                  trace_id=turn.trace_id)
            else:
                report = await deliver_text(turn.text or "…", send=send, tracer=self.tracer, trace_id=turn.trace_id)
        finally:
            await self.close_turn(turn)
        return report

    async def close_turn(self, turn: TurnResult) -> None:
        """Return the terminal tool results to the OpenClaw session (tool_choice=none), so the agent's
        transcript has no dangling client tool call. The model's closing text is traced, not delivered."""
        if not turn.pending_closure or not turn.session_key:
            return
        messages, turn.pending_closure = turn.pending_closure, None
        try:
            with self.tracer.span(turn.trace_id, agent=AGENT, kind="openclaw_call", name="gateway close tool loop",
                                  input=f"{len(messages)} follow-up messages",
                                  data={"openclaw_session_key": turn.session_key, "tool_choice": "none"}) as span:
                response = await self.openclaw.chat(session_key=turn.session_key, messages=messages,
                                                    tools=self.registry.definitions(), tool_choice="none")
                span.data.update({"openclaw_run_id": response.run_id, "delivered_to_user": False})
                span.set_output(response.content)
        except OpenClawUnavailable:
            pass  # already traced as failed; the user has the result


_QUOTA = re.compile(r"usage.?limit|rate.?limit|quota|too many requests|\b429\b|limit (reached|exceeded)", re.I)


# Fixed system messages are English; the digest content itself follows the saved digest language.
def _quota_text(who: str, error: str) -> str:
    return (f"⏳ {who}: the ChatGPT/Codex subscription limit is exhausted or temporarily exceeded. The request was "
            f"not processed and no paid API is used instead. Please retry later.\nDetails: {error[:200]}")


def _researcher_error_text(error: str) -> str:
    if _QUOTA.search(error):
        return _quota_text("Researcher", error)
    return ("❌ The Researcher is unavailable, so no digest was produced. No news is made up in place of the "
            f"research result.\nReason: {error[:300]}\nPlease try again later.")


def _coordinator_error_text(error: str, digest_flow: bool) -> str:
    if _QUOTA.search(error):
        return _quota_text("Coordinator", error)
    if error.startswith("openclaw:"):
        return "❌ The Coordinator (OpenClaw) is unavailable; the request was not processed. " + error[9:][:300]
    if digest_flow:
        return ("❌ No digest was created: the Coordinator did not complete the verified flow "
                "(request_research → submit_digest). ") + error[:200]
    return "❌ Could not process the request: " + error[:300]
