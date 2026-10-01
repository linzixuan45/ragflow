import threading

import pytest

from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService, NotFoundError
from api.apps.services.corpus_tool_service import (
    CORPUS_EVIDENCE_SCHEMA,
    CorpusSearchUnavailableError,
    CorpusToolService,
)


class _CitationService:
    def issue(self, tenant_id, dataset_id, document_id, target):
        return f"cit1.opaque-{target['page']}"


class _CorpusService:
    chunk_page = staticmethod(CorpusFsService.chunk_page)

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


class _Recall:
    """Fake fused recall: returns the merged candidate pool for every scope."""

    def __init__(self, chunks_by_scope, *, on_call=None):
        self._chunks_by_scope = chunks_by_scope
        self._on_call = on_call
        self.calls: list[dict] = []

    async def recall(self, scopes, question, **kwargs):
        self.calls.append({"scopes": scopes, "question": question, **kwargs})
        if self._on_call is not None:
            self._on_call(scopes, question, kwargs)
        chunks = []
        for scope in scopes:
            for chunk in self._chunks_by_scope(scope["scope"], kwargs):
                chunks.append({**chunk, "_scope": scope["scope"]})
        return chunks


@pytest.mark.asyncio
async def test_search_returns_ranked_path_only_evidence_across_scopes():
    def chunks_for(scope, _kwargs):
        if scope == "handbook":
            return [
                {"chunk_id": "chunk-a1", "doc_id": "dataset-1-doc-a", "content_with_weight": "alpha", "page_num_int": [1], "similarity": 0.95, "vector_similarity": 0.91, "term_similarity": 0.7},
                {"chunk_id": "chunk-a1-duplicate", "doc_id": "dataset-1-doc-a", "content_with_weight": " alpha ", "page_num_int": [1], "similarity": 0.94},
                {"chunk_id": "chunk-a2", "doc_id": "dataset-1-doc-a", "content_with_weight": "beta", "page_num_int": [2], "similarity": 0.93},
                {"chunk_id": "chunk-a3", "doc_id": "dataset-1-doc-a", "content_with_weight": "gamma", "page_num_int": [3], "similarity": 0.92},
                {"chunk_id": "chunk-a4", "doc_id": "dataset-1-doc-a", "content_with_weight": "delta", "page_num_int": [4], "similarity": 0.91},
            ]
        return [
            {"chunk_id": "chunk-b1", "doc_id": "dataset-2-doc-b", "content_with_weight": "best", "page_num_int": [1], "similarity": 0.99},
        ]

    recall = _Recall(chunks_for)
    service = CorpusToolService(
        "tenant-1",
        "agent-user",
        service_factory=lambda dataset_id: _CorpusService(dataset_id),
        recall=recall,
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
    assert result["search_meta"]["scope_count"] == 2
    assert result["search_meta"]["raw_candidate_count"] == 6
    assert result["search_meta"]["deduplicated_count"] == 5
    assert result["search_meta"]["document_limited_count"] == 4
    assert result["search_meta"]["returned_count"] == 4
    assert result["search_meta"]["ranking_mode"] == "score_then_source_rank"
    assert result["search_meta"]["search_params"]["use_kg"] is False
    serialized = str(result)
    assert "chunk-a1" not in serialized
    assert "dataset-1-doc-a" not in serialized
    assert "dataset_id" not in serialized


@pytest.mark.asyncio
async def test_search_isolates_sync_corpus_io_from_the_event_loop_thread():
    event_loop_thread = threading.get_ident()
    corpus_service = _ThreadTrackingCorpusService("dataset-1")

    def assert_on_loop_thread(_scopes, _question, _kwargs):
        assert threading.get_ident() == event_loop_thread

    recall = _Recall(
        lambda _scope, _kwargs: [{"chunk_id": "chunk-a1", "doc_id": "dataset-1-doc-a", "content_with_weight": "alpha", "page_num_int": [1], "similarity": 0.95}],
        on_call=assert_on_loop_thread,
    )
    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda _dataset_id: corpus_service,
        recall=recall,
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
    recall = _Recall(lambda _scope, _kwargs: [{"chunk_id": "chunk-a1", "doc_id": "dataset-1-doc-a", "content_with_weight": "alpha", "page_num_int": [1]}])
    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda dataset_id: _FailingCitationCorpusService(dataset_id),
        recall=recall,
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


@pytest.mark.asyncio
async def test_search_defaults_use_kg_off_and_maps_graph_chunks_to_paths():
    captured = {}

    def chunks_for(_scope, kwargs):
        captured["use_kg"] = kwargs.get("use_kg")
        chunks = [
            {
                "chunk_id": "chunk-a1",
                "doc_id": "dataset-1-doc-a",
                "content_with_weight": "alpha",
                "page_num_int": [1],
                "similarity": 0.80,
            }
        ]
        if kwargs.get("use_kg"):
            chunks.insert(
                0,
                {
                    "chunk_id": "kg-1",
                    "doc_id": "",
                    "content_with_weight": "entity-relation summary",
                    "docnm_kwd": "Related content in Knowledge Graph",
                    "similarity": 1.0,
                },
            )
        return chunks

    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda dataset_id: _CorpusService(dataset_id),
        recall=_Recall(chunks_for),
        citation_service=_CitationService(),
    )

    default_result = await service.search(
        scopes=[{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
        query="platform",
    )
    assert captured["use_kg"] is False
    assert [item["snippet"] for item in default_result["items"]] == ["alpha"]

    kg_result = await service.search(
        scopes=[{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
        query="platform",
        use_kg=True,
    )
    assert captured["use_kg"] is True
    assert [(item["path"], item["snippet"]) for item in kg_result["items"]] == [
        ("/a.pdf", "entity-relation summary"),
        ("/a.pdf", "alpha"),
    ]
    serialized = str(kg_result)
    assert "dataset-1-doc-a" not in serialized
    assert "kg-1" not in serialized
    assert kg_result["search_meta"]["search_params"]["use_kg"] is True


class _ScopedCorpusService:
    """Corpus FS double whose visible files depend on the requested path."""

    chunk_page = staticmethod(CorpusFsService.chunk_page)

    def __init__(self, dataset_id, entries_by_path):
        self.dataset_id = dataset_id
        self.entries_by_path = entries_by_path
        self.requested_paths = []

    def search_context(self, path):
        self.requested_paths.append(path)
        entries = self.entries_by_path[path]
        by_document = {entry["_document_id"]: entry for entry in entries}
        documents = {entry["_document_id"]: {"id": entry["_document_id"]} for entry in entries}
        return by_document, documents, 11

    @staticmethod
    def search_vector_weight(mode, configured_weight):
        return 1.0 if mode == "semantic" else configured_weight

    def citation_context(self, document):
        return {}, None

    def citation_target(self, entry, document, content_revision, *, chunk, context):
        return {"path": entry["path"], "page": (chunk.get("page_num_int") or [None])[0], "source_hash": None}

    def search_images(self, document_id, chunk):
        return []


class _DisabledCorpusService(_ScopedCorpusService):
    def search_context(self, path):
        raise NotFoundError("dataset not found")


def _scoped_service(services):
    return CorpusToolService(
        "tenant-1",
        service_factory=lambda dataset_id: services[dataset_id],
        recall=_Recall(lambda scope, _kwargs: _SCOPED_CHUNKS[scope]),
        citation_service=_CitationService(),
    )


def _entry(document_id, path):
    return {"_document_id": document_id, "path": path, "kind": "file", "name": path.rsplit("/", 1)[-1]}


_SCOPED_CHUNKS = {
    "root": [{"chunk_id": "c-root", "doc_id": "doc-root", "content_with_weight": "root", "page_num_int": [1], "similarity": 0.9}],
    "sub": [{"chunk_id": "c-sub", "doc_id": "doc-sub", "content_with_weight": "sub", "page_num_int": [1], "similarity": 0.8}],
    "left": [{"chunk_id": "c-left", "doc_id": "doc-left", "content_with_weight": "left", "page_num_int": [1], "similarity": 0.7}],
    "right": [{"chunk_id": "c-right", "doc_id": "doc-right", "content_with_weight": "right", "page_num_int": [1], "similarity": 0.6}],
    "empty": [],
    "outside": [{"chunk_id": "c-outside", "doc_id": "doc-elsewhere", "content_with_weight": "outside", "page_num_int": [1], "similarity": 0.9}],
}


@pytest.mark.asyncio
async def test_same_dataset_under_two_paths_keeps_both_scopes_distinct():
    """Contract: one dataset may appear twice with different paths."""

    service = _scoped_service(
        {
            "dataset-1": _ScopedCorpusService(
                "dataset-1",
                {"/": [_entry("doc-root", "/root.md")], "/sub": [_entry("doc-sub", "/sub/sub.md")]},
            )
        }
    )

    result = await service.search(
        scopes=[
            {"scope": "root", "dataset_id": "dataset-1", "path": "/"},
            {"scope": "sub", "dataset_id": "dataset-1", "path": "/sub"},
        ],
        query="platform",
    )

    assert [(item["scope"], item["path"]) for item in result["items"]] == [("root", "/root.md"), ("sub", "/sub/sub.md")]
    assert result["search_meta"]["scope_count"] == 2
    assert set(result["search_meta"]["content_revisions"]) == {"root", "sub"}


@pytest.mark.asyncio
async def test_same_file_name_in_two_datasets_is_not_cross_deduplicated():
    """Contract: dedup is scoped; equal paths from different libraries both survive."""

    service = _scoped_service(
        {
            "dataset-1": _ScopedCorpusService("dataset-1", {"/": [_entry("doc-left", "/同名.md")]}),
            "dataset-2": _ScopedCorpusService("dataset-2", {"/": [_entry("doc-right", "/同名.md")]}),
        }
    )

    result = await service.search(
        scopes=[
            {"scope": "left", "dataset_id": "dataset-1", "path": "/"},
            {"scope": "right", "dataset_id": "dataset-2", "path": "/"},
        ],
        query="platform",
    )

    assert [(item["scope"], item["path"]) for item in result["items"]] == [("left", "/同名.md"), ("right", "/同名.md")]
    assert result["search_meta"]["deduplicated_count"] == 2


@pytest.mark.asyncio
async def test_empty_library_scope_contributes_nothing_without_failing_the_request():
    service = _scoped_service(
        {
            "dataset-1": _ScopedCorpusService("dataset-1", {"/": [_entry("doc-root", "/root.md")]}),
            "dataset-2": _ScopedCorpusService("dataset-2", {"/": []}),
        }
    )

    result = await service.search(
        scopes=[
            {"scope": "root", "dataset_id": "dataset-1", "path": "/"},
            {"scope": "empty", "dataset_id": "dataset-2", "path": "/"},
        ],
        query="platform",
    )

    assert [(item["scope"], item["path"]) for item in result["items"]] == [("root", "/root.md")]
    assert result["search_meta"]["scope_count"] == 2
    assert result["search_meta"]["content_revisions"] == {"root": 11, "empty": 11}


@pytest.mark.asyncio
async def test_hits_outside_the_requested_path_are_dropped():
    """Contract: a sub-tree scope must not surface chunks from outside it."""

    service = _scoped_service({"dataset-1": _ScopedCorpusService("dataset-1", {"/sub": [_entry("doc-sub", "/sub/sub.md")]})})

    result = await service.search(
        scopes=[{"scope": "outside", "dataset_id": "dataset-1", "path": "/sub"}],
        query="platform",
    )

    assert result["items"] == []
    assert result["search_meta"]["raw_candidate_count"] == 0
    assert result["total"] == 0
    assert result["truncated"] is False
    assert result["continuation"] == {"mode": "terminal"}


@pytest.mark.asyncio
async def test_limit_truncates_but_reports_the_full_document_limited_total():
    chunks = [{"chunk_id": f"c{index}", "doc_id": "doc-root", "content_with_weight": f"text-{index}", "page_num_int": [index], "similarity": 0.9 - index / 100} for index in range(3)]
    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda _dataset_id: _ScopedCorpusService("dataset-1", {"/": [_entry("doc-root", "/root.md")]}),
        recall=_Recall(lambda _scope, _kwargs: chunks),
        citation_service=_CitationService(),
    )

    result = await service.search(
        scopes=[{"scope": "root", "dataset_id": "dataset-1", "path": "/"}],
        query="platform",
        limit=2,
    )

    assert len(result["items"]) == 2
    assert result["total"] == 3
    assert result["truncated"] is True
    assert result["continuation"] == {"mode": "refine"}
    assert [item["rank"] for item in result["items"]] == [1, 2]
    assert result["search_meta"]["document_limited_count"] == 3
    assert result["search_meta"]["returned_count"] == 2


@pytest.mark.asyncio
async def test_disabled_library_fails_the_request_instead_of_silently_dropping_the_scope():
    """Contract: an inaccessible dataset is an error, not an empty result."""

    service = _scoped_service(
        {
            "dataset-1": _DisabledCorpusService("dataset-1", {}),
            "dataset-2": _ScopedCorpusService("dataset-2", {"/": [_entry("doc-right", "/right.md")]}),
        }
    )

    with pytest.raises(CorpusFsError, match="dataset not found"):
        await service.search(
            scopes=[
                {"scope": "disabled", "dataset_id": "dataset-1", "path": "/"},
                {"scope": "right", "dataset_id": "dataset-2", "path": "/"},
            ],
            query="platform",
        )


@pytest.mark.asyncio
async def test_citation_sources_are_only_read_for_returned_items():
    """Reading every candidate's source object dominated a 16-dataset search."""

    documents = 20
    entries = [_entry(f"doc-{index}", f"/file-{index}.md") for index in range(documents)]
    reads: list[str] = []

    class _CountingService(_ScopedCorpusService):
        def citation_context(self, document):
            reads.append(document["id"])
            return {}, None

    chunks = [
        {"chunk_id": f"c{index}", "doc_id": f"doc-{index}", "content_with_weight": f"text-{index}", "page_num_int": [1], "similarity": 0.9 - index / 100}
        for index in range(documents)
    ]
    service = CorpusToolService(
        "tenant-1",
        service_factory=lambda _dataset_id: _CountingService("dataset-1", {"/": entries}),
        recall=_Recall(lambda _scope, _kwargs: chunks),
        citation_service=_CitationService(),
    )

    result = await service.search(
        scopes=[{"scope": "root", "dataset_id": "dataset-1", "path": "/"}],
        query="platform",
        limit=2,
    )

    assert result["search_meta"]["raw_candidate_count"] == documents
    assert len(result["items"]) == 2
    assert len(reads) == 2
    assert all(item["citation_ref"] for item in result["items"])
