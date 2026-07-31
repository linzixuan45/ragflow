import pytest

from api.db.joint_services import tenant_model_service
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLangfuseService, TenantLLMService
from common import settings


@pytest.mark.parametrize(
    ("model_type", "model_id", "config_name", "expected_base_url"),
    [
        ("embedding", "Qwen3-Embedding-4B@VLLM", "EMBEDDING_CFG", "http://10.100.15.70:8000/v1"),
        ("rerank", "Qwen3-Reranker-4B@VLLM", "RERANK_CFG", "http://10.100.15.70:8001/v1"),
    ],
)
def test_configured_system_default_does_not_require_tenant_provider(monkeypatch, model_type, model_id, config_name, expected_base_url):
    monkeypatch.setattr(
        settings,
        config_name,
        {"model": model_id, "factory": "VLLM", "api_key": "x", "base_url": expected_base_url, "max_model_len": 2048},
    )
    monkeypatch.setattr(
        tenant_model_service.TenantModelProviderService,
        "get_by_tenant_id_and_provider_name",
        lambda *_args: pytest.fail("system defaults must not query tenant providers"),
    )

    config = tenant_model_service.get_model_config_from_provider_instance("tenant-without-models", model_type, model_id)

    assert config == {
        "llm_factory": "VLLM",
        "api_key": "x",
        "llm_name": model_id.split("@", 1)[0],
        "api_base": expected_base_url,
        "model_type": model_type,
        "max_tokens": 2048,
    }


def test_model_entry_preserves_provider_context_limit():
    entry = settings._parse_model_entry(
        {
            "name": "Qwen3-Embedding-4B",
            "factory": "VLLM",
            "api_key": "x",
            "base_url": "http://10.100.15.70:8000/v1",
            "max_model_len": 2048,
        }
    )

    config = settings._resolve_per_model_config(entry, "", "", "")

    assert config["max_model_len"] == 2048


def test_vllm_embedding_instance_receives_provider_context_limit(monkeypatch):
    import rag.llm as llm_models

    captured = {}

    def build_embedding(_api_key, _model_name, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(llm_models.EmbeddingModel, "VLLM", build_embedding)

    TenantLLMService.model_instance.__wrapped__(
        TenantLLMService,
        {
            "llm_factory": "VLLM",
            "api_key": "x",
            "llm_name": "Qwen3-Embedding-4B",
            "api_base": "http://10.100.15.70:8000/v1",
            "model_type": "embedding",
            "max_tokens": 2048,
        }
    )

    assert captured["max_model_len"] == 2048


def test_vllm_rerank_instance_receives_provider_context_limit(monkeypatch):
    import rag.llm as llm_models

    captured = {}

    def build_reranker(_api_key, _model_name, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(llm_models.RerankModel, "VLLM", build_reranker)

    TenantLLMService.model_instance.__wrapped__(
        TenantLLMService,
        {
            "llm_factory": "VLLM",
            "api_key": "x",
            "llm_name": "Qwen3-Reranker-4B",
            "api_base": "http://10.100.15.70:8001/v1",
            "model_type": "rerank",
            "max_tokens": 1024,
        },
    )

    assert captured["max_model_len"] == 1024


def test_llm_bundle_preserves_language_for_runtime_identity(monkeypatch):
    monkeypatch.setattr(TenantLLMService, "model_instance", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda **_kwargs: None)

    bundle = LLM4Tenant(
        "tenant",
        {
            "llm_factory": "OpenAI-API-Compatible",
            "api_key": "x",
            "llm_name": "vision-model",
            "api_base": "http://vision/v1",
            "model_type": "image2text",
        },
        lang="English",
    )

    assert bundle.lang == "English"


def test_non_default_model_still_requires_tenant_provider(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_CFG", {"model": "Qwen3-Embedding-4B@VLLM", "factory": "VLLM", "api_key": "x", "base_url": "http://local/v1"})
    monkeypatch.setattr(tenant_model_service.TenantModelProviderService, "get_by_tenant_id_and_provider_name", lambda *_args: None)

    with pytest.raises(LookupError, match="Provider VLLM not found"):
        tenant_model_service.get_model_config_from_provider_instance("tenant-without-models", "embedding", "other-model@VLLM")


def test_mineru_env_config_matches_remote_service_contract(monkeypatch):
    monkeypatch.setenv("MINERU_APISERVER", "http://10.100.15.70:8886")
    monkeypatch.setenv("MINERU_BACKEND", "hybrid-engine")
    monkeypatch.setenv("MINERU_PRELOAD", "true")

    config = tenant_model_service._collect_env_config(
        tenant_model_service.MINERU_ENV_KEYS,
        tenant_model_service.MINERU_DEFAULT_CONFIG,
    )

    assert config["MINERU_APISERVER"] == "http://10.100.15.70:8886"
    assert config["MINERU_BACKEND"] == "hybrid-engine"
    assert "MINERU_PRELOAD" not in config
