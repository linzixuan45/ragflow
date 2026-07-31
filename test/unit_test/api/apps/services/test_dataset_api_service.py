from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from api.apps.services import dataset_api_service
from api.db.services.file_service import FileService


@pytest.mark.asyncio
async def test_create_dataset_initializes_corpus_file_hierarchy(monkeypatch):
    created = {"id": "dataset-1", "name": "Policies", "embd_id": ""}
    persisted = {**created, "embd_id": "embedding-1"}
    dataset = SimpleNamespace(name="Policies", to_dict=lambda: persisted)
    calls = []
    transaction = {"active": False}

    @contextmanager
    def atomic():
        transaction["active"] = True
        try:
            yield
        finally:
            transaction["active"] = False

    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "create_with_name", lambda **_kwargs: (True, created.copy()))
    monkeypatch.setattr(dataset_api_service.TenantService, "get_by_id", lambda _tenant_id: (True, SimpleNamespace(embd_id="embedding-1")))
    monkeypatch.setattr(dataset_api_service.DB, "atomic", atomic, raising=False)
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "save", lambda **_kwargs: transaction["active"])
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, dataset))
    monkeypatch.setattr(dataset_api_service, "remap_dictionary_keys", lambda value: value)
    monkeypatch.setattr(
        FileService,
        "ensure_dataset_folder",
        classmethod(lambda _cls, tenant_id, name: calls.append((tenant_id, name, transaction["active"]))),
        raising=False,
    )

    success, result = await dataset_api_service.create_dataset("tenant-1", {"name": "Policies"})

    assert success is True
    assert result == persisted
    assert calls == [("tenant-1", "Policies", True)]


@pytest.mark.asyncio
async def test_create_dataset_rolls_back_when_saved_dataset_cannot_be_loaded(monkeypatch):
    events = []

    @contextmanager
    def atomic():
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise

    monkeypatch.setattr(dataset_api_service.DB, "atomic", atomic, raising=False)
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "create_with_name", lambda **_kwargs: (True, {"id": "dataset-1", "name": "Policies", "embd_id": "embedding-1"}))
    monkeypatch.setattr(dataset_api_service.TenantService, "get_by_id", lambda _tenant_id: (True, SimpleNamespace(embd_id="embedding-1")))
    monkeypatch.setattr(dataset_api_service, "verify_embedding_availability", lambda *_args: (True, None))
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "save", lambda **_kwargs: True)
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (False, None))

    with pytest.raises(RuntimeError, match="Dataset created failed"):
        await dataset_api_service.create_dataset("tenant-1", {"name": "Policies"})

    assert events == ["begin", "rollback"]
