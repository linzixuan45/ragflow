import importlib.util
import inspect
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest


def _load_mineru_parser(monkeypatch):
    repo_root = Path(__file__).resolve().parents[4]

    deepdoc_mod = ModuleType("deepdoc")
    deepdoc_mod.__path__ = [str(repo_root / "deepdoc")]
    monkeypatch.setitem(sys.modules, "deepdoc", deepdoc_mod)

    parser_mod = ModuleType("deepdoc.parser")
    parser_mod.__path__ = [str(repo_root / "deepdoc" / "parser")]
    monkeypatch.setitem(sys.modules, "deepdoc.parser", parser_mod)

    pdf_parser_mod = ModuleType("deepdoc.parser.pdf_parser")

    class _RAGFlowPdfParser:
        pass

    pdf_parser_mod.RAGFlowPdfParser = _RAGFlowPdfParser
    monkeypatch.setitem(sys.modules, "deepdoc.parser.pdf_parser", pdf_parser_mod)

    utils_mod = ModuleType("deepdoc.parser.utils")
    utils_mod.extract_pdf_outlines = lambda *_args, **_kwargs: []
    monkeypatch.setitem(sys.modules, "deepdoc.parser.utils", utils_mod)

    module_name = "test_mineru_parser_unit_module"
    module_path = repo_root / "deepdoc" / "parser" / "mineru_parser.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_sanitize_section_text_removes_escaped_html_tags(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    text = "&lt;table&gt;&lt;tr&gt;&lt;td&gt;Alpha&lt;/td&gt;&lt;td&gt;Beta&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"

    sanitized = module.MinerUParser._sanitize_section_text(text)

    assert sanitized == "AlphaBeta"
    assert "<td>" not in sanitized
    assert "</td>" not in sanitized


def test_transfer_to_sections_logs_sections_dropped_after_sanitization(monkeypatch, caplog):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "&lt;td&gt;&lt;/td&gt;",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        }
    ]

    with caplog.at_level(logging.DEBUG, logger=parser.logger.name):
        sections = parser._transfer_to_sections(outputs, parse_method="pipeline")

    assert sections == []
    assert "Skip section after sanitization" in caplog.text
    assert f"type={module.MinerUContentType.TEXT}" in caplog.text


def test_transfer_to_sections_skips_page_chrome_without_duplicating_text(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    fixture_path = Path(__file__).resolve().parents[3] / "fixtures" / "mineru" / "bmw_page_chrome_content_list.json"
    outputs = __import__("json").loads(fixture_path.read_text(encoding="utf-8"))

    sections = parser._transfer_to_sections(outputs, parse_method="raw")
    texts = [section[0] for section in sections]

    assert texts == ["打开和关闭", "车辆装备", "车辆钥匙", "概述", "安全提示"]
    assert texts.count("打开和关闭") == 1
    assert texts.count("概述") == 1
    assert "77" not in texts
    assert "Online Edition for Part no." not in " ".join(texts)


def test_transfer_to_sections_skips_unknown_types_without_duplicating_text(monkeypatch, caplog):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Primary content",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
        {
            "type": "sidebar",
            "text": "Should not repeat previous section",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Next content",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
    ]

    with caplog.at_level(logging.DEBUG, logger=parser.logger.name):
        sections = parser._transfer_to_sections(outputs, parse_method="raw")

    assert [section[0] for section in sections] == ["Primary content", "Next content"]
    assert "Skip unsupported section type=sidebar" in caplog.text


def test_transfer_to_sections_preserves_mineru_text_level_for_source_projection(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "1 Introduction",
            "text_level": 1,
            "page_idx": 0,
            "bbox": (10, 20, 110, 40),
        },
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Body text",
            "page_idx": 0,
            "bbox": (10, 50, 110, 70),
        },
    ]

    sections = parser._transfer_to_sections(outputs, parse_method="raw")

    assert [section[0] for section in sections] == ["1 Introduction", "Body text"]
    assert parser.source_projection_sections == [
        {
            "text": "1 Introduction",
            "layout_type": "title",
            "level": 1,
            "position_tag": "@@1\t10.0\t110.0\t20.0\t40.0##",
            "source_index": 0,
        },
        {
            "text": "Body text",
            "layout_type": "text",
            "position_tag": "@@1\t10.0\t110.0\t50.0\t70.0##",
            "source_index": 1,
        },
    ]


