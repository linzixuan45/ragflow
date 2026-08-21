"""Model-oriented, path-only tools over one or more Corpus FS datasets."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable
from typing import Any

from peewee import InterfaceError, OperationalError

from api.apps.services import dataset_api_service
from api.apps.services.corpus_citation_service import CorpusCitationService
from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService
from api.apps.services.corpus_secret_service import corpus_secret

CORPUS_EVIDENCE_SCHEMA = "ragflow.corpus.evidence.v1"
_MAX_SCOPES = 50
_MAX_CANDIDATES_PER_SCOPE = 50


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


class CorpusToolService:
    """Own generic Corpus evidence shaping independently of any Agent runtime."""

    def __init__(
        self,
        tenant_id: str,
        actor_ref: str | None = None,
        *,
        service_factory: Callable[[str], CorpusFsService] | None = None,
        searcher: Callable[..., Any] | None = None,
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
        self._searcher = searcher or dataset_api_service.search
        self._citation_service = citation_service or CorpusCitationService(corpus_secret("citation"))

    @staticmethod
    async def _run_sync_io(operation: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(operation, *args)
        except (InterfaceError, OperationalError, OSError) as exc:
            raise CorpusSearchUnavailableError(str(exc)) from exc

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
    ) -> dict[str, Any]:
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

        async def search_scope(context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            document_ids = list(context["by_document"])
            if not document_ids:
                return context, []
            candidate_limit = min(_MAX_CANDIDATES_PER_SCOPE, max(limit * 4, limit))
            ok, result = await self._searcher(
                context["dataset_id"],
                self.tenant_id,
                {
                    "question": query,
                    "doc_ids": document_ids,
                    "page": 1,
                    "size": candidate_limit,
                    "top_k": _MAX_CANDIDATES_PER_SCOPE,
                    "similarity_threshold": similarity_threshold,
                    "vector_similarity_weight": context["service"].search_vector_weight(mode, vector_similarity_weight),
                },
            )
            if not ok:
                raise CorpusSearchUnavailableError(str(result))
            return context, list(result.get("chunks") or [])

        scope_results = await asyncio.gather(*(search_scope(context) for context in contexts))
        ranked: list[tuple[dict[str, Any], float | None, int, int, str]] = []
        citation_contexts: dict[tuple[str, str], Any] = {}
        for context, chunks in scope_results:
            service = context["service"]
            content_revision = context["content_revision"]
            for source_rank, chunk in enumerate(chunks):
                document_id = chunk.get("doc_id")
                entry = context["by_document"].get(document_id)
                document = context["documents"].get(document_id)
                if not entry or not document:
                    continue
                citation_key = (context["dataset_id"], document_id)
                citation_context = citation_contexts.get(citation_key)
                if citation_context is None:
                    citation_context = citation_contexts[citation_key] = await self._run_sync_io(
                        service.citation_context,
                        document,
                    )
                target = service.citation_target(
                    entry,
                    document,
                    content_revision,
                    chunk=chunk,
                    context=citation_context,
                )
                score = _finite_float(chunk.get("score"))
                if score is None:
                    score = _finite_float(chunk.get("similarity"))
                evidence = {
                    "scope": context["scope"],
                    "path": entry["path"],
                    "snippet": str(chunk.get("content_with_weight") or chunk.get("snippet") or ""),
                    "citation_ref": self._citation_service.issue(
                        self.tenant_id,
                        context["dataset_id"],
                        document_id,
                        target,
                    ),
                    "page": target.get("page") if isinstance(target, dict) else _page(chunk),
                    "positions": chunk.get("position_int") or chunk.get("positions") or [],
                    "score": score,
                    "vector_similarity": _finite_float(chunk.get("vector_similarity")),
                    "term_similarity": _finite_float(chunk.get("term_similarity")),
                    "images": service.search_images(document_id, chunk),
                }
                ranked.append((evidence, score, context["scope_index"], source_rank, document_id))

        ranked.sort(
            key=lambda item: (
                item[1] is None,
                -item[1] if item[1] is not None else item[3],
                item[3],
                item[2],
            )
        )
        deduplicated: list[tuple[dict[str, Any], str]] = []
        seen: set[tuple[str, str, int | None, str]] = set()
        for evidence, _score, _scope_index, _source_rank, document_id in ranked:
            snippet_hash = hashlib.sha256(" ".join(evidence["snippet"].split()).encode()).hexdigest()[:16]
            key = (evidence["scope"], evidence["path"], evidence["page"], snippet_hash)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append((evidence, document_id))

        document_limited: list[dict[str, Any]] = []
        document_counts: dict[tuple[str, str], int] = {}
        for evidence, document_id in deduplicated:
            key = (evidence["scope"], document_id)
            count = document_counts.get(key, 0)
            if count >= per_document_limit:
                continue
            document_counts[key] = count + 1
            document_limited.append(evidence)

        items = document_limited[:limit]
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
            },
        }
