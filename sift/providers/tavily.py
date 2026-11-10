import httpx

from .base import Candidate, Provider, parse_ts


class TavilyProvider(Provider):
    """Tavily web search as a fallback tier. About 1,000 searches/month free.

    The `content` field is used as the snippet so it stands in when a page
    can't be fetched; pages are still fetched live when possible.
    """

    name = "tavily"
    cap = 900
    period = "month"

    def __init__(self, client: httpx.AsyncClient, key: str | None = None):
        self.client = client
        self.key = key

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        body: dict = {"query": query, "max_results": limit, "search_depth": "basic"}
        if time_range:
            from sift.pipeline.fresh import tavily_time_range

            body["time_range"] = tavily_time_range(time_range)
        data = await self._post_json(
            self.client,
            "https://api.tavily.com/search",
            json=body,
            headers={"Authorization": f"Bearer {self.key}"},
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
                    snippet=r.get("content") or "",
                    source=self.name,
                    published=parse_ts(r.get("published_date")),
                    extra={"score": r.get("score")},
                )
            )
        return out
