import httpx

from .base import Candidate, Provider, strip_html


class SearXNGProvider(Provider):
    """Self-hosted SearXNG meta-search (federates Google, Bing, DuckDuckGo, …).

    Sift queries SearXNG's stable JSON API and turns its raw links into
    candidates that the pipeline then fetches, cleans and ranks. The HTML
    parsing lives in SearXNG, not Sift, so keeping the SearXNG container up to
    date is all the "engine changed" maintenance this needs. Self-hosted, so
    uncapped (Sift relies on SearXNG's own rate limiter).
    """

    name = "searxng"

    def __init__(self, client: httpx.AsyncClient, url: str, categories: str = "general"):
        self.client = client
        self.url = url.rstrip("/")
        self.categories = categories

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            f"{self.url}/search",
            params={"q": query, "format": "json", "categories": self.categories},
        )
        out = []
        for r in data.get("results", []):
            url = r.get("url")
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    title=r.get("title") or url,
                    snippet=strip_html(r.get("content"), max_len=400),
                    source=self.name,
                    extra={"engine": r.get("engine"), "score": r.get("score")},
                )
            )
            if len(out) >= limit:
                break
        return out
