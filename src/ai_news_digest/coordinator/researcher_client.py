"""Editor-side A2A client for the Researcher (official a2a-sdk client, JSON-RPC binding)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from a2a.client import A2ACardResolver, A2AClientError, A2AClientTimeoutError, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_message, new_text_part
from a2a.types import Role, SendMessageRequest, Task, TaskState
from pydantic import ValidationError

from ai_news_digest.models import RESEARCH_REQUEST_MEDIA_TYPE, ResearchRequest, ResearchResult
from ai_news_digest.tracing import Tracer

AGENT = "app"


class ResearcherUnavailable(RuntimeError):
    """The Researcher could not be reached or did not return a valid result."""


def _is_transient(exc: BaseException) -> bool:
    """Connection-level problems before any task was accepted are safe to retry."""
    if isinstance(exc, A2AClientTimeoutError):
        return False  # the task may still be running; do not duplicate long LLM work
    cause = exc.__cause__ or exc.__context__
    candidates = [exc, cause]
    for candidate in candidates:
        if isinstance(candidate, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
            return True
        if isinstance(candidate, httpx.HTTPStatusError) and candidate.response.status_code in {502, 503, 504}:
            return True
    status = getattr(exc, "status_code", None)
    return status in {502, 503, 504}


class ResearcherClient:
    def __init__(self, base_url: str, tracer: Tracer, *, timeout_seconds: float = 300.0,
                 max_attempts: int = 2, backoff_seconds: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.tracer = tracer
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, min(max_attempts, 3))
        self.backoff_seconds = backoff_seconds

    async def research(self, request: ResearchRequest) -> tuple[ResearchResult, str]:
        """Discover the Researcher, send the task, wait for the result. Returns (result, task_id)."""
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._attempt(request, attempt)
            except ResearcherUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                transient = _is_transient(exc)
                self.tracer.event(request.trace_id, agent=AGENT, kind="a2a_retry", name="researcher",
                                  status="failed", error=f"{type(exc).__name__}: {exc}",
                                  data={"attempt": attempt, "max_attempts": self.max_attempts,
                                        "transient": transient})
                if not transient or attempt == self.max_attempts:
                    break
                await asyncio.sleep(self.backoff_seconds * attempt)
        raise ResearcherUnavailable(_describe(last_error))

    async def _attempt(self, request: ResearchRequest, attempt: int) -> tuple[ResearchResult, str]:
        timeout = httpx.Timeout(self.timeout_seconds, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            with self.tracer.span(request.trace_id, agent=AGENT, kind="a2a_discovery", name="agent_card",
                                  input={"base_url": self.base_url}, data={"attempt": attempt}) as span:
                card = await A2ACardResolver(httpx_client=http, base_url=self.base_url).get_agent_card()
                iface = card.supported_interfaces[0] if card.supported_interfaces else None
                span.data.update({
                    "agent_name": card.name, "agent_version": card.version,
                    "skills": [s.id for s in card.skills],
                    "protocol_binding": iface.protocol_binding if iface else None,
                    "protocol_version": iface.protocol_version if iface else None,
                    "interface_url": iface.url if iface else None,
                })
                span.set_output(f"{card.name} v{card.version}")

            client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
            message = new_message(
                parts=[
                    new_text_part(f"ResearchRequest {request.request_id}: find relevant AI news."),
                    new_data_part(request.model_dump(mode="json"), media_type=RESEARCH_REQUEST_MEDIA_TYPE),
                ],
                role=Role.ROLE_USER,
                context_id=request.trace_id,
            )
            task: Task | None = None
            try:
                with self.tracer.span(
                    request.trace_id, agent=AGENT, kind="a2a_send", name="SendMessage",
                    input={"request_id": request.request_id, "message_id": message.message_id,
                           "period_hours": request.period_hours, "interests": request.interests,
                           "exclude_urls": len(request.exclude_urls)},
                    data={"attempt": attempt, "binding": "JSONRPC"},
                ) as span:
                    async for event in client.send_message(SendMessageRequest(message=message)):
                        if event.HasField("task"):
                            task = event.task
                        elif event.HasField("message"):
                            span.set_output(get_message_text(event.message))
                    if task is None:
                        raise ResearcherUnavailable("Researcher did not return an A2A task")
                    state = TaskState.Name(task.status.state)
                    span.data.update({"task_id": task.id, "context_id": task.context_id, "task_state": state})
                    span.set_output(f"task {task.id} -> {state}")
            finally:
                await client.close()

            state = TaskState.Name(task.status.state)
            status_text = get_message_text(task.status.message) if task.status.HasField("message") else ""
            self.tracer.event(request.trace_id, agent=AGENT, kind="a2a_task", name="task_status",
                              status="completed" if task.status.state == TaskState.TASK_STATE_COMPLETED else "failed",
                              output=status_text, data={"task_id": task.id, "task_state": state})
            if task.status.state != TaskState.TASK_STATE_COMPLETED:
                raise ResearcherUnavailable(f"Researcher task {task.id} ended in {state}: {status_text}")

            with self.tracer.span(request.trace_id, agent=AGENT, kind="a2a_result", name="validate_result",
                                  data={"task_id": task.id}) as span:
                payloads = [p for art in task.artifacts for p in get_data_parts(art.parts)]
                if not payloads:
                    raise ResearcherUnavailable(f"Researcher task {task.id} returned no data artifact")
                try:
                    result = ResearchResult.model_validate(payloads[0])
                except ValidationError as exc:
                    raise ResearcherUnavailable(f"invalid ResearchResult: {exc.error_count()} errors") from exc
                if result.request_id != request.request_id or result.trace_id != request.trace_id:
                    raise ResearcherUnavailable("ResearchResult does not match the request id")
                span.set_output({"articles": len(result.articles), "incomplete": result.incomplete,
                                 "source_errors": [e.source_id for e in result.source_errors]})
            return result, task.id


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    if isinstance(exc, A2AClientTimeoutError):
        return "Researcher timed out"
    if isinstance(exc, A2AClientError) or isinstance(exc, httpx.HTTPError):
        return f"Researcher is unreachable ({type(exc).__name__}: {str(exc)[:200]})"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def new_request_id() -> str:
    return uuid.uuid4().hex
