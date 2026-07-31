"""
Unit tests for markdown chunk merging logic in rag/app/naive.py.

Tests the _is_short_header() helper function to ensure short markdown headers
are correctly identified and will be force-merged with the next section.

Uses lazy import via fixture to avoid triggering deepdoc model loading
at pytest collection time (which would fail in CI without model files).
"""

import ast
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).parents[4]


class TestIsShortHeader:
    """Test cases for _is_short_header() function."""

    @pytest.fixture(autouse=True)
    def _lazy_import(self):
        sys.path.insert(0, str(_REPO))
        from rag.app.naive import _is_short_header

        self._is_short_header = _is_short_header

    def test_short_header_h1(self):
        """Short level-1 header should return True."""
        text = "# Quick Start"
        result = self._is_short_header(text)
        assert result is True

    def test_short_header_h2(self):
        """Short level-2 header should return True."""
        text = "## Quick Travel"
        result = self._is_short_header(text)
        assert result is True

    def test_short_header_h3(self):
        """Short level-3 header should return True."""
        text = "### Setup"
        result = self._is_short_header(text)
        assert result is True

    def test_long_header(self):
        """Long header (> 50 tokens) should return False."""
        text = "# " + "Very long header " * 20  # ~100 tokens
        result = self._is_short_header(text)
        assert result is False

    def test_non_header_short_text(self):
        """Short text without header pattern should return False."""
        text = "This is short"
        result = self._is_short_header(text)
        assert result is False

    def test_empty_text(self):
        """Empty text should return False."""
        text = ""
        result = self._is_short_header(text)
        assert result is False

    def test_whitespace_only(self):
        """Whitespace-only text should return False."""
        text = "   "
        result = self._is_short_header(text)
        assert result is False

    def test_header_exactly_50_tokens(self):
        """Header with exactly 50 tokens should return False (strict <)."""
        words = ["word"] * 49
        text = "# " + " ".join(words)
        result = self._is_short_header(text, max_tokens=50)
        assert result is False

    def test_header_49_tokens(self):
        """Header with 49 tokens should return True (< 50)."""
        words = ["word"] * 48
        text = "# " + " ".join(words)
        result = self._is_short_header(text, max_tokens=50)
        assert result is True

    def test_custom_max_tokens(self):
        """Should respect custom max_tokens parameter."""
        # "# Short" = 2 tokens in cl100k_base encoding
        text = "# Short"
        result = self._is_short_header(text, max_tokens=5)
        assert result is True  # 2 < 5 → short

        result = self._is_short_header(text, max_tokens=2)
        assert result is False  # 2 < 2 → not short

    def test_header_with_special_chars(self):
        """Header with special characters should still be recognized."""
        text = "## API Endpoint: /api/v1/users"
        result = self._is_short_header(text)
        assert result is True

    def test_header_with_cjk_chars(self):
        """Header with CJK characters should be recognized."""
        text = "## 快速旅行"
        result = self._is_short_header(text)
        assert result is True


def test_by_mineru_reports_parser_failure():
    class FailingParser:
        def parse_pdf(self, **_kwargs):
            raise RuntimeError("MinerU API returned 400: invalid hybrid backend")

    class Bundle:
        mdl = FailingParser()

    source_path = _REPO / "rag" / "app" / "naive.py"
    parsed = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "by_mineru")
    namespace = {
        "logging": logging,
        "MAXIMUM_PAGE_NUMBER": 100000,
        "LLMType": SimpleNamespace(OCR="ocr", IMAGE2TEXT="image2text"),
        "get_first_provider_model_name": lambda *_args: "mineru-model",
        "ensure_mineru_from_env": lambda *_args: None,
        "get_model_config_from_provider_instance": lambda *_args: {},
        "LLMBundle": lambda **_kwargs: Bundle(),
        "get_tenant_default_model_by_type": lambda *_args: (_ for _ in ()).throw(LookupError("not configured")),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    progress = []

    result = namespace["by_mineru"](
        "sample.pdf",
        binary=b"%PDF",
        tenant_id="tenant",
        callback=lambda progress_value, message: progress.append((progress_value, message)),
    )

    assert result == (None, None, None)
    assert progress[-1] == (-1, "MinerU API returned 400: invalid hybrid backend")


def test_by_mineru_forwards_page_window():
    captured = {}

    class Parser:
        def parse_pdf(self, **kwargs):
            captured.update(kwargs)
            return [], []

    class Bundle:
        mdl = Parser()

    source_path = _REPO / "rag" / "app" / "naive.py"
    parsed = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "by_mineru")
    namespace = {
        "logging": logging,
        "MAXIMUM_PAGE_NUMBER": 100000,
        "LLMType": SimpleNamespace(OCR="ocr", IMAGE2TEXT="image2text"),
        "get_first_provider_model_name": lambda *_args: "mineru-model",
        "ensure_mineru_from_env": lambda *_args: None,
        "get_model_config_from_provider_instance": lambda *_args: {},
        "LLMBundle": lambda **_kwargs: Bundle(),
        "get_tenant_default_model_by_type": lambda *_args: (_ for _ in ()).throw(LookupError("not configured")),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)

    namespace["by_mineru"](
        "sample.pdf",
        binary=b"%PDF",
        from_page=12,
        to_page=24,
        tenant_id="tenant",
        callback=lambda *_args: None,
    )

    assert captured["from_page"] == 12
    assert captured["to_page"] == 24


def test_rtl_normalization_preserves_structured_source_projection_section():
    source_path = _REPO / "rag" / "app" / "naive.py"
    parsed = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "_normalize_section_text_for_rtl_presentation_forms"
    )
    namespace = {"normalize_arabic_presentation_forms": lambda text: text.replace("presentation", "normalized")}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)

    sections = namespace["_normalize_section_text_for_rtl_presentation_forms"](
        [{"text": "presentation title", "layout_type": "title", "level": 2, "position_tag": "@@1"}]
    )

    assert sections == [{"text": "normalized title", "layout_type": "title", "level": 2, "position_tag": "@@1"}]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
