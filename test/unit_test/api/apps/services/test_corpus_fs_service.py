import base64
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import xxhash

from api.apps.services.corpus_context_cache import dataset_context_cache
from api.apps.services.corpus_errors import (
    CorpusFsError,
    EntryConflictError,
    MoveCycleError,
    NotFoundError,
    PreconditionRequiredError,
    RevisionConflictError,
    VersionNotFoundError,
    is_unavailable_io,
)
from api.apps.services.corpus_fs_service import CorpusFsService
from api.apps.services.document_markdown_service import DocumentMarkdownService, MarkdownVersionConflict, MarkdownVersionNotFound
from api.apps.services.source_projection_service import SourceProjectionService
from api.db import FileType
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from common import settings
from common.constants import TaskStatus


def test_public_entry_never_exposes_internal_identifiers_or_storage_fields():
    entry = {
        "kind": "file",
        "path": "/handbook.md",
        "name": "handbook.md",
        "size_bytes": 12,
        "_document_id": "internal-document-id",
        "_file_id": "internal-file-id",
        "_content_hash": "internal-hash",
        "location": "bucket/object-key",
    }

    result = CorpusFsService.public_entry(entry)

    assert result == {"kind": "file", "path": "/handbook.md", "name": "handbook.md", "size_bytes": 12}


