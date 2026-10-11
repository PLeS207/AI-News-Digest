"""Operational commands: config check, live smoke test, one-off A2A research, memory reset.

    python -m ai_news_digest.cli check-config
    python -m ai_news_digest.cli smoke            # LIVE: Anthropic model, Telegram getMe, Researcher card
    python -m ai_news_digest.cli research         # LIVE: one A2A task to the Researcher, prints result
    python -m ai_news_digest.cli runs             # text summary of traced runs (TRACE_FILES or TRACE_DIR)
    python -m ai_news_digest.cli reset-memory --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from ai_news_digest import config
from ai_news_digest.tracing import Tracer, new_id


def _ok(label: str, detail: str = "") -> None:
    print(f"[ OK ] {label}{(': ' + detail) if detail else ''}")


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label}: {detail}")


def check_config(require_telegram: bool = True) -> bool:
    good = True
    llm = config.load_llm_config()
    if llm.api_key:
        _ok("ANTHROPIC_API_KEY", "set (value hidden)")
    else:
        _fail("ANTHROPIC_API_KEY", "not set")
        good = False
    _ok("ANTHROPIC_MODEL", llm.model)
    for name, fn in (("TELEGRAM_BOT_TOKEN", config.telegram_token),
                     ("ALLOWED_TELEGRAM_USER_ID", config.allowed_telegram_user_id)):
        try:
            fn()
            _ok(name, "set (value hidden)")
        except config.ConfigError as exc:
            if require_telegram:
                _fail(name, str(exc))
                good = False
    try:
        sources = config.load_sources()
        _ok("sources", ", ".join(s.id for s in sources))
    except config.ConfigError as exc:
        _fail("sources", str(exc))
        good = False
    _ok("RESEARCHER_URL", config.researcher_url())
    _ok("EDITOR_DB_PATH", str(config.editor_db_path()))
    _ok("TRACE_DIR", str(config.trace_dir()))
    return good


async def smoke() -> bool:
    good = True
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
    except Exception as exc:  # noqa: BLE001
        _fail("Anthropic", f"{type(exc).__name__}: {str(exc)[:300]}")
        good = False
    try:
        token = config.telegram_token()
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"https://api.telegram.org/bot{token}/getMe")
        data = r.json()
        if data.get("ok"):
            _ok("Telegram getMe", f"@{data['result'].get('username')}")
        else:
            _fail("Telegram getMe", str(data.get("description")))
            good = False
    except Exception as exc:  # noqa: BLE001
        _fail("Telegram", type(exc).__name__)
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
    return good


async def research_once(period: int) -> int:
    from ai_news_digest.editor.researcher_client import ResearcherClient, new_request_id
    from ai_news_digest.models import ResearchRequest

    defaults = config.load_digest_defaults()
    tracer = Tracer("editor", config.trace_dir())
    trace_id = new_id("tr_cli_")
    client = ResearcherClient(config.researcher_url(), tracer, timeout_seconds=config.a2a_timeout_seconds())
    req = ResearchRequest(request_id=new_request_id(), trace_id=trace_id, period_hours=period,
                          interests=defaults.interests, max_items=defaults.max_items)
    tracer.event(trace_id, agent="editor", kind="run", name="cli_research", status="started")
    result, task_id = await client.research(req)
    tracer.event(trace_id, agent="editor", kind="run", name="cli_research", status="completed")
    print(json.dumps({"trace_id": trace_id, "a2a_task_id": task_id, **result.model_dump(mode="json")},
                     ensure_ascii=False, indent=2))
    return 0


def runs_summary(limit: int) -> int:
    import os
    from pathlib import Path

    from ai_news_digest.tracing import read_events

    files_env = os.environ.get("TRACE_FILES")
    if files_env:
        files = [Path(p) for p in files_env.split(",") if p]
    else:
        files = [config.trace_dir() / svc / "events.jsonl" for svc in ("editor", "researcher")]
    events = read_events(files)
    by_trace: dict[str, list[dict]] = {}
    for e in events:
        by_trace.setdefault(e["trace_id"], []).append(e)
    rows = []
    for trace_id, evs in by_trace.items():
        root = [e for e in evs if e["kind"] == "run" and e["agent"] == "editor"] or \
               [e for e in evs if e["kind"] == "run"]
        if not root:
            continue
        final = next((e for e in reversed(root) if e["status"] != "started"), None)
        tasks = sorted({str(e["data"].get("task_id")) for e in evs
                        if e["kind"] == "a2a_send" and e.get("data", {}).get("task_id")})
        tools = sorted({e["name"] for e in evs if e["kind"] == "tool_call"})
        rows.append((root[0]["ts"][:19], trace_id, final["status"] if final else "running",
                     f"{(final or {}).get('duration_ms') or 0:.0f}ms",
                     ",".join(sorted({e["agent"] for e in evs})), ",".join(tasks) or "-", ",".join(tools) or "-"))
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
    sub.add_parser("smoke")
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
        return 0 if asyncio.run(smoke()) else 1
    if args.cmd == "research":
        return asyncio.run(research_once(args.period))
    if args.cmd == "runs":
        return runs_summary(args.limit)
    if args.cmd == "reset-memory":
        return reset_memory(args.yes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
