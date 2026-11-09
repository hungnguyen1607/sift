"""Turn a natural-language agent question into a keyword query.

Most upstream search APIs AND every term, so filler words ("how do I", "vs",
"the") starve them: "postgres jsonb vs mysql json" gets 1 StackExchange hit,
"postgres jsonb mysql json" gets 10.
"""

import re

_STOPWORDS = frozenset(
    """
    a an the and or but if then else of to in on at by for with from into onto over about as
    is are was were be been being am do does did doing done have has had having can could
    should would will shall may might must i me my we our you your it its this that these those
    there here what which who whom whose when where why how whats hows
    please tell show explain give find get need want know best way ways
    vs versus compare comparison compared between difference differences
    """.split()
)
# Keep quoted phrases whole; keep tokens like c++, c#, node.js, gpt-4o, 3.12.
_TOKEN_RE = re.compile(r'"[^"]+"|[\w][\w.+#\-\'’]*[\w+#]|[\w]')


def keyword_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(query)
    kept = [
        t for t in tokens if t.startswith('"') or re.sub(r"['’]", "", t.lower()) not in _STOPWORDS
    ]
    return " ".join(kept) if kept else query.strip()
