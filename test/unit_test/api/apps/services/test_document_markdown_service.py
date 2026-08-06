from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from api.apps.services.document_markdown_service import DocumentMarkdownService, MarkdownVersionConflict
from api.db.db_models import Document, DocumentMarkdownVersion, File, Knowledgebase
from common import settings
from common.constants import TaskStatus


def test_public_markdown_version_omits_storage_and_document_identifiers(monkeypatch):
    row = SimpleNamespace(
        id="version-id",
        file_id="file-1",
        revision=3,
        content_sha256="digest",
        create_date="2026-07-27T10:00:00",
        created_by="actor",
        actor_ref="deerflow-user-1",
        restored_from_revision=1,
        publish_status="ACTIVE",
        published_document_id="published-document-id",
        publish_error=None,
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
    assert "file_id" not in result
    assert "published_document_id" not in result
    assert result["actor_ref"] == "deerflow-user-1"
    assert result["publish_status"] == "ACTIVE"


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
    first_document = {"id": "doc-1", "kb_id": "dataset-1"}
    replacement_document = {"id": "doc-2", "kb_id": "dataset-1"}
    save = DocumentMarkdownService._save

    with database.bind_ctx([DocumentMarkdownVersion]):
        database.create_tables([DocumentMarkdownVersion])
        first = save(first_document, "file-1", "service-tenant", "deerflow-user", "# first", 0)
        second = save(replacement_document, "file-1", "service-tenant", "deerflow-user", "# second", 1)

        assert first["version"] == 1
        assert second["version"] == 2
        assert first["content_sha256"] != second["content_sha256"]
        assert [item["version"] for item in DocumentMarkdownService.versions("file-1")] == [2, 1]
        assert DocumentMarkdownVersion.select().count() == 2
        with pytest.raises(MarkdownVersionConflict):
            save(replacement_document, "file-1", "service-tenant", "deerflow-user", "stale", 1)
    database.close()


def test_delete_all_removes_storage_objects_before_database_rows(monkeypatch):
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
            file_id="file-1",
            document_id="doc-1",
            dataset_id="dataset-1",
            revision=1,
            content_sha256="a" * 64,
            object_name="markdown/version-1.md",
            created_by="actor",
            actor_ref="deerflow-user",
            publish_status="ACTIVE",
            published_document_id="shadow-1",
        )

        DocumentMarkdownService._delete_all("file-1")

        assert events == [
            ("storage", "dataset-1", "markdown/version-1.md", 1),
            (
                "storage",
                "dataset-1",
                f"markdown-publications/file-1/1-{'a' * 64}-shadow-1.md",
                1,
            ),
        ]
        assert DocumentMarkdownVersion.select().count() == 0
    database.close()


def test_delete_all_keeps_database_rows_when_storage_cleanup_fails(monkeypatch):
    database = SqliteDatabase(":memory:")

    class Storage:
        @staticmethod
        def rm(_bucket, _object_name):
            raise OSError("storage unavailable")

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    with database.bind_ctx([DocumentMarkdownVersion]):
        database.create_tables([DocumentMarkdownVersion])
        DocumentMarkdownVersion.create(
            id="version-1",
            file_id="file-1",
            document_id="doc-1",
            dataset_id="dataset-1",
            revision=1,
            content_sha256="a" * 64,
            object_name="markdown/version-1.md",
            created_by="actor",
        )

        with pytest.raises(OSError, match="storage unavailable"):
            DocumentMarkdownService._delete_all("file-1")

        assert DocumentMarkdownVersion.select().count() == 1
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
    monkeypatch.setattr(DocumentMarkdownService, "current_row", classmethod(lambda _cls, _file_id: None))
    monkeypatch.setattr("api.apps.services.document_markdown_service.get_uuid", lambda: "losing-version-id")
    monkeypatch.setattr(
        DocumentMarkdownVersion,
        "create",
        lambda **_kwargs: (_ for _ in ()).throw(IntegrityError("duplicate revision")),
    )

    with pytest.raises(MarkdownVersionConflict, match="version changed"):
        DocumentMarkdownService._save(
            {"id": "doc-1", "kb_id": "dataset-1"},
            "file-1",
            "service-tenant",
            "deerflow-user",
            "same content",
            0,
        )

    assert objects == {("dataset-1", "other-writer.md"): b"winner"}


