import httpx

from .base import Candidate, Provider, ProviderError, strip_html


class MarginaliaProvider(Provider):
    """Marginalia: independent and small websites.

    The shared "public" key is rate-limited across all users and often answers
    with a plain-text "QPM Limit Exceeded"; that is reported as a provider error
    and the search continues without it. Ask Marginalia for your own key.
    """

    name = "marginalia"

    def __init__(self, client: httpx.AsyncClient, key: str = "public"):
        self.client = client
        self.key = key

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        try:
            resp = await self.client.get(
                "https://api2.marginalia-search.com/search",
                params={"query": query, "count": limit},
                headers={"API-Key": self.key},
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"marginalia: {type(e).__name__}: {e}") from e
        if resp.status_code == 429 or "limit exceeded" in resp.text[:100].lower():
            raise ProviderError("marginalia: rate limited upstream")
        if resp.status_code >= 400:
            raise ProviderError(f"marginalia: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError(f"marginalia: non-JSON response: {resp.text[:80]!r}") from e
        return [
            Candidate(
                url=r["url"],
                title=strip_html(r.get("title")) or r["url"],
                snippet=strip_html(r.get("description"), max_len=400),
                source=self.name,
            )
            for r in data.get("results", [])
            if r.get("url")
        ]
