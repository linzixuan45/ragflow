"""The Corpus FS keyword-only search path.

This module intentionally has no model imports.  A request executes exactly one
document-store search and never falls back to semantic retrieval.
"""

from __future__ import annotations

from common import settings
from common.constants import TaskStatus
from common.doc_store.doc_store_base import MatchTextExpr, OrderByExpr
from api.common.check_team_permission import check_kb_team_permission
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from rag.nlp import search


class SearchUnavailableError(RuntimeError):
    code = "SEARCH_UNAVAILABLE"


class KeywordSearchService:
    def authorized_document_ids(self, tenant_id: str, dataset_ids: list[str], document_ids: list[str] | None) -> list[str]:
        if not isinstance(dataset_ids, list) or not dataset_ids or len(dataset_ids) > 50 or not all(isinstance(dataset_id, str) and dataset_id for dataset_id in dataset_ids):
            raise ValueError("dataset_ids must be a non-empty list")
        if document_ids is not None and (not isinstance(document_ids, list) or len(document_ids) > 1000 or not all(isinstance(document_id, str) and document_id for document_id in document_ids)):
            raise ValueError("document_ids must be a list of non-empty strings")

        requested = set(document_ids) if document_ids is not None else None
        authorized: list[str] = []
        for dataset_id in dataset_ids:
            ok, dataset = KnowledgebaseService.get_by_id(dataset_id)
            if not ok or dataset.status != "1" or not check_kb_team_permission(dataset, tenant_id):
                raise PermissionError("dataset not found")
            documents = DocumentService.query(kb_id=dataset_id, status="1", run=TaskStatus.DONE.value)
            for document in documents:
                document_id = document.id
                if requested is None or document_id in requested:
                    authorized.append(document_id)
        return authorized

    def search_authorized(self, tenant_id: str, dataset_ids: list[str], document_ids: list[str] | None, query: str, **kwargs) -> dict:
        authorized_document_ids = self.authorized_document_ids(tenant_id, dataset_ids, document_ids)
        if document_ids is not None and not authorized_document_ids:
            return {"total": 0, "chunks": []}
        return self.search(tenant_id, dataset_ids, authorized_document_ids, query, **kwargs)

    def search(self, tenant_id: str, dataset_ids: list[str], document_ids: list[str], query: str, *, match: str = "any", exclude: list[str] | None = None, offset: int = 0, limit: int = 20, highlight: bool = True, count_only: bool = False) -> dict:
        if (
            not isinstance(query, str)
            or not query
            or len(query.encode("utf-8")) > 4096
            or match not in {"any", "all", "phrase"}
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or not isinstance(highlight, bool)
            or not isinstance(count_only, bool)
            or (exclude is not None and (not isinstance(exclude, list) or len(exclude) > 20 or not all(isinstance(item, str) and item and len(item.encode("utf-8")) <= 256 for item in exclude)))
        ):
            raise ValueError("invalid keyword search request")
        if getattr(settings, "DOC_ENGINE", "elasticsearch").lower() not in {"elasticsearch", "es"}:
            raise SearchUnavailableError("keyword search requires Elasticsearch")
        index_name = search.index_name(tenant_id)
        if not settings.docStoreConn.index_exist(index_name, dataset_ids[0] if dataset_ids else None):
            raise SearchUnavailableError("keyword search is unavailable")
        condition = {"doc_id": document_ids} if document_ids else {}
        match_expression = MatchTextExpr(["content_ltks", "title_tks"], f'"{query}"' if match == "phrase" else query, limit, {"operator": "and" if match == "all" else "or"})
        if exclude:
            condition["must_not"] = {"content_ltks": exclude}
        order_by = OrderByExpr().desc("_score").asc("doc_id").asc("chunk_order_int")
        result = settings.docStoreConn.search(["id", "doc_id", "content_with_weight", "page_num_int", "position_int"], ["content_with_weight"] if highlight else [], condition, [match_expression], order_by, offset, 0 if count_only else limit, index_name, dataset_ids)
        total = settings.docStoreConn.get_total(result)
        if count_only:
            return {"total": total, "chunks": []}
        fields = settings.docStoreConn.get_fields(result, ["id", "doc_id", "content_with_weight", "page_num_int", "position_int"])
        return {"total": total, "chunks": list(fields.values())}
