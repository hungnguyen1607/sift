# Sift

**An open-source, self-hostable search API for AI agents.** Tavily-compatible, token-budgeted,
citation-precise, and free to run.

Sift gathers candidate links from free sources (its own local index, Wikipedia, StackExchange,
Hacker News, Marginalia, GitHub, arXiv/OpenAlex, and capped free tiers of Brave/Tavily/Serper),
then fetches, cleans, chunks, ranks and token-packs the pages, returning the best passages for an
agent's context window. Every response says which sources it used, whether it hit the cache, and
what it cost (always `$0`).

> **Status: week 6 of 6.** `/v1/search` returns ranked, token-budgeted passages with exact quote
> offsets, caches repeat queries, splits multi-part questions, filters by freshness, and supports
> API keys + rate limits. `/v1/extract` fetches clean text, `/v1/verify` checks claims, and an MCP
> server exposes it all to agents. See the [roadmap](#roadmap).

## Architecture

```mermaid
flowchart LR
    A[Agent / Tavily client] -->|POST /v1/search| API[FastAPI API]
    API --> Q[Query planner]
    Q --> C[Providers]
    C -->|Wikipedia · SE · HN · GitHub · arXiv| C
    C -->|SearXNG · Brave → Tavily → Serper| F[Web backend]
    C -->|local| I[(Postgres + pgvector)]
    C --> P[Fetcher · clean · chunk]
    P --> R[rank · rerank · pack]
    R --> API
    API --> Cache[(Redis: cache · quota · rate limit)]
    API --> J[(Postgres job queue)]
    J --> W[Worker → embed → index]
    API -->|/v1/verify| V[Verify]
    API -->|/v1/extract| E[Extract]
    MCP[MCP server] --> API
```

## Running in 5 minutes

```bash
git clone https://github.com/hungnguyen1607/sift && cd sift
cp .env.example .env
docker compose up -d               # api + worker + postgres (pgvector) + redis + searxng
docker compose exec api sift seed load devdocs ./path/to/devdocs-pack
curl -s localhost:8000/v1/search -H 'content-type: application/json' \
  -d '{"query": "how do I reverse a list in python", "max_results": 5}'
```

The Compose file pulls the prebuilt image from GHCR, so nothing builds locally and the embedding
model is already baked in. Use `SIFT_PORT=8077 docker compose up -d` if port 8000 is taken.
No API keys are needed: Sift runs on the local index + public APIs, plus a self-hosted **SearXNG**
instance for general web search (so "search anything" works out of the box, no vendor keys).
Optional provider and LLM keys go in `.env` (see `.env.example`). Interactive docs:
<http://localhost:8000/docs>.

## API

```
POST /v1/search  { query, max_results=5, max_tokens?, search_depth, include_domains,
                   exclude_domains, fresh_only?, time_range?, include_raw_content,
                   include_answer?, ... }
  → { query, results: [{title, url, content, score, source, sources, published, updated,
                        quote_start, quote_end, section, raw_content?}],
      sub_queries?, answer?, response_time, providers_used, providers_skipped, cache_hit,
      cost_usd, timings_ms, tokens_used? }
POST /v1/extract { urls:[...] } → { results:[{url, title, raw_content, published, updated}],
                                    failed_results:[{url, error}], response_time }
POST /v1/verify  { claim, max_sources=5 } → { verdict: supported|contradicted|unclear,
                                              confidence, supporting:[…], contradicting:[…] }
GET  /v1/usage   → per-provider usage and remaining free-tier budget
```

Each result is a passage (~300 words) and `raw_content[quote_start:quote_end] == content`, so
an agent can cite the exact quote. `quote_*` are null when a page couldn't be fetched and the
provider's snippet stands in. With `max_tokens`, results are greedily packed to fit the budget
(never cutting a passage) and the response carries `tokens_used`.

Request fields follow Tavily's, and unknown Tavily fields (`api_key`, `topic`, …) are ignored, so
Tavily clients can point their base URL at Sift.

**Auth and rate limits.** By default (`SIFT_AUTH=off`) Sift is anonymous for local self-hosting.
Set `SIFT_AUTH=on` and manage keys with `python -m sift.cli keys create --name demo | list |
revoke <prefix>`. Keys are `sk_sift_…`, stored as sha256 only, and accepted as a Bearer token or a
Tavily-style `api_key` body field. Every key is rate-limited (60 rpm by default); responses carry
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.

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
   fits in 0.8 s; the rest rank lexically. On `search_depth=advanced`, a cross-encoder reranker
   (ms-marco-MiniLM-L-6-v2 by default) rescored the top ~15 passages within a 1.5 s budget. Then
   near-duplicate removal (5-gram Jaccard), at most 2 results per site, and token-budget packing.
6. **Indexing.** Newly fetched pages go onto a Postgres job queue (`FOR UPDATE SKIP LOCKED`,
   retries with backoff). The worker embeds full passages and writes them to the index;
   unchanged pages are skipped by content hash. Searches never wait on writes.

### Web search backend

"Search anything" comes from a web backend, and Sift ships one by default: a **self-hosted
SearXNG** instance (federates Google, Bing, DuckDuckGo, …) in the Compose stack. Sift queries
SearXNG's JSON API, then fetches, cleans, ranks and cites the links — so the HTML parsing lives in
SearXNG, not Sift, and keeping the container updated (`docker compose pull searxng`) is the only
"engine changed" maintenance. `SIFT_SEARCH_WEB` controls when it fires:

- `always` (default) — on every cache miss, so a fresh query always hits the web; cache hits still
  return in ~13 ms without re-scraping.
- `auto` — only on thin/recency/`advanced` queries.
- `off` — never.

Optional named tiers (Brave → Tavily → Serper) can be added as backups by setting their free keys;
each is capped below its free tier and the quota manager makes overspending impossible. SearXNG is
uncapped (self-hosted), so it's the primary backend with the named tiers as fallback.

> Honest note: SearXNG scrapes Google/Bing/DuckDuckGo, which is ToS-gray, and heavy use can get
> your IP rate-limited. It's fine for personal/agent use; SearXNG's limiter plus the cache-first
> `always` mode keep it proportional to your actual new-query volume.

### Caches

- **Query cache:** a request's normalized form is sha256'd; the JSON response is zlib-compressed
  and cached for 24 h (1 h for `fresh_only`/`time_range=day`). A hit returns with `cache_hit:
  true` and the current `response_time`.
- **Single-flight:** concurrent identical queries wait for the first one's result instead of each
  fanning out.
- **URL → page cache:** clean page text for 24 h, so pages fetched but not yet indexed are served
  instantly.

### Query planning

Multi-part questions are split before searching (rule-based, no LLM needed):
`"compare Postgres and MySQL for JSON workloads"` → `["Postgres JSON workloads", "MySQL JSON
workloads"]`; several sentences in one are split at question marks; lists like
`"pros and cons of X, Y and Z"` become one sub-query per item. At most 3 sub-queries, run in
parallel with a shared fetch budget and page pool, then merged round-robin so every side appears.
The response includes `sub_queries`. With `SIFT_LLM_*` configured, an LLM planner is tried first
and the rules are the fallback.

### Freshness

`time_range` (day/week/month/year) sets a window; `fresh_only` without a range means one year.
The date used is `max(published, updated)`. `fresh_only` drops undated passages; `time_range`
alone demotes them. Recency is passed upstream where possible (HN `numericFilters`, StackExchange
`fromdate`, Brave `freshness`, Tavily `time_range`, Serper `tbs`).

### Optional answer model

`include_answer=true` returns a `answer` field. Off by default: without `SIFT_LLM_*` configured
the answer is `null` and `providers_skipped` notes "no LLM configured". With a model, the packed
passages are passed numbered and the model must cite `[n]` and refuse when they don't contain the
answer.

### `/v1/verify`

Checks a claim: runs a search, classifies each passage as supporting/contradicting/unrelated,
and returns a `verdict` (supported needs ≥2 different sites; contradicted needs a strong
contradiction that outweighs support). With an LLM the classification is one batched call and the
quote must be an exact substring (checked in code); without one the verdict is always `unclear`
and the top passages are returned ranked by relevance.

### MCP server

`claude mcp add sift -- python -m sift.mcp_server` (set `SIFT_URL`/`SIFT_API_KEY`; also works
against the hosted demo). Exposes `search`, `extract` and `verify` as tools that call the HTTP
API, returning compact text an agent can cite as `[n]`.

### Seed packs

Embedding on CPU is slow (~1.5 ms/word), so big knowledge bases are built **once on a GPU and
shipped pre-embedded**. A pack is a directory of Parquet files plus a `manifest.json` (license,
source, model, dim, passage count, per-file sha256). The builder uses the same `clean`, `chunk`
and `embedding_input` code as the live pipeline, so offsets and embeddings match:

```bash
python -m sift.seeds build devdocs --out devdocs-pack/ --gpu --limit 500   # crawls official docs
python -m sift.seeds build wikipedia-200k --out wiki-pack/ --gpu           # pageviews + HF dataset
python -m sift.seeds build devdocs --pages-jsonl pages.jsonl --out custom/ # a custom pack
docker compose exec api sift seed load devdocs ./devdocs-pack
docker compose exec api sift seed list      # available packs
docker compose exec api sift seed status    # what's installed
```

The `devdocs` builder crawls each official docs site with the live `Fetcher` (robots.txt, honest
User-Agent, one request per second per host) and records a per-site license table in the manifest.
`wikipedia-200k` ranks the top articles by pageviews and pulls clean text from the Hugging Face
`wikimedia/wikipedia` dataset, capping at ~8 passages per article. The loader bulk-`COPY`s into
staging, upserts pages (`ON CONFLICT` with live data winning when newer), and drops/rebuilds the
HNSW index for big loads. See `plan.md` week 5 for measured costs.

A fresh-machine test (clone → `docker compose up` → a search, timed) is `scripts/self_host_test.sh`.

### CLI

`sift` is a single entry point (inside the container: `docker compose exec api sift …`):

```bash
sift seed list | load <pack> <dir> | status
sift keys create --name demo | list | revoke <prefix>
sift stats        # pages, passages, queue depth/lag, cache hit rate
sift search "query" [--json]
sift doctor       # DB, Redis, provider keys, LLM, outbound access
```

### Ecosystem

Tavily's own clients (`tavily-python`, `langchain`'s `TavilySearch`) hard-code
`https://api.tavily.com` and don't accept a base URL, so Sift ships a thin `SiftSearch` tool
(`examples/sift_search.py`) plus `examples/langchain_agent.py` — a 3-question agent that cites
its sources as `[n]`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q        # HTTP is mocked with respx, Redis with fakeredis

