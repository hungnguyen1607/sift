"""Readers that pull structured ``fields`` out of a search result.

Each ``*_fields`` function turns a :class:`Candidate` (a search-engine result,
sometimes with schema.org JSON-LD in ``extra``) into a dict of typed fields for
its vertical: channel/duration for videos, price/currency for shopping,
address/lat/lon for places, and so on. All best-effort and rule-based.
"""

from __future__ import annotations

import ast
import re
import unicodedata

from sift.providers.base import Candidate

from .base import Fields
from .urls import _host, _on, _platform, _segments, article_like, store_like


def _literal(v):
    """SearXNG sometimes hands nested values through as Python reprs."""
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v
    return v


def clean(fields: Fields) -> Fields:
    """Drop empty values so results only carry fields that were actually found."""
    return {k: v for k, v in fields.items() if v not in (None, "", [], {})}


_clean = clean  # internal alias; kept for readability at call sites


# -------------------------------------------------------------- videos / news


def video_fields(c: Candidate) -> Fields:
    e = c.extra
    return clean(
        {
            "platform": _platform(c.url),
            "channel": e.get("author"),
            "duration": e.get("length"),
            "published": c.published.isoformat() if c.published else None,
            "views": e.get("views"),
            "thumbnail": e.get("thumbnail"),
            "embed_url": e.get("iframe_src"),
        }
    )


def news_fields(c: Candidate) -> Fields:
    e = c.extra
    age, publisher = None, None
    meta = str(e.get("metadata") or "")
    if "|" in meta:
        age, publisher = (s.strip() for s in meta.split("|", 1))
    elif e.get("pubdate"):
        age = str(e["pubdate"])
    return clean(
        {
            "publisher": publisher or _host(c.url),
            "published": c.published.isoformat() if c.published else None,
            "age": age,
        }
    )


# ------------------------------------------------------------------- people

_LI_SNIPPET = {
    "company": re.compile(r"Experience:\s*([^·|]+)"),
    "school": re.compile(r"Education:\s*([^·|]+)"),
    "location": re.compile(r"Location:\s*([^·|]+?)(?:\.\s|\s+View\b|\s*[·|]|\s*\.{3}|$)"),
    "connections": re.compile(r"(\d[\d,+]*)\s+connections"),
}
_IG_TITLE = re.compile(r"^(?P<name>.*?)\s*\(@(?P<handle>[\w.]+)")
_FOLLOWERS = re.compile(r"([\d.,]+[KMB]?)\s+Followers", re.I)
_SITE_SUFFIX = re.compile(
    r"\s+[|•·–—-]\s+(LinkedIn|Instagram.*|X|Twitter|GitHub|Crunchbase)\s*$", re.I
)


def people_fields(c: Candidate) -> Fields:
    host = _host(c.url)
    title = _SITE_SUFFIX.sub("", c.title).strip()
    f: Fields = {"platform": _platform(c.url)}
    if _on(host, "linkedin.com"):
        name, _, headline = re.sub(r"\s+[-–]\s+", "\0", title, count=1).partition("\0")
        name, headline = name.strip(), headline.strip()
        slug = _slug_name(_segments(c.url)[1] if len(_segments(c.url)) > 1 else "")
        if slug and not _same_person(name, slug):
            # A title with no name in it ("User Researcher - Product at Shopify"):
            # the whole title is the headline; the URL slug carries the name.
            name, headline = slug, title
        f["name"], f["headline"] = name, headline
        for key, rx in _LI_SNIPPET.items():
            m = rx.search(c.snippet)
            if m:
                f[key] = m.group(1).strip()
        segs = _segments(c.url)
        f["handle"] = segs[1] if len(segs) > 1 else None
    elif _on(host, "instagram.com") or _on(host, "x.com") or _on(host, "twitter.com"):
        m = _IG_TITLE.match(title)
        segs = _segments(c.url)
        f["name"] = m.group("name").strip(" |") if m else title
        f["handle"] = m.group("handle") if m else (segs[0] if segs else None)
        fm = _FOLLOWERS.search(c.snippet)
        if fm:
            f["followers"] = fm.group(1)
    elif _on(host, "github.com"):
        segs = _segments(c.url)
        handle = segs[0] if segs else ""
        f["handle"] = handle or None
        title = re.sub(r"\s+-\s+Overview$", "", title)
        m = re.match(r"^(.+?)\s+\((.+)\)$", title)
        if m and m.group(2).lower() == handle.lower():  # "Name (login)"
            f["name"] = m.group(1)
        elif m and m.group(1).lower() == handle.lower():  # "login (Name)"
            f["name"] = m.group(2)
        elif handle and title.lower().endswith(" " + handle.lower()):  # "Name login"
            f["name"] = title[: -len(handle)].strip()
        else:
            f["name"] = title
    else:
        f["name"] = title
    return clean(f)


