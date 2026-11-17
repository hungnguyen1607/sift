import re

import httpx

from .base import Candidate
from .wikipedia import WikipediaProvider

# "things to do in Lisbon", "3 days in Lisbon itinerary" → "Lisbon": Wikivoyage
# search ranks the destination article first only for the bare place name.
_TRAVEL_FILLER = re.compile(
    r"(?i)\b(what are the |the )?(best |top )?(things to (do|see)|places to (visit|see)|"
    r"where to (stay|eat)|travel guide|itinerary|trip|vacation|holiday|honeymoon|visit(ing)?|"
    r"best time to visit|\d+\s+days?|weekend|guide|hotels?|flights?|cheap|in|to|for|a|an|the|of)\b"
)


def destination(query: str) -> str:
    rest = _TRAVEL_FILLER.sub(" ", query)
    return " ".join(rest.replace("?", " ").split()) or query


class WikivoyageProvider(WikipediaProvider):
    """Wikivoyage travel guides through the same MediaWiki API as Wikipedia
    (search plus plain-text extracts, so articles are never scraped)."""

    name = "wikivoyage"
    keyword_search = False

    def __init__(self, client: httpx.AsyncClient, lang: str = "en"):
        super().__init__(client, lang)
        self.host = f"{lang}.wikivoyage.org"
        self.api = f"https://{self.host}/w/api.php"

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        out = await super().search(destination(query), limit, time_range)
        for c in out:
            c.source = self.name
        return out
