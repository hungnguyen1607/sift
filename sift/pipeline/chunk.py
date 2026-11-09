"""Split page text into ~300-word passages with exact character offsets.

Every span satisfies ``text[start:end] == passage``, so an agent can cite the
precise quote. Passages break at paragraph boundaries, a heading starts a new
passage, and fenced code blocks and tables are never cut unless they are huge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^(```|~~~)", re.M)
_HEADING_RE = re.compile(r"^#{1,6} +(.+)$")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass(slots=True, frozen=True)
class Span:
    start: int
    end: int
    heading: str | None = None


@dataclass(slots=True)
class _Block:
    start: int
    end: int
    words: int
    kind: str  # "text" | "code" | "heading"


def _words(s: str) -> int:
    return len(s.split())


def _blocks(text: str) -> list[_Block]:
    """Paragraphs separated by blank lines; a fenced code block is one block
    even when it contains blank lines."""
    blocks: list[_Block] = []
    pos, n = 0, len(text)
    while pos < n:
        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            break
        fence = _FENCE_RE.match(text, pos)
        if fence:
            close = re.compile(r"^" + re.escape(fence.group(1)) + r"[ \t]*$", re.M)
            line_end = text.find("\n", pos)
            m = close.search(text, line_end + 1) if line_end != -1 else None
            end = m.end() if m else n
            kind = "code"
        else:
            m = re.compile(r"\n[ \t]*\n").search(text, pos)
            end = m.start() if m else n
            first_line = text[pos : text.find("\n", pos) if "\n" in text[pos:end] else end]
            kind = (
                "heading" if _HEADING_RE.match(first_line) and "\n" not in text[pos:end] else "text"
            )
        seg_end = end
        while seg_end > pos and text[seg_end - 1] in " \t\r\n":
            seg_end -= 1
        blocks.append(_Block(pos, seg_end, _words(text[pos:seg_end]), kind))
        pos = end
    return blocks


def _split_long(text: str, b: _Block, max_words: int) -> list[_Block]:
    """Split an oversized block at sentence (prose) or line (code) boundaries."""
    seg = text[b.start : b.end]
    if b.kind == "code":
        cuts = [m.end() for m in re.finditer(r"\n", seg)]
    else:
        cuts = [m.start() for m in _SENTENCE_RE.finditer(seg)] or [
            m.start() for m in re.finditer(r" ", seg)
        ]
    pieces, start, last_cut = [], 0, 0
    for cut in [*cuts, len(seg)]:
        if _words(seg[start:cut]) > max_words and last_cut > start:
            pieces.append((start, last_cut))
            start = last_cut
        last_cut = cut
    pieces.append((start, len(seg)))
    out = []
    for s, e in pieces:
        while s < e and seg[s] in " \t\r\n":
            s += 1
        while e > s and seg[e - 1] in " \t\r\n":
            e -= 1
        if e > s:
            out.append(_Block(b.start + s, b.start + e, _words(seg[s:e]), b.kind))
    return out


def chunk(
    text: str, target_words: int = 300, max_words: int = 400, min_words: int = 40
) -> list[Span]:
    blocks: list[_Block] = []
    for b in _blocks(text):
        # Code and tables may run to 2x the max to stay whole; prose may not.
        limit = max_words * 2 if b.kind == "code" or text[b.start] == "|" else max_words
        blocks.extend(_split_long(text, b, max_words) if b.words > limit else [b])

    spans: list[Span] = []
    cur: list[_Block] = []
    heading: str | None = None
    cur_heading: str | None = None

    def flush():
        nonlocal cur
        if cur:
            spans.append(Span(cur[0].start, cur[-1].end, cur_heading))
        cur = []

    for b in blocks:
        words = sum(x.words for x in cur)
        if b.kind == "heading":
            if words >= min_words:
                flush()
            heading = _HEADING_RE.match(text[b.start : b.end]).group(1).strip()
            if not cur:
                cur_heading = heading
            cur.append(b)
            continue
        if cur and (words >= target_words or words + b.words > max_words):
            flush()
        if not cur:
            cur_heading = heading
        cur.append(b)
    flush()

    # A tiny trailing passage reads better merged into the previous one.
    if len(spans) >= 2:
        last, prev = spans[-1], spans[-2]
        if (
            _words(text[last.start : last.end]) < min_words
            and _words(text[prev.start : last.end]) <= max_words
        ):
            spans[-2:] = [Span(prev.start, last.end, prev.heading)]
    return spans
