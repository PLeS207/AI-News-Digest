from datetime import timezone

import pytest

from ai_news_digest.config import SourceConfig
from ai_news_digest.models import ResearchRequest
from ai_news_digest.researcher.agent import Researcher
from ai_news_digest.researcher.rss import (
    FeedFetchError,
    canonical_url,
    clean_html,
    fetch_feed_bytes,
    filter_entries,
    parse_feed,
)
from conftest import FIXTURES, NOW


def _src(**kw):
    base = dict(id="s", name="S", url="https://feeds.test/x")
    base.update(kw)
    return SourceConfig(**base)


def test_canonical_url_strips_tracking_but_keeps_article_params():
    assert canonical_url("https://OpenAI.com/index/a/?utm_source=rss&utm_medium=x#frag") == "https://openai.com/index/a"
    assert canonical_url("https://huggingface.co/blog/leaderboard?id=42&fbclid=abc") == \
        "https://huggingface.co/blog/leaderboard?id=42"
    assert canonical_url("https://example.com/posts/llm-eval?utm_term=a&page=2") == \
        "https://example.com/posts/llm-eval?page=2"
    with pytest.raises(ValueError):
        canonical_url("javascript:alert(1)")


def test_clean_html_and_excerpt_limit():
    text = clean_html("<p>Hello&nbsp;<b>world</b></p><script>evil()</script>" + " word" * 300, 100)
    assert "<" not in text and "evil" not in text
    assert text.startswith("Hello world")
    assert len(text) <= 101 and text.endswith("…")


def test_parse_rss_dates_in_utc(limits):
    entries = parse_feed(_src(id="openai_news"), (FIXTURES / "openai_news.xml").read_bytes(), limits)
    assert len(entries) == 4
    first = entries[0]
    assert first.url == "https://openai.com/index/agent-tooling"
    assert first.published_at.tzinfo == timezone.utc and first.published_at.hour == 7
    assert "AI agents" in first.excerpt and "<b>" not in first.excerpt
    assert entries[3].published_at is None


def test_parse_atom_with_offset_timezone(limits):
    entries = parse_feed(_src(), (FIXTURES / "atom_feed.xml").read_bytes(), limits)
    assert entries[0].published_at.isoformat() == "2026-10-10T06:00:00+00:00"
    assert entries[0].url == "https://example.com/posts/llm-eval?page=2"


def test_not_a_feed_is_rejected(limits):
    with pytest.raises(FeedFetchError):
        parse_feed(_src(), (FIXTURES / "not_a_feed.html").read_bytes(), limits)


def test_time_window_undated_and_duplicates(limits):
    src = _src(id="openai_news")
    entries = parse_feed(src, (FIXTURES / "openai_news.xml").read_bytes(), limits)
    out = filter_entries(entries, source=src, now=NOW, period_hours=72, interests=["MCP"],
                         exclude_urls=set(), seen_urls=set())
    urls = [e.url for e in out.entries]
    assert urls == ["https://openai.com/index/agent-tooling", "https://openai.com/index/incident-triage"]
    assert out.stats.undated_skipped == 1
    assert out.stats.in_window == 2

    hf = _src(id="huggingface_blog")
    hf_entries = parse_feed(hf, (FIXTURES / "huggingface_blog.xml").read_bytes(), limits)
    out = filter_entries(hf_entries, source=hf, now=NOW, period_hours=72, interests=["MCP"],
                         exclude_urls=set(), seen_urls=set())
    assert [e.url for e in out.entries] == ["https://huggingface.co/blog/mcp-smolagents",
                                            "https://huggingface.co/blog/leaderboard?id=42"]

    out24 = filter_entries(hf_entries, source=hf, now=NOW, period_hours=24, interests=["MCP"],
                           exclude_urls=set(), seen_urls=set())
    assert len(out24.entries) == 1


