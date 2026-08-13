import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.mineru_formats import is_mineru_file

_REPO = Path(__file__).resolve().parents[5]
_PARSER_PATH = _REPO / "rag" / "flow" / "parser" / "parser.py"


def _parser_method(name):
    parsed = ast.parse(_PARSER_PATH.read_text(encoding="utf-8"))
    parser_class = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "Parser")
    method = next(node for node in parser_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    return method


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
def test_dataflow_invoke_routes_every_mineru_suffix_before_family_dispatch(filename):
    calls = []

    class FakeSelf:
        _canvas = SimpleNamespace(_doc_id=None, _kb_id=None)
        _param = SimpleNamespace(setups={})

        def __getattr__(self, name):
            if name.startswith("_"):
                return lambda *_args, **_kwargs: calls.append(name)
            raise AttributeError(name)

        @staticmethod
        def output():
            return {}

    class Upstream:
        @staticmethod
        def model_validate(kwargs):
            return SimpleNamespace(name=kwargs["name"], file={"created_by": "user", "id": "file"})

    async def run_in_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def save_image(*_args, **_kwargs):
        return None

    namespace = {
        "ParserFromUpstream": Upstream,
        "FileService": SimpleNamespace(get_blob=lambda *_args: b"input"),
        "is_mineru_file": is_mineru_file,
        "thread_pool_exec": run_in_thread,
        "asyncio": asyncio,
        "image2id": save_image,
        "partial": lambda function, **_kwargs: function,
        "settings": SimpleNamespace(STORAGE_IMPL=SimpleNamespace(put=lambda *_args, **_kwargs: None)),
        "get_uuid": lambda: "id",
    }
    method = _parser_method("_invoke")
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(_PARSER_PATH), "exec"), namespace)  # noqa: S102

    asyncio.run(namespace["_invoke"](FakeSelf(), name=filename, file={"created_by": "user", "id": "file"}))

    assert calls == ["_mineru"]


@pytest.mark.parametrize(
    ("filename", "family"),
    [("slides.ppt", "_slides"), ("sheet.xls", "_spreadsheet"), ("data.csv", "_spreadsheet"), ("document.doc", "_doc")],
)
def test_dataflow_keeps_non_mineru_formats_on_existing_handlers(filename, family):
    calls = []

    class FakeSelf:
        _canvas = SimpleNamespace(_doc_id=None, _kb_id=None)
        _param = SimpleNamespace(
            setups={
                "slides": {"suffix": ["ppt"]},
                "spreadsheet": {"suffix": ["xls", "csv"]},
                "doc": {"suffix": ["doc"]},
            }
        )

        def __getattr__(self, name):
            if name.startswith("_"):
                return lambda *_args, **_kwargs: calls.append(name)
            raise AttributeError(name)

        @staticmethod
        def output():
            return {}

    class Upstream:
        @staticmethod
        def model_validate(kwargs):
            return SimpleNamespace(name=kwargs["name"], file={"created_by": "user", "id": "file"})

    async def run_in_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    namespace = {
        "ParserFromUpstream": Upstream,
        "FileService": SimpleNamespace(get_blob=lambda *_args: b"input"),
        "is_mineru_file": is_mineru_file,
        "thread_pool_exec": run_in_thread,
        "asyncio": asyncio,
        "image2id": lambda *_args, **_kwargs: None,
        "partial": lambda function, **_kwargs: function,
        "settings": SimpleNamespace(STORAGE_IMPL=SimpleNamespace(put=lambda *_args, **_kwargs: None)),
        "get_uuid": lambda: "id",
    }
    method = _parser_method("_invoke")
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(_PARSER_PATH), "exec"), namespace)  # noqa: S102

    asyncio.run(namespace["_invoke"](FakeSelf(), name=filename, file={"created_by": "user", "id": "file"}))

    assert calls == [family]


def test_dataflow_mineru_emits_shared_json_contract():
    captured = {}

    class MinerUParser:
        def __init__(self):
            self.outlines = []

        @staticmethod
        def parse_pdf(**kwargs):
            captured.update(kwargs)
            return [("Body", "text", "position")], [((None, "<table>value</table>"), [[0, 1, 2, 3, 4]])]

        @staticmethod
        def extract_positions(_value):
            return [([0], 1, 2, 3, 4)]

    class FakeSelf:
        callback = staticmethod(lambda *_args: None)
        _canvas = SimpleNamespace(_tenant_id="tenant")
        _param = SimpleNamespace(setups={"pdf": {"parse_method": "MinerU", "lang": "Chinese"}})

        def __init__(self):
            self.outputs = {}

        def set_output(self, key, value):
            self.outputs[key] = value

    method = _parser_method("_mineru")
    namespace = {
        "random": SimpleNamespace(randint=lambda *_args: 1),
        "re": re,
        "LLMType": SimpleNamespace(OCR="ocr"),
        "get_first_provider_model_name": lambda *_args: "mineru-model",
        "ensure_mineru_from_env": lambda *_args: None,
        "get_model_config_from_provider_instance": lambda *_args: {},
        "LLMBundle": lambda *_args, **_kwargs: SimpleNamespace(mdl=MinerUParser()),
        "mineru_pipeline_boxes": lambda parser, lines, media: [
            {"text": lines[0][0], "layout_type": lines[0][1], "positions": [[1, 1, 2, 3, 4]]},
            {"text": media[0][0][1], "layout_type": "table", "positions": [[1, 1, 2, 3, 4]]},
        ],
        "enhance_media_sections_with_vision": lambda *_args, **_kwargs: None,
        "normalize_pdf_items_metadata": lambda boxes: boxes,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(_PARSER_PATH), "exec"), namespace)  # noqa: S102

    parser = FakeSelf()
    namespace["_mineru"](parser, "report.docx", b"office", file={"id": "file-1"})

    assert captured["filepath"] == "report.docx"
    assert captured["binary"] == b"office"
    assert parser.outputs["output_format"] == "json"
    assert parser.outputs["file"] == {"id": "file-1", "outlines": []}
    assert [item["doc_type_kwd"] for item in parser.outputs["json"]] == ["text", "table"]
