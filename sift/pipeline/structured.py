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


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def product_fields(html: str) -> dict:
    for node in _objects(html):
        if not _types(node) & {"Product", "ProductGroup"}:
            continue
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
    meta = {m.group("k").lower().split(":")[-1]: m.group("v") for m in _META_RE.finditer(html)}
    if "amount" in meta:
        return _clean({"price": _num(meta["amount"]), "currency": meta.get("currency")})
    return {}


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
