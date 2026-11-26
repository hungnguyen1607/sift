"""schema.org data from a page's ``<script type="application/ld+json">`` blocks.

Product pages (most Shopify/WooCommerce stores) publish ``Product``/``Offer``
with a price; ATS job boards (Lever, Greenhouse, Ashby) publish
``JobPosting``. Reading these is how Sift gets prices and job details without
an LLM or a vendor API.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from urllib.parse import urljoin, urlsplit

from sift.pipeline.verticals.fields import clean as _clean

_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S
)
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?P<k>(?:og|product):price:(?:amount|currency))[\"']"
    r"[^>]+content=[\"'](?P<v>[^\"']+)[\"']",
    re.I,
)


def _objects(html: str):
    for block in _LD_RE.findall(html):
        try:
            data = json.loads(htmllib.unescape(block.strip()))
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "mainEntity", "itemListElement"):
                    if key in node:
                        stack.append(node[key])


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    return set(t) if isinstance(t, list) else {t} if isinstance(t, str) else set()


def _name(v) -> str | None:
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, list) and v:
        return _name(v[0])
    return v if isinstance(v, str) else None


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _product(node: dict) -> dict:
    """name/brand/price/currency/availability/rating from a Product node."""
    offer = _first(node.get("offers")) or {}
    if not offer:
        # Shopify's ProductGroup: the price lives on the first variant's offer.
        for variant in node.get("hasVariant") or []:
            if isinstance(variant, dict) and variant.get("offers"):
                offer = _first(variant["offers"]) or {}
                break
    if isinstance(offer, dict) and "AggregateOffer" in _types(offer):
        price = offer.get("lowPrice") or offer.get("price")
    else:
        price = offer.get("price") if isinstance(offer, dict) else None
    if price is None and isinstance(offer, dict):
        spec = _first(offer.get("priceSpecification")) or {}
        price = spec.get("price") if isinstance(spec, dict) else None
    rating = node.get("aggregateRating") or {}
    avail = offer.get("availability") if isinstance(offer, dict) else None
    return _clean(
        {
            "name": node.get("name"),
            "brand": _name(node.get("brand")),
            "price": _num(price),
            "currency": offer.get("priceCurrency") if isinstance(offer, dict) else None,
            "availability": avail.rsplit("/", 1)[-1] if isinstance(avail, str) else None,
            "rating": _num(rating.get("ratingValue")) if isinstance(rating, dict) else None,
            "reviews": _num(rating.get("reviewCount")) if isinstance(rating, dict) else None,
        }
    )


def _product_nodes(html: str) -> list[dict]:
    """Product nodes in document order, including ItemList entries
    (``{"@type": "ListItem", "item": {Product}}``)."""
    out: list[dict] = []
    for block in _LD_RE.findall(html):
        try:
            data = json.loads(htmllib.unescape(block.strip()))
        except ValueError:
            continue

        def walk(node, depth=0):
            if depth > 6:
                return
            if isinstance(node, list):
                for n in node:
                    walk(n, depth + 1)
            elif isinstance(node, dict):
                if _types(node) & {"Product", "ProductGroup"}:
                    out.append(node)
                    return
                for key in ("@graph", "mainEntity", "itemListElement", "item"):
                    if key in node:
                        walk(node[key], depth + 1)

        walk(data)
    return out


def product_fields(html: str) -> dict:
    """The page's own product. A page listing several products (a category or
    search page) has none: see ``product_list``."""
    nodes = _product_nodes(html)
    if len(nodes) == 1:
        return _product(nodes[0])
    if nodes:
        return {}
    meta = {m.group("k").lower().split(":")[-1]: m.group("v") for m in _META_RE.finditer(html)}
    if "amount" in meta:
        return _clean({"price": _num(meta["amount"]), "currency": meta.get("currency")})
    return {}


def product_list(html: str, base_url: str, limit: int = 8) -> list[dict]:
    """Products a listing page publishes, each with its own URL and price."""
    nodes = _product_nodes(html)
    if len(nodes) < 2:
        return []
    out, seen = [], set()
    for node in nodes:
        offer = _first(node.get("offers")) or {}
        url = node.get("url") or (offer.get("url") if isinstance(offer, dict) else None)
        if not isinstance(url, str) or not url.strip():
            continue
        url = urljoin(base_url, url.strip())
        fields = _product(node)
        if not fields.get("name") or url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(url)
        out.append({"url": url, **fields})
        if len(out) >= limit:
            break
    return out


# Shopify stores (a large share of small retailers) serve every product as
# JSON at /products/<handle>.js: ~30 KB instead of a 1-3 MB page, with the
# price in minor units. Locale/collection prefixes are allowed.
_SHOPIFY_PATH = re.compile(
    r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/collections/[^/]+)?/products/([^/?#.]+)"
)
SHOPIFY_TYPES = ("text/javascript", "application/javascript", "application/json")


def shopify_json_url(url: str) -> str | None:
    parts = urlsplit(url)
    m = _SHOPIFY_PATH.match(parts.path.lower())
    if not m or not parts.netloc:
        return None
    return f"{parts.scheme or 'https'}://{parts.netloc}/products/{m.group(1)}.js"


def shopify_fields(body: str) -> dict:
    try:
        d = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(d, dict) or "price" not in d or "variants" not in d:
        return {}  # not a Shopify product
    price = _num(d.get("price_min", d.get("price")))
    avail = d.get("available")
    return _clean(
        {
            "name": d.get("title"),
            "brand": d.get("vendor"),
            "price": price / 100 if price is not None else None,
            "availability": None if avail is None else ("InStock" if avail else "OutOfStock"),
        }
    )


_SHOPIFY_COLLECTION = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/collections/([^/?#.]+)/?$")


def shopify_collection_url(url: str, limit: int = 8) -> str | None:
    """A Shopify collection page's product feed: /collections/<handle>/products.json."""
    parts = urlsplit(url)
    m = _SHOPIFY_COLLECTION.match(parts.path.lower())
    if not m or not parts.netloc:
        return None
    base = f"{parts.scheme or 'https'}://{parts.netloc}"
    return f"{base}/collections/{m.group(1)}/products.json?limit={limit}"


