"""Coverage accounting for the fused multi-dataset recall path."""

from __future__ import annotations

import asyncio

import pytest

from api.apps.services import corpus_recall_service
from api.apps.services.corpus_recall_service import CorpusRecallService


class _Result:
    def __init__(self, rows: list[dict]):
        self.ids = [row["id"] for row in rows]
        self.field = {row["id"]: row for row in rows}
        self.query_vector = [0.0]
        self.keywords = []
        self.highlight = {}
        self.aggregation = []
        self.total = len(rows)


class _SearchResult:
    def __init__(self, **kwargs):
        self.ids = kwargs["ids"]
        self.field = kwargs["field"]
        self.query_vector = kwargs.get("query_vector")
        self.keywords = kwargs.get("keywords", [])
        self.highlight = kwargs.get("highlight", {})
        self.aggregation = kwargs.get("aggregation", [])
        self.total = kwargs.get("total", len(self.ids))


class _Dealer:
    """Records every window request and answers from a canned script."""

    SearchResult = _SearchResult

    def __init__(self, script):
        self.script = script
        self.requests: list[dict] = []

    async def search(self, req, idx_names, kb_ids, embd_mdl, highlight=False, rank_feature=None):
        self.requests.append(dict(req))
        return _Result(self.script(req))

    def rerank_by_model(self, rerank_mdl, sres, query, tkweight=0.3, vtweight=0.7, rank_feature=None):
        size = len(sres.ids)
        return [1.0] * size, [0.5] * size, [0.5] * size

    def retrieval_by_children(self, chunks, tenant_ids):
        return chunks


def scope(label: str, dataset_id: str, visible: list[str]) -> dict:
    return {
        "scope": label,
        "dataset_id": dataset_id,
        "path": "/",
        "visible": {document_id: {"path": f"/{document_id}"} for document_id in visible},
        "kb": object(),
    }


def row(chunk_id: str, dataset_id: str, document_id: str) -> dict:
    return {
        "id": chunk_id,
        "chunk_id": chunk_id,
        "kb_id": dataset_id,
        "doc_id": document_id,
        "content_with_weight": "text",
        "content_ltks": "text",
        "position_int": [],
    }


@pytest.fixture(autouse=True)
def _no_models(monkeypatch):
    monkeypatch.setattr(CorpusRecallService, "_embedding_model", staticmethod(lambda *a, **k: object()))
    monkeypatch.setattr(CorpusRecallService, "_rerank_model", staticmethod(lambda *a, **k: object()))
    monkeypatch.setattr(corpus_recall_service, "label_question", lambda *a, **k: {})


def build(script):
    dealer = _Dealer(script)
    recall = CorpusRecallService("tenant-1")
    recall._dealer = dealer
    return recall, dealer


async def call(recall, scopes):
    return await recall._recall_group(
        scopes,
        "tenant-1",
        "embd-1",
        "what is ai",
        similarity_threshold=0.0,
        vector_similarity_weight=0.3,
        use_kg=False,
    )


@pytest.mark.asyncio
async def test_scope_whose_strict_hits_are_invisible_gets_its_own_window():
    """A scope must be recovered when the shared window only reaches it outside its path."""

    def script(req):
        if req["doc_ids"] is None:
            # Shared window: the only hit belongs to a document the scope cannot see.
            return [row("c1", "dataset-1", "hidden-document")]
        if req["question"]:
            return [row("c2", "dataset-1", "visible-document")]
        return []

    recall, dealer = build(script)
    chunks = await call(recall, [scope("s0", "dataset-1", ["visible-document"])])

    recovery = [req for req in dealer.requests if req["doc_ids"] and req["question"]]
    assert len(recovery) == 1, dealer.requests
    assert recovery[0]["doc_ids"] == ["visible-document"]
    assert [chunk["chunk_id"] for chunk in chunks] == ["c2"]
    assert chunks[0]["_scope"] == "s0"


@pytest.mark.asyncio
async def test_visible_strict_hit_does_not_trigger_a_recovery_window():
    def script(req):
        if req["doc_ids"] is None:
            return [row("c1", "dataset-1", "visible-document")]
        return [row("c9", "dataset-1", "visible-document")]

    recall, dealer = build(script)
    chunks = await call(recall, [scope("s0", "dataset-1", ["visible-document"])])

    assert [req for req in dealer.requests if req["doc_ids"]] == []
    assert [chunk["chunk_id"] for chunk in chunks] == ["c1"]


