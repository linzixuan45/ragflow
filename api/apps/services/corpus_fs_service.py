"""Dataset-isolated Corpus FS projections and path-only mutations."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
from collections import defaultdict
from typing import Any

from peewee import OperationalError, ProgrammingError

from api.apps.services.corpus_path_service import AmbiguousPathError, CorpusPathRegistry, CorpusPathResolver
from api.apps.services.source_projection_service import SourceProjectionService
from api.common.check_team_permission import check_file_team_permission, check_kb_team_permission
from api.db import FileType
from api.db.db_models import Document, File2Document
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
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
        tenant_root = FileService.get_root_folder(self.tenant_id)
        FileService.init_knowledgebase_docs(tenant_root["id"], self.tenant_id)
        kb_root = FileService.get_kb_folder(self.tenant_id)
        document_ids = [document["id"] for document in self._documents()]
        links = list(File2Document.select().where(File2Document.document_id.in_(document_ids)).dicts()) if document_ids else []
        for link in links:
            ok, current = FileService.get_by_id(link["file_id"])
            seen: set[str] = set()
            while ok and current.id not in seen and current.parent_id != current.id:
                seen.add(current.id)
                if current.parent_id == kb_root["id"]:
                    if current.name != dataset.name:
                        FileService.update_by_id(current.id, {"name": dataset.name})
                        current.name = dataset.name
                    return current.to_dict()
                ok, current = FileService.get_by_id(current.parent_id)
        return FileService.new_a_file_from_kb(self.tenant_id, dataset.name, kb_root["id"])

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
        linked_doc_ids: set[str] = set()
        file_docs: dict[str, list[dict]] = defaultdict(list)
        for link in links:
            doc, file = docs.get(link["document_id"]), files.get(link.get("file_id"))
            if doc is None or file is None or not check_file_team_permission(file, self.tenant_id):
                continue
            linked_doc_ids.add(doc["id"])
            file_docs[file["id"]].append(doc)

        entries: dict[str, dict] = {"/": {"kind": "directory", "name": "/", "path": "/"}, "/files": {"kind": "directory", "name": "files", "path": "/files", "_file_id": root["id"]}, "/documents": {"kind": "directory", "name": "documents", "path": "/documents"}}
        children: dict[str, list[dict]] = defaultdict(list)
        children["/"].extend([entries["/files"], entries["/documents"]])
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
                parts = self._file_parts(file, {}) if parent_path == "/files" else [file["name"]]
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
        for doc in docs.values():
            if doc["id"] not in linked_doc_ids:
                path = "/documents/" + doc["name"]
                entries[path] = self._file_entry(path, doc["name"], doc, None)
                children["/documents"].append(entries[path])
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

    @staticmethod
    def _file_parts(file: dict, files: dict[str, dict]) -> list[str]:
        parts = [file["name"]]
        current = file
        seen = {file["id"]}
        while current["parent_id"] in files and current["parent_id"] not in seen:
            current = files[current["parent_id"]]
            seen.add(current["id"])
            parts.append(current["name"])
        parts = list(reversed(parts))
        # The historic corpus importer encoded logical paths in a flat File
        # name (for example, "公司制度__001_发文管理规定.md").  Keep File.parent_id
        # authoritative when it exists, but recover those legacy directories
        # when the File record itself has no hierarchy.
        if len(parts) == 1 and "__" in parts[0] and not re.match(r"^\d+_", parts[0]):
            encoded_parts = [part for part in parts[0].split("__") if part]
            if len(encoded_parts) > 1:
                return encoded_parts
        return parts

    @staticmethod
    def _file_entry(path: str, name: str, doc: dict, file: dict | None) -> dict:
        return {"kind": "file", "path": path, "name": name, "size_bytes": doc.get("size", 0), "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "parse_status": "DONE" if doc.get("run") == TaskStatus.DONE.value else "PENDING", "chunk_count": doc.get("chunk_num", 0), "token_count": doc.get("token_num", 0), "updated_at": doc.get("update_date"), "_document_id": doc["id"], "_file_id": file and file["id"], "_content_hash": doc.get("content_hash") or ""}

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

    @staticmethod
    def _image_ref_secret() -> bytes:
        return os.environ.get("RAGFLOW_CORPUS_CURSOR_SECRET", "ragflow-corpus-fs-development-secret").encode()

    def _image_ref(self, document_id: str, image_id: str) -> str:
        value = f"{self.tenant_id}:{self.dataset_id}:{document_id}:{image_id}".encode()
        return hmac.new(self._image_ref_secret(), value, hashlib.sha256).hexdigest()

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
        if document.get("run") != TaskStatus.DONE.value:
            raise DocumentNotReadyError("document is not ready")
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
        if document.get("type", "").lower() in {"txt", "md", "markdown"}:
            try:
                text = settings.STORAGE_IMPL.get(document["id"], document["location"]).read().decode("utf-8")
                return text, "raw_text", [], [{"content": text, "start": 0, "end": len(text.encode("utf-8"))}]
            except (UnicodeDecodeError, OSError, AttributeError):
                pass
        fields = self._chunk_fields(document["id"])
        chunks = sorted(fields.items(), key=lambda item: self._chunk_position_key(*item))
        content_parts: list[str] = []
        images: list[dict] = []
        blocks: list[dict] = []
        offset = 0
        for index, (_, chunk) in enumerate(chunks):
            prefix = "\n\n" if index else ""
            chunk_content = chunk.get("content_with_weight") or ""
            content_part = prefix + chunk_content
            start = offset + len(prefix.encode("utf-8"))
            content_parts.append(content_part)
            offset += len(content_part.encode("utf-8"))
            image_id = chunk.get("img_id") or ""
            block = {"content": chunk_content, "start": start, "end": offset}
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
