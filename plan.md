# Sift: the open-source search API for AI agents

> An open-source, self-hostable search API for AI agents: Tavily-compatible, token-budgeted,
> citation-precise, and free to run.

## Context

Sift is a general-purpose search API for AI agents, like Tavily: any question an agent asks. It's a
tool you'll use from Claude Code, Cursor and your own agents, and your flagship resume project
(backend, infra, AI tooling).

"Free" alone isn't a reason to pick it, so Sift wins on three things Tavily doesn't offer:

1. **Open source and self-hostable.** One `docker compose up` gives anyone their own agent search.
   It's private (queries stay on your box except capped fallbacks), has no vendor lock-in, and
   costs $0 per query. SearXNG is self-hosted but returns raw links, not agent-ready passages;
   nothing fills this gap well.
2. **Agent-native output:**
   - token budgets
   - exact quote citations
   - dates on every result
   - code blocks and tables kept intact
   - multi-part question splitting
   - a `/verify` endpoint for checking claims
3. **Shows its work:** every response says which sources were used, which were skipped and why,
   whether it hit the cache, per-stage timings, and the cost ($0).

The honest constraint: nobody can index the whole web for free. Sift copies what Tavily most likely
does:

- get candidate links from many free sources
- fetch the pages live (or read them through the source's API)
- clean, chunk, embed and rank paragraphs
- return the best ones
- save everything into a local index that grows with every search, so repeat-ish questions come
  back fast

## Status (as of 2026-11-08)

| Week | State | Notes |
|---|---|---|
| 1 | ✅ done | Compose, schema, provider interface and quota manager, 4 providers, `/v1/search`, `/v1/usage` |
| 2 | ✅ done | Fetch, clean, chunk, embed, rank, background index writer; returns passages with exact quotes |
| 3 | ✅ done | GitHub/OpenAlex/arXiv, Brave/Tavily/Serper tiers, reranker, `max_tokens`, query+page caches, auth+rate limit, `/v1/extract` |
| 4 | ✅ done | Query planning, `/v1/verify`, `fresh_only`/`time_range`, optional answer model, MCP server |
| 5 | ✅ done | Seed-pack format/builder/crawler/loader, devdocs + wikipedia builders, `sift` CLI, LangChain demo, fresh-machine script; full pack data build + VM test are off-box |
| 6 | ✅ done (tooling) | Eval suite (queries, runner, LLM judge, metrics), k6 load test, deploy configs, CI/CD, publish docs; measuring/deploy/publish are external |
| — | — | — |

- 325 tests pass (Python 3.12), including Postgres integration tests.
- Nothing is committed yet: only `git init` has been run.

## Setup

- Repo: `~/sift`, MIT license. Code lives in the `sift/` Python package.
- Local dev: `SIFT_PORT=8077 docker compose up -d` (port 8000 is taken on the dev machine).
- DB integration tests: run pytest in a container on the `sift_default` network with
  `SIFT_TEST_DATABASE_URL=postgresql://sift:sift@postgres:5432/sift_test`.
- Demo hosting: your Contabo VPS (x86, 6 vCPU, 11 GB RAM, about 80 GB free). It shares the box with
  other apps, so the budget is about 3 GB RAM and 2–3 cores.

## How one search works

```
search("compare Postgres and MySQL for JSON workloads", max_tokens=2000)
 1. plan       split multi-part questions into sub-queries ("Postgres JSON…", "MySQL JSON…")   [week 4]
 2. cache      24 h query cache in Redis → return immediately on a hit                        [week 3]
 3. candidates in parallel, 1.5 s budget, ~10 links per provider
      • local index           Postgres full-text + pgvector HNSW, fused with RRF in SQL       ✅
      • keyword rewrite       drop filler words for upstream APIs (they AND every term)       ✅
      • public APIs           Wikipedia · StackExchange · HN Algolia · Marginalia             ✅
                              GitHub · arXiv/OpenAlex                                          [week 3]
      • free web-search tiers Brave → Tavily → Serper, only when the above are thin,
                              hard caps, at most one per search                               [week 3]
 4. pages      3 s budget; top 5 (basic) / 10 (advanced) links not already indexed             ✅
      • Wikipedia → plain-text extracts API; StackExchange → question + top answers from the
        search call itself (StackOverflow's robots.txt blocks all crawlers); HN discussions →
        Algolia items API
      • everything else → httpx fetch, robots.txt on every redirect hop, honest UA, 3 MB cap,
        cached robots files and failures
      • unfetchable in time → provider snippet stands in (quote offsets null)
 5. clean      trafilatura → Markdown, code blocks + tables kept, published/updated dates,
               Markdown escapes undone so identifiers quote exactly; runs in a process pool     ✅
 6. chunk      ~300-word passages, exact character offsets, headings label sections,
               code fences and tables never cut                                               ✅
 7. rank       BM25 + bge-small vectors → RRF; live passages embed their first 64 words,
               best first, only as many as fit in 0.8 s (adaptive to measured speed)          ✅
               advanced: cross-encoder rerank of top ~15                                      [week 3]
               near-duplicate removal (5-gram Jaccard), max 2 per site                        ✅
 8. pack       greedily fill max_tokens with the best passages (no half-cut passages)        [week 3]
 9. return     [{title, url, content, quote_start, quote_end, section, published, updated,
                 score, source, sources}]                                                     ✅
10. save       new pages → Postgres job queue → worker embeds full passages → local index     ✅
```

## Agent-native features (what makes it better, not just free)

| Feature | What the agent gets | Why it matters | State |
|---|---|---|---|
| `max_tokens` budget | the best passages that fit its context window | Tavily returns N results of unknown size; agents waste context | week 3 |
| Exact quote citations | `quote_start`/`quote_end` offsets into `raw_content`, plus the exact text and its section | cite precisely, fewer hallucinated citations | ✅ |
| Dates on every result | `published` / `updated`; `fresh_only=true` filter | time-sensitive questions stop getting stale answers | dates ✅, filter week 4 |
| Structure kept | code blocks and tables as Markdown | coding and data questions need them intact | ✅ |
| Query planning | multi-part questions split, then merged | "compare X and Y" covers both sides | week 4 |
| `POST /v1/verify` | `{claim}` → supporting / contradicting passages + verdict | agents can check their own answers | week 4 |
| Transparency | `providers_used`, `providers_skipped`, `cache_hit`, `timings_ms`, `cost_usd: 0` | debuggable, and shows the self-hosted value | ✅ |
| Tavily-compatible | same request and response fields; unknown Tavily fields ignored | LangChain/CrewAI Tavily tools work by changing the base URL (to verify, week 5) | ✅ fields |

## Free sources

| Source | Good for | Cost / limit | How content is read | State |
|---|---|---|---|---|
| Local index | anything seen before, plus seed packs | free | stored passages + embeddings | ✅ |
| Wikipedia API | facts, entities, definitions | free (polite rate, honest UA) | TextExtracts API (plain text) | ✅ |
| StackExchange API | programming Q&A | 300/day without a key, 10K/day with one (caps 280 / 9,500) | answers come with the search call (custom filter) | ✅ |
| Hacker News (Algolia) | tech news and discussion | free | articles fetched; discussions via items API | ✅ |
| Marginalia | independent and small websites | free; shared `public` key is almost always rate-limited | fetched | ✅ code, ⚠️ never seen a real response |
| GitHub search API | repos, READMEs, issues | free with a token (search: 30 requests/min) | README via API (raw Markdown) | week 3 |
| arXiv / OpenAlex | research papers | free (OpenAlex: polite pool with `mailto`; arXiv: 1 request/3 s) | abstracts via API | week 3 |
| Brave Search API | general web | about 1,000 a month ($5 free credit; set a $5 usage limit) | fetched | week 3 |
| Tavily | general web, fallback | about 1,000 a month free, no card | its `content` as snippet; fetched | week 3 |
| Serper | Google results, last resort | 2,500 once (lifetime) | fetched | week 3 |

- **Quota manager:** Redis counters per provider per day, month or lifetime, capped below each free
  tier. A call is reserved with `INCR` before it is made; if Redis is down, capped providers are
  refused. Spending money is impossible by design.
- **APIs only:** Sift never scrapes Google, Bing or DuckDuckGo result pages, and never fetches sites
  whose robots.txt disallows it (StackOverflow is read only through its API).
- **Self-hosters** plug in their own keys, or run without fallbacks: local index + public APIs only.
- **Free-tier numbers** change; recheck each provider's pricing page when signing up and set the
  caps in config.

## Stack

- **API:** FastAPI + Pydantic v2, uvicorn
- **Index:** Postgres 16 + pgvector (`halfvec(384)`, HNSW) + `tsvector` full-text; keyword and vector
  search in one database, fused with RRF in SQL (HNSW confirmed in EXPLAIN; 23 ms on 50k passages)
- **Models (free, CPU, fastembed/ONNX):**
  - embeddings: `BAAI/bge-small-en-v1.5` (baked into the image)
  - reranker: to be picked by benchmark in week 3 (see below); `bge-reranker-base` is 1 GB and
    likely too slow on the VPS
  - token counting: `tiktoken` (`o200k_base`), or a chars/4 estimate if the dependency is too heavy
- **Cleaning:** trafilatura + htmldate, in a 2-process pool
- **Redis:** quota counters, robots.txt cache, failed-URL cache ✅; API-key rate limits, 24 h query
  cache, URL → page cache (week 3)
- **Worker:** Postgres `FOR UPDATE SKIP LOCKED` job queue, retries with backoff, stuck-job recovery,
  content-hash skip. Searches never wait on writes. ✅
- **MCP server** (official Python SDK, stdio): `search(query, max_tokens, depth, include_domains,
  fresh_only)`, `extract(urls)`, `verify(claim)` (week 4)
- **Optional answer and verdict model:** off by default; self-hosters can point it at any
  OpenAI-compatible or Anthropic endpoint (week 4)
- **Packaging:** Docker Compose (api, worker, postgres, redis), a `sift` CLI (seed, keys, stats),
  GitHub Actions (ruff + pytest + image build to GHCR), Caddy for HTTPS on the demo VPS

## API

```
POST /v1/search  { query, max_results=5, max_tokens?, search_depth="basic"|"advanced",
                   include_domains, exclude_domains, fresh_only=false, time_range?,
                   include_raw_content=false, include_answer=false }
  → { query, results:[{title, url, content, quote_start, quote_end, section, published, updated,
                       score, source, sources, raw_content?}],
      sub_queries?, answer?, response_time, providers_used, providers_skipped, cache_hit,
      cost_usd, timings_ms, tokens_used? }
POST /v1/extract { urls:[...] } → { results:[{url, title, raw_content, published, updated}],
                                     failed_results:[{url, error}], response_time }
POST /v1/verify  { claim, max_sources=5 } → { verdict: supported|contradicted|unclear,
                                              confidence, supporting:[…], contradicting:[…] }
GET  /v1/usage   → this key's requests + remaining free-tier budget per provider
```

- **Basic:** local + public APIs + up to 5 live pages. Target under 2 s; currently 2.5–3.5 s cold,
  about 1.7 s warm.
- **Advanced:** adds the free web tiers, 10 pages and the reranker, under 5 s.
- **Auth:** `Authorization: Bearer sk_sift_…` (or Tavily-style `api_key` in the body), keys stored
  hashed. 60 requests per minute per key. Auth is optional for local self-hosting
  (`SIFT_AUTH=off`).

## Repo layout

```
sift/
  sift/
    api/          main.py, deps.py (Engine), schemas.py, routes/{search,usage}.py ✅
                  routes/{extract,verify}.py, auth.py, ratelimit.py                     [3–4]
    providers/    base.py (interface + quota), wikipedia.py, stackexchange.py, hn.py,
                  marginalia.py ✅ · github.py, openalex.py, arxiv.py, brave.py,
                  tavily.py, serper.py                                                  [3]
    pipeline/     candidates.py, keywords.py, fetch.py, clean.py, htmlmd.py, chunk.py,
                  embed.py, rank.py, models.py, search.py ✅
                  rerank.py, pack.py, cache.py                                          [3]
                  plan.py, verify.py, fresh.py, answer.py                               [4]
    index/        db.py, migrations/{001,002}.sql, search.py, writer.py, vectors.py ✅
    worker/       queue.py, jobs.py, __main__.py ✅
    seeds/        devdocs.py, wikipedia.py, packs.py                                    [5]
    cli/          sift.py (seed, keys, stats, search)                                   [3, 5]
    mcp_server/   server.py                                                             [4]
  eval/           queries.jsonl, compare_tavily.py, token_efficiency.py, citations.py,
                  load_test.js, runs/                                                   [6]
  examples/       langchain_tavily.py, crewai.py                                        [5]
  deploy/         Caddyfile, docker-compose.prod.yml, backup.sh                         [6]
  docker-compose.yml, Dockerfile, .github/workflows/ci.yml, README.md, LICENSE (MIT), plan.md
```

## Milestones

About 6 weeks part-time. Each week ends with something working, all tests green, and a commit.

### Week 1: foundations ✅

- Repo, Compose, Postgres + pgvector; provider interface + quota manager
- Wikipedia, StackExchange, HN, Marginalia; `/v1/search` returns merged links; `/v1/usage`
- Keyword rewrite for upstream APIs (HN hits for a natural question went from 1 to 10)

### Week 2: passages ✅

- Fetch + robots + caches; cleaning (dates, code/tables); chunking with offsets
- Embeddings; BM25 + vector RRF; dedup/diversity
- Returns passages; background index writer (job queue + worker)
- API content paths for Wikipedia, StackExchange and HN; adaptive live-embedding budget

### Week 3: fallbacks, budgets, caches, auth

**Before starting:** commit Weeks 1–2; register a StackExchange app key (300 → 10K/day); get a
Marginalia key; sign up for Brave, Tavily and Serper and put the keys in `.env`.

**1. Remaining free providers** (the plan promises 9 sources; 4 exist)
- `github.py`: `GET /search/repositories?q=…&per_page=10` with `Authorization: Bearer $SIFT_GITHUB_TOKEN`.
  - Candidate = repo URL + description.
  - `owns(github.com/{owner}/{repo})` → `fetch_page` reads the README through
    `GET /repos/{o}/{r}/readme` (`Accept: application/vnd.github.raw`), so it's already Markdown
    with code blocks.
  - Cap: search allows 30 requests/min; add a per-minute period to the quota manager.
  - Optional later: issue search for error messages.
- `openalex.py`: `GET https://api.openalex.org/works?search=…&per-page=10&mailto=…`
  (polite pool).
  - Content is the abstract, rebuilt from `abstract_inverted_index`.
  - Dates come from `publication_date`.
- `arxiv.py` (optional if OpenAlex covers it): Atom API, 1 request per 3 s.
  - Enforce with a Redis lock per provider.
- Only query science providers when the query looks academic. A cheap classifier: words like
  paper, study, arxiv, "et al", theorem, dataset. Otherwise they add latency and noise.
- Tests: respx fixtures per provider, owns/fetch_page, the abstract reconstruction.

**2. Free web-search tiers with hard caps**
- `brave.py`:
  - `GET https://api.search.brave.com/res/v1/web/search?q=&count=10`, header
    `X-Subscription-Token`.
  - Parse `web.results[].{title,url,description,page_age}`.
  - Cap 900/month; Brave also allows only 1 request/s, so enforce that with a Redis per-second key.
- `tavily.py`:
  - `POST https://api.tavily.com/search` with Bearer key, body
    `{query, max_results: 10, search_depth: "basic"}`.
  - Parse `results[].{title,url,content,score,published_date}`.
  - Cap 900/month.
- `serper.py`:
  - `POST https://google.serper.dev/search`, header `X-API-KEY`, body `{q, num: 10}`.
  - Parse `organic[].{title,link,snippet,date}`.
  - Cap 2,400, lifetime: add `period="lifetime"` (key without a date).
- **When to escalate** (the "thin" rule): after the free providers return, call **one** fallback
  if any of these hold:
  - fewer than 4 candidates in total
  - no candidate's title or snippet contains a majority of the query keywords
  - the query has recency words (today, latest, 2026, news, release, price)
  - `search_depth="advanced"` (always gets one fallback)
- Order: Brave → Tavily → Serper. Take the first with quota left; a failure moves to the next.
- Latency: start the fallback right away for recency or advanced queries, instead of waiting for
  the free stage to finish.
- Each tier can be turned off: an empty key means the provider isn't built.
- Plan's quota test: cap Brave at 3, run 5 thin searches, confirm searches 4–5 skip Brave and use
  Tavily, and `providers_skipped.brave == "quota exhausted"`.
- `GET /v1/usage` shows `used / cap / remaining / period` for each tier.

**3. Reranker** (`pipeline/rerank.py`, advanced only)
- Benchmark first on the target hardware (a 2-CPU container limit): 15 query–passage pairs,
  passages cut to about 256 tokens. Candidates (all in fastembed `TextCrossEncoder`):

  | Model | Size | Expectation |
  |---|---|---|
  | `Xenova/ms-marco-MiniLM-L-6-v2` | 0.08 GB | fastest; likely default |
  | `jinaai/jina-reranker-v1-tiny-en` | 0.13 GB | fast, longer context |
  | `jinaai/jina-reranker-v1-turbo-en` | 0.15 GB | middle |
  | `BAAI/bge-reranker-base` | 1.04 GB | best quality, probably too slow for 2 cores |

- Pick the best quality model that reranks 15 passages in under 1.5 s. Record the numbers in the
  README.
- Score: final = rerank score for the top 15; the rest keep RRF order below them.
- Same pattern as live embedding: time budget, best-first, partial results kept.
- Load lazily, in the API process only, with its own executor.
- Memory check: the API container stays under 1 GB.

**4. Token-budget packing** (`pipeline/pack.py`)
- `count_tokens(text)` via `tiktoken.get_encoding("o200k_base")`, with an LRU cache.
- Per-result overhead: count title + URL + about 20 tokens for JSON keys.
- Greedy: go through passages in rank order; skip any that don't fit and keep trying smaller
  ones; never cut a passage.
- When `max_tokens` is set, `max_results` becomes an upper bound (default 20).
- Return `tokens_used`.
- Tests, property-based over random passage sizes: never exceeds `max_tokens`, never splits, and
  a budget smaller than any passage returns `[]`.

**5. Redis caches** (`pipeline/cache.py`)
- **Query cache:**
  - Key = sha256 of the request after normalization: lowercase, collapse whitespace, sorted
    domain lists, `api_key` removed.
  - Stored value = the response JSON, compressed with zlib; TTL 24 h (1 h when `time_range=day`
    or `fresh_only`).
  - A hit returns `cache_hit: true` with the stored results; `response_time` is the new value.
- **Single-flight:** `SET NX` lock per key for 10 s. Concurrent identical queries wait (up to 5 s)
  for the first one's result instead of each fanning out.
- **URL → page cache:** clean page text, zlib-compressed, 24 h, for pages fetched but not yet
  indexed (worker lag).
- **Memory:** Redis `maxmemory 192mb` with `volatile-lru`; every cache key has a TTL, while quota
  keys are never evicted (they also have TTLs, but they're tiny).
- **Metrics:** hit/miss counters, shown in `sift stats`.

**6. API keys + rate limit**
- Migration `003_api_keys.sql`: `api_keys(id, name, prefix, key_hash unique, created_at,
  revoked_at, rpm_limit default 60, monthly_limit null)`.
- Key format: `sk_sift_` + 32 url-safe random bytes. Store sha256 only: the keys are high-entropy,
  so no bcrypt is needed. Store the first 12 characters as `prefix` for display.
- `api/auth.py` dependency:
  - `SIFT_AUTH=off` (default) → anonymous.
  - `on` → require a Bearer token, or `api_key` in the JSON body (Tavily compatibility).
  - 401 for missing or invalid keys, 403 for revoked keys.
  - Cache key lookups in-process for 60 s.
- `api/ratelimit.py`: Redis sliding window (two fixed windows weighted).
  - Over the limit → 429 with `Retry-After`.
  - Every response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.
- Per-key monthly request counters feed `/v1/usage`.
- Minimal CLI now: `python -m sift.cli keys create --name demo | list | revoke <prefix>` (the full
  CLI comes in week 5).
- Tests: 401/403/429, header values, the body `api_key` path, and auth off.

**7. `POST /v1/extract`**
- Up to 20 URLs, deduplicated by normalized URL.
- Order of sources for each URL:
  1. the index (if indexed less than 7 days ago)
  2. the URL → page cache
  3. a provider that `owns` the URL
  4. a fetch, then clean
- Budget 5 s overall; per-URL failures go to `failed_results` with a reason (robots, timeout,
  HTTP 404, unsupported type).
- Pages fetched here are queued for indexing too.
- Response shape follows Tavily's extract: `results`, `failed_results`, `response_time`.

**8. Latency work** (carried over: cold basic is 2.5–3.5 s against the plan's 2 s)
- **Streaming candidates:** start fetching a page as soon as any provider returns it, instead of
  after all providers finish (an `asyncio.Queue` between the stages). This saves up to 1 s.
- **Fetch budget** for basic 3.0 → 2.0 s; advanced stays at 3.0.
- **Warm-up:** pre-warm the process pool and the embedder at startup (embedder ✅).
- **Measure:** add `timings_ms` per provider to the logs, and track p50/p95 locally with a
  20-query script.
- **Target:** basic cold p50 ≤ 2.2 s; warm (cache hit) ≤ 200 ms.

**Week 3 done when:**
- All of the above have tests.
- The quota test passes.
- `max_tokens=500` never returns more than 500 tokens.
- A repeated query returns in under 200 ms with `cache_hit: true`.
- Auth on → 401 without a key; 429 at the 61st request in a minute.
- `/v1/extract` works on 5 mixed URLs (Wikipedia, SO, a blog, a PDF → failed, a robots-blocked
  site → failed).
- Commit.

### Week 4: planning, verify, freshness, MCP

**1. Query planning** (`pipeline/plan.py`)
- **Rule-based by default** (no LLM needed):
  - "compare X and Y (for Z)", "X vs Y (for Z)", "difference between X and Y", "X or Y for Z" →
    `["X Z", "Y Z"]`
  - several questions in one ("What is X? How do I Y?") → split at question marks
  - lists ("pros and cons of X, Y and Z") → one sub-query per item, with shared context
  - at most 3 sub-queries; if nothing matches, the query passes through unchanged
- **Optional LLM planner** when `SIFT_LLM_*` is configured: a JSON-mode prompt returning
  `{"sub_queries": [...]}`, 1 s timeout, falling back to the rules.
- **Execution:**
  - Each sub-query runs candidates + pages in parallel, sharing one fetch budget and one page
    pool (don't fetch the same URL twice).
  - Quota is counted per sub-query call. Fallback tiers are allowed for at most one sub-query
    per search.
- **Merge:** rank each sub-query's passages separately, then round-robin so every sub-query gets
  at least 1 result (coverage), then dedup, diversity and packing.
- The response includes `sub_queries`.
- **Tests:**
  - a parser table of about 30 phrasings
  - coverage: a fake provider per side, so both sides appear in the results
  - a single-part query is unchanged

**2. `fresh_only` and `time_range`** (`pipeline/fresh.py`)
- `time_range` (day, week, month, year, or d/w/m/y) sets a window. `fresh_only` without a range
  means 1 year.
- The date used is `max(published, updated)`. For Wikipedia, only `updated` is known (last edit),
  and the docs should say so.
- With `fresh_only`, undated passages are dropped. With only `time_range`, undated passages are
  ranked lower (score × 0.5).
- Pass recency to providers where possible:
  - HN `numericFilters=created_at_i>…`
  - StackExchange `fromdate`
  - Brave `freshness=pd|pw|pm|py`
  - Tavily `time_range`
  - Serper `tbs=qdr:d|w|m|y`
- Tests: the filter math, undated handling, and provider parameter mapping.

**3. Optional answer model** (`pipeline/answer.py`, `include_answer=true`)
- Off by default; `include_answer=true` without a model configured → `answer: null` and a note in
  `providers_skipped`.
- Config:
  - `SIFT_LLM_PROVIDER=anthropic|openai`, `SIFT_LLM_BASE_URL`, `SIFT_LLM_API_KEY`,
    `SIFT_LLM_MODEL`
  - suggested Anthropic default: `claude-haiku-5-5`
  - any OpenAI-compatible server works (Ollama, vLLM, LM Studio)
- Prompt: the packed passages, numbered; the answer must cite `[n]`; refuse when the passages
  don't contain the answer.
- Timeout 4 s; the answer is never cached separately (it lives in the query cache).

**4. `POST /v1/verify`** (`pipeline/verify.py`)
- Run a search with the claim as the query (planner on; `max_results` = 2 × `max_sources`).
- **Classify each passage** as supports / contradicts / unrelated:
  - With an LLM configured: one batched call returning JSON
    `[{id, label, confidence, quote}]`. The quote must be an exact substring, checked in code;
    otherwise the label is dropped.
  - Without an LLM: a small NLI cross-encoder if a benchmark shows it fits (look for an ONNX
    `nli-deberta-v3-xsmall` export). Otherwise the verdict is always `unclear`, with the
    supporting passages ranked by relevance. The README must be honest about this.
- **Verdict rules:**
  - `supported`: at least 2 different sites support with confidence ≥ 0.7, and none contradicts
    at ≥ 0.7
  - `contradicted`: at least 1 contradicts at ≥ 0.8, and contradictions outweigh support
  - otherwise `unclear`
- Response: `verdict`, `confidence`, `supporting[]` and `contradicting[]`, each with url, quote
  offsets and the exact quote.
- **Test set:** `eval/claims.jsonl` with 10 known-true and 10 known-false claims (for example
  "Python 3.12 removed the distutils module" is true; "PostgreSQL was first released in 2005" is
  false). The target from the plan: 10 true/false claims get the right verdicts, with sources.

**5. MCP server** (`sift/mcp_server/server.py`)
- Official `mcp` Python SDK (`FastMCP`), stdio transport.
- Talks to the HTTP API (`SIFT_URL`, `SIFT_API_KEY`) instead of importing the pipeline, so it stays
  light and works against the hosted demo too.
- **Tools:**
  - `search(query, max_tokens=2000, depth="basic", include_domains=None, fresh_only=False)`:
    returns compact text, not JSON. Each result is
    `[n] title — url (published) \n > exact quote`, followed by a line with providers and
    cache info.
  - `extract(urls)`: returns each page's clean Markdown, cut to a token limit.
  - `verify(claim)`: returns the verdict plus the cited quotes.
- Tool descriptions should tell the model when to use each tool and to cite `[n]` with its URL.
- **Install:** `claude mcp add sift -- python -m sift.mcp_server`; also document Cursor's
  `mcp.json`.
- **Tests:** in-process MCP client calling each tool against a fake HTTP API (respx), plus a manual
  check: ask Claude Code a question and confirm it calls `search` and cites the results.

**Week 4 done when:**
- "compare Postgres and MySQL for JSON workloads" returns results about both.
- `fresh_only` drops old results.
- The verify claim set is scored (record the accuracy, whatever it is).
- Claude Code uses the MCP server end to end.
- Commit.

### Week 5: seed packs, CLI, ecosystem, self-host

**The embedding problem, measured in week 2:** bge-small on this CPU costs about 1.5 ms per word
under load, so the worker indexes about 1 passage per 0.8 s. The planned 3–5M passages would take
about 1–1.5 months of CPU. Seed packs therefore have to be **built once on a GPU and shipped
pre-embedded**.

**1. Pack format and builder** (`sift/seeds/packs.py`)
- **Format:** a directory of Parquet files plus `manifest.json`.
  - Manifest fields: name, version, license, source, built_at, model, dim, passage count, sha256
    per file.
  - Columns:
    - pages: `url, title, text, source, published, updated`
    - passages: `page_url, ord, char_start, char_end, heading, embedding (float16[384])`
- **Builder:** `python -m sift.seeds build <pack> --out dir/`.
  - Uses the **same** `clean`, `chunk` and `embedding_input` code as the live pipeline, so the
    offsets and embeddings are consistent.
  - Runs fastembed on GPU (`onnxruntime-gpu`, `providers=["CUDAExecutionProvider"]`) on free
    Kaggle or Colab GPUs.
  - Expected speed: about 1,000+ passages/s on a T4, so about 1M passages in 20–30 minutes. Check
    on the first run.
- **Hosting:** GitHub Releases (2 GB per file limit, so split the files) or a Hugging Face dataset
  repo (better for multi-GB). Record the download size per pack.

**2. Packs**
- **devdocs:** docs for Python, JS/TS (MDN), React, Node, Rails, Postgres, Redis, Docker, FastAPI,
  AWS basics.
  - Prefer each project's **docs source repo or official download** over crawling: Python docs
    HTML archive, the MDN `content` repo (CC-BY-SA), Postgres docs tarball, React/Node/FastAPI
    GitHub docs folders.
  - Crawl only where there's no download: polite crawler using `Fetcher` (robots), 1 request/s per
    host, sitemap-driven.
  - Keep a per-site license table in the manifest.
  - Expected size: about 100–300K passages.
- **wikipedia-200k:** the top ~200K articles by pageviews.
  - Rank with the Wikimedia pageviews API (top articles per month, summed over 12 months, minus
    Main_Page and special pages).
  - Text from the Hugging Face `wikimedia/wikipedia` English Parquet dataset (already clean
    text), or the official dump.
  - **Cap at about 8 passages per article** (lead + first sections), which gives about 1.2–1.6M
    passages instead of 4M. This fits the ~4 GB disk budget with HNSW and keeps the build
    affordable.
  - License: CC BY-SA. Results from this pack carry `license` and an attribution line.

**3. Loader** (`sift seed load <pack>`)
- Download with resume and sha256 checks.
- Bulk load with `COPY` into staging tables, then `INSERT … ON CONFLICT (url_norm)`. Live data wins
  when newer.
- **For big loads, drop the HNSW index and rebuild it afterwards.** Inserting 50k rows with a live
  HNSW index took over 5 minutes on this machine.
- Rebuild with `SET maintenance_work_mem = '1GB'` and
  `max_parallel_maintenance_workers = 2`, inside the 3 GB budget. Measure the build time for 1M
  rows and record it.
- Progress output, resumable, and idempotent (rerunning is safe).
- `sift seed list` shows the available packs, their sizes and what's installed.

**4. `sift` CLI** (`sift/cli/sift.py`, console script `sift`)
- `sift seed list | load <pack>... | status`
- `sift keys create --name | list | revoke`
- `sift stats`: pages, passages, index size on disk, query cache hit rate, provider usage this
  month, worker queue depth and lag
- `sift search "query" [--json]`: debug output with timings
- `sift doctor`: checks DB, Redis, model files, provider keys, and outbound access to each
  provider
- Typer or argparse; works inside the container (`docker compose exec api sift …`).

**5. Ecosystem demos** (`examples/`)
- **Verify first** which Tavily clients accept a custom base URL:
  - `tavily-python` (`TavilyClient`: look for an `api_base_url` or similar option)
  - `langchain-tavily` (`TavilySearch`)
  - CrewAI's Tavily tool
- Record the results in the README:
  - If a client accepts a base URL, the demo is: change the base URL, everything else stays.
  - If it doesn't, ship a 20-line `SiftSearch` LangChain tool and say plainly in the README that
    the client hard-codes Tavily's URL.
- `examples/langchain_agent.py`: an agent answering 3 questions with citations.

**6. One-command self-host**
- README "Running in 5 minutes":
  - `git clone`, `cp .env.example .env`, `docker compose up -d`
  - `docker compose exec api sift seed load devdocs`
  - `curl` example and the MCP setup
- Compose pulls the GHCR image by default, so nothing builds locally. The model is already baked
  in.
- **Test on a fresh machine:** a clean VM (multipass or a fresh cloud VM) must go from nothing to a
  working search in 5 minutes. Time it and record the result.

**Week 5 done when:**
- devdocs is built, published and loaded.
- wikipedia-200k is built, or there's a documented plan with its measured cost.
- The CLI works.
- The fresh-machine test passes.
- The LangChain demo works, with the verified compatibility finding written down.
- Commit.

### Week 6: measure, deploy, publish

**1. Evaluation** (`eval/`). Only measured numbers go in the README and resume.
- `queries.jsonl`: 100 queries, 20 each of coding, facts, news/recent, how-to and multi-part. Each
  has `id`, `query`, `category` and optional `notes` (what a good answer needs).
- `run.py`: runs Sift (basic and advanced) and Tavily (basic, 100 calls, inside its free tier).
  - Raw responses are saved to `eval/runs/<date>/`, so judging can be re-run **without
    re-spending** Tavily credits.
- `judge.py`: LLM judge (for example `claude-sonnet-5-5` via the Anthropic API).
  - Labels each top-5 result 0/1/2 (irrelevant / partial / answers).
  - Randomized order, blind to which system produced it.
  - Hand-label 20 queries and report the agreement between you and the judge.
- Metrics:
  - **Relevance@5:** share of top-5 results labeled ≥ 1, plus nDCG@5.
  - **Token efficiency** (headline): tokens returned per query where the judge says the answer
    can be derived from the results. Compare Sift (`max_tokens=1500`) and Tavily (default
    content).
  - **Citation precision:** the judge writes a claim from each result and checks whether the
    cited quote supports it. Sift uses exact quotes; Tavily uses its content snippet.
  - **Latency:** p50/p95 per system from the run logs.
  - **Cache value:** after a week of the demo, the share of queries answered fully from the local
    index, and cost per query (target $0.000).
- Output: Markdown tables printed by `compare_tavily.py` and `token_efficiency.py`, pasted into the
  README.

**2. Load test** (`eval/load_test.js`, k6)
- 10 virtual users, 5 minutes; 70% repeated queries, 30% new ones from a list.
- Report p50/p95 for cold and warm, error rate, and `docker stats` peak memory.
- **Run with the paid-tier fallbacks disabled**
  (`SIFT_ENABLED_PROVIDERS=wikipedia,stackexchange,hn,marginalia,github,openalex`). A load test
  must not burn the monthly free quotas.
- Watch the worker queue depth; if it grows without bound, document it or add a second worker.

**3. Deploy to Contabo**
- **First, check the VPS:**
  - what already owns ports 80 and 443
  - whether a reverse proxy is already running; if so, add a site to it instead of a second
    Caddy
  - free RAM and disk
- `deploy/docker-compose.prod.yml`:
  - GHCR image pinned to a tag
  - Postgres and Redis not published to the host
  - memory limits: api 1 GB, worker 768 MB, postgres 1.5 GB, redis 256 MB
  - log rotation (`json-file`, `max-size: 10m`)
- **Caddy:** `sift.<your-domain>` → `api:8000`, automatic HTTPS.
- **Config:** `SIFT_AUTH=on`, demo keys, a lower per-key limit (for example 20 requests per
  minute) for public keys.
- **Backups:** nightly `pg_dump` via cron to a local file with 7-day retention (optionally also
  copied off the box).
- **Monitoring:** an `/healthz` check (Uptime Kuma, or a cron + curl + PushNotification-style
  alert) and a weekly `sift stats` snapshot.
- **Check:** the public `/docs` loads, and `docker stats` stays within about 3 GB while the other
  apps keep running normally.

**4. CI/CD**
- Existing CI: ruff + pytest + an image push to GHCR on `main`.
- Add a Postgres service container in CI so the integration tests run there.
- Tag releases `v0.x`; the image gets semver tags.
- Optional deploy job: SSH + `docker compose pull && up -d`, run manually.

**5. Publish**
- **README:**
  - Mermaid architecture diagram
  - demo GIF (recorded with `vhs` or asciinema → gif): a Claude Code session using the MCP
    server
  - the measured numbers table
  - "Honest limits"
- **Resume bullets** filled in from the measurements (templates below).
- Make the GitHub repo public: check the license, that no secrets are in history, and that
  `.env` is ignored.
- Optional: a Show HN post, and a post in the MCP servers list.

**Week 6 done when:** the eval tables are in the README, the load test has been run, the demo is
live behind HTTPS, there's a tagged release, and the repo is public.

## Stretch goals

A web playground, int8 vector quantization, a local small-LLM answer mode, a Helm chart, per-domain
trust scores, issue search for GitHub, a second worker for faster indexing.

## Evaluation (where the resume numbers come from)

1. **Relevance:** 100 mixed queries (coding, facts, news, how-to, multi-part). LLM judge plus hand
   checks give relevance@5 for Sift vs Tavily (about 100 Tavily calls, inside its free tier).
2. **Token efficiency:** tokens returned per useful answer, Sift (with `max_tokens`) vs Tavily.
   This is the headline agent metric.
3. **Citation precision:** the share of cited quotes that actually support the claim (judge
   check).
4. **Latency:** p50/p95 for basic and advanced, cold vs warm. k6 at 10 concurrent users on the
   VPS.
5. **Cache value:** the share of queries answered fully from the local index after a week, and the
   cost per query (target $0.000).
6. The resume and README carry only measured numbers.

## Resume bullet templates (fill in after measuring)

- Built Sift, an open-source, self-hostable search API for AI agents (FastAPI, Postgres/pgvector,
  Redis): federates N free sources, live-fetches and ranks pages, and grows a local index of N
  passages; Tavily-compatible, $0 per query.
- Designed agent-native retrieval (hybrid full-text + HNSW vectors, RRF, cross-encoder rerank,
  token-budget packing, quote-offset citations), reaching X% of Tavily's relevance@5 with Y% fewer
  tokens per useful answer.
- Shipped an MCP server, a /verify claim-checking endpoint and a one-command Docker deploy; a
  per-provider quota manager guarantees zero spend; p95 Z ms warm on a shared 6-vCPU VPS.

## Honest limits

- Coverage of breaking news and obscure pages depends on about 2,000 free web searches a month (on
  the demo), plus the growing cache.
- First-time questions take about 2.5–3.5 s (live fetching; measured in week 2). Cached ones should
  come back in under 200 ms once the query cache lands.
- Not a full web index. It's an answer engine over what free sources point to, plus everything it
  has seen.
- Embedding on CPU is slow (about 1.5 ms per word under load). Live ranking embeds only passage
  openings within a budget, and seed packs must be built on a GPU.
- StackOverflow is read only through its API (its robots.txt blocks crawlers), so API quotas
  limit it.
- Without an LLM configured, `/verify` can rank evidence but may only say `unclear`.

## Risks and open decisions

| Risk / decision | Impact | Plan |
|---|---|---|
| Reranker too slow on 2 cores | advanced mode over 5 s | benchmark in week 3; use MiniLM-L6 or skip the rerank under load |
| Seed pack GPU build | packs late or smaller | free Kaggle/Colab GPU; cap Wikipedia at ~8 passages/article |
| HNSW build memory on the VPS | OOM next to the other apps | build the index offline or on the dev machine; load into the VPS with `pg_dump`/restore |
| Tavily clients may hard-code their URL | "change the base URL" claim may be false | verify in week 5; ship a small adapter if needed |
| Free-tier terms change | quota caps wrong | caps in config; check the pricing pages at signup |
| Verify without an LLM | weak verdicts | NLI model if it fits, otherwise document `unclear` |
| VPS ports 80/443 already used | Caddy can't bind | reuse the existing proxy |
| Domain for the demo | needed for HTTPS | **you decide** before week 6 |
| LLM for the eval judge | small API cost | **you decide** (Anthropic API suggested) |

## Verification

- `docker compose up` then pytest:
  - providers (mocked HTTP), quota caps never exceeded, robots handling, chunk offsets match page
    text, RRF math, dedup ✅
  - token packing never exceeds `max_tokens`, auth/429 (week 3)
- Search spot-checks: 10 varied queries return relevant passages with correct quotes and dates.
  `providers_used` shows paid-tier fallbacks only when needed.
- Quota test: cap Brave at 3, run 5 searches, and confirm searches 4–5 skip Brave.
- Cache test: the same query twice; the second has `cache_hit: true` and comes back in under
  200 ms.
- Verify: 10 known-true and 10 known-false claims get the right verdicts, with sources.
- MCP: `claude mcp add sift …`, then ask Claude Code a question and confirm it calls `search` and
  cites the results.
- Self-host test: a fresh machine runs `git clone` + `docker compose up` +
  `sift seed load devdocs`, and searches work within 5 minutes.
- Deploy: the public `/docs` loads. `docker stats` stays within about 3 GB while the VPS's other
  apps run normally.
- Eval: `python eval/compare_tavily.py` and `eval/token_efficiency.py` print the head-to-head
  tables.
