import asyncio


class _Request:
    def __init__(self):
        self.headers = {}

    async def get_json(self):
        return {
            "scopes": [{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
            "query": "VPN",
            "limit": 8,
        }


def test_corpus_search_route_uses_shared_tool_service(monkeypatch):
    from api.apps.restful_apis import corpus_fs_api

    captured = {}

    class ToolService:
        def __init__(self, tenant_id, actor_ref):
            captured["identity"] = (tenant_id, actor_ref)

        async def search(self, **kwargs):
            captured["request"] = kwargs
            return {"schema": "ragflow.corpus.evidence.v1", "items": []}

    monkeypatch.setattr(corpus_fs_api, "request", _Request())
    monkeypatch.setattr(corpus_fs_api, "CorpusToolService", ToolService)
    monkeypatch.setattr(corpus_fs_api, "get_result", lambda *, data: data)

    route = corpus_fs_api.corpus_search.__wrapped__.__wrapped__
    result = asyncio.run(route(tenant_id="tenant-1"))

    assert result["schema"] == "ragflow.corpus.evidence.v1"
    assert captured["identity"] == ("tenant-1", "tenant-1")
    assert captured["request"] == {
        "scopes": [{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
        "query": "VPN",
        "limit": 8,
        "per_document_limit": 3,
        "similarity_threshold": 0.0,
        "vector_similarity_weight": 0.3,
        "mode": "hybrid",
    }
