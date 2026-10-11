"""Environment-based configuration shared by the app, researcher and CLI."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INTERESTS = ["AI agents", "MCP", "LLM", "AWS Bedrock", "developer tools"]
DEFAULT_MODEL = "claude-opus-5-5"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class SourceConfig:
    id: str
    name: str
    url: str
    enabled: bool = True
    interest_filter: bool = False
    max_entries: int = 10


@dataclass(frozen=True)
class Limits:
    """Hard limits enforced in code (not by the prompt)."""

    max_llm_iterations: int = 4
    max_tool_calls: int = 8
    max_total_entries: int = 30
    max_excerpt_chars: int = 600
    max_feed_bytes: int = 3_000_000
    http_timeout_seconds: float = 15.0


@dataclass(frozen=True)
class LLMConfig:
    api_key: str | None
    model: str = DEFAULT_MODEL
    effort: str = "low"
    refusal_fallback: bool = True
    max_tokens: int = 8000
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class DigestDefaults:
    interests: list[str] = field(default_factory=lambda: list(DEFAULT_INTERESTS))
    language: str = "ru"
    max_items: int = 5
    period_hours: int = 72


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def load_sources(path: str | Path | None = None) -> list[SourceConfig]:
    path = Path(path or _env("SOURCES_FILE", "config/sources.toml"))
    if not path.exists():
        raise ConfigError(f"Sources file not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sources = [SourceConfig(**item) for item in data.get("sources", [])]
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        raise ConfigError("Duplicate source id in sources file")
    enabled = [s for s in sources if s.enabled]
    if not enabled:
        raise ConfigError("No enabled sources configured")
    return enabled


def load_limits() -> Limits:
    return Limits(
        max_llm_iterations=_env_int("AGENT_MAX_ITERATIONS", 4),
        max_tool_calls=_env_int("AGENT_MAX_TOOL_CALLS", 8),
        max_total_entries=_env_int("RSS_MAX_TOTAL_ENTRIES", 30),
    )


def load_llm_config() -> LLMConfig:
    return LLMConfig(
        api_key=_env("ANTHROPIC_API_KEY"),
        model=_env("ANTHROPIC_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        effort=_env("ANTHROPIC_EFFORT", "low") or "low",
        refusal_fallback=_env_bool("ANTHROPIC_REFUSAL_FALLBACK", True),
    )


def load_digest_defaults() -> DigestDefaults:
    return DigestDefaults(
        language=_env("DEFAULT_LANGUAGE", "ru") or "ru",
        max_items=_env_int("DEFAULT_MAX_ITEMS", 5),
        period_hours=_env_int("DEFAULT_PERIOD_HOURS", 72),
    )


def trace_dir() -> Path:
    return Path(_env("TRACE_DIR", "data/traces") or "data/traces")


def editor_db_path() -> Path:
    return Path(_env("EDITOR_DB_PATH", "data/editor/memory.sqlite3") or "data/editor/memory.sqlite3")


def researcher_url() -> str:
    return _env("RESEARCHER_URL", "http://localhost:9100") or "http://localhost:9100"


def a2a_timeout_seconds() -> float:
    return float(_env_int("A2A_TIMEOUT_SECONDS", 300))


def a2a_max_attempts() -> int:
    return max(1, min(_env_int("A2A_MAX_ATTEMPTS", 2), 3))


def telegram_token() -> str:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and put the token into .env")
    return token


def allowed_telegram_user_id() -> int:
    raw = _env("ALLOWED_TELEGRAM_USER_ID")
    if not raw:
        raise ConfigError(
            "ALLOWED_TELEGRAM_USER_ID is not set. The bot refuses to start without an allowlist; "
            "put your numeric Telegram user id into .env (see README, 'Telegram user ID')."
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"ALLOWED_TELEGRAM_USER_ID must be a numeric Telegram user id, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError("ALLOWED_TELEGRAM_USER_ID must be a positive integer")
    return value


def require_api_key(cfg: LLMConfig) -> None:
    if not cfg.api_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set. Put your Anthropic API key into .env")
