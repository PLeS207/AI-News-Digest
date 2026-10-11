"""Dashboard renders merged traces from both services (headless Streamlit AppTest)."""

from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")

ROOT = Path(__file__).resolve().parents[1]


async def test_dashboard_renders_runs_from_both_services(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("offline_demo", ROOT / "scripts" / "offline_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    await demo.main(tmp_path)

    files = [tmp_path / "traces" / "editor" / "events.jsonl", tmp_path / "traces" / "researcher" / "events.jsonl"]
    assert all(f.exists() for f in files)
    monkeypatch.setenv("TRACE_FILES", ",".join(str(f) for f in files))
    at = streamlit_testing.AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    runs = at.dataframe[0].value
    assert len(runs) == 5
    assert set(runs["status"].str.split().str[-1]) >= {"completed", "failed", "completed_with_errors"}
    assert runs["a2a_task"].str.len().gt(0).sum() == 2
    # pick a digest run: its timeline merges Editor and Researcher events under one trace_id
    digest_trace = runs[runs["a2a_task"].str.len() > 0]["trace_id"].iloc[0]
    at.selectbox[0].select(digest_trace).run()
    assert not at.exception
    timeline = at.dataframe[1].value
    assert {"editor", "researcher"} <= set(timeline["agent"])
    kinds = set(timeline["kind"])
    assert {"llm_call", "tool_call", "a2a_discovery", "a2a_send", "a2a_task", "rss_fetch",
            "memory_read", "telegram_send", "delivery"} <= kinds
    assert timeline["a2a_task_id"].str.len().gt(0).any()