def test_create_folder_returns_path_only_entry(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_writable", lambda path, folder=None: {"path": "/", "kind": "directory", "_file_id": "private-root"})
    monkeypatch.setattr(FileService, "query", classmethod(lambda _cls, **_kwargs: []))

    class Inserted:
        pass

    monkeypatch.setattr(FileService, "insert", classmethod(lambda _cls, _value: Inserted()))

    result = service.create_folder("/", "policies")

    assert result == {"kind": "directory", "name": "policies", "path": "/policies", "child_count": 0}
    assert "private-root" not in str(result)


def test_uploaded_entries_are_resolved_by_document_id_after_automatic_rename(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    renamed = {
        "kind": "file",
        "path": "/resume(1).pdf",
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
                "/resume.pdf": {
                    "kind": "file",
                    "path": "/resume.pdf",
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
            "path": "/resume(1).pdf",
            "name": "resume(1).pdf",
            "size_bytes": 12,
        }
    ]


def test_replace_original_requires_matching_hash_and_updates_source(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    source = b"old workbook"
    replacement = b"new workbook"
    current_hash = xxhash.xxh128(source).hexdigest()
    entry = {
        "kind": "file",
        "path": "/report.xlsx",
        "name": "report.xlsx",
        "_file_id": "file-1",
        "_document_id": "doc-1",
        "_content_hash": current_hash,
    }
    document = {
        "id": "doc-1",
        "name": "report.xlsx",
        "run": TaskStatus.DONE.value,
    }
    stored = {("bucket", "report.xlsx"): source}
    updates = []

    monkeypatch.setattr(service, "resolve_document", lambda _path: (entry, document))
    monkeypatch.setattr(
        File2DocumentService,
        "get_storage_address",
        classmethod(lambda _cls, **_kwargs: ("bucket", "report.xlsx")),
    )
    monkeypatch.setattr(
        DocumentService,
        "update_by_id",
        classmethod(lambda _cls, document_id, values: updates.append((document_id, values))),
    )
    monkeypatch.setattr(
        FileService,
        "update_by_id",
        classmethod(lambda _cls, file_id, values: updates.append((file_id, values))),
    )

    class Storage:
        @staticmethod
        def get(bucket, name):
            return stored[(bucket, name)]

        @staticmethod
        def put(bucket, name, data):
            stored[(bucket, name)] = data

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    with pytest.raises(RevisionConflictError):
        service.replace_original("/report.xlsx", replacement, "stale")

    result = service.replace_original("/report.xlsx", replacement, current_hash)

    assert stored[("bucket", "report.xlsx")] == replacement
    assert result["path"] == "/report.xlsx"
    assert result["content_hash"] == xxhash.xxh128(replacement).hexdigest()
    assert ("doc-1", {"size": len(replacement), "content_hash": result["content_hash"]}) in updates
    assert ("file-1", {"size": len(replacement)}) in updates


def test_projection_uses_dataset_root_as_canonical_root(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "_dataset", lambda: SimpleNamespace(content_revision=1))
    monkeypatch.setattr(service, "dataset_context", lambda _dataset: ({}, {}, [], {"id": "root"}))

    entries, children = service._projection()

    assert set(entries) == {"/"}
    assert children["/"] == []


def test_descendant_files_loads_one_query_per_directory_level(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    levels = {
        ("root",): {
            "folder-a": {"id": "folder-a", "parent_id": "root", "type": FileType.FOLDER.value},
            "file-a": {"id": "file-a", "parent_id": "root", "type": FileType.DOC.value},
        },
        ("folder-a",): {
            "file-b": {"id": "file-b", "parent_id": "folder-a", "type": FileType.DOC.value},
        },
    }
    calls: list[tuple[str, ...]] = []

    def fake_files_by_parents(parent_ids):
        calls.append(tuple(sorted(parent_ids)))
        return levels.get(tuple(sorted(parent_ids)), {})

    monkeypatch.setattr(CorpusFsService, "_files_by_parents", staticmethod(fake_files_by_parents))

    files = service._descendant_files("root")

    assert set(files) == {"folder-a", "file-a", "file-b"}
    assert calls == [("root",), ("folder-a",)]


def test_projection_document_selection_prefers_explicit_file_active_pointer():
    linked = [{"id": "old-document"}, {"id": "shadow-document"}]

    assert CorpusFsService._select_active_document({"active_document_id": "shadow-document"}, linked) == {"id": "shadow-document"}
    assert CorpusFsService._select_active_document({}, linked) == {"id": "old-document"}


def _tree_projection():
    entries = {
        "/": {"kind": "directory", "name": "/", "path": "/", "_file_id": "root"},
        "/docs": {"kind": "directory", "name": "docs", "path": "/docs", "_file_id": "folder-docs"},
        "/docs/a.pdf": {"kind": "file", "name": "a.pdf", "path": "/docs/a.pdf", "_document_id": "doc-a", "parse_status": "FAILED"},
        "/docs/nested": {"kind": "directory", "name": "nested", "path": "/docs/nested", "_file_id": "folder-nested"},
        "/docs/nested/b.pdf": {"kind": "file", "name": "b.pdf", "path": "/docs/nested/b.pdf", "_document_id": "doc-b", "parse_status": "DONE"},
        "/top.md": {"kind": "file", "name": "top.md", "path": "/top.md", "_document_id": "doc-top", "parse_status": "RUNNING"},
    }
    children = {
        "/": [entries["/docs"], entries["/top.md"]],
        "/docs": [entries["/docs/a.pdf"], entries["/docs/nested"]],
        "/docs/nested": [entries["/docs/nested/b.pdf"]],
    }
    return entries, children


def _tree_service(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "_projection", _tree_projection)
    return service


def test_tree_recursive_lists_only_files_under_the_scope(monkeypatch):
    service = _tree_service(monkeypatch)

    items, more = service.tree("/", 50, 0, recursive=True)

    assert [item["path"] for item in items] == ["/docs/a.pdf", "/docs/nested/b.pdf", "/top.md"]
    assert all(item["kind"] == "file" for item in items)
    assert more is False


def test_tree_recursive_scopes_to_the_requested_subdirectory(monkeypatch):
    service = _tree_service(monkeypatch)

    items, more = service.tree("/docs", 50, 0, recursive=True)

    assert [item["path"] for item in items] == ["/docs/a.pdf", "/docs/nested/b.pdf"]
    assert more is False


def test_tree_recursive_paginates_and_reports_more(monkeypatch):
    service = _tree_service(monkeypatch)

    first, more_first = service.tree("/", 2, 0, recursive=True)
    second, more_second = service.tree("/", 2, 2, recursive=True)

    assert [item["path"] for item in first] == ["/docs/a.pdf", "/docs/nested/b.pdf"]
    assert more_first is True
    assert [item["path"] for item in second] == ["/top.md"]
    assert more_second is False


def test_tree_without_recursive_keeps_direct_children_only(monkeypatch):
    service = _tree_service(monkeypatch)

    items, more = service.tree("/", 50, 0)

    assert [item["path"] for item in items] == ["/docs", "/top.md"]
    assert items[0]["child_count"] == 2
    assert more is False


def test_dataset_file_root_is_resolved_without_mutating_file_hierarchy(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(FileService, "_find_root_folder", classmethod(lambda _cls, _tenant_id: {"id": "tenant-root"}))
    monkeypatch.setattr(FileService, "_find_kb_folder", classmethod(lambda _cls, _tenant_id, _root: {"id": "kb-root"}))
    monkeypatch.setattr(
        FileService,
        "query",
        classmethod(
            lambda _cls, **kwargs: (
                [SimpleNamespace(to_dict=lambda: {"id": "dataset-root", "name": "Policies"})] if kwargs == {"tenant_id": "tenant-1", "parent_id": "kb-root", "name": "Policies"} else []
            )
        ),
    )
    monkeypatch.setattr(FileService, "init_knowledgebase_docs", classmethod(lambda *_args: pytest.fail("read path must not initialize folders")))
    monkeypatch.setattr(FileService, "new_a_file_from_kb", classmethod(lambda *_args: pytest.fail("read path must not create folders")))
    monkeypatch.setattr(FileService, "update_by_id", classmethod(lambda *_args: pytest.fail("read path must not rename folders")))

    assert service._dataset_file_root(SimpleNamespace(name="Policies"), "kb-root", []) == {"id": "dataset-root", "name": "Policies"}


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
    with pytest.raises(RuntimeError), service.content_mutation(7):
        calls.append("mutate")
        raise RuntimeError("mutation failed")
    assert calls == ["mutate"]
    with pytest.raises(PreconditionRequiredError), service.content_mutation(0):
        pass
    with pytest.raises(RevisionConflictError), service.content_mutation(6):
        pass


def test_content_mutation_reads_the_rows_it_wrote_despite_the_context_cache(monkeypatch):
    """A mutation must see its own writes.

    ``dataset_context`` is cached by ``content_revision``, and that counter only
    moves once the mutation commits. A snapshot taken before the mutation is
    therefore still keyed as current from inside it, so serving the cache hid
    the rows ``FileService.upload_document`` had just inserted: the upload's
    final read raised "uploaded document is not visible" and every Corpus FS
    upload answered 400 INVALID_ARGUMENT.
    """
    dataset_context_cache.clear()
    monkeypatch.setenv("CORPUS_CONTEXT_CACHE", "1")

    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    dataset = type("Dataset", (), {"status": "1", "content_revision": 7})()
    rows = [{"id": "doc-1", "status": "1"}]

    class UpdateQuery:
        def where(self, *_args):
            return self

        def execute(self):
            return 1

    monkeypatch.setattr(service, "_document_rows", lambda: [dict(row) for row in rows])
    monkeypatch.setattr(service, "_dataset_tree", lambda _docs, _dataset: ({"id": "root"}, {}, []))
    monkeypatch.setattr(service, "_dataset", lambda: dataset)
    monkeypatch.setattr(service, "_locked_dataset", lambda: dataset, raising=False)
    monkeypatch.setattr("api.apps.services.corpus_fs_service.DB.atomic", nullcontext, raising=False)
    monkeypatch.setattr("api.apps.services.corpus_fs_service.Knowledgebase.update", lambda **_kwargs: UpdateQuery())

    # Warm the cache with the pre-mutation snapshot under the current revision.
    docs, _files, _links, _root = service.dataset_context(dataset)
    assert set(docs) == {"doc-1"}

    with service.content_mutation(7):
        rows.append({"id": "doc-2", "status": "1"})
        docs, _files, _links, _root = service.dataset_context(dataset)
        assert set(docs) == {"doc-1", "doc-2"}

    assert service._in_mutation is False


def test_move_rejects_destination_inside_source(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entries = {
        "/policies": {"path": "/policies", "name": "policies", "kind": "directory", "_file_id": "source-id"},
        "/policies/archive": {"path": "/policies/archive", "name": "archive", "kind": "directory", "_file_id": "destination-id"},
    }
    monkeypatch.setattr(service, "resolve_writable", lambda path, folder=None: entries[path])

    with pytest.raises(MoveCycleError):
        service.move(["/policies"], "/policies/archive")


def test_search_scope_rejects_missing_paths_and_keeps_empty_directories_empty(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entries = {
        "/": {"kind": "directory", "path": "/"},
        "/empty": {"kind": "directory", "path": "/empty"},
        "/other.md": {"kind": "file", "path": "/other.md", "_document_id": "doc-other"},
        "/scoped": {"kind": "directory", "path": "/scoped"},
        "/scoped/inside.md": {"kind": "file", "path": "/scoped/inside.md", "_document_id": "doc-inside"},
    }
    monkeypatch.setattr(service, "_projection", lambda: (entries, {}))

    assert service.search_scope("/empty") == {}
    assert set(service.search_scope("/scoped")) == {"doc-inside"}
    with pytest.raises(NotFoundError, match="path not found"):
        service.search_scope("/missing")


def test_find_rejects_a_missing_scope(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: ({"/": {"kind": "directory", "path": "/"}}, {}),
    )

    with pytest.raises(NotFoundError, match="path not found"):
        service.find("report", "/missing", 20)


def test_search_mode_controls_vector_weight():
    assert CorpusFsService.search_vector_weight("semantic", 0.3) == 1.0
    assert CorpusFsService.search_vector_weight("hybrid", 0.3) == 0.3


def test_search_evidence_projects_internal_image_id_to_opaque_reference():
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    images = service.search_evidence(
        "document-1",
        {
            "image_id": "dataset-1-source-projections/document-1/3/images/0.png",
        },
        citation_target={},
        context=({}, None),
    )

    assert images["images"] == [
        {
            "image_ref": service._image_ref(
                "document-1",
                "dataset-1-source-projections/document-1/3/images/0.png",
            )
        }
    ]
    assert "source-projections" not in str(images)
    assert service.search_evidence("document-1", {"image_id": ""}, citation_target={}, context=({}, None))["images"] == []


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

    _, _, _, blocks = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"})

    assert blocks[0]["level"] == 2


@pytest.mark.parametrize("filename", ["manual.docx", "slides.pptx", "book.xlsx", "scan.png"])
def test_mineru_formats_read_and_download_the_same_source_projection(monkeypatch, filename):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    image_id = "dataset-1-source-projections/document-1/4/images/1.png"
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [
            {"block_ref": "0", "kind": "paragraph", "page": 1, "text": "other page"},
            {"block_ref": "1", "kind": "image", "page": 2, "text": "", "image_id": image_id},
        ],
    )
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda _cls, _doc: projection))
    monkeypatch.setattr(service, "_chunk_fields", lambda _id: pytest.fail("existing projection was bypassed"))
    monkeypatch.setattr(settings, "STORAGE_IMPL", SimpleNamespace(get=lambda *_args: b"image-bytes"))
    document = {"id": "document-1", "name": filename, "type": "doc", "run": TaskStatus.RUNNING.value}

    context = service.citation_context(document)
    content, source, images, blocks = service.content_text(document, page=2, kinds={"image"}, citation_context=context)

    assert content == ""
    assert source == "source_projection:4"
    assert len(blocks) == len(images) == 1
    assert blocks[0]["page"] == 2
    assert service.image_data(document, images[0]["image_ref"]) == (b"image-bytes", image_id[len("dataset-1-") :])


def test_chunk_fallback_applies_page_and_kind_filters(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(
        service,
        "_chunk_fields",
        lambda _id: {
            "first": {"content_with_weight": "page one", "page_num_int": [1]},
            "second": {"content_with_weight": "caption", "page_num_int": [2], "img_id": "dataset-1-img.png", "doc_type_kwd": "image"},
            "third": {"content_with_weight": "page two text", "page_num_int": [2]},
        },
    )
    content, source, images, blocks = service.content_text({"id": "doc-1", "type": "doc", "run": TaskStatus.DONE.value}, page=2, kinds={"image"})
    assert (content, source) == ("caption", "parsed_chunks")
    assert len(blocks) == len(images) == 1
    assert blocks[0]["page"] == 2


def test_content_page_keeps_blocks_whole_across_byte_limit():
    blocks = [
        {"block_ref": "a", "content": "a" * 20, "start": 0, "end": 20},
        {"block_ref": "b", "content": "b" * 20, "start": 20, "end": 40},
        {"block_ref": "c", "content": "c" * 20, "start": 40, "end": 60},
    ]

    first, next_index, _ = CorpusFsService.content_page(blocks, 0, 32)
    second, final_index, _ = CorpusFsService.content_page(blocks, next_index, 32)

    assert [block["block_ref"] for block in first] == ["a"]
    assert [block["block_ref"] for block in second] == ["b"]
    assert final_index == 2
    assert first[0]["content"] == "a" * 20


def test_content_page_continues_inside_oversized_utf8_block_without_loss():
    text = "内容" * 20
    blocks = [{"block_ref": "a", "content": text, "start": 0, "end": 120}]
    index = offset = 0
    fragments = []
    while index < len(blocks):
        page, index, offset = CorpusFsService.content_page(blocks, index, 8, byte_offset=offset)
        assert len(page[0]["content"].encode()) <= 8
        fragments.append(page[0]["content"])
    assert "".join(fragments) == text


def test_content_page_bounds_empty_image_blocks_and_continues():
    blocks = [{"block_ref": str(i), "content": "", "image_ref": "a" * 64} for i in range(200)]
    page, index, offset = CorpusFsService.content_page(blocks, 0, 1)
    assert len(page) == index == 1
    assert offset == 0


def test_split_block_citation_range_keeps_original_utf8_coordinates():
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    original = {"block_ref": "0", "content": "前言正文", "start": 0, "end": 12}
    _, index, offset = service.content_page([original], 0, 6)
    visible, _, _ = service.content_page([original], index, 6, byte_offset=offset)
    target = service.citation_target({"path": "/guide.pdf"}, {"id": "doc-1"}, 1, block=visible[0], context=({"projection_revision": 4, "source_sha256": "a" * 64}, []))
    assert visible[0]["content"] == "正文"
    assert target["block_ref"] == "0"
    assert target["range"] == {"start": 6, "end": 12}


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
        {"path": "/report.pdf"},
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
        {"path": "/report.pdf"},
        {"id": "doc-1", "type": "pdf"},
        11,
        chunk={"id": "chunk-1", "content_with_weight": "matched evidence", "page_num_int": [2], "position_int": [[2, 10, 100, 80, 120]]},
    )

    assert target == {
        "path": "/report.pdf",
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
        {"path": "/report.pdf"},
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
        {"path": "/report.pdf"},
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
        {"path": "/report.pdf"},
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
        lambda: ({"/current.pdf": {"kind": "file", "path": "/current.pdf", "_document_id": "doc-1"}}, {}),
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
        "path": "/old-name.pdf",
        "content_revision": 9,
        "projection_revision": 3,
        "page": 7,
        "block_ref": "5",
        "range": {"start": 0, "end": 9},
        "source_hash": "b" * 64,
    }

    preview = service.citation_preview("doc-1", target)

    assert calls == [("doc-1", 3, "b" * 64)]
    assert preview["current_path"] == "/current.pdf"
    assert preview["content"] == "versioned"
    assert preview["blocks"][0]["block_ref"] == "5"


def test_raw_text_citation_preview_uses_the_source_hash_and_byte_range(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    document = {"id": "doc-1", "kb_id": "dataset-1", "status": "1", "type": "txt"}
    monkeypatch.setattr(service, "_documents", lambda: [document])
    monkeypatch.setattr(
        service,
        "_projection",
        lambda: ({"/note.txt": {"kind": "file", "path": "/note.txt", "_document_id": "doc-1"}}, {}),
    )
    monkeypatch.setattr("api.apps.services.corpus_fs_service.File2DocumentService.get_storage_address", classmethod(lambda _cls, **_kwargs: ("bucket", "note.txt")))

    class Storage:
        @staticmethod
        def get(_bucket, _object_name):
            return "前言 evidence 后记".encode()

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())
    source_hash = hashlib.sha256("前言 evidence 后记".encode()).hexdigest()
    target = {
        "path": "/note.txt",
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
        "/report.pdf",
        "report.pdf",
        {"id": "doc-1", "run": run, "progress": 0.25, "progress_msg": "working"},
        {"id": "file-1"},
    )

    assert entry["parse_status"] == expected
    assert entry["parse_progress"] == 0.25
    assert entry["parse_message"] == "working"


def test_original_data_resolves_storage_without_exposing_internal_ids(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    entry = {"path": "/report.pdf", "name": "report.pdf", "mime_type": "application/pdf"}
    document = {"id": "active-shadow"}
    entry["_file_id"] = "file-1"
    monkeypatch.setattr(service, "resolve_document", lambda _path: (entry, document))

    def storage_address(_cls, **kwargs):
        assert kwargs == {"file_id": "file-1"}
        return "bucket", "object"

    monkeypatch.setattr(File2DocumentService, "get_storage_address", classmethod(storage_address))

    class Storage:
        @staticmethod
        def get(bucket, object_name):
            assert (bucket, object_name) == ("bucket", "object")
            return b"source"

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    assert service.original_data("/report.pdf") == (entry, b"source", "object")


def test_delete_logical_file_removes_source_shadow_versions_and_objects(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    file = SimpleNamespace(id="file-1", type="pdf")
    source_link = SimpleNamespace(document_id="source-doc")
    documents = {
        "source-doc": SimpleNamespace(id="source-doc"),
        "shadow-doc": SimpleNamespace(id="shadow-doc"),
    }
    events = []

    monkeypatch.setattr(service, "deletion_targets", lambda _paths: (["file-1"], 1, 0))
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda _cls, _file_id: (True, file)))
    monkeypatch.setattr(FileService, "delete", classmethod(lambda _cls, value: events.append(("file", value.id))))
    monkeypatch.setattr(File2DocumentService, "get_by_file_id", classmethod(lambda _cls, _file_id: [source_link]))
    monkeypatch.setattr(File2DocumentService, "get_storage_address", classmethod(lambda _cls, **_kwargs: ("source-bucket", "source-object")))
    monkeypatch.setattr(File2DocumentService, "delete_by_file_id", classmethod(lambda _cls, file_id: events.append(("links", file_id))))
    monkeypatch.setattr(DocumentMarkdownService, "publication_document_ids", classmethod(lambda _cls, file_id: ["shadow-doc"]))
    monkeypatch.setattr(DocumentMarkdownService, "delete_objects", classmethod(lambda _cls, file_id: events.append(("version-objects", file_id))))
    monkeypatch.setattr(DocumentMarkdownService, "delete_rows", classmethod(lambda _cls, file_id: events.append(("version-rows", file_id))))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda _cls, doc_id: (True, documents[doc_id])))
    monkeypatch.setattr(DocumentService, "get_tenant_id", classmethod(lambda _cls, _doc_id: "tenant-1"))
    monkeypatch.setattr(DocumentService, "remove_document", classmethod(lambda _cls, doc, _tenant_id: events.append(("document", doc.id)) or True))

    class Storage:
        @staticmethod
        def rm(bucket, object_name):
            events.append(("source-object", bucket, object_name))

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    assert service.delete(["/report.pdf"]) == {
        "success_count": 1,
        "error_count": 0,
        "failed_paths": [],
    }
    assert events == [
        ("version-objects", "file-1"),
        ("source-object", "source-bucket", "source-object"),
        ("document", "source-doc"),
        ("document", "shadow-doc"),
        ("version-rows", "file-1"),
        ("links", "file-1"),
        ("file", "file-1"),
    ]


def test_delete_logical_file_keeps_source_link_when_version_cleanup_fails(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    file = SimpleNamespace(id="file-1", type="pdf")
    source_link = SimpleNamespace(document_id="source-doc")
    events = []

    monkeypatch.setattr(service, "deletion_targets", lambda _paths: (["file-1"], 1, 0))
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda _cls, _file_id: (True, file)))
    monkeypatch.setattr(FileService, "delete", classmethod(lambda _cls, _file: pytest.fail("file must be retained")))
    monkeypatch.setattr(File2DocumentService, "get_by_file_id", classmethod(lambda _cls, _file_id: [source_link]))
    monkeypatch.setattr(File2DocumentService, "get_storage_address", classmethod(lambda _cls, **_kwargs: ("source-bucket", "source-object")))
    monkeypatch.setattr(File2DocumentService, "delete_by_file_id", classmethod(lambda _cls, _file_id: pytest.fail("source link must be retained")))
    monkeypatch.setattr(DocumentMarkdownService, "publication_document_ids", classmethod(lambda _cls, _file_id: []))
    monkeypatch.setattr(
        DocumentMarkdownService,
        "delete_objects",
        classmethod(lambda _cls, _file_id: (_ for _ in ()).throw(OSError("storage unavailable"))),
    )
    monkeypatch.setattr(DocumentMarkdownService, "delete_rows", classmethod(lambda _cls, _file_id: pytest.fail("version rows must be retained")))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda _cls, _doc_id: (True, SimpleNamespace(id="source-doc"))))
    monkeypatch.setattr(DocumentService, "get_tenant_id", classmethod(lambda _cls, _doc_id: "tenant-1"))
    monkeypatch.setattr(DocumentService, "remove_document", classmethod(lambda _cls, doc, _tenant_id: events.append(("document", doc.id)) or True))

    assert service.delete(["/report.pdf"]) == {
        "success_count": 0,
        "error_count": 1,
        "failed_paths": ["/report.pdf"],
    }
    assert events == []


