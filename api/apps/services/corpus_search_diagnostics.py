"""Opt-in stage timing for a fanned-out Corpus FS search.

The M11 tail-latency investigation could attribute the *in-process* tail to
``citation`` (then ``rerank``), but not the tail that only appears behind the
HTTP endpoint. The two disagree because they do not share a cache: the in-process
probe starts from an empty ``citation_manifest_cache`` on every process, while an
HTTP request is served by a long-lived API process whose cache accumulates and
evicts across requests. This module closes that gap by timing the same stages on
the request that actually serves HTTP and by reporting the cache
hit/miss/eviction deltas that request produced.

Collection is opt-in via ``CORPUS_SEARCH_DIAGNOSTICS=1``. With it off, ``stage``
costs one ``ContextVar`` read and ``search_meta`` is unchanged, so ordinary
callers keep the same response payload and the same latency.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from api.apps.services.corpus_context_cache import citation_manifest_cache, dataset_context_cache

_ENV = "CORPUS_SEARCH_DIAGNOSTICS"

_collector: ContextVar[SearchDiagnostics | None] = ContextVar("corpus_search_diagnostics", default=None)


def enabled() -> bool:
    return os.environ.get(_ENV, "0") != "0"


class SearchDiagnostics:
    """Wall seconds per stage and cache deltas for one search request."""

    def __init__(self) -> None:
        self._seconds: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._before = {
            "citation_manifest": citation_manifest_cache.stats(),
            "dataset_context": dataset_context_cache.stats(),
        }

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - start)

    def record(self, name: str, seconds: float) -> None:
        """Add a stage timed by the caller.

        Needed for a region that has to be closed *before* the response snapshot
        is taken -- the handler that writes the snapshot cannot time itself with
        a context manager and still appear in its own output.
        """
        self._seconds[name] = self._seconds.get(name, 0.0) + seconds
        self._calls[name] = self._calls.get(name, 0) + 1

    @staticmethod
    def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        return {
            "hits": after["hits"] - before["hits"],
            "misses": after["misses"] - before["misses"],
            "evictions": after["evictions"] - before["evictions"],
            "entries": after["entries"],
            "enabled": after["enabled"],
            "ttl_seconds": after["ttl_seconds"],
            "max_entries": after["max_entries"],
        }

    def snapshot(self) -> dict[str, Any]:
        after = {
            "citation_manifest": citation_manifest_cache.stats(),
            "dataset_context": dataset_context_cache.stats(),
        }
        return {
            "seconds": {name: round(value, 3) for name, value in sorted(self._seconds.items())},
            "calls": dict(sorted(self._calls.items())),
            "cache": {name: self._delta(self._before[name], after[name]) for name in sorted(after)},
        }


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Time ``name`` for the request being served, if a collector is installed."""
    diagnostics = _collector.get()
    if diagnostics is None:
        yield
        return
    with diagnostics.stage(name):
        yield


@contextlib.contextmanager
def collect() -> Iterator[SearchDiagnostics | None]:
    """Install a collector for the current request; a no-op unless switched on."""
    if not enabled():
        yield None
        return
    diagnostics = SearchDiagnostics()
    token = _collector.set(diagnostics)
    try:
        yield diagnostics
    finally:
        _collector.reset(token)