def test_delivered_urls_are_excluded(limits):
    src = _src(id="openai_news")
    entries = parse_feed(src, (FIXTURES / "openai_news.xml").read_bytes(), limits)
    out = filter_entries(entries, source=src, now=NOW, period_hours=72, interests=["MCP"],
                         exclude_urls={"https://openai.com/index/agent-tooling"}, seen_urls=set())
    assert [e.url for e in out.entries] == ["https://openai.com/index/incident-triage"]
    assert out.stats.excluded_delivered == 1


def test_aws_interest_filter(limits):
    src = _src(id="aws_whats_new", interest_filter=True)
    entries = parse_feed(src, (FIXTURES / "aws_whats_new.xml").read_bytes(), limits)
    out = filter_entries(entries, source=src, now=NOW, period_hours=72, interests=["AWS Bedrock", "MCP"],
                         exclude_urls=set(), seen_urls=set())
    titles = [e.title for e in out.entries]
    assert any("Bedrock" in t for t in titles) and any("SageMaker" in t for t in titles)
    assert not any("EC2" in t or "RDS" in t for t in titles)
    assert out.stats.filtered_out_by_interest == 2


async def test_http_errors_become_feed_errors(feed_http, limits):
    with pytest.raises(FeedFetchError, match="HTTP 503"):
        await fetch_feed_bytes(_src(url="https://feeds.test/broken.xml"), feed_http, limits)
    with pytest.raises(FeedFetchError, match="timeout"):
        await fetch_feed_bytes(_src(url="https://feeds.test/slow.xml"), feed_http, limits)
    small = limits.__class__(max_feed_bytes=100)
    with pytest.raises(FeedFetchError, match="larger than"):
        await fetch_feed_bytes(_src(url="https://feeds.test/openai.xml"), feed_http, small)


async def test_researcher_reports_source_error_and_partial_result(
        feed_http, limits, researcher_llm, researcher_tracer, sources):
    sources = sources[:2] + [SourceConfig(id="broken", name="Broken", url="https://feeds.test/broken.xml")]
    researcher = Researcher(llm=researcher_llm, sources=sources, limits=limits, tracer=researcher_tracer,
                            http_client=feed_http)
    req = ResearchRequest(request_id="req-00000001", trace_id="tr-00000001", period_hours=72,
                          interests=["MCP"], max_items=5, now=NOW)
    result = await researcher.research(req)
    assert result.incomplete is True
    assert [e.source_id for e in result.source_errors] == ["broken"]
    assert "HTTP 503" in result.source_errors[0].error
    urls = {a.url for a in result.articles}
    assert "https://openai.com/index/agent-tooling" in urls
    assert "https://huggingface.co/blog/mcp-smolagents" in urls
    assert all(a.published_at >= NOW.replace(day=7) for a in result.articles)
    hf = next(a for a in result.articles if a.source_id == "huggingface_blog")
    assert hf.excerpt_is_empty is True


async def test_rss_tool_rejects_unknown_source(feed_http, limits, researcher_tracer, sources):
    from ai_news_digest.agents.llm import ScriptedBackend, fake_response, tool_use

    calls = []

    def script(system, messages, tools):
        calls.append(list(messages))
        if len(calls) == 1:
            return fake_response(tool_use("fetch_rss", {"source_id": "https://evil.example/feed"}))
        return fake_response(tool_use("submit_selection", {"selected": []}))

    researcher = Researcher(llm=ScriptedBackend(script), sources=sources, limits=limits,
                            tracer=researcher_tracer, http_client=feed_http)
    req = ResearchRequest(request_id="req-00000002", trace_id="tr-00000002", period_hours=72,
                          interests=["MCP"], max_items=3, now=NOW)
    result = await researcher.research(req)
    error_block = calls[1][-1]["content"][0]
    assert error_block["is_error"] and "unknown source_id" in error_block["content"]
    assert result.articles == [] and result.incomplete  # nothing fetched -> flagged incomplete