def test_delete_logical_file_keeps_metadata_when_source_object_cleanup_fails(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    file = SimpleNamespace(id="file-1", type="pdf")
    source_link = SimpleNamespace(document_id="source-doc")
    events = []

    monkeypatch.setattr(service, "deletion_targets", lambda _paths: (["file-1"], 1, 0))
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda _cls, _file_id: (True, file)))
    monkeypatch.setattr(FileService, "delete", classmethod(lambda _cls, _file: pytest.fail("file must be retained on source-object failure")))
    monkeypatch.setattr(File2DocumentService, "get_by_file_id", classmethod(lambda _cls, _file_id: [source_link]))
    monkeypatch.setattr(File2DocumentService, "get_storage_address", classmethod(lambda _cls, **_kwargs: ("source-bucket", "source-object")))
    monkeypatch.setattr(File2DocumentService, "delete_by_file_id", classmethod(lambda _cls, _file_id: pytest.fail("source link must be retained on source-object failure")))
    monkeypatch.setattr(DocumentMarkdownService, "publication_document_ids", classmethod(lambda _cls, _file_id: []))
    monkeypatch.setattr(DocumentMarkdownService, "delete_objects", classmethod(lambda _cls, file_id: events.append(("version-objects", file_id))))
    monkeypatch.setattr(DocumentMarkdownService, "delete_rows", classmethod(lambda _cls, _file_id: pytest.fail("version rows must be retained on source-object failure")))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda _cls, _doc_id: (True, SimpleNamespace(id="source-doc"))))
    monkeypatch.setattr(DocumentService, "get_tenant_id", classmethod(lambda _cls, _doc_id: "tenant-1"))
    monkeypatch.setattr(DocumentService, "remove_document", classmethod(lambda _cls, _doc, _tenant_id: pytest.fail("document must be retained on source-object failure")))

    class Storage:
        @staticmethod
        def rm(bucket, object_name):
            raise OSError("source object unavailable")

    monkeypatch.setattr(settings, "STORAGE_IMPL", Storage())

    result = service.delete(["/report.pdf"])

    assert result == {
        "success_count": 0,
        "error_count": 1,
        "failed_paths": ["/report.pdf"],
    }
    assert events == [("version-objects", "file-1")]


