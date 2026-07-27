import gzip
import io
import json

from PIL import Image

from api.apps.services.source_projection_service import SourceProjectionService
from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from rag.utils.source_projection import source_projection_blocks


def test_normalize_pdf_blocks_preserves_order_type_coordinates_and_image_association():
    blocks = SourceProjectionService.normalize_pdf_blocks(
        [
            {"text": "Introduction", "layout_type": "title", "positions": [[1, 10, 20, 30, 40]]},
            {"text": "Figure 1", "layout_type": "figure", "positions": [[1, 10, 20, 50, 60]], "img_id": "dataset-image"},
            {"text": "Body", "layout_type": "text", "positions": [[1, 10, 20, 70, 80]]},
        ]
    )

    assert [(block["ordinal"], block["kind"], block["page"]) for block in blocks] == [(0, "heading", 1), (1, "image", 1), (2, "paragraph", 1)]
    assert blocks[1]["image_id"] == "dataset-image"
    assert blocks[2]["positions"] == [[1, 10, 20, 70, 80]]


def test_normalize_pdf_blocks_preserves_parser_declared_image_ocr_and_caption():
    blocks = SourceProjectionService.normalize_pdf_blocks(
        [
            {"text": "Figure", "layout_type": "figure", "positions": [[1, 0, 0, 10, 10]]},
            {"text": "literal text in the image", "layout_type": "ocr", "positions": [[1, 0, 10, 10, 20]]},
            {"text": "Figure 1: caption", "layout_type": "figure caption", "positions": [[1, 0, 20, 10, 30]]},
        ]
    )

    assert [block["kind"] for block in blocks] == ["image", "image_ocr", "image_caption"]
    assert blocks[1]["parent_block_ref"] == "0"
    assert blocks[2]["parent_block_ref"] == "0"


def test_projection_stream_is_ndjson_with_manifest_and_flat_blocks():
    manifest = {"document_id": "doc-1", "projection_revision": 2, "block_count": 1}
    block = {"block_ref": "0", "ordinal": 0, "kind": "paragraph", "text": "hello"}
    payload = gzip.compress("\n".join([json.dumps(manifest), json.dumps(block)]).encode())

    lines = gzip.decompress(payload).decode().splitlines()

    assert json.loads(lines[0]) == manifest
    assert json.loads(lines[1]) == block


def test_standard_pdf_parser_blocks_preserve_deepdoc_and_mineru_shapes():
    blocks = source_projection_blocks(
        [
            ("DeepDOC body", "@@1\t10\t20\t30\t40##"),
            ("MinerU heading", "title", [[2, 10, 20, 30, 40]]),
        ],
        [((None, "<table><tr><td>cell</td></tr></table>"), [[3, 10, 20, 30, 40]])],
    )

    normalized = SourceProjectionService.normalize_pdf_blocks(blocks)

    assert [block["kind"] for block in normalized] == ["paragraph", "heading", "table"]
    assert normalized[1]["page"] == 2
    assert normalized[2]["positions"] == [[3, 10, 20, 30, 40]]


def test_source_projection_unifies_body_tables_and_figures_in_pdf_order():
    image = object()
    blocks = source_projection_blocks(
        [
            ("later body", "text", [[2, 10, 20, 100, 120]]),
            ("earlier heading", "title", [[1, 10, 20, 20, 40]]),
        ],
        [
            ((image, "Figure 1: result"), [[1, 10, 20, 60, 90]]),
            ((None, "<table><tr><td>value</td></tr></table>"), [[2, 10, 20, 40, 80]]),
        ],
    )

    assert [block["layout_type"] for block in blocks] == ["title", "figure", "table", "text"]
    assert blocks[1]["image"] is image


def test_source_projection_keeps_image_only_figures():
    image = object()

    blocks = source_projection_blocks([], [((image, ""), [[3, 10, 20, 30, 40]])])

    assert len(blocks) == 1
    assert blocks[0]["layout_type"] == "figure"
    assert blocks[0]["text"] == ""
    assert blocks[0]["image"] is image