def _publication_database(monkeypatch, *, run=TaskStatus.DONE.value, progress=1):
    database = SqliteDatabase(":memory:")
    models = [Knowledgebase, Document, File, DocumentMarkdownVersion]
    database.bind(models)
    database.create_tables(models)
    Knowledgebase.create(
        id="dataset-1",
        tenant_id="tenant-1",
        name="Dataset",
        embd_id="embedding",
        created_by="tenant-1",
        content_revision=5,
    )
    Document.create(
        id="old-doc",
        kb_id="dataset-1",
        parser_id="naive",
        type="doc",
        created_by="tenant-1",
        name="source.pdf",
        suffix="pdf",
        run=TaskStatus.DONE.value,
        progress=1,
    )
    Document.create(
        id="shadow-doc",
        kb_id="dataset-1",
        parser_id="naive",
        type="doc",
        created_by="tenant-1",
        name="shadow.md",
        suffix="md",
        run=run,
        progress=progress,
        progress_msg="parse failed" if run == TaskStatus.FAIL.value else "",
        is_shadow=True,
    )
    File.create(
        id="file-1",
        parent_id="parent",
        tenant_id="tenant-1",
        created_by="tenant-1",
        name="source.pdf",
        type="pdf",
        active_document_id="old-doc",
    )
    DocumentMarkdownVersion.create(
        id="version-1",
        file_id="file-1",
        document_id="old-doc",
        dataset_id="dataset-1",
        revision=1,
        content_sha256="a" * 64,
        object_name="version-1.md",
        created_by="tenant-1",
        actor_ref="user-1",
        publish_status="ACTIVE",
        published_document_id="old-doc",
    )
    DocumentMarkdownVersion.create(
        id="version-2",
        file_id="file-1",
        document_id="old-doc",
        dataset_id="dataset-1",
        revision=2,
        content_sha256="b" * 64,
        object_name="version-2.md",
        created_by="tenant-1",
        actor_ref="user-2",
        publish_status="PENDING",
        published_document_id="shadow-doc",
        replaced_document_id="old-doc",
    )

    monkeypatch.setattr("api.apps.services.document_markdown_service.DB", database)
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.get_by_id",
        classmethod(lambda _cls, doc_id: (True, Document.get_by_id(doc_id))),
    )
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.update_progress_immediately",
        classmethod(lambda _cls, _documents: None),
    )
    monkeypatch.setattr("rag.nlp.search.index_name", lambda _tenant_id: "index")
    return database


def test_pending_publication_keeps_old_document_active_and_searchable(monkeypatch):
    database = _publication_database(
        monkeypatch, run=TaskStatus.RUNNING.value, progress=0.5
    )
    updates = []

    class DocStore:
        @staticmethod
        def index_exist(*_args):
            return True

        @staticmethod
        def update(*args):
            updates.append(args)
            return True

    monkeypatch.setattr(settings, "docStoreConn", DocStore())

    result = DocumentMarkdownService._refresh_publish("file-1", 2, "tenant-1")

    assert result["publish_status"] == "PENDING"
    assert File.get_by_id("file-1").active_document_id == "old-doc"
    assert DocumentMarkdownVersion.get_by_id("version-1").publish_status == "ACTIVE"
    assert updates == []
    database.close()


def test_successful_publication_switches_active_pointer_and_revision(monkeypatch):
    database = _publication_database(monkeypatch)
    updates = []

    class DocStore:
        @staticmethod
        def index_exist(*_args):
            return True

        @staticmethod
        def update(query, values, *_args):
            updates.append((query, values))
            return True

    monkeypatch.setattr(settings, "docStoreConn", DocStore())

    result = DocumentMarkdownService._refresh_publish("file-1", 2, "tenant-1")

    assert result["publish_status"] == "ACTIVE"
    assert result["active_version"] == 2
    assert File.get_by_id("file-1").active_document_id == "shadow-doc"
    assert Knowledgebase.get_by_id("dataset-1").content_revision == 6
    assert DocumentMarkdownVersion.get_by_id("version-1").publish_status == "SUPERSEDED"
    assert updates == [
        ({"doc_id": "shadow-doc"}, {"available_int": 1}),
        ({"doc_id": "old-doc"}, {"available_int": 0}),
    ]
    database.close()


def test_publication_activation_claim_prevents_duplicate_revision_increment(monkeypatch):
    database = _publication_database(monkeypatch)
    stale_pending = DocumentMarkdownVersion.get_by_id("version-2")

    first = DocumentMarkdownService._activate_publication(stale_pending, "file-1")
    second = DocumentMarkdownService._activate_publication(stale_pending, "file-1")

    assert first is True
    assert second is False
    assert File.get_by_id("file-1").active_document_id == "shadow-doc"
    assert Knowledgebase.get_by_id("dataset-1").content_revision == 6
    database.close()


def test_failed_publication_keeps_old_pointer_and_chunks(monkeypatch):
    database = _publication_database(
        monkeypatch, run=TaskStatus.FAIL.value, progress=-1
    )
    updates = []

    class DocStore:
        @staticmethod
        def index_exist(*_args):
            return True

        @staticmethod
        def update(*args):
            updates.append(args)
            return True

    monkeypatch.setattr(settings, "docStoreConn", DocStore())

    result = DocumentMarkdownService._refresh_publish("file-1", 2, "tenant-1")

    assert result["publish_status"] == "FAILED"
    assert File.get_by_id("file-1").active_document_id == "old-doc"
    assert DocumentMarkdownVersion.get_by_id("version-1").publish_status == "ACTIVE"
    assert Knowledgebase.get_by_id("dataset-1").content_revision == 5
    assert updates == []
    database.close()


