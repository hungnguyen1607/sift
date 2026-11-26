"""Turning a user query into what the engines see, and scoring entries against it.

``search_query`` strips the words that say *which vertical* ("find", "videos",
"on linkedin") so only the subject reaches the engines; ``key_terms`` and
``coverage`` then measure how many of the query's real terms an entry contains,
with synonym folding so "MEng" matches "Master of Engineering".
"""

from __future__ import annotations

import re

from .base import Vertical
from .detection import _PEOPLE_WORDS, _SITE_RE, strip_count

# Words that say *which vertical* rather than *what to find*: dropped before the
# query goes to the search engines and before coverage is measured.
_STRIP: dict[str, re.Pattern] = {
    "videos": re.compile(
        r"\b(find|show|recommend|watch|me|some|good|best|videos?|youtube|vimeo|dailymotion|"
        r"tiktok|on)\b",
        re.I,
    ),
    "news": re.compile(
        r"\b(latest|news|headlines|breaking|today'?s?|recent|newest|this (week|month)|on|"
        r"about|the)\b",
        re.I,
    ),
    "people": re.compile(
        r"\b(find|list|show|search|get|give|me|for|profiles?|people|persons?|accounts?|who|"
        r"are|is|working|works|work|currently|at|on|linkedin|instagram|github|twitter|"
        r"crunchbase|of|the|from|grads?|graduates?|alumni|alumnus|students?|studying|studied|"
        r"in|with|and)\b",
        re.I,
    ),
    "social": re.compile(
        r"\b(reddit|subreddit|posts?|threads?|about|on|what|do|does|people|say|think|tweets?|"
        r"twitter|mastodon|lemmy|bluesky|facebook|the|of)\b",
        re.I,
    ),
    "jobs": re.compile(
        r"\b(find|list|show|me|jobs?|job (openings?|postings?|listings?)|openings?|positions?|"
        r"roles?|hiring|for|in|at|near|on|linkedin|indeed)\b",
        re.I,
    ),
    "travel": re.compile(
        r"\b((what are the |the )?(best |top )?(things to (do|see)|places to (visit|see)|"
        r"where to (stay|eat)|travel guide|itinerary|trip|vacation|holiday|honeymoon|visit(ing)?|"
        r"best time to visit|\d+\s+days?|weekend|guide|in|to|for|a|an|the|of))\b",
        re.I,
    ),
    "shopping": re.compile(
        r"\b(buy|cheapest|where to buy|for sale|deals? (on|for)|best price|on sale|"
        r"discount on|a|an|the)\b|\bunder \$\s?\d+(\.\d+)?|\$\d+\s*(or less|max|budget)",
        re.I,
    ),
}


def search_query(v: Vertical, query: str) -> str:
    """The query minus count and vertical words; site: operators are kept."""
    q = strip_count(query)
    rx = _STRIP.get(v.name)
    if rx is None:
        return q
    sites = _SITE_RE.findall(q)
    rest = _SITE_RE.sub(" ", q)
    cleaned = " ".join(rx.sub(" ", rest).replace("?", " ").split())
    if not cleaned:
        return q
    if v.name == "people":
        # Profiles say "fitness influencer", "UX designer"; the plural matches
        # Instagram/LinkedIn topic pages ("Miami Fitness Influencers") instead.
        cleaned = " ".join(_singular(w) for w in cleaned.split())
    return " ".join([cleaned, *(f"site:{s}" for s in sites)])


_LEADING_GLUE = re.compile(r"(?i)^(?:(?:for|a|an|the|of|on|some|me)\s+)+")


def price_query(query: str) -> str:
    """A cleaned shopping query plus "price": "for standing desk" → "standing desk price"."""
    q = _LEADING_GLUE.sub("", query).strip() or query
    return q if re.search(r"(?i)\bprices?\b", q) else f"{q} price"


def _singular(word: str) -> str:
    low = word.lower()
    if not _PEOPLE_WORDS.fullmatch(low) or not low.endswith("s") or low.endswith("ss"):
        return word
    if low.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1]


_TERM_RE = re.compile(r"[\w][\w.+#\-]*[\w+#]|\w")
_FILLER = frozenset(
    "a an the and or of to in on at by for with from is are be how what which who".split()
)


def key_terms(v: Vertical, query: str) -> list[str]:
    q = _SITE_RE.sub(" ", search_query(v, query)).lower()
    out: list[str] = []
    for t in _TERM_RE.findall(q.replace("'s", " ").replace("’s", " ")):
        if len(t) > 1 and t not in _FILLER and t not in out:
            out.append(t)
    return out


# Words that count as each other when matching entries: profiles write degrees
# and titles many ways ("Master of Engineering", "MEng", "M.Sc."), so a query
# term matches any spelling in its group.
_SYNONYM_GROUPS = (
    ("master", "masters", "msc", "m.sc", "meng", "m.eng", "masc", "m.a.sc", "mba", "mcs"),
    ("bachelor", "bachelors", "bsc", "b.sc", "beng", "b.eng", "bcomm", "b.comm", "bba"),
    ("phd", "ph.d", "doctorate", "doctoral"),
    ("engineer", "developer", "programmer"),
    ("grad", "graduate", "alumni", "alumnus", "alum"),
)
_SYNONYMS = {w: g for g in _SYNONYM_GROUPS for w in g}


def _has_word(term: str, hay: str) -> bool:
    if re.search(rf"(?<!\w){re.escape(term)}", hay):
        return True
    return (
        len(term) > 3
        and term.endswith("s")
        and bool(re.search(rf"(?<!\w){re.escape(term[:-1])}", hay))
    )


def _has(term: str, hay: str) -> bool:
    return any(_has_word(t, hay) for t in _SYNONYMS.get(term, (term,)))


# "Ex-Shopify", "former Shopify", "Shopify Alumni", "Shopify Certified": a past
# or second-hand tie, which shouldn't count as matching "Shopify".
def _only_past(term: str, hay: str) -> bool:
    t = re.escape(term)
    past = re.compile(
        rf"(\bex-?\s?{t}|\bformer(ly)?\s+(at\s+)?{t}|\bprev(ious(ly)?)?\.?\s*@?\s*{t}|"
        rf"{t}\s+(alumni|alumnus|certified|partner|expert|plus))",
        re.I,
    )
    plain = len(re.findall(rf"(?<!\w){t}", hay, re.I))
    return plain > 0 and plain <= len(past.findall(hay))


def coverage(v: Vertical, terms: list[str], hay: str) -> float:
    """Share of the key terms an entry contains (1.0 = all of them)."""
    if not terms:
        return 1.0
    hay = hay.lower()
    hits = 0
    for t in terms:
        if not _has(t, hay):
            continue
        if v.name == "people" and _only_past(t, hay):
            continue
        hits += 1
    return hits / len(terms)


_PRICE_CAP = re.compile(r"(?i)\b(?:under|below|less than|max|<)\s*\$\s?(\d+(?:\.\d+)?)")


def price_cap(query: str) -> float | None:
    m = _PRICE_CAP.search(query)
    return float(m.group(1)) if m else None
