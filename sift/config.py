from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIFT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://sift:sift@localhost:5432/sift"
    redis_url: str = "redis://localhost:6379/0"
    user_agent: str = "Sift/0.1 (+https://github.com/hungnguyen1607/sift)"

    # Total time budget for the candidate stage (all providers run in parallel).
    provider_timeout: float = 1.5
    candidates_per_provider: int = 10

    wikipedia_lang: str = "en"
    stackexchange_key: str | None = None
    stackexchange_site: str = "stackoverflow"
    marginalia_key: str = "public"

    # Free web-search fallback tiers (an empty key leaves the tier unbuilt).
    github_token: str | None = None
    openalex_mailto: str | None = None
    brave_key: str | None = None
    tavily_key: str | None = None
    serper_key: str | None = None

    # Self-hosted SearXNG web backend (no keys, no vendor). Set the URL to enable
    # it, e.g. "http://searxng:8080". `search_web` controls when it fires:
    #   "always" — every cache miss (default); "auto" — thin/recency/advanced;
    #   "off" — never.
    searxng_url: str | None = None
    search_web: str = "always"

    # Live page fetching (per search request).
    fetch_timeout: float = 3.0
    fetch_budget: float = 3.0  # whole fetch + clean stage (advanced)
    fetch_budget_basic: float = 2.0  # tighter budget for basic searches
    fetch_max_bytes: int = 3_000_000
    fetch_pages_basic: int = 5
    fetch_pages_advanced: int = 10
    clean_workers: int = 2

    # Embeddings. The API embeds only the opening words of the top live
    # passages, within a time budget; the worker embeds full passages for the index.
    model_cache: str | None = None
    embed_threads: int = 2
    worker_embed_threads: int = 1
    embed_live_passages: int = 12
    embed_live_words: int = 64
    embed_budget: float = 0.8

    # Ranking.
    local_candidates: int = 30
    per_site_limit: int = 2

    # Index growth bound: evict least-recently-used pages beyond `max_pages`
    # and/or pages unused for `page_retention_days`. 0 disables that rule.
    max_pages: int = 0
    page_retention_days: int = 0

    # Reranker (advanced depth only), loaded lazily in the API process.
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_passages: int = 15
    rerank_budget: float = 1.5
    rerank_tokens: int = 256

    # Caches.
    query_cache_ttl: int = 24 * 3600
    query_cache_short_ttl: int = 3600
    url_cache_ttl: int = 24 * 3600

    # Auth + rate limit.
    auth: str = "off"  # "off" (anonymous) or "on" (require a Bearer/body key)
    rpm_limit: int = 60
    key_cache_ttl: int = 60

    # Optional answer/planner/verify model (off by default; self-hosters point it
    # at any Anthropic or OpenAI-compatible endpoint).
    llm_provider: str | None = None  # "anthropic" | "openai"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout: float = 4.0

    # Set to a comma-separated list to restrict providers, e.g. "local,wikipedia".
    enabled_providers: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
