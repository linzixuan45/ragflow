import hashlib
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from api.apps.services.corpus_fs_service import CorpusFsError, CorpusFsService, EntryConflictError, MoveCycleError, NotFoundError, PreconditionRequiredError, RevisionConflictError
from api.apps.services.document_markdown_service import DocumentMarkdownService, MarkdownVersionConflict
from api.apps.services.source_projection_service import SourceProjectionService
from api.db.services.file_service import FileService
from common import settings
from common.constants import TaskStatus


def test_public_entry_never_exposes_internal_identifiers_or_storage_fields():
    entry = {
        "kind": "file",
        "path": "/files/handbook.md",
        "name": "handbook.md",
        "size_bytes": 12,
        "_document_id": "internal-document-id",
        "_file_id": "internal-file-id",
        "_content_hash": "internal-hash",
        "location": "bucket/object-key",
    }

    result = CorpusFsService.public_entry(entry)

    assert result == {"kind": "file", "path": "/files/handbook.md", "name": "handbook.md", "size_bytes": 12}


def test_create_folder_returns_path_only_entry(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_writable", lambda path, folder=None: {"path": "/files", "kind": "directory", "_file_id": "private-root"})
    monkeypatch.setattr(FileService, "query", classmethod(lambda _cls, **_kwargs: []))

    class Inserted:
        pass

    monkeypatch.setattr(FileService, "insert", classmethod(lambda _cls, _value: Inserted()))

    result = service.create_folder("/files", "policies")

    assert result == {"kind": "directory", "name": "policies", "path": "/files/policies", "child_count": 0}
    assert "private-root" not in str(result)


def test_uploaded_entries_are_resolved_by_document_id_after_automatic_rename(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    renamed = {
        "kind": "file",
        "path": "/files/resume(1).pdf",
        "name": "resume(1).pdf",
        "_document_id": "doc-new",
        "_file_id": "file-new",
        "size_bytes": 12,
    }
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: (
            {
                "/files/resume.pdf": {
                    "kind": "file",
                    "path": "/files/resume.pdf",
                    "name": "resume.pdf",
                    "_document_id": "doc-existing",
                },
                renamed["path"]: renamed,
            },
            {},
        ),
    )

    assert service.uploaded_entries(["doc-new"]) == [
        {
            "kind": "file",
            "path": "/files/resume(1).pdf",
            "name": "resume(1).pdf",
            "size_bytes": 12,
        }
    ]


def test_projection_has_one_canonical_files_root(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "_documents", list)
    monkeypatch.setattr(service, "_dataset_file_root", lambda: {"id": "root"})
    monkeypatch.setattr(service, "_descendant_files", lambda _root_id: {})

    entries, children = service._projection()

    assert set(entries) == {"/", "/files"}
    assert [entry["path"] for entry in children["/"]] == ["/files"]


def test_dataset_file_root_is_resolved_without_mutating_file_hierarchy(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "_dataset", lambda: SimpleNamespace(name="Policies"))
    monkeypatch.setattr(service, "_documents", list)
    monkeypatch.setattr(FileService, "_find_root_folder", classmethod(lambda _cls, _tenant_id: {"id": "tenant-root"}))
    monkeypatch.setattr(FileService, "_find_kb_folder", classmethod(lambda _cls, _tenant_id, _root: {"id": "kb-root"}))
    monkeypatch.setattr(
        FileService,
        "query",
        classmethod(lambda _cls, **kwargs: [SimpleNamespace(to_dict=lambda: {"id": "dataset-root", "name": "Policies"})] if kwargs == {"tenant_id": "tenant-1", "parent_id": "kb-root", "name": "Policies"} else []),
    )
    monkeypatch.setattr(FileService, "init_knowledgebase_docs", classmethod(lambda *_args: pytest.fail("read path must not initialize folders")))
    monkeypatch.setattr(FileService, "new_a_file_from_kb", classmethod(lambda *_args: pytest.fail("read path must not create folders")))
    monkeypatch.setattr(FileService, "update_by_id", classmethod(lambda *_args: pytest.fail("read path must not rename folders")))

    assert service._dataset_file_root() == {"id": "dataset-root", "name": "Policies"}


@pytest.fixture(autouse=True)
def _corpus_secret(monkeypatch):
    monkeypatch.setenv("RAGFLOW_CORPUS_SECRET", "test-corpus-secret-with-at-least-32-bytes")


def test_content_mutation_increments_revision_only_after_success(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    dataset = type("Dataset", (), {"status": "1", "content_revision": 7})()
    calls = []

    class UpdateQuery:
        def where(self, *_args):
            return self

        def execute(self):
            calls.append("bump")
            return 1

    monkeypatch.setattr(service, "_dataset", lambda: dataset)
    monkeypatch.setattr(service, "_locked_dataset", lambda: dataset, raising=False)
    monkeypatch.setattr("api.apps.services.corpus_fs_service.DB.atomic", nullcontext, raising=False)
    monkeypatch.setattr("api.apps.services.corpus_fs_service.Knowledgebase.update", lambda **_kwargs: UpdateQuery())

    with service.content_mutation(7):
        calls.append("mutate")

    assert calls == ["mutate", "bump"]

    calls.clear()
    with pytest.raises(RuntimeError):
        with service.content_mutation(7):
            calls.append("mutate")
            raise RuntimeError("mutation failed")
    assert calls == ["mutate"]
    with pytest.raises(PreconditionRequiredError), service.content_mutation(0):
        pass
    with pytest.raises(RevisionConflictError), service.content_mutation(6):
        pass


def test_move_rejects_destination_inside_source(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entries = {
        "/files/policies": {"path": "/files/policies", "name": "policies", "kind": "directory", "_file_id": "source-id"},
        "/files/policies/archive": {"path": "/files/policies/archive", "name": "archive", "kind": "directory", "_file_id": "destination-id"},
    }
    monkeypatch.setattr(service, "resolve_writable", lambda path, folder=None: entries[path])

    with pytest.raises(MoveCycleError):
        service.move(["/files/policies"], "/files/policies/archive")


def test_search_scope_rejects_missing_paths_and_keeps_empty_directories_empty(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entries = {
        "/": {"kind": "directory", "path": "/"},
        "/files": {"kind": "directory", "path": "/files"},
        "/files/empty": {"kind": "directory", "path": "/files/empty"},
        "/files/other.md": {"kind": "file", "path": "/files/other.md", "_document_id": "doc-other"},
        "/files/scoped": {"kind": "directory", "path": "/files/scoped"},
        "/files/scoped/inside.md": {"kind": "file", "path": "/files/scoped/inside.md", "_document_id": "doc-inside"},
    }
    monkeypatch.setattr(service, "_projection", lambda: (entries, {}))

    assert service.search_scope("/files/empty") == {}
    assert set(service.search_scope("/files/scoped")) == {"doc-inside"}
    with pytest.raises(NotFoundError, match="path not found"):
        service.search_scope("/files/missing")


def test_find_rejects_a_missing_scope(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: ({"/files": {"kind": "directory", "path": "/files"}}, {}),
    )

    with pytest.raises(NotFoundError, match="path not found"):
        service.find("report", "/files/missing", 20)


def test_search_mode_controls_vector_weight():
    assert CorpusFsService.search_vector_weight("semantic", 0.3) == 1.0
    assert CorpusFsService.search_vector_weight("hybrid", 0.3) == 0.3


def test_parsed_content_projects_opaque_image_reference_without_image_id(monkeypatch):
    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {
                "chunk-1": {"content_with_weight": "before", "img_id": "dataset-1-chunk-1"},
                "chunk-2": {"content_with_weight": "after", "img_id": ""},
            }

    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    content, source, images, blocks = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"})

    assert (content, source) == ("before\n\nafter", "parsed_chunks")
    assert images == [{"image_ref": service._image_ref("document-1", "dataset-1-chunk-1"), "offset": 6}]
    assert blocks[0] == {"block_ref": service._chunk_ref("document-1", "chunk-1"), "content": "before", "start": 0, "end": 6, "image_ref": images[0]["image_ref"]}
    assert "chunk-1" not in blocks[0]["block_ref"]
    assert "dataset-1-chunk-1" not in str(images)


def test_parsed_content_sorts_by_first_pdf_position_not_doc_store_order(monkeypatch):
    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {
                "late": {"content_with_weight": "second", "position_int": [[1, 20, 100, 300, 340]], "page_num_int": [1], "top_int": [300]},
                "early": {"content_with_weight": "first", "position_int": [[1, 20, 100, 120, 160]], "page_num_int": [1], "top_int": [120]},
                "next-page": {"content_with_weight": "third", "position_int": [[2, 20, 100, 20, 60]], "page_num_int": [2], "top_int": [20]},
            }

    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    content, _, _, blocks = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"})

    assert content == "first\n\nsecond\n\nthird"
    assert [block["content"].lstrip() for block in blocks] == ["first", "second", "third"]


def test_image_data_requires_a_reference_for_an_image_in_the_document(monkeypatch):
    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {"chunk-1": {"content_with_weight": "caption", "img_id": "dataset-1-chunk-1.png"}}

    class Storage:
        @staticmethod
        def get(bucket, name):
            assert (bucket, name) == ("dataset-1", "chunk-1.png")
            return b"image-bytes"

    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    document = {"id": "document-1"}

    assert service.image_data(document, service._image_ref("document-1", "dataset-1-chunk-1.png")) == (b"image-bytes", "chunk-1.png")


def test_pdf_content_prefers_active_source_projection_and_filters_blocks(monkeypatch):
    monkeypatch.setattr(
        SourceProjectionService,
        "active",
        classmethod(
            lambda _cls, _document: (
                {"projection_revision": 4},
                [
                    {"block_ref": "0", "ordinal": 0, "kind": "heading", "level": 1, "page": 1, "positions": [[1, 1, 2, 3, 4]], "text": "Title", "parent_block_ref": None, "image_id": None},
                    {"block_ref": "1", "ordinal": 1, "kind": "paragraph", "page": 2, "positions": [[2, 1, 2, 3, 4]], "text": "Body", "parent_block_ref": None, "image_id": None},
                ],
            )
        ),
    )
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    content, source, images, blocks = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"}, page=2, kinds={"paragraph"})

    assert content == "Body"
    assert source == "source_projection:4"
    assert images == []
    assert blocks == [{"block_ref": "1", "ordinal": 1, "kind": "paragraph", "page": 2, "positions": [[2, 1, 2, 3, 4]], "parent_block_ref": None, "content": "Body", "start": 0, "end": 4}]


def test_pdf_content_exposes_source_projection_heading_level(monkeypatch):
    monkeypatch.setattr(
        SourceProjectionService,
        "active",
        classmethod(
            lambda _cls, _document: (
                {"projection_revision": 4},
                [
                    {
                        "block_ref": "0",
                        "ordinal": 0,
                        "kind": "heading",
                        "level": 2,
                        "page": 1,
                        "positions": [[1, 1, 2, 3, 4]],
                        "text": "Prior Work",
                        "parent_block_ref": None,
                        "image_id": None,
                    }
                ],
            )
        ),
    )
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    _, _, _, blocks = service.content_text(
        {"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"}
    )

    assert blocks[0]["level"] == 2


def test_content_page_keeps_blocks_whole_across_byte_limit():
    blocks = [
        {"block_ref": "a", "content": "a" * 20, "start": 0, "end": 20},
        {"block_ref": "b", "content": "b" * 20, "start": 20, "end": 40},
        {"block_ref": "c", "content": "c" * 20, "start": 40, "end": 60},
    ]

    first, next_index = CorpusFsService.content_page(blocks, 0, 32)
    second, final_index = CorpusFsService.content_page(blocks, next_index, 32)

    assert [block["block_ref"] for block in first] == ["a"]
    assert [block["block_ref"] for block in second] == ["b"]
    assert final_index == 2
    assert first[0]["content"] == "a" * 20


def test_content_page_returns_oversized_first_block_without_splitting():
    blocks = [{"block_ref": "a", "content": "内容" * 20, "start": 0, "end": 120}]

    page, next_index = CorpusFsService.content_page(blocks, 0, 8)

    assert page == blocks
    assert next_index == 1


def test_content_text_reuses_the_citation_projection_snapshot(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    snapshot = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [
            {
                "block_ref": "8",
                "ordinal": 8,
                "kind": "paragraph",
                "page": 2,
                "positions": [[2, 10, 100, 80, 120]],
                "parent_block_ref": None,
                "text": "versioned evidence",
            }
        ],
    )
    monkeypatch.setattr(
        SourceProjectionService,
        "active",
        classmethod(lambda _cls, _document: pytest.fail("projection snapshot was read twice")),
    )

    content, source, _, blocks = service.content_text(
        {"id": "doc-1", "type": "pdf"},
        citation_context=snapshot,
    )
    target = service.citation_target(
        {"path": "/files/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        block=blocks[0],
        context=snapshot,
    )

    assert content == "versioned evidence"
    assert source == "source_projection:4"
    assert target["projection_revision"] == 4
    assert target["block_ref"] == "8"


def test_citation_target_maps_search_chunk_to_versioned_projection_block(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [
            {"block_ref": "8", "page": 2, "positions": [[2, 10, 100, 20, 60]], "text": "first paragraph"},
            {"block_ref": "9", "page": 2, "positions": [[2, 10, 100, 80, 120]], "text": "matched evidence"},
        ],
    )
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda _cls, _document: projection))

    target = service.citation_target(
        {"path": "/files/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        chunk={"id": "chunk-1", "content_with_weight": "matched evidence", "page_num_int": [2], "position_int": [[2, 10, 100, 80, 120]]},
    )

    assert target == {
        "path": "/files/report.pdf",
        "content_revision": 11,
        "projection_revision": 4,
        "page": 2,
        "block_ref": "9",
        "range": {"start": 0, "end": 16},
        "source_hash": "a" * 64,
    }


def test_citation_target_accepts_retrieval_chunk_fields(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [
            {"block_ref": "8", "page": 1, "positions": [[1, 10, 100, 80, 120]], "text": "matched evidence"},
            {"block_ref": "9", "page": 2, "positions": [[2, 10, 100, 80, 120]], "text": "matched evidence"},
        ],
    )
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda _cls, _document: projection))

    target = service.citation_target(
        {"path": "/files/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        chunk={"chunk_id": "chunk-1", "content_with_weight": "matched evidence", "positions": [[2, 10, 100, 80, 120]]},
    )

    assert target["projection_revision"] == 4
    assert target["block_ref"] == "9"
    assert target["page"] == 2


def test_citation_target_uses_retrieval_chunk_id_when_projection_does_not_match(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [{"block_ref": "8", "page": 1, "positions": [[1, 10, 100, 20, 60]], "text": "unrelated paragraph"}],
    )
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda _cls, _document: projection))

    target = service.citation_target(
        {"path": "/files/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        chunk={"chunk_id": "chunk-1", "content_with_weight": "matched evidence", "positions": []},
    )

    assert target["projection_revision"] is None
    assert target["block_ref"] == service._chunk_ref("doc-1", "chunk-1")


def test_citation_target_falls_back_to_chunk_when_projection_blocks_do_not_match(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [{"block_ref": "8", "page": 2, "positions": [[2, 10, 100, 20, 60]], "text": "unrelated paragraph"}],
    )
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda _cls, _document: projection))

    target = service.citation_target(
        {"path": "/files/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        chunk={"id": "chunk-1", "content_with_weight": "matched evidence", "page_num_int": [2], "position_int": [[2, 200, 300, 80, 120]]},
    )

    assert target["projection_revision"] is None
    assert target["block_ref"] == service._chunk_ref("doc-1", "chunk-1")
    assert target["range"] is None


def test_citation_preview_reads_the_cited_projection_revision(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    document = {"id": "doc-1", "kb_id": "dataset-1", "status": "1", "type": "pdf"}
    monkeypatch.setattr(service, "_documents", lambda: [document])
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: ({"/files/current.pdf": {"kind": "file", "path": "/files/current.pdf", "_document_id": "doc-1"}}, {}),
    )
    calls = []

    def revision(_cls, document_id, projection_revision, source_hash):
        calls.append((document_id, projection_revision, source_hash))
        return (
            {"projection_revision": 3, "source_sha256": "b" * 64},
            [{"block_ref": "5", "ordinal": 5, "kind": "paragraph", "page": 7, "positions": [[7, 1, 2, 3, 4]], "text": "versioned evidence"}],
        )

    monkeypatch.setattr(SourceProjectionService, "revision", classmethod(revision))
    target = {
        "path": "/files/old-name.pdf",
        "content_revision": 9,
        "projection_revision": 3,
        "page": 7,
        "block_ref": "5",
        "range": {"start": 0, "end": 9},
        "source_hash": "b" * 64,
    }

    preview = service.citation_preview("doc-1", target)

    assert calls == [("doc-1", 3, "b" * 64)]
    assert preview["current_path"] == "/files/current.pdf"
    assert preview["content"] == "versioned"
    assert preview["blocks"][0]["block_ref"] == "5"


def test_raw_text_citation_preview_uses_the_source_hash_and_byte_range(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    document = {"id": "doc-1", "kb_id": "dataset-1", "status": "1", "type": "txt"}
    monkeypatch.setattr(service, "_documents", lambda: [document])
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: ({"/files/note.txt": {"kind": "file", "path": "/files/note.txt", "_document_id": "doc-1"}}, {}),
    )
    monkeypatch.setattr("api.apps.services.corpus_fs_service.File2DocumentService.get_storage_address", classmethod(lambda _cls, **_kwargs: ("bucket", "note.txt")))

    class Storage:
        @staticmethod
        def get(_bucket, _object_name):
            return "前言 evidence 后记".encode()

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    source_hash = hashlib.sha256("前言 evidence 后记".encode()).hexdigest()
    target = {
        "path": "/files/note.txt",
        "content_revision": 3,
        "projection_revision": None,
        "page": None,
        "block_ref": "raw:0",
        "range": {"start": 7, "end": 15},
        "source_hash": source_hash,
    }

    assert service.citation_preview("doc-1", target)["content"] == "evidence"


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (TaskStatus.UNSTART.value, "UNSTARTED"),
        (TaskStatus.RUNNING.value, "RUNNING"),
        (TaskStatus.CANCEL.value, "CANCELLED"),
        (TaskStatus.DONE.value, "DONE"),
        (TaskStatus.FAIL.value, "FAILED"),
        (TaskStatus.SCHEDULE.value, "SCHEDULED"),
    ],
)
def test_file_entry_exposes_complete_parse_lifecycle(run, expected):
    entry = CorpusFsService._file_entry(
        "/files/report.pdf",
        "report.pdf",
        {"id": "doc-1", "run": run, "progress": 0.25, "progress_msg": "working"},
        {"id": "file-1"},
    )

    assert entry["parse_status"] == expected
    assert entry["parse_progress"] == 0.25
    assert entry["parse_message"] == "working"


