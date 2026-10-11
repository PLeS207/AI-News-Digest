"""Read-only observability dashboard: merges app, OpenClaw and Researcher traces by trace_id.

Run: streamlit run dashboard/app.py   (TRACE_FILES=path1,path2,... to override sources)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ai_news_digest.tracing import join_openclaw_events, read_events

DEFAULT_FILES = ("data/traces/app/events.jsonl,data/traces/editor/events.jsonl,"
                 "data/traces/researcher/events.jsonl,data/traces/openclaw/events.jsonl")
TRACE_FILES = [Path(p.strip()) for p in os.environ.get("TRACE_FILES", DEFAULT_FILES).split(",") if p.strip()]
ROOT_AGENTS = ("app", "editor", "researcher")  # "editor": historical traces of the earlier in-process Editor

st.set_page_config(page_title="AI News Digest — agent traces", page_icon="🛰️", layout="wide")


def _data(e: dict[str, Any]) -> dict[str, Any]:
    return e.get("data") or {}


def _usage(e: dict[str, Any]) -> tuple[int, int]:
    usage = _data(e).get("usage") or {}
    tin = usage.get("input_tokens", usage.get("input", 0)) or 0
    tout = usage.get("output_tokens", usage.get("output", 0)) or 0
    return int(tin), int(tout)


def _task_id(e: dict[str, Any]) -> str:
    return str(_data(e).get("task_id") or _data(e).get("a2a_task_id") or "")


def _is_llm(e: dict[str, Any]) -> bool:
    return e["kind"] == "llm_call" and e["status"] != "started"


def build_runs(events: list[dict[str, Any]]) -> pd.DataFrame:
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_trace.setdefault(e["trace_id"], []).append(e)
    rows = []
    for trace_id, evs in by_trace.items():
        runs = [e for e in evs if e["kind"] == "run" and e.get("service") != "openclaw"]
        if not runs:
            continue
        root_agent = next((a for a in ROOT_AGENTS if any(e["agent"] == a for e in runs)), runs[0]["agent"])
        root = [e for e in runs if e["agent"] == root_agent]
        final = next((e for e in reversed(root) if e["status"] != "started"), None)
        failed_any = any(e["status"] == "failed" for e in evs
                         if e["kind"] in {"run", "a2a_task", "delivery", "rss_fetch", "a2a_retry", "action_check"})
        status = final["status"] if final else "running"
        if status == "completed" and failed_any:
            status = "completed_with_errors"
        llm = [e for e in evs if _is_llm(e)]
        usage_events = [e for e in evs if _data(e).get("usage")]
        tokens_in = sum(_usage(e)[0] for e in usage_events)
        tokens_out = sum(_usage(e)[1] for e in usage_events)
        task_ids = sorted({_task_id(e) for e in evs if e["kind"].startswith("a2a") and _task_id(e)})
        oc_runs = sorted({_data(e).get("openclaw_run_id") for e in evs
                          if e["kind"] == "openclaw_call" and _data(e).get("openclaw_run_id")})
        oc_sessions = sorted({_data(e).get("openclaw_session_key") for e in evs
                              if e.get("service") == "openclaw" and _data(e).get("openclaw_session_key")})
        agents = sorted({f"{e['agent']} ({e.get('runtime', 'python')})" for e in evs})
        rows.append({
            "started": root[0]["ts"],
            "trace_id": trace_id,
            "entry": root[0]["name"],
            "status": status,
            "duration_s": round((final or {}).get("duration_ms", 0) / 1000, 1) if final else None,
            "agents (runtime)": ", ".join(agents),
            "llm_calls": len(llm),
            "tool_calls": sum(1 for e in evs if e["kind"] == "tool_call" and e["status"] != "started"),
            "openclaw_runs": len(oc_runs),
            "openclaw_session": ", ".join(oc_sessions),
            "a2a_task": ", ".join(task_ids),
            "tokens in/out": f"{tokens_in}/{tokens_out}" if usage_events else "",
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
        data = dict(_data(e))
        run_id = data.pop("openclaw_run_id", None) or ""
        session = data.pop("openclaw_session_key", None) or ""
        for key in ("usage", "task_id", "a2a_task_id", "openclaw_session_id"):
            data.pop(key, None)
        rows.append({
            "time": e["ts"][11:23],
            "service": e["service"],
            "agent": e["agent"],
            "runtime": e.get("runtime", "python"),
            "kind": e["kind"],
            "name": e["name"],
            "status": e["status"],
            "ms": e.get("duration_ms"),
            "openclaw_run_id": run_id,
            "openclaw_session": session,
            "a2a_task_id": _task_id(e),
            "tokens": f"{tin}/{tout}" if (tin or tout) else "",
            "input": e.get("input") or "",
            "output": e.get("output") or "",
            "error": e.get("error") or "",
            "data": str(data) if data else "",
        })
    return pd.DataFrame(rows)


STATUS_ICON = {"completed": "✅", "failed": "❌", "denied": "⛔", "rejected": "🚫", "started": "▶️",
               "running": "⏳", "completed_with_errors": "⚠️"}


def render() -> None:
    events = join_openclaw_events(read_events(TRACE_FILES))
    st.title("🛰️ AI News Digest — agent activity")
    st.caption("Read-only view of real trace events: app (Telegram, Coordinator tools, delivery; Python), "
               "Editor/Coordinator and Researcher agents (OpenClaw runtime: model calls and tool choice, "
               "digest-telemetry plugin hooks) and the Researcher service (Python: A2A server, RSS tools). "
               "Token counts come from the runtime; no dollar cost is computed for subscription use. "
               f"Sources: {', '.join(str(p) for p in TRACE_FILES)}")
    if not events:
        st.info("No trace events yet. Send a message to the bot, then refresh.")
        return

    runs = build_runs(events)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Runs", len(runs))
    c2.metric("Failed runs", int((runs["status"] == "failed").sum()) if not runs.empty else 0)
    oc_llm = [e for e in events if _is_llm(e) and e.get("service") == "openclaw"]
    legacy = sum(1 for e in events if _is_llm(e) and e.get("service") == "researcher")  # pre-OpenClaw Researcher
    c3.metric("Model calls · editor (OpenClaw)", sum(1 for e in oc_llm if e["agent"] == "editor"))
    c4.metric("Model calls · researcher (OpenClaw)", sum(1 for e in oc_llm if e["agent"] == "researcher"),
              help=f"plus {legacy} legacy in-process Researcher calls" if legacy else None)
    c5.metric("Tool calls", sum(1 for e in events if e["kind"] == "tool_call" and e["status"] != "started"))
    c6.metric("A2A tasks", len({_task_id(e) for e in events if e["kind"] == "a2a_send" and _task_id(e)}))

    tab_runs, tab_other = st.tabs(["Runs", "Other events (auth, /new, unlinked)"])
    with tab_runs:
        if runs.empty:
            st.info("No runs yet.")
        else:
            show = runs.copy()
            show["status"] = show["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
            st.dataframe(show, hide_index=True, width="stretch")
            indexed = runs.set_index("trace_id")
            trace_id = st.selectbox("Trace", runs["trace_id"].tolist(),
                                    format_func=lambda t: f"{t} — {indexed.loc[t, 'started'][:19]} — "
                                                          f"{indexed.loc[t, 'status']}")
            hide_started = st.toggle("Hide 'started' events of spans", value=True)
            evs = [e for e in events if e["trace_id"] == trace_id]
            tl = timeline(evs, hide_started)
            agents = st.multiselect("Agents", sorted(tl["agent"].unique()), default=sorted(tl["agent"].unique()))
            tl = tl[tl["agent"].isin(agents)]
            tl["status"] = tl["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
            st.subheader("Timeline")
            st.dataframe(tl, hide_index=True, width="stretch", height=560)
            for agent in ("editor", "researcher"):
                proof = [e for e in evs if e["kind"] == "llm_input" and e["agent"] == agent]
                route = next((_data(e).get("api") for e in evs if e["kind"] == "llm_call" and e["agent"] == agent
                              and _data(e).get("api")), None)
                if proof:
                    d = _data(proof[0])
                    st.caption(f"OpenClaw evidence ({agent}): workspace instructions loaded = "
                               f"{d.get('workspace_instructions_loaded')}, native tools after policy = "
                               f"{d.get('native_tools_after_policy')}, model = {d.get('provider')}/{d.get('model')}, "
                               f"route = {route}")
            errors = [e for e in evs if e.get("error")]
            if errors:
                st.subheader("Errors")
                for e in errors:
                    st.error(f"[{e['agent']}/{e.get('runtime', 'python')}] {e['kind']} {e['name']}: {e['error']}")
    with tab_other:
        known = set(runs.get("trace_id", []))
        orphan = [e for e in events if e["trace_id"] not in known]
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
