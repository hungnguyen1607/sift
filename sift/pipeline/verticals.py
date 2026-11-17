"""Search verticals: which kind of result a query wants, and where to get it.

Rule-based like the rest of Sift (no LLM). ``detect_vertical`` maps a query to
one of the verticals below; each vertical says which SearXNG categories and
``site:`` lists supply its candidates, which URLs count as real entries
(a LinkedIn profile, not a LinkedIn post), how to canonicalize them for
dedupe, and how to read structured ``fields`` (channel, price, address, …) out
of a search result.

Two result shapes:

* ``answer`` — the normal pipeline: fetch pages, quote the best passages
  (web, news, travel guides, academic, code).
* ``list`` — many entries built from search-result titles and snippets, plus
  schema.org JSON-LD from pages that allow crawling (people, videos, social,
  jobs, shopping, places).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from sift.pipeline.keywords import has_recency_words
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


# ---------------------------------------------------------------- URL helpers


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


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


# -------------------------------------------------------------- field parsers


def _literal(v):
    """SearXNG sometimes hands nested values through as Python reprs."""
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v
    return v


def _platform(url: str) -> str:
    host = _host(url)
    if host == "youtu.be":
        return "youtube"
    if host == "news.ycombinator.com":
        return "hackernews"
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def _clean(fields: Fields) -> Fields:
    return {k: v for k, v in fields.items() if v not in (None, "", [], {})}


def video_fields(c: Candidate) -> Fields:
    e = c.extra
    return _clean(
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
    return _clean(
        {
            "publisher": publisher or _host(c.url),
            "published": c.published.isoformat() if c.published else None,
            "age": age,
        }
    )


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
    return _clean(f)


def _slug_name(slug: str) -> str | None:
    """linkedin.com/in/sarah-donovan-515366146 → "Sarah Donovan"."""
    words = [w for w in re.split(r"[-_]", slug) if w.isalpha() and len(w) > 1]
    return " ".join(w.capitalize() for w in words[:4]) or None


def _fold(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _same_person(name: str, slug_name: str) -> bool:
    parts = set(_fold(slug_name).split())
    return any(w in parts for w in re.findall(r"[a-z]+", _fold(name)))


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
    return _clean(f)


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
        "lat": _float(e.get("latitude")),
        "lon": _float(e.get("longitude")),
    }
    for item in _literal(e.get("data")) or []:
        if isinstance(item, dict) and item.get("key") and item.get("value"):
            f[str(item["key"])] = item["value"]
    for link in _literal(e.get("links")) or []:
        if isinstance(link, dict) and link.get("label") == "official website":
            f["website"] = link.get("url")
    return _clean(f)


def _float(v) -> float | None:
    try:
        return round(float(v), 7)
    except (TypeError, ValueError):
        return None


def generic_fields(c: Candidate) -> Fields:
    return _clean({"platform": _platform(c.url)})


# ------------------------------------------------------------------ registry

# Sites searched for a vertical: the ones the query names ("on instagram"),
# else the defaults (the first ones). Few site: filters per query, because
# search engines handle long OR-chains poorly.
PEOPLE_SITES = {
    "linkedin.com/in": ("linkedin",),
    "instagram.com": ("instagram", "insta", "ig"),
    "github.com": ("github",),
    "x.com": ("twitter", "x.com", "on x"),
    "crunchbase.com/person": ("crunchbase", "founder", "founders"),
}
SOCIAL_SITES = {
    "reddit.com": ("reddit", "subreddit", "r/"),
    "x.com": ("twitter", "tweet", "tweets", "x.com", "on x"),
    "threads.net": ("threads",),
    "bsky.app": ("bluesky", "bsky"),
    "facebook.com": ("facebook", "fb"),
    "instagram.com": ("instagram", "insta"),
}
JOB_SITES = {
    "jobs.lever.co": (),
    "greenhouse.io": (),
    "jobs.ashbyhq.com": (),
    "linkedin.com/jobs": ("linkedin",),
    "indeed.com": ("indeed",),
}
_DEFAULT_SITES = {"people": 1, "social": 1, "jobs": 3}


def named_sites(v: Vertical, query: str) -> tuple[str, ...]:
    """The vertical's sites the query names ("reddit posts about …")."""
    table = {"people": PEOPLE_SITES, "social": SOCIAL_SITES, "jobs": JOB_SITES}.get(v.name)
    if table is None:
        return ()
    q = f" {query.lower()} "
    return tuple(
        site
        for site, words in table.items()
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", q) for w in words)
    )