def test_save_markdown_maps_version_conflict_to_corpus_revision_conflict(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_document", lambda _path: ({"path": "/report.pdf", "_file_id": "file-1"}, {"id": "doc-1"}))

    def conflict(*_args, **_kwargs):
        raise MarkdownVersionConflict("changed")

    monkeypatch.setattr(DocumentMarkdownService, "save", conflict)

    with pytest.raises(RevisionConflictError):
        service.save_markdown("/report.pdf", "updated", 2)


def test_publish_markdown_uses_stable_file_and_maps_missing_version(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor", actor_ref="deer-user")
    entry = {"path": "/report.pdf", "_file_id": "file-1"}
    document = {"id": "doc-active"}
    file = SimpleNamespace(to_dict=lambda: {"id": "file-1", "active_document_id": "doc-active"})
    monkeypatch.setattr(service, "resolve_document", lambda _path: (entry, document))
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda _cls, _file_id: (True, file)))

    def missing(*_args, **_kwargs):
        raise MarkdownVersionNotFound("missing")

    monkeypatch.setattr(DocumentMarkdownService, "begin_publish", missing)

    with pytest.raises(VersionNotFoundError):
        service.publish_markdown("/report.pdf", 3, 3)


def test_retry_parse_rejects_an_already_running_document(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "resolve_document", lambda _path: ({"path": "/report.pdf"}, {"id": "doc-1", "run": TaskStatus.RUNNING.value}))

    with pytest.raises(EntryConflictError):
        service.retry_parse("/report.pdf")