def test_transfer_to_sections_exposes_heading_level_to_pipeline_consumer(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    sections = parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.TEXT,
                "text": "2.1 Prior Work",
                "text_level": 2,
                "page_idx": 2,
                "bbox": (10, 20, 110, 40),
            }
        ],
        parse_method="pipeline",
    )

    assert sections == [("2.1 Prior Work", "title", "@@3\t10.0\t110.0\t20.0\t40.0##", {"level": 2})]


def test_vlm_description_cache_deduplicates_concurrent_content(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    image_path = tmp_path / "image.bin"
    image_path.write_bytes(b"same-image-content")
    module._VLM_DESCRIPTION_CACHE.clear()
    module._VLM_DESCRIPTION_INFLIGHT.clear()
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    class Model:
        tenant_id = "tenant"
        llm_name = "vision-model"
        lang = "English"

    def generate():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=1)
        return "description"

    results = []
    first = threading.Thread(target=lambda: results.append(module._cached_vlm_description(str(image_path), Model(), "prompt", generate)))
    second = threading.Thread(target=lambda: results.append(module._cached_vlm_description(str(image_path), Model(), "prompt", generate)))
    first.start()
    entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert calls == 1
    assert results == ["description", "description"]


def test_vlm_description_cache_separates_models(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    image_path = tmp_path / "image.bin"
    image_path.write_bytes(b"same-image-content")
    module._VLM_DESCRIPTION_CACHE.clear()
    module._VLM_DESCRIPTION_INFLIGHT.clear()

    class Model:
        tenant_id = "tenant"
        lang = "English"

        def __init__(self, name):
            self.llm_name = name

    first = module._cached_vlm_description(str(image_path), Model("model-a"), "prompt", lambda: "a")
    second = module._cached_vlm_description(str(image_path), Model("model-b"), "prompt", lambda: "b")

    assert (first, second) == ("a", "b")


def test_vlm_description_cache_separates_language_and_endpoint(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    image_path = tmp_path / "image.bin"
    image_path.write_bytes(b"same-image-content")
    module._VLM_DESCRIPTION_CACHE.clear()
    module._VLM_DESCRIPTION_INFLIGHT.clear()

    class Model:
        tenant_id = "tenant"
        llm_name = "vision-model"

        def __init__(self, lang, api_base):
            self.lang = lang
            self.model_config = {
                "llm_factory": "OpenAI-API-Compatible",
                "llm_name": self.llm_name,
                "api_base": api_base,
                "model_type": "image2text",
            }

    first = module._cached_vlm_description(str(image_path), Model("English", "http://vision-a/v1"), "prompt", lambda: "a")
    second = module._cached_vlm_description(str(image_path), Model("Chinese", "http://vision-a/v1"), "prompt", lambda: "b")
    third = module._cached_vlm_description(str(image_path), Model("English", "http://vision-b/v1"), "prompt", lambda: "c")

    assert (first, second, third) == ("a", "b", "c")


def test_vlm_description_cache_is_bounded(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    first_path = tmp_path / "first.bin"
    second_path = tmp_path / "second.bin"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    module._VLM_DESCRIPTION_CACHE.clear()
    module._VLM_DESCRIPTION_INFLIGHT.clear()
    monkeypatch.setenv("VLM_DESCRIPTION_CACHE_SIZE", "1")

    module._cached_vlm_description(str(first_path), object(), "prompt", lambda: "first")
    module._cached_vlm_description(str(second_path), object(), "prompt", lambda: "second")

    assert len(module._VLM_DESCRIPTION_CACHE) == 1
    assert next(iter(module._VLM_DESCRIPTION_CACHE.values())) == "second"


def test_mineru_vlm_workers_are_configurable(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    paths = []
    for index in range(3):
        path = tmp_path / f"image-{index}.bin"
        path.write_bytes(str(index).encode())
        paths.append(str(path))
    outputs = [
        {"type": module.MinerUContentType.IMAGE, "img_path": path}
        for path in paths
    ]
    worker_counts = []

    class ImmediateExecutor:
        def __init__(self, max_workers):
            worker_counts.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, function, *args):
            future = module.Future()
            future.set_result(function(*args))
            return future

    picture_module = ModuleType("rag.app.picture")
    picture_module.vision_llm_chunk = lambda **_kwargs: "generated"
    generator_module = ModuleType("rag.prompts.generator")
    generator_module.vision_llm_figure_describe_prompt = lambda: "prompt"
    monkeypatch.setitem(sys.modules, "rag.app.picture", picture_module)
    monkeypatch.setitem(sys.modules, "rag.prompts.generator", generator_module)
    monkeypatch.setattr(module, "ThreadPoolExecutor", ImmediateExecutor)
    monkeypatch.setattr(module, "as_completed", lambda futures: futures)
    monkeypatch.setattr(module, "_cached_vlm_description", lambda *_args: "generated")
    monkeypatch.setenv("VLM_PARALLEL_PER_DOC", "2")

    parser._enhance_images_with_vlm(outputs, object())

    assert worker_counts == [2]
    assert [item["vlm_description"] for item in outputs] == ["generated"] * 3


@pytest.mark.parametrize("text_level", [None, 0, 7, True, "2"])
def test_transfer_to_sections_does_not_infer_heading_from_invalid_text_level(monkeypatch, text_level):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.TEXT,
                "text": "1 Looks Like A Heading",
                "text_level": text_level,
                "page_idx": 0,
                "bbox": (10, 20, 110, 40),
            }
        ],
        parse_method="raw",
    )

    assert parser.source_projection_sections[0]["layout_type"] == "text"
    assert "level" not in parser.source_projection_sections[0]


