"""Structured JSONL trace events written by instrumented code (never by the model).

Each service writes to its own file (<TRACE_DIR>/<service>/events.jsonl). The dashboard
merges files from both services by ``trace_id``.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_FIELD_CHARS = 600

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    re.compile(r"(?i)(authorization|x-api-key)\s*[:=]\s*(bearer\s+)?[^\s\"',}]+"),
    re.compile(r"(?i)bot\d{6,12}:[A-Za-z0-9_\-]{30,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def clip(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    """Serialize + redact + truncate a value for the trace."""
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    value = redact(value)
    if len(value) > limit:
        return value[:limit] + f"… [+{len(value) - limit} chars]"
    return value


def _safe_data(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    raw = redact(json.dumps(data, ensure_ascii=False, default=str))
    if len(raw) > 4000:
        return {"truncated": raw[:4000]}
    return json.loads(raw)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


class Tracer:
    def __init__(self, service: str, base_dir: str | Path):
        self.service = service
        self.path = Path(base_dir) / service / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def event(
        self,
        trace_id: str,
        *,
        agent: str,
        kind: str,
        name: str,
        status: str,
        input: Any = None,
        output: Any = None,
        error: Any = None,
        duration_ms: float | None = None,
        span_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": trace_id,
            "span_id": span_id or new_id(),
            "service": self.service,
            "agent": agent,
            "kind": kind,
            "name": name,
            "status": status,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "input": clip(input),
            "output": clip(output),
            "error": clip(error, 1000),
            "data": _safe_data(data),
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return record

    @contextmanager
    def span(
        self,
        trace_id: str,
        *,
        agent: str,
        kind: str,
        name: str,
        input: Any = None,
        data: dict[str, Any] | None = None,
    ) -> Iterator["Span"]:
        span = Span(self, trace_id, agent, kind, name, data or {})
        self.event(trace_id, agent=agent, kind=kind, name=name, status="started", input=input,
                   span_id=span.span_id, data=data)
        try:
            yield span
        except BaseException as exc:
            span.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.finish()


class Span:
    def __init__(self, tracer: Tracer, trace_id: str, agent: str, kind: str, name: str, data: dict[str, Any]):
        self.tracer = tracer
        self.trace_id = trace_id
        self.agent = agent
        self.kind = kind
        self.name = name
        self.span_id = new_id()
        self.data = dict(data)
        self.output: Any = None
        self.error: str | None = None
        self.status = "completed"
        self._start = time.perf_counter()
        self._done = False

    def set_output(self, output: Any) -> None:
        self.output = output

    def fail(self, error: str, status: str = "failed") -> None:
        self.error = error
        self.status = status

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        self.tracer.event(
            self.trace_id, agent=self.agent, kind=self.kind, name=self.name, status=self.status,
            output=self.output, error=self.error, span_id=self.span_id,
            duration_ms=(time.perf_counter() - self._start) * 1000, data=self.data,
        )


def read_events(paths: list[Path]) -> list[dict[str, Any]]:
    """Read and merge events from several JSONL files (tolerates partial lines)."""
    events: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    events.sort(key=lambda e: e.get("ts", ""))
    return events
