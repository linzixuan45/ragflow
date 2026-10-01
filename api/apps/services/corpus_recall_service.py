"""Fused multi-dataset recall for the Corpus search path.

The single-dataset retrieval entry (``dataset_api_service.search``) runs the whole
pipeline -- query embedding, document-store search, deleted-chunk pruning, parent
chunk expansion and reranking -- once per dataset. Corpus search fans that out
over every requested scope, so both latency and reranker load grow linearly with
the number of datasets.

This service keeps the same retrieval semantics but fuses the candidate stage:

1. one strict search across every in-scope dataset (kb filter only),
2. a filter-only fallback per dataset whose strict query cannot reach it (this
   mirrors ``Dealer.search``'s empty-result fallback, which the per-dataset path
   relied on: with a ``doc_ids`` filter it degrades to "any chunk of these
   documents"),
3. one rerank over the merged pool instead of one per dataset.

Candidate visibility stays exact: chunks whose document is not visible under the
requested path are dropped in memory, so the strict query needs no per-dataset
``doc_ids`` filter.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from common import settings
from common.constants import LLMType
from common.misc_utils import thread_pool_exec
from rag.app.tag import label_question
from rag.nlp import search as search_module

logger = logging.getLogger(__name__)


class CorpusRecallService:
    """Recall candidates for many datasets with one embed, one window and one rerank."""

    STRICT_POOL = 40
    FALLBACK_PER_SCOPE = 10
    FALLBACK_TRIGGER = 1
    # The reranker costs roughly two seconds per 40k tokens of *distinct*
    # candidate text, and the merged pool is "one shared strict window plus one
    # recovery window per dataset whose strict hits are invisible", so its size
    # grows with the scope count: 92 candidates at 12 scopes, 127 at 16. The
    # budget below is spent as a per-scope quota, never as a global score cut --
    # dropping the globally lowest scoring candidates is exactly the window
    # monopoly that the per-dataset recovery window exists to prevent. With
    # MIN_RECOVERY_PER_SCOPE the pool is bounded by
    # STRICT_POOL + MAX_SCOPES * MIN_RECOVERY_PER_SCOPE = 140 at 50 scopes.
    POOL_BUDGET_CANDIDATES = 64
    MIN_RECOVERY_PER_SCOPE = 2
    # At 50 scopes the recovery windows are issued as one gather of ~50
    # concurrent searches. Their Elasticsearch work runs in worker threads, but
    # the Python side of that burst competes for the GIL with the event loop:
    # measured stalls were 0.14~0.25s at 50 scopes versus 0.01~0.07s at 12~16.
    # Bounding the fan-out keeps the loop responsive without lengthening the
    # request (the windows are independent, so a few at a time suffice).
    RECOVERY_CONCURRENCY = 8

    @classmethod
    def recovery_quota(cls, scope_count: int) -> int:
        """Candidates each recovered dataset may add to the rerank pool."""
        if scope_count <= 0:
            return cls.FALLBACK_PER_SCOPE
        return max(cls.MIN_RECOVERY_PER_SCOPE, min(cls.FALLBACK_PER_SCOPE, cls.POOL_BUDGET_CANDIDATES // scope_count))

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self._dealer = settings.retriever

    # -- model resolution --------------------------------------------------
    def _embedding_model(self, tenant_id: str, embd_id: str):
        from api.db.joint_services.tenant_model_service import get_model_config_from_provider_instance
        from api.db.services.llm_service import LLMBundle

        config = get_model_config_from_provider_instance(tenant_id, LLMType.EMBEDDING, embd_id)
        return LLMBundle(tenant_id, config)

    def _rerank_model(self, tenant_id: str, model_id: str | None):
        from api.db.joint_services.tenant_model_service import get_model_config_from_provider_instance
        from api.db.services.llm_service import LLMBundle

        if not model_id:
            return None
        try:
            config = get_model_config_from_provider_instance(tenant_id, LLMType.RERANK.value, model_id)
            return LLMBundle(tenant_id, config)
        except Exception:
            logger.warning("corpus recall: rerank model unavailable, skipping rerank", exc_info=True)
            return None

    # -- public API --------------------------------------------------------
    async def recall(
        self,
        scopes: list[dict[str, Any]],
        question: str,
        *,
        similarity_threshold: float = 0.0,
        vector_similarity_weight: float = 0.3,
        mode: str = "hybrid",
        use_kg: bool = False,
    ) -> list[dict[str, Any]]:
        """Return shaped candidate chunks, each carrying a private ``_scope`` key."""
        from api.db.services.knowledgebase_service import KnowledgebaseService

        kbs = {kb.id: kb for kb in KnowledgebaseService.get_by_ids([scope["dataset_id"] for scope in scopes])}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for scope in scopes:
            kb = kbs.get(scope["dataset_id"])
            if kb is None:
                continue
            enriched = {**scope, "kb": kb}
            groups[(kb.tenant_id, kb.embd_id)].append(enriched)
        if not groups:
            return []

        chunks: list[dict[str, Any]] = []
        for (tenant_id, embd_id), group in groups.items():
            chunks.extend(
                await self._recall_group(
                    group,
                    tenant_id,
                    embd_id,
                    question,
                    similarity_threshold=similarity_threshold,
                    vector_similarity_weight=vector_similarity_weight,
                    use_kg=use_kg,
                )
            )
        return chunks

    # -- implementation ----------------------------------------------------
    @staticmethod
    def _scope_of(chunk: dict[str, Any], scopes: list[dict[str, Any]]) -> dict[str, Any] | None:
        document_id = chunk.get("doc_id")
        if document_id:
            for scope in scopes:
                if scope["dataset_id"] != chunk.get("kb_id"):
                    continue
                if scope["visible"].get(document_id) is not None:
                    return scope
            return None
        # Knowledge-graph summaries carry no document id: bind them to the scope
        # that produced them.
        tagged = chunk.get("_scope")
        return next((scope for scope in scopes if scope["scope"] == tagged), None)

    async def _recall_group(
        self,
        scopes: list[dict[str, Any]],
        tenant_id: str,
        embd_id: str,
        question: str,
        *,
        similarity_threshold: float,
        vector_similarity_weight: float,
        use_kg: bool,
    ) -> list[dict[str, Any]]:
        kb_ids = [scope["dataset_id"] for scope in scopes]
        idx_names = [search_module.index_name(tenant_id)]
        embd_mdl = await thread_pool_exec(self._embedding_model, tenant_id, embd_id)
        rerank_mdl = await thread_pool_exec(self._rerank_model, tenant_id, settings.RERANK_MDL)
        labels = await thread_pool_exec(label_question, question, [scope["kb"] for scope in scopes])

        strict = await self._dealer.search(
            {
                "kb_ids": kb_ids,
                "doc_ids": None,
                "question": question,
                "page": 1,
                "topk": self.STRICT_POOL,
                "size": self.STRICT_POOL,
                "similarity": similarity_threshold,
                "available_int": 1,
            },
            idx_names,
            kb_ids,
            embd_mdl,
            False,
            rank_feature=labels,
        )
        # Coverage is counted per *scope* and only for hits that are visible
        # under that scope's path. The strict window is not filtered by
        # visibility (it cannot be: one window serves every scope), so counting
        # its raw hits would mark a dataset as reachable even when all of its
        # hits fall outside the requested sub-tree -- leaving that scope with no
        # recovery window and no fallback at all.
        scopes_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for scope in scopes:
            scopes_by_dataset[scope["dataset_id"]].append(scope)
        per_scope: dict[str, int] = defaultdict(int)

        def count_visible(row: dict[str, Any], *, only: dict[str, Any] | None = None) -> None:
            document_id = row.get("doc_id")
            if not document_id:
                return
            candidates = [only] if only is not None else scopes_by_dataset.get(row.get("kb_id"), [])
            for scope in candidates:
                if scope is not None and scope["visible"].get(document_id) is not None:
                    per_scope[scope["scope"]] += 1

        merged: dict[str, dict[str, Any]] = {}
        for chunk_id in strict.ids:
            row = dict(strict.field.get(chunk_id) or {})
            row["id"] = chunk_id
            count_visible(row)
            merged[chunk_id] = row

        # A window shared by every dataset is dominated by the few datasets whose
        # vocabulary matches the question, so datasets outside it must still get
        # their own best candidates -- otherwise the only thing left for them is
        # the arbitrary filter-only fallback below.
        missing = [scope for scope in scopes if per_scope.get(scope["scope"], 0) < self.FALLBACK_TRIGGER]
        quota = self.recovery_quota(len(scopes))
        window = asyncio.Semaphore(self.RECOVERY_CONCURRENCY)

        async def recovery_search(scope: dict[str, Any]):
            async with window:
                return await self._dealer.search(
                    {
                        "kb_ids": [scope["dataset_id"]],
                        "doc_ids": list(scope["visible"]),
                        "question": question,
                        "page": 1,
                        "topk": quota,
                        "size": quota,
                        "similarity": similarity_threshold,
                        "available_int": 1,
                    },
                    idx_names,
                    [scope["dataset_id"]],
                    embd_mdl,
                    False,
                    rank_feature=labels,
                )

        async def fallback_search(scope: dict[str, Any]):
            async with window:
                return await self._dealer.search(
                    {
                        "kb_ids": [scope["dataset_id"]],
                        "doc_ids": list(scope["visible"]),
                        "question": "",
                        "page": 1,
                        "size": quota,
                        "available_int": 1,
                    },
                    idx_names,
                    [scope["dataset_id"]],
                    None,
                )

        if missing:
            strict_again = await asyncio.gather(*(recovery_search(scope) for scope in missing))
            for scope, result in zip(missing, strict_again):
                for chunk_id in result.ids:
                    row = merged.get(chunk_id)
                    if row is None:
                        row = dict(result.field.get(chunk_id) or {})
                        row["id"] = chunk_id
                        merged[chunk_id] = row
                    count_visible(row, only=scope)

        # Datasets the question cannot reach at all keep the historical behaviour:
        # a filter-only window over their visible documents, ranked by the reranker.
        empty = [scope for scope in scopes if per_scope.get(scope["scope"], 0) == 0]
        if empty:
            results = await asyncio.gather(*(fallback_search(scope) for scope in empty))
            for scope, result in zip(empty, results):
                for chunk_id in result.ids:
                    row = merged.get(chunk_id)
                    if row is None:
                        row = dict(result.field.get(chunk_id) or {})
                        row["id"] = chunk_id
                        merged[chunk_id] = row
                    count_visible(row, only=scope)

        if use_kg:
            await self._merge_kg(merged, scopes, tenant_id, question, embd_mdl)

        candidates = [row for row in merged.values() if self._scope_of(row, scopes) is not None]
        if not candidates:
            return []

        sres = self._dealer.SearchResult(
            total=len(candidates),
            ids=[row["id"] for row in candidates],
            query_vector=strict.query_vector,
            field={row["id"]: row for row in candidates},
            highlight={},
            aggregation=[],
            keywords=strict.keywords,
        )

        if rerank_mdl is not None:
            sim, tsim, vsim = await thread_pool_exec(
                self._dealer.rerank_by_model,
                rerank_mdl,
                sres,
                question,
                1 - vector_similarity_weight,
                vector_similarity_weight,
                rank_feature=labels,
            )
        else:
            knn_scores = await self._dealer._knn_scores(sres, idx_names, kb_ids)
            sim, tsim, vsim = await thread_pool_exec(
                self._dealer.rerank_with_knn,
                sres,
                question,
                knn_scores,
                1 - vector_similarity_weight,
                vector_similarity_weight,
                rank_feature=labels,
            )

        post_threshold = 0.0 if vector_similarity_weight <= 0 else similarity_threshold
        chunks: list[dict[str, Any]] = []
        for index, row in enumerate(candidates):
            if float(sim[index]) < post_threshold:
                continue
            scope = self._scope_of(row, scopes)
            chunks.append(self._shape_chunk(row, sim[index], tsim[index], vsim[index], scope))

        if chunks:
            chunks = await thread_pool_exec(self._dealer.retrieval_by_children, chunks, [tenant_id])
            for chunk in chunks:
                if chunk.get("_scope") is None and chunk.get("kb_id"):
                    scope = next((item for item in scopes if item["dataset_id"] == chunk["kb_id"]), None)
                    chunk["_scope"] = scope["scope"] if scope else None
        return chunks

    async def _merge_kg(self, merged, scopes, tenant_id, question, embd_mdl) -> None:
        from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
        from api.db.services.llm_service import LLMBundle

        for scope in scopes:
            try:
                chat_mdl = LLMBundle(tenant_id, get_tenant_default_model_by_type(tenant_id, LLMType.CHAT))
                summary = await settings.kg_retriever.retrieval(question, [tenant_id], [scope["dataset_id"]], embd_mdl, chat_mdl)
                if summary and summary.get("content_with_weight"):
                    summary["id"] = summary.get("chunk_id") or f"kg-{scope['scope']}-{len(merged)}"
                    summary["doc_id"] = ""
                    summary["kb_id"] = summary.get("kb_id") or scope["dataset_id"]
                    summary["_scope"] = scope["scope"]
                    merged[summary["id"]] = summary
            except Exception:
                logger.warning("corpus recall: KG retrieval failed for dataset=%s", scope["dataset_id"], exc_info=True)

    @staticmethod
    def _shape_chunk(row: dict[str, Any], sim: float, tsim: float, vsim: float, scope: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "chunk_id": row["id"],
            "content_ltks": row.get("content_ltks", ""),
            "content_with_weight": row.get("content_with_weight", ""),
            "doc_id": row.get("doc_id", ""),
            "docnm_kwd": row.get("docnm_kwd", ""),
            "kb_id": row.get("kb_id"),
            "important_kwd": row.get("important_kwd", []),
            "tag_kwd": row.get("tag_kwd", []),
            "image_id": row.get("img_id", ""),
            "similarity": float(sim),
            "vector_similarity": float(vsim),
            "term_similarity": float(tsim),
            "positions": row.get("position_int", []),
            "position_int": row.get("position_int", []),
            "page_num_int": row.get("page_num_int", []),
            "top_int": row.get("top_int", []),
            "chunk_order_int": row.get("chunk_order_int"),
            "doc_type_kwd": row.get("doc_type_kwd", ""),
            "mom_id": row.get("mom_id", ""),
            "row_id": row.get("row_id()"),
            "_scope": scope["scope"] if scope else None,
        }
