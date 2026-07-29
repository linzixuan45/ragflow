#!/usr/bin/env python3
"""Generate minimal stub packages for the OSS DeepDoc Docker image.

The deepdoc vision modules (ocr.py, recognizer.py, etc.) import from
``common``, ``rag``, and ``deepdoc`` at module level.  In the full
RAGFlow environment these packages pull in heavy dependencies (torch,
pdfplumber, database connectors, beartype) that are not needed by the
ONNX-only inference server.

This script writes lightweight replacement modules under /app so the
import chain succeeds without pulling in the full dependency tree.

Why stubs instead of conditionally lazy imports in the vision code?
The vision modules are shared between the full Python backend and the
Docker server.  Keeping the stubs here avoids adding Docker-specific
guards to the shared code.
"""

import os

TARGET = os.environ.get("STUB_TARGET", "/app")


def write(path: str, content: str) -> None:
    full = os.path.join(TARGET, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content.lstrip("\n"))


# ── deepdoc ────────────────────────────────────────────────────────────
# Real deepdoc/__init__.py calls beartype_this_package() which requires
# the beartype library.

write(
    "deepdoc/__init__.py",
    """
# Minimal deepdoc __init__ for Docker — avoids beartype dependency.
""",
)

# Real deepdoc/vision/__init__.py imports pdfplumber and
# AscendLayoutRecognizer (requires ais_bench).  The Docker server only
# needs the four ONNX-based classes below.

write(
    "deepdoc/vision/__init__.py",
    """
# Minimal deepdoc.vision __init__ for Docker — avoids pdfplumber and Ascend imports.
from .ocr import OCR
from .recognizer import Recognizer
from .layout_recognizer import LayoutRecognizer4YOLOv10 as LayoutRecognizer
from .table_structure_recognizer import TableStructureRecognizer

__all__ = ["OCR", "Recognizer", "LayoutRecognizer", "TableStructureRecognizer"]
""",
)

# ── common ─────────────────────────────────────────────────────────────
# Real common.settings imports rag.utils.es_conn and other database/storage
# connectors.  The server only needs PARALLEL_DEVICES for OCR.

write(
    "common/__init__.py",
    """
# Stub common.__init__ for Docker deepdoc service.
import os


class _Settings:
    PARALLEL_DEVICES = int(os.environ.get("PARALLEL_DEVICES", "0"))


settings = _Settings()
""",
)

# Real common.file_utils derives the project base from __file__.  In
# Docker the project root is always /app.

write(
    "common/file_utils.py",
    """
# Stub common.file_utils for Docker deepdoc service.
import os

_PROJECT_BASE = None


def get_project_base_directory(*args):
    global _PROJECT_BASE
    if _PROJECT_BASE is None:
        _PROJECT_BASE = os.environ.get("RAGFLOW_PROJECT_BASE", "/app")
    if args:
        return os.path.join(_PROJECT_BASE, *args)
    return _PROJECT_BASE
""",
)

write(
    "common/onnx_utils.py",
    """
# ONNX Runtime is the DeepDoc service's inference backend.
import os


def get_onnx_cuda_device_count():
    if os.getenv("DEVICE", "cpu").lower() != "gpu":
        return 0

    import onnxruntime as ort

    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except RuntimeError:
            pass

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return 0

    configured = os.getenv("PARALLEL_DEVICES")
    if configured is None:
        return 1
    try:
        return max(0, int(configured))
    except ValueError:
        return 1
""",
)

# Real common.misc_utils pulls in urllib/SSRF guard/redis/sqlalchemy/...
# The deepdoc server only needs the helpers invoked from the vision and
# parser modules (``thread_pool_exec`` from t_ocr/pdf_parser,
# ``get_uuid`` from anywhere that creates ids).  Stub the rest with
# minimal placeholders so module-level imports succeed without
# pulling in heavy dependencies.
write(
    "common/misc_utils.py",
    """
# Stub common.misc_utils for Docker deepdoc service.
import asyncio
import contextvars
import functools
import os
import uuid
from concurrent.futures import ThreadPoolExecutor


def get_uuid():
    return uuid.uuid1().hex


_max_workers = int(os.environ.get("THREAD_POOL_MAX_WORKERS", "8"))
_executor = ThreadPoolExecutor(max_workers=_max_workers)


async def thread_pool_exec(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    if kwargs:
        inner = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(_executor, ctx.run, inner)
    return await loop.run_in_executor(_executor, ctx.run, func, *args)
""",
)

# ── rag ────────────────────────────────────────────────────────────────

write(
    "rag/__init__.py",
    """
# Stub rag package for Docker deepdoc service.
""",
)

# table_structure_recognizer.py imports rag_tokenizer at module level.
# Its tokenize/tag methods are only called from blockType() /
# construct_table(), which are NOT invoked by the TSR adapter's
# __call__() path.  The stub exists solely to satisfy the module-level
# import; its methods are never called at server runtime.

write(
    "rag/nlp/__init__.py",
    """
# Stub rag.nlp module for Docker deepdoc service.
# Provides minimal rag_tokenizer to satisfy table_structure_recognizer import.


class _StubTokenizer:
    def tokenize(self, text):
        return text

    def tag(self, word):
        return ""


rag_tokenizer = _StubTokenizer()
""",
)

# operators.py imports ensure_pil_image at module level and calls it in
# NormalizeImage.__call__ / ToCHWImage.__call__ (OCR text detection path).
# The real rag.utils.lazy_image imports concat_img from rag.nlp, pulling
# in the entire NLP stack.

write(
    "rag/utils/lazy_image.py",
    """
# Stub rag.utils.lazy_image for Docker.
from PIL import Image


def ensure_pil_image(img):
    if isinstance(img, Image.Image):
        return img
    return None
""",
)


if __name__ == "__main__":
    print(f"Docker stubs written to {TARGET}")
