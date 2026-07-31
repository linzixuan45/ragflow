from types import SimpleNamespace

import pytest

from api.apps.services.keyword_search_service import KeywordSearchService, SearchUnavailableError


class Store:
    def __init__(self):
        self.calls = []

    def index_exist(self, *_args):
        return True

    def search(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"hits": {"total": {"value": 0}, "hits": []}}

    def get_total(self, _result):
        return 0

    def get_fields(self, _result, _fields):
        return {}


def test_keyword_search_performs_one_store_query_without_model_calls(monkeypatch):
    store = Store()
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.docStoreConn", store)
    service = KeywordSearchService()

    assert service.search("tenant", ["dataset"], ["doc"], "VPN request", match="all", limit=20) == {"total": 0, "consumed_count": 0, "chunks": []}
    assert len(store.calls) == 1
    args, _ = store.calls[0]
    assert args[2] == {"doc_id": ["doc"]}
    assert args[5:7] == (0, 20)


@pytest.mark.parametrize(
    ("match", "expected_query", "expected_minimum"),
    [
        ("any", "vpn request AND NOT (blocked site)", 0),
        ("all", "vpn request AND NOT (blocked site)", "100%"),
        ("phrase", '"vpn request" AND NOT (blocked site)', 0),
    ],
)
def test_keyword_search_tokenizes_literal_query_and_exclusions(monkeypatch, match, expected_query, expected_minimum):
    store = Store()
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.docStoreConn", store)
    monkeypatch.setattr(
        "api.apps.services.keyword_search_service.rag_tokenizer.tokenize",
        lambda value: {"VPN requests": "vpn request", "blocked sites": "blocked site"}[value],
    )

    KeywordSearchService().search("tenant", ["dataset"], ["doc"], "VPN requests", match=match, exclude=["blocked sites"])

    expression = store.calls[0][0][3][0]
    assert expression.matching_text == expected_query
    assert expression.extra_options["minimum_should_match"] == expected_minimum


def test_keyword_search_preserves_the_doc_store_row_id(monkeypatch):
    store = Store()
    store.get_total = lambda _result: 1
    store.get_fields = lambda _result, _fields: {"chunk-1": {"doc_id": "doc-1", "content_with_weight": "evidence"}}
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.docStoreConn", store)

    result = KeywordSearchService().search("tenant", ["dataset"], ["doc-1"], "evidence")

    assert result == {
        "total": 1,
        "consumed_count": 1,
        "chunks": [{"id": "chunk-1", "doc_id": "doc-1", "content_with_weight": "evidence"}],
    }


def test_keyword_search_reports_consumed_hits_separately_from_public_mapping(monkeypatch):
    store = Store()
    store.get_total = lambda _result: 20
    store.get_fields = lambda _result, _fields: {
        "chunk-1": {"doc_id": "missing-public-path", "content_with_weight": "hidden"},
        "chunk-2": {"doc_id": "doc-1", "content_with_weight": "visible"},
    }
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.docStoreConn", store)

    result = KeywordSearchService().search("tenant", ["dataset"], ["doc-1"], "evidence", offset=5, limit=2)

    assert result["consumed_count"] == 2


def test_keyword_page_state_advances_by_consumed_hits_not_returned_items():
    page = KeywordSearchService.page_state(
        {"total": 20, "consumed_count": 5, "chunks": []},
        offset=10,
        returned_count=2,
        count_only=False,
    )

    assert page == {
        "total": 20,
        "returned_count": 2,
        "consumed_count": 5,
        "next_offset": 15,
        "truncated": True,
    }


def test_keyword_search_rejects_non_es_backend(monkeypatch):
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.docStoreConn", SimpleNamespace(index_exist=lambda *_: False))
    with pytest.raises(SearchUnavailableError):
        KeywordSearchService().search("tenant", ["dataset"], [], "VPN")


def test_keyword_search_does_not_fall_back_when_engine_is_not_elasticsearch(monkeypatch):
    monkeypatch.setattr("api.apps.services.keyword_search_service.settings.DOC_ENGINE", "infinity")
    with pytest.raises(SearchUnavailableError):
        KeywordSearchService().search("tenant", ["dataset"], [], "VPN")


def test_internal_search_filters_requested_documents_to_authorized_active_documents(monkeypatch):
    service = KeywordSearchService()
    monkeypatch.setattr(service, "authorized_document_ids", lambda tenant_id, dataset_ids, document_ids: ["doc-active"])
    monkeypatch.setattr(service, "search", lambda tenant_id, dataset_ids, document_ids, query, **kwargs: {"total": 1, "chunks": [{"doc_id": document_ids[0]}]})

    result = service.search_authorized("tenant", ["dataset"], ["doc-active", "doc-foreign"], "VPN")

    assert result == {"total": 1, "chunks": [{"doc_id": "doc-active"}]}


def test_internal_search_requires_at_least_one_dataset():
    with pytest.raises(ValueError, match="dataset_ids"):
        KeywordSearchService().search_authorized("tenant", [], [], "VPN")


def test_internal_search_with_no_authorized_requested_document_does_not_query_dataset(monkeypatch):
    service = KeywordSearchService()
    monkeypatch.setattr(service, "authorized_document_ids", lambda tenant_id, dataset_ids, document_ids: [])
    monkeypatch.setattr(service, "search", lambda *_args, **_kwargs: pytest.fail("must not search without an authorized document"))

    assert service.search_authorized("tenant", ["dataset"], ["foreign-document"], "VPN") == {"total": 0, "consumed_count": 0, "chunks": []}


@pytest.mark.parametrize("kwargs", [{"offset": "0"}, {"limit": True}, {"highlight": "false"}, {"exclude": [""]}])
def test_keyword_search_rejects_noncanonical_request_types(kwargs):
    with pytest.raises(ValueError, match="invalid keyword search request"):
        KeywordSearchService().search("tenant", ["dataset"], [], "VPN", **kwargs)
