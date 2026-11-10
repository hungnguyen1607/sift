"""`python -m sift.seeds build <pack> --out dir/` — build a seed pack.

`devdocs` crawls official docs; `wikipedia-200k` ranks articles by pageviews and
pulls text from the Hugging Face dataset. Both run best on a GPU box with
``onnxruntime-gpu`` (`--gpu`). For a custom pack, pass ``--pages-jsonl`` (one
`{url, title, text, source, published, updated}` object per line).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sift.pipeline.embed import MODEL_NAME, normalize
from sift.pipeline.models import Page
from sift.seeds.packs import PACKS, build_pack


def make_embedder(gpu: bool = False):
    from fastembed import TextEmbedding

    kwargs = {}
    if gpu:
        try:
            import onnxruntime  # noqa: F401

            kwargs["providers"] = ["CUDAExecutionProvider"]
        except Exception:  # onnxruntime-gpu not installed → fall back to CPU
            print("warning: CUDA not available, using CPU", file=__import__("sys").stderr)
    model = TextEmbedding(MODEL_NAME, **kwargs)

    def embed(texts: list[str]) -> list[list[float]]:
        return [normalize(v) for v in model.embed(texts, batch_size=64)]

    return embed


def _load_pages(path: Path) -> list[Page]:
    pages = []
    for line in path.read_text().splitlines():
        d = json.loads(line)
        pages.append(
            Page(
                url=d["url"],
                title=d.get("title") or d["url"],
                text=d["text"],
                source=d.get("source", "seed"),
                published=d.get("published"),
                updated=d.get("updated"),
            )
        )
    return pages


async def _build(args: argparse.Namespace) -> None:
    embed = await asyncio.to_thread(make_embedder, args.gpu)
    out = Path(args.out)

    if args.pages_jsonl:
        pages = await asyncio.to_thread(_load_pages, Path(args.pages_jsonl))
        manifest = await asyncio.to_thread(
            build_pack,
            pages,
            out,
            name=args.pack,
            license=args.license or "custom",
            source=args.source or "custom",
            embed=embed,
        )
        print(f"built {manifest.name}: {manifest.passage_count} passages → {out}")
        return

    if args.pack == "devdocs":
        from sift.seeds.devdocs import build_devdocs

        manifest, n_pages = await build_devdocs(
            out, embed, limit_per_source=args.limit, delay=args.delay
        )
        print(f"built devdocs: {manifest.passage_count} passages from {n_pages} pages → {out}")
    elif args.pack == "wikipedia-200k":
        from sift.seeds.wikipedia import build_wikipedia

        manifest, n_pages = await build_wikipedia(out, embed, top_k=args.top_k)
        print(
            f"built wikipedia-200k: {manifest.passage_count} passages "
            f"from {n_pages} articles → {out}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sift.seeds", description="Build seed packs")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a pack")
    b.add_argument("pack", choices=list(PACKS))
    b.add_argument("--out", required=True)
    b.add_argument("--pages-jsonl", help="build a custom pack from a JSONL of pages")
    b.add_argument("--license", help="license for a custom (--pages-jsonl) pack")
    b.add_argument("--source", help="source label for a custom (--pages-jsonl) pack")
    b.add_argument("--gpu", action="store_true")
    b.add_argument("--limit", type=int, default=500, help="devdocs: pages per source")
    b.add_argument("--delay", type=float, default=1.0, help="devdocs: seconds between requests")
    b.add_argument("--top-k", type=int, default=200_000, help="wikipedia: number of articles")
    args = parser.parse_args(argv)
    asyncio.run(_build(args))


if __name__ == "__main__":
    main()
