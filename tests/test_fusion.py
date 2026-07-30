import pytest

from rag_assistant.fusion import rrf


def test_rrf_basic_scores():
    # doc "b" is #2 dense and #3 lexical → 1/62 + 1/63
    fused = dict(rrf([["a", "b", "c"], ["c", "x", "b"]]))
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 63)


def test_rrf_rewards_presence_in_both_lists():
    # "both" is mid-ranked in both lists; "single" tops one list only.
    fused = rrf([["single", "both", "z1"], ["z2", "both", "z3"]])
    order = [doc for doc, _ in fused]
    assert order.index("both") < order.index("single")


def test_rrf_missing_from_one_list_contributes_nothing():
    fused = dict(rrf([["a"], []]))
    assert fused["a"] == pytest.approx(1 / 61)


def test_rrf_deterministic_tiebreak():
    a = rrf([["x"], ["y"]])
    b = rrf([["y"], ["x"]])
    assert a == b  # equal scores → stable id ordering


def test_rrf_rejects_bad_k():
    with pytest.raises(ValueError):
        rrf([["a"]], k=0)
