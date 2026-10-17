import asyncio
import threading
from types import SimpleNamespace


def test_image_uses_signed_citation_version_and_reauthorizes_document(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    calls = []
    target = {"projection_revision": 4, "source_hash": "a" * 64}
    service = SimpleNamespace(
        authorize_citation=lambda doc_id: (calls.append(("authorize", doc_id)) or {"id": doc_id}, {"path": "/renamed.docx"}),
        image_data=lambda doc, ref, **kwargs: (calls.append((doc, ref, kwargs)) or b"image", "image.png"),
    )
    monkeypatch.setattr(corpus_fs_api, "request", SimpleNamespace(args={"path": "/old.docx", "image_ref": "a" * 64, "citation_ref": "signed-citation"}))
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args: service)
    monkeypatch.setattr(corpus_fs_api, "_citation_service", lambda: SimpleNamespace(resolve_locator=lambda ref, tenant, dataset: ("doc-1", target)))

    async def response(_data):
        return SimpleNamespace(headers={})

    monkeypatch.setattr(corpus_fs_api, "make_response", response)
    result = asyncio.run(_route(corpus_fs_api, "image")("tenant-1", "dataset-1"))
    assert calls == [("authorize", "doc-1"), ({"id": "doc-1"}, "a" * 64, {"citation_target": target})]
    assert result.headers["Cache-Control"] == "private, no-store"


class _AwaitableDict(dict):
    def __await__(self):
        async def value():
            return self

        return value().__await__()


class _Service:
    def __init__(self):
        self.io_thread = None

    def _dataset(self):
        return SimpleNamespace(id="dataset-1")

    def revision(self, _dataset):
        return {"content_revision": 8}

    def original_data(self, path):
        self.io_thread = threading.get_ident()
        return (
            {"name": "legal.docx", "mime_type": "application/octet-stream", "content_hash": "abc"},
            b"docx-bytes",
            "hash",
        )

    def deletion_targets(self, paths):
        self.io_thread = threading.get_ident()
        return (["doc-1"], 1, 0)

    def markdown_current(self, path):
        self.io_thread = threading.get_ident()
        return {"path": path, "content": "# note"}


def _route(module, name):
    return getattr(module, name).__wrapped__.__wrapped__


def test_blob_reads_original_data_off_the_event_loop_thread(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    service = _Service()
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(corpus_fs_api, "request", SimpleNamespace(args={"path": "/legal.docx"}, headers={}))
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)

    captured = {}

    async def fake_file_response(data, name, content_type, content_hash):
        captured["data"] = data
        captured["name"] = name
        return {"data": data, "name": name}

    monkeypatch.setattr(corpus_fs_api, "_file_response", fake_file_response)

    result = asyncio.run(_route(corpus_fs_api, "blob")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert service.io_thread not in {None, event_loop_thread}
    assert result["name"] == "legal.docx"
    assert captured["data"] == b"docx-bytes"


def test_delete_preview_runs_off_the_event_loop_thread(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    service = _Service()
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(
        corpus_fs_api,
        "request",
        SimpleNamespace(get_json=lambda: _AwaitableDict({"paths": ["/legal.docx"]}), headers={}),
    )
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)
    monkeypatch.setattr(corpus_fs_api, "_issue_delete_token", lambda *_args, **_kwargs: "token")

    result = asyncio.run(_route(corpus_fs_api, "delete_preview")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert service.io_thread not in {None, event_loop_thread}
    assert result["file_count"] == 1
    assert result["confirmation_token"] == "token"


def test_derived_content_get_runs_off_the_event_loop_thread(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    service = _Service()
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(
        corpus_fs_api,
        "request",
        SimpleNamespace(method="GET", args={"path": "/note.md"}, get_json=lambda: _AwaitableDict({}), headers={}),
    )
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)

    result = asyncio.run(_route(corpus_fs_api, "derived_content")(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert service.io_thread not in {None, event_loop_thread}
    assert result["path"] == "/note.md"
    assert result["content"] == "# note"
