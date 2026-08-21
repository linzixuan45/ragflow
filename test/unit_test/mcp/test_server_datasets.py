#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import importlib.util
import json
from pathlib import Path

import pytest


def _load_mcp_server():
    server_path = Path(__file__).resolve().parents[3] / "mcp" / "server" / "server.py"
    spec = importlib.util.spec_from_file_location("ragflow_mcp_server_unit", server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _datasets(count):
    return [{"id": f"dataset-{idx}", "description": f"description-{idx}"} for idx in range(count)]


def test_retrieval_chunk_mapping_drops_internal_ids_and_raw_fields(mcp_server):
    connector = mcp_server.RAGFlowConnector(base_url=mcp_server.BASE_URL)

    mapped = connector._map_chunk_fields(
        {
            "id": "chunk-secret",
            "doc_id": "document-secret",
            "dataset_id": "dataset-secret",
            "content_with_weight": "approved evidence",
            "document_keyword": "handbook.pdf",
            "similarity": 0.9,
            "vector_similarity": 0.8,
            "term_similarity": 0.7,
            "page_num_int": [2],
            "position_int": [[1, 2, 3, 4]],
            "untrusted_extra": "must not leak",
        },
        {"dataset-secret": {"name": "Handbook"}},
        {"document-secret": {"name": "handbook.pdf", "document_id": "document-secret"}},
    )

    assert mapped == {
        "content": "approved evidence",
        "dataset_name": "Handbook",
        "document_name": "handbook.pdf",
        "page": [2],
        "positions": [[1, 2, 3, 4]],
        "score": 0.9,
        "vector_similarity": 0.8,
        "term_similarity": 0.7,
    }


def test_corpus_toolbox_exposes_read_only_path_tools(mcp_server):
    assert {tool.name for tool in mcp_server._corpus_tools()} == {
        "ragflow_corpus_tree",
        "ragflow_corpus_find",
        "ragflow_corpus_read",
        "ragflow_corpus_grep",
        "ragflow_corpus_search",
        "ragflow_corpus_stat",
        "ragflow_corpus_citation_preview",
    }


@pytest.mark.asyncio
async def test_tool_discovery_does_not_embed_dynamic_dataset_catalog(mcp_server):
    class Connector:
        async def list_datasets(self, **_kwargs):
            raise AssertionError("tool discovery must not fetch datasets")

        async def list_chats(self, **_kwargs):
            raise AssertionError("tool discovery must not fetch chats")

    tools = await mcp_server.list_tools.__wrapped__(connector=Connector(), api_key="unit-key")

    assert "ragflow_corpus_search" in {tool.name for tool in tools}
    assert all("dataset-" not in tool.description for tool in tools)


@pytest.mark.asyncio
async def test_corpus_search_calls_path_only_batch_endpoint(monkeypatch, mcp_server):
    connector = mcp_server.RAGFlowConnector(base_url=mcp_server.BASE_URL)
    calls = []

    async def _post(path, json=None, api_key="", **_kwargs):
        calls.append((path, json, api_key))
        return _FakeResponse(
            {
                "code": 0,
                "data": {
                    "schema": "ragflow.corpus.evidence.v1",
                    "items": [{"scope": "handbook", "path": "/guide.pdf", "snippet": "evidence", "citation_ref": "cit1.opaque"}],
                },
            }
        )

    monkeypatch.setattr(connector, "_post", _post)

    result = await connector.corpus_search(
        api_key="unit-key",
        scopes=[{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
        query="VPN",
        limit=8,
    )

    assert json.loads(result[0].text)["items"][0]["path"] == "/guide.pdf"
    assert calls == [
        (
            "/datasets/corpus-search",
            {
                "scopes": [{"scope": "handbook", "dataset_id": "dataset-1", "path": "/"}],
                "query": "VPN",
                "limit": 8,
                "per_document_limit": 3,
                "similarity_threshold": 0.0,
                "vector_similarity_weight": 0.3,
                "mode": "hybrid",
            },
            "unit-key",
        )
    ]


@pytest.fixture()
def mcp_server():
    return _load_mcp_server()


def _stub_dataset_pages(monkeypatch, connector, datasets):
    requests = []

    async def _get(path, params=None, api_key=""):
        requests.append({"path": path, "params": dict(params), "api_key": api_key})
        page = params["page"]
        page_size = params["page_size"]
        start = (page - 1) * page_size
        end = start + page_size
        return _FakeResponse({"code": 0, "data": datasets[start:end], "total": len(datasets)})

    monkeypatch.setattr(connector, "_get", _get)
    return requests


@pytest.mark.asyncio
async def test_list_datasets_default_fetches_all_with_rest_page_size_limit(monkeypatch, mcp_server):
    connector = mcp_server.RAGFlowConnector(base_url=mcp_server.BASE_URL)
    requests = _stub_dataset_pages(monkeypatch, connector, _datasets(250))

    result = await connector.list_datasets(api_key="unit-key")

    rows = [json.loads(line) for line in result.splitlines()]
    assert [row["id"] for row in rows] == [f"dataset-{idx}" for idx in range(250)]
    assert [request["params"]["page"] for request in requests] == [1, 2, 3]
    assert all(request["path"] == "/datasets" for request in requests)
    assert all(request["params"]["page_size"] == 100 for request in requests)


@pytest.mark.asyncio
async def test_resolve_dataset_ids_fetches_all_pages_and_deduplicates(monkeypatch, mcp_server):
    connector = mcp_server.RAGFlowConnector(base_url=mcp_server.BASE_URL)
    datasets = _datasets(101) + [{"id": "dataset-100", "description": "duplicate"}]
    requests = _stub_dataset_pages(monkeypatch, connector, datasets)

    result = await connector.resolve_dataset_ids(api_key="unit-key")

    assert result == [f"dataset-{idx}" for idx in range(101)]
    assert [request["params"]["page"] for request in requests] == [1, 2]
    assert all(request["params"]["page_size"] == 100 for request in requests)


@pytest.mark.asyncio
async def test_list_datasets_clamps_explicit_page_size_to_rest_limit_and_preserves_filters(monkeypatch, mcp_server):
    connector = mcp_server.RAGFlowConnector(base_url=mcp_server.BASE_URL)
    requests = _stub_dataset_pages(monkeypatch, connector, _datasets(150))

    result = await connector.list_datasets(
        api_key="unit-key",
        page=1,
        page_size=1000,
        orderby="name",
        desc=False,
        id="dataset-1",
        name="target",
    )

    assert len(result.splitlines()) == 100
    assert len(requests) == 1
    assert requests[0]["params"] == {
        "page": 1,
        "page_size": 100,
        "orderby": "name",
        "desc": False,
        "id": "dataset-1",
        "name": "target",
    }