def _slug_name(slug: str) -> str | None:
    """linkedin.com/in/sarah-donovan-515366146 → "Sarah Donovan"."""
    words = [w for w in re.split(r"[-_]", slug) if w.isalpha() and len(w) > 1]
    return " ".join(w.capitalize() for w in words[:4]) or None


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _same_person(name: str, slug_name: str) -> bool:
    parts = set(_fold(slug_name).split())
    return any(w in parts for w in re.findall(r"[a-z]+", _fold(name)))


# ------------------------------------------------------------------- social


def social_fields(c: Candidate) -> Fields:
    host, segs = _host(c.url), _segments(c.url)
    f: Fields = {
        "platform": _platform(c.url),
        "published": c.published.isoformat() if c.published else None,
    }
    if _on(host, "reddit.com") and len(segs) >= 2 and segs[0] == "r":
        f["community"] = f"r/{segs[1]}"
    elif (_on(host, "x.com") or _on(host, "twitter.com")) and segs:
        f["author"] = "@" + segs[0]
    meta = c.extra.get("metadata")
    if meta:
        f["stats"] = str(meta)
    return clean(f)


# ------------------------------------------------------------------- places


def place_fields(c: Candidate) -> Fields:
    e = c.extra
    addr = _literal(e.get("address"))
    address = None
    if isinstance(addr, dict):
        street = " ".join(str(addr[k]) for k in ("house_number", "road") if addr.get(k))
        parts = [street, addr.get("locality"), addr.get("postcode"), addr.get("country")]
        address = ", ".join(str(p) for p in parts if p)
    f: Fields = {
        "type": e.get("type"),
        "address": address,
        "lat": _coord(e.get("latitude")),
        "lon": _coord(e.get("longitude")),
    }
    for item in _literal(e.get("data")) or []:
        if isinstance(item, dict) and item.get("key") and item.get("value"):
            f[str(item["key"])] = item["value"]
    for link in _literal(e.get("links")) or []:
        if isinstance(link, dict) and link.get("label") == "official website":
            f["website"] = link.get("url")
    return clean(f)


def _coord(v) -> float | None:
    """A latitude/longitude rounded to ~1 cm, or None if not a number."""
    try:
        return round(float(v), 7)
    except (TypeError, ValueError):
        return None


def generic_fields(c: Candidate) -> Fields:
    return clean({"platform": _platform(c.url)})


