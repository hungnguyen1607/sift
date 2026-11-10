import httpx
import respx

from sift.seeds.wikipedia import rank_titles, top_titles


def test_rank_titles_merges_across_months():
    # B appears twice → outranks A and C.
    assert rank_titles([["A", "B"], ["B", "C"]]) == ["B", "A", "C"]


def test_rank_titles_is_stable_and_ranked():
    assert rank_titles([["x"]]) == ["x"]
    assert rank_titles([]) == []


@respx.mock
async def test_top_titles_filters_special_pages():
    respx.get(url__regex=r".*pageviews/top/.*").respond(
        json={
            "items": [
                {
                    "articles": [
                        {"article": "Python", "views": 100},
                        {"article": "Main_Page", "views": 999},
                        {"article": "Special:Search", "views": 50},
                    ]
                }
            ]
        }
    )
    async with httpx.AsyncClient() as client:
        titles = await top_titles(client, months=1)
    assert titles == ["Python"]
