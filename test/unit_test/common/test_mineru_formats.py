from common.mineru_formats import (
    DOCX_SUFFIXES,
    IMAGE_SUFFIXES,
    MINERU_SUFFIXES,
    OFFICE_SUFFIXES,
    PDF_SUFFIXES,
    PPTX_SUFFIXES,
    XLSX_SUFFIXES,
    is_mineru_file,
    uses_source_projection,
)


def test_mineru_suffix_groups_match_supported_input_contract():
    assert PDF_SUFFIXES == ["pdf"]
    assert IMAGE_SUFFIXES == ["png", "jpeg", "jp2", "webp", "gif", "bmp", "jpg", "tiff"]
    assert DOCX_SUFFIXES == ["docx"]
    assert PPTX_SUFFIXES == ["pptx"]
    assert XLSX_SUFFIXES == ["xlsx"]
    assert OFFICE_SUFFIXES == ["docx", "pptx", "xlsx"]
    assert MINERU_SUFFIXES == {
        "pdf",
        "png",
        "jpeg",
        "jp2",
        "webp",
        "gif",
        "bmp",
        "jpg",
        "tiff",
        "docx",
        "pptx",
        "xlsx",
    }


def test_mineru_file_detection_is_case_insensitive_and_does_not_expand_scope():
    assert is_mineru_file("Report.PDF")
    assert is_mineru_file("slides.PPTX")
    assert is_mineru_file("scan.JP2")
    assert not is_mineru_file("legacy.ppt")
    assert not is_mineru_file("legacy.xls")
    assert not is_mineru_file("image.tif")


def test_source_projection_covers_every_mineru_format_not_only_pdf():
    for filename in ("document.pdf", "document.docx", "slides.pptx", "sheet.xlsx", "scan.png", "scan.JPG"):
        assert uses_source_projection(filename), filename


def test_source_projection_keeps_non_mineru_formats_out():
    for filename in ("legacy.doc", "legacy.ppt", "legacy.xls", "notes.txt", "page.html"):
        assert not uses_source_projection(filename), filename
