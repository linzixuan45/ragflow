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

    @staticmethod
    def _query(document_id: str):
        return DocumentMarkdownVersion.select().where(DocumentMarkdownVersion.document_id == document_id)

    @classmethod
    def current_row(cls, document_id: str):
        return cls._query(document_id).order_by(DocumentMarkdownVersion.revision.desc()).first()

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
    def current(cls, document_id: str) -> dict | None:
        row = cls.current_row(document_id)
        return cls.public(row, include_content=True) if row else None

    @classmethod
    def versions(cls, document_id: str) -> list[dict]:
        rows = cls._query(document_id).order_by(DocumentMarkdownVersion.revision.desc())
        return [cls.public(row) for row in rows]

    @classmethod
    @DB.connection_context()
    def delete_all(cls, document_id: str) -> None:
        cls._delete_all(document_id)

    @classmethod
    def _delete_all(cls, document_id: str) -> None:
        rows = list(cls._query(document_id))
        DocumentMarkdownVersion.delete().where(DocumentMarkdownVersion.document_id == document_id).execute()
        for row in rows:
            try:
                settings.STORAGE_IMPL.rm(row.dataset_id, row.object_name)
            except Exception:
                logging.exception("Markdown derivative cleanup failed: dataset=%s object=%s", row.dataset_id, row.object_name)

    @classmethod
    @DB.connection_context()
    def save(cls, document: dict, actor_id: str, content: str, expected_version: int) -> dict:
        return cls._save(document, actor_id, content, expected_version)

    @classmethod
    def _save(cls, document: dict, actor_id: str, content: str, expected_version: int) -> dict:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("content exceeds 16 MiB")
        current = cls.current_row(document["id"])
        current_revision = current.revision if current else 0
        if expected_version != current_revision:
            raise MarkdownVersionConflict("markdown version changed")
        revision = current_revision + 1
        digest = hashlib.sha256(encoded).hexdigest()
        version_id = get_uuid()
        object_name = f"{cls._PREFIX}/{document['id']}/{revision}-{digest}-{version_id}.md"
        settings.STORAGE_IMPL.put(document["kb_id"], object_name, encoded)
        if not settings.STORAGE_IMPL.obj_exist(document["kb_id"], object_name):
            raise OSError("failed to persist markdown derivative")
        try:
            row = DocumentMarkdownVersion.create(
                id=version_id,
                document_id=document["id"],
                dataset_id=document["kb_id"],
                revision=revision,
                content_sha256=digest,
                object_name=object_name,
                created_by=actor_id,
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
    def restore(cls, document: dict, actor_id: str, revision: int, expected_version: int) -> dict:
        source = cls._query(document["id"]).where(DocumentMarkdownVersion.revision == revision).first()
        if not source:
            raise MarkdownVersionNotFound("markdown version not found")
        current = cls.current_row(document["id"])
        current_revision = current.revision if current else 0
        if expected_version != current_revision:
            raise MarkdownVersionConflict("markdown version changed")
        content = cls._read(source)
        result = cls._save(document, actor_id, content, expected_version)
        restored = cls.current_row(document["id"])
        DocumentMarkdownVersion.update(restored_from_revision=revision).where(DocumentMarkdownVersion.id == restored.id).execute()
        result["restored_from_version"] = revision
        return result

    @classmethod
    def public(cls, row: DocumentMarkdownVersion, *, include_content: bool = False) -> dict:
        result = {
            "version": row.revision,
            "content_sha256": row.content_sha256,
            "created_at": row.create_date,
            "restored_from_version": row.restored_from_revision,
        }
        if include_content:
            result["content"] = cls._read(row)
        return result
