"""Reciprocal Rank Fusion (RRF) helper -- no external dependencies."""


def rrf_fuse(keyword_ids: list, semantic_ids: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion: merge two ranked ID lists into one.

    Each list contributes 1/(k + rank) to an ID's score (rank is 0-based).
    IDs appearing in both lists accumulate score from both, so they rise
    above IDs found by only one search. Ties broken by first appearance.
    """
    scores: dict = {}
    order: dict = {}
    for ranked in (keyword_ids, semantic_ids):
        for rank, vid in enumerate(ranked):
            if vid not in scores:
                scores[vid] = 0.0
                order[vid] = len(order)
            scores[vid] += 1.0 / (k + rank)
    return sorted(scores, key=lambda v: (-scores[v], order[v]))
