"""Dataset-isolated, read-only Corpus FS projections."""

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
from api.db.db_models import Document, File, File2Document
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.constants import TaskStatus
from common.doc_store.doc_store_base import OrderByExpr
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

    def _projection(self) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        docs = {doc["id"]: doc for doc in self._documents()}
        links = list(File2Document.select().where(File2Document.document_id.in_(list(docs))).dicts()) if docs else []
        file_ids = [item["file_id"] for item in links if item.get("file_id")]
        files = {item["id"]: item for item in File.select().where(File.id.in_(file_ids)).dicts()} if file_ids else {}
        linked_doc_ids: set[str] = set()
        file_docs: dict[str, list[dict]] = defaultdict(list)
        for link in links:
            doc, file = docs.get(link["document_id"]), files.get(link.get("file_id"))
            if doc is None or file is None or not check_file_team_permission(file, self.tenant_id):
                continue
            linked_doc_ids.add(doc["id"])
            file_docs[file["id"]].append(doc)

        entries: dict[str, dict] = {"/": {"kind": "directory", "name": "/", "path": "/"}, "/files": {"kind": "directory", "name": "files", "path": "/files"}, "/documents": {"kind": "directory", "name": "documents", "path": "/documents"}}
        children: dict[str, list[dict]] = defaultdict(list)
        children["/"].extend([entries["/files"], entries["/documents"]])
        for file_id, linked in file_docs.items():
            file = files[file_id]
            parts = self._file_parts(file, files)
            if not parts:
                continue
            parent = "/files"
            for part in parts[:-1]:
                path = parent + "/" + part
                if path not in entries:
                    entries[path] = {"kind": "directory", "name": part, "path": path}
                    children[parent].append(entries[path])
                parent = path
            for doc in linked:
                path = parent + "/" + parts[-1]
                entries[path] = self._file_entry(path, parts[-1], doc, file)
                children[parent].append(entries[path])
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
                    content = prefix + block.get("text", "")
                    start = offset
                    offset += len(content.encode("utf-8"))
                    public_block = {key: block.get(key) for key in ("block_ref", "ordinal", "kind", "page", "positions", "parent_block_ref")}
                    public_block.update({"content": content, "start": start, "end": offset})
                    image_id = block.get("image_id")
                    if image_id:
                        image_ref = self._image_ref(document["id"], image_id)
                        public_block["image_ref"] = image_ref
                        images.append({"image_ref": image_ref, "offset": offset})
                    blocks.append(public_block)
                    content_parts.append(content)
                content = "".join(content_parts)
                if content:
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
            start = offset
            chunk_content = prefix + (chunk.get("content_with_weight") or "")
            content_parts.append(chunk_content)
            offset += len(chunk_content.encode("utf-8"))
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