@pytest.mark.asyncio
async def test_scope_still_empty_after_recovery_gets_the_filter_only_fallback():
    def script(req):
        if req["doc_ids"] is None:
            return []
        if req["question"]:
            return []
        return [row("c3", "dataset-1", "visible-document")]

    recall, dealer = build(script)
    chunks = await call(recall, [scope("s0", "dataset-1", ["visible-document"])])

    fallback = [req for req in dealer.requests if req["question"] == ""]
    assert len(fallback) == 1
    assert fallback[0]["doc_ids"] == ["visible-document"]
    assert [chunk["chunk_id"] for chunk in chunks] == ["c3"]


@pytest.mark.asyncio
async def test_two_scopes_of_one_dataset_are_counted_independently():
    """The same dataset under two paths: one covered, one not."""

    def script(req):
        if req["doc_ids"] is None:
            return [row("c1", "dataset-1", "document-a")]
        return [row("c2", "dataset-1", "document-b")]

    recall, dealer = build(script)
    chunks = await call(
        recall,
        [scope("a", "dataset-1", ["document-a"]), scope("b", "dataset-1", ["document-b"])],
    )

    recovery = [req for req in dealer.requests if req["doc_ids"]]
    assert len(recovery) == 1
    assert recovery[0]["doc_ids"] == ["document-b"]
    assert {chunk["_scope"] for chunk in chunks} == {"a", "b"}


@pytest.mark.asyncio
async def test_nested_scopes_bind_a_chunk_to_the_first_visible_scope_once():
    """Overlapping paths: the chunk is reported once, under the first scope."""

    def script(req):
        if req["doc_ids"] is None:
            return [row("c1", "dataset-1", "document-a")]
        return []

    recall, dealer = build(script)
    chunks = await call(
        recall,
        [scope("root", "dataset-1", ["document-a"]), scope("sub", "dataset-1", ["document-a"])],
    )

    assert [req for req in dealer.requests if req["doc_ids"]] == []
    assert [chunk["chunk_id"] for chunk in chunks] == ["c1"]
    assert chunks[0]["_scope"] == "root"


@pytest.mark.asyncio
async def test_chunks_of_unknown_documents_never_reach_the_pool():
    """A stale index row (deleted document) is dropped the way prune would."""

    def script(req):
        if req["doc_ids"] is None:
            return [row("stale", "dataset-1", "deleted-document")]
        if req["question"]:
            return [row("c2", "dataset-1", "visible-document")]
        return []

    recall, _dealer = build(script)
    chunks = await call(recall, [scope("s0", "dataset-1", ["visible-document"])])

    assert [chunk["chunk_id"] for chunk in chunks] == ["c2"]
    assert all(chunk["doc_id"] in {"visible-document"} for chunk in chunks)


def test_recovery_quota_spends_the_pool_budget_per_scope():
    quota = CorpusRecallService.recovery_quota

    # Small scope counts keep the historical window: the budget only starts to
    # bind once the shared strict window can no longer cover every dataset.
    assert quota(1) == 10
    assert quota(6) == 10
    assert quota(12) == 5
    assert quota(16) == 4
    # Floor: a dataset outside the shared window always gets a few candidates,
    # so the pool stays bounded instead of growing with the scope count.
    assert quota(32) == 2
    assert quota(50) == 2


def test_pool_stays_bounded_at_the_maximum_scope_count():
    assert CorpusRecallService.STRICT_POOL + 50 * CorpusRecallService.recovery_quota(50) == 140


@pytest.mark.asyncio
async def test_recovered_windows_use_the_per_scope_quota():
    captured: list[dict] = []

    def script(req):
        captured.append(dict(req))
        if req["doc_ids"] is None:
            return []
        if req["question"]:
            dataset_id = req["kb_ids"][0]
            return [row(f"c-{dataset_id}", dataset_id, req["doc_ids"][0])]
        return []

    recall, _dealer = build(script)
    scopes = [scope(f"s{index}", f"dataset-{index}", [f"doc-{index}"]) for index in range(16)]

    await call(recall, scopes)

    recovery = [req for req in captured if req["doc_ids"]]
    assert len(recovery) == 16
    assert {req["size"] for req in recovery} == {CorpusRecallService.recovery_quota(16)}


@pytest.mark.asyncio
async def test_recovery_searches_are_issued_with_bounded_concurrency():
    """50 concurrent windows stalled the event loop; the fan-out is capped."""

    active = 0
    peak = 0

    class _SlowDealer(_Dealer):
        async def search(self, req, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await super().search(req, *args, **kwargs)
            finally:
                active -= 1

    dealer = _SlowDealer(lambda req: [])
    recall = CorpusRecallService("tenant-1")
    recall._dealer = dealer
    scopes = [scope(f"s{index}", f"dataset-{index}", [f"doc-{index}"]) for index in range(50)]

    await call(recall, scopes)

    assert len(dealer.requests) >= 50
    assert peak <= CorpusRecallService.RECOVERY_CONCURRENCY
