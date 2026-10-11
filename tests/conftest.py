"""Shared offline test fixtures: fixed clock, fixture feeds via httpx.MockTransport, fake LLMs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news_digest.agents.llm import ScriptedBackend, fake_response, tool_use
from ai_news_digest.config import DigestDefaults, Limits, SourceConfig
from ai_news_digest.tracing import Tracer

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

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
    return Tracer("editor", tmp_path / "traces")


@pytest.fixture
def researcher_tracer(tmp_path: Path) -> Tracer:
    return Tracer("researcher", tmp_path / "traces")


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_result" and not block.get("is_error"):
                    out.append(json.loads(block["content"]))
    return out


def researcher_script(system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """Fake Researcher LLM: fetch every listed source, then select every returned item."""
    results = _tool_results(messages)
    if not results:
        task = messages[0]["content"]
        ids = re.findall(r"^- ([a-z0-9_]+):", task, flags=re.M)
        return fake_response({"type": "text", "text": "Fetching sources."},
                             *[tool_use("fetch_rss", {"source_id": sid}) for sid in ids])
    max_sel = int(re.search(r"Select at most (\d+)", messages[0]["content"]).group(1))
    items = [i for r in results for i in r.get("items", [])][:max_sel]
    return fake_response(tool_use("submit_selection", {
        "selected": [{"item_id": i["item_id"], "relevance_reason": f"matches interests: {i['title'][:40]}"}
                     for i in items],
    }))


@pytest.fixture
def researcher_llm() -> ScriptedBackend:
    return ScriptedBackend(researcher_script, model="fake-researcher")


def editor_script(system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """Fake Editor LLM that mimics tool usage for the demo scenario."""
    last_user = next(m for m in reversed(messages) if m["role"] == "user" and isinstance(m["content"], str))
    text = last_user["content"].split("<user_message>")[-1].lower()
    results = _tool_results(messages[messages.index(last_user):])
    errors = [b for m in messages[messages.index(last_user):] if m["role"] == "user" and isinstance(m["content"], list)
              for b in m["content"] if b.get("is_error")]

    if "запомни" in text:
        if results:
            return fake_response({"type": "text", "text": "Запомнил: максимум 2 новости."})
        return fake_response(tool_use("update_user_settings", {
            "interests": ["AI agents", "MCP", "AWS Bedrock"], "language": "ru", "max_items": 2}))
    if "настройки" in text:
        if results:
            s = results[-1]
            return fake_response({"type": "text", "text": f"Лимит: {s['max_items']}, интересы: {', '.join(s['interests'])}"})
        return fake_response(tool_use("get_user_settings", {}))
    if "последний" in text:
        return fake_response(tool_use("show_last_digest", {}))
    if "запрещ" in text:  # tries to use a Researcher-only tool
        if errors:
            return fake_response({"type": "text", "text": "У меня нет доступа к RSS."})
        return fake_response(tool_use("fetch_rss", {"source_id": "openai_news"}))
    if "дайджест" in text:
        if errors and not results:
            return fake_response({"type": "text", "text": "Researcher недоступен."})
        if not results:
            return fake_response(tool_use("request_research", {}))
        research = results[-1]
        if "error" in research:
            return fake_response({"type": "text", "text": "Researcher недоступен."})
        if "выдума" in text and not errors:
            return fake_response(tool_use("submit_digest", {"items": [
                {"url": "https://example.com/made-up", "summary": "x", "application": "y"}]}))
        items = [{"url": a["url"], "summary": f"Кратко: {a['title']}. Основано на RSS-анонсе.",
                  "application": "Можно оценить для платформенной команды."}
                 for a in research["articles"][: research["max_items"]]]
        return fake_response(tool_use("submit_digest", {"intro": "Свежие новости", "items": items}))
    return fake_response({"type": "text", "text": "Готов помочь с дайджестом."})


@pytest.fixture
def editor_llm() -> ScriptedBackend:
    return ScriptedBackend(editor_script, model="fake-editor")
