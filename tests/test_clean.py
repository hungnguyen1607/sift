from sift.pipeline.clean import clean_html, tidy_markdown, wiki_extract_to_markdown
from sift.pipeline.htmlmd import html_to_markdown

ARTICLE = """<!doctype html><html><head><title>JSONB indexing guide | Example Blog</title>
<meta property="article:published_time" content="2024-03-05T10:00:00Z">
</head><body><nav>Home | About | Login</nav><article>
<h1>JSONB indexing guide</h1>
<p>PostgreSQL offers GIN indexes for jsonb columns. They make containment queries fast,
and the jsonb_path_ops operator class produces smaller indexes that support fewer operators.
This guide walks through creating them and checking that the planner uses them.</p>
<pre><code>CREATE INDEX idx_data ON events USING gin (data jsonb_path_ops);

SELECT * FROM events WHERE data @> '{"type": "click"}';</code></pre>
<p>The table below compares the two operator classes in terms of size and supported operators,
which matters when choosing between flexibility and index size on large tables.</p>
<table><tr><th>Operator class</th><th>Size</th></tr>
<tr><td>jsonb_ops</td><td>large</td></tr><tr><td>jsonb_path_ops</td><td>small</td></tr></table>
<p>In short, prefer jsonb_path_ops when you only need the containment operator.</p>
</article><footer>Copyright 2001 Example</footer></body></html>"""


def test_clean_keeps_code_tables_and_dates():
    out = clean_html(ARTICLE, "https://example.com/jsonb")
    text = out["text"]
    assert "```" in text and "CREATE INDEX idx_data" in text
    fence = text[text.index("```") : text.index("```", text.index("```") + 3)]
    assert "jsonb_path_ops);" in fence and "SELECT * FROM events" in fence
    # Markdown escapes are undone so identifiers can be quoted exactly.
    assert "| jsonb_path_ops | small |" in text
    assert "the jsonb_path_ops operator class" in text
    assert "Login" not in text and "Copyright" not in text
    assert out["published"] == "2024-03-05"
    assert out["title"] == "JSONB indexing guide | Example Blog"


def test_clean_rejects_pages_without_content():
    assert clean_html("<html><body><nav>Home</nav></body></html>") is None


def test_unescape_leaves_code_alone():
    md = "a\\_b and \\[x\\]\n\n```\nliteral\\_kept\n```\n\nc\\*d"
    assert tidy_markdown(md) == "a_b and [x]\n\n```\nliteral\\_kept\n```\n\nc*d"


def test_tidy_markdown():
    assert tidy_markdown("Fact<sup>\\[2\\]</sup>.¶\n\n\n\nNext  ") == "Fact.\n\nNext"


def test_wiki_extract():
    extract = (
        "PostgreSQL is a database.\n\n== History ==\nStarted in 1986.\n\n"
        "== Empty ==\n\n=== Also empty ===\n\n== Features ==\nMVCC.\n\n"
        "== See also ==\nMySQL\n\n== References ==\n1. x"
    )
    md = wiki_extract_to_markdown(extract)
    assert md == "PostgreSQL is a database.\n\n## History\nStarted in 1986.\n\n## Features\nMVCC."


def test_html_fragment_to_markdown():
    body = (
        "<p>Use <code>reversed()</code> or <strong>slicing</strong>:</p>"
        '<pre class="lang-py s-code-block"><code>for x in reversed(xs):\n    print(x)\n</code></pre>'
        "<ul><li>fast</li><li>lazy</li></ul><blockquote><p>Quoted</p></blockquote>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert html_to_markdown(body) == (
        "Use `reversed()` or **slicing**:\n\n"
        "```py\nfor x in reversed(xs):\n    print(x)\n```\n\n"
        "- fast\n- lazy\n\n> Quoted\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    )
