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


@pytest.mark.asyncio
async def test_search_defaults_to_system_rerank_model_when_rerank_id_absent(monkeypatch):
    """search() must fall back to the configured system default rerank model
    (settings.RERANK_MDL) when neither search_config nor the request body
    provides an explicit rerank_id, so the corpus fs path can rely on the
    LOCAL_RERANK_MODEL (Qwen3-Reranker-4B) without per-tenant setup."""
    from common import settings

    kb = SimpleNamespace(tenant_id="tenant-1", embd_id="", to_dict=lambda: {"id": "dataset-1"})
    req = {
        "question": "test query",
        "doc_ids": [],
        "page": 1,
        "size": 15,
        "similarity_threshold": 0.2,
        "vector_similarity_weight": 0.3,
        "top_k": 50,
    }
    captured = {}

    class DummyBundle:
        def __init__(self, tenant_id, model_config):
            self.tenant_id = tenant_id
            self.model_config = model_config

    async def fake_retrieval(*_args, **kwargs):
        captured["rerank_mdl"] = kwargs.get("rerank_mdl")
        return {"chunks": [], "total": 0, "doc_aggs": []}

    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "accessible", lambda *_a, **_k: True)
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, kb))
    monkeypatch.setattr(
        dataset_api_service.UserTenantService,
        "query",
        lambda **_kw: [SimpleNamespace(tenant_id="tenant-1")],
    )
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "query", lambda **_kw: [object()])
    monkeypatch.setattr(settings, "RERANK_MDL", "Qwen3-Reranker-4B@VLLM")
    monkeypatch.setattr(
        dataset_api_service,
        "get_model_config_from_provider_instance",
        lambda _tenant_id, _model_type, model_name: {
            "llm_factory": "VLLM",
            "api_key": "x",
            "llm_name": "Qwen3-Reranker-4B",
            "api_base": "http://10.100.15.70:8001/v1",
            "model_type": "rerank",
            "max_tokens": 2048,
        }
        if model_name == "Qwen3-Reranker-4B@VLLM"
        else (_ for _ in ()).throw(LookupError(model_name)),
    )
    monkeypatch.setattr(
        "api.db.joint_services.tenant_model_service.get_tenant_default_model_by_type",
        lambda _tenant_id, _model_type: {
            "llm_factory": "VLLM",
            "api_key": "x",
            "llm_name": "Qwen3-Embedding-4B",
            "api_base": "http://10.100.15.70:8000/v1",
            "model_type": "embedding",
        },
    )
    monkeypatch.setattr("api.db.services.llm_service.LLMBundle", DummyBundle)
    monkeypatch.setattr("rag.app.tag.label_question", lambda *_a, **_k: {})
    monkeypatch.setattr(settings.retriever, "retrieval", fake_retrieval)
    monkeypatch.setattr(settings.retriever, "retrieval_by_children", lambda chunks, _tenant_ids: chunks)

    success, _ = await dataset_api_service.search("dataset-1", "tenant-1", req)

    assert success is True
    assert isinstance(captured.get("rerank_mdl"), DummyBundle)
    assert captured["rerank_mdl"].model_config["llm_name"] == "Qwen3-Reranker-4B"
