from common import settings
from common.constants import TaskStatus

from api.apps.services.corpus_fs_service import CorpusFsService


def test_public_entry_never_exposes_internal_identifiers_or_storage_fields():
    entry = {
        "kind": "file",
        "path": "/documents/handbook.md",
        "name": "handbook.md",
        "size_bytes": 12,
        "_document_id": "internal-document-id",
        "_file_id": "internal-file-id",
        "_content_hash": "internal-hash",
        "location": "bucket/object-key",
    }

    result = CorpusFsService.public_entry(entry)

    assert result == {"kind": "file", "path": "/documents/handbook.md", "name": "handbook.md", "size_bytes": 12}


def test_flat_legacy_storage_name_projects_to_logical_directories():
    file = {"id": "file-1", "name": "公司制度__001_发文管理规定.md", "parent_id": "root"}

    assert CorpusFsService._file_parts(file, {}) == ["公司制度", "001_发文管理规定.md"]


def test_real_file_parent_hierarchy_remains_authoritative():
    parent = {"id": "folder-1", "name": "真实目录", "parent_id": "root"}
    file = {"id": "file-1", "name": "历史__名称.md", "parent_id": "folder-1"}

    assert CorpusFsService._file_parts(file, {"folder-1": parent}) == ["真实目录", "历史__名称.md"]


def test_numbered_legacy_root_filename_is_not_mistaken_for_a_directory():
    file = {"id": "file-1", "name": "002_建议反馈___异常处理.md", "parent_id": "root"}

    assert CorpusFsService._file_parts(file, {}) == ["002_建议反馈___异常处理.md"]


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

    content, source, images = service.content_text({"id": "document-1", "run": TaskStatus.DONE.value, "type": "pdf"})

    assert (content, source) == ("before\n\nafter", "parsed_chunks")
    assert images == [{"image_ref": service._image_ref("document-1", "dataset-1-chunk-1"), "offset": 6}]
    assert "dataset-1-chunk-1" not in str(images)


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
