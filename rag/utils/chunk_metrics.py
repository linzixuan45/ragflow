import math

from common.token_utils import num_tokens_from_string


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * percentile) - 1))
    return values[index]


def summarize_chunk_tokens(
    chunks: list[dict],
    *,
    short_threshold: int = 16,
    title_count: int = 0,
) -> dict:
    token_counts = [num_tokens_from_string(str(chunk.get("content_with_weight") or "")) for chunk in chunks]
    ordered = sorted(token_counts)
    short_count = sum(count <= short_threshold for count in token_counts)
    total = len(token_counts)
    return {
        "count": total,
        "empty_count": sum(count == 0 for count in token_counts),
        "min": ordered[0] if ordered else 0,
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "max": ordered[-1] if ordered else 0,
        "short_threshold": short_threshold,
        "short_count": short_count,
        "short_ratio": round(short_count / total, 4) if total else 0.0,
        "title_count": title_count,
    }
