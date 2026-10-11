"""Application persistent memory in SQLite: settings, digest history, delivered URLs, conversations.

Only the `app` process opens this database (the OpenClaw Coordinator reads/writes it exclusively
through backend tools; the Researcher never sees it). A single allowed Telegram user is served, so the
settings row uses a fixed key ("owner") and no Telegram identifiers are stored.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_news_digest.config import DigestDefaults
from ai_news_digest.models import Digest, UserSettings

OWNER = "owner"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    owner TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    period_hours INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    delivered_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    data TEXT NOT NULL
);
-- added in the OpenClaw migration; CREATE IF NOT EXISTS keeps existing databases compatible
CREATE TABLE IF NOT EXISTS conversations (
    chat_key TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivered_urls (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    digest_id INTEGER REFERENCES digests(id),
    delivered_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: str | Path, defaults: DigestDefaults):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults = defaults
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:  # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    # ---- settings -------------------------------------------------------------------------
    def default_settings(self) -> UserSettings:
        d = self.defaults
        return UserSettings(interests=list(d.interests), language=d.language, max_items=d.max_items,
                            period_hours=d.period_hours)

    def get_settings(self) -> tuple[UserSettings, bool]:
        """Return (settings, is_stored). Defaults are returned (not stored) when nothing was saved."""
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM settings WHERE owner = ?", (OWNER,)).fetchone()
        if row is None:
            return self.default_settings(), False
        return UserSettings.model_validate_json(row["data"]), True

    def save_settings(self, settings: UserSettings) -> UserSettings:
        settings = settings.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        settings = UserSettings.model_validate(settings.model_dump())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(owner, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(owner) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (OWNER, settings.model_dump_json(), _now()),
            )
        return settings

    # ---- conversations (short-term OpenClaw session per chat; /new rotates it) ---------------
    def current_conversation(self, chat_key: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT conversation_id FROM conversations WHERE chat_key = ?", (chat_key,)).fetchone()
        return row["conversation_id"] if row else self.new_conversation(chat_key)

    def new_conversation(self, chat_key: str) -> str:
        conversation_id = uuid.uuid4().hex[:16]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations(chat_key, conversation_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET conversation_id = excluded.conversation_id, "
                "created_at = excluded.created_at",
                (chat_key, conversation_id, _now()),
            )
        return conversation_id

    # ---- digests --------------------------------------------------------------------------
    def create_digest(self, digest: Digest, rendered_text: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO digests(trace_id, created_at, period_hours, item_count, delivered_count, status, "
                "rendered_text, data) VALUES (?, ?, ?, ?, 0, 'pending', ?, ?)",
                (digest.trace_id, _now(), digest.period_hours, len(digest.items), rendered_text,
                 digest.model_dump_json()),
            )
            return int(cur.lastrowid)

    def mark_delivered(self, digest_id: int, items: list[tuple[str, str]]) -> None:
        """Record URLs that were part of a Telegram message that was sent successfully."""
        with self._connect() as conn:
            for url, title in items:
                conn.execute(
                    "INSERT INTO delivered_urls(url, title, digest_id, delivered_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(url) DO NOTHING",
                    (url, title, digest_id, _now()),
                )
            conn.execute("UPDATE digests SET delivered_count = delivered_count + ? WHERE id = ?",
                         (len(items), digest_id))

    def finish_digest(self, digest_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE digests SET status = ? WHERE id = ?", (status, digest_id))

    def last_digest(self) -> tuple[int, Digest] | None:
        """Most recent digest that reached the user (fully or partially), as a typed object."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, data FROM digests WHERE status IN ('delivered', 'partial', 'empty') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return int(row["id"]), Digest.model_validate_json(row["data"])

    def last_digests(self, limit: int = 3) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, trace_id, created_at, period_hours, item_count, delivered_count, status, rendered_text "
                "FROM digests WHERE status IN ('delivered', 'partial', 'empty') ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delivered_urls(self, limit: int = 500) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT url FROM delivered_urls ORDER BY delivered_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["url"] for r in rows]

    def is_delivered(self, url: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM delivered_urls WHERE url = ?", (url,)).fetchone() is not None

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                "digests": conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0],
                "delivered_urls": conn.execute("SELECT COUNT(*) FROM delivered_urls").fetchone()[0],
                "settings_saved": conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0],
            }


def settings_summary(settings: UserSettings, stored: bool) -> dict[str, Any]:
    data = json.loads(settings.model_dump_json())
    data["source"] = "sqlite" if stored else "defaults (nothing saved yet)"
    return data
