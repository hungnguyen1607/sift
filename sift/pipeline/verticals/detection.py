"""The vertical registry and the rules that pick a vertical for a query.

``detect_vertical`` maps a query to one of the :data:`VERTICALS` by rules only
(no LLM): a ``site:`` operator first, then an ordered list of keyword rules with
guard words that veto a match (``youtube api`` is about *building*, not finding
videos), then a people heuristic, then strong academic signals, else ``web``.
"""

from __future__ import annotations

import re

from .base import Vertical
from .fields import (
    generic_fields,
    job_fields_from_title,
    news_fields,
    people_fields,
    place_fields,
    shop_fields,
    social_fields,
    video_fields,
)
from .urls import (
    _on,
    job_canonical,
    job_url_ok,
    profile_canonical,
    profile_url_ok,
    shop_canonical,
    social_url_ok,
    video_canonical,
)

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
# How many of a vertical's sites to search when the query names none.
_DEFAULT_SITES = {"people": 1, "social": 1, "jobs": 3}
# The per-vertical site tables, keyed by vertical name.
_SITE_TABLES = {"people": PEOPLE_SITES, "social": SOCIAL_SITES, "jobs": JOB_SITES}


def named_sites(v: Vertical, query: str) -> tuple[str, ...]:
    """The vertical's sites the query names ("reddit posts about …")."""
    table = _SITE_TABLES.get(v.name)
    if table is None:
        return ()
    q = f" {query.lower()} "
    return tuple(
        site
        for site, words in table.items()
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", q) for w in words)
    )


def pick_sites(v: Vertical, query: str) -> tuple[str, ...]:
    table = _SITE_TABLES.get(v.name)
    if table is None:
        return v.sites
    return named_sites(v, query) or tuple(table)[: _DEFAULT_SITES.get(v.name, 1)]


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
        parse=job_fields_from_title,
    ),
    # Product pages come from plain web search (not Wikipedia/StackOverflow/HN).
    "shopping": Vertical(
        "shopping",
        "list",
        categories="general",
        fetch=8,
        per_site=3,
        canonical=shop_canonical,
        parse=shop_fields,
    ),
    "places": Vertical("places", "list", categories="map", rank_by_source=True, parse=place_fields),
    # an explicit site: query on any other site
    "site": Vertical("site", "list", parse=generic_fields),
}


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
