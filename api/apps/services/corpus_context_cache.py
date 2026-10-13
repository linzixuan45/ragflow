"""Process-local cache for the Corpus FS dataset context.

Every Corpus FS read used to rebuild a dataset's context from scratch: all
``status=1`` document rows, the whole file subtree (one query per directory
level), and the ``file2document`` links. On a twelve-dataset search that is the
largest remaining term after the retrieval fix, and it is per-dataset data that
only changes when the dataset's ``content_revision`` moves.

The cached value deliberately contains **no permission-filtered data**. Files
are re-checked against the caller's tenant on every request, so a cached entry
created for one caller can never hand another caller a file it may not see; the
key includes the caller's tenant anyway, because the file tree is resolved
inside that tenant's folder hierarchy.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 32


def _config(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class DatasetContextCache:
    """TTL + LRU cache keyed by ``(tenant_id, dataset_id, content_revision)``."""

    def __init__(self, *, ttl_seconds: float | None = None, max_entries: int | None = None) -> None:
        self.ttl_seconds = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self.max_entries = int(DEFAULT_MAX_ENTRIES if max_entries is None else max_entries)
        self._entries: OrderedDict[tuple[str, str, int], tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: tuple[str, str, int]) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if now - stored_at > self.ttl_seconds:
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: tuple[str, str, int], value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def discard_dataset(self, dataset_id: str) -> None:
        """Drop every entry for one dataset, whatever the tenant or revision.

        Creating a document inserts a row the projection reads, but only Corpus
        FS mutations move ``content_revision`` — a document added through the
        plain upload API would otherwise stay invisible until the TTL expired.
        """
        with self._lock:
            stale = [key for key in self._entries if isinstance(key, tuple) and len(key) > 1 and key[1] == dataset_id]
            for key in stale:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "enabled": cache_enabled(),
            }


dataset_context_cache = DatasetContextCache(
    ttl_seconds=_config("CORPUS_CONTEXT_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS),
    max_entries=int(_config("CORPUS_CONTEXT_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)),
)

# Citation manifests for documents whose source has to be read to hash it: the
# whole object is fetched to compute its sha256, which cost 0.3~0.6s for the
# 130~320 MB Office files and made citations the largest stage of a 16~50
# dataset search. The value is `(manifest, blocks)` and is keyed by the
# document's stored content fingerprint, so it cannot outlive its content.
citation_manifest_cache = DatasetContextCache(
    ttl_seconds=_config("CORPUS_CITATION_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS),
    max_entries=int(_config("CORPUS_CITATION_CACHE_MAX_ENTRIES", 128)),
)

# Parsed PDF source projections, keyed by (dataset_id, object_name, revision).
# ``object_name`` embeds the document id and the projection revision, so the
# stored bytes can never change under a key. Reading one costs a storage GET,
# a gunzip and one JSON parse per line, and the largest projections run to
# thousands of blocks; a twelve-dataset search resolved thirteen of them per
# request, which is what made ``citation_context`` the largest stage of a
# request that cites a PDF.
source_projection_cache = DatasetContextCache(
    ttl_seconds=_config("CORPUS_PROJECTION_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS),
    max_entries=int(_config("CORPUS_PROJECTION_CACHE_MAX_ENTRIES", 32)),
)


def cache_enabled() -> bool:
    return os.environ.get("CORPUS_CONTEXT_CACHE", "1") != "0"
