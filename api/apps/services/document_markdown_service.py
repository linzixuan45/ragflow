"""Immutable Markdown derivatives addressed through Corpus FS paths."""

from __future__ import annotations

import hashlib
import logging

from peewee import IntegrityError

from api.db.db_models import DB, DocumentMarkdownVersion
from common import settings
from common.misc_utils import get_uuid


class MarkdownVersionConflict(ValueError):
    pass


class MarkdownVersionNotFound(ValueError):
    pass


class DocumentMarkdownService:
    _PREFIX = "markdown-derivatives"
    _PUBLICATION_PREFIX = "markdown-publications"
    _RETIREMENT_PENDING = "active version switched; previous index retirement is pending"

    @staticmethod
    def _query(file_id: str):
        return DocumentMarkdownVersion.select().where(DocumentMarkdownVersion.file_id == file_id)

    @classmethod
    def current_row(cls, file_id: str):
        return cls._query(file_id).order_by(DocumentMarkdownVersion.revision.desc()).first()

    @staticmethod
    def _read(row: DocumentMarkdownVersion) -> str:
        data = settings.STORAGE_IMPL.get(row.dataset_id, row.object_name)
        if data is None:
            raise MarkdownVersionNotFound("markdown derivative object is missing")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MarkdownVersionNotFound("markdown derivative is not UTF-8") from exc

    @classmethod
    def current(cls, file_id: str) -> dict | None:
        row = cls.current_row(file_id)
        return cls.public(row, include_content=True) if row else None

    @classmethod
    def versions(cls, file_id: str) -> list[dict]:
        rows = cls._query(file_id).order_by(DocumentMarkdownVersion.revision.desc())
        return [cls.public(row) for row in rows]

    @classmethod
    def active_row(cls, file_id: str):
        return cls._query(file_id).where(DocumentMarkdownVersion.publish_status == "ACTIVE").order_by(DocumentMarkdownVersion.revision.desc()).first()

    @classmethod
    def version_row(cls, file_id: str, revision: int):
        return cls._query(file_id).where(DocumentMarkdownVersion.revision == revision).first()

    @classmethod
    def publication_document_ids(cls, file_id: str) -> list[str]:
        return [
            row.published_document_id
            for row in cls._query(file_id)
            if row.published_document_id
        ]

    @classmethod
    @DB.connection_context()
    def delete_all(cls, file_id: str) -> None:
        cls._delete_all(file_id)

    @classmethod
    def _delete_all(cls, file_id: str) -> None:
        cls._delete_objects(file_id)
        cls._delete_rows(file_id)

    @classmethod
    @DB.connection_context()
    def delete_objects(cls, file_id: str) -> None:
        cls._delete_objects(file_id)

    @classmethod
    def _delete_objects(cls, file_id: str) -> None:
        rows = list(cls._query(file_id))
        for row in rows:
            settings.STORAGE_IMPL.rm(row.dataset_id, row.object_name)
            if row.published_document_id:
                publication_object = (
                    f"{cls._PUBLICATION_PREFIX}/{file_id}/{row.revision}-"
                    f"{row.content_sha256}-{row.published_document_id}.md"
                )
                settings.STORAGE_IMPL.rm(row.dataset_id, publication_object)

    @classmethod
    @DB.connection_context()
    def delete_rows(cls, file_id: str) -> None:
        cls._delete_rows(file_id)

    @classmethod
    def _delete_rows(cls, file_id: str) -> None:
        DocumentMarkdownVersion.delete().where(DocumentMarkdownVersion.file_id == file_id).execute()

    @classmethod
    @DB.connection_context()
    def save(cls, document: dict, file_id: str, service_actor_id: str, actor_ref: str, content: str, expected_version: int) -> dict:
        return cls._save(document, file_id, service_actor_id, actor_ref, content, expected_version)

    @classmethod
    def _save(cls, document: dict, file_id: str, service_actor_id: str, actor_ref: str, content: str, expected_version: int) -> dict:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("content exceeds 16 MiB")
        current = cls.current_row(file_id)
        current_revision = current.revision if current else 0
        if expected_version != current_revision:
            raise MarkdownVersionConflict("markdown version changed")
        revision = current_revision + 1
        digest = hashlib.sha256(encoded).hexdigest()
        version_id = get_uuid()
        object_name = f"{cls._PREFIX}/{file_id}/{revision}-{digest}-{version_id}.md"
        settings.STORAGE_IMPL.put(document["kb_id"], object_name, encoded)
        if not settings.STORAGE_IMPL.obj_exist(document["kb_id"], object_name):
            raise OSError("failed to persist markdown derivative")
        try:
            row = DocumentMarkdownVersion.create(
                id=version_id,
                file_id=file_id,
                document_id=document["id"],
                dataset_id=document["kb_id"],
                revision=revision,
                content_sha256=digest,
                object_name=object_name,
                created_by=service_actor_id,
                actor_ref=actor_ref,
                publish_status="SAVED",
            )
        except IntegrityError as exc:
            settings.STORAGE_IMPL.rm(document["kb_id"], object_name)
            raise MarkdownVersionConflict("markdown version changed") from exc
        except Exception:
            settings.STORAGE_IMPL.rm(document["kb_id"], object_name)
            raise
        return cls.public(row, include_content=True)

    @classmethod
    @DB.connection_context()
    def restore(cls, document: dict, file_id: str, service_actor_id: str, actor_ref: str, revision: int, expected_version: int) -> dict:
        source = cls._query(file_id).where(DocumentMarkdownVersion.revision == revision).first()
        if not source:
            raise MarkdownVersionNotFound("markdown version not found")
        current = cls.current_row(file_id)
        current_revision = current.revision if current else 0
        if expected_version != current_revision:
            raise MarkdownVersionConflict("markdown version changed")
        content = cls._read(source)
        result = cls._save(document, file_id, service_actor_id, actor_ref, content, expected_version)
        restored = cls.current_row(file_id)
        DocumentMarkdownVersion.update(restored_from_revision=revision).where(DocumentMarkdownVersion.id == restored.id).execute()
        result["restored_from_version"] = revision
        return result

    @classmethod
    @DB.connection_context()
    def begin_publish(
        cls,
        document: dict,
        file: dict,
        service_actor_id: str,
        actor_ref: str,
        revision: int,
        expected_version: int,
    ) -> dict:
        return cls._begin_publish(
            document,
            file,
            service_actor_id,
            actor_ref,
            revision,
            expected_version,
        )

    @classmethod
    def _begin_publish(
        cls,
        document: dict,
        file: dict,
        service_actor_id: str,
        actor_ref: str,
        revision: int,
        expected_version: int,
    ) -> dict:
        from api.db import FileType
        from api.db.services.document_service import DocumentService
        from common.constants import ParserType, TaskStatus

        current = cls.current_row(file["id"])
        current_revision = current.revision if current else 0
        if expected_version != current_revision:
            raise MarkdownVersionConflict("markdown version changed")
        target = cls.version_row(file["id"], revision)
        if target is None:
            raise MarkdownVersionNotFound("markdown version not found")
        if target.publish_status in {"PENDING", "ACTIVE"}:
            return cls.publish_public(target, file["id"])
        pending = cls._query(file["id"]).where(
            (DocumentMarkdownVersion.publish_status == "PENDING")
            & (DocumentMarkdownVersion.id != target.id)
        ).first()
        if pending is not None:
            raise MarkdownVersionConflict("another markdown version is being published")
        if target.publish_status == "FAILED" and target.published_document_id:
            cls._cleanup_publication_attempt(target, service_actor_id)
            target = cls.version_row(file["id"], revision)

        content = cls._read(target).encode("utf-8")
        shadow_id = get_uuid()
        location = f"{cls._PUBLICATION_PREFIX}/{file['id']}/{revision}-{target.content_sha256}-{shadow_id}.md"
        settings.STORAGE_IMPL.put(target.dataset_id, location, content)
        if not settings.STORAGE_IMPL.obj_exist(target.dataset_id, location):
            raise OSError("failed to persist Markdown publication")

        shadow = {
            "id": shadow_id,
            "kb_id": target.dataset_id,
            "parser_id": ParserType.NAIVE.value,
            "pipeline_id": None,
            "parser_config": document.get("parser_config") or {},
            "created_by": service_actor_id,
            "type": FileType.DOC.value,
            "name": f".__ragflow_version__{file['id']}_{revision}.md",
            "location": location,
            "source_type": "deerflow-version",
            "suffix": "md",
            "size": len(content),
            "thumbnail": "",
            "content_hash": target.content_sha256[:32],
            "run": TaskStatus.UNSTART.value,
            "status": "1",
            "is_shadow": True,
        }
        inserted_shadow = None
        try:
            inserted_shadow = DocumentService.insert(shadow)
            DocumentMarkdownVersion.update(
                publish_status="PENDING",
                published_document_id=shadow_id,
                replaced_document_id=file.get("active_document_id") or document["id"],
                publish_error=None,
                actor_ref=actor_ref,
            ).where(DocumentMarkdownVersion.id == target.id).execute()
            DocumentService.run(service_actor_id, shadow, {})
        except Exception as exc:
            DocumentMarkdownVersion.update(
                publish_status="FAILED",
                published_document_id=None,
                publish_error=str(exc)[:2000],
            ).where(DocumentMarkdownVersion.id == target.id).execute()
            if inserted_shadow is not None:
                try:
                    DocumentService.remove_document(inserted_shadow, service_actor_id)
                except Exception:
                    logging.exception("Failed to remove unpublished shadow document: %s", shadow_id)
            settings.STORAGE_IMPL.rm(target.dataset_id, location)
            raise
        return cls.publish_public(cls.version_row(file["id"], revision), file["id"])

    @classmethod
    def _cleanup_publication_attempt(
        cls, row: DocumentMarkdownVersion, tenant_id: str
    ) -> None:
        from api.db.services.document_service import DocumentService

        found, document = DocumentService.get_by_id(row.published_document_id)
        if found and document is not None and not DocumentService.remove_document(
            document, tenant_id
        ):
            raise OSError("failed to remove previous publication attempt")
        publication_object = (
            f"{cls._PUBLICATION_PREFIX}/{row.file_id}/{row.revision}-"
            f"{row.content_sha256}-{row.published_document_id}.md"
        )
        settings.STORAGE_IMPL.rm(row.dataset_id, publication_object)
        DocumentMarkdownVersion.update(published_document_id=None).where(
            DocumentMarkdownVersion.id == row.id
        ).execute()

    @classmethod
    @DB.connection_context()
    def refresh_publish(cls, file_id: str, revision: int, tenant_id: str) -> dict:
        return cls._refresh_publish(file_id, revision, tenant_id)

    @classmethod
    def _refresh_publish(cls, file_id: str, revision: int, tenant_id: str) -> dict:
        from api.db.services.document_service import DocumentService
        from common.constants import TaskStatus
        from rag.nlp import search

        row = cls.version_row(file_id, revision)
        if row is None:
            raise MarkdownVersionNotFound("markdown version not found")
        if row.publish_status == "ACTIVE" and row.publish_error == cls._RETIREMENT_PENDING:
            cls._retire_replaced_chunks(row, search.index_name(tenant_id))
            row = cls.version_row(file_id, revision)
        if row.publish_status != "PENDING":
            return cls.publish_public(row, file_id)

        found, document = DocumentService.get_by_id(row.published_document_id)
        if not found or document is None:
            DocumentMarkdownVersion.update(publish_status="FAILED", publish_error="published document is missing").where(DocumentMarkdownVersion.id == row.id).execute()
            return cls.publish_public(cls.version_row(file_id, revision), file_id)

        DocumentService.update_progress_immediately([document.to_dict()])
        found, document = DocumentService.get_by_id(row.published_document_id)
        if not found or document is None:
            raise MarkdownVersionNotFound("published document is missing")
        if str(document.run) == TaskStatus.FAIL.value or (document.progress is not None and document.progress < 0):
            DocumentMarkdownVersion.update(publish_status="FAILED", publish_error=(document.progress_msg or "parse failed")[:2000]).where(DocumentMarkdownVersion.id == row.id).execute()
            return cls.publish_public(cls.version_row(file_id, revision), file_id)
        if str(document.run) != TaskStatus.DONE.value or (document.progress or 0) < 1:
            return cls.publish_public(row, file_id)

        index_name = search.index_name(tenant_id)
        if settings.docStoreConn.index_exist(index_name, row.dataset_id):
            updated = settings.docStoreConn.update({"doc_id": row.published_document_id}, {"available_int": 1}, index_name, row.dataset_id)
            if updated is False:
                raise OSError("failed to activate published index chunks")
        try:
            activated = cls._activate_publication(row, file_id)
        except Exception:
            if settings.docStoreConn.index_exist(index_name, row.dataset_id):
                settings.docStoreConn.update({"doc_id": row.published_document_id}, {"available_int": 0}, index_name, row.dataset_id)
            raise

        if not activated:
            current = cls.version_row(file_id, revision)
            if current is None:
                raise MarkdownVersionNotFound("markdown version not found")
            if current.publish_status == "ACTIVE":
                cls._retire_replaced_chunks(current, index_name)
            return cls.publish_public(cls.version_row(file_id, revision), file_id)

        cls._retire_replaced_chunks(row, index_name)
        return cls.publish_public(cls.version_row(file_id, revision), file_id)

    @classmethod
    def _activate_publication(cls, row: DocumentMarkdownVersion, file_id: str) -> bool:
        from api.db.db_models import Document, File, Knowledgebase

        with DB.atomic():
            claimed = DocumentMarkdownVersion.update(
                publish_status="ACTIVE", publish_error=None
            ).where(
                (DocumentMarkdownVersion.id == row.id)
                & (DocumentMarkdownVersion.publish_status == "PENDING")
            ).execute()
            if claimed != 1:
                return False
            if File.update(active_document_id=row.published_document_id).where(File.id == file_id).execute() != 1:
                raise MarkdownVersionNotFound("logical file is missing")
            Document.update(status="1").where(Document.id == row.published_document_id).execute()
            DocumentMarkdownVersion.update(publish_status="SUPERSEDED").where(
                (DocumentMarkdownVersion.file_id == file_id)
                & (DocumentMarkdownVersion.publish_status == "ACTIVE")
                & (DocumentMarkdownVersion.id != row.id)
            ).execute()
            if Knowledgebase.update(content_revision=Knowledgebase.content_revision + 1).where(
                Knowledgebase.id == row.dataset_id
            ).execute() != 1:
                raise MarkdownVersionNotFound("dataset is missing")
        return True

    @classmethod
    def _retire_replaced_chunks(cls, row: DocumentMarkdownVersion, index_name: str) -> bool:
        if not row.replaced_document_id or row.replaced_document_id == row.published_document_id:
            return True
        try:
            if not settings.docStoreConn.index_exist(index_name, row.dataset_id):
                return True
            retired = settings.docStoreConn.update(
                {"doc_id": row.replaced_document_id},
                {"available_int": 0},
                index_name,
                row.dataset_id,
            )
            if retired is False:
                raise OSError("index update returned false")
        except Exception:
            logging.exception("Previous published index retirement failed: document=%s", row.replaced_document_id)
            DocumentMarkdownVersion.update(publish_error=cls._RETIREMENT_PENDING).where(
                DocumentMarkdownVersion.id == row.id
            ).execute()
            return False
        DocumentMarkdownVersion.update(publish_error=None).where(
            (DocumentMarkdownVersion.id == row.id)
            & (DocumentMarkdownVersion.publish_error == cls._RETIREMENT_PENDING)
        ).execute()
        return True

    @classmethod
    def publish_public(cls, row: DocumentMarkdownVersion, file_id: str) -> dict:
        active = cls.active_row(file_id)
        return {
            "version": row.revision,
            "active_version": active.revision if active else None,
            "publish_status": row.publish_status,
            "publish_error": row.publish_error,
            "actor_ref": row.actor_ref,
            "created_at": row.create_date,
            "content_sha256": row.content_sha256,
        }

    @classmethod
    def public(cls, row: DocumentMarkdownVersion, *, include_content: bool = False) -> dict:
        result = {
            "version": row.revision,
            "content_sha256": row.content_sha256,
            "created_at": row.create_date,
            "actor_ref": row.actor_ref,
            "restored_from_version": row.restored_from_revision,
            "publish_status": row.publish_status,
            "publish_error": row.publish_error,
        }
        if include_content:
            result["content"] = cls._read(row)
        return result
