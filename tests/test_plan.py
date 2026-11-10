import pytest

from sift.pipeline.plan import plan, plan_queries

COMPARE_CASES = [
    (
        "compare Postgres and MySQL for JSON workloads",
        ["Postgres JSON workloads", "MySQL JSON workloads"],
    ),
    ("compare Postgres and MySQL", ["Postgres", "MySQL"]),
    ("comparing Redis and Memcached for caching", ["Redis caching", "Memcached caching"]),
    ("Postgres vs MySQL for JSON workloads", ["Postgres JSON workloads", "MySQL JSON workloads"]),
    ("Postgres versus MySQL", ["Postgres", "MySQL"]),
    ("python vs go for web backends", ["python web backends", "go web backends"]),
    ("difference between Postgres and MySQL", ["Postgres", "MySQL"]),
    ("what's the difference between lists and tuples", ["lists", "tuples"]),
    ("what is the difference between TCP and UDP", ["TCP", "UDP"]),
    ("Postgres or MySQL for JSON", ["Postgres JSON", "MySQL JSON"]),
    ("react or vue for a new project", ["react a new project", "vue a new project"]),
    ("postgres jsonb vs mysql json", ["postgres jsonb", "mysql json"]),
]

QUESTION_CASES = [
    (
        "What is a vector index? How do I build one?",
        ["What is a vector index", "How do I build one"],
    ),
    ("What is pgvector? Is it fast?", ["What is pgvector", "Is it fast"]),
    ("how do I start? what next?", ["how do I start", "what next"]),
]

LIST_CASES = [
    (
        "pros and cons of Postgres, MySQL and SQLite",
        ["pros and cons of Postgres", "pros and cons of MySQL", "pros and cons of SQLite"],
    ),
    ("pros and cons of Python, Java", ["pros and cons of Python", "pros and cons of Java"]),
    (
        "advantages and disadvantages of Rust, Go and C",
        [
            "advantages and disadvantages of Rust",
            "advantages and disadvantages of Go",
            "advantages and disadvantages of C",
        ],
    ),
]

SINGLE_CASES = [
    "how do I reverse a list in python",
    "postgres jsonb tutorial",
    "what is the best way to learn rust",
    "the history of the internet",
]


@pytest.mark.parametrize(("q", "expected"), COMPARE_CASES + QUESTION_CASES + LIST_CASES)
def test_plan_splits(q, expected):
    assert plan(q) == expected


@pytest.mark.parametrize("q", SINGLE_CASES)
def test_plan_passes_single_queries_through(q):
    assert plan(q) == [q]


def test_plan_caps_at_three():
    assert plan("pros and cons of a, b, c, d, e") == [
        "pros and cons of a",
        "pros and cons of b",
        "pros and cons of c",
    ]


async def test_plan_queries_uses_llm_when_configured():
    class FakeLLM:
        configured = True

        async def chat_json(self, system, user):
            return {"sub_queries": ["postgres jsonb", "mysql json"]}

    class E:
        llm = FakeLLM()

    assert await plan_queries(E(), "compare postgres and mysql") == ["postgres jsonb", "mysql json"]


async def test_plan_queries_falls_back_to_rules_on_bad_llm():
    class BadLLM:
        configured = True

        async def chat_json(self, system, user):
            return None

    class E:
        llm = BadLLM()

    assert await plan_queries(E(), "postgres vs mysql") == ["postgres", "mysql"]
