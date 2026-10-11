"""Dashboard renders merged traces (headless Streamlit AppTest).

The OpenClaw hook events below are TEST DATA shaped like the digest-telemetry plugin output; they
check the join logic only. Real OpenClaw events come from scripts/openclaw_integration_check.py.
"""

import json
from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")

ROOT = Path(__file__).resolve().parents[1]


async def test_dashboard_shows_app_openclaw_and_researcher_in_one_trace(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("offline_demo", ROOT / "scripts" / "offline_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    await demo.main(tmp_path)

    app_file = tmp_path / "traces" / "app" / "events.jsonl"
    researcher_file = tmp_path / "traces" / "researcher" / "events.jsonl"
    app_events = [json.loads(line) for line in app_file.read_text().splitlines()]
    call = next(e for e in app_events if e["kind"] == "openclaw_call" and e["status"] == "completed"
                and e["data"].get("requested_tools") == ["request_research"])
    run_id, session = call["data"]["openclaw_run_id"], call["data"]["openclaw_session_key"]
    oc_file = tmp_path / "traces" / "openclaw" / "events.jsonl"
    oc_file.parent.mkdir(parents=True)
    base = {"trace_id": None, "service": "openclaw", "agent": "editor", "runtime": "openclaw", "input": None,
            "output": None, "error": None, "duration_ms": 12.0}
    oc_events = [
        {**base, "ts": call["ts"], "span_id": "a", "kind": "llm_input", "name": "openclaw.llm_input",
         "status": "completed", "data": {"openclaw_run_id": run_id, "openclaw_session_key": f"agent:editor:{session}",
                                         "native_tools_after_policy": [], "workspace_instructions_loaded": True}},
        {**base, "ts": call["ts"], "span_id": "b", "kind": "llm_call", "name": "openclaw.model_call c1",
         "status": "completed", "data": {"openclaw_run_id": run_id, "openclaw_session_key": f"agent:editor:{session}"}},
    ]
    oc_file.write_text("".join(json.dumps(e) + "\n" for e in oc_events))

    monkeypatch.setenv("TRACE_FILES", ",".join(str(f) for f in (app_file, researcher_file, oc_file)))
    at = streamlit_testing.AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    runs = at.dataframe[0].value
    assert len(runs) == 5
    assert set(runs["status"].str.split().str[-1]) >= {"completed", "failed", "completed_with_errors"}
    joined = runs[runs["trace_id"] == call["trace_id"]].iloc[0]
    assert "editor (openclaw)" in joined["agents (runtime)"] and "researcher (python)" in joined["agents (runtime)"]
    assert joined["a2a_task"] and joined["openclaw_runs"] >= 1

    at.selectbox[0].select(call["trace_id"]).run()
    assert not at.exception
    timeline = at.dataframe[1].value
    assert {"app", "editor", "researcher"} <= set(timeline["agent"])
    assert {"python", "openclaw"} <= set(timeline["runtime"])
    assert {"openclaw_call", "llm_input", "tool_call", "a2a_discovery", "a2a_send", "a2a_task", "rss_fetch",
            "memory_read", "telegram_send", "delivery"} <= set(timeline["kind"])
    assert (timeline["openclaw_run_id"] == run_id).sum() >= 3
