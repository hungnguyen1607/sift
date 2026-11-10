"""Seed packs: pre-embedded passages, built once (on a GPU) and shipped as
Parquet + a manifest.

The format mirrors the live pipeline so offsets and embeddings are consistent:
  - ``pages.parquet``      url, title, text, source, published, updated
  - ``passages.parquet``   page_url, ord, char_start, char_end, heading, embedding
  - ``manifest.json``      name, version, license, source, built_at, model, dim,
                           passage_count, sha256 per file

Embeddings are stored as ``float16`` bytes (``dim * 2`` bytes each), matching
the ``halfvec`` column in Postgres.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from sift.pipeline.chunk import Span, chunk
from sift.pipeline.embed import DIM, MODEL_NAME, embedding_input
from sift.pipeline.models import Page

PAGES_FILE = "pages.parquet"
PASSAGES_FILE = "passages.parquet"
MANIFEST_FILE = "manifest.json"

DEFAULT_MAX_PASSAGES_PER_PAGE = 80
EMBED_BATCH = 256


@dataclass(slots=True)
class Manifest:
    name: str
    version: int
    license: str
    source: str
    built_at: str
    model: str
    dim: int
    passage_count: int
    files: dict[str, str] = field(default_factory=dict)  # filename → sha256
    licenses: dict[str, str] = field(default_factory=dict)  # per-source license table

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "license": self.license,
            "source": self.source,
            "built_at": self.built_at,
            "model": self.model,
            "dim": self.dim,
            "passage_count": self.passage_count,
            "files": self.files,
            "licenses": self.licenses,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Manifest:
        return cls(
            name=d["name"],
            version=d["version"],
            license=d.get("license", ""),
            source=d.get("source", ""),
            built_at=d.get("built_at", ""),
            model=d.get("model", MODEL_NAME),
            dim=d.get("dim", DIM),
            passage_count=d.get("passage_count", 0),
            files=d.get("files", {}),
            licenses=d.get("licenses", {}),
        )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _embedding_bytes(vec: list[float]) -> bytes:
    import numpy as np

    return np.asarray(vec, dtype=np.float16).tobytes()


def build_pack(
    pages: Iterable[Page],
    out_dir: Path,
    *,
    name: str,
    license: str,
    source: str,
    embed: Callable[[list[str]], list[list[float]]],
    model: str = MODEL_NAME,
    dim: int = DIM,
    max_passages_per_page: int = DEFAULT_MAX_PASSAGES_PER_PAGE,
    batch_size: int = EMBED_BATCH,
    licenses: dict[str, str] | None = None,
) -> Manifest:
    """Chunk + embed pages with the live pipeline's code, then write the pack."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    page_rows: list[dict] = []
    passage_rows: list[dict] = []
    pending: list[tuple[Page, Span, int]] = []  # (page, span, per-page ord)

    for page in pages:
        spans = chunk(page.text)[:max_passages_per_page]
        page_rows.append(
            {
                "url": page.url,
                "title": page.title,
                "text": page.text,
                "source": page.source,
                "published": page.published,
                "updated": page.updated,
            }
        )
        for ord_i, s in enumerate(spans):
            pending.append((page, s, ord_i))

    # Embed in batches.
    embeddings: dict[int, list[float]] = {}
    texts = [embedding_input(p.title, s.heading, p.text[s.start : s.end]) for p, s, _ in pending]
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        for j, vec in enumerate(embed(batch)):
            embeddings[i + j] = vec

    for k, (page, s, ord_i) in enumerate(pending):
        passage_rows.append(
            {
                "page_url": page.url,
                "ord": ord_i,
                "char_start": s.start,
                "char_end": s.end,
                "heading": s.heading,
                "embedding": _embedding_bytes(embeddings[k]),
            }
        )

    pages_path = out_dir / PAGES_FILE
    passages_path = out_dir / PASSAGES_FILE
    pq.write_table(
        pa.table(
            {
                "url": [r["url"] for r in page_rows],
                "title": [r["title"] for r in page_rows],
                "text": [r["text"] for r in page_rows],
                "source": [r["source"] for r in page_rows],
                "published": [r["published"] for r in page_rows],
                "updated": [r["updated"] for r in page_rows],
            }
        ),
        pages_path,
    )
    pq.write_table(
        pa.table(
            {
                "page_url": [r["page_url"] for r in passage_rows],
                "ord": [r["ord"] for r in passage_rows],
                "char_start": [r["char_start"] for r in passage_rows],
                "char_end": [r["char_end"] for r in passage_rows],
                "heading": [r["heading"] for r in passage_rows],
                "embedding": pa.array(
                    [r["embedding"] for r in passage_rows], type=pa.binary(dim * 2)
                ),
            }
        ),
        passages_path,
    )

    manifest = Manifest(
        name=name,
        version=1,
        license=license,
        source=source,
        built_at=datetime.now(UTC).isoformat(),
        model=model,
        dim=dim,
        passage_count=len(passage_rows),
        files={
            PAGES_FILE: sha256_file(pages_path),
            PASSAGES_FILE: sha256_file(passages_path),
        },
        licenses=licenses or {},
    )
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest.to_dict(), indent=2))
    return manifest


def read_manifest(directory: Path) -> Manifest:
    path = Path(directory) / MANIFEST_FILE
    if not path.exists():
        raise FileNotFoundError(f"no {MANIFEST_FILE} in {directory}")
    return Manifest.from_dict(json.loads(path.read_text()))


def verify_pack(directory: Path) -> Manifest:
    """Check that every file named in the manifest exists and hashes correctly."""
    directory = Path(directory)
    manifest = read_manifest(directory)
    for filename, expected in manifest.files.items():
        path = directory / filename
        if not path.exists():
            raise FileNotFoundError(f"missing {filename} in pack")
        if sha256_file(path) != expected:
            raise ValueError(f"sha256 mismatch for {filename}")
    return manifest


# The packs Sift ships. They are built once (see `python -m sift.seeds build`) and
# hosted on GitHub Releases / Hugging Face; `url` is filled in when published.
PACKS: dict[str, dict] = {
    "devdocs": {
        "title": "Developer documentation",
        "license": "varies per site (see manifest)",
        "source": "Python, MDN, React, Node, Rails, Postgres, Redis, Docker, FastAPI, AWS",
        "size": "~100–300K passages",
        "url": None,
    },
    "wikipedia-200k": {
        "title": "Wikipedia top 200K articles",
        "license": "CC BY-SA",
        "source": "Wikimedia (top articles by pageviews)",
        "size": "~1.2–1.6M passages",
        "url": None,
    },
}