def drop_far_places(fields: list[dict | None], max_km: float = 30.0) -> list[bool]:
    """Keep places near where most results are. "cafes near Concordia
    University" matches both Montreal and Chicago; the median point of all
    results sits in the larger cluster, and anything over ``max_km`` from it
    goes. Entries without coordinates are kept."""
    pts = [
        (f["lat"], f["lon"])
        for f in fields
        if f and isinstance(f.get("lat"), float) and isinstance(f.get("lon"), float)
    ]
    if len(pts) < 3:
        return [True] * len(fields)
    lats, lons = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
    center = (lats[len(lats) // 2], lons[len(lons) // 2])
    return [
        not (f and isinstance(f.get("lat"), float) and isinstance(f.get("lon"), float))
        or _km(center, (f["lat"], f["lon"])) <= max_km
        for f in fields
    ]


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(h))


# --------------------------------------------------------------------- jobs

# Job boards' result titles follow fixed templates, so the role and company
# come from the search result itself, with no fetch (Greenhouse pages carry no
# JSON-LD at all, and Lever pages are slow).
_JOB_TITLE_RES = (
    # Greenhouse: "Job Application for Data Analyst at Later"
    re.compile(r"^Job Application for (?P<title>.+?)(?: at (?P<company>.+?))?$"),
    # LinkedIn: "Shopify hiring Backend Developer in Montreal, QC | LinkedIn"
    re.compile(r"^(?P<company>.+?) hiring (?P<title>.+?)(?: in (?P<location>.+?))?$"),
    # Ashby: "Software Engineer @ Linear"
    re.compile(r"^(?P<title>.+?) @ (?P<company>.+)$"),
    # Indeed: "Data Analyst - Acme Corp - Vancouver, BC"
    re.compile(r"^(?P<title>[^-]+?) - (?P<company>[^-]+?) - (?P<location>.+)$"),
)
_TITLE_SUFFIX = re.compile(
    r"\s*(?:[|\-–]\s*(?:LinkedIn|Indeed(?:\.com)?|jobs\.lever\.co|Lever|Ashby|Greenhouse)\s*)+$",
    re.I,
)
_REMOTE = re.compile(r"(?i)\bremote\b")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _board_slug(url: str) -> str | None:
    """The company slug in an ATS URL: jobs.lever.co/<slug>/…, greenhouse.io/<slug>/jobs/…"""
    host, segs = _host(url), _segments(url)
    if segs and (_on(host, "lever.co") or _on(host, "ashbyhq.com") or _on(host, "greenhouse.io")):
        return segs[0]
    return None


def job_fields_from_title(c: Candidate) -> Fields:
    title = _TITLE_SUFFIX.sub("", c.title or "").strip()
    title = re.sub(r"\s*(?:\.{3}|…)$", "", title)  # engines cut long titles
    title = re.sub(r"\s+at$", "", title)  # … and leave "Engineer at" behind
    out: dict = {"platform": _platform(c.url)}
    slug = _board_slug(c.url)
    company = role = location = None
    host = _host(c.url)
    if _on(host, "lever.co"):
        # "Kabam - Data Analyst"; but "Senior Python Engineer - Data (Remote)"
        # has no company, so the part before " - " must match the URL's slug.
        head, sep, rest = title.partition(" - ")
        if sep and slug and (_norm(head) in _norm(slug) or _norm(slug) in _norm(head)):
            company, role = head, rest
        else:
            role = title
    else:
        for rx in _JOB_TITLE_RES:
            m = rx.match(title)
            if m:
                d = m.groupdict()
                role, company, location = d.get("title"), d.get("company"), d.get("location")
                break
    if not company and slug and not slug.isdigit():
        company = slug.replace("-", " ").title()
    out.update(title=role, company=company, location=location)
    if _REMOTE.search(f"{c.title} {c.snippet}"):
        out["remote"] = True
    return clean(out)


# ------------------------------------------------------------------ shopping

# A price in a result's title or snippet: "$89.99", "CA$129", "From £45",
# "$79 – $99" (the first, lower amount is taken), "129.00 USD".
_CUR = r"(?P<cur>US\$|CA\$|C\$|AU\$|A\$|\$|£|€)"
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?"
_PRICE_RE = re.compile(
    rf"(?<![\w$]){_CUR}\s?(?P<amt>{_AMOUNT})(?![\d%])"
    rf"|(?<![\w.,])(?P<amt2>{_AMOUNT})\s?(?P<cur2>USD|CAD|EUR|GBP)\b"
)
# Amounts that aren't the item's price: discounts, shipping thresholds, caps.
_NOT_PRICE_BEFORE = re.compile(
    r"(?i)\b(save|saving|off|shipping|over|orders?|under|below|less than|up to|"
    r"budget|spend|credit|reward|gift card|was|reg\.?|msrp|list)\W*$"
)
_NOT_PRICE_AFTER = re.compile(r"(?i)^\W*(off|or more|\+|shipping|savings?|discount|credit)\b")
_CUR_CODE = {
    "$": "USD",
    "US$": "USD",
    "CA$": "CAD",
    "C$": "CAD",
    "AU$": "AUD",
    "A$": "AUD",
    "£": "GBP",
    "€": "EUR",
}
# The smallest and largest amounts accepted as a real item price.
_MIN_PRICE, _MAX_PRICE = 0.5, 50_000


def snippet_price(text: str) -> tuple[float, str] | None:
    """The first plausible item price in result text, with its currency."""
    for m in _PRICE_RE.finditer(text):
        if _NOT_PRICE_BEFORE.search(text[max(0, m.start() - 24) : m.start()]):
            continue
        if _NOT_PRICE_AFTER.search(text[m.end() : m.end() + 16]):
            continue
        amount = float((m.group("amt") or m.group("amt2")).replace(",", ""))
        if not _MIN_PRICE <= amount <= _MAX_PRICE:
            continue
        cur = m.group("cur") or m.group("cur2")
        return amount, _CUR_CODE.get(cur, cur)
    return None


def shop_fields(c: Candidate) -> Fields:
    """Platform plus a price read from the result itself, so pages that can't
    be fetched (bot walls, 429s, the fetch budget) can still show one. A price
    read from the fetched page replaces it."""
    fields = generic_fields(c)
    if not store_like(c.url) or article_like(c.url):
        return fields
    found = snippet_price(f"{c.title} · {c.snippet}")
    if found:
        fields.update(price=found[0], currency=found[1], price_source="snippet")
    return fields
