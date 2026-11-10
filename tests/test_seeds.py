import json

import pyarrow.parquet as pq

from sift.pipeline.embed import DIM
from sift.pipeline.models import Page
from sift.seeds.packs import (
    MANIFEST_FILE,
    PAGES_FILE,
    PASSAGES_FILE,
    build_pack,
    read_manifest,
    verify_pack,
)

DOC = (
    "# PostgreSQL JSONB\n\nPostgreSQL stores JSON as jsonb, a binary format. " * 30
    + "\n\n## Indexing\n\njsonb supports GIN indexes. " * 30
)


def fake_embed(texts):
    return [[0.25] * DIM for _ in texts]


def make_pages():
    return [
        Page("https://pg.example/jsonb", "JSONB", DOC, "devdocs"),
        Page("https://pg.example/indexes", "Indexes", "GIN indexes are fast. " * 60, "devdocs"),
    ]


def test_build_and_verify_roundtrip(tmp_path):
    manifest = build_pack(
        make_pages(), tmp_path, name="devdocs", license="x", source="docs", embed=fake_embed
    )
    assert manifest.passage_count > 0
    assert verify_pack(tmp_path) is not None

    pages = pq.read_table(tmp_path / PAGES_FILE).to_pylist()
    assert {p["url"] for p in pages} == {"https://pg.example/jsonb", "https://pg.example/indexes"}

    passages = pq.read_table(tmp_path / PASSAGES_FILE).to_pylist()
    assert len(passages) == manifest.passage_count
    # per-page ord restarts at 0
    first_ords = {}
    for p in passages:
        first_ords.setdefault(p["page_url"], p["ord"])
    assert all(v == 0 for v in first_ords.values())
    # embeddings are dim*2 bytes (float16)
    assert len(passages[0]["embedding"]) == DIM * 2


def test_offsets_match_page_text(tmp_path):
    build_pack(make_pages(), tmp_path, name="d", license="x", source="s", embed=fake_embed)
    pages = {p["url"]: p["text"] for p in pq.read_table(tmp_path / PAGES_FILE).to_pylist()}
    for p in pq.read_table(tmp_path / PASSAGES_FILE).to_pylist():
        text = pages[p["page_url"]]
        assert text[p["char_start"] : p["char_end"]].strip()


def test_verify_detects_tampering(tmp_path):
    build_pack(make_pages(), tmp_path, name="d", license="x", source="s", embed=fake_embed)
    (tmp_path / PASSAGES_FILE).write_bytes(b"tampered")
    import pytest

    with pytest.raises(ValueError, match="sha256"):
        verify_pack(tmp_path)


def test_manifest_roundtrip(tmp_path):
    build_pack(make_pages(), tmp_path, name="d", license="MIT", source="s", embed=fake_embed)
    m = read_manifest(tmp_path)
    assert m.name == "d" and m.license == "MIT" and m.dim == DIM
    raw = json.loads((tmp_path / MANIFEST_FILE).read_text())
    assert set(raw["files"]) == {PAGES_FILE, PASSAGES_FILE}
