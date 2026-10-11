"""Typed contracts exchanged between Editor and Researcher over A2A, plus memory models."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

RESEARCH_REQUEST_MEDIA_TYPE = "application/vnd.ai-news-digest.research-request+json"
RESEARCH_RESULT_MEDIA_TYPE = "application/vnd.ai-news-digest.research-result+json"
CONTRACT_VERSION = "1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


class ResearchRequest(BaseModel):
    """Editor -> Researcher task payload (sent as an A2A DataPart)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    request_id: str = Field(min_length=8, max_length=64)
    trace_id: str = Field(min_length=8, max_length=64)
    period_hours: int = Field(ge=1, le=336)
    interests: list[str] = Field(min_length=1, max_length=15)
    max_items: int = Field(ge=1, le=10)
    exclude_urls: list[str] = Field(default_factory=list, max_length=1000)
    focus: str | None = Field(default=None, max_length=300)
    now: datetime | None = Field(
        default=None, description="Override of the collection time (tests / reproducible runs only)."
    )

    @field_validator("interests")
    @classmethod
    def _clean_interests(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip()[:60] for v in value if v and v.strip()]
        if not cleaned:
            raise ValueError("at least one interest is required")
        return cleaned

    @field_validator("now")
    @classmethod
    def _now_utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value else None


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str = Field(min_length=1, max_length=400)
    url: str = Field(pattern=r"^https?://", max_length=2048)
    published_at: datetime
    available_excerpt: str = Field(default="", max_length=1000)
    excerpt_is_empty: bool = False
    relevance_reason: str = Field(min_length=1, max_length=500)

    @field_validator("published_at")
    @classmethod
    def _published_utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class SourceError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    error: str = Field(max_length=500)


class SourceStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    fetched_entries: int = 0
    in_window: int = 0
    undated_skipped: int = 0
    excluded_delivered: int = 0
    filtered_out_by_interest: int = 0
    passed_to_model: int = 0


class ResearchResult(BaseModel):
    """Researcher -> Editor result (returned as an A2A artifact DataPart)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    request_id: str
    trace_id: str
    period_hours: int
    window_start: datetime
    collected_at: datetime
    articles: list[Article] = Field(default_factory=list, max_length=20)
    source_errors: list[SourceError] = Field(default_factory=list)
    source_stats: list[SourceStats] = Field(default_factory=list)
    incomplete: bool = False
    incomplete_reasons: list[str] = Field(default_factory=list)
    researcher_notes: str = Field(default="", max_length=1000)

    @field_validator("window_start", "collected_at")
    @classmethod
    def _dt_utc(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class UserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interests: list[str] = Field(min_length=1, max_length=15)
    language: str = Field(min_length=2, max_length=10)
    max_items: int = Field(ge=1, le=10)
    period_hours: int = Field(ge=1, le=336)
    notes: list[str] = Field(default_factory=list, max_length=10)
    updated_at: datetime | None = None

    @field_validator("interests")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for v in value:
            v = v.strip()[:60]
            if v and v.lower() not in {s.lower() for s in seen}:
                seen.append(v)
        if not seen:
            raise ValueError("at least one interest is required")
        return seen


class DigestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    title: str = Field(max_length=400)
    url: str = Field(pattern=r"^https?://", max_length=2048)
    published_at: datetime
    summary: str = Field(min_length=1, max_length=900)
    application: str = Field(min_length=1, max_length=500)
    excerpt_is_empty: bool = False


class Digest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    language: str
    period_hours: int
    intro: str = ""
    items: list[DigestItem] = Field(default_factory=list)
    empty_reason: str | None = None
    research_incomplete: bool = False
    source_errors: list[SourceError] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
