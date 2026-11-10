"""An agent that answers three questions using Sift, with citations.

Requires a running Sift API (see README) and `langchain` + `langchain-openai`
(`pip install langchain langchain-openai`). The `SiftSearch` tool is in
`sift_search.py`; Tavily's own clients hard-code their URL, so this uses Sift's
API directly through a thin tool.
"""

from __future__ import annotations

import os

from examples.sift_search import SiftSearch, search_sift

QUESTIONS = [
    "What is PostgreSQL JSONB and when should I use it?",
    "How does pgvector's HNSW index work?",
    "What are the trade-offs between Redis and Memcached for caching?",
]


def run_without_langchain() -> None:
    """Fallback when LangChain isn't installed: call the tool directly."""
    tool = SiftSearch(base_url=os.environ.get("SIFT_URL", "http://localhost:8000"))
    for q in QUESTIONS:
        print(f"\nQ: {q}\n")
        print(search_sift(q, max_tokens=tool.max_tokens, base_url=tool.base_url))


def run_with_langchain() -> None:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    tools = [SiftSearch(base_url=os.environ.get("SIFT_URL", "http://localhost:8000"))]
    agent = create_tool_calling_agent(llm, tools, prompt=None)
    executor = AgentExecutor(agent=agent, tools=tools)
    for q in QUESTIONS:
        print(f"\nQ: {q}\n")
        print(executor.invoke({"input": q})["output"])


if __name__ == "__main__":
    try:
        import langchain  # noqa: F401

        run_with_langchain()
    except ImportError:
        run_without_langchain()
