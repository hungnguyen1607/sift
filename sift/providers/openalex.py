import httpx

from .base import Candidate, Provider, parse_ts


def rebuild_abstract(inverted_index: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the sentence."""
    if not inverted_index:
        return ""
    pos: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for p in positions:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


class OpenAlexProvider(Provider):
    """OpenAlex scholarly works search (polite pool, mailto identifies you).

    Content is the abstract, rebuilt from its inverted index; dates come from
    the publication date. No hard quota — the polite pool asks for a valid
    mailto and a modest rate.
    """

    name = "openalex"
    academic_only = True

    def __init__(self, client: httpx.AsyncClient, mailto: str | None = None):
        self.client = client
        self.mailto = mailto or "sift@example.com"

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            "https://api.openalex.org/works",
            params={"search": query, "per-page": limit, "mailto": self.mailto},
        )
        out = []
        for item in data.get("results", []):
            abstract = rebuild_abstract(item.get("abstract_inverted_index"))
            title = item.get("title") or item.get("display_name") or ""
            if not title:
                continue
            out.append(
                Candidate(
                    url=item.get("id") or item.get("doi") or "",
                    title=title,
                    snippet=abstract[:400],
                    source=self.name,
                    published=parse_ts(item.get("publication_date")),
                    content=abstract,
                    extra={
                        "authorships": len(item.get("authorships", [])),
                        "cited_by": item.get("cited_by_count"),
                    },
                )
            )
        return [c for c in out if c.url]