# Index/queue integration tests need Postgres + pgvector (e.g. inside the compose network):
SIFT_TEST_DATABASE_URL=postgresql://sift:sift@postgres:5432/sift_test pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout:
- `sift/api`: FastAPI app, schemas, routes, auth and rate limit
- `sift/providers`: one file per source, plus `base.py` with the interface and quota manager
- `sift/pipeline`: candidates, fetch, clean, chunk, embed, rerank, pack, cache, extract, plan,
  fresh, answer, verify, and `search.py`, which orchestrates them
- `sift/index`: migrations, hybrid search, writer
- `sift/worker`: job queue and index jobs
- `sift/seeds`: seed-pack format, builder and loader
- `sift/cli`: the `sift` CLI (seed, keys, stats, search, doctor)
- `sift/mcp_server`: the MCP server (stdio)

## Roadmap

| Week | Deliverable |
|---|---|
| 1 ✅ | Compose stack, Postgres+pgvector schema, provider interface + quota manager, Wikipedia / StackExchange / HN / Marginalia, `/v1/search`, `/v1/usage` |
| 2 ✅ | Fetch (robots.txt, cache), cleaning (dates, code/tables), chunking with offsets, embeddings, hybrid RRF, dedup/diversity, background index writer |
| 3 ✅ | GitHub/OpenAlex/arXiv, Brave/Tavily/Serper tiers with hard caps, reranker, `max_tokens` packing, query+page caches, API keys + rate limit, `/v1/extract` |
| 4 ✅ | Query planning, `/v1/verify`, `fresh_only`/`time_range`, optional answer model, MCP server |
| 5 ✅ | Seed-pack format/builder/loader, `sift` CLI (seed/keys/stats/search/doctor), LangChain demo, 5-minute self-host |
| 6 ✅ | Eval tooling (queries, runner, LLM judge, metrics), k6 load test, deploy configs, CI/CD, publish docs |

