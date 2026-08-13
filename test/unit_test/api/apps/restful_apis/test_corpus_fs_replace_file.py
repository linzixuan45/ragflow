import asyncio
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace


class _AwaitableDict(dict):
    def __await__(self):
        async def value():
            return self

        return value().__await__()


class _Service:
    def __init__(self, *, parse_error: Exception | None = None):
        self.parse_error = parse_error
        self.calls = []

    @contextmanager
    def content_mutation(self, revision):
        self.calls.append(("revision", revision))
        yield

    def replace_original(self, path, content, expected_content_hash):
        self.calls.append(("replace", path, content, expected_content_hash))
        return {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "content_hash": "saved-hash",
        }

    def retry_parse(self, path):
        self.calls.append(("parse", path))
        if self.parse_error:
            raise self.parse_error
        return {"status": "RUNNING"}


def _route_function(module):
    return module.replace_file.__wrapped__.__wrapped__


def test_replace_file_route_forwards_revision_hash_and_parse_status(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    service = _Service()
    request = SimpleNamespace(
        form=_AwaitableDict(
            path="/files/report.xlsx", expected_content_hash="source-hash"
        ),
        files=_AwaitableDict(
            file=SimpleNamespace(read=lambda: b"replacement-xlsx")
        ),
        headers={"If-Match": '"7"'},
    )
    monkeypatch.setattr(corpus_fs_api, "request", request)
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)

    result = asyncio.run(
        _route_function(corpus_fs_api)(tenant_id="tenant-1", dataset_id="dataset-1")
    )

    assert result["content_hash"] == "saved-hash"
    assert result["parse_status"] == "RUNNING"
    assert service.calls == [
        ("revision", 7),
        ("replace", "/files/report.xlsx", b"replacement-xlsx", "source-hash"),
        ("parse", "/files/report.xlsx"),
    ]


def test_replace_file_route_reports_parse_queue_failure_after_source_save(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    service = _Service(parse_error=RuntimeError("queue unavailable"))
    request = SimpleNamespace(
        form=_AwaitableDict(
            path="/files/report.xlsx", expected_content_hash="source-hash"
        ),
        files=_AwaitableDict(
            file=SimpleNamespace(read=lambda: BytesIO(b"replacement-xlsx").read())
        ),
        headers={"If-Match": "8"},
    )
    monkeypatch.setattr(corpus_fs_api, "request", request)
    monkeypatch.setattr(corpus_fs_api, "_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(corpus_fs_api, "_success", lambda _service, data: data)

    result = asyncio.run(
        _route_function(corpus_fs_api)(tenant_id="tenant-1", dataset_id="dataset-1")
    )

    assert result["content_hash"] == "saved-hash"
    assert result["parse_status"] == "FAILED"
    assert result["parse_message"] == "queue unavailable"