@pytest.mark.parametrize(
    "backend",
    ["pipeline", "vlm-engine", "hybrid-engine", "vlm-http-client", "hybrid-http-client"],
)
def test_mineru_backend_matches_service_contract(monkeypatch, backend):
    module = _load_mineru_parser(monkeypatch)

    assert module.MinerUBackend(backend).value == backend


def test_mineru_defaults_to_hybrid_engine(monkeypatch):
    module = _load_mineru_parser(monkeypatch)

    assert module.MinerUParseOptions().backend is module.MinerUBackend.HYBRID_ENGINE
    assert inspect.signature(module.MinerUParser.check_installation).parameters["backend"].default == "hybrid-engine"
    assert inspect.signature(module.MinerUParser._read_output).parameters["backend"].default == "hybrid-engine"
    assert inspect.signature(module.MinerUParser.parse_pdf).parameters["backend"].default == "hybrid-engine"


def test_hybrid_engine_does_not_require_server_url(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.example")
    monkeypatch.setattr(parser, "_is_http_endpoint_valid", lambda url: url.endswith("/openapi.json"))

    available, reason = parser.check_installation(backend="hybrid-engine")

    assert available is True
    assert reason == ""


def test_hybrid_http_client_requires_server_url(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.example")
    monkeypatch.setattr(parser, "_is_http_endpoint_valid", lambda url: url.endswith("/openapi.json"))

    available, reason = parser.check_installation(backend="hybrid-http-client")

    assert available is False
    assert "MINERU_SERVER_URL required" in reason


def test_hybrid_http_client_rejects_an_unreachable_server(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(
        mineru_api="http://mineru.example",
        mineru_server_url="http://vlm.example",
    )
    monkeypatch.setattr(
        parser,
        "_is_http_endpoint_valid",
        lambda url: url.endswith("/openapi.json"),
    )

    available, reason = parser.check_installation(backend="hybrid-http-client")

    assert available is False
    assert "not accessible" in reason


def test_read_output_supports_hybrid_method_directory(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    output_dir = tmp_path / "hybrid_auto"
    output_dir.mkdir()
    expected = [{"type": "text", "text": "Hybrid output"}]
    (output_dir / "sample_content_list.json").write_text(__import__("json").dumps(expected), encoding="utf-8")

    outputs = parser._read_output(tmp_path, "sample", method="auto", backend="hybrid-engine")

    assert outputs == expected


def test_mineru_http_error_includes_response_body(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.example")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    class ErrorResponse:
        text = '{"detail":"hybrid backend rejected the request"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            raise module.requests.HTTPError("400 Client Error", response=self)

    request_data = {}

    def post(**kwargs):
        request_data.update(kwargs["data"])
        return ErrorResponse()

    monkeypatch.setattr(module.requests, "post", post)

    options = module.MinerUParseOptions(start_page_id=12, end_page_id=23)
    with pytest.raises(RuntimeError, match="hybrid backend rejected the request"):
        parser._run_mineru_api(
            pdf_path,
            tmp_path,
            options,
        )

    assert request_data["backend"] == module.MinerUBackend.HYBRID_ENGINE
    assert request_data["start_page_id"] == 12
    assert request_data["end_page_id"] == 23
    assert "preload" not in request_data
    assert "output_dir" not in request_data


def test_mineru_request_uses_high_quality_structured_image_output(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.example")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    request_data = {}

    class ZipResponse:
        headers = {"Content-Type": "application/zip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        raw = __import__("io").BytesIO(b"zip")

    def post(**kwargs):
        request_data.update(kwargs["data"])
        return ZipResponse()

    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(parser, "_extract_zip_no_root", lambda *_args, **_kwargs: None)

    options = module.MinerUParseOptions()
    parser._run_mineru_api(pdf_path, tmp_path, options)

    assert request_data["effort"] == module.MinerUEffort.HIGH
    assert request_data["image_analysis"] is True
    assert request_data["return_content_list"] is True
    assert request_data["return_images"] is True
    assert request_data["response_format_zip"] is True
    assert request_data["return_md"] is False
    assert request_data["return_middle_json"] is False
    assert request_data["return_model_output"] is False


@pytest.mark.parametrize(
    ("suffix", "expected_mime"),
    [
        ("pdf", "application/pdf"),
        ("png", "image/png"),
        ("jpeg", "image/jpeg"),
        ("jpg", "image/jpeg"),
        ("jp2", "image/jp2"),
        ("webp", "image/webp"),
        ("gif", "image/gif"),
        ("bmp", "image/bmp"),
        ("tiff", "image/tiff"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_mineru_request_preserves_input_filename_and_mime(monkeypatch, tmp_path, suffix, expected_mime):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.example")
    input_path = tmp_path / f"sample.{suffix}"
    input_path.write_bytes(b"input")
    uploaded = {}

    class ZipResponse:
        headers = {"Content-Type": "application/zip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        raw = __import__("io").BytesIO(b"zip")

    def post(**kwargs):
        filename, _stream, mime_type = kwargs["files"]["files"]
        uploaded.update(filename=filename, mime_type=mime_type)
        return ZipResponse()

    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(parser, "_extract_zip_no_root", lambda *_args, **_kwargs: None)

    parser._run_mineru_api(input_path, tmp_path, module.MinerUParseOptions())

    assert uploaded == {"filename": input_path.name, "mime_type": expected_mime}


def test_parse_non_pdf_skips_pdf_only_processing(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    captured = {}

    monkeypatch.setattr(module, "extract_pdf_outlines", lambda *_args: (_ for _ in ()).throw(AssertionError("PDF outline extraction called")))
    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("PDF rendering called")))

    def capture_run(input_path, output_dir, options, callback=None):
        captured["suffix"] = input_path.suffix
        captured["options"] = options
        return output_dir

    monkeypatch.setattr(parser, "_run_mineru", capture_run)
    monkeypatch.setattr(parser, "_read_output", lambda *_args, **_kwargs: [])

    parser.parse_pdf("report.docx", binary=b"PK", output_dir=str(tmp_path))

    assert captured["suffix"] == ".docx"
    assert parser.outlines == []
    assert parser.page_images is None
    assert parser.page_sizes == []


def test_transfer_to_tables_preserves_mineru_image_and_global_position(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 12
    image_path = tmp_path / "figure.png"
    module.Image.new("RGB", (8, 6), "white").save(image_path)
    outputs = [
        {
            "type": module.MinerUContentType.IMAGE,
            "img_path": str(image_path),
            "image_caption": ["图 1 总体结构"],
            "image_footnote": ["关键尺寸单位：mm"],
            "page_idx": 2,
            "bbox": (10, 20, 110, 220),
        }
    ]

    tables = parser._transfer_to_tables(outputs)

    assert len(tables) == 1
    ((image, text), positions) = tables[0]
    assert image.size == (8, 6)
    assert text == "图 1 总体结构\n关键尺寸单位：mm"
    assert positions == [[14, 10.0, 110.0, 20.0, 220.0]]


def test_coordinates_use_pdf_page_size_when_page_rendering_is_unavailable(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 0
    parser.page_images = None
    parser.page_sizes = [(612.0, 792.0)]
    image_path = tmp_path / "figure.png"
    module.Image.new("RGB", (4, 4), "white").save(image_path)

    position_tag = parser._line_tag({"page_idx": 0, "bbox": (100, 200, 900, 800)})
    media = parser._transfer_to_tables(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "img_path": str(image_path),
                "page_idx": 0,
                "bbox": (100, 200, 900, 800),
            }
        ]
    )

    assert position_tag == "@@1\t61.2\t550.8\t158.4\t633.6##"
    assert media[0][1][0] == pytest.approx([0, 61.2, 550.8, 158.4, 633.6])


def test_page_geometry_survives_pdfium_render_failure_under_global_lock(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    class Page:
        width = 612
        height = 792

        @staticmethod
        def to_image(**_kwargs):
            raise RuntimeError("PDFium render failed")

    class Pdf:
        def __init__(self):
            self.pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Lock:
        entered = False

        def __enter__(self):
            self.entered = True

        def __exit__(self, *_args):
            return False

    lock = Lock()
    monkeypatch.setitem(module.sys.modules, module.LOCK_KEY_pdfplumber, lock)
    monkeypatch.setattr(module.pdfplumber, "open", lambda _source: Pdf())

    parser.__images__(b"%PDF")

    assert lock.entered is True
    assert parser.page_images is None
    assert parser.page_sizes == [(612.0, 792.0)]
    assert parser.total_page == 1


def test_transfer_to_tables_keeps_image_without_caption_for_projection(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 0
    image_path = tmp_path / "figure.png"
    module.Image.new("RGB", (4, 4), "white").save(image_path)

    tables = parser._transfer_to_tables(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "img_path": str(image_path),
                "page_idx": 0,
                "bbox": (1, 2, 3, 4),
            }
        ]
    )

    assert len(tables) == 1
    assert tables[0][0][0].size == (4, 4)
    assert tables[0][0][1] == ""


def test_transfer_to_tables_supports_high_effort_chart_shape(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 0
    image_path = tmp_path / "chart.jpg"
    module.Image.new("RGB", (10, 8), "white").save(image_path)

    tables = parser._transfer_to_tables(
        [
            {
                "type": "chart",
                "img_path": str(image_path),
                "content": "| 类别 | 数值 |\n| --- | --- |\n| A | 10 |",
                "chart_caption": ["图 2 性能分布"],
                "chart_footnote": [],
                "page_idx": 1,
                "bbox": [20, 30, 120, 230],
            }
        ]
    )

    assert len(tables) == 1
    ((image, text), positions) = tables[0]
    assert image.size == (10, 8)
    assert text == "图 2 性能分布\n| 类别 | 数值 |\n| --- | --- |\n| A | 10 |"
    assert positions == [[1, 20.0, 120.0, 30.0, 230.0]]


def test_transfer_to_tables_preserves_table_image_without_section_duplication(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 0
    image_path = tmp_path / "table.png"
    module.Image.new("RGB", (12, 6), "white").save(image_path)
    output = {
        "type": module.MinerUContentType.TABLE,
        "img_path": str(image_path),
        "table_body": "<table><tr><td>零件</td></tr></table>",
        "table_caption": ["表 1 BOM"],
        "table_footnote": [],
        "page_idx": 0,
        "bbox": [2, 4, 20, 40],
    }

    assert parser._transfer_to_sections([output], parse_method="pipeline") == []
    tables = parser._transfer_to_tables([output])
    assert len(tables) == 1
    assert tables[0][0][0].size == (12, 6)
    assert "<table>" in tables[0][0][1]


def test_transfer_to_sections_does_not_duplicate_image_owned_by_figure_channel(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    image_path = tmp_path / "figure.png"
    module.Image.new("RGB", (4, 4), "white").save(image_path)

    sections = parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "img_path": str(image_path),
                "image_caption": ["图 1"],
                "vlm_description": "装配关系说明",
                "page_idx": 0,
                "bbox": (0, 0, 1, 1),
            }
        ],
        parse_method="pipeline",
    )

    assert sections == []


def test_transfer_to_sections_keeps_image_semantics_when_image_file_is_missing(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    sections = parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "img_path": "/missing/figure.png",
                "image_caption": ["图 3 装配关系"],
                "page_idx": 0,
                "bbox": (0, 0, 1, 1),
            }
        ],
        parse_method="pipeline",
    )

    assert sections[0][0] == "图 3 装配关系"
    assert sections[0][1] == module.MinerUContentType.IMAGE


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("English", "ch"),
        ("Chinese", "ch"),
        ("Japanese", "ch"),
        ("Korean", "korean"),
        ("Russian", "east_slavic"),
    ],
)
def test_language_mapping_matches_service_contract(monkeypatch, language, expected):
    module = _load_mineru_parser(monkeypatch)

    assert module.LANGUAGE_TO_MINERU_MAP[language] == expected


def test_parse_pdf_forwards_exclusive_page_window_to_mineru(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    captured = {}

    def capture_images(_pdf, zoomin=1, page_from=0, page_to=module.MAXIMUM_PAGE_NUMBER, callback=None):
        captured["images"] = (zoomin, page_from, page_to)
        parser.page_from = page_from
        parser.page_to = page_to

    def capture_run(_pdf, output_dir, options, callback=None):
        captured["options"] = options
        return output_dir

    monkeypatch.setattr(parser, "__images__", capture_images)
    monkeypatch.setattr(parser, "_run_mineru", capture_run)
    monkeypatch.setattr(parser, "_read_output", lambda *_args, **_kwargs: [])

    parser.parse_pdf(
        "sample.pdf",
        binary=b"%PDF",
        output_dir=str(tmp_path),
        from_page=12,
        to_page=24,
    )

    assert captured["images"] == (1, 12, 24)
    assert captured["options"].start_page_id == 12
    assert captured["options"].end_page_id == 23


def test_parse_pdf_rejects_empty_page_window(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    with pytest.raises(ValueError, match="to_page must be greater than from_page"):
        parser.parse_pdf("sample.pdf", binary=b"%PDF", from_page=5, to_page=5)


def test_transfer_records_original_output_order_for_projection(monkeypatch, tmp_path):
    """docx 的 MinerU 输出无 bbox 且 page_idx 恒为 0，数组顺序是唯一阅读序信号。"""
    from PIL import Image

    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(first_image)
    Image.new("RGB", (4, 4), (0, 255, 0)).save(second_image)

    outputs = [
        {"type": module.MinerUContentType.TEXT, "text": "Alpha", "page_idx": 0, "bbox": (0, 0, 1, 1)},
        {"type": module.MinerUContentType.IMAGE, "img_path": str(first_image), "image_caption": [], "page_idx": 0, "bbox": (0, 0, 1, 1)},
        {"type": module.MinerUContentType.TEXT, "text": "Beta", "page_idx": 0, "bbox": (0, 0, 1, 1)},
        {"type": module.MinerUContentType.IMAGE, "img_path": str(second_image), "image_caption": [], "page_idx": 0, "bbox": (0, 0, 1, 1)},
    ]

    parser._transfer_to_sections(outputs, parse_method="raw")
    media = parser._transfer_to_tables(outputs)

    assert [section["source_index"] for section in parser.source_projection_sections] == [0, 2]
    assert len(media) == 2
    assert parser.source_projection_media_indexes == [1, 3]
