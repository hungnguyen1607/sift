"""HTML → clean Markdown text plus published/updated dates.

``clean_html`` is CPU-bound (lxml parsing takes 0.1-1 s on big pages), so the
API runs it in a process pool; it only takes and returns plain picklable data.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date

import trafilatura
from htmldate import find_date

MAX_TEXT_CHARS = 200_000
_MIN_DATE = "1995-01-01"

_SUP_RE = re.compile(r"<sup>.*?</sup>", re.S)
_TITLE_RES = [
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S),
]
_WIKI_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
_WIKI_TAIL_RE = re.compile(
    r"^#{2,3} (See also|References|External links|Notes|Further reading|Bibliography"
    r"|Citations)\s*$",
    re.M,
)


_ESCAPE_RE = re.compile(r"\\([_*\[\]#`<>~])")
_FENCED_RE = re.compile(r"(^```.*?^```[ \t]*$)", re.M | re.S)


def _unescape_outside_code(text: str) -> str:
    """trafilatura escapes Markdown characters in prose ("jsonb\\_path\\_ops").
    Agents quote identifiers, so give them the literal text back."""
    parts = _FENCED_RE.split(text)
    return "".join(p if p.startswith("```") else _ESCAPE_RE.sub(r"\1", p) for p in parts)


def tidy_markdown(text: str) -> str:
    text = _unescape_outside_code(_SUP_RE.sub("", text))
    text = text.replace("¶", "").replace("\xa0", " ")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT_CHARS]


def wiki_extract_to_markdown(extract: str) -> str:
    """Wikipedia plain-text extract ("== History ==") → Markdown, minus the
    reference sections at the end."""
    text = _WIKI_HEADING_RE.sub(lambda m: "#" * len(m.group(1)) + " " + m.group(2), extract)
    m = _WIKI_TAIL_RE.search(text)
    if m:
        text = text[: m.start()]
    # Drop headings whose section turned out empty.
    text = re.sub(r"^(#{2,6} .+)\n+(?=#{2,6} |\Z)", "", text, flags=re.M)
    return tidy_markdown(text)


def _title(html: str) -> str:
    for rx in _TITLE_RES:
        m = rx.search(html[:200_000])
        if m:
            return " ".join(htmllib.unescape(m.group(1)).split())
    return ""


def _date(html: str, original: bool) -> str | None:
    try:
        return find_date(
            html,
            original_date=original,
            extensive_search=False,
            min_date=_MIN_DATE,
            max_date=date.today().isoformat(),
        )
    except Exception:  # htmldate raises on odd markup; a missing date is fine
        return None


def clean_html(html: str, url: str | None = None) -> dict | None:
    """Return {title, text, published, updated} or None if no main content."""
    text = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_formatting=True,
        include_comments=False,
        include_images=False,
        favor_precision=True,
        deduplicate=False,
        with_metadata=False,
    )
    if not text or len(text.split()) < 30:
        return None
    published = _date(html, original=True)
    updated = _date(html, original=False)
    return {
        "title": _title(html),
        "text": tidy_markdown(text),
        "published": published,
        "updated": updated if updated != published else None,
    }
