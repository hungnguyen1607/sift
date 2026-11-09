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

    # Live page fetching (per search request).
    fetch_timeout: float = 3.0
    fetch_budget: float = 3.0  # whole fetch + clean stage
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

    # Set to a comma-separated list to restrict providers, e.g. "local,wikipedia".
    enabled_providers: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