def pick_sites(v: Vertical, query: str) -> tuple[str, ...]:
    table = {"people": PEOPLE_SITES, "social": SOCIAL_SITES, "jobs": JOB_SITES}.get(v.name)
    if table is None:
        return v.sites
    return named_sites(v, query) or tuple(table)[: _DEFAULT_SITES.get(v.name, 1)]


def on_site(url: str, site: str) -> bool:
    """``site`` as in a site: filter: a domain with an optional path prefix."""
    domain, _, path = site.partition("/")
    if not _on(_host(url), domain):
        return False
    return not path or "/".join(_segments(url)).startswith(path)


VERTICALS: dict[str, Vertical] = {
    "web": Vertical("web", "answer"),
    "academic": Vertical("academic", "answer", categories="science"),
    "code": Vertical("code", "answer", categories="it"),
    "news": Vertical("news", "answer", categories="news", exclusive=True, parse=news_fields),
    "travel": Vertical(
        "travel", "answer", categories="general", providers=("wikivoyage",), exclusive=True
    ),
    "videos": Vertical(
        "videos", "list", categories="videos", canonical=video_canonical, parse=video_fields
    ),
    "people": Vertical(
        "people",
        "list",
        sites=tuple(PEOPLE_SITES),
        site_providers=(("github.com", "github_users"),),
        url_ok=profile_url_ok,
        canonical=profile_canonical,
        parse=people_fields,
    ),
    "social": Vertical(
        "social",
        "list",
        categories="social media",
        sites=tuple(SOCIAL_SITES),
        per_site=None,
        providers=("hn",),
        url_ok=social_url_ok,
        parse=social_fields,
    ),
    "jobs": Vertical(
        "jobs",
        "list",
        sites=tuple(JOB_SITES),
        fetch=8,
        providers=("hn_jobs", "arbeitnow"),
        url_ok=job_url_ok,
        canonical=job_canonical,
        parse=generic_fields,
    ),
    # Product pages come from plain web search (not Wikipedia/StackOverflow/HN).
    "shopping": Vertical(
        "shopping", "list", categories="general", fetch=8, per_site=3, parse=generic_fields
    ),
    "places": Vertical("places", "list", categories="map", rank_by_source=True, parse=place_fields),
    # an explicit site: query on any other site
    "site": Vertical("site", "list", parse=generic_fields),
}
VERTICAL_NAMES = tuple(VERTICALS)


# ----------------------------------------------------------------- detection

_SITE_RE = re.compile(r"(?i)\bsite:(\S+)")

