"""The ``Vertical`` descriptor: everything one kind of search needs to know.

A vertical says which SearXNG categories and ``site:`` lists supply its
candidates, which URLs count as real entries, how to canonicalize them for
dedupe, and how to read structured ``fields`` out of a search result. The
registry of concrete verticals lives in :mod:`.detection`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sift.providers.base import Candidate

Fields = dict[str, object]


@dataclass(frozen=True, slots=True)
class Vertical:
    name: str
    shape: str  # "answer" | "list"
    categories: str | None = None  # SearXNG categories, comma-separated
    sites: tuple[str, ...] = ()  # OR-ed site: filters on a general web search
    fetch: int = 0  # list shape: fetch this many top pages for JSON-LD fields
    per_site: int | None = None  # None = no cap (one site can fill the list)
    providers: tuple[str, ...] = ()  # extra providers by name (engine.vertical_providers)
    # Answer shape: use only the vertical's own sources (news from news engines,
    # not Wikipedia/StackOverflow/the local index).
    exclusive: bool = False
    # Keep the source's order instead of text matching (map results are
    # ranked by the geocoder; "cafes" won't match "coffeehouse").
    rank_by_source: bool = False
    # Providers that only run when their site is picked (GitHub's user search
    # when the query names GitHub).
    site_providers: tuple[tuple[str, str], ...] = ()
    url_ok: Callable[[str], bool] | None = None
    canonical: Callable[[str], str] | None = None
    parse: Callable[[Candidate], Fields] | None = None
