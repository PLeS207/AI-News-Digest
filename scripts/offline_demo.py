"""OFFLINE demo: fills trace files so the dashboard can be explored without credentials.

Uses a FAKE Gateway (instead of OpenClaw), a FAKE Researcher LLM and local RSS fixtures, but a REAL
A2A server/client over HTTP on localhost. It does not prove that OpenClaw, the real model or
Telegram work; for the real OpenClaw runtime use scripts/openclaw_integration_check.py.

    python scripts/offline_demo.py --out data/offline-demo
    TRACE_FILES=data/offline-demo/traces/app/events.jsonl,data/offline-demo/traces/researcher/events.jsonl \
        streamlit run dashboard/app.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import httpx  # noqa: E402

from ai_news_digest.agents.llm import ScriptedBackend  # noqa: E402
from ai_news_digest.config import DigestDefaults, Limits, SourceConfig  # noqa: E402
from ai_news_digest.coordinator.memory import MemoryStore  # noqa: E402
from ai_news_digest.coordinator.openclaw_client import OpenClawClient  # noqa: E402
from ai_news_digest.coordinator.researcher_client import ResearcherClient  # noqa: E402
from ai_news_digest.coordinator.service import CoordinatorService  # noqa: E402
from ai_news_digest.researcher.agent import Researcher  # noqa: E402
from ai_news_digest.testing.fakes import FakeGateway, researcher_script  # noqa: E402
from ai_news_digest.tracing import Tracer  # noqa: E402
from conftest import NOW, feed_handler  # noqa: E402
from test_a2a_roundtrip import running_researcher  # noqa: E402

TOKEN = "offline-demo-token-0123456789abcdef"


async def main(out: Path) -> None:
    sources = [
        SourceConfig(id="openai_news", name="OpenAI News", url="https://feeds.test/openai.xml"),
        SourceConfig(id="huggingface_blog", name="Hugging Face Blog", url="https://feeds.test/hf.xml"),
        SourceConfig(id="aws_whats_new", name="AWS What's New", url="https://feeds.test/aws.xml", interest_filter=True),
        SourceConfig(id="broken_source", name="Broken (demo)", url="https://feeds.test/broken.xml"),
    ]
    app_tracer = Tracer("app", out / "traces")
    researcher_tracer = Tracer("researcher", out / "traces")
    researcher = Researcher(llm=ScriptedBackend(researcher_script, "fake-researcher"), sources=sources,
                            limits=Limits(), tracer=researcher_tracer,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(feed_handler)))
    memory = MemoryStore(out / "memory.sqlite3", DigestDefaults())
    gateway = OpenClawClient("http://fake-gateway", TOKEN, transport=FakeGateway(TOKEN).transport())

    async def send(text: str) -> None:
        print("--- bot ->\n" + text + "\n")

    async with running_researcher(researcher, researcher_tracer) as url:
        client = ResearcherClient(url, app_tracer, timeout_seconds=30)

        async def research(req):
            return await client.research(req.model_copy(update={"now": NOW}))

        coord = CoordinatorService(openclaw=gateway, memory=memory, tracer=app_tracer, research=research,
                                   sources=sources)
        for msg in ["Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Пиши по-русски, максимум 2 новости",
                    "Подготовь дайджест AI-новостей за последние 72 часа"]:
            print(f">>> user: {msg}")
            await coord.deliver(await coord.handle(msg, "demo"), send)
        coord.new_conversation("demo")
        for msg in ["Какие интересы и настройки ты сохранил?",
                    "Собери ещё один дайджест, пропусти уже отправленные новости"]:
            print(f">>> user: {msg}")
            await coord.deliver(await coord.handle(msg, "demo"), send)

    down = ResearcherClient(url, app_tracer, timeout_seconds=5, backoff_seconds=0.2)  # server is stopped now
    coord.registry.ctx.research = down.research
    msg = "Подготовь дайджест ещё раз"
    print(f">>> user: {msg}  (researcher stopped)")
    await coord.deliver(await coord.handle(msg, "demo"), send)
    print(f"Traces written to {out / 'traces'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("data/offline-demo"))
    asyncio.run(main(parser.parse_args().out))
