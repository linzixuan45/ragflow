"""Corpus evidence identity, projection media, and public kind regressions."""

import base64
from types import SimpleNamespace

import pytest

from api.apps.services.corpus_citation_service import CorpusCitationService
from api.apps.services.corpus_errors import ImageNotFoundError
from api.apps.services.corpus_fs_service import CorpusFsService
from api.apps.services.corpus_tool_service import CorpusToolService
from api.apps.services.source_projection_service import SourceProjectionService
from common import settings
from common.constants import TaskStatus


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(CorpusFsService, "_image_ref_secret", staticmethod(lambda: b"review-only-secret-32-bytes-value!"))
    return CorpusFsService("tenant", "dataset", "reader")


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["guide.pdf", "guide.docx"])
async def test_search_image_and_its_projection_citation_can_be_read_together(service, monkeypatch, filename):
    chunk_id = "dataset-chunk-preview.jpeg"
    projection_id = "dataset-source-projections/doc/4/images/0.png"
    chunk = {"id": "chunk", "img_id": chunk_id, "content_with_weight": "diagram", "position_int": [[1, 0, 10, 0, 10]]}
    projection = (
        {"projection_revision": 4, "source_sha256": "a" * 64},
        [{"block_ref": "0", "kind": "image", "page": 1, "positions": [[1, 0, 10, 0, 10]], "text": "diagram", "image_id": projection_id}],
    )
    document = {"id": "doc", "name": filename, "type": "pdf" if filename.endswith(".pdf") else "doc"}
    monkeypatch.setattr(service, "_active_projection", lambda _: projection)
    monkeypatch.setattr(service, "_chunk_fields", lambda _: {"chunk": chunk})
    monkeypatch.setattr(SourceProjectionService, "revision", classmethod(lambda _cls, *_args: projection))
    monkeypatch.setattr(settings, "STORAGE_IMPL", SimpleNamespace(get=lambda *_args: b"pixels"))
    monkeypatch.setattr(service, "search_context", lambda _: ({"doc": {"path": "/" + filename}}, {"doc": document}, 1))

    class Recall:
        async def recall(self, *_args, **_kwargs):
            return [{**chunk, "doc_id": "doc", "_scope": "root", "similarity": 0.99}]

    citations = CorpusCitationService(b"test-citation-key")
    tool = CorpusToolService("tenant", service_factory=lambda _: service, recall=Recall(), citation_service=citations)
    response = await tool.search(scopes=[{"scope": "root", "dataset_id": "dataset", "path": "/"}], query="diagram", limit=2)
    item = response["items"][0]
    doc_id, target = citations.resolve_locator(item["citation_ref"], "tenant", "dataset")
    assert doc_id == "doc" and target["projection_revision"] == 4
    image_ref = item["images"][0]["image_ref"]
    assert service.image_data(document, image_ref, citation_target=target)[0] == b"pixels"
    assert image_ref == service._image_ref("doc", projection_id)


def test_active_office_projection_exposes_inline_table_pixels(service, monkeypatch):
    raw = b"\x89PNG\r\n\x1a\ninline-image"
    html = '<table><tr><td><img src="data:image/png;base64,' + base64.b64encode(raw).decode() + '"></td></tr></table>'
    projection = ({"projection_revision": 4, "source_sha256": "a" * 64}, [{"block_ref": "0", "kind": "table", "page": 1, "text": html}])
    monkeypatch.setattr(service, "_active_projection", lambda _: None)
    monkeypatch.setattr(service, "_document_fs_path", lambda _: "/guide.docx")
    monkeypatch.setattr(service, "_chunk_fields", lambda _: {"chunk": {"content_with_weight": html}})
    objects = {}
    monkeypatch.setattr(
        settings,
        "STORAGE_IMPL",
        SimpleNamespace(
            get=lambda bucket, key: objects.get((bucket, key)),
            put=lambda bucket, key, value: objects.__setitem__((bucket, key), value),
            obj_exist=lambda bucket, key: (bucket, key) in objects,
        ),
    )
    document = {"id": "doc", "name": "guide.docx", "type": "doc", "run": TaskStatus.DONE.value}
    fallback_content, _, fallback_images, _ = service.content_text(document)
    assert fallback_images and "data:image" not in fallback_content
    monkeypatch.setattr(service, "_active_projection", lambda _: projection)
    content, source, images, _ = service.content_text(document)
    assert source == "source_projection:4"
    assert images, f"inline pixels were not exposed; data URL remains: {'data:image' in content}"
    assert "data:image" not in content


