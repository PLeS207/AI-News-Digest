"""Researcher A2A server (official a2a-sdk, A2A protocol 1.0, JSON-RPC binding)."""

from __future__ import annotations

import logging
import os

import uvicorn
from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from pydantic import ValidationError
from starlette.applications import Starlette

from ai_news_digest import __version__, config
from ai_news_digest.config import ConfigError, load_limits, load_sources, trace_dir
from ai_news_digest.openclaw_client import OpenClawClient
from ai_news_digest.models import RESEARCH_REQUEST_MEDIA_TYPE, RESEARCH_RESULT_MEDIA_TYPE, ResearchRequest
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.researcher.openclaw_loop import session_key
from ai_news_digest.tracing import Tracer

log = logging.getLogger("researcher")

SKILL_ID = "ai_news_research"


def build_agent_card(public_url: str) -> AgentCard:
    skill = AgentSkill(
        id=SKILL_ID,
        name="AI news research",
        description=("Fetches configured RSS sources for a time window and returns relevant AI news "
                     "as a structured ResearchResult with evidence excerpts and source links."),
        input_modes=[RESEARCH_REQUEST_MEDIA_TYPE, "application/json"],
        output_modes=[RESEARCH_RESULT_MEDIA_TYPE, "application/json"],
        tags=["rss", "ai-news", "research"],
        examples=["Find AI agents / MCP / AWS Bedrock news from the last 72 hours"],
    )
    return AgentCard(
        name="AI News Researcher",
        description="Researcher agent of the AI News Digest bot. Accepts ResearchRequest data parts.",
        version=__version__,
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(
                protocol_binding=TransportProtocol.JSONRPC.value,
                url=public_url,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        skills=[skill],
    )


class ResearcherExecutor(AgentExecutor):
    def __init__(self, researcher: Researcher, tracer: Tracer):
        self.researcher = researcher
        self.tracer = tracer

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)

        payloads = get_data_parts(context.message.parts) if context.message else []
        trace_id = "unknown-trace"
        try:
            if not payloads:
                raise ValueError("message has no ResearchRequest data part")
            request = ResearchRequest.model_validate(payloads[0])
            trace_id = request.trace_id
        except (ValidationError, ValueError) as exc:
            self.tracer.event(trace_id, agent="researcher", kind="a2a_task", name="validate_request",
                              status="failed", error=str(exc)[:500], data={"task_id": task.id})
            await updater.update_status(TaskState.TASK_STATE_REJECTED,
                                        message=new_text_message(f"Invalid ResearchRequest: {exc}"[:1000]))
            return

        a2a_data = {"task_id": task.id, "context_id": task.context_id, "request_id": request.request_id}
        a2a_data |= {"openclaw_session_key": session_key(request.request_id),
                     "openclaw_agent": self.researcher.openclaw.model}
        with self.tracer.span(trace_id, agent="researcher", kind="run", name="research",
                              input=request.model_dump(mode="json", exclude={"exclude_urls"}) | {
                                  "exclude_urls_count": len(request.exclude_urls)},
                              data=a2a_data) as run_span:
            self.tracer.event(trace_id, agent="researcher", kind="a2a_task", name="task_received",
                              status="started", data=a2a_data)
            await updater.update_status(TaskState.TASK_STATE_WORKING,
                                        message=new_text_message("Fetching RSS sources…"))
            try:
                result = await self.researcher.research(request)
            except Exception as exc:  # noqa: BLE001 - report any agent failure as a failed A2A task
                log.exception("research failed")
                run_span.fail(f"{type(exc).__name__}: {exc}")
                self.tracer.event(trace_id, agent="researcher", kind="a2a_task", name="task_failed",
                                  status="failed", error=f"{type(exc).__name__}: {exc}", data=a2a_data)
                await updater.failed(message=new_text_message(f"Research failed: {type(exc).__name__}: {exc}"[:1000]))
                return
            await updater.add_artifact(
                parts=[new_data_part(result.model_dump(mode="json"), media_type=RESEARCH_RESULT_MEDIA_TYPE)],
                name="research_result",
            )
            await updater.complete(message=new_text_message(
                f"{len(result.articles)} articles, incomplete={result.incomplete}"))
            run_span.set_output({"articles": len(result.articles), "incomplete": result.incomplete,
                                 "source_errors": [e.model_dump() for e in result.source_errors]})
            self.tracer.event(trace_id, agent="researcher", kind="a2a_task", name="task_completed",
                              status="completed", data=a2a_data | {"articles": len(result.articles)})

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")


def build_app(researcher: Researcher, tracer: Tracer, public_url: str) -> Starlette:
    card = build_agent_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=ResearcherExecutor(researcher, tracer),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = []
    routes.extend(create_agent_card_routes(card))
    routes.extend(create_jsonrpc_routes(handler, "/"))
    return Starlette(routes=routes)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        token = config.openclaw_gateway_token()
        sources = load_sources()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    host = os.environ.get("RESEARCHER_HOST", "0.0.0.0")
    port = int(os.environ.get("RESEARCHER_PORT", "9100"))
    public_url = os.environ.get("RESEARCHER_PUBLIC_URL", f"http://localhost:{port}")
    tracer = Tracer("researcher", trace_dir())
    fixed_now = None
    if os.environ.get("RESEARCHER_FIXED_NOW"):  # offline integration check only (fixture feeds)
        from datetime import datetime

        fixed_now = datetime.fromisoformat(os.environ["RESEARCHER_FIXED_NOW"])
    # The model loop runs in the OpenClaw `researcher` agent (ChatGPT/Codex subscription sign-in).
    openclaw = OpenClawClient(config.openclaw_url(), token, agent_id="researcher",
                              timeout_seconds=config.openclaw_timeout_seconds())
    researcher = Researcher(openclaw=openclaw, sources=sources, limits=load_limits(), tracer=tracer,
                            fixed_now=fixed_now)
    log.info("Researcher A2A server on %s:%s (card url %s, model agent %s, sources %s)",
             host, port, public_url, openclaw.model, [s.id for s in sources])
    uvicorn.run(build_app(researcher, tracer, public_url), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
