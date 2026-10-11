"""Backend tools of the OpenClaw Coordinator, executed by the app.

OpenClaw sees these as client function tools. Every call returned by the Gateway is re-checked
here: allowlist, strict argument schema (unknown fields rejected), and whether the operation is
allowed in the current request state. Tool handlers are bound to the TurnState the app created
for this Telegram message, so the model cannot pick a trace id, a user or another request's
research result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_news_digest.config import SourceConfig
from ai_news_digest.coordinator.memory import MemoryStore, settings_summary
from ai_news_digest.coordinator.researcher_client import ResearcherUnavailable, new_request_id
from ai_news_digest.models import Digest, DigestItem, ResearchRequest, ResearchResult, UserSettings
from ai_news_digest.tracing import Tracer

AGENT = "app"
URL_RE = re.compile(r"https?://|www\.", re.I)

# --- routing --------------------------------------------------------------------------------------
# English and Russian on purpose: users may write in either language (multilingual input support).
_DIGEST_WORDS = re.compile(r"(дайджест|digest|новост|news)", re.I)
_NOT_NEW_DIGEST = re.compile(
    r"((последн|прошл|предыдущ)\w*\s+(дайджест|выпуск)|сохран|настро|запомни|"
    r"\b(last|latest|previous|recent|saved|stored)\s+digest|\bresend|\bre-send|setting|remember)", re.I)


def route_intent(text: str, command: str | None = None) -> str | None:
    """Deterministic pre-routing: '/digest' or an unambiguous request for a new digest -> 'digest'."""
    if command == "digest":
        return "digest"
    if _DIGEST_WORDS.search(text) and not _NOT_NEW_DIGEST.search(text):
        return "digest"
    return None


class ToolRejected(Exception):
    """Returned to the Coordinator as a tool error; the turn continues."""


class ToolNotAllowed(ToolRejected):
    """Tool outside the Coordinator allowlist."""


@dataclass
class TurnState:
    trace_id: str
    chat_key: str
    intent: str | None  # "digest" when the request must produce a digest
    research: ResearchResult | None = None
    research_task_id: str | None = None
    research_error: str | None = None
    research_calls: int = 0
    outcome: str | None = None  # "digest" | "reply" | "resend_last"
    digest: Digest | None = None
    reply_text: str | None = None
    tool_calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def digest_flow(self) -> bool:
        return self.intent == "digest" or self.research_calls > 0


# --- argument models ---------------------------------------------------------------------------
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class UpdateSettingsArgs(_Args):
    interests: list[str] | None = Field(default=None, max_length=15)
    language: str | None = Field(default=None, min_length=2, max_length=10)
    max_items: int | None = Field(default=None, ge=1, le=10)
    period_hours: int | None = Field(default=None, ge=1, le=336)
    add_note: str | None = Field(default=None, max_length=200)
    clear_notes: bool | None = None


class HistoryArgs(_Args):
    limit: int = Field(default=3, ge=1, le=5)


class ResearchArgs(_Args):
    period_hours: int | None = Field(default=None, ge=1, le=336)
    focus: str | None = Field(default=None, max_length=300)


class DigestItemArgs(_Args):
    url: str = Field(max_length=2048)
    summary: str = Field(min_length=1, max_length=900)
    application: str = Field(min_length=1, max_length=500)


class SubmitDigestArgs(_Args):
    items: list[DigestItemArgs] = Field(max_length=10)
    intro: str | None = Field(default=None, max_length=300)
    empty_reason: str | None = Field(default=None, max_length=300)


class ReplyArgs(_Args):
    text: str = Field(min_length=1, max_length=3000)
    kind: Literal["settings", "confirmation", "help", "info", "clarification"]


@dataclass
class ToolContext:
    memory: MemoryStore
    tracer: Tracer
    research: Callable[[ResearchRequest], Awaitable[tuple[ResearchResult, str]]]
    sources: dict[str, SourceConfig]


Handler = Callable[[Any, TurnState, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CoordinatorTool:
    name: str
    description: str
    args: type[_Args]
    handler: Handler
    terminal: bool = False

    def definition(self) -> dict[str, Any]:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": schema}}


# --- handlers ----------------------------------------------------------------------------------------
def _settings_read(state: TurnState, ctx: ToolContext) -> tuple[UserSettings, bool]:
    settings, stored = ctx.memory.get_settings()
    ctx.tracer.event(state.trace_id, agent=AGENT, kind="memory_read", name="settings", status="completed",
                     output=settings_summary(settings, stored))
    return settings, stored


async def _get_user_settings(args: NoArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    settings, stored = _settings_read(state, ctx)
    out = settings_summary(settings, stored)
    out["delivered_urls_count"] = len(ctx.memory.delivered_urls(limit=10_000))
    out["sources"] = list(ctx.sources)
    return out


async def _update_user_settings(args: UpdateSettingsArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    if state.digest_flow:
        raise ToolRejected("settings cannot be changed inside a digest request")
    current, _ = ctx.memory.get_settings()
    data = current.model_dump()
    changed = []
    for key in ("interests", "language", "max_items", "period_hours"):
        value = getattr(args, key)
        if value is not None:
            data[key] = value
            changed.append(key)
    if args.add_note:
        data["notes"] = (data.get("notes") or [])[-9:] + [args.add_note]
        changed.append("notes")
    if args.clear_notes:
        data["notes"] = []
        changed.append("notes")
    if not changed:
        raise ToolRejected("no settings fields provided")
    try:
        new = UserSettings.model_validate(data)
    except ValidationError as exc:
        raise ToolRejected(f"invalid settings: {exc.errors()[0]['msg']}") from exc
    saved = ctx.memory.save_settings(new)
    ctx.tracer.event(state.trace_id, agent=AGENT, kind="memory_write", name="settings", status="completed",
                     input=args.model_dump(exclude_none=True), output=settings_summary(saved, True),
                     data={"changed": changed})
    return {"saved": True, "changed": changed, "settings": settings_summary(saved, True)}


async def _get_digest_history(args: HistoryArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    rows = ctx.memory.last_digests(args.limit)
    ctx.tracer.event(state.trace_id, agent=AGENT, kind="memory_read", name="digest_history", status="completed",
                     output=f"{len(rows)} digests")
    return {"digests": [{k: r[k] for k in ("id", "created_at", "period_hours", "item_count", "delivered_count",
                                           "status")} for r in rows],
            "delivered_urls_count": len(ctx.memory.delivered_urls(limit=10_000))}


async def _show_last_digest(args: NoArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    if state.digest_flow:
        raise ToolRejected("this request asks for a NEW digest: call request_research instead")
    state.outcome = "resend_last"
    last = ctx.memory.last_digest()
    return {"accepted": True, "found": last is not None,
            "note": "The app re-sends the stored digest to the user verbatim."}


async def _request_research(args: ResearchArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    if state.research_calls:
        raise ToolRejected("research was already requested in this request; use its result")
    state.research_calls += 1
    settings, _ = _settings_read(state, ctx)
    exclude = ctx.memory.delivered_urls()
    ctx.tracer.event(state.trace_id, agent=AGENT, kind="memory_read", name="delivered_urls", status="completed",
                     output=f"{len(exclude)} delivered URLs excluded")
    request = ResearchRequest(
        request_id=new_request_id(), trace_id=state.trace_id,  # bound by the app, never by the model
        period_hours=args.period_hours or settings.period_hours, interests=settings.interests,
        max_items=settings.max_items, exclude_urls=exclude, focus=args.focus,
    )
    try:
        result, task_id = await ctx.research(request)
    except ResearcherUnavailable as exc:
        state.research_error = str(exc)
        return {"error": f"Researcher unavailable: {exc}"}
    state.research, state.research_task_id = result, task_id
    return {
        "a2a_task_id": task_id, "period_hours": result.period_hours,
        "collected_at": result.collected_at.isoformat(), "max_items": settings.max_items,
        "language": settings.language, "incomplete": result.incomplete,
        "incomplete_reasons": result.incomplete_reasons,
        "source_errors": [e.model_dump() for e in result.source_errors],
        "articles": [{"url": a.url, "title": a.title, "source_id": a.source_id,
                      "published_at": a.published_at.isoformat(), "available_excerpt": a.available_excerpt,
                      "excerpt_is_empty": a.excerpt_is_empty, "relevance_reason": a.relevance_reason}
                     for a in result.articles],
    }


async def _submit_digest(args: SubmitDigestArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    result = state.research
    if result is None:
        raise ToolRejected("call request_research first: a digest must use the Researcher result of this request")
    settings, _ = _settings_read(state, ctx)
    if len(args.items) > settings.max_items:
        raise ToolRejected(f"too many items: the user's limit is {settings.max_items}")
    by_url = {a.url: a for a in result.articles}
    items: list[DigestItem] = []
    for raw in args.items:
        article = by_url.get(raw.url.strip())
        if article is None:
            raise ToolRejected(f"url not present in this request's Researcher result: {raw.url!r}")
        if any(i.url == article.url for i in items):
            continue
        if ctx.memory.is_delivered(article.url):
            raise ToolRejected(f"url was already delivered earlier: {article.url!r}")
        source = ctx.sources.get(article.source_id)
        items.append(DigestItem(
            source_id=article.source_id, source_name=source.name if source else article.source_id,
            title=article.title, url=article.url, published_at=article.published_at,
            summary=raw.summary.strip(), application=raw.application.strip(),
            excerpt_is_empty=article.excerpt_is_empty,
        ))
    state.digest = Digest(
        trace_id=state.trace_id, language=settings.language, period_hours=result.period_hours,
        intro=(args.intro or "")[:300], items=items,
        empty_reason=args.empty_reason if not items else None,
        research_incomplete=result.incomplete, source_errors=result.source_errors,
    )
    state.outcome = "digest"
    ctx.tracer.event(state.trace_id, agent=AGENT, kind="link_check", name="verify_links", status="completed",
                     output=f"{len(items)} links verified against Researcher result",
                     data={"urls": [i.url for i in items], "a2a_task_id": state.research_task_id})
    return {"accepted": True, "items": len(items), "note": "The app delivers the digest to the user."}


async def _reply(args: ReplyArgs, state: TurnState, ctx: ToolContext) -> dict[str, Any]:
    if state.digest_flow:
        raise ToolRejected("this is a digest request: a free-text reply is not a digest. "
                           "Call request_research and then submit_digest.")
    if URL_RE.search(args.text):
        raise ToolRejected("replies must not contain links; news links are only delivered via submit_digest")
    state.reply_text = args.text
    state.outcome = "reply"
    return {"accepted": True}


TOOLS: list[CoordinatorTool] = [
    CoordinatorTool("get_user_settings", "Read the user's saved settings from persistent memory (SQLite).",
                    NoArgs, _get_user_settings),
    CoordinatorTool("update_user_settings",
                    "Persist settings the user explicitly asked to change. interests replaces the whole list.",
                    UpdateSettingsArgs, _update_user_settings),
    CoordinatorTool("get_digest_history", "List recent digests and delivery statistics.", HistoryArgs,
                    _get_digest_history),
    CoordinatorTool("show_last_digest", "Re-send the last delivered digest verbatim. Ends the request.",
                    NoArgs, _show_last_digest, terminal=True),
    CoordinatorTool("request_research",
                    "Ask the Researcher agent (A2A protocol) for fresh relevant articles. Uses saved interests "
                    "and limit, skips delivered URLs. Required before submit_digest.",
                    ResearchArgs, _request_research),
    CoordinatorTool("submit_digest",
                    "Submit the digest built ONLY from this request's request_research articles (exact urls). "
                    "Ends the request.", SubmitDigestArgs, _submit_digest, terminal=True),
    CoordinatorTool("reply",
                    "Send a short text answer for non-digest requests (settings, confirmations, help). "
                    "No links. Ends the request.", ReplyArgs, _reply, terminal=True),
]
ALLOWED_TOOL_NAMES = [t.name for t in TOOLS]


class CoordinatorToolRegistry:
    def __init__(self, ctx: ToolContext, tools: list[CoordinatorTool] | None = None):
        self.ctx = ctx
        self._tools = {t.name: t for t in (tools or TOOLS)}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    async def dispatch(self, name: str, arguments_raw: str, state: TurnState) -> tuple[str, bool]:
        """Execute one tool call. Returns (tool message content, ok)."""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotAllowed(f"tool '{name}' is not allowed for the Coordinator")
        try:
            raw = json.loads(arguments_raw or "{}")
        except json.JSONDecodeError as exc:
            raise ToolRejected(f"arguments are not valid JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ToolRejected("arguments must be a JSON object")
        try:
            args = tool.args.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ToolRejected(f"invalid arguments: {'.'.join(map(str, first['loc']))}: {first['msg']}") from exc
        if state.outcome is not None:
            raise ToolRejected("this request is already completed")
        result = await tool.handler(args, state, self.ctx)
        return json.dumps(result, ensure_ascii=False, default=str), True

    def is_terminal(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.terminal)