def test_previous_index_retirement_failure_is_retried(monkeypatch):
    database = _publication_database(monkeypatch)
    old_attempts = 0

    class DocStore:
        @staticmethod
        def index_exist(*_args):
            return True

        @staticmethod
        def update(query, _values, *_args):
            nonlocal old_attempts
            if query == {"doc_id": "old-doc"}:
                old_attempts += 1
                return old_attempts > 1
            return True

    monkeypatch.setattr(settings, "docStoreConn", DocStore())

    first = DocumentMarkdownService._refresh_publish("file-1", 2, "tenant-1")
    second = DocumentMarkdownService._refresh_publish("file-1", 2, "tenant-1")

    assert first["publish_status"] == "ACTIVE"
    assert first["publish_error"] == DocumentMarkdownService._RETIREMENT_PENDING
    assert second["publish_error"] is None
    assert old_attempts == 2
    assert Knowledgebase.get_by_id("dataset-1").content_revision == 6
    database.close()


def test_parse_start_failure_removes_inserted_shadow_document(monkeypatch):
    target = SimpleNamespace(
        id="version-2",
        dataset_id="dataset-1",
        revision=2,
        content_sha256="b" * 64,
        publish_status="SAVED",
    )
    removed = []
    objects = set()

    class UpdateQuery:
        def where(self, *_args):
            return self

        def execute(self):
            return 1

    class Storage:
        @staticmethod
        def put(bucket, object_name, _content):
            objects.add((bucket, object_name))

        @staticmethod
        def obj_exist(bucket, object_name):
            return (bucket, object_name) in objects

        @staticmethod
        def rm(bucket, object_name):
            objects.discard((bucket, object_name))

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    monkeypatch.setattr(DocumentMarkdownService, "current_row", classmethod(lambda _cls, _file_id: SimpleNamespace(revision=2)))
    monkeypatch.setattr(DocumentMarkdownService, "version_row", classmethod(lambda _cls, _file_id, _revision: target))

    class EmptyQuery:
        def where(self, *_args):
            return self

        def first(self):
            return None

    monkeypatch.setattr(DocumentMarkdownService, "_query", staticmethod(lambda _file_id: EmptyQuery()))
    monkeypatch.setattr(DocumentMarkdownService, "_read", staticmethod(lambda _row: "content"))
    monkeypatch.setattr(DocumentMarkdownVersion, "update", lambda **_kwargs: UpdateQuery())
    monkeypatch.setattr("api.apps.services.document_markdown_service.get_uuid", lambda: "shadow-doc")
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.insert",
        classmethod(lambda _cls, shadow: SimpleNamespace(**shadow)),
    )
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.run",
        classmethod(lambda _cls, *_args: (_ for _ in ()).throw(RuntimeError("queue unavailable"))),
    )
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.remove_document",
        classmethod(lambda _cls, document, tenant_id: removed.append((document.id, tenant_id)) or True),
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        DocumentMarkdownService._begin_publish(
            {"id": "old-doc", "parser_config": {}},
            {"id": "file-1", "active_document_id": "old-doc"},
            "tenant-1",
            "deer-user",
            2,
            2,
        )

    assert removed == [("shadow-doc", "tenant-1")]
    assert objects == set()


def test_failed_publication_retry_cleans_previous_shadow_and_object(monkeypatch):
    row = SimpleNamespace(
        id="version-2",
        file_id="file-1",
        dataset_id="dataset-1",
        revision=2,
        content_sha256="b" * 64,
        published_document_id="failed-shadow",
    )
    removed = []

    class UpdateQuery:
        def where(self, *_args):
            return self

        def execute(self):
            return 1

    class Storage:
        @staticmethod
        def rm(bucket, object_name):
            removed.append(("object", bucket, object_name))

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    monkeypatch.setattr(DocumentMarkdownVersion, "update", lambda **_kwargs: UpdateQuery())
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.get_by_id",
        classmethod(lambda _cls, _doc_id: (True, SimpleNamespace(id="failed-shadow"))),
    )
    monkeypatch.setattr(
        "api.db.services.document_service.DocumentService.remove_document",
        classmethod(lambda _cls, document, tenant_id: removed.append(("document", document.id, tenant_id)) or True),
    )

    DocumentMarkdownService._cleanup_publication_attempt(row, "tenant-1")

    assert removed == [
        ("document", "failed-shadow", "tenant-1"),
        (
            "object",
            "dataset-1",
            f"markdown-publications/file-1/2-{'b' * 64}-failed-shadow.md",
        ),
    ]
