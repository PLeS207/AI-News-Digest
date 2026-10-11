"""Single delivery path for everything the bot sends to Telegram.

* new digest: stored as pending, sent chunk by chunk; URLs are marked delivered only after the
  chunk that carries their link was sent successfully;
* re-send of the last digest (/last and "show the last digest"): the stored typed Digest is
  re-rendered and sent completely, without creating a new digest or touching delivered URLs;
* plain replies: escaped and split like any other message.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ai_news_digest.coordinator.digest import CHUNK_LIMIT, Chunk, _pack, _text_lines, render_chunks, render_text
from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.models import Digest
from ai_news_digest.tracing import Tracer

SendFn = Callable[[str], Awaitable[Any]]
AGENT = "app"


async def _send_chunks(chunks: list[Chunk], send: SendFn, tracer: Tracer, trace_id: str, label: str,
                       on_sent: Callable[[Chunk], None] | None = None) -> int:
    sent = 0
    for i, chunk in enumerate(chunks, start=1):
        try:
            with tracer.span(trace_id, agent=AGENT, kind="telegram_send", name=f"{label} {i}/{len(chunks)}",
                             input=chunk.text, data={"urls": [u for u, _ in chunk.items], "chars": len(chunk.text)}) as span:
                await send(chunk.text)
                span.set_output("delivered")
        except Exception:  # noqa: BLE001 - stop at the first failed message, keep what was delivered
            break
        sent += 1
        if on_sent:
            on_sent(chunk)
    return sent


async def deliver_digest(digest: Digest, *, send: SendFn, memory: MemoryStore, tracer: Tracer,
                         trace_id: str) -> dict[str, Any]:
    chunks = render_chunks(digest)
    digest_id = memory.create_digest(digest, render_text(digest))
    tracer.event(trace_id, agent=AGENT, kind="memory_write", name="digest_history", status="completed",
                 output=f"digest #{digest_id} stored as pending", data={"digest_id": digest_id})
    delivered: list[str] = []

    def mark(chunk: Chunk) -> None:
        if chunk.items:
            memory.mark_delivered(digest_id, chunk.items)
            delivered.extend(u for u, _ in chunk.items)
            tracer.event(trace_id, agent=AGENT, kind="memory_write", name="delivered_urls", status="completed",
                         output=f"{len(chunk.items)} URLs marked delivered",
                         data={"digest_id": digest_id, "urls": [u for u, _ in chunk.items]})

    sent = await _send_chunks(chunks, send, tracer, trace_id, "digest_chunk", mark)
    if not digest.items:
        status = "empty" if sent == len(chunks) else "failed"
    elif sent == len(chunks):
        status = "delivered"
    elif sent == 0:
        status = "failed"
    else:
        status = "partial"
    memory.finish_digest(digest_id, status)
    tracer.event(trace_id, agent=AGENT, kind="delivery", name="digest_delivery",
                 status="completed" if status in {"delivered", "empty"} else "failed",
                 output=f"{status}: {sent}/{len(chunks)} messages, {len(delivered)} URLs delivered",
                 data={"digest_id": digest_id, "status": status})
    return {"digest_id": digest_id, "status": status, "sent_chunks": sent, "total_chunks": len(chunks),
            "delivered_urls": delivered}


async def resend_last_digest(*, send: SendFn, memory: MemoryStore, tracer: Tracer, trace_id: str,
                             empty_text: str = "Дайджестов пока нет.") -> dict[str, Any]:
    last = memory.last_digest()
    tracer.event(trace_id, agent=AGENT, kind="memory_read", name="last_digest", status="completed",
                 output=f"found={last is not None}", data={"digest_id": last[0] if last else None})
    if last is None:
        sent = await _send_chunks(text_chunks(empty_text), send, tracer, trace_id, "reply")
        return {"status": "none", "sent_chunks": sent}
    digest_id, digest = last
    chunks = render_chunks(digest, resent_on=digest.created_at.strftime("%Y-%m-%d %H:%M UTC"))
    sent = await _send_chunks(chunks, send, tracer, trace_id, "resend_chunk")
    status = "resent" if sent == len(chunks) else ("partial" if sent else "failed")
    tracer.event(trace_id, agent=AGENT, kind="delivery", name="resend_last_digest",
                 status="completed" if status == "resent" else "failed",
                 output=f"{status}: {sent}/{len(chunks)} messages (no new digest, delivered URLs unchanged)",
                 data={"digest_id": digest_id, "status": status})
    return {"digest_id": digest_id, "status": status, "sent_chunks": sent, "total_chunks": len(chunks)}


def text_chunks(text: str, limit: int = CHUNK_LIMIT) -> list[Chunk]:
    lines = [line for paragraph in (text or "…").split("\n") for line in _text_lines("", paragraph, limit)]
    return _pack([lines], limit)


async def deliver_text(text: str, *, send: SendFn, tracer: Tracer, trace_id: str) -> dict[str, Any]:
    chunks = text_chunks(text)
    sent = await _send_chunks(chunks, send, tracer, trace_id, "reply")
    if sent < len(chunks):
        raise RuntimeError(f"reply delivery failed after {sent}/{len(chunks)} messages")
    return {"status": "sent", "sent_chunks": sent}

