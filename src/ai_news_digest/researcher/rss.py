"""RSS/Atom fetching and normalization used by the Researcher's fetch_rss tool."""

from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from ai_news_digest.config import Limits, SourceConfig

USER_AGENT = "ai-news-digest/0.1 (+https://github.com/; RSS reader for a personal digest bot)"

_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_hsenc", "_hsmi",
    "ref", "ref_src", "trk", "sc_channel", "sc_campaign", "sc_medium", "sc_content",
    "sc_country", "sc_geo", "sc_outcome", "sc_publisher", "sc_category", "sc_detail", "sc_segment",
}
_TRACKING_PREFIXES = ("utm_", "mkt_", "pk_", "hsa_")

# Generic AI vocabulary used together with user interests for sources with interest_filter=true.
AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "ml", "llm", "genai", "generative",
    "agent", "agents", "agentic", "mcp", "model context protocol", "bedrock", "sagemaker",
    "foundation model", "claude", "anthropic", "openai", "gpt", "inference", "rag", "embedding",
    "q developer", "kiro", "nova", "copilot", "chatbot", "neural", "transformer", "vector",
]


class FeedFetchError(RuntimeError):
    pass


@dataclass
class FeedEntry:
    source_id: str
    title: str
    url: str
    published_at: datetime | None
    excerpt: str


@dataclass
class FeedStats:
    fetched_entries: int = 0
    in_window: int = 0
    undated_skipped: int = 0
    excluded_delivered: int = 0
    filtered_out_by_interest: int = 0
    passed_to_model: int = 0


@dataclass
class FilteredFeed:
    entries: list[FeedEntry] = field(default_factory=list)
    stats: FeedStats = field(default_factory=FeedStats)


def canonical_url(url: str) -> str:
    """Normalize scheme/host, drop fragments and tracking params, keep article-identifying params."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query, doseq=True), ""))


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")


def clean_html(text: str, limit: int) -> str:
    text = _SCRIPT_RE.sub(" ", text or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut + "…"
    return text


def _entry_datetime(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            # feedparser returns UTC struct_time
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def parse_feed(source: SourceConfig, content: bytes, limits: Limits) -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FeedFetchError(f"not a valid RSS/Atom feed: {parsed.get('bozo_exception')!s:.200}")
    if not parsed.version and not parsed.entries:
        raise FeedFetchError("response is not RSS/Atom")
    entries: list[FeedEntry] = []
    for raw in parsed.entries:
        link = raw.get("link") or ""
        title = clean_html(raw.get("title") or "", 300)
        if not link or not title:
            continue
        try:
            url = canonical_url(link)
        except ValueError:
            continue
        if len(url) > 2048:
            continue
        summary = raw.get("summary") or ""
        if not summary and raw.get("content"):
            summary = raw["content"][0].get("value", "")
        entries.append(FeedEntry(
            source_id=source.id, title=title, url=url, published_at=_entry_datetime(raw),
            excerpt=clean_html(summary, limits.max_excerpt_chars),
        ))
    return entries


async def fetch_feed_bytes(source: SourceConfig, client: httpx.AsyncClient, limits: Limits) -> bytes:
    try:
        async with client.stream("GET", source.url, headers={"User-Agent": USER_AGENT},
                                 timeout=limits.http_timeout_seconds, follow_redirects=True) as response:
            if response.status_code != 200:
                raise FeedFetchError(f"HTTP {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limits.max_feed_bytes:
                    raise FeedFetchError(f"response larger than {limits.max_feed_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise FeedFetchError(f"timeout after {limits.http_timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"network error: {type(exc).__name__}") from exc


def matches_interests(entry: FeedEntry, interests: Iterable[str]) -> bool:
    haystack = f" {entry.title} {entry.excerpt} ".lower()
    haystack = re.sub(r"[^\w\s]", " ", haystack)
    terms = [t.lower() for t in interests] + AI_KEYWORDS
    for term in terms:
        term = re.sub(r"[^\w\s]", " ", term).strip()
        if term and re.search(rf"\b{re.escape(term)}\b", haystack):
            return True
    return False


def filter_entries(
    entries: list[FeedEntry],
    *,
    source: SourceConfig,
    now: datetime,
    period_hours: int,
    interests: list[str],
    exclude_urls: set[str],
    seen_urls: set[str],
) -> FilteredFeed:
    """Apply time window, delivered/duplicate exclusion, interest filter and per-source cap."""
    window_start = now - timedelta(hours=period_hours)
    future_tolerance = now + timedelta(minutes=10)
    out = FilteredFeed()
    out.stats.fetched_entries = len(entries)
    candidates: list[FeedEntry] = []
    for entry in entries:
        if entry.published_at is None:
            out.stats.undated_skipped += 1
            continue
        if not (window_start <= entry.published_at <= future_tolerance):
            continue
        out.stats.in_window += 1
        if entry.url in exclude_urls:
            out.stats.excluded_delivered += 1
            continue
        if entry.url in seen_urls:
            continue
        if source.interest_filter and not matches_interests(entry, interests):
            out.stats.filtered_out_by_interest += 1
            continue
        seen_urls.add(entry.url)
        candidates.append(entry)
    candidates.sort(key=lambda e: e.published_at, reverse=True)  # type: ignore[arg-type,return-value]
    out.entries = candidates[: source.max_entries]
    out.stats.passed_to_model = len(out.entries)
    return out