## Measured numbers

These come from `python eval/run.py` + `python eval/judge.py` + `python eval/compare_tavily.py`
(100 mixed queries, LLM-judged, blind) and `k6 run eval/load_test.js`. Nothing here is fabricated;
run the scripts and paste the output.

| Metric | Sift | Tavily |
|---|---|---|
| relevance@5 | _run eval_ | _run eval_ |
| nDCG@5 | _run eval_ | _run eval_ |
| tokens / useful answer | _run eval_ | _run eval_ |
| p50 / p95 latency (warm) | _run load test_ | — |
| cost per query | $0.000 | — |

## Honest limits

- **Latency.** A first-time question takes about 2.5–3.5 s; repeats with indexed pages take
  about 1.7 s, and cached queries return in well under 200 ms. The plan's "basic under 2 s" still
  only holds for repeats.
- **Reranker.** The default is `Xenova/ms-marco-MiniLM-L-6-v2` (0.08 GB), the smallest and
  fastest candidate, chosen so advanced searches stay within budget on 2 cores. Set
  `SIFT_RERANKER_MODEL` to try `jinaai/jina-reranker-v1-turbo-en` or `BAAI/bge-reranker-base`
  (1 GB, best quality but likely too slow for a shared VPS). It loads lazily on the first
  advanced search.
- **Embedding throughput.** bge-small on CPU costs about 1.5 ms per word under load, so the
  worker indexes about one passage per 0.8 s on one thread. Pre-embedding the planned 3–5M-passage
  seed packs on this hardware would take weeks; seed packs will need precomputed embeddings
  or a GPU run (see week 5).
- **Seed packs are code-complete but the full data isn't built.** The pack
  format, builder, crawler and loader are implemented and tested, and a small
  `devdocs` pack was built end-to-end from live Python docs to prove the
  pipeline. The full `devdocs` (~100–300K passages) and `wikipedia-200k`
  (~1.2–1.6M passages) still need a one-time off-box build (crawl official docs /
  pull the HF dump, embed on a free GPU) and publishing to GitHub Releases /
  Hugging Face.
- **Marginalia.** The shared `public` key is usually rate-limited. Set `SIFT_MARGINALIA_KEY`.
- **Science providers.** arXiv and OpenAlex are only queried when a query looks academic, and
  arXiv is throttled to one request per 3 s. Without a GitHub token, GitHub search is disabled.

## License

MIT
