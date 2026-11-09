"""Request/response models. Field names follow Tavily's API so existing Tavily
clients (LangChain, CrewAI) work by changing the base URL."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    # Tavily clients send fields we don't use yet (api_key, topic, days, ...).
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(5, ge=1, le=20)
    max_tokens: int | None = Field(None, ge=50, le=50_000)
    search_depth: Literal["basic", "advanced"] = "basic"
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    fresh_only: bool = False
    time_range: Literal["day", "week", "month", "year", "d", "w", "m", "y"] | None = None
    include_raw_content: bool = False
    include_answer: bool = False


class SearchResult(BaseModel):
    title: str
    url: str
    content: str
    score: float
    source: str
    sources: list[str]
    published: datetime | None = None
    updated: datetime | None = None
    # raw_content[quote_start:quote_end] == content. Null when the page couldn't
    # be fetched and the provider snippet stands in for it.
    quote_start: int | None = None
    quote_end: int | None = None
    section: str | None = None
    raw_content: str | None = None


class SearchResponse(BaseModel):
    query: str
    answer: str | None = None
    images: list[str] = Field(default_factory=list)
    results: list[SearchResult]
    sub_queries: list[str] | None = None
    response_time: float
    providers_used: list[str]
    providers_skipped: dict[str, str]
    cache_hit: bool = False
    cost_usd: float = 0.0
    timings_ms: dict[str, int] = Field(default_factory=dict)
