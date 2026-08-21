import threading

import pytest

from api.apps.services.corpus_tool_service import (
    CORPUS_EVIDENCE_SCHEMA,
    CorpusSearchUnavailableError,
    CorpusToolService,
)


class _CitationService:
    def issue(self, tenant_id, dataset_id, document_id, target):
        return f"cit1.opaque-{target['page']}"


class _CorpusService:
    def __init__(self, dataset_id):
        self.dataset_id = dataset_id

    def search_context(self, path):
        assert path == "/"
        by_document = {
            f"{self.dataset_id}-doc-a": {"path": "/a.pdf"},
            f"{self.dataset_id}-doc-b": {"path": "/b.pdf"},
        }
        documents = {
            f"{self.dataset_id}-doc-a": {"id": f"{self.dataset_id}-doc-a"},
            f"{self.dataset_id}-doc-b": {"id": f"{self.dataset_id}-doc-b"},
        }
        return by_document, documents, 7

    @staticmethod
    def search_vector_weight(mode, configured_weight):
        return 1.0 if mode == "semantic" else configured_weight

    def citation_context(self, document):
        return {}, None

    def citation_target(self, entry, document, content_revision, *, chunk, context):
        return {
            "path": entry["path"],
            "content_revision": content_revision,
            "projection_revision": 1,
            "page": chunk.get("page_num_int", [None])[0],
            "block_ref": None,
            "range": None,
            "source_hash": None,
        }

    def search_images(self, document_id, chunk):
        return []


class _ThreadTrackingCorpusService(_CorpusService):
    def __init__(self, dataset_id):
        super().__init__(dataset_id)
        self.search_context_thread = None
        self.citation_context_thread = None

    def search_context(self, path):
        self.search_context_thread = threading.get_ident()
        return super().search_context(path)

    def citation_context(self, document):
        self.citation_context_thread = threading.get_ident()
        return super().citation_context(document)


class _FailingCitationCorpusService(_CorpusService):
    def citation_context(self, document):
        raise OSError(9, "Bad file descriptor")

@pytest.mark.asyncio
async def test_search_returns_ranked_path_only_evidence_across_scopes():
    async def searcher(dataset_id, tenant_id, request):
        assert tenant_id == "tenant-1"
        assert request["doc_ids"] == [f"{dataset_id}-doc-a", f"{dataset_id}-doc-b"]
        chunks = []
        if dataset_id == "dataset-1":
            chunks = [
                {"id": "chunk-a1", "doc_id": "dataset-1-doc-a", "content_with_weight": "alpha", "page_num_int": [1], "similarity": 0.95, "vector_similarity": 0.91, "term_similarity": 0.7},
                {"id": "chunk-a1-duplicate", "doc_id": "dataset-1-doc-a", "content_with_weight": " alpha ", "page_num_int": [1], "similarity": 0.94},
                {"id": "chunk-a2", "doc_id": "dataset-1-doc-a", "content_with_weight": "beta", "page_num_int": [2], "similarity": 0.93},
                {"id": "chunk-a3", "doc_id": "dataset-1-doc-a", "content_with_weight": "gamma", "page_num_int": [3], "similarity": 0.92},
                {"id": "chunk-a4", "doc_id": "dataset-1-doc-a", "content_with_weight": "delta", "page_num_int": [4], "similarity": 0.91},
            ]
        else:
            chunks = [
                {"id": "chunk-b1", "doc_id": "dataset-2-doc-b", "content_with_weight": "best", "page_num_int": [1], "similarity": 0.99},
            ]
        return True, {"chunks": chunks}

    service = CorpusToolService(
        "tenant-1",
        "agent-user",
        service_factory=lambda dataset_id: _CorpusService(dataset_id),
        searcher=searcher,
        citation_service=_CitationService(),
    )

    result = await service.search(
        scopes=[
            {"scope": "handbook", "dataset_id": "dataset-1", "path": "/"},
            {"scope": "policies", "dataset_id": "dataset-2", "path": "/"},
        ],
        query="platform",
        limit=5,
        per_document_limit=3,
    )

    assert result["schema"] == CORPUS_EVIDENCE_SCHEMA
    assert [(item["scope"], item["path"], item["snippet"]) for item in result["items"]] == [
        ("policies", "/b.pdf", "best"),
        ("handbook", "/a.pdf", "alpha"),
        ("handbook", "/a.pdf", "beta"),
        ("handbook", "/a.pdf", "gamma"),
    ]
    assert result["items"][1]["score"] == 0.95
    assert result["items"][1]["vector_similarity"] == 0.91
    assert result["search_meta"] == {
        "scope_count": 2,
        "raw_candidate_count": 6,
        "deduplicated_count": 5,
        "document_limited_count": 4,
        "returned_count": 4,
        "ranking_mode": "score_then_source_rank",
    }
    serialized = str(result)
    assert "chunk-a1" not in serialized
    assert "dataset-1-doc-a" not in serialized
    assert "dataset_id" not in serialized


@pytest.mark.asyncio
async def test_search_isolates_sync_corpus_io_from_the_event_loop_thread():
    event_loop_thread = threading.get_ident()
    corpus_service = _ThreadTrackingCorpusService("dataset-1")

    async def searcher(dataset_id, tenant_id, request):
        assert threading.get_ident() == event_loop_thread
        return True, {
            "chunks": [
                {
                    "id": "chunk-a1",
                    "doc_id": "dataset-1-doc-a",
                    "content_with_weight": "alpha",
                    "page_num_int": [1],
                    "similarity": 0.95,
                }
            ]
        }

    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda _dataset_id: corpus_service,
        searcher=searcher,
        citation_service=_CitationService(),
    )

    await service.search(
        scopes=[{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
        query="platform",
    )

    assert corpus_service.search_context_thread != event_loop_thread
    assert corpus_service.citation_context_thread != event_loop_thread


@pytest.mark.asyncio
async def test_search_maps_citation_io_failure_to_search_unavailable():
    async def searcher(dataset_id, tenant_id, request):
        return True, {
            "chunks": [
                {
                    "id": "chunk-a1",
                    "doc_id": "dataset-1-doc-a",
                    "content_with_weight": "alpha",
                    "page_num_int": [1],
                }
            ]
        }

    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda dataset_id: _FailingCitationCorpusService(dataset_id),
        searcher=searcher,
        citation_service=_CitationService(),
    )

    with pytest.raises(CorpusSearchUnavailableError) as failure:
        await service.search(
            scopes=[{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
            query="platform",
        )

    assert failure.value.code == "SEARCH_UNAVAILABLE"
    assert failure.value.status == 503
    assert isinstance(failure.value.__cause__, OSError)
