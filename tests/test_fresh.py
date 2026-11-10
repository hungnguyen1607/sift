from datetime import UTC, datetime, timedelta

from sift.pipeline.fresh import (
    apply_freshness,
    brave_freshness,
    cutoff_date,
    cutoff_epoch,
    serper_tbs,
    tavily_time_range,
    window_days,
)
from sift.pipeline.models import Page, Passage
from sift.pipeline.rank import Ranked

NOW = datetime(2026, 1, 15, tzinfo=UTC)


def r(url, text, published=None, updated=None, score=0.5):
    page = Page(url, "T", text, "x", published=published, updated=updated)
    return Ranked(Passage(page, text, 0, len(text)), score)


def test_window_days():
    assert window_days("day", False) == 1
    assert window_days("week", False) == 7
    assert window_days("month", False) == 30
    assert window_days("year", False) == 365
    assert window_days(None, True) == 365  # fresh_only defaults to a year
    assert window_days(None, False) is None


def test_cutoff_date():
    assert cutoff_date("day", False, NOW) == NOW - timedelta(days=1)
    assert cutoff_date(None, False, NOW) is None
    assert cutoff_date("week", False, NOW) == NOW - timedelta(days=7)


def test_cutoff_epoch_is_unix_seconds():
    assert cutoff_epoch("day", False, NOW) == int((NOW - timedelta(days=1)).timestamp())


def test_fresh_only_drops_old_and_undated():
    fresh = r("https://a.dev/", "recent", updated=NOW - timedelta(days=2))
    old = r("https://b.dev/", "old", updated=NOW - timedelta(days=400))
    undated = r("https://c.dev/", "no date")
    out = apply_freshness([fresh, old, undated], None, True, now=NOW)
    assert [x.passage.page.url for x in out] == ["https://a.dev/"]


def test_time_range_alone_demotes_undated_but_keeps_them():
    dated = r("https://a.dev/", "recent", updated=NOW - timedelta(days=2), score=0.5)
    undated = r("https://c.dev/", "no date", score=0.5)
    out = apply_freshness([dated, undated], "week", False, now=NOW)
    kept = {x.passage.page.url: x.score for x in out}
    assert set(kept) == {"https://a.dev/", "https://c.dev/"}
    assert kept["https://c.dev/"] < kept["https://a.dev/"]  # demoted


def test_uses_updated_over_published():
    p = r(
        "https://a.dev/", "x", published=NOW - timedelta(days=500), updated=NOW - timedelta(days=3)
    )
    assert apply_freshness([p], "year", False, now=NOW)  # updated is fresh


def test_brave_freshness():
    assert brave_freshness("day") == "pd"
    assert brave_freshness("week") == "pw"
    assert brave_freshness("month") == "pm"
    assert brave_freshness("year") == "py"
    assert brave_freshness(None) is None


def test_serper_tbs():
    assert serper_tbs("day") == "qdr:d"
    assert serper_tbs("week") == "qdr:w"
    assert serper_tbs("month") == "qdr:m"
    assert serper_tbs("year") == "qdr:y"


def test_tavily_time_range():
    assert tavily_time_range("day") == "day"
    assert tavily_time_range("w") == "week"
    assert tavily_time_range("m") == "month"
    assert tavily_time_range("y") == "year"
