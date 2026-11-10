from eval.metrics import (
    dcg,
    group_metrics,
    ndcg_at_k,
    percentile,
    relevance_at_k,
    token_efficiency,
)


def test_relevance_at_k():
    assert relevance_at_k([2, 1, 0, 0, 0]) == 0.4
    assert relevance_at_k([2, 1, 0], k=3) == 2 / 3
    assert relevance_at_k([]) == 0.0


def test_ndcg_perfect_ranking_is_one():
    assert ndcg_at_k([2, 2, 2, 2, 2]) == 1.0
    assert ndcg_at_k([0, 0, 0, 0, 0]) == 0.0


def test_ndcg_rewards_rank_position():
    assert ndcg_at_k([2, 0, 0, 0, 0]) > ndcg_at_k([0, 0, 0, 0, 2])


def test_dcg():
    assert dcg([2], 5) == 3.0  # (2^2 - 1) / log2(2) = 3


def test_percentile():
    assert percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert percentile([1, 2, 3, 4, 5], 100) == 5.0
    assert percentile([], 50) is None


def test_token_efficiency():
    pairs = [(1500, True), (2000, False), (1000, True)]
    assert token_efficiency(pairs) == 1250.0
    assert token_efficiency([(1500, False)]) is None


def test_group_metrics_per_system():
    import pytest

    judged = [
        {"system": "sift", "labels": [2, 1, 0, 0, 0]},
        {"system": "sift", "labels": [0, 0, 0, 0, 0]},
        {"system": "tavily", "labels": [2, 2, 2, 2, 2]},
    ]
    m = group_metrics(judged)
    assert m["sift"]["relevance@5"] == pytest.approx(0.2)
    assert m["tavily"]["relevance@5"] == 1.0
