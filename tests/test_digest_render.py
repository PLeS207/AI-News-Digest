"""Telegram HTML rendering, chunking, full re-send and partial-delivery accounting."""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser

from ai_news_digest.coordinator.delivery import deliver_digest, resend_last_digest
from ai_news_digest.coordinator.digest import TELEGRAM_LIMIT, render_chunks
from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.models import Digest, DigestItem

ALLOWED_TAGS = {"b", "i", "a"}


class TagChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unexpected <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")

    def handle_data(self, data):
        if "<" in data or ">" in data:
            self.errors.append(f"raw angle bracket in {data[:30]!r}")

    def handle_entityref(self, name):
        if name not in {"amp", "lt", "gt", "quot"}:
            self.errors.append(f"bad entity &{name};")


def assert_valid_telegram_html(text: str) -> None:
    assert len(text) <= TELEGRAM_LIMIT
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors and not checker.stack, (checker.errors, checker.stack)


def _item(i: int, **kw) -> DigestItem:
    data = dict(source_id="openai_news", source_name="OpenAI <News> & Co", title=f"Title {i} <b>&amp;",
                url=f"https://a.test/{i}?q=1&x=<y>", published_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
                summary="Summary & <tags> " * 10, application="Use it & check <it>.")
    data.update(kw)
    return DigestItem(**data)


def long_digest() -> Digest:
    items = [_item(i) for i in range(4)]
    items.append(_item(4, summary=("Очень длинное саммари & <x> " * 40)[:900],
                       application=("Применение & <y> " * 40)[:500],
                       title=("Длинный заголовок & <z> " * 30)[:400]))
    items.append(_item(5, url="https://very.long.test/" + "segment-&-<>/" * 150))
    items.extend(_item(i, summary="S" * 880, application="A" * 480) for i in range(6, 10))
    return Digest(trace_id="tr-long", language="ru", period_hours=72, intro="Intro & <intro>", items=items)


def test_long_digest_is_split_into_valid_html_messages_without_truncation():
    digest = long_digest()
    chunks = render_chunks(digest, limit=1500)  # small limit forces splits inside a long item
    assert len(chunks) > 3
    for chunk in chunks:
        assert_valid_telegram_html(chunk.text)
    joined = "\n".join(c.text for c in chunks)
    for item in digest.items:  # every summary word and every link survives
        assert f'href="{item.url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}"' in joined
    assert joined.count("💡") == len(digest.items)
    # each URL is accounted to exactly one message (the one carrying its link)
    urls = [u for c in chunks for u, _ in c.items]
    assert sorted(urls) == sorted(i.url for i in digest.items)
    assert "&amp;" in joined and "&lt;" in joined and "<z>" not in joined
    # default Telegram-sized chunks are valid too
    for chunk in render_chunks(digest):
        assert_valid_telegram_html(chunk.text)


async def test_resend_uses_full_digest_through_both_paths(tmp_path, tracer, defaults):
    memory = MemoryStore(tmp_path / "m.sqlite3", defaults)
    digest = long_digest()
    first: list[str] = []

    async def send_first(text):
        first.append(text)

    report = await deliver_digest(digest, send=send_first, memory=memory, tracer=tracer, trace_id="tr-1")
    assert report["status"] == "delivered" and report["total_chunks"] >= 2
    before = (memory.stats(), sorted(memory.delivered_urls()))

    resent: list[str] = []

    async def send_again(text):
        resent.append(text)

    out = await resend_last_digest(send=send_again, memory=memory, tracer=tracer, trace_id="tr-2")
    assert out["status"] == "resent" and out["sent_chunks"] == len(resent) >= report["total_chunks"]
    for text in resent:
        assert_valid_telegram_html(text)
    for item in digest.items:
        assert any(item.title.split()[0] in t for t in resent)
    assert (memory.stats(), sorted(memory.delivered_urls())) == before  # no new digest, counters unchanged


async def test_partial_delivery_marks_only_sent_chunks(tmp_path, tracer, defaults):
    memory = MemoryStore(tmp_path / "m.sqlite3", defaults)
    digest = long_digest()
    attempts = 0

    async def flaky(text):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("telegram send failed")

    report = await deliver_digest(digest, send=flaky, memory=memory, tracer=tracer, trace_id="tr-p")
    assert report["status"] == "partial" and report["sent_chunks"] == 1
    delivered = set(memory.delivered_urls())
    assert delivered == set(report["delivered_urls"]) and 0 < len(delivered) < len(digest.items)
    assert not memory.is_delivered(digest.items[-1].url)

    memory2 = MemoryStore(tmp_path / "m2.sqlite3", defaults)

    async def broken(text):
        raise RuntimeError("down")

    report = await deliver_digest(digest, send=broken, memory=memory2, tracer=tracer, trace_id="tr-f")
    assert report["status"] == "failed" and memory2.delivered_urls() == []
    assert memory2.last_digest() is None  # a failed digest is not offered for re-send