def test_paragraph_filter_keeps_legacy_text_chunks(service, monkeypatch):
    monkeypatch.setattr(service, "_active_projection", lambda _: None)
    monkeypatch.setattr(service, "_chunk_fields", lambda _: {"chunk": {"doc_type_kwd": "text", "content_with_weight": "body", "page_num_int": [1]}})
    assert service.content_text({"id": "doc", "name": "legacy.pptx", "type": "doc", "run": TaskStatus.DONE.value})[0] == "body"
    content, source, _, _ = service.content_text({"id": "doc", "name": "legacy.pptx", "type": "doc", "run": TaskStatus.DONE.value}, kinds={"paragraph"})
    assert (content, source) == ("body", "parsed_chunks")


def test_projection_inline_media_ranges_and_retired_version_membership(service, monkeypatch):
    def block(payload):
        return {"block_ref": "0", "kind": "table", "page": 1, "text": '前<table><td><img src="data:image/png;base64,' + base64.b64encode(payload).decode() + '"></td></table>后'}

    old = ({"projection_revision": 4, "source_sha256": "a" * 64}, [block(b"old-pixels")])
    new = ({"projection_revision": 5, "source_sha256": "b" * 64}, [block(b"new-pixels")])
    document = {"id": "doc", "name": "guide.docx"}
    entry = {"path": "/guide.docx"}
    objects = {}
    monkeypatch.setattr(
        settings,
        "STORAGE_IMPL",
        SimpleNamespace(
            get=lambda bucket, key: objects.get((bucket, key)),
            put=lambda bucket, key, value: objects.__setitem__((bucket, key), value),
            obj_exist=lambda bucket, key: (bucket, key) in objects,
        ),
    )
    monkeypatch.setattr(service, "_document_fs_path", lambda _: entry["path"])
    monkeypatch.setattr(service, "authorize_citation", lambda _: (document, entry))
    monkeypatch.setattr(service, "_active_projection", lambda _: old)
    monkeypatch.setattr(service, "_chunk_fields", lambda _: pytest.fail("cited projection must never fall back to current chunks"))
    monkeypatch.setattr(SourceProjectionService, "revision", classmethod(lambda _cls, doc, rev, digest: old if (doc, rev, digest) == ("doc", 4, "a" * 64) else None))
    content, _, images, blocks = service.content_text(document)
    assert images and "data:image" not in content
    search_target = service.citation_target(entry, document, 1, chunk={"content_with_weight": old[1][0]["text"]}, context=old)
    search_evidence = service.search_evidence("doc", {"content_with_weight": old[1][0]["text"]}, citation_target=search_target, context=old)
    assert search_evidence["snippet"] == content and "data:image" not in search_evidence["snippet"]
    assert search_evidence["images"][0]["image_ref"] == images[0]["image_ref"]
    assert content.encode()[blocks[0]["start"] : blocks[0]["end"]].decode() == blocks[0]["content"]
    visible, _, _ = service.content_page(blocks, 0, 9)
    target = service.citation_target(entry, document, 1, block=visible[0], context=old)
    assert service.citation_preview("doc", target)["content"] == visible[0]["content"]
    # The document manifest now contains assets from both versions.
    monkeypatch.setattr(service, "_active_projection", lambda _: new)
    new_ref = service.content_text(document)[2][0]["image_ref"]
    assert service.image_data(document, images[0]["image_ref"], citation_target=target)[0] == b"old-pixels"
    with pytest.raises(ImageNotFoundError):
        service.image_data(document, new_ref, citation_target=target)
    with pytest.raises(ImageNotFoundError):
        service.image_data(document, new_ref, citation_target={**target, "source_hash": "c" * 64})
    for key in list(objects):
        if objects[key] == b"old-pixels":
            del objects[key]
    # An existing immutable projection can re-materialize its own inline pixels.
    assert service.image_data(document, images[0]["image_ref"], citation_target=target)[0] == b"old-pixels"


@pytest.mark.parametrize("stored,expected", [("text", "paragraph"), (None, "paragraph"), ("image", "image"), ("table", "table")])
def test_chunk_kind_filter_and_result_share_public_kind(service, monkeypatch, stored, expected):
    monkeypatch.setattr(service, "_active_projection", lambda _: None)
    monkeypatch.setattr(service, "_chunk_fields", lambda _: {"chunk": {"doc_type_kwd": stored, "content_with_weight": "body"}})
    _, _, _, blocks = service.content_text({"id": "doc", "run": TaskStatus.DONE.value}, kinds={expected})
    assert blocks[0]["kind"] == expected
