"""Read-only observability dashboard: merges Editor and Researcher trace files by trace_id.

Run: streamlit run dashboard/app.py   (TRACE_FILES=path1,path2 to override sources)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ai_news_digest.tracing import read_events

DEFAULT_FILES = "data/traces/editor/events.jsonl,data/traces/researcher/events.jsonl"
TRACE_FILES = [Path(p.strip()) for p in os.environ.get("TRACE_FILES", DEFAULT_FILES).split(",") if p.strip()]

st.set_page_config(page_title="AI News Digest — agent traces", page_icon="🛰️", layout="wide")


def _usage(e: dict[str, Any]) -> tuple[int, int]:
    usage = (e.get("data") or {}).get("usage") or {}
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _task_id(e: dict[str, Any]) -> str:
    return str((e.get("data") or {}).get("task_id") or (e.get("data") or {}).get("a2a_task_id") or "")


def build_runs(events: list[dict[str, Any]]) -> pd.DataFrame:
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_trace.setdefault(e["trace_id"], []).append(e)
    rows = []
    for trace_id, evs in by_trace.items():
        runs = [e for e in evs if e["kind"] == "run"]
        if not runs:
            continue
        root_agent = "editor" if any(e["agent"] == "editor" for e in runs) else runs[0]["agent"]
        root = [e for e in runs if e["agent"] == root_agent]
        final = next((e for e in reversed(root) if e["status"] != "started"), None)
        failed_any = any(e["status"] == "failed" for e in evs if e["kind"] in {"run", "a2a_task", "delivery", "rss_fetch", "a2a_retry"})
        status = final["status"] if final else "running"
        if status == "completed" and failed_any:
            status = "completed_with_errors"
        llm = [e for e in evs if e["kind"] == "llm_call" and e["status"] != "started"]
        tokens_in = sum(_usage(e)[0] for e in llm)
        tokens_out = sum(_usage(e)[1] for e in llm)
        task_ids = sorted({_task_id(e) for e in evs if e["kind"].startswith("a2a") and _task_id(e)})
        rows.append({
            "started": root[0]["ts"],
            "trace_id": trace_id,
            "entry": root[0]["name"],
            "status": status,
            "duration_s": round((final or {}).get("duration_ms", 0) / 1000, 1) if final else None,
            "agents": ", ".join(sorted({e["agent"] for e in evs})),
            "llm_calls": len(llm),
            "tool_calls": sum(1 for e in evs if e["kind"] == "tool_call"),
            "a2a_task": ", ".join(task_ids),
            "tokens_in/out": f"{tokens_in}/{tokens_out}" if llm else "",
            "request": root[0].get("input") or "",
        })
    df = pd.DataFrame(rows)
    return df.sort_values("started", ascending=False) if not df.empty else df


def timeline(evs: list[dict[str, Any]], hide_started: bool) -> pd.DataFrame:
    rows = []
    for e in evs:
        if hide_started and e["status"] == "started" and e["kind"] != "run":
            continue
        tin, tout = _usage(e)
        data = dict(e.get("data") or {})
        for key in ("usage", "task_id", "a2a_task_id"):
            data.pop(key, None)
        rows.append({
            "time": e["ts"][11:23],
            "service": e["service"],
            "agent": e["agent"],
            "kind": e["kind"],
            "name": e["name"],
            "status": e["status"],
            "ms": e.get("duration_ms"),
            "a2a_task_id": _task_id(e),
            "tokens": f"{tin}/{tout}" if (tin or tout) else "",
            "input": e.get("input") or "",
            "output": e.get("output") or "",
            "error": e.get("error") or "",
            "data": str(data) if data else "",
        })
    return pd.DataFrame(rows)


STATUS_ICON = {"completed": "✅", "failed": "❌", "denied": "⛔", "started": "▶️", "running": "⏳",
               "completed_with_errors": "⚠️"}


def render() -> None:
    events = read_events(TRACE_FILES)
    st.title("🛰️ AI News Digest — agent activity")
    st.caption("Read-only view of real trace events written by the Editor (app) and Researcher (A2A server) "
               f"processes. Sources: {', '.join(str(p) for p in TRACE_FILES)}")
    if not events:
        st.info("No trace events yet. Send a message to the bot, then refresh.")
        return

    runs = build_runs(events)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Runs", len(runs))
    c2.metric("Failed runs", int((runs["status"] == "failed").sum()) if not runs.empty else 0)
    c3.metric("LLM calls", sum(1 for e in events if e["kind"] == "llm_call" and e["status"] != "started"))
    c4.metric("Tool calls", sum(1 for e in events if e["kind"] == "tool_call"))
    c5.metric("A2A tasks", len({_task_id(e) for e in events if e["kind"] == "a2a_send" and _task_id(e)}))

    tab_runs, tab_other = st.tabs(["Runs", "Other events (auth, /new, …)"])
    with tab_runs:
        if runs.empty:
            st.info("No runs yet.")
        else:
            show = runs.copy()
            show["status"] = show["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
            st.dataframe(show, hide_index=True, width="stretch")
            trace_id = st.selectbox("Trace", runs["trace_id"].tolist(),
                                    format_func=lambda t: f"{t} — {runs.set_index('trace_id').loc[t, 'started'][:19]}"
                                                          f" — {runs.set_index('trace_id').loc[t, 'status']}")
            hide_started = st.toggle("Hide 'started' events of spans", value=True)
            evs = [e for e in events if e["trace_id"] == trace_id]
            tl = timeline(evs, hide_started)
            agents = st.multiselect("Agents", sorted(tl["agent"].unique()), default=sorted(tl["agent"].unique()))
            tl = tl[tl["agent"].isin(agents)]
            tl["status"] = tl["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
            st.subheader("Timeline")
            st.dataframe(tl, hide_index=True, width="stretch", height=520)
            errors = [e for e in evs if e.get("error")]
            if errors:
                st.subheader("Errors")
                for e in errors:
                    st.error(f"[{e['agent']}] {e['kind']} {e['name']}: {e['error']}")
    with tab_other:
        orphan = [e for e in events if e["trace_id"] not in set(runs.get("trace_id", []))]
        st.dataframe(timeline(orphan, hide_started=False), hide_index=True, width="stretch")


with st.sidebar:
    st.header("Refresh")
    auto = st.toggle("Auto-refresh every 5 s", value=False)
    st.button("Refresh now")
    st.caption("The dashboard never writes to traces or memory.")

if auto:
    st.fragment(run_every="5s")(render)()
else:
    render()
