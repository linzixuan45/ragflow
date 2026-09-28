import asyncio
from types import SimpleNamespace

from api.apps.services.keyword_search_service import KeywordSearchService


class _AwaitableDict(dict):
    def __await__(self):
        async def value():
            return self

        return value().__await__()


class _GrepService:
    tenant_id = "tenant-1"
    dataset_id = "dataset-1"
    actor_digest = "digest"

    def __init__(self, chunk):
        self.chunk = chunk
        self.image_calls = []

    def _dataset(self):
        return SimpleNamespace(id=self.dataset_id)

    def revision(self, _dataset):
        return {"content_revision": 4}

    def search_scope(self, _path):
        return {self.chunk["doc_id"]: {"path": "/通用素材/营业执照.docx"}}

    def _documents(self):
        return [{"id": self.chunk["doc_id"]}]

    def citation_context(self, _document):
        return {}

    def citation_target(self, _entry, _document, _revision, *, chunk, context):
        return {"page": (chunk.get("page_num_int") or [None])[0]}

    def search_images(self, document_id, chunk):
        self.image_calls.append((document_id, chunk.get("img_id")))
        if not chunk.get("img_id"):
            return []
        return [{"image_ref": "a" * 64}]


def _route(module, name):
    return getattr(module, name).__wrapped__.__wrapped__


def _install_grep(monkeypatch, chunk, *, count_only=False):
    from api.apps.restful_apis import corpus_fs_api

    service = _GrepService(chunk)
    payload = {"query": "营业执照"}
    if count_only:
        payload["count_only"] = True

    def fake_search_authorized(self, _tenant_id, _dataset_ids, _document_ids, _query, **kwargs):
        if kwargs.get("count_only"):
            return {"total": 3, "consumed_count": 0, "chunks": []}
        return {"total": 1, "consumed_count": 1, "chunks": [dict(chunk)]}

    monkeypatch.setattr(corpus_fs_api, "request", SimpleNamespace(get_json=lambda: _AwaitableDict(payload), headers={}))
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)
    monkeypatch.setattr(corpus_fs_api, "_issue_citation", lambda *_args, **_kwargs: "cit1.opaque")
    monkeypatch.setattr(KeywordSearchService, "search_authorized", fake_search_authorized)
    return service


def test_grep_exposes_an_image_reference_for_figure_hits(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    chunk = {"id": "chunk-1", "doc_id": "doc-1", "content_with_weight": "Title: 验收报告", "img_id": "dataset-1-chunk-1", "page_num_int": [1]}
    service = _install_grep(monkeypatch, chunk)

    result = asyncio.run(_route(corpus_fs_api, "grep")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert result["items"][0]["images"] == [{"image_ref": "a" * 64}]
    assert service.image_calls == [("doc-1", "dataset-1-chunk-1")]


def test_grep_returns_no_image_for_text_only_hits(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    chunk = {"id": "chunk-1", "doc_id": "doc-1", "content_with_weight": "正文条款", "img_id": "", "page_num_int": [1]}
    service = _install_grep(monkeypatch, chunk)

    result = asyncio.run(_route(corpus_fs_api, "grep")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert result["items"][0]["images"] == []
    assert service.image_calls == [("doc-1", "")]


def test_grep_count_only_does_not_resolve_images(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    chunk = {"id": "chunk-1", "doc_id": "doc-1", "content_with_weight": "Title: 验收报告", "img_id": "dataset-1-chunk-1", "page_num_int": [1]}
    service = _install_grep(monkeypatch, chunk, count_only=True)

    result = asyncio.run(_route(corpus_fs_api, "grep")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert result["items"] == []
    assert service.image_calls == []
