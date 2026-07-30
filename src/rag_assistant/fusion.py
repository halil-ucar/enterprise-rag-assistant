"""Reciprocal Rank Fusion — pure function, unit-tested.

Why rank-based: dense similarity and ts_rank live on incompatible scales;
normalizing scores across them is fragile. RRF only uses positions.
k=60 is the industry default (Elasticsearch, Azure AI Search, OpenSearch) —
a default, not a proven optimum; treat changes as an eval decision.
"""

from __future__ import annotations

from collections.abc import Sequence

RRF_K = 60


def rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fuse ranked id lists. Returns (id, score) sorted by score desc, then id (stable ties).

    Each list contributes 1/(k + position) per id, positions are 1-based.
    Ids missing from a list simply contribute nothing for that list.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for pos, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + pos)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
