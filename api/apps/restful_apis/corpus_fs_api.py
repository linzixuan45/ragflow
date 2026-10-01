"""Dataset-scoped, path-only Corpus FS HTTP contract."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from quart import make_response, request

from api.apps import login_required
from api.apps.services.corpus_citation_service import CorpusCitationError, CorpusCitationService
from api.apps.services.corpus_cursor_service import CorpusCursorError, CorpusCursorService, CorpusPageCursorService
from api.apps.services.corpus_delegation_service import CorpusDelegationError, CorpusDelegationService
from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService, CorpusUnavailableError, InvalidConfirmationError, PreconditionRequiredError, is_unavailable_io
from api.apps.services.corpus_path_service import AmbiguousPathError, CorpusPathError
from api.apps.services.corpus_search_diagnostics import collect as collect_search_diagnostics
from api.apps.services.corpus_search_diagnostics import stage as search_stage
from api.apps.services.corpus_secret_service import corpus_secret
from api.apps.services.corpus_tool_service import CorpusToolService
from api.apps.services.keyword_search_service import KeywordSearchService, SearchUnavailableError
from api.db.db_models import Task
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.task_service import TaskService, queue_tasks
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_data_result, get_result
from common.constants import TaskStatus


def _actor_digest(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode()).hexdigest()


def _service(tenant_id: str, dataset_id: str, *, require_delegation: bool = False) -> CorpusFsService:
    actor_ref = tenant_id
    if require_delegation:
        try:
            delegation = CorpusDelegationService.verify(
                request.headers.get("X-RAGFlow-Delegation"),
                tenant_id,
                audience=CorpusDelegationService.corpus_fs_audience,
                purpose="corpus-fs",
            )
        except CorpusDelegationError as exc:
            raise CorpusFsError("delegation is required") from exc
        if dataset_id not in delegation.dataset_ids:
            raise CorpusFsError("dataset is not delegated")
        actor_ref = delegation.actor_id
    return CorpusFsService(tenant_id, dataset_id, _actor_digest(actor_ref), actor_ref=actor_ref)


def _error(error: Exception):
    if is_unavailable_io(error):
        error = CorpusUnavailableError(str(error))
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
    revision = data.get("revision")
    if not isinstance(revision, dict) or not isinstance(revision.get("content_revision"), int):
        revision = service.revision(service._dataset())
        data["revision"] = revision
    response = get_result(data=data)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{revision["content_revision"]}"'
    return response


def _expected_revision() -> int:
    value = request.headers.get("If-Match")
    if value is None:
        raise PreconditionRequiredError("If-Match is required")
    match = re.fullmatch(r'(?:W/)?"?(\d+)"?', value.strip())
    if not match or int(match.group(1)) < 1:
        raise CorpusFsError("If-Match must contain a positive content revision")
    return int(match.group(1))


async def _run_sync_io(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run Corpus FS ORM/storage off the Quart event-loop thread.

    Peewee connections are thread-local. Async routes that `await` and then
    query MySQL on the loop thread share one socket with every other coroutine,
    which collapses into OSError(9) and used to be returned as HTTP 400
    INVALID_ARGUMENT.
    """
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except Exception as exc:
        if is_unavailable_io(exc):
            raise CorpusUnavailableError(str(exc)) from exc
        raise


def _ensure_revision(service: CorpusFsService, payload: dict) -> dict:
    revision = payload.get("revision")
    if not isinstance(revision, dict) or not isinstance(revision.get("content_revision"), int):
        payload["revision"] = service.revision(service._dataset())
    return payload


async def _commit_content_mutation(service: CorpusFsService, mutate: Callable[[], dict]) -> Any:
    expected_revision = _expected_revision()

    def run() -> dict:
        with service.content_mutation(expected_revision):
            payload = mutate()
        return _ensure_revision(service, payload)

    return _success(service, await _run_sync_io(run))


async def _commit_read(service: CorpusFsService, load: Callable[[], dict]) -> Any:
    def run() -> dict:
        return _ensure_revision(service, load())

    return _success(service, await _run_sync_io(run))


