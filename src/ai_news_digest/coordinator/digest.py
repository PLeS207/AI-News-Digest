"""Rendering a Digest into Telegram HTML messages.

Messages are assembled from self-contained HTML lines (every tag opened in a line is closed in
that line), so a split never produces unclosed tags or broken entities and nothing is cut with
``[:4096]``. A news item stays in one message when it fits; otherwise it is split between lines.
Each chunk records the URLs whose link line it carries, which drives delivery accounting.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from ai_news_digest.models import Digest, DigestItem

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800  # headroom under Telegram's 4096-character limit
MAX_LINK_TEXT = 120

LABELS = {
    "ru": {
        "title": "🗞 AI-дайджест за последние {hours} ч",
        "count": "Новостей: {n}",
        "published": "Опубликовано",
        "application": "Возможное применение для Cloud Architect / DevOps (вывод агента)",
        "source_note": ("ℹ️ Саммари составлены по тексту RSS-анонсов; полные статьи не читались. "
                        "Рекомендации о применении — вывод агента."),
        "no_excerpt": "в RSS нет описания — саммари по заголовку",
        "empty": "За последние {hours} ч релевантных новостей не найдено.",
        "incomplete": "⚠️ Результат неполный: {reasons}",
        "source_error": "источник {sid}: {err}",
        "resent": "↩️ Повтор сохранённого дайджеста от {date}",
    },
    "en": {
        "title": "🗞 AI digest for the last {hours} h",
        "count": "Items: {n}",
        "published": "Published",
        "application": "Possible use for a Cloud Architect / DevOps (agent's inference)",
        "source_note": ("ℹ️ Summaries are based on RSS announcement text; full articles were not read. "
                        "Application notes are the agent's inference."),
        "no_excerpt": "RSS has no description — summary based on the title",
        "empty": "No relevant news found for the last {hours} h.",
        "incomplete": "⚠️ Incomplete result: {reasons}",
        "source_error": "source {sid}: {err}",
        "resent": "↩️ Re-sending the saved digest from {date}",
    },
}


def labels(language: str) -> dict[str, str]:
    return LABELS.get(language.lower()[:2], LABELS["en"])


@dataclass
class Chunk:
    text: str
    items: list[tuple[str, str]] = field(default_factory=list)  # (url, title) whose link is in this chunk


@dataclass
class _Line:
    html: str
    url: tuple[str, str] | None = None  # set on the link line of an item


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def _split_plain(text: str, limit: int) -> list[str]:
    """Split plain (unescaped) text into pieces whose escaped form fits `limit`."""
    pieces: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if len(_e(candidate)) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        while len(_e(word)) > limit:  # a single enormous token
            cut = limit // 6
            pieces.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        pieces.append(current)
    return pieces


def _text_lines(prefix_html: str, text: str, limit: int) -> list[_Line]:
    """`prefix_html` + escaped text, split into self-contained lines below `limit`."""
    room = max(limit - len(prefix_html) - 4, 200)
    parts = _split_plain(text, room)
    out = [_Line(prefix_html + _e(parts[0]) if parts else prefix_html)]
    out.extend(_Line(_e(p)) for p in parts[1:])
    return out


def _link_line(url: str, title: str) -> _Line:
    shown = url if len(url) <= MAX_LINK_TEXT else url[: MAX_LINK_TEXT - 1] + "…"
    return _Line(f'🔗 <a href="{html.escape(url, quote=True)}">{_e(shown)}</a>', url=(url, title))


def item_lines(index: int, item: DigestItem, lb: dict[str, str], limit: int = CHUNK_LIMIT) -> list[_Line]:
    published = item.published_at.strftime("%Y-%m-%d %H:%M UTC")
    no_excerpt = f" · <i>{_e(lb['no_excerpt'])}</i>" if item.excerpt_is_empty else ""
    lines = [_Line(f"<b>{_e(part)}</b>") for part in _split_plain(f"{index}. {item.title}", limit - 10)]
    lines.append(_Line(f"🗓 {lb['published']}: {published} · {_e(item.source_name)}{no_excerpt}"))
    lines.extend(_text_lines("", item.summary, limit))
    lines.extend(_text_lines(f"💡 <i>{_e(lb['application'])}:</i> ", item.application, limit))
    lines.append(_link_line(item.url, item.title))
    return lines


def _pack(blocks: list[list[_Line]], limit: int) -> list[Chunk]:
    """Pack blocks (lists of lines) into chunks; blocks are kept whole when they fit."""
    chunks: list[Chunk] = []
    current: list[str] = []
    current_urls: list[tuple[str, str]] = []
    size = 0

    def flush() -> None:
        nonlocal current, current_urls, size
        if current:
            chunks.append(Chunk(text="\n".join(current).strip("\n"), items=current_urls))
        current, current_urls, size = [], [], 0

    for block in blocks:
        block_text = "\n".join(line.html for line in block)
        separator = 2 if current else 0
        if size + separator + len(block_text) <= limit:
            if current:
                current.append("")
            current.extend(line.html for line in block)
            current_urls.extend(line.url for line in block if line.url)
            size += separator + len(block_text)
            continue
        if len(block_text) <= limit:
            flush()
            current.extend(line.html for line in block)
            current_urls.extend(line.url for line in block if line.url)
            size = len(block_text)
            continue
        # A single oversized block: split it between its self-contained lines.
        flush()
        for line in block:
            if size + 1 + len(line.html) > limit and current:
                flush()
            current.append(line.html)
            if line.url:
                current_urls.append(line.url)
            size += 1 + len(line.html)
    flush()
    return chunks


def render_chunks(digest: Digest, *, resent_on: str | None = None, limit: int = CHUNK_LIMIT) -> list[Chunk]:
    lb = labels(digest.language)
    header: list[_Line] = []
    if resent_on:
        header.append(_Line(f"<i>{_e(lb['resent'].format(date=resent_on))}</i>"))
    header.append(_Line(f"<b>{_e(lb['title'].format(hours=digest.period_hours))}</b>"))
    if digest.items:
        header.append(_Line(_e(lb["count"].format(n=len(digest.items)))))
    if digest.intro:
        header.extend(_text_lines("", digest.intro, limit))

    footer: list[_Line] = []
    if digest.research_incomplete or digest.source_errors:
        errs = "; ".join(lb["source_error"].format(sid=e.source_id, err=e.error) for e in digest.source_errors)
        footer.extend(_text_lines("", lb["incomplete"].format(reasons=errs or "—"), limit))
    if digest.items:
        footer.append(_Line(_e(lb["source_note"])))

    blocks: list[list[_Line]] = [header]
    if not digest.items:
        blocks.append(_text_lines("", digest.empty_reason or lb["empty"].format(hours=digest.period_hours), limit))
    for i, item in enumerate(digest.items, start=1):
        blocks.append(item_lines(i, item, lb, limit))
    if footer:
        blocks.append(footer)
    chunks = _pack(blocks, limit)
    assert all(len(c.text) <= TELEGRAM_LIMIT for c in chunks)
    return chunks


def render_text(digest: Digest) -> str:
    return "\n\n".join(c.text for c in render_chunks(digest))
