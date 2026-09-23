from rag.graphrag.chat_llm import resolve_graphrag_chat_model_config


def test_override_wins_over_tenant_default(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise AssertionError("tenant default must not be used when chat_llm is complete")

    monkeypatch.setattr("rag.graphrag.chat_llm.tenant_chat_model_config", _fail)

    config = resolve_graphrag_chat_model_config(
        "tenant-1",
        {
            "llm_id": "Qwen/Qwen3.6-35B-A3B@SILICONFLOW@SILICONFLOW",
            "graphrag": {
                "use_graphrag": True,
                "chat_llm": {
                    "llm_factory": "OpenAI-API-Compatible",
                    "llm_name": "Qwen3_32B_INT8",
                    "api_base": "http://10.100.15.70:11421/v1",
                    "deerflow_model": "Qwen3-35B-A3B-FP8",
                },
            },
        },
        "tenant-default",
    )

    assert config["llm_factory"] == "OpenAI-API-Compatible"
    assert config["llm_name"] == "Qwen3_32B_INT8"
    assert config["api_base"] == "http://10.100.15.70:11421/v1"
    assert config["model_type"] == "chat"
    assert "extra_body" not in config


def test_override_forwards_thinking_extra_body(monkeypatch):
    monkeypatch.setattr("rag.graphrag.chat_llm.tenant_chat_model_config", lambda *_a, **_k: None)

    config = resolve_graphrag_chat_model_config(
        "tenant-1",
        {
            "graphrag": {
                "chat_llm": {
                    "llm_factory": "OpenAI-API-Compatible",
                    "llm_name": "Qwen3_32B_INT8",
                    "api_base": "http://10.100.15.70:11421/v1",
                    "extra_body": {
                        "enable_thinking": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                },
            },
        },
        "tenant-default",
    )

    assert config["extra_body"]["enable_thinking"] is False
    assert config["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_incomplete_override_falls_back_to_tenant_default(monkeypatch):
    captured = {}

    def _tenant(tenant_id, llm_id):
        captured["args"] = (tenant_id, llm_id)
        return {"llm_factory": "SILICONFLOW", "llm_name": llm_id}

    monkeypatch.setattr("rag.graphrag.chat_llm.tenant_chat_model_config", _tenant)

    config = resolve_graphrag_chat_model_config(
        "tenant-1",
        {
            "llm_id": "tenant-chat",
            "graphrag": {"chat_llm": {"llm_name": "missing-endpoint"}},
        },
        "fallback-chat",
    )

    assert captured["args"] == ("tenant-1", "tenant-chat")
    assert config["llm_name"] == "tenant-chat"


def test_missing_parser_config_uses_fallback_llm_id(monkeypatch):
    captured = {}

    def _tenant(tenant_id, llm_id):
        captured["llm_id"] = llm_id
        return {"llm_name": llm_id}

    monkeypatch.setattr("rag.graphrag.chat_llm.tenant_chat_model_config", _tenant)

    resolve_graphrag_chat_model_config("tenant-1", None, "fallback-chat")
    assert captured["llm_id"] == "fallback-chat"