# Ordered: the first matching rule wins.
_RULES: list[tuple[str, re.Pattern]] = [
    (
        "jobs",
        re.compile(
            r"\b(internships?|job (openings?|postings?|listings?|offers?)|careers? at|"
            r"(we'?re|who'?s|companies|startups) hiring|hiring (a|an|for)\b|"
            r"jobs? (at|in|for|near)\b|(remote|junior|senior|entry[- ]level|part[- ]time|"
            r"full[- ]time|new grad|freelance|contract)\b.*\b(jobs?|roles?|positions?|openings?|"
            r"gigs?)\b|\b\w+ (developer|engineer|designer|analyst|manager|scientist|nurse|"
            r"teacher|barista|driver|intern) (jobs?|roles?|positions?|openings?)\b|"
            r"\b(jobs?|work|employment) (hiring|available)\b)"
        ),
    ),
    (
        "travel",
        re.compile(
            r"\b(flights? (to|from)|cheap flights|hotels? (in|near)|where to stay|"
            r"things to do in|itinerary|travel guide|(trip|vacation|holiday|honeymoon) (to|in)|"
            r"\d+ days in|visa requirements? for|best time to visit|weekend (in|at|getaway)|"
            r"getaway (to|in)|road trip|backpacking|places to visit|what to see in)\b"
        ),
    ),
    (
        "shopping",
        re.compile(
            r"(\b(buy|cheapest|for sale|deals? (on|for)|best price|where to buy|on sale|"
            r"discount on)\b|\bunder \$\s?\d|\$\d+\s*(or less|max|budget))"
        ),
    ),
    (
        "places",
        re.compile(
            r"\b((restaurants?|caf[eé]s?|coffee shops?|bars?|pubs?|gyms?|pharmac(y|ies)|"
            r"hospitals?|clinics?|parks?|museums?|librar(y|ies)|baker(y|ies)|grocery stores?|"
            r"supermarkets?|dentists?|atms?|gas stations?|parking|pizza|sushi|ramen|brunch)"
            r"\s+(in|near|around|close to|by)\b|near me\b|address of|opening hours (of|for)|"
            r"directions to)"
        ),
    ),
    (
        "videos",
        re.compile(
            r"\b(youtube|vimeo|dailymotion|tiktok|videos? (on|about|of|for|explaining|showing|"
            r"that|tutorials?)|(tutorial|explainer|lecture|talk|demo|review) videos?|"
            r"video tutorials?|(find|show|recommend|watch)\b.*\bvideos?|ted ?x? talks?|keynote|"
            r"trailer|livestream|music video|official video|watch (the|a|an)\b)\b|^watch\b"
        ),
    ),
    (
        "social",
        re.compile(
            r"\b(reddit|subreddit|r/\w+|mastodon|lemmy|tweets?|twitter|bluesky|hacker news|"
            r"threads\.net|on threads|posts? (about|on)|discussions? (about|on)|"
            r"what (do |are )?people (say|saying|think|thinking) about)\b"
        ),
    ),
    (
        "news",
        re.compile(
            r"\b(news|headlines|breaking|press release|what happened (in|to|at|with|on)|"
            r"latest (on|about|developments)|what'?s new (with|in|at|on))\b"
        ),
    ),
    (
        "academic",
        re.compile(
            r"\b(papers?|studies|study|research|preprints?|journal articles?|literature review|"
            r"meta-analys[ie]s)\s+(on|about|of|for|regarding|into)\b"
        ),
    ),
]
# Words that mean the query is about building software ("youtube api quota",
# "cron jobs", "reddit scraper"), not about finding that kind of result.
_GUARDS: dict[str, re.Pattern] = {
    "videos": re.compile(
        r"\b(api|sdk|upload|embed|download|codec|ffmpeg|algorithm|scrape|bitrate|thumbnails?|"
        r"monetiz\w*|subscribers)\b|^how (to|do|can|should) (i |you )?(make|create|start|grow|"
        r"edit|record|upload|download|monetize)\b"
    ),
    "jobs": re.compile(
        r"\b(cron|celery|sidekiq|background|batch|queue|kubernetes|github actions|ci|sql|"
        r"steve jobs|scheduler|airflow)\b"
    ),
    "social": re.compile(r"\b(api|sdk|scrape|scraper|library|bot|oauth)\b"),
    "news": re.compile(r"\b(api|rss parser|scrape|scraper)\b|^how (to|do|does|can|should)\b"),
    "people": re.compile(
        r"\b(api|sdk|library|how (do|to|can)|tutorial|salary|salaries|interview)\b"
    ),
}
_PEOPLE_WORDS = re.compile(
    r"\b(people|persons?|profiles?|alumni|alumnus|students?|grads?|graduates?|engineers?|"
    r"developers?|employees?|founders?|recruiters?|ceos?|ctos?|designers?|researchers?|"
    r"creators?|influencers?|youtubers?|streamers?|accounts?|scientists?|managers?|pms?|"
    r"analysts?|marketers?|consultants?|executives?|directors?|vps?|interns?|professors?|"
    r"artists?|photographers?|writers?|journalists?|investors?|partners?|leaders?|coach(es)?|"
    r"trainers?|models?|chefs?|athletes?)\b"
)
# Naming a profile platform alongside people words is enough on its own
# ("instagram fitness influencers in miami").
_PROFILE_PLATFORM = re.compile(r"\b(linkedin|instagram|github|crunchbase)\b")
_PEOPLE_VERBS = re.compile(
    r"^(find|list|show|search|get|who are|who is|looking for)\b|\b(profiles?|people who|"
    r"working at|who work(s)? at|currently at|on linkedin|on instagram|on github|on x)\b"
)
_COUNT_LEAD = re.compile(
    r"^(?:find|list|show|get|give me|search for)\s+(?:me\s+)?(?:the\s+)?(\d{1,3})\b"
)
_COUNT_NOUN = re.compile(
    r"(?<![\d.])\b(\d{1,3})\s+(?:\w+\s+){0,3}?(?:people|persons|profiles|accounts|alumni|"
    r"students|grads|graduates|engineers|developers|employees|founders|recruiters|creators|"
    r"influencers|videos|jobs|roles|postings|places|restaurants|cafes|posts|threads|products)\b"
)

