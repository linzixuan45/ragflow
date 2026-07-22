"""Read-only Corpus FS HTTP contract."""

from __future__ import annotations

import hashlib
import mimetypes
import os

from quart import make_response, request

from api.apps import login_required
from api.apps.services.corpus_cursor_service import CorpusCursorError, CorpusCursorService, CorpusPageCursorService
from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService
from api.apps.services.corpus_path_service import AmbiguousPathError, CorpusPathError
from api.apps.services.keyword_search_service import KeywordSearchService, SearchUnavailableError
from api.apps.services import dataset_api_service
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_data_result, get_result


def _actor_digest(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode()).hexdigest()


def _service(tenant_id: str, dataset_id: str) -> CorpusFsService:
    return CorpusFsService(tenant_id, dataset_id, _actor_digest(tenant_id))


def _error(error: Exception):
    if isinstance(error, AmbiguousPathError):
        response = get_error_data_result(message="AMBIGUOUS_PATH")
        response.status_code = 409
        return response
    if isinstance(error, CorpusFsError):
        response = get_error_data_result(message=error.code)
        response.status_code = error.status
        return response
    response = get_error_data_result(message="INVALID_ARGUMENT")
    response.status_code = 400
    return response


def _success(service: CorpusFsService, data: dict):
    data["revision"] = service.revision(service._dataset())
    response = get_result(data=data)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _page_cursor_service() -> CorpusPageCursorService:
    return CorpusPageCursorService(os.environ.get("RAGFLOW_CORPUS_CURSOR_SECRET", "ragflow-corpus-fs-development-secret").encode())


def _page_context(service: CorpusFsService, operation: str, scope: dict) -> dict:
    return {
        "actor": service.actor_digest,
        "dataset": service.dataset_id,
        "operation": operation,
        "revision": service.revision(service._dataset()),
        "scope": scope,
    }


def _page_offset(service: CorpusFsService, operation: str, scope: dict, cursor: str | None) -> tuple[int, dict]:
    context = _page_context(service, operation, scope)
    return (_page_cursor_service().parse(cursor, context) if cursor else 0, context)


def _next_page_cursor(context: dict, offset: int, more: bool) -> str | None:
    return _page_cursor_service().issue(context, offset) if more else None


