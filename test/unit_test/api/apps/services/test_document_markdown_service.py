from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from api.apps.services.document_markdown_service import DocumentMarkdownService, MarkdownVersionConflict
from api.db.db_models import DocumentMarkdownVersion
from common import settings


def test_public_markdown_version_omits_storage_and_document_identifiers(monkeypatch):
    row = SimpleNamespace(
        id="version-id",
        revision=3,
        content_sha256="digest",
        create_date="2026-07-27T10:00:00",
        created_by="actor",
        restored_from_revision=1,
        dataset_id="dataset-1",
        object_name="private/object.md",
    )

    class Storage:
        @staticmethod
        def get(bucket, object_name):
            assert (bucket, object_name) == ("dataset-1", "private/object.md")
            return b"# content"

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    result = DocumentMarkdownService.public(row, include_content=True)

    assert result["content"] == "# content"
    assert "dataset_id" not in result
    assert "object_name" not in result
    assert "created_by" not in result


def test_save_creates_immutable_versions_and_checks_expected_version(monkeypatch):
    database = SqliteDatabase(":memory:")
    objects = {}

    class Storage:
        @staticmethod
        def put(bucket, object_name, data):
            objects[(bucket, object_name)] = data

        @staticmethod
        def get(bucket, object_name):
            return objects.get((bucket, object_name))

        @staticmethod
        def obj_exist(bucket, object_name):
            return (bucket, object_name) in objects

        @staticmethod
        def rm(bucket, object_name):
            objects.pop((bucket, object_name), None)

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    document = {"id": "doc-1", "kb_id": "dataset-1"}
    save = DocumentMarkdownService._save

    with database.bind_ctx([DocumentMarkdownVersion]):
        database.create_tables([DocumentMarkdownVersion])
        first = save(document, "actor", "# first", 0)
        second = save(document, "actor", "# second", 1)

        assert first["version"] == 1
        assert second["version"] == 2
        assert first["content_sha256"] != second["content_sha256"]
        assert DocumentMarkdownVersion.select().count() == 2
        with pytest.raises(MarkdownVersionConflict):
            save(document, "actor", "stale", 1)
    database.close()


def test_delete_all_removes_database_rows_before_storage_objects(monkeypatch):
    database = SqliteDatabase(":memory:")
    events = []

    class Storage:
        @staticmethod
        def rm(bucket, object_name):
            events.append(("storage", bucket, object_name, DocumentMarkdownVersion.select().count()))

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    with database.bind_ctx([DocumentMarkdownVersion]):
        database.create_tables([DocumentMarkdownVersion])
        DocumentMarkdownVersion.create(
            id="version-1",
            document_id="doc-1",
            dataset_id="dataset-1",
            revision=1,
            content_sha256="a" * 64,
            object_name="markdown/version-1.md",
            created_by="actor",
        )

        DocumentMarkdownService._delete_all("doc-1")

        assert events == [("storage", "dataset-1", "markdown/version-1.md", 0)]
        assert DocumentMarkdownVersion.select().count() == 0
    database.close()


def test_save_maps_unique_revision_race_without_removing_another_writer_object(monkeypatch):
    objects = {("dataset-1", "other-writer.md"): b"winner"}

    class Storage:
        @staticmethod
        def put(bucket, object_name, data):
            objects[(bucket, object_name)] = data

        @staticmethod
        def obj_exist(bucket, object_name):
            return (bucket, object_name) in objects

        @staticmethod
        def rm(bucket, object_name):
            objects.pop((bucket, object_name), None)

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    monkeypatch.setattr(DocumentMarkdownService, "current_row", classmethod(lambda _cls, _document_id: None))
    monkeypatch.setattr("api.apps.services.document_markdown_service.get_uuid", lambda: "losing-version-id")
    monkeypatch.setattr(
        DocumentMarkdownVersion,
        "create",
        lambda **_kwargs: (_ for _ in ()).throw(IntegrityError("duplicate revision")),
    )

    with pytest.raises(MarkdownVersionConflict, match="version changed"):
        DocumentMarkdownService._save(
            {"id": "doc-1", "kb_id": "dataset-1"},
            "actor",
            "same content",
            0,
        )

    assert objects == {("dataset-1", "other-writer.md"): b"winner"}
