from datetime import datetime, timezone

from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.models import Digest, DigestItem


def test_defaults_are_not_stored(tmp_path, defaults):
    store = MemoryStore(tmp_path / "m.sqlite3", defaults)
    settings, stored = store.get_settings()
    assert not stored
    assert settings.max_items == 5 and settings.language == "ru" and settings.period_hours == 72


def test_settings_survive_recreating_the_store(tmp_path, defaults):
    db = tmp_path / "memory.sqlite3"
    first = MemoryStore(db, defaults)
    s, _ = first.get_settings()
    first.save_settings(s.model_copy(update={"max_items": 2, "interests": ["MCP", "AWS Bedrock"]}))
    del first

    second = MemoryStore(db, defaults)  # simulates an application restart
    loaded, stored = second.get_settings()
    assert stored and loaded.max_items == 2 and loaded.interests == ["MCP", "AWS Bedrock"]
    assert loaded.updated_at is not None


def test_digest_history_and_delivered_urls_persist(tmp_path, defaults):
    db = tmp_path / "memory.sqlite3"
    store = MemoryStore(db, defaults)
    item = DigestItem(source_id="s", source_name="S", title="T", url="https://a.test/1",
                      published_at=datetime(2026, 10, 9, tzinfo=timezone.utc), summary="x", application="y")
    digest = Digest(trace_id="tr-1", language="ru", period_hours=72, items=[item])
    digest_id = store.create_digest(digest, "rendered")
    store.mark_delivered(digest_id, [("https://a.test/1", "T")])
    store.finish_digest(digest_id, "delivered")

    reopened = MemoryStore(db, defaults)
    assert reopened.delivered_urls() == ["https://a.test/1"]
    assert reopened.is_delivered("https://a.test/1")
    rows = reopened.last_digests()
    assert rows[0]["rendered_text"] == "rendered" and rows[0]["delivered_count"] == 1


def test_reset_memory_cli(tmp_path, defaults, monkeypatch):
    from ai_news_digest import cli

    db = tmp_path / "memory.sqlite3"
    store = MemoryStore(db, defaults)
    s, _ = store.get_settings()
    store.save_settings(s.model_copy(update={"max_items": 2}))
    monkeypatch.setenv("EDITOR_DB_PATH", str(db))
    assert cli.main(["reset-memory"]) == 1 and db.exists()  # requires explicit --yes
    assert cli.main(["reset-memory", "--yes"]) == 0 and not db.exists()


def test_pre_migration_database_is_opened_without_data_loss(tmp_path, defaults):
    """A DB created by the pre-OpenClaw version (no `conversations` table) keeps all data."""
    import sqlite3

    from ai_news_digest.models import UserSettings

    db = tmp_path / "memory.sqlite3"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE settings (owner TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE digests (id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, created_at TEXT NOT NULL,
            period_hours INTEGER NOT NULL, item_count INTEGER NOT NULL, delivered_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, rendered_text TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE delivered_urls (url TEXT PRIMARY KEY, title TEXT NOT NULL, digest_id INTEGER REFERENCES digests(id),
            delivered_at TEXT NOT NULL);
    """)
    settings = UserSettings(interests=["MCP"], language="ru", max_items=2, period_hours=48)
    item = DigestItem(source_id="s", source_name="S", title="T", url="https://a.test/1",
                      published_at=datetime(2026, 10, 9, tzinfo=timezone.utc), summary="x", application="y")
    digest = Digest(trace_id="tr-old", language="ru", period_hours=48, items=[item])
    old.execute("INSERT INTO settings VALUES ('owner', ?, '2026-10-10T00:00:00+00:00')", (settings.model_dump_json(),))
    old.execute("INSERT INTO digests (trace_id, created_at, period_hours, item_count, delivered_count, status, "
                "rendered_text, data) VALUES ('tr-old', '2026-10-10', 48, 1, 1, 'delivered', 'old html', ?)",
                (digest.model_dump_json(),))
    old.execute("INSERT INTO delivered_urls VALUES ('https://a.test/1', 'T', 1, '2026-10-10')")
    old.commit()
    old.close()

    store = MemoryStore(db, defaults)
    loaded, stored = store.get_settings()
    assert stored and loaded.max_items == 2 and loaded.period_hours == 48
    assert store.delivered_urls() == ["https://a.test/1"]
    digest_id, last = store.last_digest()
    assert digest_id == 1 and last.items[0].url == "https://a.test/1"
    assert store.current_conversation("chat")  # new table created on the fly
