"""Rendering a Digest into Telegram HTML messages, split into chunks that track their URLs."""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from ai_news_digest.models import Digest

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800  # headroom under Telegram's 4096-char message limit

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
    },
}


def labels(language: str) -> dict[str, str]:
    return LABELS.get(language.lower()[:2], LABELS["en"])


@dataclass
class Chunk:
    text: str
    items: list[tuple[str, str]] = field(default_factory=list)  # (url, title) included in this chunk


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def render_item(index: int, item, lb: dict[str, str]) -> str:
    published = item.published_at.strftime("%Y-%m-%d %H:%M UTC")
    no_excerpt = f" · <i>{_e(lb['no_excerpt'])}</i>" if item.excerpt_is_empty else ""
    return (
        f"<b>{index}. {_e(item.title)}</b>\n"
        f"🗓 {lb['published']}: {published} · {_e(item.source_name)}{no_excerpt}\n"
        f"{_e(item.summary)}\n"
        f"💡 <i>{_e(lb['application'])}:</i> {_e(item.application)}\n"
        f"🔗 <a href=\"{html.escape(item.url, quote=True)}\">{_e(item.url)}</a>"
    )


def render_chunks(digest: Digest) -> list[Chunk]:
    lb = labels(digest.language)
    header_lines = [f"<b>{_e(lb['title'].format(hours=digest.period_hours))}</b>"]
    if digest.items:
        header_lines.append(lb["count"].format(n=len(digest.items)))
    if digest.intro:
        header_lines.append(_e(digest.intro))
    footer_lines: list[str] = []
    if digest.research_incomplete or digest.source_errors:
        errs = "; ".join(lb["source_error"].format(sid=e.source_id, err=e.error) for e in digest.source_errors)
        footer_lines.append(_e(lb["incomplete"].format(reasons=errs or "—")))
    if digest.items:
        footer_lines.append(_e(lb["source_note"]))

    if not digest.items:
        body = digest.empty_reason or lb["empty"].format(hours=digest.period_hours)
        text = "\n\n".join(["\n".join(header_lines), _e(body), *footer_lines])
        return [Chunk(text=text[:TELEGRAM_LIMIT])]

    chunks: list[Chunk] = []
    current = Chunk(text="\n".join(header_lines))
    for i, item in enumerate(digest.items, start=1):
        block = render_item(i, item, lb)
        candidate = f"{current.text}\n\n{block}" if current.text else block
        if len(candidate) > CHUNK_LIMIT and current.text:
            chunks.append(current)
            current = Chunk(text=block)
        else:
            current.text = candidate
        current.items.append((item.url, item.title))
    footer = "\n\n".join(footer_lines)
    if footer:
        if len(current.text) + len(footer) + 2 > CHUNK_LIMIT:
            chunks.append(current)
            current = Chunk(text=footer)
        else:
            current.text = f"{current.text}\n\n{footer}"
    chunks.append(current)
    for chunk in chunks:
        chunk.text = chunk.text[:TELEGRAM_LIMIT]
    return chunks


def render_text(digest: Digest) -> str:
    return "\n\n".join(c.text for c in render_chunks(digest))
