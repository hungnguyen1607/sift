# Sift

**An open-source, self-hostable search API for AI agents.** Tavily-compatible, token-budgeted,
citation-precise, and free to run.

Sift gathers candidate links from free sources (its own local index, Wikipedia, StackExchange,
Hacker News, Marginalia, and capped free tiers of web-search APIs), then fetches, cleans, chunks
and ranks the pages, returning the best passages for an agent's context window. Every response
says which sources it used, whether it hit the cache, and what it cost (always `$0`).

> **Status: week 2 of 6.** `/v1/search` returns ranked passages with exact quote offsets,
> dates and section headings, and every fetched page is indexed in the background so repeat
> questions get faster. See the [roadmap](#roadmap).

## Run it

```bash
git clone https://github.com/hungnguyen1607/sift && cd sift
docker compose up -d            # api + worker + postgres (pgvector) + redis
curl -s localhost:8000/v1/search -H 'content-type: application/json' \
  -d '{"query": "how do I reverse a list in python", "max_results": 5}'
```

Interactive docs: <http://localhost:8000/docs>. Use `SIFT_PORT=8077 docker compose up -d` if port
8000 is taken. No API keys are needed. Optional keys go in `.env` (see `.env.example`).

## API

```
POST /v1/search  { query, max_results=5, search_depth, include_domains, exclude_domains,
                   include_raw_content, ... }
  → { query, results: [{title, url, content, score, source, sources, published, updated,
                        quote_start, quote_end, section, raw_content?}],
      response_time, providers_used, providers_skipped, cache_hit, cost_usd, timings_ms }
GET  /v1/usage   → per-provider usage and remaining free-tier budget
```

Each result is a passage (~300 words) and `raw_content[quote_start:quote_end] == content`, so
an agent can cite the exact quote. `quote_*` are null when a page couldn't be fetched and the
provider's snippet stands in.

Request fields follow Tavily's, and unknown Tavily fields (`api_key`, `topic`, …) are ignored, so
Tavily clients can point their base URL at Sift.

## How a search works

1. **Candidates (1.5 s budget).** Upstream APIs AND every term, so `"how do I reverse a list in
   python"` is sent as `"reverse list python"` (HN hits: 1 → 10). Providers run in parallel; a
   slow or failing one is dropped and reported in `providers_skipped`. Results are merged by
   normalized URL with Reciprocal Rank Fusion. In parallel, the local index runs a hybrid
   search: Postgres full-text plus HNSW vectors, fused with RRF in SQL.
2. **Quota manager.** Before each upstream call, a Redis `INCR` on
   `sift:quota:<provider>:<period>` reserves one request. Callers that land above the cap give it
   back and skip that provider, so concurrent requests can't exceed a free tier. If Redis is
   down, capped providers are refused (fail closed): spending money is impossible by design.
3. **Pages (3 s budget).** Pages already in the index are reused. The rest come from the
   cheapest polite source:
   - Wikipedia: the plain-text extracts API.
   - StackExchange: the question and its top answers arrive with the search call itself.
     StackOverflow's robots.txt disallows all crawlers, so Sift never scrapes it.
   - HN discussions: Algolia's items API.
   - Anything else: fetched with robots.txt checked on every redirect hop, an honest
     User-Agent, a 3 MB cap, and cached robots files and failures. HTML is cleaned by
     trafilatura in a process pool, keeping code blocks and tables as Markdown and extracting
     published and updated dates.
   A page that can't be fetched in time falls back to its snippet.
4. **Chunking.** ~300-word passages at paragraph boundaries. A heading starts a passage and
   labels it (`section`), and code fences and tables are never cut. Property tests check that
   the offsets are exact.
5. **Ranking.** BM25 over the pooled passages, plus bge-small-en-v1.5 vector similarity, fused
   with RRF. Indexed passages carry full embeddings. Live passages get their opening 64 words
   embedded, best-first in small batches, and only as many as the embedder's measured speed
   fits in 0.8 s; the rest rank lexically. Then near-duplicate removal (5-gram Jaccard) and at
   most 2 results per site.
6. **Indexing.** Newly fetched pages go onto a Postgres job queue (`FOR UPDATE SKIP LOCKED`,
   retries with backoff). The worker embeds full passages and writes them to the index;
   unchanged pages are skipped by content hash. Searches never wait on writes.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q        # HTTP is mocked with respx, Redis with fakeredis

# Index/queue integration tests need Postgres + pgvector (e.g. inside the compose network):
SIFT_TEST_DATABASE_URL=postgresql://sift:sift@postgres:5432/sift_test pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout:
- `sift/api`: FastAPI app, schemas, routes
- `sift/providers`: one file per source, plus `base.py` with the interface and quota manager
- `sift/pipeline`: candidates, fetch, clean, chunk, embed, rank, and `search.py`, which
  orchestrates them
- `sift/index`: migrations, hybrid search, writer
- `sift/worker`: job queue and index jobs

## Roadmap

| Week | Deliverable |
|---|---|
| 1 ✅ | Compose stack, Postgres+pgvector schema, provider interface + quota manager, Wikipedia / StackExchange / HN / Marginalia, `/v1/search`, `/v1/usage` |
| 2 ✅ | Fetch (robots.txt, cache), cleaning (dates, code/tables), chunking with offsets, embeddings, hybrid RRF, dedup/diversity, background index writer |
| 3 | Brave/Tavily/Serper fallbacks with hard caps, reranker, `max_tokens` packing, Redis caches, API keys + rate limit, `/v1/extract` |
| 4 | Query planning, `/v1/verify`, `fresh_only`, MCP server |
| 5 | Seed packs + `sift` CLI, LangChain demo, 5-minute self-host |
| 6 | Evals vs Tavily, load test, demo deploy, published image |

## Known limits (measured on a busy 8-core laptop)

- **Latency.** A first-time question takes about 2.5–3.5 s; repeats with indexed pages take
  about 1.7 s. The plan's "basic under 2 s" holds only for repeats so far.
- **Embedding throughput.** bge-small on CPU costs about 1.5 ms per word under load, so the
  worker indexes about one passage per 0.8 s on one thread. Pre-embedding the planned 3–5M-passage
  seed packs on this hardware would take weeks; seed packs will need precomputed embeddings
  or a GPU run (see week 5).
- **Marginalia.** The shared `public` key is usually rate-limited. Set `SIFT_MARGINALIA_KEY`.

## License

MIT