_ACADEMIC_STRONG = re.compile(
    r"\b(papers?|arxiv|preprints?|et al|journals?|peer-reviewed|citations?|doi|"
    r"systematic review|meta-analys[ie]s)\b"
)

_SITE_VERTICAL = [
    (lambda h, s: _on(h, "linkedin.com") and s[:1] == ["jobs"], "jobs"),
    (lambda h, s: _on(h, "linkedin.com"), "people"),
    (lambda h, s: _on(h, "crunchbase.com"), "people"),
    (lambda h, s: h in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com"), "videos"),
    (lambda h, s: _on(h, "tiktok.com"), "videos"),
    (lambda h, s: _on(h, "reddit.com") or _on(h, "facebook.com"), "social"),
    (
        lambda h, s: _on(h, "threads.net") or _on(h, "bsky.app") or _on(h, "mastodon.social"),
        "social",
    ),
    (
        lambda h, s: (
            (_on(h, "instagram.com") or _on(h, "x.com") or _on(h, "twitter.com"))
            and s[:1] in (["p"], ["reel"], ["status"])
        ),
        "social",
    ),
    (lambda h, s: _on(h, "instagram.com") or _on(h, "x.com") or _on(h, "twitter.com"), "people"),
    (lambda h, s: h.endswith(("lever.co", "greenhouse.io", "ashbyhq.com", "indeed.com")), "jobs"),
]


def site_target(query: str) -> tuple[str, list[str]] | None:
    """(host, path segments) of a ``site:`` operator, if any."""
    m = _SITE_RE.search(query)
    if not m:
        return None
    raw = m.group(1).strip("\"'").lower()
    host, _, path = raw.partition("/")
    return host.removeprefix("www."), [s for s in path.split("/") if s]


def detect_vertical(query: str) -> str:
    q = " ".join(query.lower().split())
    site = site_target(q)
    if site:
        host, segs = site
        for test, name in _SITE_VERTICAL:
            if test(host, segs):
                return name
        return "site"
    for name, rx in _RULES:
        guard = _GUARDS.get(name)
        if rx.search(q) and not (guard and guard.search(q)):
            return name
    if (
        _PEOPLE_WORDS.search(q)
        and (_PEOPLE_VERBS.search(q) or requested_count(q) or _PROFILE_PLATFORM.search(q))
        and not _GUARDS["people"].search(q)
    ):
        return "people"
    # Only strong signals: "theorem" or "benchmark" alone are ordinary questions.
    if _ACADEMIC_STRONG.search(q):
        return "academic"
    return "web"


def requested_count(query: str) -> int | None:
    """``find 30 engineers`` / ``25 remote python jobs`` → the number. Versions
    ("python 3.12 features") and durations ("5 days in Lisbon") don't count."""
    q = " ".join(query.lower().split())
    m = _COUNT_LEAD.search(q) or _COUNT_NOUN.search(q)
    if m is None:
        return None
    n = int(m.group(1))
    return n if 1 < n <= 100 else None


def strip_count(query: str) -> str:
    """Drop "find 30" / "list 25" so search engines see only the subject."""
    return (
        re.sub(r"(?i)^\s*(find|list|show|get|give me)\s+(me\s+)?\d{1,3}\s+", "", query).strip()
        or query
    )


def wants_recent(query: str) -> bool:
    return has_recency_words(query)


# ------------------------------------------------- query cleaning + coverage

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


def _has(term: str, hay: str) -> bool:
    if re.search(rf"(?<!\w){re.escape(term)}", hay):
        return True
    return (
        len(term) > 3
        and term.endswith("s")
        and bool(re.search(rf"(?<!\w){re.escape(term[:-1])}", hay))
    )


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