async def _file_response(data: bytes, name: str, content_type: str, content_hash: str):
    start, end = 0, len(data) - 1
    status = 200
    range_value = request.headers.get("Range")
    if range_value:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value.strip())
        if not match or not any(match.groups()) or not data:
            response = await make_response(b"", 416)
            response.headers["Content-Range"] = f"bytes */{len(data)}"
            return response
        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else len(data) - 1
        else:
            length = int(last)
            start = max(0, len(data) - length)
        if start >= len(data) or end < start:
            response = await make_response(b"", 416)
            response.headers["Content-Range"] = f"bytes */{len(data)}"
            return response
        end = min(end, len(data) - 1)
        status = 206
    response = await make_response(data[start : end + 1], status)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    response.headers["Content-Length"] = str(end - start + 1 if data else 0)
    response.headers["Cache-Control"] = "private, no-store"
    if content_hash:
        response.headers["ETag"] = f'"{content_hash}"'
    if status == 206:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return response


def _page_cursor_service() -> CorpusPageCursorService:
    return CorpusPageCursorService(corpus_secret("page-cursor"))


def _citation_service() -> CorpusCitationService:
    return CorpusCitationService(corpus_secret("citation"))


def _issue_citation(service: CorpusFsService, document: dict, target: dict) -> str:
    return _citation_service().issue(service.tenant_id, service.dataset_id, document["id"], target)


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


def _delete_secret() -> bytes:
    return corpus_secret("delete-confirmation")


def _delete_snapshot(dataset_id: str, paths: list[str], target_ids: list[str]) -> str:
    value = json.dumps({"dataset": dataset_id, "paths": paths, "targets": target_ids}, separators=(",", ":"), sort_keys=True).encode()
    return hmac.new(_delete_secret(), value, hashlib.sha256).hexdigest()


def _issue_delete_token(service: CorpusFsService, paths: list[str], target_ids: list[str]) -> str:
    payload = json.dumps({"actor": service.actor_digest, "dataset": service.dataset_id, "paths": paths, "snapshot": _delete_snapshot(service.dataset_id, paths, target_ids), "exp": int(time.time()) + 300}, separators=(",", ":"), sort_keys=True).encode()
    return f"{payload.hex()}.{hmac.new(_delete_secret(), payload, hashlib.sha256).hexdigest()}"


def _parse_delete_token(service: CorpusFsService, paths: list[str], token: str) -> dict:
    try:
        encoded, supplied = token.rsplit(".", 1)
        payload = bytes.fromhex(encoded)
        data = json.loads(payload)
        expected = hmac.new(_delete_secret(), payload, hashlib.sha256).hexdigest()
    except Exception as exc:
        raise InvalidConfirmationError("invalid delete confirmation") from exc
    if not hmac.compare_digest(expected, supplied) or data.get("actor") != service.actor_digest or data.get("dataset") != service.dataset_id or data.get("paths") != paths or int(data.get("exp", 0)) < int(time.time()):
        raise InvalidConfirmationError("delete confirmation expired or mismatched")
    return data


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


def _body_paths(payload: dict, name: str = "paths", maximum: int = 100) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise CorpusFsError(f"{name} is invalid")
    if any(not isinstance(path, str) or not path or len(path.encode("utf-8")) > 1024 for path in value):
        raise CorpusFsError(f"{name} is invalid")
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


