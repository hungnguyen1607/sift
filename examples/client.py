"""Drop-in web search for any project or AI agent that can't do its own web
search (e.g. models without browsing), or as a self-hosted replacement for a
web-search API like Brave/Tavily.

The client lives in `sift/client.py` (stdlib only, so you can copy it into
another project). Here's the whole call:

    from sift import search
    for r in search("who created Rust?")["results"]:
        print(r["title"], r["url"], r["content"])

Run this demo against a running Sift:

    SIFT_URL=http://localhost:8000 python examples/client.py "your question"
"""

from __future__ import annotations

import sys

from sift import search


def main() -> None:
    queries = sys.argv[1:] or ["how do I reverse a list in python?"]
    for q in queries:
        out = search(q)
        for i, r in enumerate(out["results"], 1):
            print(f"[{i}] {r['title']} — {r['url']}")
            print(f"    {r['content'][:160]}")
        print(
            f"providers: {', '.join(out.get('providers_used', []))}  "
            f"({out.get('response_time')}s, cache_hit={out.get('cache_hit')})\n"
        )


if __name__ == "__main__":
    main()
