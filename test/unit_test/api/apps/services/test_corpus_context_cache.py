"""Dataset-context cache: hit/invalidations, and the permission re-check.

The cached value is the permission-independent data layer (documents, files,
links, root). Permissions are re-applied on every request, so a cached entry
must never be able to serve a file the current caller may not see.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from api.apps.services import corpus_fs_service
from api.apps.services.corpus_context_cache import DatasetContextCache, citation_manifest_cache, dataset_context_cache
from api.apps.services.corpus_fs_service import CorpusFsService
from api.db import FileType


@pytest.fixture(autouse=True)
def _isolated_cache():
    dataset_context_cache.clear()
    citation_manifest_cache.clear()
    yield
    dataset_context_cache.clear()
    citation_manifest_cache.clear()


def _service() -> CorpusFsService:
    return CorpusFsService("tenant-1", "dataset-1", "actor")


def _tree(monkeypatch, service, *, file_tenant="tenant-1"):
    files = {
        "file-1": {
            "id": "file-1",
            "parent_id": "root",
            "name": "a.md",
            "type": FileType.DOC.value,
            "tenant_id": file_tenant,
        }
    }
    links = [{"file_id": "file-1", "document_id": "doc-1"}]
    monkeypatch.setattr(service, "_dataset_tree", lambda _docs, _dataset: ({"id": "root"}, files, links))


# -- cache primitive -------------------------------------------------------


def test_cache_returns_stored_values_and_counts_hits_and_misses():
    cache = DatasetContextCache(ttl_seconds=60, max_entries=2)

    assert cache.get(("t", "d", 1)) is None
    cache.put(("t", "d", 1), "value")

    assert cache.get(("t", "d", 1)) == "value"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_cache_expires_entries_after_the_ttl():
    cache = DatasetContextCache(ttl_seconds=0.05, max_entries=2)
    cache.put(("t", "d", 1), "value")

    assert cache.get(("t", "d", 1)) == "value"
    time.sleep(0.06)

    assert cache.get(("t", "d", 1)) is None


def test_cache_evicts_the_least_recently_used_entry():
    cache = DatasetContextCache(ttl_seconds=60, max_entries=2)
    cache.put(("t", "d", 1), "one")
    cache.put(("t", "d", 2), "two")
    cache.get(("t", "d", 1))
    cache.put(("t", "d", 3), "three")

    assert cache.get(("t", "d", 2)) is None
    assert cache.get(("t", "d", 1)) == "one"
    assert cache.get(("t", "d", 3)) == "three"


# -- service integration ---------------------------------------------------


def test_dataset_context_is_served_from_cache_until_the_revision_moves(monkeypatch):
    service = _service()
    loads: list[int] = []

    def document_rows():
        loads.append(1)
        return [{"id": "doc-1", "status": "1"}]

    monkeypatch.setattr(service, "_document_rows", document_rows)
    monkeypatch.setattr(service, "_dataset_tree", lambda _docs, _dataset: ({"id": "root"}, {}, []))

    service.dataset_context(SimpleNamespace(content_revision=7))
    service.dataset_context(SimpleNamespace(content_revision=7))

    assert len(loads) == 1

    service.dataset_context(SimpleNamespace(content_revision=8))

    assert len(loads) == 2


def test_dataset_context_skips_the_cache_when_it_is_disabled(monkeypatch):
    service = _service()
    loads: list[int] = []

    def document_rows():
        loads.append(1)
        return [{"id": "doc-1", "status": "1"}]

    monkeypatch.setattr(service, "_document_rows", document_rows)
    monkeypatch.setattr(service, "_dataset_tree", lambda _docs, _dataset: ({"id": "root"}, {}, []))
    monkeypatch.setenv("CORPUS_CONTEXT_CACHE", "0")

    service.dataset_context(SimpleNamespace(content_revision=7))
    service.dataset_context(SimpleNamespace(content_revision=7))

    assert len(loads) == 2


def test_cache_keys_are_scoped_to_the_calling_tenant(monkeypatch):
    service_a = CorpusFsService("tenant-1", "dataset-1", "actor")
    service_b = CorpusFsService("tenant-2", "dataset-1", "actor")
    loads: list[str] = []

    def rows_for(service):
        def document_rows():
            loads.append(service.tenant_id)
            return [{"id": "doc-1", "status": "1"}]

        return document_rows

    for service in (service_a, service_b):
        monkeypatch.setattr(service, "_document_rows", rows_for(service))
        monkeypatch.setattr(service, "_dataset_tree", lambda _docs, _dataset: ({"id": "root"}, {}, []))

    service_a.dataset_context(SimpleNamespace(content_revision=1))
    service_b.dataset_context(SimpleNamespace(content_revision=1))

    assert loads == ["tenant-1", "tenant-2"]


def test_cached_context_still_applies_file_permissions_on_every_call(monkeypatch):
    """The cache must not be able to hand back an entry the caller lost access to."""

    service = _service()
    monkeypatch.setattr(service, "_dataset", lambda: SimpleNamespace(content_revision=1, name="Policies"))
    monkeypatch.setattr(service, "_document_rows", lambda: [{"id": "doc-1", "status": "1", "name": "a.md"}])
    _tree(monkeypatch, service)
    allowed = {"value": True}
    monkeypatch.setattr(corpus_fs_service, "check_file_team_permission", lambda _file, _tenant: allowed["value"])

    first = service.search_scope("/")
    assert set(first) == {"doc-1"}

    # Same revision, same cached data layer -- but the permission verdict changed.
    allowed["value"] = False
    second = service.search_scope("/")

    assert second == {}


def _storage_service(monkeypatch, service, reads):
    from api.apps.services import corpus_fs_service as module

    monkeypatch.setattr(module.File2DocumentService, "get_storage_address", staticmethod(lambda doc_id: ("bucket", f"{doc_id}.bin")))

    class _Storage:
        @staticmethod
        def get(_bucket, _name):
            reads.append(_name)
            return b"payload"

    monkeypatch.setattr(module.settings, "STORAGE_IMPL", _Storage)
    return service


def test_storage_citation_manifest_is_cached_by_the_document_fingerprint(monkeypatch):
    service = _service()
    reads: list[str] = []
    _storage_service(monkeypatch, service, reads)
    document = {"id": "doc-1", "type": "doc", "content_hash": "fingerprint-a"}

    first = service.citation_context(document)
    second = service.citation_context(document)

    assert first == second
    assert len(first[0]["source_sha256"]) == 64
    assert reads == ["doc-1.bin"]


def test_storage_citation_manifest_is_reread_when_the_fingerprint_changes(monkeypatch):
    service = _service()
    reads: list[str] = []
    _storage_service(monkeypatch, service, reads)

    service.citation_context({"id": "doc-1", "type": "doc", "content_hash": "fingerprint-a"})
    service.citation_context({"id": "doc-1", "type": "doc", "content_hash": "fingerprint-b"})

    assert reads == ["doc-1.bin", "doc-1.bin"]


def test_storage_citation_manifest_is_not_cached_without_a_fingerprint(monkeypatch):
    service = _service()
    reads: list[str] = []
    _storage_service(monkeypatch, service, reads)
    document = {"id": "doc-1", "type": "doc", "content_hash": ""}

    service.citation_context(document)
    service.citation_context(document)

    assert reads == ["doc-1.bin", "doc-1.bin"]
