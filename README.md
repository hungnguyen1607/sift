# Sift
Cute little project for anyone needing web search engine for their ai agents

A self-hosted web-search engine for AI agents. Returns the best web passages with exact quote citations, inside a token budget — free, no vendor keys.

It's a drop-in, **Tavily-compatible** `/v1/search` an agent can call instead of a paid search API. What makes it interesting:

- **No paid dependencies.** Federates the open web through self-hosted **SearXNG**, keeps its own **PostgreSQL + pgvector** index of pages it has already fetched, and ranks passages with **BM25 + embeddings** — no LLM and no API keys required to run it.
- **Structured only when it helps.** Rules plus a small embedding classifier detect when a query wants **jobs, products, people, places, …** and return typed `fields`, always falling back to plain web search so *any* query returns something.
- **A polite crawler.** robots.txt is respected and rate-limited engines are backed off per `Retry-After` — worked around, never evaded.

## How it works

clone → configure → start → check → search → connect your agent

1. **clone** — `git clone https://github.com/hungnguyen1607/sift && cd sift` — gets the code.
2. **configure** — `cp .env.example .env` — defaults work; all keys optional. Set a SearXNG secret: `SEARXNG_SECRET=$(openssl rand -hex 32) && sed -i "s/CHANGE_ME/$SEARXNG_SECRET/" searxng-settings.yml`.
3. **start** — `docker compose up -d --build` — runs api, worker, postgres, redis, searxng.
4. **check** — `docker compose exec api sift doctor` — verifies DB, Redis, SearXNG, and outbound access.
5. **search** — `curl -s localhost:8000/v1/search -H 'content-type: application/json' -d '{"query":"how do I reverse a list in python"}'` — returns cited passages.
6. **connect** — `claude mcp add sift -- python -m sift.mcp_server`, or `from sift import search` — lets your agent search through it.

Port 8000 taken? use `SIFT_PORT=8077 docker compose up -d --build`. API docs at `/docs`.

## Kinds of search

Send any query and you get web passages back — that's the floor, and it always works. On top of that, Sift can return **structured** results for certain kinds of query (jobs with title/company, products with price, people, places, …). Your agent controls this:

- **`vertical` omitted or `"auto"`** (default): Sift guesses from the query (rules, no LLM, no keys) and only switches to a structured shape when it's confident. If a guessed vertical finds nothing, the query automatically falls back to a plain web search (`fell_back_to_web: true`, `detected_vertical` keeps the guess) — so a query is never left empty by a wrong guess.
- **`vertical` set explicitly**: the agent names the shape it wants (e.g. `"jobs"`). No guessing, no silent fallback.

| vertical | example | returns |
|---|---|---|
| `web` | how do I reverse a list in python | quoted passages from fetched pages |
| `news` | latest openai news | quoted passages from news articles, with publisher and date |
| `travel` | things to do in Lisbon | Wikivoyage guides and travel pages |
| `academic` / `code` | arxiv paper on diffusion models | papers / docs, repos and Q&A |
| `videos` | python asyncio tutorial video | channel, duration, embed URL |
| `people` | find 20 Concordia grads at Shopify | name, headline, company, school, location (LinkedIn by default; say "on instagram" / "on github" for others; GitHub also uses GitHub's own user search by location and language) |
| `social` | reddit posts about framework laptop | Reddit/X/Mastodon/Lemmy/HN threads |
| `jobs` | remote python jobs | Lever/Greenhouse/Ashby postings (title, company, location, salary from JSON-LD), HN "Who is hiring?", Arbeitnow |
| `shopping` | mechanical keyboard under $100 | products: name, price, currency, availability. Prices come from the store's schema.org data or Shopify feed, from products a category page lists (`listed_on`), or from the result snippet (`price_source: "snippet"`). Snippet prices are not read from review/roundup pages; a store page's own schema.org Product data still sets a price |
| `places` | cafes near Concordia University | OpenStreetMap: address, lat/lon, opening hours |

List verticals return up to 100 entries (`max_results`, or "find 30 …" in the query) in `results[].fields`. They are built from search-engine snippets, so treat them as leads to check. Sites that block crawlers (LinkedIn, Instagram, Reddit) are never fetched. When fewer entries are found than asked for, `providers_skipped.listing` says "found K of N". When SearXNG's engines are rate-limited, `providers_skipped` names them. `searxng-settings.yml` enables Bing, Yahoo, Mwmbl and Mojeek (an independent index) next to Brave, DuckDuckGo and Google CSE, so one engine's rate limit doesn't empty a search. If you set a Brave, Serper or Tavily key, it's used when SearXNG finds nothing.

### When engines push back

The engines behind SearXNG rate-limit bursts. Sift works around that without evading anything:

- **Cooldowns.** An engine that answers "too many requests" or shows a CAPTCHA is skipped for 3 minutes, doubling up to 30 minutes if the block continues. Retrying it would only extend the block. `GET /v1/health` lists the engines that are cooling down and when each one will be tried again.
- **Pacing.** At most 4 SearXNG requests run at once, at least 150 ms apart.
- **No waiting on slow engines.** SearXNG answers with whatever engines replied within 2 s (1.5 s for videos and news; 3 s for maps, whose geocoders are slower) instead of waiting for the slowest.
- **Stale fallback.** If a live search finds nothing, the last good response for the same query (kept 7 days) is returned with `"stale": true` and `stale_age_s`. Empty responses are never cached.
- **Per-vertical freshness.** News is cached for 1 hour, social for 3 hours, travel and academic for 7 days, everything else for 24 hours.
- **Polite store fetching.** robots.txt is checked for every page and feed. A store that answers 429 or 503 is left alone for its `Retry-After` (1 minute to 1 hour, default 10 minutes), not just that one page.

### Detection

Sift picks the vertical with rules first. When the rules say plain web, it compares the query's embedding with about 200 labelled phrasings (`sift/pipeline/vertical_examples.py`) and switches only on a close, confident match. On 53 held-out phrasings the rules were not written for, this routes 36 correctly (31 with the rules alone), and it misroutes none of the 95 web queries in `eval/queries.jsonl` that the rules call web. Reproduce both numbers with `python eval/detection_eval.py`. Turn it off with `SIFT_VERTICAL_KNN=false`.

### Measuring it

`python eval/vertical_score.py` runs 60 vertical queries (`eval/verticals.jsonl`) against a running Sift. It scores each one with rules: routed to the right vertical, enough results, on topic, the right kind of page, fields filled in, and distance for places. It writes `eval/runs/<date>/SCORECARD.md` with p50 and p95 latency per vertical. No LLM or API key is needed. `eval/run.py` plus `eval/judge.py` compare answer search with Tavily; that comparison needs a Tavily key and an LLM key for the judge.
