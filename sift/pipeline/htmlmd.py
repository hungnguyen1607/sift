"""Small HTML-fragment → Markdown converter for API-supplied bodies
(StackExchange questions and answers). Keeps code blocks and tables intact."""

from __future__ import annotations

import re

from lxml import html as lxml_html

_BLOCK = {
    "p",
    "div",
    "pre",
    "ul",
    "ol",
    "blockquote",
    "table",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
}


def html_to_markdown(fragment: str) -> str:
    if not fragment or not fragment.strip():
        return ""
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    out = _blocks(root).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def _inline(el) -> str:
    parts = [el.text or ""]
    for child in el:
        parts.append(_inline_el(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _inline_el(el) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag == "code":
        return f"`{el.text_content()}`"
    if tag in ("strong", "b"):
        return f"**{_inline(el).strip()}**"
    if tag in ("em", "i"):
        return f"*{_inline(el).strip()}*"
    if tag == "br":
        return "\n"
    if tag == "img":
        return ""
    if tag in _BLOCK:
        return "\n\n" + _block(el) + "\n\n"
    return _inline(el)


def _blocks(el) -> str:
    """Render children of a container, grouping loose inline content into paragraphs."""
    out: list[str] = []
    buf = [el.text or ""]
    for child in el:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag in _BLOCK:
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
            out.append(_block(child))
        else:
            buf.append(_inline_el(child))
        buf.append(child.tail or "")
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return "\n\n".join(b for b in out if b)


def _block(el) -> str:
    tag = el.tag
    if tag == "pre":
        code = el.text_content().rstrip("\n")
        m = re.search(r"lang-([\w+#-]+)", el.get("class", ""))
        lang = m.group(1) if m and m.group(1) not in ("none", "default") else ""
        return f"```{lang}\n{code}\n```"
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return "#" * int(tag[1]) + " " + _inline(el).strip()
    if tag in ("ul", "ol"):
        items = []
        for i, li in enumerate(el.findall("li"), start=1):
            marker = f"{i}." if tag == "ol" else "-"
            body = _blocks(li).replace("\n", "\n   ")
            items.append(f"{marker} {body}")
        return "\n".join(items)
    if tag == "blockquote":
        return "\n".join("> " + line if line else ">" for line in _blocks(el).splitlines())
    if tag == "hr":
        return "---"
    if tag == "table":
        rows = [
            [" ".join(_inline(c).split()).replace("|", "\\|") for c in tr if c.tag in ("td", "th")]
            for tr in el.iter("tr")
        ]
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)
    return _blocks(el)
