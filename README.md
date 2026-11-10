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
