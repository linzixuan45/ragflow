import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace


class _AwaitableDict(dict):
    def __await__(self):
        async def value():
            return self

        return value().__await__()

    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class _Service:
    def __init__(self):
        self.tenant_id = "tenant-1"
        self.mutation_thread = None
        self.calls = []

    def _dataset(self):
        return SimpleNamespace(id="dataset-1", tenant_id="tenant-1")

    @contextmanager
    def content_mutation(self, revision):
        self.mutation_thread = threading.get_ident()
        self.calls.append(("revision", revision))
        yield

    def resolve_writable(self, parent_path, folder=True):
        self.calls.append(("resolve", parent_path, folder))

    def relocate_uploaded_documents(self, document_ids, parent_path):
        self.calls.append(("relocate", list(document_ids), parent_path))

    def uploaded_entries(self, document_ids):
        return [{"path": "/legal.docx", "name": "legal.docx", "kind": "file"}]

    def revision(self, _dataset):
        return {"content_revision": 8}


def _route_function(module):
    return module.upload.__wrapped__.__wrapped__


def _install_request(monkeypatch, module, *, filename="legal.docx", content=b"docx-bytes"):
    request = SimpleNamespace(
        form=_AwaitableDict(parent_path="/"),
        files=_AwaitableDict(file=SimpleNamespace(filename=filename, read=lambda: content)),
        headers={"If-Match": '"7"'},
    )
    monkeypatch.setattr(module, "request", request)
    return request


def test_upload_runs_content_mutation_off_the_event_loop_thread(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api
    from api.db.services.file_service import FileService

    service = _Service()
    event_loop_thread = threading.get_ident()
    _install_request(monkeypatch, corpus_fs_api)
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)
    monkeypatch.setattr(
        FileService,
        "upload_document",
        lambda *_args, **_kwargs: (
            [],
            [({"id": "doc-1", "name": "legal.docx"}, b"docx-bytes")],
        ),
    )
    monkeypatch.setattr(corpus_fs_api.DocumentService, "filter_update", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(corpus_fs_api.TaskService, "filter_delete", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(corpus_fs_api.DocumentService, "get_by_id", lambda _doc_id: (False, None))

    result = asyncio.run(_route_function(corpus_fs_api)(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert service.mutation_thread not in {None, event_loop_thread}
    assert result["items"][0]["path"] == "/legal.docx"
    assert ("revision", 7) in service.calls
    assert ("resolve", "/", True) in service.calls


def test_upload_maps_closed_mysql_connection_to_unavailable(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api
    from api.apps.services.corpus_errors import CorpusUnavailableError
    from api.db.services.file_service import FileService

    captured = {}

    def fake_error(error):
        captured["error"] = error
        return error

    _install_request(monkeypatch, corpus_fs_api)
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: _Service())
    monkeypatch.setattr(corpus_fs_api, "_error", fake_error)
    monkeypatch.setattr(
        FileService,
        "upload_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(9, "Bad file descriptor")),
    )

    result = asyncio.run(_route_function(corpus_fs_api)(tenant_id="tenant-1", dataset_id="dataset-1"))

    assert result is captured["error"]
    assert isinstance(result, CorpusUnavailableError)
    assert result.code == "UNAVAILABLE"
    assert result.status == 503
    assert isinstance(result.__cause__, OSError)
    assert result.__cause__.errno == 9


def test_error_maps_closed_mysql_connection_to_http_503(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    captured = {}

    class _Response:
        status_code = 200

    def fake_get_error_data_result(message="Sorry! Data missing!", code=None, **_kwargs):
        captured["message"] = message
        return _Response()

    monkeypatch.setattr(corpus_fs_api, "get_error_data_result", fake_get_error_data_result)

    response = corpus_fs_api._error(OSError(9, "Bad file descriptor"))

    assert captured["message"] == "UNAVAILABLE"
    assert response.status_code == 503


def test_error_does_not_map_file_not_found_to_unavailable(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    captured = {}

    class _Response:
        status_code = 200

    def fake_get_error_data_result(message="Sorry! Data missing!", code=None, **_kwargs):
        captured["message"] = message
        return _Response()

    monkeypatch.setattr(corpus_fs_api, "get_error_data_result", fake_get_error_data_result)

    response = corpus_fs_api._error(FileNotFoundError("missing object"))

    assert captured["message"] == "INVALID_ARGUMENT"
    assert response.status_code == 400
