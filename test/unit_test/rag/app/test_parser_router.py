import pytest

from rag.app.parser_router import effective_parser_id, uses_table_row_tasks


@pytest.mark.parametrize(
    "filename",
    [
        "document.pdf",
        "image.png",
        "image.jpeg",
        "image.jpg",
        "image.jp2",
        "image.webp",
        "image.gif",
        "image.bmp",
        "image.tiff",
        "document.docx",
        "slides.pptx",
        "sheet.xlsx",
    ],
)
def test_mineru_inputs_route_to_naive_chunker(filename):
    assert effective_parser_id("picture", filename) == "naive"
    assert effective_parser_id("presentation", filename) == "naive"
    assert effective_parser_id("table", filename) == "naive"


def test_other_inputs_keep_the_selected_parser():
    assert effective_parser_id("picture", "image.tif") == "picture"
    assert effective_parser_id("presentation", "slides.ppt") == "presentation"
    assert effective_parser_id("table", "sheet.xls") == "table"


def test_mineru_xlsx_does_not_use_legacy_table_row_tasks():
    assert uses_table_row_tasks("table", "sheet.xlsx") is False
    assert uses_table_row_tasks("table", "sheet.xls") is True
    assert uses_table_row_tasks("naive", "sheet.xlsx") is False
