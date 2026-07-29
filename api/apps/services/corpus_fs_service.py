"""Dataset-isolated Corpus FS projections and path-only mutations."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
from collections import defaultdict
from contextlib import contextmanager
from typing import Any

from peewee import OperationalError, ProgrammingError

from api.apps.services.corpus_path_service import AmbiguousPathError, CorpusPathRegistry, CorpusPathResolver
from api.apps.services.document_markdown_service import DocumentMarkdownService, MarkdownVersionConflict, MarkdownVersionNotFound
from api.apps.services.corpus_secret_service import corpus_secret
from api.apps.services.source_projection_service import SourceProjectionService
from api.common.check_team_permission import check_file_team_permission, check_kb_team_permission
from api.db import FileType
from api.db.db_models import DB, Document, File2Document, Knowledgebase, Task
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, cancel_all_task_of
from common import settings
from common.constants import TaskStatus
from common.doc_store.doc_store_base import OrderByExpr
from common.misc_utils import get_uuid
from rag.nlp import search


class CorpusFsError(ValueError):
    code = "INVALID_ARGUMENT"
    status = 400


class NotFoundError(CorpusFsError):
    code = "NOT_FOUND"
    status = 404


class DocumentNotReadyError(CorpusFsError):
    code = "DOCUMENT_NOT_READY"
    status = 409


class UnsupportedContentError(CorpusFsError):
    code = "UNSUPPORTED_CONTENT"
    status = 415


class ImageNotFoundError(CorpusFsError):
    code = "IMAGE_NOT_FOUND"
    status = 404


class MoveCycleError(CorpusFsError):
    code = "MOVE_CYCLE"
    status = 422


class EntryConflictError(CorpusFsError):
    code = "ENTRY_CONFLICT"
    status = 409


class InvalidConfirmationError(CorpusFsError):
    code = "INVALID_CONFIRMATION"
    status = 409


class RevisionConflictError(CorpusFsError):
    code = "REVISION_CONFLICT"
    status = 409


class PreconditionRequiredError(CorpusFsError):
    code = "PRECONDITION_REQUIRED"
    status = 428


class CorpusFsService:
    registry = CorpusPathRegistry()

    def __init__(self, tenant_id: str, dataset_id: str, actor_digest: str):
        self.tenant_id = tenant_id
        self.dataset_id = dataset_id
        self.actor_digest = actor_digest

    def _dataset(self):
        ok, dataset = KnowledgebaseService.get_by_id(self.dataset_id)
        if not ok or dataset.status != "1" or not check_kb_team_permission(dataset, self.tenant_id):
            raise NotFoundError("dataset not found")
        return dataset

    def _documents(self) -> list[dict[str, Any]]:
        self._dataset()
        return list(Document.select().where((Document.kb_id == self.dataset_id) & (Document.status == "1")).dicts())

    def _dataset_file_root(self) -> dict:
        dataset = self._dataset()
        tenant_root = FileService._find_root_folder(self.tenant_id)
        kb_root = FileService._find_kb_folder(self.tenant_id, tenant_root)
        if tenant_root is None or kb_root is None:
            raise NotFoundError("dataset file hierarchy is not initialized")
        document_ids = [document["id"] for document in self._documents()]
        links = list(File2Document.select().where(File2Document.document_id.in_(document_ids)).dicts()) if document_ids else []
        root = None
        for link in links:
            ok, current = FileService.get_by_id(link["file_id"])
            seen: set[str] = set()
            while ok and current.id not in seen and current.parent_id != current.id:
                seen.add(current.id)
                if current.parent_id == kb_root["id"]:
                    root = current.to_dict()
                    break
                ok, current = FileService.get_by_id(current.parent_id)
            if root:
                break
        if root is None:
            candidates = FileService.query(tenant_id=self.tenant_id, parent_id=kb_root["id"], name=dataset.name)
            root = candidates[0].to_dict() if candidates else None
        if root is None:
            raise NotFoundError("dataset file hierarchy is not initialized")

        return root

    @staticmethod
    def _descendant_files(root_id: str) -> dict[str, dict]:
        result: dict[str, dict] = {}
        pending = [root_id]
        while pending:
            parent_id = pending.pop()
            for item in FileService.list_all_files_by_parent_id(parent_id):
                value = item.to_dict() if hasattr(item, "to_dict") else dict(item)
                if value["id"] in result:
                    continue
                result[value["id"]] = value
                if value.get("type") == FileType.FOLDER.value:
                    pending.append(value["id"])
        return result

    def _projection(self) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        docs = {doc["id"]: doc for doc in self._documents()}
        root = self._dataset_file_root()
        files = self._descendant_files(root["id"])
        links = list(File2Document.select().where((File2Document.document_id.in_(list(docs))) & (File2Document.file_id.in_(list(files)))).dicts()) if docs and files else []
        file_docs: dict[str, list[dict]] = defaultdict(list)
        for link in links:
            doc, file = docs.get(link["document_id"]), files.get(link.get("file_id"))
            if doc is None or file is None or not check_file_team_permission(file, self.tenant_id):
                continue
            file_docs[file["id"]].append(doc)

        entries: dict[str, dict] = {"/": {"kind": "directory", "name": "/", "path": "/"}, "/files": {"kind": "directory", "name": "files", "path": "/files", "_file_id": root["id"]}}
        children: dict[str, list[dict]] = defaultdict(list)
        children["/"].append(entries["/files"])
        by_parent: dict[str, list[dict]] = defaultdict(list)
        for file in files.values():
            by_parent[file["parent_id"]].append(file)

        def project(parent_id: str, parent_path: str) -> None:
            for file in by_parent.get(parent_id, []):
                path = parent_path + "/" + file["name"]
                if file.get("type") == FileType.FOLDER.value:
                    entry = {"kind": "directory", "name": file["name"], "path": path, "_file_id": file["id"]}
                    entries[path] = entry
                    children[parent_path].append(entry)
                    project(file["id"], path)
                    continue
                linked = file_docs.get(file["id"], [])
                if not linked:
                    continue
                doc = linked[0]
                parts = [file["name"]]
                display_parent = parent_path
                for part in parts[:-1]:
                    virtual_path = display_parent + "/" + part
                    if virtual_path not in entries:
                        virtual = {"kind": "directory", "name": part, "path": virtual_path}
                        entries[virtual_path] = virtual
                        children[display_parent].append(virtual)
                    display_parent = virtual_path
                path = display_parent + "/" + parts[-1]
                entry = self._file_entry(path, parts[-1], doc, file)
                entries[path] = entry
                children[display_parent].append(entry)

        project(root["id"], "/files")
        for path, values in list(children.items()):
            try:
                children[path] = CorpusPathResolver.display_names(path, values)
                for value in children[path]:
                    entries[value["path"]] = value
            except AmbiguousPathError:
                raise
        return entries, children

    def resolve_writable(self, path: str, *, folder: bool | None = None) -> dict:
        path = CorpusPathResolver.normalize(path)
        if path != "/files" and not path.startswith("/files/"):
            raise CorpusFsError("path is outside writable /files tree")
        entries, _ = self._projection()
        entry = entries.get(path)
        if entry is None or not entry.get("_file_id"):
            raise NotFoundError("writable path not found")
        if folder is True and entry["kind"] != "directory":
            raise CorpusFsError("path is not a directory")
        if folder is False and entry["kind"] != "file":
            raise CorpusFsError("path is not a file")
        return entry

    def create_folder(self, parent_path: str, name: str) -> dict:
        parent = self.resolve_writable(parent_path, folder=True)
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise CorpusFsError("invalid folder name")
        if FileService.query(name=name, parent_id=parent["_file_id"]):
            raise EntryConflictError("entry already exists")
        FileService.insert({"id": get_uuid(), "parent_id": parent["_file_id"], "tenant_id": self.tenant_id, "created_by": self.tenant_id, "name": name, "location": "", "size": 0, "type": FileType.FOLDER.value})
        return {"kind": "directory", "name": name, "path": parent_path.rstrip("/") + "/" + name, "child_count": 0}

    def move(self, source_paths: list[str], destination_path: str, new_name: str | None = None) -> int:
        if new_name and len(source_paths) != 1:
            raise CorpusFsError("new_name requires one source")
        destination = self.resolve_writable(destination_path, folder=True)
        sources = [self.resolve_writable(path) for path in source_paths]
        if any(source["path"] == "/files" for source in sources):
            raise CorpusFsError("/files cannot be moved")
        for source in sources:
            if destination_path == source["path"] or destination_path.startswith(source["path"].rstrip("/") + "/"):
                raise MoveCycleError("cannot move into descendant")
            name = new_name or source["name"]
            if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
                raise CorpusFsError("invalid entry name")
            if FileService.query(name=name, parent_id=destination["_file_id"]):
                raise EntryConflictError("entry already exists")
            ok, file = FileService.get_by_id(source["_file_id"])
            if not ok:
                raise NotFoundError("source not found")
            if file.type != FileType.FOLDER.value and new_name and os.path.splitext(file.name.lower())[1] != os.path.splitext(new_name.lower())[1]:
                raise CorpusFsError("file extension cannot be changed")
            updates = {"parent_id": destination["_file_id"]}
            if new_name:
                updates["name"] = new_name
            FileService.update_by_id(file.id, updates)
            if new_name:
                for link in File2DocumentService.get_by_file_id(file.id):
                    DocumentService.update_by_id(link.document_id, {"name": new_name})
        return len(sources)

    def deletion_targets(self, paths: list[str]) -> tuple[list[str], int, int]:
        entries = [self.resolve_writable(path) for path in paths]
        if any(entry["path"] == "/files" for entry in entries):
            raise CorpusFsError("/files cannot be deleted")
        ids: list[str] = []
        file_count = 0
        folder_count = 0
        for entry in entries:
            current = [entry["_file_id"]]
            while current:
                file_id = current.pop()
                if file_id in ids:
                    continue
                ids.append(file_id)
                ok, file = FileService.get_by_id(file_id)
                if not ok:
                    continue
                if file.type == FileType.FOLDER.value:
                    folder_count += 1
                    current.extend(child.id for child in FileService.list_all_files_by_parent_id(file_id))
                else:
                    file_count += 1
        return ids, file_count, folder_count

    def delete(self, paths: list[str]) -> dict:
        ids, _, _ = self.deletion_targets(paths)
        errors: list[str] = []
        success_count = 0
        for file_id in reversed(ids):
            ok, file = FileService.get_by_id(file_id)
            if not ok:
                continue
            if file.type != FileType.FOLDER.value:
                for link in File2DocumentService.get_by_file_id(file_id):
                    found, document = DocumentService.get_by_id(link.document_id)
                    tenant_id = DocumentService.get_tenant_id(link.document_id)
                    if found and document and tenant_id and not DocumentService.remove_document(document, tenant_id):
                        errors.append(f"failed to remove document for path target {file_id}")
                File2DocumentService.delete_by_file_id(file_id)
            try:
                FileService.delete(file)
                success_count += 1
            except Exception:
                errors.append(f"failed to remove path target {file_id}")
        return {"success_count": success_count, "error_count": len(errors)}

    def relocate_uploaded_documents(self, document_ids: list[str], destination_path: str) -> None:
        destination = self.resolve_writable(destination_path, folder=True)
        for document_id in document_ids:
            links = File2DocumentService.get_by_document_id(document_id)
            if not links:
                raise CorpusFsError("uploaded document has no file entry")
            FileService.update_by_id(links[0].file_id, {"parent_id": destination["_file_id"]})

    def uploaded_entries(self, document_ids: list[str]) -> list[dict]:
        entries, _ = self._projection()
        by_document_id = {
            entry["_document_id"]: entry
            for entry in entries.values()
            if entry.get("kind") == "file" and entry.get("_document_id")
        }
        uploaded = []
        for document_id in document_ids:
            entry = by_document_id.get(document_id)
            if entry is None:
                raise CorpusFsError("uploaded document is not visible")
            uploaded.append(self.public_entry(entry))
        return uploaded

    @staticmethod
    def _file_entry(path: str, name: str, doc: dict, file: dict | None) -> dict:
        status = {
            TaskStatus.UNSTART.value: "UNSTARTED",
            TaskStatus.RUNNING.value: "RUNNING",
            TaskStatus.CANCEL.value: "CANCELLED",
            TaskStatus.DONE.value: "DONE",
            TaskStatus.FAIL.value: "FAILED",
            TaskStatus.SCHEDULE.value: "SCHEDULED",
        }.get(str(doc.get("run")), "UNKNOWN")
        return {
            "kind": "file",
            "path": path,
            "name": name,
            "size_bytes": doc.get("size", 0),
            "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "parse_status": status,
            "parse_progress": doc.get("progress", 0),
            "parse_message": doc.get("progress_msg") or "",
            "chunk_count": doc.get("chunk_num", 0),
            "token_count": doc.get("token_num", 0),
            "created_at": doc.get("create_date"),
            "updated_at": doc.get("update_date"),
            "content_hash": doc.get("content_hash") or "",
            "_document_id": doc["id"],
            "_file_id": file and file["id"],
            "_content_hash": doc.get("content_hash") or "",
        }

    @contextmanager
    def content_mutation(self, expected_revision: int):
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise PreconditionRequiredError("If-Match must contain a positive content revision")
        self._dataset()
        with DB.atomic():
            dataset = self._locked_dataset()
            if dataset.content_revision != expected_revision:
                raise RevisionConflictError("content revision changed")
            yield
            updated = Knowledgebase.update(content_revision=expected_revision + 1).where(
                (Knowledgebase.id == self.dataset_id) & (Knowledgebase.content_revision == expected_revision)
            ).execute()
            if updated != 1:
                raise RevisionConflictError("content revision changed")

    def _locked_dataset(self):
        dataset = Knowledgebase.select().where(Knowledgebase.id == self.dataset_id).for_update().first()
        if dataset is None:
            raise NotFoundError("dataset not found")
        return dataset

    def search_scope(self, path: str) -> dict[str, dict]:
        path = CorpusPathResolver.normalize(path)
        entries, _ = self._projection()
        root = entries.get(path)
        if root is None:
            raise NotFoundError("path not found")
        if root["kind"] == "file":
            return {root["_document_id"]: root}
        prefix = path.rstrip("/") + "/"
        return {
            entry["_document_id"]: entry
            for entry_path, entry in entries.items()
            if entry["kind"] == "file" and (path == "/" or entry_path.startswith(prefix))
        }

    @staticmethod
    def search_vector_weight(mode: str, configured_weight: float) -> float:
        return 1.0 if mode == "semantic" else configured_weight

    def tree(self, path: str, limit: int, offset: int = 0) -> tuple[list[dict], bool]:
        path = CorpusPathResolver.normalize(path)
        if limit < 1 or limit > 200 or offset < 0:
            raise CorpusFsError("invalid pagination")
        entries, children = self._projection()
        if path not in entries or entries[path]["kind"] != "directory":
            raise NotFoundError("path not found")
        values = sorted(children.get(path, []), key=lambda item: (item["kind"] != "directory", item["name"], item["path"]))
        for value in values:
            self.registry.put(self.tenant_id, self.dataset_id, self.actor_digest, value["path"], {key: value[key] for key in ("_document_id", "_file_id", "_content_hash") if key in value})
        return [self.public_entry(value, len(children.get(value["path"], []))) for value in values[offset : offset + limit]], offset + limit < len(values)

    def find(self, name: str, path: str, limit: int, offset: int = 0, *, recursive: bool = True, match: str = "contains") -> tuple[list[dict], bool]:
        path = CorpusPathResolver.normalize(path)
        if not name or limit < 1 or limit > 200 or match not in {"contains", "prefix", "exact"}:
            raise CorpusFsError("invalid find request")
        entries, _ = self._projection()
        root = entries.get(path)
        if root is None:
            raise NotFoundError("path not found")
        if root["kind"] != "directory":
            raise CorpusFsError("path is not a directory")
        prefix = path.rstrip("/") + "/"
        def matches(entry_path: str, entry: dict) -> bool:
            if entry["kind"] != "file" or not entry_path.startswith(prefix):
                return False
            if not recursive and entry_path[len(prefix) :].count("/"):
                return False
            candidate = entry["name"].casefold()
            needle = name.casefold()
            return {"contains": needle in candidate, "prefix": candidate.startswith(needle), "exact": candidate == needle}[match]

        values = [entry for entry_path, entry in entries.items() if matches(entry_path, entry)]
        values.sort(key=lambda item: item["path"])
        return [self.public_entry(item) for item in values[offset : offset + limit]], offset + limit < len(values)

    def stat(self, path: str) -> dict:
        path = CorpusPathResolver.normalize(path)
        entries, children = self._projection()
        entry = entries.get(path)
        if entry is None:
            raise NotFoundError("path not found")
        return self.public_entry(entry, len(children.get(path, [])))

    def usage(self, path: str, depth: int) -> dict:
        path = CorpusPathResolver.normalize(path)
        if depth < 0 or depth > 3:
            raise CorpusFsError("depth must be between 0 and 3")
        entries, _ = self._projection()
        if path not in entries:
            raise NotFoundError("path not found")
        prefix = path.rstrip("/") + "/"
        selected = [item for item_path, item in entries.items() if item["kind"] == "file" and item_path.startswith(prefix) and item_path.count("/") - path.count("/") <= depth + 1]
        return {"path": path, "document_count": len(selected), "size_bytes": sum(item["size_bytes"] for item in selected), "chunk_count": sum(item["chunk_count"] for item in selected), "token_count": sum(item["token_count"] for item in selected)}

    def resolve_document(self, path: str) -> tuple[dict, dict]:
        path = CorpusPathResolver.normalize(path)
        entries, _ = self._projection()
        entry = entries.get(path)
        if entry is None or entry["kind"] != "file":
            raise NotFoundError("path not found")
        doc = next((value for value in self._documents() if value["id"] == entry["_document_id"]), None)
        if doc is None:
            raise NotFoundError("path not found")
        return entry, doc

    def original_data(self, path: str) -> tuple[dict, bytes, str]:
        entry, document = self.resolve_document(path)
        bucket, object_name = File2DocumentService.get_storage_address(doc_id=document["id"])
        data = settings.STORAGE_IMPL.get(bucket, object_name)
        if data is None:
            raise NotFoundError("source object not found")
        return entry, data, object_name

    def parse_state(self, path: str) -> dict:
        entry, document = self.resolve_document(path)
        public = self._file_entry(entry["path"], entry["name"], document, None)
        return {
            "path": entry["path"],
            "status": public["parse_status"],
            "progress": public["parse_progress"],
            "message": public["parse_message"],
            "chunk_count": public["chunk_count"],
            "token_count": public["token_count"],
            "started_at": document.get("process_begin_at"),
            "updated_at": document.get("update_date"),
        }

    def retry_parse(self, path: str) -> dict:
        _, document = self.resolve_document(path)
        if str(document.get("run")) == TaskStatus.RUNNING.value:
            raise EntryConflictError("document is already being parsed")
        if str(document.get("run")) == TaskStatus.DONE.value:
            DocumentService.clear_chunk_num_when_rerun(document["id"])
        DocumentService.update_by_id(document["id"], {"run": TaskStatus.RUNNING.value, "progress": 0, "progress_msg": "", "chunk_num": 0, "token_num": 0})
        TaskService.filter_delete([Task.doc_id == document["id"]])
        index_name = search.index_name(self.tenant_id)
        if settings.docStoreConn.index_exist(index_name, self.dataset_id):
            settings.docStoreConn.delete({"doc_id": document["id"]}, index_name, self.dataset_id)
        found, current = DocumentService.get_by_id(document["id"])
        if not found:
            raise NotFoundError("document not found")
        DocumentService.run(self.tenant_id, current.to_dict(), {})
        return {"path": path, "status": "RUNNING"}

    def cancel_parse(self, path: str) -> dict:
        _, document = self.resolve_document(path)
        tasks = list(TaskService.query(doc_id=document["id"]))
        unfinished = any((task.progress or 0) < 1 for task in tasks)
        if str(document.get("run")) not in {TaskStatus.RUNNING.value, TaskStatus.CANCEL.value} and not unfinished:
            raise EntryConflictError("document has no running parse task")
        cancel_all_task_of(document["id"])
        message = (document.get("progress_msg") or "") + "\nTask stopped by user."
        DocumentService.update_by_id(document["id"], {"run": TaskStatus.CANCEL.value, "progress": 0, "progress_msg": message})
        return {"path": path, "status": "CANCELLED"}

    def parse_documents(self, paths: list[str], action: str) -> dict:
        if action not in {"retry", "cancel"}:
            raise CorpusFsError("action must be retry or cancel")
        if not isinstance(paths, list) or not paths or len(paths) > 100:
            raise CorpusFsError("paths must contain 1 to 100 entries")

        normalized_paths = list(dict.fromkeys(CorpusPathResolver.normalize(path) for path in paths))
        operation = self.retry_parse if action == "retry" else self.cancel_parse
        items = []
        counts = {"succeeded": 0, "skipped": 0, "failed": 0}
        for path in normalized_paths:
            try:
                result = operation(path)
                item = {"path": path, "status": result["status"], "outcome": "succeeded"}
            except EntryConflictError as error:
                item = {"path": path, "status": "UNCHANGED", "outcome": "skipped", "message": str(error)}
            except CorpusFsError as error:
                item = {"path": path, "status": "UNCHANGED", "outcome": "failed", "message": str(error)}
            counts[item["outcome"]] += 1
            items.append(item)
        return {
            "action": action,
            "success_count": counts["succeeded"],
            "skipped_count": counts["skipped"],
            "failed_count": counts["failed"],
            "items": items,
        }

    def markdown_current(self, path: str) -> dict:
        entry, document = self.resolve_document(path)
        result = DocumentMarkdownService.current(document["id"])
        if result is None:
            raise NotFoundError("markdown derivative not found")
        return {"path": entry["path"], **result}

    def markdown_versions(self, path: str) -> dict:
        entry, document = self.resolve_document(path)
        return {"path": entry["path"], "items": DocumentMarkdownService.versions(document["id"])}

    def save_markdown(self, path: str, content: str, expected_version: int) -> dict:
        entry, document = self.resolve_document(path)
        try:
            result = DocumentMarkdownService.save(document, self.tenant_id, content, expected_version)
        except MarkdownVersionConflict as exc:
            raise RevisionConflictError(str(exc)) from exc
        except ValueError as exc:
            raise CorpusFsError(str(exc)) from exc
        return {"path": entry["path"], **result}

    def restore_markdown(self, path: str, version: int, expected_version: int) -> dict:
        entry, document = self.resolve_document(path)
        try:
            result = DocumentMarkdownService.restore(document, self.tenant_id, version, expected_version)
        except MarkdownVersionConflict as exc:
            raise RevisionConflictError(str(exc)) from exc
        except MarkdownVersionNotFound as exc:
            raise NotFoundError(str(exc)) from exc
        return {"path": entry["path"], **result}

    @staticmethod
    def _image_ref_secret() -> bytes:
        return corpus_secret("reference")

    def _image_ref(self, document_id: str, image_id: str) -> str:
        value = f"{self.tenant_id}:{self.dataset_id}:{document_id}:{image_id}".encode()
        return hmac.new(self._image_ref_secret(), value, hashlib.sha256).hexdigest()

    def _chunk_ref(self, document_id: str, chunk_id: str) -> str:
        value = f"chunk:{self.tenant_id}:{self.dataset_id}:{document_id}:{chunk_id}".encode()
        return f"chunk:{hmac.new(self._image_ref_secret(), value, hashlib.sha256).hexdigest()}"

    def _chunk_fields(self, document_id: str) -> dict[str, dict]:
        rows = settings.docStoreConn.search(
            ["content_with_weight", "chunk_order_int", "page_num_int", "position_int", "top_int", "img_id"],
            [],
            {"doc_id": document_id},
            [],
            OrderByExpr(),
            0,
            10000,
            search.index_name(self.tenant_id),
            [self.dataset_id],
        )
        return settings.docStoreConn.get_fields(rows, ["content_with_weight", "chunk_order_int", "page_num_int", "position_int", "top_int", "img_id"])

    @staticmethod
    def _chunk_position_key(chunk_id: str, chunk: dict) -> tuple:
        positions = chunk.get("position_int") or []
        valid_positions = [position for position in positions if isinstance(position, (list, tuple)) and len(position) >= 5]
        if valid_positions:
            page, left, _, top, _ = min(valid_positions, key=lambda position: (position[0], position[3], position[1]))
            return (0, page, top, left, chunk_id)

        pages = chunk.get("page_num_int") or []
        tops = chunk.get("top_int") or []
        if pages:
            return (1, pages[0], tops[0] if tops else 0, 0, chunk_id)

        order = chunk.get("chunk_order_int")
        if order is not None:
            return (2, order, 0, 0, chunk_id)
        return (3, 0, 0, 0, chunk_id)

    def content_text(self, document: dict, page: int | None = None, kinds: set[str] | None = None) -> tuple[str, str, list[dict], list[dict]]:
        if document.get("type", "").lower() == "pdf":
            try:
                projection = SourceProjectionService.active(document)
            except (OperationalError, ProgrammingError):
                projection = None
            if projection:
                manifest, projection_blocks = projection
                selected = [
                    block
                    for block in projection_blocks
                    if (page is None or block.get("page") == page) and (not kinds or block.get("kind") in kinds)
                ]
                blocks: list[dict] = []
                images: list[dict] = []
                content_parts: list[str] = []
                offset = 0
                for index, block in enumerate(selected):
                    prefix = "\n\n" if index else ""
                    content = block.get("text", "")
                    content_part = prefix + content
                    start = offset + len(prefix.encode("utf-8"))
                    offset += len(content_part.encode("utf-8"))
                    public_block = {key: block.get(key) for key in ("block_ref", "ordinal", "kind", "page", "positions", "parent_block_ref")}
                    if "level" in block:
                        public_block["level"] = block["level"]
                    public_block.update({"content": content, "start": start, "end": offset})
                    image_id = block.get("image_id")
                    if image_id:
                        image_ref = self._image_ref(document["id"], image_id)
                        public_block["image_ref"] = image_ref
                        images.append({"image_ref": image_ref, "offset": offset})
                    blocks.append(public_block)
                    content_parts.append(content_part)
                content = "".join(content_parts)
                if blocks:
                    return content, f"source_projection:{manifest['projection_revision']}", images, blocks
                raise UnsupportedContentError("projection has no readable text for the selected filter")
        if document.get("run") != TaskStatus.DONE.value:
            raise DocumentNotReadyError("document is not ready")
        if document.get("type", "").lower() in {"txt", "md", "markdown"}:
            try:
                bucket, object_name = File2DocumentService.get_storage_address(doc_id=document["id"])
                text = settings.STORAGE_IMPL.get(bucket, object_name).decode("utf-8")
                return text, "raw_text", [], [{"content": text, "start": 0, "end": len(text.encode("utf-8"))}]
            except (UnicodeDecodeError, OSError, AttributeError, TypeError):
                pass
        fields = self._chunk_fields(document["id"])
        chunks = sorted(fields.items(), key=lambda item: self._chunk_position_key(*item))
        content_parts: list[str] = []
        images: list[dict] = []
        blocks: list[dict] = []
        offset = 0
        for index, (chunk_id, chunk) in enumerate(chunks):
            prefix = "\n\n" if index else ""
            chunk_content = chunk.get("content_with_weight") or ""
            content_part = prefix + chunk_content
            start = offset + len(prefix.encode("utf-8"))
            content_parts.append(content_part)
            offset += len(content_part.encode("utf-8"))
            image_id = chunk.get("img_id") or ""
            block = {"block_ref": self._chunk_ref(document["id"], chunk_id), "content": chunk_content, "start": start, "end": offset}
            if image_id:
                image_ref = self._image_ref(document["id"], image_id)
                block["image_ref"] = image_ref
                images.append({"image_ref": image_ref, "offset": offset})
            blocks.append(block)
        content = "".join(content_parts)
        if not content:
            raise UnsupportedContentError("document has no readable text projection")
        return content, "parsed_chunks", images, blocks

    @staticmethod
    def _first_page(value: Any) -> int | None:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        try:
            page = int(value)
        except (TypeError, ValueError):
            return None
        return page if page > 0 else None

    @staticmethod
    def _position_overlap(left: Any, right: Any) -> float:
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)) or len(left) < 5 or len(right) < 5:
            return -1
        try:
            if int(left[0]) != int(right[0]):
                return -1
            horizontal = max(0.0, min(float(left[2]), float(right[2])) - max(float(left[1]), float(right[1])))
            vertical = max(0.0, min(float(left[4]), float(right[4])) - max(float(left[3]), float(right[3])))
            return horizontal * vertical
        except (TypeError, ValueError):
            return -1

    def citation_context(self, document: dict) -> tuple[dict, list[dict] | None]:
        if document.get("type", "").lower() == "pdf":
            try:
                projection = SourceProjectionService.active(document)
            except (OperationalError, ProgrammingError):
                projection = None
            if projection:
                return projection
        bucket, object_name = File2DocumentService.get_storage_address(doc_id=document["id"])
        source = settings.STORAGE_IMPL.get(bucket, object_name)
        if hasattr(source, "read"):
            source = source.read()
        if not isinstance(source, bytes):
            raise NotFoundError("source object not found")
        return {"projection_revision": None, "source_sha256": hashlib.sha256(source).hexdigest()}, None

    def citation_target(
        self,
        entry: dict,
        document: dict,
        content_revision: int,
        *,
        block: dict | None = None,
        chunk: dict | None = None,
        context: tuple[dict, list[dict] | None] | None = None,
    ) -> dict:
        manifest, projection_blocks = context or self.citation_context(document)
        selected = block
        page = self._first_page((chunk or {}).get("page_num_int")) or self._first_page((block or {}).get("page"))
        if chunk is not None and projection_blocks:
            chunk_positions = chunk.get("position_int") or []
            ranked = []
            for candidate in projection_blocks:
                overlap = max(
                    (self._position_overlap(chunk_position, block_position) for chunk_position in chunk_positions for block_position in (candidate.get("positions") or [])),
                    default=-1,
                )
                if overlap > 0:
                    ranked.append((overlap, candidate))
            if ranked:
                selected = max(ranked, key=lambda item: item[0])[1]
            elif snippet := str(chunk.get("content_with_weight") or ""):
                selected = next(
                    (
                        candidate
                        for candidate in projection_blocks
                        if (page is None or candidate.get("page") == page) and snippet in str(candidate.get("text") or "")
                    ),
                    None,
                )

        content = str((selected or {}).get("content", (selected or {}).get("text", "")))
        snippet = str((chunk or {}).get("content_with_weight") or "")
        start = content.find(snippet) if content and snippet else 0
        start = max(start, 0)
        start_bytes = len(content[:start].encode())
        cited_text = snippet if snippet and content[start : start + len(snippet)] == snippet else content
        block_ref = (selected or {}).get("block_ref")
        if block_ref is None and chunk is not None:
            chunk_id = chunk.get("id")
            block_ref = self._chunk_ref(document["id"], chunk_id) if chunk_id else None
        if block_ref is None and block is not None:
            block_ref = "raw:0"
        page = self._first_page((selected or {}).get("page")) or page
        return {
            "path": entry["path"],
            "content_revision": content_revision,
            "projection_revision": manifest.get("projection_revision") if chunk is None or selected is not None else None,
            "page": page,
            "block_ref": str(block_ref) if block_ref is not None else None,
            "range": {"start": start_bytes, "end": start_bytes + len(cited_text.encode())} if cited_text else None,
            "source_hash": manifest["source_sha256"],
        }

    def authorize_citation(self, document_id: str) -> tuple[dict, dict]:
        documents = {document["id"]: document for document in self._documents()}
        document = documents.get(document_id)
        if document is None:
            raise NotFoundError("citation document not found")
        entries, _ = self._projection()
        entry = next((item for item in entries.values() if item.get("kind") == "file" and item.get("_document_id") == document_id), None)
        if entry is None:
            raise NotFoundError("citation document not found")
        return document, entry

    def citation_preview(self, document_id: str, target: dict) -> dict:
        document, entry = self.authorize_citation(document_id)
        projection_revision = target.get("projection_revision")
        if projection_revision is not None:
            projection = SourceProjectionService.revision(document_id, projection_revision, target["source_hash"])
            if projection is None:
                raise NotFoundError("citation projection not found")
            _, projection_blocks = projection
            selected = [block for block in projection_blocks if str(block.get("block_ref")) == target.get("block_ref")]
            if not selected and target.get("page") is not None:
                selected = [block for block in projection_blocks if block.get("page") == target["page"]]
            if not selected:
                raise NotFoundError("citation block not found")
            blocks = []
            for block in selected:
                public = {key: block.get(key) for key in ("block_ref", "ordinal", "kind", "page", "positions", "parent_block_ref")}
                if "level" in block:
                    public["level"] = block["level"]
                public["content"] = block.get("text", "")
                blocks.append(public)
            content = "\n\n".join(block["content"] for block in blocks)
        else:
            manifest, _ = self.citation_context(document)
            if manifest["source_sha256"] != target["source_hash"]:
                raise NotFoundError("citation source version not found")
            block_ref = target.get("block_ref")
            if block_ref == "raw:0":
                bucket, object_name = File2DocumentService.get_storage_address(doc_id=document_id)
                source = settings.STORAGE_IMPL.get(bucket, object_name)
                if hasattr(source, "read"):
                    source = source.read()
                try:
                    content = source.decode("utf-8")
                except (AttributeError, UnicodeDecodeError):
                    raise UnsupportedContentError("citation source is not UTF-8 text") from None
            else:
                fields = self._chunk_fields(document_id)
                chunk = next((value for chunk_id, value in fields.items() if hmac.compare_digest(self._chunk_ref(document_id, chunk_id), block_ref or "")), None)
                if chunk is None:
                    raise NotFoundError("citation block not found")
                content = chunk.get("content_with_weight") or ""
            blocks = [{"block_ref": target.get("block_ref"), "page": target.get("page"), "content": content}]

        byte_range = target.get("range")
        if byte_range is not None and len(blocks) == 1:
            content = content.encode()[byte_range["start"] : byte_range["end"]].decode("utf-8", errors="ignore")
        return {"current_path": entry["path"], "content": content, "blocks": blocks}

    @staticmethod
    def content_page(blocks: list[dict], start_index: int, maximum_bytes: int) -> tuple[list[dict], int]:
        """Return complete blocks bounded by the byte budget whenever possible."""
        if start_index < 0 or start_index > len(blocks):
            raise CorpusFsError("invalid content cursor")
        page: list[dict] = []
        size = 0
        index = start_index
        while index < len(blocks):
            block_size = len(str(blocks[index].get("content") or "").encode("utf-8"))
            if page and size + block_size > maximum_bytes:
                break
            page.append(blocks[index])
            size += block_size
            index += 1
            if size >= maximum_bytes:
                break
        return page, index

    def image_data(self, document: dict, image_ref: str) -> tuple[bytes, str]:
        try:
            projection = SourceProjectionService.active(document) if document.get("type", "").lower() == "pdf" else None
        except (OperationalError, ProgrammingError):
            projection = None
        if projection:
            _, blocks = projection
            for block in blocks:
                image_id = block.get("image_id") or ""
                if image_id and hmac.compare_digest(image_ref, self._image_ref(document["id"], image_id)):
                    prefix = f"{self.dataset_id}-"
                    if image_id.startswith(prefix):
                        object_name = image_id[len(prefix) :]
                        data = settings.STORAGE_IMPL.get(self.dataset_id, object_name)
                        if data:
                            return data, object_name
                    break
        for chunk in self._chunk_fields(document["id"]).values():
            image_id = chunk.get("img_id") or ""
            if image_id and hmac.compare_digest(image_ref, self._image_ref(document["id"], image_id)):
                prefix = f"{self.dataset_id}-"
                if not image_id.startswith(prefix):
                    break
                object_name = image_id[len(prefix) :]
                if object_name:
                    data = settings.STORAGE_IMPL.get(self.dataset_id, object_name)
                    if data:
                        return data, object_name
                break
        raise ImageNotFoundError("image not found")

    @staticmethod
    def public_entry(entry: dict, child_count: int | None = None) -> dict:
        result = {key: value for key, value in entry.items() if not key.startswith("_") and key not in {"location", "document_id", "file_id", "chunk_id"}}
        if result["kind"] == "directory":
            result["child_count"] = child_count or 0
        return result

    @staticmethod
    def revision(dataset: Any) -> dict:
        return {"content_revision": getattr(dataset, "content_revision", 1), "acl_revision": getattr(dataset, "acl_revision", 1)}
