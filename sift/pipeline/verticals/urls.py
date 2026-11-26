"""URL predicates and canonicalizers used by the verticals.

Two jobs: deciding whether a URL is a real entry for a vertical (a LinkedIn
*profile*, not a post) and collapsing the many URLs that point at one thing
(``youtu.be/ID`` and ``youtube.com/watch?v=ID`` → one key) so dedupe works.
All rule-based, no network.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def _platform(url: str) -> str:
    host = _host(url)
    if host == "youtu.be":
        return "youtube"
    if host == "news.ycombinator.com":
        return "hackernews"
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


# ------------------------------------------------------------- people profiles

_IG_RESERVED = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "about", "direct"}
_X_RESERVED = {"i", "search", "home", "hashtag", "explore", "intent", "share", "settings"}
_GH_RESERVED = {
    "features",
    "topics",
    "orgs",
    "marketplace",
    "sponsors",
    "collections",
    "about",
    "pricing",
    "enterprise",
    "login",
    "search",
    "trending",
}


def profile_url_ok(url: str) -> bool:
    """A profile page, not a post, job, company or search page."""
    host, segs = _host(url), _segments(url)
    if _on(host, "linkedin.com"):
        return len(segs) >= 2 and segs[0] == "in"
    if _on(host, "instagram.com"):
        return len(segs) == 1 and segs[0].lower() not in _IG_RESERVED
    if _on(host, "x.com") or _on(host, "twitter.com"):
        return len(segs) == 1 and segs[0].lower() not in _X_RESERVED
    if _on(host, "github.com"):
        return len(segs) == 1 and segs[0].lower() not in _GH_RESERVED
    if _on(host, "crunchbase.com"):
        return len(segs) >= 2 and segs[0] in ("person", "organization")
    return True


def profile_canonical(url: str) -> str:
    """ca.linkedin.com/in/x/ and www.linkedin.com/in/x → one key."""
    host, segs = _host(url), _segments(url)
    if _on(host, "linkedin.com") and len(segs) >= 2:
        return f"https://www.linkedin.com/in/{segs[1].lower()}"
    if _on(host, "twitter.com") and segs:
        return f"https://x.com/{segs[0]}"
    if segs and (_on(host, "instagram.com") or _on(host, "x.com") or _on(host, "github.com")):
        return f"https://{host}/{segs[0]}"
    return url


# -------------------------------------------------------------------- videos


def video_canonical(url: str) -> str:
    """youtu.be/ID, youtube.com/watch?v=ID&t=1, /shorts/ID → one watch URL."""
    parts = urlsplit(url)
    host, segs = _host(url), _segments(url)
    vid = None
    if host == "youtu.be" and segs:
        vid = segs[0]
    elif _on(host, "youtube.com"):
        if segs[:1] == ["watch"]:
            vid = (parse_qs(parts.query).get("v") or [None])[0]
        elif len(segs) >= 2 and segs[0] in ("shorts", "embed", "live"):
            vid = segs[1]
    return f"https://www.youtube.com/watch?v={vid}" if vid else url


# -------------------------------------------------------------------- social


def social_url_ok(url: str) -> bool:
    """A post or thread (profiles belong to the people vertical)."""
    host, segs = _host(url), _segments(url)
    if _on(host, "reddit.com"):
        return len(segs) >= 2 and segs[0] in ("r", "u", "user")
    if _on(host, "x.com") or _on(host, "twitter.com"):
        return "status" in segs
    if _on(host, "instagram.com"):
        return bool(segs) and segs[0] in ("p", "reel", "reels")
    if _on(host, "threads.net") or _on(host, "threads.com"):
        return "post" in segs or (bool(segs) and segs[0].startswith("@"))
    return True


# ---------------------------------------------------------------------- jobs


def job_url_ok(url: str) -> bool:
    host, segs = _host(url), _segments(url)
    if _on(host, "lever.co") or _on(host, "ashbyhq.com"):
        return len(segs) >= 2  # /<company>/<posting-id>
    if _on(host, "greenhouse.io"):
        return "jobs" in segs
    if _on(host, "linkedin.com"):
        return segs[:2] == ["jobs", "view"]
    if _on(host, "indeed.com"):
        return bool(segs) and segs[0] in ("viewjob", "rc", "cmp", "m")
    return True


def job_canonical(url: str) -> str:
    """Drop the /apply step and tracking query of an ATS posting."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    if _host(url).endswith(("lever.co", "ashbyhq.com", "greenhouse.io")):
        return f"{parts.scheme}://{parts.netloc}{path}"
    return url


# ------------------------------------------------------------------- shopping


def shop_canonical(url: str) -> str:
    """keychron.com/products/x.oembed and /products/x?variant=1 → /products/x."""
    parts = urlsplit(url)
    path = re.sub(r"\.(oembed|json|js|atom)$", "", parts.path)
    if "/products/" in path:
        return f"{parts.scheme}://{parts.netloc}{path}"
    return f"{parts.scheme}://{parts.netloc}{path}" + (f"?{parts.query}" if parts.query else "")


# Pages that mention prices without selling anything.
_NOT_STORE_HOSTS = ("wikipedia.org", "wikimedia.org", "reddit.com", "youtube.com", "quora.com")
_NOT_STORE_PATH = re.compile(r"(?i)/(tag|tags|topic|topics|comment|comments|wiki|forum|forums)/")


def store_like(url: str) -> bool:
    parts = urlsplit(url)
    host = _host(url)
    if any(_on(host, h) for h in _NOT_STORE_HOSTS):
        return False
    return not _NOT_STORE_PATH.search(parts.path)


# Reviews, roundups and guides quote many prices, none of them "the" price.
_ARTICLE_PATH = re.compile(
    r"(?i)/(blogs?|reviews?|picks|guides?|articles?|news|gallery|extras|best|how-to)(/|$)"
    r"|/(best|top-\d+|how)-|[-_,]reviews?\b|[-_]vs[-_]"
)


def article_like(url: str) -> bool:
    return bool(_ARTICLE_PATH.search(urlsplit(url).path))


# A single product, not a category or search page: Shopify /products/x,
# WooCommerce /product/x, Amazon /dp/X, eBay /itm/X, Best Buy /product/…
_PRODUCT_PATH = re.compile(r"/(products?|dp|itm|ip|p)/[^/?#]+", re.I)
_LISTING_PATH = re.compile(
    r"/(collections?|category|categories|c|s|search|shop|browse)(/|$|\?)", re.I
)


def product_url(url: str) -> bool:
    path = urlsplit(url).path
    return bool(_PRODUCT_PATH.search(path)) and not (
        _LISTING_PATH.search(path) and "/products/" not in path
    )


# ----------------------------------------------------------------- site: match


def on_site(url: str, site: str) -> bool:
    """``site`` as in a site: filter: a domain with an optional path prefix."""
    domain, _, path = site.partition("/")
    if not _on(_host(url), domain):
        return False
    return not path or "/".join(_segments(url)).startswith(path)