def test_original_data_resolves_storage_without_exposing_internal_ids(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entry = {"path": "/files/report.pdf", "name": "report.pdf", "mime_type": "application/pdf"}
    document = {"id": "doc-1"}
    monkeypatch.setattr(service, "resolve_document", lambda _path: (entry, document))
    monkeypatch.setattr("api.apps.services.corpus_fs_service.File2DocumentService.get_storage_address", classmethod(lambda _cls, **_kwargs: ("bucket", "object")))

    class Storage:
        @staticmethod
        def get(bucket, object_name):
            assert (bucket, object_name) == ("bucket", "object")
            return b"source"

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    assert service.original_data("/files/report.pdf") == (entry, b"source", "object")


def test_save_markdown_maps_version_conflict_to_corpus_revision_conflict(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_document", lambda _path: ({"path": "/files/report.pdf"}, {"id": "doc-1"}))

    def conflict(*_args, **_kwargs):
        raise MarkdownVersionConflict("changed")

    monkeypatch.setattr(DocumentMarkdownService, "save", conflict)

    with pytest.raises(RevisionConflictError):
        service.save_markdown("/files/report.pdf", "updated", 2)


def test_retry_parse_rejects_an_already_running_document(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_document", lambda _path: ({"path": "/files/report.pdf"}, {"id": "doc-1", "run": TaskStatus.RUNNING.value}))

    with pytest.raises(EntryConflictError):
        service.retry_parse("/files/report.pdf")


def test_parse_documents_reports_partial_batch_outcomes(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    retried = []

    def retry(path):
        if path == "/files/running.pdf":
            raise EntryConflictError("document is already being parsed")
        if path == "/files/missing.pdf":
            raise NotFoundError("path not found")
        retried.append(path)
        return {"path": path, "status": "RUNNING"}

    monkeypatch.setattr(service, "retry_parse", retry)

    result = service.parse_documents(
        ["/files/failed.pdf", "/files/running.pdf", "/files/missing.pdf"],
        "retry",
    )

    assert retried == ["/files/failed.pdf"]
    assert result == {
        "action": "retry",
        "success_count": 1,
        "skipped_count": 1,
        "failed_count": 1,
        "items": [
            {"path": "/files/failed.pdf", "status": "RUNNING", "outcome": "succeeded"},
            {
                "path": "/files/running.pdf",
                "status": "UNCHANGED",
                "outcome": "skipped",
                "message": "document is already being parsed",
            },
            {
                "path": "/files/missing.pdf",
                "status": "UNCHANGED",
                "outcome": "failed",
                "message": "path not found",
            },
        ],
    }


def test_parse_documents_rejects_invalid_action_and_empty_batch():
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    with pytest.raises(CorpusFsError):
        service.parse_documents([], "retry")
    with pytest.raises(CorpusFsError):
        service.parse_documents(["/files/report.pdf"], "pause")


def test_pdf_content_keeps_active_projection_readable_during_reparse(monkeypatch):
    monkeypatch.setattr(
        SourceProjectionService,
        "active",
        classmethod(
            lambda _cls, _document: (
                {"projection_revision": 7},
                [
                    {
                        "block_ref": "0",
                        "ordinal": 0,
                        "kind": "paragraph",
                        "page": 1,
                        "positions": [[1, 1, 2, 3, 4]],
                        "text": "previous active projection",
                        "parent_block_ref": None,
                        "image_id": None,
                    }
                ],
            )
        ),
    )
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    content, source, _, _ = service.content_text(
        {"id": "document-1", "run": TaskStatus.RUNNING.value, "type": "pdf"}
    )

    assert content == "previous active projection"
    assert source == "source_projection:7"
