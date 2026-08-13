import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.mineru_formats import is_mineru_file
from rag.app.parser_router import effective_parser_id


@pytest.fixture(scope="module")
def get_parser():
    source_path = Path(__file__).resolve().parents[5] / "api" / "db" / "services" / "file_service.py"
    parsed = ast.parse(source_path.read_text(encoding="utf-8"))
    file_service = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "FileService")
    function = next(node for node in file_service.body if isinstance(node, ast.FunctionDef) and node.name == "get_parser")
    namespace = {
        "re": re,
        "FileType": SimpleNamespace(VISUAL="visual", AURAL="aural"),
        "ParserType": SimpleNamespace(PICTURE=SimpleNamespace(value="picture"), AUDIO=SimpleNamespace(value="audio"), PRESENTATION=SimpleNamespace(value="presentation"), EMAIL=SimpleNamespace(value="email")),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)  # noqa: S102
    function = namespace["get_parser"].__func__
    function.__globals__.update(is_mineru_file=is_mineru_file, effective_parser_id=effective_parser_id)
    return function


@pytest.mark.parametrize("filename", ["report.pdf", "scan.jp2", "scan.tiff", "report.docx", "slides.pptx", "sheet.xlsx"])
def test_mineru_inputs_are_stored_with_naive_parser(get_parser, filename):
    assert get_parser("visual", filename, "table") == "naive"


def test_non_mineru_inputs_keep_existing_parser_rules(get_parser):
    assert get_parser("visual", "scan.tif", "table") == "picture"
    assert get_parser("doc", "slides.ppt", "table") == "presentation"
    assert get_parser("doc", "sheet.xls", "table") == "table"