def test_projection_images_are_materialized_in_the_dataset_bucket(monkeypatch):
    stored = {}

    class Storage:
        @staticmethod
        def get(bucket, name):
            assert (bucket, name) == ("imagetemps", "old-image")
            return b"existing-image"

        @staticmethod
        def put(bucket, name, data):
            stored[(bucket, name)] = data

    monkeypatch.setattr("api.apps.services.source_projection_service.settings.STORAGE_IMPL", Storage())
    generated = Image.new("RGB", (2, 2), "white")
    blocks = [
        {"block_ref": "0", "_image": generated, "image_id": None},
        {"block_ref": "1", "image_id": "imagetemps-old-image"},
    ]

    SourceProjectionService._store_block_images("dataset-1", "doc-1", 3, blocks)

    assert blocks[0]["image_id"] == "dataset-1-source-projections/doc-1/3/images/0.png"
    assert blocks[1]["image_id"] == "dataset-1-source-projections/doc-1/3/images/1.png"
    assert stored[("dataset-1", "source-projections/doc-1/3/images/1.png")] == b"existing-image"
    assert Image.open(io.BytesIO(stored[("dataset-1", "source-projections/doc-1/3/images/0.png")])).size == (2, 2)


def test_projection_page_windows_merge_in_global_pdf_order_without_duplicates():
    first_window = SourceProjectionService.normalize_pdf_blocks(
        [
            {"text": "page 1", "layout_type": "text", "positions": [[1, 0, 100, 10, 20]]},
            {"text": "page 2", "layout_type": "text", "positions": [[2, 0, 100, 10, 20]]},
        ]
    )
    second_window = SourceProjectionService.normalize_pdf_blocks(
        [
            {"text": "page 2", "layout_type": "text", "positions": [[2, 0, 100, 10, 20]]},
            {"text": "page 3", "layout_type": "text", "positions": [[3, 0, 100, 10, 20]]},
        ]
    )

    merged = SourceProjectionService.merge_projection_blocks(first_window, second_window)

    assert [block["text"] for block in merged] == ["page 1", "page 2", "page 3"]
    assert [block["ordinal"] for block in merged] == [0, 1, 2]
    assert [block["block_ref"] for block in merged] == ["0", "1", "2"]


def test_projection_page_window_offsets_deepdoc_sections_but_not_global_tables():
    source_blocks = source_projection_blocks(
        [
            ("window page 1", "@@1\t0\t100\t10\t20##"),
            ("window page 2", "text", [[2, 0, 100, 10, 20]]),
        ],
        [((None, "<table><tr><td>global page 13</td></tr></table>"), [[13, 0, 100, 30, 40]])],
        section_page_offset=12,
    )
    blocks = SourceProjectionService.normalize_pdf_blocks(source_blocks)

    assert [block["page"] for block in blocks] == [13, 13, 14]
    assert [block["text"] for block in blocks] == [
        "window page 1",
        "<table><tr><td>global page 13</td></tr></table>",
        "window page 2",
    ]


def test_deepdoc_page_window_restores_global_page_numbers(monkeypatch):
    parser = RAGFlowPdfParser.__new__(RAGFlowPdfParser)
    captured: dict[str, int] = {}

    def load_window(_fnm, _zoom, page_from=0, page_to=0, callback=None):
        captured.update({"from_page": page_from, "to_page": page_to})
        parser.page_from = page_from
        parser.boxes = [{"page_number": 1, "position_tag": "@@1\t10\t20\t30\t40##", "positions": [[1, 10, 20, 30, 40]]}]

    monkeypatch.setattr(parser, "__images__", load_window)
    monkeypatch.setattr(parser, "_layouts_rec", lambda _zoom: None)
    monkeypatch.setattr(parser, "_table_transformer_job", lambda _zoom, auto_rotate: None)
    monkeypatch.setattr(parser, "_text_merge", lambda: None)
    monkeypatch.setattr(parser, "_concat_downward", lambda: None)
    monkeypatch.setattr(parser, "_filter_forpages", lambda: None)
    monkeypatch.setattr(parser, "_extract_table_figure", lambda *_args: [])
    monkeypatch.setattr(parser, "_RAGFlowPdfParser__filterout_scraps", lambda boxes, _zoom: boxes)

    boxes, _ = parser("document.pdf", from_page=48, to_page=51)

    assert captured == {"from_page": 48, "to_page": 51}
    assert boxes[0]["page_number"] == 49
    assert boxes[0]["positions"] == [[49, 10, 20, 30, 40]]
    assert boxes[0]["position_tag"].startswith("@@49\t")
