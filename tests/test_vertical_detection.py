"""Vertical detection over ~200 phrasings. Add a case here whenever a real
query gets routed to the wrong kind of search."""

import pytest

from sift.pipeline.vertical_examples import EXAMPLES as CASES
from sift.pipeline.verticals import detect_vertical


@pytest.mark.parametrize("query,vertical", [(q, v) for v, qs in CASES.items() for q in qs])
def test_detect_vertical_battery(query, vertical):
    assert detect_vertical(query) == vertical
