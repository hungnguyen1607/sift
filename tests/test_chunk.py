import random

import pytest

from sift.pipeline.chunk import chunk


def _words(n, seed=0):
    rnd = random.Random(seed)
    vocab = "postgres index jsonb query table row column vector search page text the of".split()
    return " ".join(rnd.choice(vocab) for _ in range(n))


def _doc(seed):
    rnd = random.Random(seed)
    parts = []
    for i in range(rnd.randint(3, 25)):
        kind = rnd.random()
        if kind < 0.15:
            parts.append(f"## Section {i}")
        elif kind < 0.3:
            body = "\n".join(f"x = {j}" for j in range(rnd.randint(2, 40)))
            parts.append(f"```python\n{body}\n\n# comment after blank line\n```")
        elif kind < 0.4:
            parts.append(
                "| a | b |\n|---|---|\n" + "\n".join(f"| {j} | {j * j} |" for j in range(5))
            )
        else:
            sentences = [
                _words(rnd.randint(5, 30), seed * 100 + i * 10 + k).capitalize() + "."
                for k in range(rnd.randint(1, 30))
            ]
            parts.append(" ".join(sentences))
    sep = rnd.choice(["\n\n", "\n\n\n", "\n \n"])
    return "  \n" + sep.join(parts) + "\n\n"


@pytest.mark.parametrize("seed", range(60))
def test_offsets_are_exact_and_ordered(seed):
    text = _doc(seed)
    spans = chunk(text)
    prev_end = 0
    for s in spans:
        passage = text[s.start : s.end]
        assert passage == passage.strip() and passage
        assert s.start >= prev_end
        prev_end = s.end
    # Every non-whitespace character lands in some passage.
    covered = "".join(text[s.start : s.end] for s in spans)
    assert "".join(covered.split()) == "".join(text.split())


@pytest.mark.parametrize("seed", range(60))
def test_code_fences_are_never_cut(seed):
    text = _doc(seed)
    for s in chunk(text):
        assert text[s.start : s.end].count("```") % 2 == 0


def test_sizes_stay_near_target():
    text = "\n\n".join(_words(60, i).capitalize() + "." for i in range(50))  # 3000 words
    spans = chunk(text, target_words=300, max_words=400)
    sizes = [len(text[s.start : s.end].split()) for s in spans]
    assert all(s <= 400 for s in sizes)
    assert all(s >= 240 for s in sizes[:-1])


def test_heading_starts_a_passage_and_labels_it():
    text = _words(200).capitalize() + ".\n\n## Indexing\n\n" + _words(100, 1) + "."
    spans = chunk(text)
    assert len(spans) == 2
    assert text[spans[1].start :].startswith("## Indexing")
    assert spans[1].heading == "Indexing"
    assert spans[0].heading is None


def test_long_paragraph_splits_at_sentences():
    text = " ".join(_words(25, i).capitalize() + "." for i in range(40))  # 1000 words, 1 paragraph
    spans = chunk(text)
    assert len(spans) >= 3
    for s in spans:
        assert text[s.start : s.end].endswith(".")


def test_tiny_tail_merges_into_previous():
    text = _words(250).capitalize() + ".\n\nThe end."
    assert len(chunk(text)) == 1


def test_empty():
    assert chunk("") == [] and chunk("  \n\n ") == []
