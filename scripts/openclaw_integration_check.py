"""OFFLINE integration check against the REAL OpenClaw Gateway (see compose.offline.yaml).

Runs inside the `app` container of the offline Compose project. The Coordinator turns go through
the real OpenClaw Gateway (agent `editor`, its workspace, tool policy, sessions, plugin hooks), whose
model provider is the scripted fake-llm service. The Researcher is the real Python A2A server whose
model loop runs in the OpenClaw agent `researcher` (same fake provider, fixture feeds over HTTP).
Telegram is replaced by an in-memory sender.

Evidence is read from three independent places:
  * the fake provider's request log  - what OpenClaw actually sent to the model (system prompt, tools);
  * the digest-telemetry plugin log - OpenClaw runtime hooks (runs, model calls, native tool policy);
  * the app/researcher traces       - backend tools, A2A, memory, delivery.
It proves the integration, NOT the real model, ChatGPT/Codex auth or Telegram (live scenario).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ai_news_digest import config
from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.openclaw_client import OpenClawClient
from ai_news_digest.coordinator.researcher_client import ResearcherClient
from ai_news_digest.coordinator.service import CoordinatorService
from ai_news_digest.coordinator.tools import ALLOWED_TOOL_NAMES
from ai_news_digest.tracing import Tracer, join_openclaw_events, read_events

FAKE_LOG = Path(os.environ.get("FAKE_LLM_LOG", "/data/fake-llm/requests.jsonl"))
OPENCLAW_TRACES = Path("/traces/openclaw/events.jsonl")
RESEARCHER_TRACES = Path("/traces/researcher/researcher/events.jsonl")
WORKSPACE_MARKER = "<!-- ai-news-digest:editor-workspace v1 -->"
RESEARCHER_MARKER = "<!-- ai-news-digest:researcher-workspace v1 -->"
RESEARCHER_TOOLS = {"fetch_rss", "submit_selection"}

results: list[dict[str, Any]] = []


def check(name: str, ok: bool, detail: Any = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail != "" else ""))
    return bool(ok)


class Sender:
    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


def fake_requests() -> list[dict[str, Any]]:
    if not FAKE_LOG.exists():
        return []
    return [json.loads(line)["body"] for line in FAKE_LOG.read_text().splitlines() if line.strip()]


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def main() -> int:
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    tracer = Tracer("app", config.trace_dir())
    db = Path(config.editor_db_path())
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    memory = MemoryStore(db, config.load_digest_defaults())
    sources = config.load_sources()
    researcher = ResearcherClient(config.researcher_url(), tracer, timeout_seconds=60)
    openclaw = OpenClawClient(config.openclaw_url(), config.openclaw_gateway_token(), agent_id="editor",
                              timeout_seconds=120)

    def coordinator(research=researcher.research, client=openclaw) -> CoordinatorService:
        return CoordinatorService(openclaw=client, memory=MemoryStore(db, config.load_digest_defaults()),
                                  tracer=tracer, research=research, sources=sources)

    chat = "integration-chat"
    health = await openclaw.health()
    agents = await openclaw.list_agents()
    check("gateway health + both agent targets", health.get("ok") and {"openclaw/editor", "openclaw/researcher"} <= set(agents),
          agents)

    coord = coordinator()
    coord.new_conversation(chat)

    async def turn(c: CoordinatorService, text: str, command: str | None = None):
        send = Sender()
        t = await c.handle(text, chat, command=command)
        report = await c.deliver(t, send)
        return t, report, send.sent

    # 1. remember a non-default limit
    t, _, sent = await turn(coord, "Remember: I'm interested in AI agents, MCP and AWS Bedrock. At most 2 news items")
    check("remember via OpenClaw -> update_user_settings", t.kind == "reply" and memory.get_settings()[0].max_items == 2,
          sent[:1])

    # 2. digest via /digest: OpenClaw -> request_research (real A2A) -> submit_digest
    t, report, sent = await turn(coord, "/digest", command="digest")
    first_urls = set(report.get("delivered_urls", []))
    check("digest delivered with 2 verified items", t.kind == "digest" and len(first_urls) == 2,
          {"status": report.get("status"), "runs": t.openclaw_run_ids})
    digest_trace = t.trace_id

    # 3. restart (new service objects on the same DB) + /new -> new OpenClaw session, memory read back
    old_session = coord.session_key(chat)
    coord = coordinator()
    coord.new_conversation(chat)
    new_session = coord.session_key(chat)
    t, _, sent = await turn(coord, "What interests and settings have you saved?")
    check("after restart + /new the agent reads limit 2 from SQLite", t.kind == "reply" and "Limit: 2" in sent[0],
          sent[:1])
    check("/new rotated the OpenClaw session", new_session != old_session, [old_session, new_session])

    # 4. another digest skips delivered URLs
    t, report, _ = await turn(coord, "Build another digest and skip the news you already sent")
    second = set(report.get("delivered_urls", []))
    check("second digest has only new links", t.kind == "digest" and second and not (second & first_urls),
          sorted(second))

    # 5. "show the last digest" re-sends the stored digest; counters unchanged
    before = (memory.stats(), sorted(memory.delivered_urls()))
    # asked in Russian on purpose: multilingual input reaches the same resend path
    t, report, sent = await turn(coord, "Покажи последний дайджест")
    check("natural 'show last digest' uses the common full re-send path",
          t.kind == "resend_last" and report.get("status") == "resent" and "Re-sending the saved digest" in sent[0]
          and (memory.stats(), sorted(memory.delivered_urls())) == before, report)

    # 6. forbidden native tool: the model asks OpenClaw for web_fetch; the runtime refuses it
    t, _, sent = await turn(coord, "This is forbidden: read the RSS feed yourself")
    check("forbidden native tool refused, agent recovers with reply", t.kind == "reply", sent[:1])

    # 7. digest flow where the model only wants to answer with text -> no digest
    t, _, sent = await turn(coord, "Digest, but answer in text")
    check("text-only answer to a digest request is not a digest", t.kind == "error" and t.digest is None, sent[:1])

    # 8. hallucinated link is rejected, the agent then submits real links
    t, report, _ = await turn(coord, "Digest, but invent a link")
    check("invented link rejected by the app", t.kind == "digest"
          and all("made-up" not in u for u in report.get("delivered_urls", [])), report.get("status"))

    # 9. Researcher unavailable -> error, no digest, failed trace (wrong A2A address)
    down = ResearcherClient("http://researcher-down.invalid:9100", tracer, timeout_seconds=5, backoff_seconds=0.2)
    t, _, sent = await turn(coordinator(research=down.research), "/digest", command="digest")
    check("researcher down -> honest error, no digest", t.failed and t.digest is None and "Researcher" in sent[0],
          sent[:1])

    # 10. OpenClaw unavailable -> error, no fallback
    dead = OpenClawClient("http://openclaw-down.invalid:18789", config.openclaw_gateway_token())
    t, _, sent = await turn(coordinator(client=dead), "/digest", command="digest")
    check("OpenClaw down -> error, no hidden fallback", t.failed and "OpenClaw" in sent[0], sent[:1])

    await asyncio.sleep(1.0)  # let the plugin flush the last hook events

    # ---- evidence from what OpenClaw sent to the model ------------------------------------------
    all_reqs = fake_requests()
    reqs = [r for r in all_reqs if "researcher" not in str(r.get("model"))]       # editor agent
    rreqs = [r for r in all_reqs if "researcher" in str(r.get("model"))]          # researcher agent
    systems = [text_of(m.get("content")) for r in reqs for m in r.get("messages", []) if m.get("role") == "system"]
    check("model calls were made by OpenClaw for both agents", len(reqs) >= 10 and len(rreqs) >= 4,
          f"editor {len(reqs)}, researcher {len(rreqs)} provider requests")
    check("editor workspace AGENTS.md + SOUL.md in the system prompt",
          systems and all(WORKSPACE_MARKER in s and "Calm, precise and brief" in s for s in systems))
    rsystems = [text_of(m.get("content")) for r in rreqs for m in r.get("messages", []) if m.get("role") == "system"]
    check("researcher workspace AGENTS.md + SOUL.md in its system prompt (not the editor's)",
          rsystems and all(RESEARCHER_MARKER in s and "Methodical and skeptical" in s and WORKSPACE_MARKER not in s
                           for s in rsystems))
    roffered = {t["function"]["name"] for r in rreqs for t in r.get("tools", [])}
    check("researcher model is offered only fetch_rss + submit_selection", roffered == RESEARCHER_TOOLS, sorted(roffered))
    check("research sessions carry no user conversation or Coordinator context",
          rreqs and not any("<user_message>" in json.dumps(r, ensure_ascii=False)
                            or "<saved_settings" in json.dumps(r, ensure_ascii=False) for r in rreqs))
    offered = [sorted(t["function"]["name"] for t in r.get("tools", [])) for r in reqs]
    allowed = set(ALLOWED_TOOL_NAMES)
    check("tools offered to the model are only the Coordinator allowlist",
          all(set(o) <= allowed for o in offered), sorted({n for o in offered for n in o}))
    first_digest = next((r for r in reqs if [t["function"]["name"] for t in r.get("tools", [])] == ["request_research"]),
                        None)
    check("/digest pins request_research (only that tool offered)", first_digest is not None)
    import re

    def user_messages(r: dict[str, Any]) -> list[str]:
        return [u for m in r.get("messages", []) if m.get("role") == "user"
                for u in re.findall(r"<user_message>(.*?)</user_message>", text_of(m.get("content")), re.S)]

    dup = [r for r in reqs if len(user_messages(r)) != len(set(user_messages(r)))]
    check("tool follow-ups do not duplicate a user request in the session transcript", not dup,
          f"{len(dup)} requests with duplicates")
    new_session_reqs = [r for r in reqs if new_session in json.dumps(r, ensure_ascii=False)]
    check("new session after /new carries no earlier conversation",
          new_session_reqs and not any("At most 2 news items" in json.dumps(r, ensure_ascii=False) for r in new_session_reqs))

    # ---- evidence from OpenClaw runtime hooks ----------------------------------------------------
    files = [config.trace_dir() / "app" / "events.jsonl", RESEARCHER_TRACES, OPENCLAW_TRACES]
    events = [e for e in join_openclaw_events(read_events(files)) if e["ts"] >= started_at]  # this run only
    oc = [e for e in events if e.get("service") == "openclaw"]
    llm_inputs = [e for e in oc if e["kind"] == "llm_input"]
    check("OpenClaw hooks recorded runs, model calls and tools",
          {"llm_input", "llm_call", "run", "tool_call"} <= {e["kind"] for e in oc}, f"{len(oc)} hook events")
    check("native OpenClaw tools after policy = [] on every model call",
          llm_inputs and all(e["data"].get("native_tools_after_policy") == [] for e in llm_inputs))
    check("workspace instructions loaded for each agent (hook)",
          {e["agent"] for e in llm_inputs} == {"editor", "researcher"}
          and all(e["data"].get("workspace_instructions_loaded") for e in llm_inputs))
    refused = [e for e in oc if e["kind"] == "tool_call" and e["name"] == "web_fetch" and e["status"] in ("failed", "denied")]
    check("runtime refused web_fetch", bool(refused), refused[0]["error"] if refused else "")
    linked = [e for e in oc if not e["trace_id"].startswith("openclaw-unlinked")]
    check("OpenClaw events joined to app traces (run id or session window)", len(linked) == len(oc), f"{len(linked)}/{len(oc)}")
    digest_events = [e for e in events if e["trace_id"] == digest_trace]
    who = {(e["agent"], e.get("runtime")) for e in digest_events}
    check("one trace holds app, OpenClaw editor, Researcher service and OpenClaw researcher",
          who == {("app", "python"), ("editor", "openclaw"), ("researcher", "python"), ("researcher", "openclaw")},
          sorted(who))
    rcalls = [e for e in digest_events if e["agent"] == "researcher" and e["kind"] == "openclaw_call"
              and e["status"] == "completed"]
    check("Researcher model loop ran through the OpenClaw researcher agent (no in-process LLM)",
          rcalls and all(str(e["data"].get("openclaw_session_key", "")).startswith("research-") for e in rcalls)
          and not any(e["agent"] == "researcher" and e["kind"] == "llm_call" and e.get("runtime") == "python"
                      for e in digest_events), f"{len(rcalls)} Gateway calls")
    a2a = [e for e in digest_events if e["kind"] == "a2a_send" and e["status"] == "completed"]
    check("Coordinator's request_research produced a real A2A task", bool(a2a and a2a[0]["data"].get("task_id")),
          a2a[0]["data"].get("task_id") if a2a else "")

    failed = [r for r in results if not r["ok"]]
    report_path = config.trace_dir() / "openclaw-integration-report.json"
    report_path.write_text(json.dumps({"passed": len(results) - len(failed), "failed": len(failed),
                                       "results": results}, ensure_ascii=False, indent=2, default=str))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; report: {report_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