@manager.route("/datasets/<dataset_id>/fs/entries/folders", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_folder(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"parent_path", "name"})
        service = _service(tenant_id, dataset_id, require_delegation=True)
        parent_path = _body_string(payload, "parent_path", "/", 1024)
        name = _body_string(payload, "name", maximum=256)

        def mutate():
            return service.create_folder(parent_path, name)

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/entries/upload", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def upload(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id, require_delegation=True)
        form = await request.form
        parent_path = form.get("parent_path", "/")
        files = await request.files
        file_objs = files.getlist("file") if "file" in files else []
        if not file_objs or any(not item.filename for item in file_objs):
            raise CorpusFsError("file is required")

        def mutate():
            service.resolve_writable(parent_path, folder=True)
            errors, uploaded = FileService.upload_document(service._dataset(), file_objs, tenant_id)
            if not uploaded:
                raise CorpusFsError("; ".join(errors) if errors else "upload failed")
            document_ids = [document["id"] for document, _ in uploaded]
            service.relocate_uploaded_documents(document_ids, parent_path)
            uploaded_entries = service.uploaded_entries(document_ids)
            for document_id in document_ids:
                DocumentService.filter_update([DocumentService.model.id == document_id], {"run": TaskStatus.RUNNING.value, "progress": 0, "progress_msg": "", "chunk_num": 0, "token_num": 0})
                TaskService.filter_delete([Task.doc_id == document_id])
                found, document = DocumentService.get_by_id(document_id)
                if found:
                    value = document.to_dict()
                    value["tenant_id"] = tenant_id
                    bucket, name = File2DocumentService.get_storage_address(doc_id=document_id)
                    queue_tasks(value, bucket, name, 0)
            return {"items": uploaded_entries, "errors": errors}

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/entries/file", methods=["PUT"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def replace_file(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id, require_delegation=True)
        form = await request.form
        path = form.get("path")
        expected_content_hash = form.get("expected_content_hash")
        files = await request.files
        file_obj = files.get("file") if "file" in files else None
        if not path or not expected_content_hash or file_obj is None:
            raise CorpusFsError("path, expected_content_hash and file are required")
        content = file_obj.read()
        if not content:
            raise CorpusFsError("file is empty")

        def mutate():
            entry = service.replace_original(path, content, expected_content_hash)
            try:
                parse = service.retry_parse(path)
                entry["parse_status"] = parse["status"]
            except Exception as exc:  # noqa: BLE001 - the source save succeeds even if any queue backend fails
                entry["parse_status"] = "FAILED"
                entry["parse_message"] = str(exc)
            return entry

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/entries/move", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def move(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"source_paths", "destination_path", "new_name"})
        source_paths = payload.get("source_paths")
        if not isinstance(source_paths, list) or not source_paths or not all(isinstance(path, str) for path in source_paths):
            raise CorpusFsError("source_paths is invalid")
        service = _service(tenant_id, dataset_id, require_delegation=True)
        destination_path = _body_string(payload, "destination_path", maximum=1024)
        new_name = payload.get("new_name")

        def mutate():
            count = service.move(source_paths, destination_path, new_name)
            return {"success_count": count, "failed_paths": []}

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/entries/delete-preview", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def delete_preview(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"paths"})
        paths = sorted(set(payload.get("paths") or []))
        if not paths or not all(isinstance(path, str) for path in paths):
            raise CorpusFsError("paths is invalid")
        service = _service(tenant_id, dataset_id, require_delegation=True)

        def load():
            target_ids, file_count, folder_count = service.deletion_targets(paths)
            return {
                "file_count": file_count,
                "folder_count": folder_count,
                "confirmation_token": _issue_delete_token(service, paths, target_ids),
            }

        return await _commit_read(service, load)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/entries", methods=["DELETE"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def delete_entries(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"paths", "confirmation_token"})
        paths = sorted(set(payload.get("paths") or []))
        if not paths or not all(isinstance(path, str) for path in paths):
            raise CorpusFsError("paths is invalid")
        service = _service(tenant_id, dataset_id, require_delegation=True)
        token = _parse_delete_token(service, paths, _body_string(payload, "confirmation_token", maximum=4096))

        def mutate():
            target_ids, _, _ = service.deletion_targets(paths)
            if not hmac.compare_digest(str(token.get("snapshot") or ""), _delete_snapshot(dataset_id, paths, target_ids)):
                raise InvalidConfirmationError("delete targets changed after preview")
            return service.delete(paths)

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/blob", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def blob(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        path = request.args.get("path", "")
        entry, data, _ = await _run_sync_io(service.original_data, path)
        return await _file_response(data, entry["name"], entry["mime_type"], entry.get("content_hash", ""))
    except (CorpusFsError, CorpusPathError) as error:
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
        page_value = request.args.get("page")
        page = int(page_value) if page_value is not None else None
        if page is not None and page < 1:
            raise CorpusFsError("page must be positive")
        type_value = request.args.get("type")
        kinds = {value.strip() for value in type_value.split(",") if value.strip()} if type_value else None
        if kinds and not kinds <= {"heading", "paragraph", "table", "image", "image_ocr", "image_caption", "header_footer", "other"}:
            raise CorpusFsError("type is invalid")
        citation_context = service.citation_context(document)
        _, source, images, blocks = service.content_text(
            document,
            page,
            kinds,
            citation_context=citation_context,
        )
        content_revision = service.revision(service._dataset())["content_revision"]
        cursor_service = CorpusCursorService(corpus_secret("content-cursor"))
        cursor = request.args.get("cursor")
        cursor_scope = hashlib.sha256(f"block-pages-v1:{entry['_content_hash']}:{source}:{page}:{','.join(sorted(kinds or []))}".encode()).hexdigest()
        block_index = cursor_service.parse(cursor, tenant_id, dataset_id, document["id"], cursor_scope)["offset"] if cursor else 0
        visible_blocks, next_index = service.content_page(blocks, block_index, maximum)
        next_cursor = cursor_service.issue(tenant_id, dataset_id, document["id"], cursor_scope, next_index) if next_index < len(blocks) else None
        segment = "\n\n".join(block.get("content", "") for block in visible_blocks)
        image_refs = {block.get("image_ref") for block in visible_blocks if block.get("image_ref")}
        visible_images = [image for image in images if image.get("image_ref") in image_refs]
        projection_revision = int(source.rsplit(":", 1)[1]) if source.startswith("source_projection:") else None
        range_start = visible_blocks[0]["start"] if visible_blocks else 0
        range_end = visible_blocks[-1]["end"] if visible_blocks else range_start
        for block in visible_blocks:
            target = service.citation_target(entry, document, content_revision, block=block, context=citation_context)
            block["citation_ref"] = _issue_citation(service, document, target)
        return _success(service, {
            "path": entry["path"],
            "content": segment,
            "content_source": source,
            "ranges": [{"start": range_start, "end": range_end}],
            "blocks": visible_blocks,
            "images": visible_images,
            "citation_ref": visible_blocks[0]["citation_ref"] if visible_blocks else None,
            "next_cursor": next_cursor,
            "projection_revision": projection_revision,
            "block_count": len(blocks),
            "returned_count": len(visible_blocks),
        })
    except CorpusCursorError:
        return _error(CorpusFsError("invalid cursor"))
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/citations/resolve", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def resolve_citation(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"citation_ref"})
        citation_ref = _body_string(payload, "citation_ref", maximum=16384)
        service = _service(tenant_id, dataset_id)

        def load():
            document_id, target = _citation_service().resolve_locator(citation_ref, tenant_id, dataset_id)
            service.authorize_citation(document_id)
            return {"dataset_id": dataset_id, **target}

        return await _commit_read(service, load)
    except CorpusCitationError:
        return _error(CorpusFsError("invalid citation"))
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/citations/preview", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def preview_citation(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"citation_ref"})
        citation_ref = _body_string(payload, "citation_ref", maximum=16384)
        service = _service(tenant_id, dataset_id)

        def load():
            document_id, target = _citation_service().resolve_locator(citation_ref, tenant_id, dataset_id)
            preview = service.citation_preview(document_id, target)
            return {"citation": {"dataset_id": dataset_id, **target}, "preview": preview}

        return await _commit_read(service, load)
    except CorpusCitationError:
        return _error(CorpusFsError("invalid citation"))
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/derived-content", methods=["GET", "PUT"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def derived_content(tenant_id: str, dataset_id: str):
    try:
        if request.method == "GET":
            service = _service(tenant_id, dataset_id)
            path = request.args.get("path", "")
            return await _commit_read(service, lambda: service.markdown_current(path))
        service = _service(tenant_id, dataset_id, require_delegation=True)
        payload = _body(await request.get_json() or {}, {"path", "content", "expected_version"})
        path = _body_string(payload, "path", maximum=1024)
        content_value = payload.get("content")
        if not isinstance(content_value, str):
            raise CorpusFsError("content must be a string")
        expected_version = _int(payload.get("expected_version"), "expected_version", 0, 2147483647)

        def mutate():
            return service.save_markdown(path, content_value, expected_version)

        return await _commit_content_mutation(service, mutate)
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/derived-content/versions", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def derived_content_versions(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        return _success(service, service.markdown_versions(request.args.get("path", "")))
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/derived-content/restore", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def restore_derived_content(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"path", "version", "expected_version"})
        service = _service(tenant_id, dataset_id, require_delegation=True)
        path = _body_string(payload, "path", maximum=1024)
        version = _int(payload.get("version"), "version", 1, 2147483647)
        expected_version = _int(payload.get("expected_version"), "expected_version", 1, 2147483647)

        def mutate():
            return service.restore_markdown(path, version, expected_version)

        return await _commit_content_mutation(service, mutate)
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/derived-content/publish", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def publish_derived_content(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"path", "version", "expected_version"})
        service = _service(tenant_id, dataset_id, require_delegation=True)
        path = _body_string(payload, "path", maximum=1024)
        version = _int(payload.get("version"), "version", 1, 2147483647)
        expected_version = _int(payload.get("expected_version"), "expected_version", 1, 2147483647)

        def mutate():
            return service.publish_markdown(path, version, expected_version)

        return await _commit_content_mutation(service, mutate)
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/derived-content/publish-status", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def derived_content_publish_status(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        path = request.args.get("path", "")
        version = _int(int(request.args.get("version", "")), "version", 1, 2147483647)
        return _success(service, service.markdown_publish_status(path, version))
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/parse-status", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
def parse_status(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        return _success(service, service.parse_state(request.args.get("path", "")))
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/parse", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def mutate_parse(tenant_id: str, dataset_id: str):
    try:
        payload = _body(await request.get_json() or {}, {"action", "paths"})
        action = _body_string(payload, "action", maximum=16)
        paths = _body_paths(payload)
        service = _service(tenant_id, dataset_id, require_delegation=True)

        def mutate():
            return service.parse_documents(paths, action)

        return await _commit_content_mutation(service, mutate)
    except (CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/<dataset_id>/fs/image", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def image(tenant_id: str, dataset_id: str):
    try:
        service = _service(tenant_id, dataset_id)
        path = request.args.get("path", "")
        image_ref = request.args.get("image_ref", "")
        if not image_ref or len(image_ref) != 64:
            raise CorpusFsError("image_ref is invalid")

        def load():
            _, document = service.resolve_document(path)
            return service.image_data(document, image_ref)

        data, object_name = await _run_sync_io(load)
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
        grep_scope = {"query": query, "path": path, "match": match, "exclude": exclude or [], "limit": limit, "highlight": highlight, "count_only": count_only}

        def load():
            offset, page_context = _page_offset(service, "grep", grep_scope, cursor)
            by_document = service.search_scope(path)
            document_ids = list(by_document)
            result = KeywordSearchService().search_authorized(tenant_id, [dataset_id], document_ids, query, match=match, exclude=exclude, offset=offset, limit=limit, highlight=highlight, count_only=count_only)
            documents = {document["id"]: document for document in service._documents()}
            content_revision = page_context["revision"]["content_revision"]
            citation_contexts = {}
            items = []
            for chunk in result["chunks"]:
                entry = by_document.get(chunk.get("doc_id"))
                document = documents.get(chunk.get("doc_id"))
                if entry and document:
                    citation_context = citation_contexts.get(document["id"])
                    if citation_context is None:
                        citation_context = citation_contexts[document["id"]] = service.citation_context(document)
                    target = service.citation_target(entry, document, content_revision, chunk=chunk, context=citation_context)
                    items.append({"path": entry["path"], "snippet": chunk.get("content_with_weight", ""), "citation_ref": _issue_citation(service, document, target), "page": target["page"], "images": service.search_images(document["id"], chunk)})
            page = KeywordSearchService.page_state(result, offset=offset, returned_count=len(items), count_only=count_only)
            return {
                "total": page["total"],
                "returned_count": page["returned_count"],
                "consumed_count": page["consumed_count"],
                "items": items,
                "next_cursor": _next_page_cursor(page_context, page["next_offset"], page["truncated"]),
                "truncated": page["truncated"],
                "revision": page_context["revision"],
            }

        return await _commit_read(service, load)
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
        payload = _body(await request.get_json() or {}, {"query", "path", "top_k", "per_document_limit", "similarity_threshold", "vector_similarity_weight", "mode", "use_kg"})
        query = _body_string(payload, "query")
        path = _body_string(payload, "path", default="/", maximum=1024)
        mode = payload.get("mode", "hybrid")
        if mode not in {"semantic", "hybrid"}:
            raise CorpusFsError("mode must be semantic or hybrid")
        top_k = _int(payload.get("top_k", 10), "top_k", 1, 50)
        per_document_limit = _int(payload.get("per_document_limit", 3), "per_document_limit", 1, 10)
        similarity_threshold = payload.get("similarity_threshold", 0.0)
        vector_similarity_weight = payload.get("vector_similarity_weight", 0.3)
        use_kg = _body_bool(payload, "use_kg", False)
        if isinstance(similarity_threshold, bool) or not isinstance(similarity_threshold, (int, float)) or not 0 <= similarity_threshold <= 1:
            raise CorpusFsError("similarity_threshold is invalid")
        if isinstance(vector_similarity_weight, bool) or not isinstance(vector_similarity_weight, (int, float)) or not 0 <= vector_similarity_weight <= 1:
            raise CorpusFsError("vector_similarity_weight is invalid")
        service = _service(tenant_id, dataset_id)
        result = await CorpusToolService(
            tenant_id,
            service_factory=lambda _dataset_id: service,
        ).search(
            scopes=[{"scope": "dataset", "dataset_id": dataset_id, "path": path}],
            query=query,
            limit=top_k,
            per_document_limit=per_document_limit,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=vector_similarity_weight,
            mode=mode,
            use_kg=use_kg,
        )
        result["items"] = [
            {key: value for key, value in item.items() if key != "scope"}
            for item in result["items"]
        ]
        return await _commit_read(service, lambda: result)
    except (ValueError, CorpusFsError, CorpusPathError) as error:
        return _error(error)


@manager.route("/datasets/corpus-search", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def corpus_search(tenant_id: str):
    with collect_search_diagnostics() as diagnostics:
        started = time.perf_counter()
        try:
            with search_stage("handler.body"):
                payload = _body(
                    await request.get_json() or {},
                    {
                        "scopes",
                        "query",
                        "limit",
                        "per_document_limit",
                        "similarity_threshold",
                        "vector_similarity_weight",
                        "mode",
                        "use_kg",
                    },
                )
            scopes = payload.get("scopes")
            actor_ref = tenant_id
            delegation_token = request.headers.get("X-RAGFlow-Delegation")
            if delegation_token:
                delegation = CorpusDelegationService.verify(
                    delegation_token,
                    tenant_id,
                    audience=CorpusDelegationService.corpus_fs_audience,
                    purpose="corpus-fs",
                )
                requested_dataset_ids = {
                    scope.get("dataset_id")
                    for scope in scopes
                    if isinstance(scope, dict)
                } if isinstance(scopes, list) else set()
                if not requested_dataset_ids or not requested_dataset_ids.issubset(delegation.dataset_ids):
                    raise CorpusDelegationError("dataset is not delegated")
                actor_ref = delegation.actor_id
            with search_stage("handler.build"):
                tool = CorpusToolService(tenant_id, actor_ref)
            with search_stage("handler.search"):
                result = await tool.search(
                    scopes=scopes,
                    query=payload.get("query"),
                    limit=payload.get("limit", 10),
                    per_document_limit=payload.get("per_document_limit", 3),
                    similarity_threshold=payload.get("similarity_threshold", 0.0),
                    vector_similarity_weight=payload.get("vector_similarity_weight", 0.3),
                    mode=payload.get("mode", "hybrid"),
                    use_kg=_body_bool(payload, "use_kg", False),
                )
            if diagnostics is not None:
                # Recorded explicitly rather than with a `stage` context manager:
                # the region has to be closed *before* the snapshot is serialised
                # into the response, and `wall - handler.total` is exactly the
                # pre-handler work (token auth) plus serialisation and transfer.
                diagnostics.record("handler.total", time.perf_counter() - started)
                result["search_meta"]["diagnostics"] = diagnostics.snapshot()
            return get_result(data=result)
        except CorpusDelegationError:
            response = get_error_data_result(message="NOT_FOUND")
            response.status_code = 404
            return response
        except (ValueError, CorpusFsError, CorpusPathError) as error:
            return _error(error)
