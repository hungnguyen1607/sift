"""Sift: an open-source, self-hostable search API for AI agents.

The client is importable without any heavy dependencies:

    from sift import search, passages, extract, verify
"""

from sift.client import extract, passages, search, verify

__all__ = ["search", "passages", "extract", "verify"]
