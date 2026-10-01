"""``token_similarity`` must stay score-identical while skipping document weights.

``similarity`` never reads a document's term weights, so the document side only
needs its key set (single terms plus adjacent bigrams). The reference below is
the previous implementation, kept as the oracle: any divergence in keys shows up
as a different score.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

# Import ``common.settings`` first: ``rag.nlp.query`` pulls ``rag.utils.redis_conn``,
# which imports ``common.settings`` back and fails on a partially initialised
# module if the query package happens to be imported first.
import common.settings  # noqa: F401  # isort: skip

from rag.nlp.query import FulltextQueryer


def reference_token_similarity(qryr: FulltextQueryer, atks, btkss) -> list[float]:
    """Pre-change implementation (weights for query *and* documents)."""

    def to_dict(tks):
        if isinstance(tks, str):
            tks = tks.split()
        d = defaultdict(int)
        wts = qryr.tw.weights(tks, preprocess=False)
        for i, (t, c) in enumerate(wts):
            d[t] += c * 0.4
            if i + 1 < len(wts):
                _t, _c = wts[i + 1]
                d[t + _t] += max(c, _c) * 0.6
        return d

    atks = to_dict(atks)
    btkss = [to_dict(tks) for tks in btkss]
    return [qryr.similarity(atks, btks) for btks in btkss]


@pytest.fixture(scope="module")
def qryr() -> FulltextQueryer:
    return FulltextQueryer()


CASES = [
    (["人工智能", "模型"], [["人工智能", "模型", "训练"]]),
    (["人工智能"], [["人工智能", "人工智能", "人工智能"]]),
    (["深度学习"], [["深度", "学习", "深度学习"]]),
    (
        ["产品", "路线图"],
        [
            ["路线图", "产品", "规划"],
            ["产品", "产品", "路线图"],
            ["无关", "文本"],
            [],
        ],
    ),
    (["ai", "模型"], [["ai", "ai", "模型", "模型", "训练", "训练"]]),
    (["单"], [["单", "词", "文", "本", "单", "词"]]),
]


@pytest.mark.parametrize(("atks", "btkss"), CASES)
def test_token_similarity_is_score_identical_to_the_weights_based_implementation(qryr, atks, btkss):
    assert qryr.token_similarity(atks, btkss) == reference_token_similarity(qryr, atks, btkss)


def test_token_similarity_matches_duplicate_free_documents(qryr):
    """Duplicated document tokens cannot change a presence-only score."""

    documents = [
        ["人工智能", "模型", "训练", "人工智能", "模型"],
        ["人工智能", "模型", "训练"],
        ["模型", "人工智能", "深度学习", "模型"],
    ]
    deduplicated = [list(dict.fromkeys(document)) for document in documents]
    assert qryr.token_similarity(["人工智能", "模型"], documents) == qryr.token_similarity(["人工智能", "模型"], deduplicated)


def test_token_similarity_scores_the_query_term_fraction(qryr):
    scores = qryr.token_similarity(["人工智能", "模型"], [["人工智能"], ["完全", "无关"]])
    assert scores[0] > scores[1]
    assert 0.0 <= scores[1] < scores[0] <= 1.0
