"""Model-oriented, path-only tools over one or more Corpus FS datasets."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable
from typing import Any

from api.apps.services.corpus_citation_service import CorpusCitationService
from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService, is_unavailable_io
from api.apps.services.corpus_recall_service import CorpusRecallService
from api.apps.services.corpus_search_diagnostics import stage
from api.apps.services.corpus_secret_service import corpus_secret

CORPUS_EVIDENCE_SCHEMA = "ragflow.corpus.evidence.v1"
_MAX_SCOPES = 50


class CorpusSearchUnavailableError(CorpusFsError):
    code = "SEARCH_UNAVAILABLE"
    status = 503


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _page(chunk: dict[str, Any]) -> int | None:
    value = chunk.get("page")
    if value is None:
        pages = chunk.get("page_num_int")
        value = pages[0] if isinstance(pages, list) and pages else pages
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chunk_document_ids(chunk: dict[str, Any], by_document: dict[str, Any], *, use_kg: bool) -> list[str]:
    candidates: list[str] = []
    doc_id = chunk.get("doc_id")
    if isinstance(doc_id, str) and doc_id:
        candidates.append(doc_id)
    source_id = chunk.get("source_id")
    if isinstance(source_id, list):
        candidates.extend(str(item) for item in source_id if item)
    elif isinstance(source_id, str) and source_id:
        candidates.append(source_id)
    mapped = [item for item in candidates if item in by_document]
    if mapped:
        return [mapped[0]]
    # KGSearch returns a synthetic chunk with an empty doc_id. Bind it to the
    # first in-scope document so the graph summary still becomes a path /
    # citation_ref hit instead of leaking upstream ids or being dropped.
    if use_kg and not candidates and by_document and chunk.get("content_with_weight"):
        return [next(iter(by_document))]
    return []


class CorpusToolService:
    """Own generic Corpus evidence shaping independently of any Agent runtime."""

    def __init__(
        self,
        tenant_id: str,
        actor_ref: str | None = None,
        *,
        service_factory: Callable[[str], CorpusFsService] | None = None,
        recall: Any | None = None,
        citation_service: CorpusCitationService | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.actor_ref = actor_ref or tenant_id
        actor_digest = hashlib.sha256(self.actor_ref.encode()).hexdigest()
        self._service_factory = service_factory or (
            lambda dataset_id: CorpusFsService(
                tenant_id,
                dataset_id,
                actor_digest,
                actor_ref=self.actor_ref,
            )
        )
        self._recall = recall or CorpusRecallService(tenant_id)
        self._citation_service = citation_service or CorpusCitationService(corpus_secret("citation"))

    @staticmethod
    async def _run_sync_io(operation: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except Exception as exc:
            if is_unavailable_io(exc):
                raise CorpusSearchUnavailableError(str(exc)) from exc
            raise

    @staticmethod
    def _normalize_scopes(scopes: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= _MAX_SCOPES:
            raise CorpusFsError("scopes must be a non-empty list")
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in scopes:
            if not isinstance(raw, dict) or set(raw) - {"scope", "dataset_id", "path"}:
                raise CorpusFsError("invalid corpus scope")
            scope = raw.get("scope")
            dataset_id = raw.get("dataset_id")
            path = raw.get("path", "/")
            if (
                not isinstance(scope, str)
                or not scope
                or len(scope.encode()) > 256
                or scope in seen
                or not isinstance(dataset_id, str)
                or not dataset_id
                or not isinstance(path, str)
                or not path.startswith("/")
            ):
                raise CorpusFsError("invalid corpus scope")
            seen.add(scope)
            normalized.append({"scope": scope, "dataset_id": dataset_id, "path": path})
        return normalized

    async def search(
        self,
        *,
        scopes: list[dict[str, Any]],
        query: str,
        limit: int = 10,
        per_document_limit: int = 3,
        similarity_threshold: float = 0.0,
        vector_similarity_weight: float = 0.3,
        mode: str = "hybrid",
        use_kg: bool = False,
    ) -> dict[str, Any]:
        with stage("normalize"):
            normalized_scopes = self._normalize_scopes(scopes)
        if not isinstance(query, str) or not query or len(query.encode()) > 4096:
            raise CorpusFsError("query is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise CorpusFsError("limit is invalid")
        if isinstance(per_document_limit, bool) or not isinstance(per_document_limit, int) or not 1 <= per_document_limit <= 10:
            raise CorpusFsError("per_document_limit is invalid")
        if mode not in {"semantic", "hybrid"}:
            raise CorpusFsError("mode must be semantic or hybrid")
        if isinstance(similarity_threshold, bool) or not isinstance(similarity_threshold, (int, float)) or not 0 <= similarity_threshold <= 1:
            raise CorpusFsError("similarity_threshold is invalid")
        if isinstance(vector_similarity_weight, bool) or not isinstance(vector_similarity_weight, (int, float)) or not 0 <= vector_similarity_weight <= 1:
            raise CorpusFsError("vector_similarity_weight is invalid")

        contexts: list[dict[str, Any]] = []
        with stage("context"):
            for scope_index, scope in enumerate(normalized_scopes):
                service = self._service_factory(scope["dataset_id"])
                by_document, documents, content_revision = await self._run_sync_io(
                    service.search_context,
                    scope["path"],
                )
                contexts.append(
                    {
                        **scope,
                        "scope_index": scope_index,
                        "service": service,
                        "by_document": by_document,
                        "documents": documents,
                        "content_revision": content_revision,
                    }
                )

        # One fused recall across every scope: a single query embedding, a single
        # strict window, a per-scope filter-only fallback only where the strict
        # query cannot reach the dataset, and a single rerank over the merged pool.
        effective_weight = CorpusFsService.search_vector_weight(mode, vector_similarity_weight)
        with stage("recall"):
            chunks = await self._recall.recall(
                scopes=[
                    {
                        "scope": context["scope"],
                        "dataset_id": context["dataset_id"],
                        "path": context["path"],
                        "visible": context["by_document"],
                    }
                    for context in contexts
                ],
                question=query,
                similarity_threshold=similarity_threshold,
                vector_similarity_weight=effective_weight,
                mode=mode,
                use_kg=bool(use_kg),
            )
        contexts_by_scope = {context["scope"]: context for context in contexts}

        # Ranking and de-duplication need a page number, not a citation. Reading the
        # source object (whole-file GET + sha256) to produce one is what made a
        # 16-dataset search spend 3.5s on candidate documents that were then
        # discarded, so the expensive half is deferred to the surviving items.
        # The page used here is exactly the page ``citation_target`` derives before
        # projection blocks refine it, so the ranking keys are unchanged for every
        # document type whose citation context carries no blocks (all non-PDFs);
        # PDF projections are still resolved eagerly because they can move the page.
        ranked: list[tuple[dict[str, Any], float | None, int, int, str, dict[str, Any]]] = []
        citation_contexts: dict[tuple[str, str], Any] = {}
        for source_rank, chunk in enumerate(chunks):
            context = contexts_by_scope.get(chunk.get("_scope"))
            if context is None:
                continue
            service = context["service"]
            content_revision = context["content_revision"]
            document_ids = _chunk_document_ids(chunk, context["by_document"], use_kg=bool(use_kg))
            if not document_ids:
                continue
            document_id = document_ids[0]
            entry = context["by_document"].get(document_id)
            document = context["documents"].get(document_id)
            if not entry or not document:
                continue
            citation_key = (context["dataset_id"], document_id)
            page = service.chunk_page(chunk)
            if (document.get("type") or "").lower() == "pdf":
                citation_context = citation_contexts.get(citation_key)
                if citation_context is None:
                    with stage("citation.context"):
                        citation_context = citation_contexts[citation_key] = await self._run_sync_io(
                            service.citation_context,
                            document,
                        )
                with stage("citation.target"):
                    target = service.citation_target(
                        entry,
                        document,
                        content_revision,
                        chunk=chunk,
                        context=citation_context,
                    )
                page = target.get("page") if isinstance(target, dict) else page
            score = _finite_float(chunk.get("score"))
            if score is None:
                score = _finite_float(chunk.get("similarity"))
            with stage("citation.images"):
                images = service.search_images(document_id, chunk)
            evidence = {
                "scope": context["scope"],
                "path": entry["path"],
                "snippet": str(chunk.get("content_with_weight") or chunk.get("snippet") or ""),
                "citation_ref": None,
                "page": page,
                "positions": chunk.get("position_int") or chunk.get("positions") or [],
                "score": score,
                "vector_similarity": _finite_float(chunk.get("vector_similarity")),
                "term_similarity": _finite_float(chunk.get("term_similarity")),
                "images": images,
            }
            ranked.append(
                (
                    evidence,
                    score,
                    context["scope_index"],
                    source_rank,
                    document_id,
                    {"chunk": chunk, "context": context, "entry": entry, "document": document, "document_id": document_id},
                )
            )

        ranked.sort(
            key=lambda item: (
                item[1] is None,
                -item[1] if item[1] is not None else item[3],
                item[3],
                item[2],
            )
        )
        deduplicated: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        seen: set[tuple[str, str, int | None, str]] = set()
        for evidence, _score, _scope_index, _source_rank, document_id, source in ranked:
            snippet_hash = hashlib.sha256(" ".join(evidence["snippet"].split()).encode()).hexdigest()[:16]
            key = (evidence["scope"], evidence["path"], evidence["page"], snippet_hash)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append((evidence, document_id, source))

        document_limited: list[dict[str, Any]] = []
        document_limited_sources: list[dict[str, Any]] = []
        document_counts: dict[tuple[str, str], int] = {}
        for evidence, document_id, source in deduplicated:
            key = (evidence["scope"], document_id)
            count = document_counts.get(key, 0)
            if count >= per_document_limit:
                continue
            document_counts[key] = count + 1
            document_limited.append(evidence)
            document_limited_sources.append(source)

        # Expose a stable, reproducible global rank (1-based) so downstream
        # evaluation can compute retrieval metrics (Hit@K / MRR / Recall@K)
        # without re-deriving order from scores. Rank follows the dedup + per-
        # document-limit pipeline order, which is deterministic for identical data
        # revision and search parameters.
        for rank, evidence in enumerate(document_limited, start=1):
            evidence["rank"] = rank

        items = document_limited[:limit]
        with stage("citation"):
            await self._attach_citations(items, document_limited_sources[:limit], citation_contexts)
        return {
            "schema": CORPUS_EVIDENCE_SCHEMA,
            "items": items,
            "total": len(document_limited),
            "truncated": len(document_limited) > len(items),
            "continuation": {"mode": "refine"} if len(document_limited) > len(items) else {"mode": "terminal"},
            "search_meta": {
                "scope_count": len(normalized_scopes),
                "raw_candidate_count": len(ranked),
                "deduplicated_count": len(deduplicated),
                "document_limited_count": len(document_limited),
                "returned_count": len(items),
                "ranking_mode": "score_then_source_rank",
                "search_params": {
                    "mode": mode,
                    "limit": limit,
                    "per_document_limit": per_document_limit,
                    "similarity_threshold": similarity_threshold,
                    "vector_similarity_weight": vector_similarity_weight,
                    "use_kg": bool(use_kg),
                },
                "content_revisions": {
                    str(context["scope"]): context["content_revision"]
                    for context in contexts
                },
            },
        }

    async def _attach_citations(
        self,
        items: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        citation_contexts: dict[tuple[str, str], Any],
    ) -> None:
        """Resolve citation_ref/page for the returned items only.

        ``ranked`` is built with the page each chunk states about itself, so the
        source object is read here -- for at most ``limit`` documents instead of
        every candidate.
        """
        for item, source in zip(items, sources):
            context = source["context"]
            document = source["document"]
            document_id = source["document_id"]
            citation_key = (context["dataset_id"], document_id)
            citation_context = citation_contexts.get(citation_key)
            if citation_context is None:
                with stage("citation.context"):
                    citation_context = citation_contexts[citation_key] = await self._run_sync_io(
                        context["service"].citation_context,
                        document,
                    )
            with stage("citation.target"):
                target = context["service"].citation_target(
                    source["entry"],
                    document,
                    context["content_revision"],
                    chunk=source["chunk"],
                    context=citation_context,
                )
            with stage("citation.issue"):
                item["citation_ref"] = self._citation_service.issue(
                    self.tenant_id,
                    context["dataset_id"],
                    document_id,
                    target,
                )
            if isinstance(target, dict) and target.get("page") is not None:
                item["page"] = target["page"]