def _image_content_type(object_name: str, data: bytes) -> str:
    guessed = mimetypes.guess_type(object_name)[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _query_bool(name: str, default: bool) -> bool:
    value = request.args.get(name)
    if value is None:
        return default
    if value == "true":
        return True
    if value == "false":
        return False
    raise CorpusFsError(f"{name} must be true or false")


def _int(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CorpusFsError(f"{name} is invalid")
    return value


def _body(payload: object, allowed: set[str]) -> dict:
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise CorpusFsError("invalid request body")
    return payload


def _body_string(payload: dict, name: str, default: str | None = None, maximum: int = 4096) -> str:
    value = payload.get(name, default)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise CorpusFsError(f"{name} is invalid")
    return value


def _body_bool(payload: dict, name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise CorpusFsError(f"{name} must be boolean")
    return value


@manager.route("/datasets/<dataset_id>/fs/tree", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def tree(tenant_id: str, dataset_id: str):
    try:
        limit = int(request.args.get("limit", 50))
        service = _service(tenant_id, dataset_id)
        path = request.args.get("path", "/")
        offset, context = _page_offset(service, "tree", {"path": path, "limit": limit}, request.args.get("cursor"))
        items, more = service.tree(path, limit, offset)
        return _success(service, {"items": items, "next_cursor": _next_page_cursor(context, offset + len(items), more)})
    except (ValueError, CorpusFsError, CorpusPathError, CorpusCursorError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/find", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def find(tenant_id: str, dataset_id: str):
    try:
        limit = int(request.args.get("limit", 50))
        service = _service(tenant_id, dataset_id)
        name = request.args.get("name", "")
        path = request.args.get("path", "/")
        match = request.args.get("match", "contains")
        recursive = _query_bool("recursive", True)
        offset, context = _page_offset(service, "find", {"name": name, "path": path, "limit": limit, "match": match, "recursive": recursive}, request.args.get("cursor"))
        items, more = service.find(name, path, limit, offset, recursive=recursive, match=match)
        return _success(service, {"items": items, "next_cursor": _next_page_cursor(context, offset + len(items), more)})
    except (ValueError, CorpusFsError, CorpusPathError, CorpusCursorError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/stat", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def stat(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        return _success(service, service.stat(request.args.get("path", "/")))
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/usage", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def usage(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        return _success(service, service.usage(request.args.get("path", "/"), int(request.args.get("depth", 0))))
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/content", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def content(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        entry, document = service.resolve_document(request.args.get("path", ""))
        maximum = int(request.args.get("max_bytes", 32768))
        if maximum < 1 or maximum > 262144:
            raise CorpusFsError("max_bytes must be between 1 and 262144")
        text, source, images, blocks = service.content_text(document)
        cursor_service = CorpusCursorService(os.environ.get("RAGFLOW_CORPUS_CURSOR_SECRET", "ragflow-corpus-fs-development-secret").encode())
        cursor = request.args.get("cursor")
        offset = cursor_service.parse(cursor, tenant_id, dataset_id, document["id"], entry["_content_hash"]) ["offset"] if cursor else 0
        encoded = text.encode("utf-8")
        segment = encoded[offset : offset + maximum].decode("utf-8", errors="ignore")
        next_offset = offset + len(segment.encode("utf-8"))
        next_cursor = cursor_service.issue(tenant_id, dataset_id, document["id"], entry["_content_hash"], next_offset) if next_offset < len(encoded) else None
        visible_images = [image for image in images if offset <= image["offset"] <= next_offset]
        visible_blocks = []
        for block in blocks:
            if block["end"] <= offset or block["start"] >= next_offset:
                continue
            block_bytes = block["content"].encode("utf-8")
            start = max(offset - block["start"], 0)
            end = min(next_offset - block["start"], len(block_bytes))
            item = {"content": block_bytes[start:end].decode("utf-8", errors="ignore")}
            if block.get("image_ref") and block["end"] <= next_offset:
                item["image_ref"] = block["image_ref"]
            visible_blocks.append(item)
        return _success(service, {"path": entry["path"], "content": segment, "content_source": source, "ranges": [{"start": offset, "end": next_offset}], "blocks": visible_blocks, "images": visible_images, "citation_ref": cursor_service.issue(tenant_id, dataset_id, document["id"], entry["_content_hash"], offset), "next_cursor": next_cursor})
    except CorpusCursorError:
        return _error(CorpusFsError("invalid cursor"))
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/image", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def image(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        _, document = service.resolve_document(request.args.get("path", ""))
        image_ref = request.args.get("image_ref", "")
        if not image_ref or len(image_ref) != 64:
            raise CorpusFsError("image_ref is invalid")
        data, object_name = service.image_data(document, image_ref)
        response = await make_response(data)
        response.headers["Content-Type"] = _image_content_type(object_name, data)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/grep", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def grep(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"query", "path", "match", "exclude", "limit", "cursor", "highlight", "count_only"})
        query = _body_string(payload, "query")
        path = _body_string(payload, "path", default="/", maximum=1024)
        match = payload.get("match", "any")
        if match not in {"any", "all", "phrase"}:
            raise CorpusFsError("match is invalid")
        exclude = payload.get("exclude")
        if exclude is not None and (not isinstance(exclude, list) or len(exclude) > 20 or not all(isinstance(item, str) and item and len(item.encode("utf-8")) <= 256 for item in exclude)):
            raise CorpusFsError("exclude is invalid")
        limit = _int(payload.get("limit", 20), "limit", 1, 100)
        highlight = _body_bool(payload, "highlight", True)
        count_only = _body_bool(payload, "count_only", False)
        cursor = payload.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise CorpusFsError("cursor is invalid")
        service = _service(tenant_id, dataset_id)
        offset, context = _page_offset(service, "grep", {"query": query, "path": path, "match": match, "exclude": exclude or [], "limit": limit, "highlight": highlight, "count_only": count_only}, cursor)
        if path == "/":
            document_ids = [document["id"] for document in service._documents()]
        else:
            entries, _ = service._projection()
            prefix = path.rstrip("/") + "/"
            document_ids = [entry["_document_id"] for entry_path, entry in entries.items() if entry["kind"] == "file" and (entry_path == path or entry_path.startswith(prefix))]
        result = KeywordSearchService().search_authorized(tenant_id, [dataset_id], document_ids, query, match=match, exclude=exclude, offset=offset, limit=limit, highlight=highlight, count_only=count_only)
        entries, _ = service._projection()
        by_document = {entry["_document_id"]: entry for entry in entries.values() if entry["kind"] == "file"}
        items = []
        for chunk in result["chunks"]:
            entry = by_document.get(chunk.get("doc_id"))
            if entry:
                items.append({"path": entry["path"], "snippet": chunk.get("content_with_weight", ""), "citation_ref": hashlib.sha256(f"{dataset_id}:{entry['path']}:{chunk.get('id', '')}".encode()).hexdigest(), "page": chunk.get("page_num_int")})
        more = not count_only and offset + len(items) < result["total"]
        return _success(service, {"total": result["total"], "items": items, "next_cursor": _next_page_cursor(context, offset + len(items), more), "truncated": more})
    except SearchUnavailableError:
        response = get_error_data_result(message="SEARCH_UNAVAILABLE")
        response.status_code = 503
        return response
    except (ValueError, CorpusFsError, CorpusPathError, CorpusCursorError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/search", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def semantic_search(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"query", "path", "top_k", "similarity_threshold", "vector_similarity_weight", "mode"})
        query = _body_string(payload, "query")
        path = _body_string(payload, "path", default="/", maximum=1024)
        mode = payload.get("mode", "hybrid")
        if mode not in {"semantic", "hybrid"}:
            raise CorpusFsError("mode must be semantic or hybrid")
        top_k = _int(payload.get("top_k", 10), "top_k", 1, 50)
        similarity_threshold = payload.get("similarity_threshold", 0.0)
        vector_similarity_weight = payload.get("vector_similarity_weight", 0.3)
        if isinstance(similarity_threshold, bool) or not isinstance(similarity_threshold, (int, float)) or not 0 <= similarity_threshold <= 1:
            raise CorpusFsError("similarity_threshold is invalid")
        if isinstance(vector_similarity_weight, bool) or not isinstance(vector_similarity_weight, (int, float)) or not 0 <= vector_similarity_weight <= 1:
            raise CorpusFsError("vector_similarity_weight is invalid")
        service = _service(tenant_id, dataset_id)
        entries, _ = service._projection()
        prefix = path.rstrip("/") + "/"
        doc_ids = [entry["_document_id"] for entry_path, entry in entries.items() if entry["kind"] == "file" and (path == "/" or entry_path == path or entry_path.startswith(prefix))]
        request_payload = {"question": query, "doc_ids": doc_ids, "size": top_k, "top_k": top_k, "similarity_threshold": similarity_threshold, "vector_similarity_weight": vector_similarity_weight}
        ok, result = await dataset_api_service.search(dataset_id, tenant_id, request_payload)
        if not ok:
            raise NotImplementedError(result)
        by_document = {entry["_document_id"]: entry for entry in entries.values() if entry["kind"] == "file"}
        items = [{"path": by_document[chunk["doc_id"]]["path"], "snippet": chunk.get("content_with_weight", ""), "citation_ref": hashlib.sha256(f"{dataset_id}:{chunk.get('doc_id')}:{chunk.get('id', '')}".encode()).hexdigest()} for chunk in result.get("chunks", []) if chunk.get("doc_id") in by_document]
        return _success(service, {"items": items, "total": len(items)})
    except NotImplementedError:
        response = get_error_data_result(message="SEARCH_UNAVAILABLE")
        response.status_code = 503
        return response
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)
