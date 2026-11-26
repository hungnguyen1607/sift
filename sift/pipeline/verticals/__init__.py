"""Search verticals: which kind of result a query wants, and where to get it.

Rule-based like the rest of Sift (no LLM). The work is split across focused
modules, re-exported here so callers import from ``sift.pipeline.verticals``:

* :mod:`.base` — the ``Vertical`` descriptor.
* :mod:`.urls` — URL predicates (is this a real entry?) and canonicalizers.
* :mod:`.fields` — readers that pull structured ``fields`` out of a result.
* :mod:`.detection` — the vertical registry and ``detect_vertical``.
* :mod:`.query` — query cleaning and term-coverage scoring.

Two result shapes: ``answer`` (fetch pages, quote the best passages — web,
news, travel, academic, code) and ``list`` (many entries built from result
titles/snippets plus schema.org JSON-LD — people, videos, social, jobs,
shopping, places).
"""

from __future__ import annotations

from .base import Fields, Vertical
from .detection import (
    JOB_SITES,
    PEOPLE_SITES,
    SOCIAL_SITES,
    VERTICALS,
    detect_vertical,
    named_sites,
    pick_sites,
    requested_count,
    site_target,
    strip_count,
)
from .fields import (
    clean,
    drop_far_places,
    generic_fields,
    job_fields_from_title,
    news_fields,
    people_fields,
    place_fields,
    shop_fields,
    snippet_price,
    social_fields,
    video_fields,
)
from .query import coverage, key_terms, price_cap, price_query, search_query
from .urls import (
    article_like,
    job_canonical,
    job_url_ok,
    on_site,
    product_url,
    profile_canonical,
    profile_url_ok,
    shop_canonical,
    social_url_ok,
    store_like,
    video_canonical,
)

__all__ = [
    "Fields",
    "Vertical",
    "VERTICALS",
    "PEOPLE_SITES",
    "SOCIAL_SITES",
    "JOB_SITES",
    "detect_vertical",
    "named_sites",
    "pick_sites",
    "requested_count",
    "site_target",
    "strip_count",
    "clean",
    "drop_far_places",
    "generic_fields",
    "job_fields_from_title",
    "news_fields",
    "people_fields",
    "place_fields",
    "shop_fields",
    "snippet_price",
    "social_fields",
    "video_fields",
    "coverage",
    "key_terms",
    "price_cap",
    "price_query",
    "search_query",
    "article_like",
    "job_canonical",
    "job_url_ok",
    "on_site",
    "product_url",
    "profile_canonical",
    "profile_url_ok",
    "shop_canonical",
    "social_url_ok",
    "store_like",
    "video_canonical",
]
