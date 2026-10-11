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
    def __init__(self, service: str, base_dir: str | Path, runtime: str = "python"):
        self.service = service
        self.runtime = runtime
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
            "runtime": self.runtime,
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


def join_openclaw_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach OpenClaw hook events (written by the digest-telemetry plugin without a trace id) to the
    app trace that issued the Gateway call.

    1. ``run_id``: the app records the response ``id`` of every /v1/chat/completions call and OpenClaw
       hooks report the same value as ``runId``;
    2. ``session_window`` (fallback, e.g. a Gateway 502 has no response id): the hook's session key
       ``agent:<agent>:<key>`` equals the app turn's ``openclaw_session_key`` and the event happened
       inside that turn's run span (turns of one chat are processed sequentially).
    The method used is stored in ``data.joined_by``."""
    run_to_trace: dict[str, str] = {}
    windows: list[tuple[str, str, str, str]] = []  # (session_key, start_ts, end_ts, trace_id)
    starts: dict[str, dict[str, Any]] = {}
    for e in events:
        data = e.get("data") or {}
        if e.get("service") == "openclaw":
            continue
        if data.get("openclaw_run_id") and e.get("trace_id"):
            run_to_trace[data["openclaw_run_id"]] = e["trace_id"]
        if e.get("kind") == "run" and data.get("openclaw_session_key"):
            if e.get("status") == "started":
                starts[e["span_id"]] = e
            elif e["span_id"] in starts:
                windows.append((data["openclaw_session_key"], starts[e["span_id"]]["ts"], e["ts"], e["trace_id"]))

    def _norm(ts: str) -> str:
        return ts.replace("+00:00", "Z")

    for e in events:
        if e.get("service") != "openclaw":
            e.setdefault("runtime", "python")
            continue
        e.setdefault("runtime", "openclaw")
        data = e.setdefault("data", {})
        run_id = data.get("openclaw_run_id")
        if run_id in run_to_trace:
            e["trace_id"], data["joined_by"] = run_to_trace[run_id], "run_id"
            continue
        session = str(data.get("openclaw_session_key") or "")
        match = next((trace for key, start, end, trace in windows
                      if session.endswith(f":{key}") and _norm(start) <= _norm(e["ts"]) <= _norm(end)), None)
        if match:
            e["trace_id"], data["joined_by"] = match, "session_window"
        else:
            e["trace_id"] = f"openclaw-unlinked:{run_id}"
    return events
