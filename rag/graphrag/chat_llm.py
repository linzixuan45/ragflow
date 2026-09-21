"""Resolve the chat model GraphRAG should bind.

DeerFlow administrators pick the extraction model in the admin console
(工具与任务 → knowledge_graph). Gateway writes that choice into
parser_config.graphrag.chat_llm as an OpenAI-compatible endpoint. When that
override is present and complete, GraphRAG uses it instead of the tenant
default llm id (which is also used by RAPTOR / keyword extraction and must
not be overwritten).
"""

from __future__ import annotations

from typing import Any

from common.constants import LLMType

_DEFAULT_FACTORY = "OpenAI-API-Compatible"
_CREDENTIAL_FIELD = "api_key"
_PLACEHOLDER_TOKEN = "not-needed"


def tenant_chat_model_config(tenant_id: str, llm_id: str | None) -> dict[str, Any]:
    """Look up the tenant/default chat model. Isolated so tests can patch it."""
    from api.db.joint_services.tenant_model_service import get_model_config_from_provider_instance

    return get_model_config_from_provider_instance(tenant_id, LLMType.CHAT, llm_id)


def resolve_graphrag_chat_model_config(
    tenant_id: str,
    kb_parser_config: dict[str, Any] | None,
    fallback_llm_id: str | None,
) -> dict[str, Any]:
    """Return the chat model config GraphRAG should instantiate.

    Preference:
    1. parser_config.graphrag.chat_llm when it has llm_name and api_base
    2. parser_config.llm_id / tenant default (existing behaviour)
    """
    parser = kb_parser_config if isinstance(kb_parser_config, dict) else {}
    graphrag = parser.get("graphrag") if isinstance(parser.get("graphrag"), dict) else {}
    chat_llm = graphrag.get("chat_llm") if isinstance(graphrag, dict) else None
    if isinstance(chat_llm, dict):
        llm_name = str(chat_llm.get("llm_name") or "").strip()
        api_base = str(chat_llm.get("api_base") or "").strip()
        if llm_name and api_base:
            factory = str(chat_llm.get("llm_factory") or _DEFAULT_FACTORY).strip() or _DEFAULT_FACTORY
            token = str(chat_llm.get(_CREDENTIAL_FIELD) or "").strip() or _PLACEHOLDER_TOKEN
            return {
                "llm_factory": factory,
                _CREDENTIAL_FIELD: token,
                "llm_name": llm_name,
                "api_base": api_base,
                "model_type": LLMType.CHAT.value,
            }

    llm_id = str(parser.get("llm_id") or "").strip() or fallback_llm_id
    return tenant_chat_model_config(tenant_id, llm_id)
