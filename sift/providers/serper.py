import httpx

from .base import Candidate, Provider, parse_ts


class SerperProvider(Provider):
    """Serper (Google results) as the last-resort fallback. 2,500 searches,
    lifetime, from the one-time free credit."""

    name = "serper"
    cap = 2400
    period = "lifetime"

    def __init__(self, client: httpx.AsyncClient, key: str | None = None):
        self.client = client
        self.key = key

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        body: dict = {"q": query, "num": limit}
        if time_range:
            from sift.pipeline.fresh import serper_tbs

            body["tbs"] = serper_tbs(time_range)
        data = await self._post_json(
            self.client,
            "https://google.serper.dev/search",
            json=body,
            headers={"X-API-KEY": self.key},
        )
        out = []
        for r in data.get("organic", []):
            url = r.get("link")
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    title=r.get("title") or url,
                    snippet=r.get("snippet") or "",
                    source=self.name,
                    published=parse_ts(r.get("date")),
                )
            )
        return out