def test_parse_documents_reports_partial_batch_outcomes(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    retried = []

    def retry(path):
        if path == "/running.pdf":
            raise EntryConflictError("document is already being parsed")
        if path == "/missing.pdf":
            raise NotFoundError("path not found")
        retried.append(path)
        return {"path": path, "status": "RUNNING"}

    monkeypatch.setattr(service, "retry_parse", retry)

    result = service.parse_documents(
        ["/failed.pdf", "/running.pdf", "/missing.pdf"],
        "retry",
    )

    assert retried == ["/failed.pdf"]
    assert result == {
        "action": "retry",
        "success_count": 1,
        "skipped_count": 1,
        "failed_count": 1,
        "items": [
            {"path": "/failed.pdf", "status": "RUNNING", "outcome": "succeeded"},
            {
                "path": "/running.pdf",
                "status": "UNCHANGED",
                "outcome": "skipped",
                "message": "document is already being parsed",
            },
            {
                "path": "/missing.pdf",
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
        service.parse_documents(["/report.pdf"], "pause")


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

    content, source, _, _ = service.content_text({"id": "document-1", "run": TaskStatus.RUNNING.value, "type": "pdf"})

    assert content == "previous active projection"
    assert source == "source_projection:7"


def test_is_unavailable_io_maps_closed_sockets_not_missing_files():
    assert is_unavailable_io(OSError(9, "Bad file descriptor")) is True
    assert is_unavailable_io(FileNotFoundError("missing object")) is False
    assert is_unavailable_io(OSError("storage unavailable")) is False


class _InlineStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def put(self, dataset_id, object_name, data):
        self.puts.append((dataset_id, object_name))
        self.objects[(dataset_id, object_name)] = data

    def get(self, dataset_id, object_name):
        return self.objects.get((dataset_id, object_name))

    def obj_exist(self, dataset_id, object_name):
        return (dataset_id, object_name) in self.objects


@pytest.mark.parametrize("document_type", ["pdf", "md"])
def test_parsed_content_materializes_inline_images_with_consistent_offsets(monkeypatch, document_type):
    payload = b"\x89PNG-inline"
    encoded = base64.b64encode(payload).decode("ascii")

    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {"chunk-1": {"content_with_weight": f'前<img src="data:image/png;base64,{encoded}">后'}}

    raw_text = f'前<img src="data:image/png;base64,{encoded}">后'
    storage = _InlineStorage({("bucket", "notes.md"): raw_text.encode()})
    monkeypatch.setattr("api.apps.services.corpus_fs_service.File2DocumentService.get_storage_address", classmethod(lambda _cls, **_kwargs: ("bucket", "notes.md")))
    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    monkeypatch.setattr(service, "_document_fs_path", lambda _document_id: "/notes.md")

    content, source, images, blocks = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": document_type})

    object_name = f"inline/{hashlib.sha256(payload).hexdigest()}.png"
    image_id = f"dataset-1-{object_name}"
    assert source == ("raw_text" if document_type == "md" else "parsed_chunks")
    assert "data:image" not in content
    assert f"[[image:dataset/dataset-1/notes.md|image_ref={service._image_ref('document-1', image_id)}]]" in content
    assert storage.objects[("dataset-1", object_name)] == payload
    block = blocks[0]
    assert content.encode("utf-8")[block["start"] : block["end"]] == block["content"].encode("utf-8")
    assert images == [{"image_ref": service._image_ref("document-1", image_id), "offset": block["end"]}]
    assert block["image_ref"] == images[0]["image_ref"]
    assert service.image_data({"id": "document-1"}, images[0]["image_ref"]) == (payload, object_name)


@pytest.mark.parametrize("raw_source", [False, True])
def test_chunk_citation_preview_materializes_before_the_byte_slice(monkeypatch, raw_source):
    payload = b"preview-bytes"
    encoded = base64.b64encode(payload).decode("ascii")
    image_tag = f'<img src="data:image/png;base64,{encoded}">'
    chunk_text = "X" + image_tag + "Y"
    source = chunk_text.encode() if raw_source else b"stored-source-object"
    storage = _InlineStorage({("bucket", "note.txt"): source})

    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {"chunk-1": {"content_with_weight": chunk_text}}

    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    document = {"id": "doc-1", "kb_id": "dataset-1", "status": "1", "type": "txt"}
    monkeypatch.setattr(service, "_documents", lambda: [document])
    monkeypatch.setattr(service, "_projection", lambda: ({"/note.txt": {"kind": "file", "path": "/note.txt", "_document_id": "doc-1"}}, {}))
    monkeypatch.setattr("api.apps.services.corpus_fs_service.File2DocumentService.get_storage_address", classmethod(lambda _cls, **_kwargs: ("bucket", "note.txt")))
    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)

    object_name = f"inline/{hashlib.sha256(payload).hexdigest()}.png"
    image_id = f"dataset-1-{object_name}"
    materialized = f"X[[image:dataset/dataset-1/note.txt|image_ref={service._image_ref('doc-1', image_id)}]]Y"
    target = {
        "path": "/note.txt",
        "content_revision": 2,
        "projection_revision": None,
        "page": None,
        "block_ref": "raw:0" if raw_source else service._chunk_ref("doc-1", "chunk-1"),
        # 区间由 citation_target 在 content_text 的产物（已资产化）上算出。
        "range": {"start": 0, "end": len(materialized.encode())},
        "source_hash": hashlib.sha256(source).hexdigest(),
    }

    preview = service.citation_preview("doc-1", target)

    assert preview["content"] == materialized
    assert preview["blocks"][0]["image_ref"] == service._image_ref("doc-1", image_id)


def test_image_data_resolves_inline_manifest_assets(monkeypatch):
    payload = b"inline-asset-bytes"
    object_name = f"inline/{hashlib.sha256(payload).hexdigest()}.png"
    image_id = f"dataset-1-{object_name}"
    manifest_name = "inline/document-1.index.json"

    class DocStore:
        @staticmethod
        def search(*_args, **_kwargs):
            return object()

        @staticmethod
        def get_fields(*_args, **_kwargs):
            return {}

    storage = _InlineStorage(
        {
            ("dataset-1", manifest_name): json.dumps({"image_ids": [image_id]}).encode("utf-8"),
            ("dataset-1", object_name): payload,
        }
    )
    monkeypatch.setattr(settings, "docStoreConn", DocStore())
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    service = CorpusFsService("tenant-1", "dataset-1", "actor")

    assert service.image_data({"id": "document-1"}, service._image_ref("document-1", image_id)) == (payload, object_name)


def test_versioned_image_read_uses_cited_projection_and_never_current_fallback(monkeypatch):
    from api.apps.services.corpus_errors import ImageNotFoundError

    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    image_id = "dataset-1-source-projections/document-1/4/images/0.png"
    target = {"projection_revision": 4, "source_hash": "a" * 64, "block_ref": "0"}
    document = {"id": "document-1", "name": "manual.docx"}
    monkeypatch.setattr(SourceProjectionService, "active", classmethod(lambda *_args: pytest.fail("must not read latest version")))
    monkeypatch.setattr(
        SourceProjectionService,
        "revision",
        classmethod(lambda _cls, doc, revision, digest: ({"projection_revision": 4}, [{"block_ref": "0", "image_id": image_id}]) if (doc, revision, digest) == ("document-1", 4, "a" * 64) else None),
    )
    monkeypatch.setattr(service, "_chunk_fields", lambda _id: pytest.fail("must not mix current chunks into old evidence"))
    monkeypatch.setattr(settings, "STORAGE_IMPL", SimpleNamespace(get=lambda *_args: b"old-image"))

    assert service.image_data(document, service._image_ref("document-1", image_id), citation_target=target)[0] == b"old-image"
    with pytest.raises(ImageNotFoundError):
        service.image_data(document, service._image_ref("another-document", image_id), citation_target=target)


def test_citation_preview_preserves_image_reference_from_retired_projection(monkeypatch):
    service = CorpusFsService("tenant-1", "dataset-1", "actor")
    image_id = "dataset-1-source-projections/doc-1/4/images/0.png"
    monkeypatch.setattr(service, "authorize_citation", lambda _id: ({"id": "doc-1"}, {"path": "/renamed.docx"}))
    monkeypatch.setattr(SourceProjectionService, "revision", classmethod(lambda *_args: ({}, [{"block_ref": "0", "kind": "image", "text": "", "image_id": image_id}])))

    preview = service.citation_preview("doc-1", {"projection_revision": 4, "source_hash": "a" * 64, "block_ref": "0"})

    assert preview["current_path"] == "/renamed.docx"
    assert preview["blocks"][0]["image_ref"] == service._image_ref("doc-1", image_id)
