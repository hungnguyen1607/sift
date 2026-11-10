import httpx

from .base import Candidate, Provider, parse_ts


class BraveProvider(Provider):
    """Brave web search (free tier). About 1,000 searches/month, 1 request/s."""

    name = "brave"
    cap = 900
    period = "month"
    rate_limit = (1, 1)

    def __init__(self, client: httpx.AsyncClient, key: str | None = None):
        self.client = client
        self.key = key

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        params: dict = {"q": query, "count": limit}
        if time_range:
            from sift.pipeline.fresh import brave_freshness

            params["freshness"] = brave_freshness(time_range)
        data = await self._get_json(
            self.client,
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"X-Subscription-Token": self.key},
        )
        out = []
        for r in data.get("web", {}).get("results", []):
            url = r.get("url")
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    title=r.get("title") or url,
                    snippet=r.get("description") or "",
                    source=self.name,
                    published=parse_ts(r.get("page_age")),
                )
            )
        return out
