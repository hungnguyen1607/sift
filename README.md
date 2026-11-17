# Sift

A self-hosted web-search engine for AI agents. Returns the best web passages with exact quote citations, inside a token budget — free, no vendor keys.

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

Sift works out which kind of result a query wants, using rules (no LLM, no keys). Set `"vertical"` to choose one yourself.

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
| `shopping` | mechanical keyboard under $100 | product pages; price and currency where the page publishes schema.org data |
| `places` | cafes near Concordia University | OpenStreetMap: address, lat/lon, opening hours |

List verticals return up to 100 entries (`max_results`, or "find 30 …" in the query) in `results[].fields`. They are built from search-engine snippets, so treat them as leads to check. Sites that block crawlers (LinkedIn, Instagram, Reddit) are never fetched. When fewer entries are found than asked for, `providers_skipped.listing` says "found K of N". When SearXNG's engines are rate-limited, `providers_skipped` names them. `searxng-settings.yml` enables Bing, Yahoo and Mwmbl next to Brave, DuckDuckGo and Google CSE, so one engine's rate limit doesn't empty a search. If you set a Brave, Serper or Tavily key, it's used when SearXNG finds nothing.
