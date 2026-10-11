"""Operational commands.

    python -m ai_news_digest.cli check-config       # app container: env/config sanity (values hidden)
    python -m ai_news_digest.cli smoke              # app container: OpenClaw Gateway + editor agent,
                                                    #   Researcher Agent Card, Telegram getMe (no LLM tokens)
    python -m ai_news_digest.cli smoke --agent-turn # + one real OpenClaw Coordinator turn (spends tokens)
    python -m ai_news_digest.cli model-check        # researcher container: Anthropic model availability
    python -m ai_news_digest.cli research           # app container: one A2A task to the Researcher
    python -m ai_news_digest.cli runs               # text summary of traced runs (TRACE_FILES or TRACE_DIR)
    python -m ai_news_digest.cli reset-memory --yes # app container: DELETE the SQLite memory
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from ai_news_digest import config
from ai_news_digest.tracing import Tracer, join_openclaw_events, new_id, read_events


def _ok(label: str, detail: str = "") -> None:
    print(f"[ OK ] {label}{(': ' + detail) if detail else ''}")


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label}: {detail}")


def check_config() -> bool:
    good = True
    for name, fn in (("TELEGRAM_BOT_TOKEN", config.telegram_token),
                     ("ALLOWED_TELEGRAM_USER_ID", config.allowed_telegram_user_id),
                     ("OPENCLAW_GATEWAY_TOKEN", config.openclaw_gateway_token)):
        try:
            fn()
            _ok(name, "set (value hidden)")
        except config.ConfigError as exc:
            _fail(name, str(exc))
            good = False
    try:
        _ok("sources", ", ".join(s.id for s in config.load_sources()))
    except config.ConfigError as exc:
        _fail("sources", str(exc))
        good = False
    _ok("OPENCLAW_URL", f"{config.openclaw_url()} (agent {config.openclaw_agent_id()})")
    _ok("RESEARCHER_URL", config.researcher_url())
    _ok("EDITOR_DB_PATH", str(config.editor_db_path()))
    _ok("TRACE_DIR", str(config.trace_dir()))
    return good


async def smoke(agent_turn: bool) -> bool:
    from ai_news_digest.coordinator.openclaw_client import OpenClawClient

    good = True
    try:
        client = OpenClawClient(config.openclaw_url(), config.openclaw_gateway_token(),
                                agent_id=config.openclaw_agent_id())
        health = await client.health()
        agents = await client.list_agents()
        if client.model in agents:
            _ok("OpenClaw Gateway", f"{health.get('status')}; agent target {client.model} available")
        else:
            _fail("OpenClaw Gateway", f"agent {client.model} not in {agents}")
            good = False
    except Exception as exc:  # noqa: BLE001
        _fail("OpenClaw Gateway", f"{type(exc).__name__}: {str(exc)[:200]}")
        good = False
    try:
        from a2a.client import A2ACardResolver

        async with httpx.AsyncClient(timeout=10) as http:
            card = await A2ACardResolver(httpx_client=http, base_url=config.researcher_url()).get_agent_card()
        iface = card.supported_interfaces[0]
        _ok("Researcher Agent Card", f"{card.name} v{card.version} {iface.protocol_binding} "
                                     f"A2A {iface.protocol_version} @ {iface.url}")
    except Exception as exc:  # noqa: BLE001
        _fail("Researcher Agent Card", f"{type(exc).__name__}: {str(exc)[:200]}")
        good = False
    try:
        token = config.telegram_token()
        async with httpx.AsyncClient(timeout=15) as http:
            data = (await http.get(f"https://api.telegram.org/bot{token}/getMe")).json()
        if data.get("ok"):
            _ok("Telegram getMe", f"@{data['result'].get('username')}")
        else:
            _fail("Telegram getMe", str(data.get("description")))
            good = False
    except Exception as exc:  # noqa: BLE001
        _fail("Telegram", type(exc).__name__)
        good = False
    if agent_turn and good:
        good = await _agent_turn() and good
    return good


async def _agent_turn() -> bool:
    """One real Coordinator turn through OpenClaw (uses the model, spends tokens; no Telegram send)."""
    from ai_news_digest.coordinator.memory import MemoryStore
    from ai_news_digest.telegram_bot import build_coordinator

    tracer = Tracer("app", config.trace_dir())
    memory = MemoryStore(config.editor_db_path(), config.load_digest_defaults())
    coordinator = build_coordinator(tracer, memory)
    turn = await coordinator.handle("Какие интересы и настройки ты сохранил?", "smoke-check")
    sent: list[str] = []

    async def capture(text: str) -> None:
        sent.append(text)

    await coordinator.deliver(turn, capture)
    if turn.kind == "reply" and not turn.failed:
        _ok("OpenClaw Coordinator turn", f"trace {turn.trace_id}, runs {turn.openclaw_run_ids}: {sent[0][:120]!r}")
        return True
    _fail("OpenClaw Coordinator turn", f"kind={turn.kind} text={(turn.text or '')[:200]!r}")
    return False


async def model_check() -> bool:
    llm_cfg = config.load_llm_config()
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=llm_cfg.api_key, timeout=60, max_retries=1)
        model = await client.models.retrieve(llm_cfg.model)
        _ok("Anthropic model available", f"{model.id} ({model.display_name})")
        resp = await client.messages.create(
            model=llm_cfg.model, max_tokens=200, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        _ok("Anthropic messages.create", f"stop_reason={resp.stop_reason} text={text[:40]!r} "
                                          f"usage=in:{resp.usage.input_tokens}/out:{resp.usage.output_tokens}")
        return True
    except Exception as exc:  # noqa: BLE001
        _fail("Anthropic", f"{type(exc).__name__}: {str(exc)[:300]}")
        return False


async def research_once(period: int) -> int:
    from ai_news_digest.coordinator.researcher_client import ResearcherClient, new_request_id
    from ai_news_digest.models import ResearchRequest

    defaults = config.load_digest_defaults()
    tracer = Tracer("app", config.trace_dir())
    trace_id = new_id("tr_cli_")
    client = ResearcherClient(config.researcher_url(), tracer, timeout_seconds=config.a2a_timeout_seconds())
    req = ResearchRequest(request_id=new_request_id(), trace_id=trace_id, period_hours=period,
                          interests=defaults.interests, max_items=defaults.max_items)
    tracer.event(trace_id, agent="app", kind="run", name="cli_research", status="started")
    result, task_id = await client.research(req)
    tracer.event(trace_id, agent="app", kind="run", name="cli_research", status="completed")
    print(json.dumps({"trace_id": trace_id, "a2a_task_id": task_id, **result.model_dump(mode="json")},
                     ensure_ascii=False, indent=2))
    return 0


def trace_files() -> list[Path]:
    import os

    files_env = os.environ.get("TRACE_FILES")
    if files_env:
        return [Path(p) for p in files_env.split(",") if p]
    base = config.trace_dir()
    return [base / "app" / "events.jsonl", base / "editor" / "events.jsonl", base / "researcher" / "events.jsonl",
            base / "openclaw" / "events.jsonl"]


def runs_summary(limit: int) -> int:
    files = trace_files()
    events = join_openclaw_events(read_events(files))
    by_trace: dict[str, list[dict]] = {}
    for e in events:
        by_trace.setdefault(e["trace_id"], []).append(e)
    rows = []
    for trace_id, evs in by_trace.items():
        python_runs = [e for e in evs if e["kind"] == "run" and e.get("service") != "openclaw"]
        root = [e for e in python_runs if e["agent"] in ("app", "editor")] or python_runs
        if not root:
            continue
        final = next((e for e in reversed(root) if e["status"] != "started"), None)
        tasks = sorted({str(e["data"].get("task_id")) for e in evs
                        if e["kind"] == "a2a_send" and e.get("data", {}).get("task_id")})
        oc_runs = sorted({e["data"].get("openclaw_run_id") for e in evs
                          if e["kind"] == "openclaw_call" and e.get("data", {}).get("openclaw_run_id")})
        agents = sorted({f"{e['agent']}({e.get('runtime', 'python')})" for e in evs})
        tools = sorted({e["name"] for e in evs if e["kind"] == "tool_call"})
        rows.append((root[0]["ts"][:19], trace_id, final["status"] if final else "running",
                     f"{(final or {}).get('duration_ms') or 0:.0f}ms", ",".join(agents),
                     f"openclaw_runs={len(oc_runs)}", ",".join(tasks) or "-", ",".join(tools) or "-"))
    print(f"files: {', '.join(str(f) for f in files)}")
    for row in sorted(rows)[-limit:]:
        print(" | ".join(row))
    return 0


def reset_memory(confirm: bool) -> int:
    path = config.editor_db_path()
    if not confirm:
        print(f"This permanently deletes {path} (settings, digest history, delivered URLs). Re-run with --yes.")
        return 1
    removed = []
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            p.unlink()
            removed.append(str(p))
    print("Removed: " + (", ".join(removed) if removed else "nothing (memory was empty)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_news_digest.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    sp = sub.add_parser("smoke")
    sp.add_argument("--agent-turn", action="store_true", help="also run one real OpenClaw Coordinator turn")
    sub.add_parser("model-check")
    rp = sub.add_parser("research")
    rp.add_argument("--period", type=int, default=72)
    runs = sub.add_parser("runs")
    runs.add_argument("--limit", type=int, default=20)
    reset = sub.add_parser("reset-memory")
    reset.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "check-config":
        return 0 if check_config() else 1
    if args.cmd == "smoke":
        return 0 if asyncio.run(smoke(args.agent_turn)) else 1
    if args.cmd == "model-check":
        return 0 if asyncio.run(model_check()) else 1
    if args.cmd == "research":
        return asyncio.run(research_once(args.period))
    if args.cmd == "runs":
        return runs_summary(args.limit)
    if args.cmd == "reset-memory":
        return reset_memory(args.yes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