def shopify_collection(body: str, base_url: str, limit: int = 8) -> list[dict]:
    """Products from a collection feed; prices are decimal strings here (the
    per-product .js feed uses cents)."""
    try:
        d = json.loads(body)
    except ValueError:
        return []
    products = d.get("products") if isinstance(d, dict) else None
    if not isinstance(products, list):
        return []
    parts = urlsplit(base_url)
    out = []
    for p in products[:limit]:
        if not isinstance(p, dict) or not p.get("handle") or not p.get("title"):
            continue
        variants = [v for v in p.get("variants") or [] if isinstance(v, dict)]
        prices = [x for x in (_num(v.get("price")) for v in variants) if x is not None]
        out.append(
            _clean(
                {
                    "url": f"{parts.scheme or 'https'}://{parts.netloc}/products/{p['handle']}",
                    "name": p.get("title"),
                    "brand": p.get("vendor"),
                    "price": min(prices) if prices else None,
                    "availability": (
                        None
                        if not variants or "available" not in variants[0]
                        else "InStock"
                        if any(v.get("available") for v in variants)
                        else "OutOfStock"
                    ),
                }
            )
        )
    return out


def job_fields(html: str) -> dict:
    for node in _objects(html):
        if "JobPosting" not in _types(node):
            continue
        loc = _first(node.get("jobLocation")) or {}
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        if isinstance(addr, dict):
            parts = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            location = ", ".join(_name(p) or "" for p in parts if p) or None
        else:
            location = addr if isinstance(addr, str) else None
        salary = None
        base = node.get("baseSalary")
        if isinstance(base, dict):
            val = base.get("value", {})
            if isinstance(val, dict):
                lo, hi = _num(val.get("minValue")), _num(val.get("maxValue"))
                amount = _num(val.get("value"))
                span = f"{lo:g}–{hi:g}" if lo and hi else f"{(amount or lo or hi or 0):g}"
                if lo or hi or amount:
                    salary = f"{span} {base.get('currency', '')} / {val.get('unitText', '')}".strip(
                        " /"
                    )
        remote = node.get("jobLocationType") == "TELECOMMUTE" or None
        return _clean(
            {
                "title": node.get("title"),
                "company": _name(node.get("hiringOrganization")),
                "location": location,
                "remote": remote,
                "employment_type": _first(node.get("employmentType")),
                "salary": salary,
                "date_posted": node.get("datePosted"),
                "valid_through": node.get("validThrough"),
            }
        )
    return {}


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v.replace(" ", ""))
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None
