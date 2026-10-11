"""Shared offline test fixtures: fixed clock, fixture feeds via httpx.MockTransport, fake OpenClaw Gateway."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_news_digest.openclaw_client import OpenClawClient
from ai_news_digest.testing.fakes import FakeGateway
from ai_news_digest.config import DigestDefaults, Limits, SourceConfig
from ai_news_digest.tracing import Tracer

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
GATEWAY_TOKEN = "test-gateway-token-0123456789abcdef"

FEED_FILES = {
    "https://feeds.test/openai.xml": "openai_news.xml",
    "https://feeds.test/hf.xml": "huggingface_blog.xml",
    "https://feeds.test/aws.xml": "aws_whats_new.xml",
    "https://feeds.test/atom.xml": "atom_feed.xml",
    "https://feeds.test/html": "not_a_feed.html",
}


@pytest.fixture
def sources() -> list[SourceConfig]:
    return [
        SourceConfig(id="openai_news", name="OpenAI News", url="https://feeds.test/openai.xml"),
        SourceConfig(id="huggingface_blog", name="Hugging Face Blog", url="https://feeds.test/hf.xml"),
        SourceConfig(id="aws_whats_new", name="AWS What's New", url="https://feeds.test/aws.xml",
                     interest_filter=True),
    ]


@pytest.fixture
def limits() -> Limits:
    return Limits()


@pytest.fixture
def defaults() -> DigestDefaults:
    return DigestDefaults()


def feed_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == "https://feeds.test/broken.xml":
        return httpx.Response(503, text="unavailable")
    if url == "https://feeds.test/slow.xml":
        raise httpx.ReadTimeout("timeout", request=request)
    name = FEED_FILES.get(url)
    if name is None:
        return httpx.Response(404)
    return httpx.Response(200, content=(FIXTURES / name).read_bytes(),
                          headers={"content-type": "application/rss+xml"})


@pytest.fixture
def feed_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(feed_handler))


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer("app", tmp_path / "traces")


@pytest.fixture
def researcher_tracer(tmp_path: Path) -> Tracer:
    return Tracer("researcher", tmp_path / "traces")


def researcher_client(transport: httpx.AsyncBaseTransport) -> OpenClawClient:
    """Client of the OpenClaw `researcher` agent over the given (fake) Gateway transport."""
    return OpenClawClient("http://openclaw.test:18789", GATEWAY_TOKEN, agent_id="researcher", transport=transport)


@pytest.fixture
def researcher_openclaw() -> OpenClawClient:
    """The Researcher's model agent behind a FAKE Gateway (scripted: fetch every source, select every item)."""
    return researcher_client(FakeGateway(token=GATEWAY_TOKEN, agent_id="researcher").transport())


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway(token=GATEWAY_TOKEN)


def make_coordinator(tmp_path, tracer, sources, defaults, research, gateway=None, transport=None,
                     db_name="memory.sqlite3"):
    from ai_news_digest.coordinator.memory import MemoryStore
    from ai_news_digest.coordinator.service import CoordinatorService

    memory = MemoryStore(tmp_path / db_name, defaults)
    client = OpenClawClient("http://openclaw.test:18789", GATEWAY_TOKEN,
                            transport=transport or (gateway.transport() if gateway else None))
    return CoordinatorService(openclaw=client, memory=memory, tracer=tracer, research=research,
                              sources=sources), memory
