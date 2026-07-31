from rag.utils.chunk_metrics import summarize_chunk_tokens


def test_summarize_chunk_tokens_reports_distribution_and_short_ratio(monkeypatch):
    monkeypatch.setattr("rag.utils.chunk_metrics.num_tokens_from_string", len)

    summary = summarize_chunk_tokens(
        [
            {"content_with_weight": ""},
            {"content_with_weight": "a"},
            {"content_with_weight": "abcd"},
            {"content_with_weight": "abcdefghij"},
        ],
        short_threshold=4,
        title_count=2,
    )

    assert summary == {
        "count": 4,
        "empty_count": 1,
        "min": 0,
        "p50": 1,
        "p90": 10,
        "max": 10,
        "short_threshold": 4,
        "short_count": 3,
        "short_ratio": 0.75,
        "title_count": 2,
    }
