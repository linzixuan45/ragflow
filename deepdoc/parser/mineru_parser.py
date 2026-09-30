#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pdfplumber
import requests
from PIL import Image

from common.constants import MAXIMUM_PAGE_NUMBER
from common.mineru_formats import file_suffix, is_mineru_file, mineru_mime_type
from deepdoc.parser.pdf_parser import RAGFlowPdfParser
from deepdoc.parser.utils import extract_pdf_outlines

_VLM_DESCRIPTION_CACHE: OrderedDict[tuple[str, str, str], str] = OrderedDict()
_VLM_DESCRIPTION_INFLIGHT: dict[tuple[str, str, str], Future] = {}
_VLM_DESCRIPTION_CACHE_LOCK = threading.Lock()


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logging.warning("Invalid %s; using %d", name, default)
        return default


def _vision_model_cache_identity(vision_model) -> str:
    model_config = getattr(vision_model, "model_config", None)
    if not isinstance(model_config, dict):
        model_config = {}
    public_config = {
        key: model_config.get(key)
        for key in ("llm_factory", "llm_name", "api_base", "model_type", "instance_name")
        if model_config.get(key) is not None
    }
    values = (
        getattr(vision_model, "tenant_id", ""),
        getattr(vision_model, "llm_name", ""),
        getattr(vision_model, "lang", ""),
        type(vision_model).__qualname__,
        json.dumps(public_config, ensure_ascii=True, sort_keys=True),
    )
    return "|".join(str(value or "") for value in values)


def _cached_vlm_description(image_path: str, vision_model, prompt: str, generate: Callable[[], str]) -> str:
    with open(image_path, "rb") as image_file:
        content_digest = hashlib.sha256(image_file.read()).hexdigest()
    cache_key = (
        content_digest,
        _vision_model_cache_identity(vision_model),
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )
    cache_size = _positive_env_int("VLM_DESCRIPTION_CACHE_SIZE", 256)

    with _VLM_DESCRIPTION_CACHE_LOCK:
        cached = _VLM_DESCRIPTION_CACHE.pop(cache_key, None)
        if cached is not None:
            _VLM_DESCRIPTION_CACHE[cache_key] = cached
            return cached
        future = _VLM_DESCRIPTION_INFLIGHT.get(cache_key)
        is_owner = future is None
        if is_owner:
            future = Future()
            _VLM_DESCRIPTION_INFLIGHT[cache_key] = future

    if not is_owner:
        return future.result()

    try:
        description = (generate() or "").strip()
    except BaseException as exc:
        with _VLM_DESCRIPTION_CACHE_LOCK:
            _VLM_DESCRIPTION_INFLIGHT.pop(cache_key, None)
        future.set_exception(exc)
        raise

    with _VLM_DESCRIPTION_CACHE_LOCK:
        if description:
            _VLM_DESCRIPTION_CACHE[cache_key] = description
            while len(_VLM_DESCRIPTION_CACHE) > cache_size:
                _VLM_DESCRIPTION_CACHE.popitem(last=False)
        _VLM_DESCRIPTION_INFLIGHT.pop(cache_key, None)
    future.set_result(description)
    return description

LOCK_KEY_pdfplumber = "global_shared_lock_pdfplumber"
if LOCK_KEY_pdfplumber not in sys.modules:
    sys.modules[LOCK_KEY_pdfplumber] = threading.Lock()


class MinerUContentType(StrEnum):
    IMAGE = "image"
    CHART = "chart"
    TABLE = "table"
    TEXT = "text"
    EQUATION = "equation"
    CODE = "code"
    LIST = "list"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    DISCARDED = "discarded"


# Mapping from language names to MinerU language codes
LANGUAGE_TO_MINERU_MAP = {
    "English": "ch",
    "Chinese": "ch",
    "Traditional Chinese": "ch",
    "Russian": "east_slavic",
    "Ukrainian": "east_slavic",
    "Indonesian": "ch",
    "Spanish": "ch",
    "Vietnamese": "ch",
    "Japanese": "ch",
    "Korean": "korean",
    "Portuguese BR": "ch",
    "German": "ch",
    "French": "ch",
    "Italian": "ch",
    "Tamil": "ta",
    "Telugu": "te",
    "Kannada": "ka",
    "Thai": "th",
    "Greek": "el",
    "Hindi": "devanagari",
    "Bulgarian": "cyrillic",
    "Turkish": "ch",
}


class MinerUBackend(StrEnum):
    """MinerU processing backend options."""

    PIPELINE = "pipeline"  # Traditional multimodel pipeline
    VLM_ENGINE = "vlm-engine"  # Local vision-language model engine
    VLM_HTTP_CLIENT = "vlm-http-client"  # HTTP client for remote vLLM server (CPU only)
    HYBRID_ENGINE = "hybrid-engine"  # Local VLM engine combined with the pipeline backend
    HYBRID_HTTP_CLIENT = "hybrid-http-client"  # Remote VLM server combined with the pipeline backend


class MinerULanguage(StrEnum):
    """MinerU supported languages for OCR (pipeline backend only)."""

    CH = "ch"  # Chinese
    CH_SERVER = "ch_server"  # Chinese (server)
    KOREAN = "korean"  # Korean
    TA = "ta"  # Tamil
    TE = "te"  # Telugu
    KA = "ka"  # Kannada
    TH = "th"  # Thai
    EL = "el"  # Greek
    ARABIC = "arabic"  # Arabic
    EAST_SLAVIC = "east_slavic"  # East Slavic
    CYRILLIC = "cyrillic"  # Cyrillic
    DEVANAGARI = "devanagari"  # Devanagari


class MinerUParseMethod(StrEnum):
    """MinerU PDF parsing methods (pipeline backend only)."""

    AUTO = "auto"  # Automatically determine the method based on the file type
    TXT = "txt"  # Use text extraction method
    OCR = "ocr"  # Use OCR method for image-based PDFs


class MinerUEffort(StrEnum):
    """MinerU hybrid parsing quality levels."""

    MEDIUM = "medium"
    HIGH = "high"


V1_TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "canceled"})
V1_POLL_INTERVAL_SECONDS = 2.0
V1_JOB_TIMEOUT_SECONDS = 1800
V1_MAX_PAGES_PER_FILE = 1000
V1_UPLOAD_CACHE_LIMIT = 64


class V1UploadCache:
    """Share one MinerU 4.x upload across jobs for identical content.

    A document split into page ranges becomes one task per range, and each task
    uploads the whole file, so the same bytes are sent once per range while only
    ``page_range`` differs. Keying the resulting ``file_id`` by server plus
    content hash lets later jobs skip the upload, and the per-key lock makes
    concurrent tasks for the same content wait for a single upload instead of
    racing to create several.
    """

    def __init__(self, limit: int = V1_UPLOAD_CACHE_LIMIT) -> None:
        self._limit = limit
        self._file_ids: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, key: tuple[str, str]) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    def get(self, key: tuple[str, str]) -> str | None:
        with self._guard:
            file_id = self._file_ids.get(key)
            if file_id is not None:
                self._file_ids.move_to_end(key)
            return file_id

    def put(self, key: tuple[str, str], file_id: str) -> None:
        with self._guard:
            self._file_ids[key] = file_id
            self._file_ids.move_to_end(key)
            while len(self._file_ids) > self._limit:
                self._file_ids.popitem(last=False)

    def drop(self, key: tuple[str, str]) -> None:
        with self._guard:
            self._file_ids.pop(key, None)


_v1_upload_cache = V1UploadCache()

# MinerU 4.x quality tier per 3.x backend (official mapping from the 4.0 changelog).
BACKEND_TO_V1_TIER = {
    MinerUBackend.PIPELINE: "basic",
    MinerUBackend.VLM_ENGINE: "advanced",
    MinerUBackend.VLM_HTTP_CLIENT: "advanced",
    MinerUBackend.HYBRID_ENGINE: "standard",
    MinerUBackend.HYBRID_HTTP_CLIENT: "standard",
}

_VISUAL_BODY_TYPES = frozenset({"image_body", "table_body", "chart_body", "code_body", "algorithm_body"})
_VISUAL_ANNOTATION_TYPES = frozenset(
    {
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
        "chart_caption",
        "chart_footnote",
        "code_caption",
        "code_footnote",
    }
)
_AUX_TEXT_TYPES = frozenset({"header", "footer", "page_number", "aside_text", "page_footnote"})


def resolve_v1_tier(backend, override: str = "") -> str:
    """Map a 3.x backend onto a MinerU 4.x tier; an explicit override wins."""
    override = (override or "").strip()
    if override:
        return override
    try:
        return BACKEND_TO_V1_TIER.get(MinerUBackend(backend), "standard")
    except ValueError:
        return "standard"


def build_v1_page_range(start_page_id: int, end_page_id: int) -> str:
    """Convert RAGFlow's 0-based inclusive page window to a V1 1-based page_range."""
    first = max(0, int(start_page_id)) + 1
    last = int(end_page_id) + 1
    if last >= V1_MAX_PAGES_PER_FILE:
        return "all" if first <= 1 else f"{first}-r1"
    if last < first:
        return "all"
    return f"{first}-{last}"


def _render_inline(content) -> str:
    """Render inline spans (text / equation / code / hyperlink) into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""
    parts = []
    for span in content:
        if isinstance(span, str):
            parts.append(span)
            continue
        if not isinstance(span, dict):
            continue
        span_type = span.get("type")
        if span_type == "hyperlink":
            parts.append(_render_inline(span.get("children") or span.get("content") or []))
            continue
        value = span.get("content")
        if isinstance(value, list):
            parts.append(_render_inline(value))
        elif value:
            parts.append(str(value))
    return "".join(parts)


def _normalize_bbox(bbox) -> list[int] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        return [int(float(value) * 1000) for value in bbox]
    except (TypeError, ValueError):
        return None


def _union_bbox(blocks: list[dict]) -> list | None:
    boxes = [block.get("bbox") for block in blocks]
    boxes = [bbox for bbox in boxes if isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(value, (int, float)) for value in bbox)]
    if not boxes:
        return None
    return [min(bbox[0] for bbox in boxes), min(bbox[1] for bbox in boxes), max(bbox[2] for bbox in boxes), max(bbox[3] for bbox in boxes)]


def _annotation_texts(block: dict, accepted: frozenset[str]) -> list[str]:
    annotations = [child for child in block.get("content") or [] if isinstance(child, dict) and child.get("type") in accepted]
    annotations.sort(key=lambda child: (child.get("index") is None, child.get("index") or 0))
    return [text for text in (_render_inline(child.get("content")) for child in annotations) if text]


def _visual_body(block: dict) -> dict:
    for child in block.get("content") or []:
        if isinstance(child, dict) and child.get("type") in _VISUAL_BODY_TYPES:
            return child
    return {}


def _flatten_nested(blocks: list, nested_types: frozenset[str], depth: int = 0) -> list[tuple[int, dict]]:
    leaves = []
    for child in blocks or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") in nested_types:
            leaves.extend(_flatten_nested(child.get("content") or [], nested_types, depth + 1))
        else:
            leaves.append((depth, child))
    return leaves


def _render_visual_item(block: dict, block_type: str) -> dict:
    body = _visual_body(block)
    item: dict[str, Any] = {"type": block_type}
    img_path = body.get("image_path") or block.get("image_path") or ""
    if img_path:
        item["img_path"] = img_path
    annotation_prefix = "image" if block_type == "image" else block_type
    item[f"{annotation_prefix}_caption"] = _annotation_texts(block, frozenset({f"{annotation_prefix}_caption"}))
    item[f"{annotation_prefix}_footnote"] = _annotation_texts(block, frozenset({f"{annotation_prefix}_footnote"}))
    body_content = body.get("content") or ""
    if block_type == "table":
        if body_content:
            item["table_body"] = body_content
    elif block_type == "code":
        item["code_body"] = body_content
        item["sub_type"] = "algorithm" if body.get("type") == "algorithm_body" or block.get("sub_type") == "algorithm" else "code"
    else:
        if body_content:
            item["content"] = body_content
    if block_type in {"image", "chart"} and block.get("sub_type"):
        item["sub_type"] = block["sub_type"]
    return item


def _render_block(block: dict) -> dict | None:
    block_type = block.get("type")
    if not isinstance(block_type, str):
        return None

    if block_type in {"doc_title", "paragraph_title"}:
        item: dict[str, Any] = {"type": "text", "text": _render_inline(block.get("content"))}
        level = block.get("level")
        if isinstance(level, int) and not isinstance(level, bool):
            item["text_level"] = level
        return item or None
    if block_type == "text":
        return {"type": "text", "text": _render_inline(block.get("content"))}
    if block_type == "equation":
        item = {"type": "equation"}
        if block.get("image_path"):
            item["img_path"] = block["image_path"]
        latex = _render_inline(block.get("content")).strip()
        if latex:
            item["text"] = latex
            item["text_format"] = "latex"
        return item
    if block_type == "list":
        items = [text for _depth, leaf in _flatten_nested(block.get("content") or [], frozenset({"list"})) if (text := _render_inline(leaf.get("content")))]
        return {"type": "list", "sub_type": block.get("sub_type") or "text", "list_items": items}
    if block_type == "index":
        items = []
        for depth, leaf in _flatten_nested(block.get("content") or [], frozenset({"index"})):
            label = _render_inline(leaf.get("content")).strip()
            if label:
                items.append(f"{'    ' * depth}- {label}")
        return {"type": "index", "list_items": items}
    if block_type in {"image", "table", "chart"}:
        return _render_visual_item(block, block_type)
    if block_type == "code":
        return _render_visual_item(block, "code")
    if block_type in _AUX_TEXT_TYPES:
        return {"type": block_type, "text": _render_inline(block.get("content"))}

    text = _render_inline(block.get("content"))
    if text:
        return {"type": "text", "text": text}
    return None


def middle_json_to_content_list(middle_json: dict) -> list[dict]:
    """Project a MinerU 4.x ``docvortex.middle`` document onto the 3.x Content List V1 shape.

    Mirrors MinerU's own ``render_content_list`` (mineru/render/_internal/content_list/v1.py):
    bbox is normalized from 0-1 to 0-1000 integers, headings carry ``text_level``,
    visual blocks keep their zip-relative ``img_path``.
    """
    items: list[dict] = []

    def append_item(item: dict, page_idx: int, bbox) -> None:
        item["page_idx"] = page_idx
        normalized = _normalize_bbox(bbox)
        if normalized:
            item["bbox"] = normalized
        items.append(item)

    def flush_references(group: list[dict], page_idx: int) -> None:
        if not group:
            return
        list_items = [text for block in group if (text := _render_inline(block.get("content")))]
        append_item({"type": "list", "sub_type": "ref_text", "list_items": list_items}, page_idx, _union_bbox(group))
        group.clear()

    for page in middle_json.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_idx = page.get("page_idx", 0)
        ref_group: list[dict] = []

        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "ref_text":
                ref_group.append(block)
                continue
            flush_references(ref_group, page_idx)
            item = _render_block(block)
            if item is None:
                continue
            append_item(item, page_idx, block.get("bbox"))
        flush_references(ref_group, page_idx)
    return items


@dataclass
class MinerUParseOptions:
    """Options for MinerU PDF parsing."""

    backend: MinerUBackend = MinerUBackend.HYBRID_ENGINE
    lang: Optional[MinerULanguage] = None  # language for OCR (pipeline backend only)
    method: MinerUParseMethod = MinerUParseMethod.AUTO
    server_url: Optional[str] = None
    delete_output: bool = True
    parse_method: str = "raw"
    formula_enable: bool = True
    table_enable: bool = True
    effort: MinerUEffort = MinerUEffort.HIGH
    image_analysis: bool = True
    start_page_id: int = 0
    end_page_id: int = MAXIMUM_PAGE_NUMBER - 1


class MinerUParser(RAGFlowPdfParser):
    def __init__(self, mineru_path: str = "mineru", mineru_api: str = "", mineru_server_url: str = ""):
        self.mineru_api = mineru_api.rstrip("/")
        self.mineru_server_url = mineru_server_url.rstrip("/")
        self._api_protocol: str | None = None
        self.outlines = []
        self.source_projection_sections = []
        # Source index of each entry in `_transfer_to_tables`'s returned media list.
        # MinerU office output carries no bbox, so array order is the only reading order.
        self.source_projection_media_indexes: list[int] = []
        self.logger = logging.getLogger(self.__class__.__name__)

    @staticmethod
    def _is_zipinfo_symlink(member: zipfile.ZipInfo) -> bool:
        return (member.external_attr >> 16) & 0o170000 == 0o120000

    def _extract_zip_no_root(self, zip_path, extract_to, root_dir):
        self.logger.info(f"[MinerU] Extract zip: zip_path={zip_path}, extract_to={extract_to}, root_hint={root_dir}")
        base_dir = Path(extract_to).resolve()
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            members = zip_ref.infolist()
            if not root_dir:
                if members and members[0].filename.endswith("/"):
                    root_dir = members[0].filename
                else:
                    root_dir = None
            if root_dir:
                root_dir = root_dir.replace("\\", "/")
                if not root_dir.endswith("/"):
                    root_dir += "/"

            for member in members:
                if member.flag_bits & 0x1:
                    raise RuntimeError(f"[MinerU] Encrypted zip entry not supported: {member.filename}")
                if self._is_zipinfo_symlink(member):
                    raise RuntimeError(f"[MinerU] Symlink zip entry not supported: {member.filename}")

                name = member.filename.replace("\\", "/")
                if root_dir and name == root_dir:
                    self.logger.info("[MinerU] Ignore root folder...")
                    continue
                if root_dir and name.startswith(root_dir):
                    name = name[len(root_dir) :]
                if not name:
                    continue
                if name.startswith("/") or name.startswith("//") or re.match(r"^[A-Za-z]:", name):
                    raise RuntimeError(f"[MinerU] Unsafe zip path (absolute): {member.filename}")

                parts = [p for p in name.split("/") if p not in ("", ".")]
                if any(p == ".." for p in parts):
                    raise RuntimeError(f"[MinerU] Unsafe zip path (traversal): {member.filename}")

                rel_path = os.path.join(*parts) if parts else ""
                dest_path = (Path(extract_to) / rel_path).resolve(strict=False)
                if dest_path != base_dir and base_dir not in dest_path.parents:
                    raise RuntimeError(f"[MinerU] Unsafe zip path (escape): {member.filename}")

                if member.is_dir():
                    os.makedirs(dest_path, exist_ok=True)
                    continue

                os.makedirs(dest_path.parent, exist_ok=True)
                with zip_ref.open(member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _is_http_endpoint_valid(url, timeout=5):
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code in [200, 301, 302, 307, 308]
        except Exception:
            return False

    def _probe_v1_health(self) -> dict | None:
        """Return the MinerU 4.x ``/v1/health`` payload, or None for a 3.x instance."""
        if not self.mineru_api:
            return None
        try:
            response = requests.get(f"{self.mineru_api}/v1/health", timeout=5)
        except requests.RequestException:
            return None
        if response.status_code not in (200, 503):
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or not ({"version", "status"} & payload.keys()):
            return None
        return payload

    def _resolve_api_protocol(self) -> str:
        if self._api_protocol:
            return self._api_protocol
        self._api_protocol = "v1" if self._probe_v1_health() is not None else "legacy"
        return self._api_protocol

    @staticmethod
    def _sanitize_section_text(section: str) -> str:
        """Normalize MinerU text blocks before chunking.

        MinerU may return HTML fragments (e.g. table_body with <tr>/<td>/<br>).
        Keep human-readable text while removing tag noise that hurts chunking.
        """
        if not section:
            return ""
        section = html.unescape(section)
        # Preserve rough structure before dropping tags.
        section = re.sub(r"(?is)<\s*br\s*/?\s*>", "\n", section)
        section = re.sub(r"(?is)</\s*(p|div|li|tr|h[1-6]|table|caption)\s*>", "\n", section)
        section = re.sub(r"(?is)<[^>]+>", "", section)
        # Collapse whitespace while preserving line boundaries.
        section = re.sub(r"[ \t]+\n", "\n", section)
        section = re.sub(r"\n{3,}", "\n\n", section)
        section = re.sub(r"[ \t]{2,}", " ", section)
        return section.strip()

    def check_installation(self, backend: str = "hybrid-engine", server_url: Optional[str] = None) -> tuple[bool, str]:
        reason = ""

        valid_backends = [item.value for item in MinerUBackend]
        if backend not in valid_backends:
            reason = f"[MinerU] Invalid backend '{backend}'. Valid backends are: {valid_backends}"
            self.logger.warning(reason)
            return False, reason

        if not self.mineru_api:
            reason = "[MinerU] MINERU_APISERVER not configured."
            self.logger.warning(reason)
            return False, reason

        health = self._probe_v1_health()
        if health is not None:
            self._api_protocol = "v1"
            if health.get("status") == "ok":
                self.logger.info(f"[MinerU] MinerU 4.x V1 api reachable, version={health.get('version')}")
                return True, reason
            reason = f"[MinerU] MinerU 4.x not ready yet: {health}"
            self.logger.warning(reason)
            return False, reason
        self._api_protocol = "legacy"

        api_openapi = f"{self.mineru_api}/openapi.json"
        try:
            api_ok = self._is_http_endpoint_valid(api_openapi)
            self.logger.info(f"[MinerU] API openapi.json reachable={api_ok} url={api_openapi}")
            if not api_ok:
                reason = f"[MinerU] MinerU API not accessible: {api_openapi}"
                return False, reason
        except Exception as exc:
            reason = f"[MinerU] MinerU API check failed: {exc}"
            self.logger.warning(reason)
            return False, reason

        if backend in {MinerUBackend.VLM_HTTP_CLIENT, MinerUBackend.HYBRID_HTTP_CLIENT}:
            resolved_server = server_url or self.mineru_server_url
            if not resolved_server:
                reason = f"[MinerU] MINERU_SERVER_URL required for {backend} backend."
                self.logger.warning(reason)
                return False, reason
            try:
                server_ok = self._is_http_endpoint_valid(resolved_server)
                self.logger.info(f"[MinerU] {backend} server check reachable={server_ok} url={resolved_server}")
                if not server_ok:
                    reason = f"[MinerU] {backend} server not accessible: {resolved_server}"
                    return False, reason
            except Exception as exc:
                reason = f"[MinerU] {backend} server probe failed: {resolved_server}: {exc}"
                self.logger.warning(reason)
                return False, reason

        return True, reason

    def _run_mineru(self, input_path: Path, output_dir: Path, options: MinerUParseOptions, callback: Optional[Callable] = None) -> Path:
        return self._run_mineru_api(input_path, output_dir, options, callback)

    def _run_mineru_api(self, input_path: Path, output_dir: Path, options: MinerUParseOptions, callback: Optional[Callable] = None) -> Path:
        if self._resolve_api_protocol() == "v1":
            return self._run_mineru_v1(input_path, output_dir, options, callback)
        input_file_path = str(input_path)

        if not os.path.exists(input_file_path):
            raise RuntimeError(f"[MinerU] Input file does not exist: {input_file_path}")

        input_file = Path(input_file_path)
        file_stem = input_file.stem.strip()
        output_path = tempfile.mkdtemp(prefix=f"{file_stem}_{options.method}_", dir=str(output_dir))
        output_zip_path = os.path.join(str(output_dir), f"{Path(output_path).name}.zip")

        data = {
            "lang_list": options.lang,
            "backend": options.backend,
            "effort": options.effort,
            "parse_method": options.method,
            "formula_enable": options.formula_enable,
            "table_enable": options.table_enable,
            "image_analysis": options.image_analysis,
            "server_url": None,
            "return_md": False,
            "return_middle_json": False,
            "return_model_output": False,
            "return_content_list": True,
            "return_images": True,
            "response_format_zip": True,
            "start_page_id": options.start_page_id,
            "end_page_id": options.end_page_id,
        }

        if options.server_url:
            data["server_url"] = options.server_url
        elif self.mineru_server_url:
            data["server_url"] = self.mineru_server_url

        self.logger.info(f"[MinerU] request {data=}")
        self.logger.info(f"[MinerU] request {options=}")

        headers = {"Accept": "application/json"}
        try:
            self.logger.info(f"[MinerU] invoke api: {self.mineru_api}/file_parse backend={options.backend} server_url={data.get('server_url')}")
            if callback:
                callback(0.20, f"[MinerU] invoke api: {self.mineru_api}/file_parse")
            with open(input_file_path, "rb") as input_stream:
                files = {"files": (input_file.name, input_stream, mineru_mime_type(input_file.name))}
                with requests.post(
                    url=f"{self.mineru_api}/file_parse",
                    files=files,
                    data=data,
                    headers=headers,
                    timeout=1800,
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "")
                    if not content_type.startswith("application/zip"):
                        raise RuntimeError(f"[MinerU] not zip returned from api: {content_type}")
                    self.logger.info(f"[MinerU] zip file returned, saving to {output_zip_path}...")
                    if callback:
                        callback(0.30, f"[MinerU] zip file returned, saving to {output_zip_path}...")
                    with open(output_zip_path, "wb") as f:
                        response.raw.decode_content = True
                        shutil.copyfileobj(response.raw, f)
                    self.logger.info(f"[MinerU] Unzip to {output_path}...")
                    self._extract_zip_no_root(output_zip_path, output_path, file_stem + "/")
                    if callback:
                        callback(0.40, f"[MinerU] Unzip to {output_path}...")
            self.logger.info("[MinerU] Api completed successfully.")
            return Path(output_path)
        except requests.RequestException as e:
            response = getattr(e, "response", None)
            response_body = ""
            if response is not None:
                try:
                    response_body = response.text.strip()
                except Exception:
                    response_body = ""
            detail = f"; response: {response_body[:2000]}" if response_body else ""
            raise RuntimeError(f"[MinerU] api failed with exception {e}{detail}") from e

    def _v1_json(self, verb: str, path: str, *, payload: dict | None = None, expect: tuple[int, ...] = (200,), timeout: int = 60) -> dict:
        kwargs: dict[str, Any] = {"timeout": timeout, "headers": {"Accept": "application/json"}}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(requests, verb)(f"{self.mineru_api}{path}", **kwargs)
        if response.status_code not in expect:
            body = (response.text or "").strip()
            detail = f"; response: {body[:2000]}" if body else ""
            raise requests.HTTPError(f"HTTP {response.status_code} for {path}{detail}", response=response)
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError(f"[MinerU] v1 response for {path} is not a JSON object: {type(data).__name__}")
        return data

    def _v1_wait_for_job(self, job_id: str, callback: Callable | None = None) -> dict:
        deadline = time.monotonic() + V1_JOB_TIMEOUT_SECONDS
        reported = False
        while True:
            job = self._v1_json("get", f"/v1/parse/jobs/{job_id}")
            status = job.get("status")
            if status in V1_TERMINAL_STATUSES:
                self.logger.info(f"[MinerU] v1 job {job_id} finished: status={status} progress={job.get('progress')}")
                return job
            if not reported:
                reported = True
                if callback:
                    callback(0.45, f"[MinerU] parse job {job_id} status={status}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"[MinerU] v1 parse job timed out after {V1_JOB_TIMEOUT_SECONDS}s: job_id={job_id}")
            time.sleep(V1_POLL_INTERVAL_SECONDS)

    def _v1_zip_file_id(self, job: dict) -> str:
        files = job.get("files") or []
        entry = files[0] if files and isinstance(files[0], dict) else {}
        zip_meta = (entry.get("output_files") or {}).get("zip") or {}
        zip_file_id = zip_meta.get("file_id")
        if entry.get("status") == "completed" and zip_file_id:
            return str(zip_file_id)
        error = entry.get("error") or job.get("error") or entry.get("status") or job.get("status")
        raise RuntimeError(f"[MinerU] v1 parse produced no zip (job={job.get('status')}, file={entry.get('status')}): {error}")

    def _v1_download_file(self, file_id: str, destination: Path) -> None:
        response = requests.get(f"{self.mineru_api}/v1/files/{file_id}/content", timeout=600, stream=True)
        response.raise_for_status()
        with open(destination, "wb") as target:
            response.raw.decode_content = True
            shutil.copyfileobj(response.raw, target)

    def _write_v1_content_list(self, output_path: Path, file_stem: str) -> None:
        middle_path = output_path / "middle_json.json"
        if not middle_path.is_file():
            raise RuntimeError(f"[MinerU] v1 zip is missing middle_json.json: {output_path}")
        with open(middle_path, encoding="utf-8") as source:
            middle_json = json.load(source)
        content_list = middle_json_to_content_list(middle_json)
        target = output_path / f"{file_stem}_content_list.json"
        target.write_text(json.dumps(content_list, ensure_ascii=False), encoding="utf-8")
        self.logger.info(f"[MinerU] rendered {len(content_list)} content_list items from v1 middle_json -> {target.name}")

    def _run_mineru_v1(self, input_path: Path, output_dir: Path, options: MinerUParseOptions, callback: Callable | None = None) -> Path:
        input_file = Path(input_path)
        file_stem = input_file.stem
        output_path = Path(tempfile.mkdtemp(prefix=f"{file_stem}_{options.method}_", dir=str(output_dir)))
        zip_path = Path(output_dir) / f"{output_path.name}.zip"
        tier = resolve_v1_tier(options.backend, os.environ.get("MINERU_TIER", ""))
        page_range = build_v1_page_range(options.start_page_id, options.end_page_id) if file_suffix(str(input_file)) == "pdf" else None

        self.logger.info(f"[MinerU] v1 request: tier={tier} page_range={page_range} backend={options.backend} file={input_file.name}")
        if callback:
            callback(0.20, f"[MinerU] invoke MinerU 4.x api: {self.mineru_api} tier={tier}")

        try:
            key = self._v1_content_key(input_file)
            file_id, reused = self._v1_upload_file_id(key, input_file)
            try:
                self._v1_execute_job(file_id, tier, page_range, zip_path, callback)
            except Exception:
                if not reused:
                    raise
                # A reused file_id can outlive the server's copy. Re-upload once
                # rather than failing the parse on a stale reference.
                self.logger.warning(f"[MinerU] v1 job failed for reused upload {file_id}; re-uploading and retrying once", exc_info=True)
                _v1_upload_cache.drop(key)
                file_id, _ = self._v1_upload_file_id(key, input_file)
                self._v1_execute_job(file_id, tier, page_range, zip_path, callback)
        except requests.RequestException as e:
            response = getattr(e, "response", None)
            response_body = ""
            if response is not None:
                try:
                    response_body = response.text.strip()
                except (AttributeError, ValueError):
                    response_body = ""
            detail = f"; response: {response_body[:2000]}" if response_body else ""
            raise RuntimeError(f"[MinerU] v1 api failed with exception {e}{detail}") from e

        self.logger.info(f"[MinerU] Unzip to {output_path}...")
        self._extract_zip_no_root(str(zip_path), str(output_path), file_stem + "/")
        self._write_v1_content_list(output_path, file_stem)
        if callback:
            callback(0.65, f"[MinerU] Unzip to {output_path}...")
        self.logger.info("[MinerU] V1 api completed successfully.")
        return output_path

    def _v1_content_key(self, input_file: Path) -> tuple[str, str]:
        """Identify an upload by server plus content so page-range tasks share it."""
        return (self.mineru_api, hashlib.sha256(input_file.read_bytes()).hexdigest())

    def _v1_upload_file_id(self, key: tuple[str, str], input_file: Path) -> tuple[str, bool]:
        """Return ``(file_id, reused)``, uploading once per content hash."""
        with _v1_upload_cache.lock_for(key):
            cached = _v1_upload_cache.get(key)
            if cached:
                self.logger.info(f"[MinerU] v1 reuse uploaded file {cached} for {input_file.name}")
                return cached, True
            upload = self._v1_json(
                "post",
                "/v1/uploads",
                payload={
                    "filename": input_file.name,
                    "bytes": input_file.stat().st_size,
                    "mime_type": mineru_mime_type(input_file.name),
                    "purpose": "parse",
                },
            )
            upload_id = upload.get("id")
            upload_url = upload.get("upload_url")
            if not upload_id or not upload_url:
                raise RuntimeError(f"[MinerU] v1 upload creation returned no id/url: {upload}")
            if str(upload_url).startswith("/"):
                upload_url = f"{self.mineru_api}{upload_url}"
            with open(input_file, "rb") as upload_body:
                put_response = requests.put(str(upload_url), data=upload_body, headers=upload.get("upload_headers") or {}, timeout=600)
            put_response.raise_for_status()

            completed = self._v1_json("post", f"/v1/uploads/{upload_id}/complete")
            file_id = (completed.get("file") or {}).get("id")
            if not file_id:
                raise RuntimeError(f"[MinerU] v1 upload completion returned no file id: {completed}")
            _v1_upload_cache.put(key, str(file_id))
            return str(file_id), False

    def _v1_execute_job(self, file_id: str, tier: str, page_range: str | None, zip_path: Path, callback: Callable | None) -> None:
        """Submit one parse job for an already-uploaded file and fetch its zip."""
        job_payload: dict[str, Any] = {
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "tier": tier,
            "output_formats": ["zip"],
        }
        if page_range:
            job_payload["files"][0]["page_range"] = page_range
        job = self._v1_json("post", "/v1/parse/jobs", payload=job_payload, expect=(200, 202))
        job_id = job.get("job_id")
        if not job_id:
            raise RuntimeError(f"[MinerU] v1 parse job submission returned no job_id: {job}")

        job = self._v1_wait_for_job(str(job_id), callback)
        self._v1_download_file(self._v1_zip_file_id(job), zip_path)
        if callback:
            callback(0.55, f"[MinerU] zip file returned, saving to {zip_path}...")

    def __images__(self, fnm, zoomin: int = 1, page_from=0, page_to=MAXIMUM_PAGE_NUMBER, callback=None):
        self.page_from = page_from
        self.page_to = page_to
        self.page_images = None
        self.page_sizes = []
        try:
            source = fnm if isinstance(fnm, (str, PathLike)) else BytesIO(fnm)
            with sys.modules[LOCK_KEY_pdfplumber], pdfplumber.open(source) as pdf:
                self.pdf = pdf
                pages = self.pdf.pages[page_from:page_to]
                self.page_sizes = [(float(page.width), float(page.height)) for page in pages]
                self.page_images = [page.to_image(resolution=72 * zoomin, antialias=True).original for page in pages]
        except Exception:
            self.page_images = None
            self.total_page = len(self.page_sizes)
            self.logger.exception("[MinerU] Failed to render local PDF pages")

    def _pdf_bbox(self, page_idx: int, bbox) -> tuple[float, float, float, float]:
        x0, top, x1, bott = (float(value) for value in bbox)
        if x0 > x1:
            x0, x1 = x1, x0
        if top > bott:
            top, bott = bott, top

        page_sizes = getattr(self, "page_sizes", None) or []
        if 0 <= page_idx < len(page_sizes):
            page_width, page_height = page_sizes[page_idx]
            x0 = (x0 / 1000.0) * page_width
            x1 = (x1 / 1000.0) * page_width
            top = (top / 1000.0) * page_height
            bott = (bott / 1000.0) * page_height
        return x0, top, x1, bott

    def _line_tag(self, bx):
        pn = [bx["page_idx"] + 1]
        x0, top, x1, bott = self._pdf_bbox(bx["page_idx"], bx.get("bbox", (0, 0, 0, 0)))

        return "@@{}\t{:.1f}\t{:.1f}\t{:.1f}\t{:.1f}##".format("-".join([str(p) for p in pn]), x0, x1, top, bott)

    def crop(self, text, ZM=1, need_position=False):
        imgs = []
        poss = self.extract_positions(text)
        if not poss:
            if need_position:
                return None, None
            return

        if not getattr(self, "page_images", None):
            self.logger.warning("[MinerU] crop called without page images; skipping image generation.")
            if need_position:
                return None, None
            return

        page_count = len(self.page_images)

        filtered_poss = []
        for pns, left, right, top, bottom in poss:
            if not pns:
                self.logger.warning("[MinerU] Empty page index list in crop; skipping this position.")
                continue
            valid_pns = [p for p in pns if 0 <= p < page_count]
            if not valid_pns:
                self.logger.warning(f"[MinerU] All page indices {pns} out of range for {page_count} pages; skipping.")
                continue
            filtered_poss.append((valid_pns, left, right, top, bottom))

        poss = filtered_poss
        if not poss:
            self.logger.warning("[MinerU] No valid positions after filtering; skip cropping.")
            if need_position:
                return None, None
            return

        max_width = max(np.max([right - left for (_, left, right, _, _) in poss]), 6)
        GAP = 6
        pos = poss[0]
        first_page_idx = pos[0][0]
        poss.insert(0, ([first_page_idx], pos[1], pos[2], max(0, pos[3] - 120), max(pos[3] - GAP, 0)))
        pos = poss[-1]
        last_page_idx = pos[0][-1]
        if not (0 <= last_page_idx < page_count):
            self.logger.warning(f"[MinerU] Last page index {last_page_idx} out of range for {page_count} pages; skipping crop.")
            if need_position:
                return None, None
            return
        last_page_height = self.page_images[last_page_idx].size[1]
        poss.append(
            (
                [last_page_idx],
                pos[1],
                pos[2],
                min(last_page_height, pos[4] + GAP),
                min(last_page_height, pos[4] + 120),
            )
        )

        positions = []
        for ii, (pns, left, right, top, bottom) in enumerate(poss):
            right = left + max_width

            if bottom <= top:
                bottom = top + 2

            for pn in pns[1:]:
                if 0 <= pn - 1 < page_count:
                    bottom += self.page_images[pn - 1].size[1]
                else:
                    self.logger.warning(f"[MinerU] Page index {pn}-1 out of range for {page_count} pages during crop; skipping height accumulation.")

            if not (0 <= pns[0] < page_count):
                self.logger.warning(f"[MinerU] Base page index {pns[0]} out of range for {page_count} pages during crop; skipping this segment.")
                continue

            img0 = self.page_images[pns[0]]
            x0, y0, x1, y1 = int(left), int(top), int(right), int(min(bottom, img0.size[1]))
            if x0 > x1:
                x0, x1 = x1, x0
            if y0 > y1:
                y0, y1 = y1, y0
            if x1 <= x0 or y1 <= y0:
                continue
            crop0 = img0.crop((x0, y0, x1, y1))
            imgs.append(crop0)
            if 0 < ii < len(poss) - 1:
                positions.append((pns[0] + self.page_from, x0, x1, y0, y1))

            bottom -= img0.size[1]
            for pn in pns[1:]:
                if not (0 <= pn < page_count):
                    self.logger.warning(f"[MinerU] Page index {pn} out of range for {page_count} pages during crop; skipping this page.")
                    continue
                page = self.page_images[pn]
                x0, y0, x1, y1 = int(left), 0, int(right), int(min(bottom, page.size[1]))
                if x0 > x1:
                    x0, x1 = x1, x0
                if y0 > y1:
                    y0, y1 = y1, y0
                if x1 <= x0 or y1 <= y0:
                    bottom -= page.size[1]
                    continue
                cimgp = page.crop((x0, y0, x1, y1))
                imgs.append(cimgp)
                if 0 < ii < len(poss) - 1:
                    positions.append((pn + self.page_from, x0, x1, y0, y1))
                bottom -= page.size[1]

        if not imgs:
            if need_position:
                return None, None
            return

        height = 0
        for img in imgs:
            height += img.size[1] + GAP
        height = int(height)
        width = int(np.max([i.size[0] for i in imgs]))
        pic = Image.new("RGB", (width, height), (245, 245, 245))
        height = 0
        for ii, img in enumerate(imgs):
            if ii == 0 or ii + 1 == len(imgs):
                img = img.convert("RGBA")
                overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                overlay.putalpha(128)
                img = Image.alpha_composite(img, overlay).convert("RGB")
            pic.paste(img, (0, int(height)))
            height += img.size[1] + GAP

        if need_position:
            return pic, positions
        return pic

    @staticmethod
    def extract_positions(txt: str):
        poss = []
        for tag in re.findall(r"@@[0-9-]+\t[0-9.\t]+##", txt):
            pn, left, right, top, bottom = tag.strip("#").strip("@").split("\t")
            left, right, top, bottom = float(left), float(right), float(top), float(bottom)
            poss.append(([int(p) - 1 for p in pn.split("-")], left, right, top, bottom))
        return poss

    def _read_output(self, output_dir: Path, file_stem: str, method: str = "auto", backend: str = "hybrid-engine") -> list[dict[str, Any]]:
        json_file = None
        subdir = None
        attempted = []

        # mirror MinerU's sanitize_filename to align ZIP naming
        def _sanitize_filename(name: str) -> str:
            sanitized = re.sub(r"[/\\\.]{2,}|[/\\]", "", name)
            sanitized = re.sub(r"[^\w.-]", "_", sanitized, flags=re.UNICODE)
            if sanitized.startswith("."):
                sanitized = "_" + sanitized[1:]
            return sanitized or "unnamed"

        safe_stem = _sanitize_filename(file_stem)
        content_names = tuple(dict.fromkeys((f"{file_stem}_content_list.json", f"{safe_stem}_content_list.json")))
        allowed_names = set(content_names)
        self.logger.info(f"[MinerU] Expected output files: {', '.join(sorted(allowed_names))}")
        self.logger.info(f"[MinerU] Searching output in: {output_dir}")

        jf = output_dir / f"{file_stem}_content_list.json"
        self.logger.info(f"[MinerU] Trying original path: {jf}")
        attempted.append(jf)
        if jf.exists():
            subdir = output_dir
            json_file = jf
        else:
            alt = output_dir / f"{safe_stem}_content_list.json"
            self.logger.info(f"[MinerU] Trying sanitized filename: {alt}")
            attempted.append(alt)
            if alt.exists():
                subdir = output_dir
                json_file = alt
            else:
                nested_alt = output_dir / safe_stem / f"{safe_stem}_content_list.json"
                self.logger.info(f"[MinerU] Trying sanitized nested path: {nested_alt}")
                attempted.append(nested_alt)
                if nested_alt.exists():
                    subdir = nested_alt.parent
                    json_file = nested_alt
                else:
                    # Try vlm subdirectory (for vlm-http-client backend)
                    vlm_path = output_dir / "vlm" / f"{file_stem}_content_list.json"
                    self.logger.info(f"[MinerU] Trying vlm subdirectory: {vlm_path}")
                    attempted.append(vlm_path)
                    if vlm_path.exists():
                        subdir = vlm_path.parent
                        json_file = vlm_path
                    else:
                        vlm_safe = output_dir / "vlm" / f"{safe_stem}_content_list.json"
                        self.logger.info(f"[MinerU] Trying vlm subdirectory with sanitized name: {vlm_safe}")
                        attempted.append(vlm_safe)
                        if vlm_safe.exists():
                            subdir = vlm_safe.parent
                            json_file = vlm_safe

        if not json_file:
            parse_subdir = None
            if backend.startswith("pipeline"):
                parse_subdir = method
            elif backend.startswith("hybrid"):
                parse_subdir = f"hybrid_{method}"
            elif backend.startswith("vlm"):
                parse_subdir = "vlm"

            if parse_subdir:
                for content_name in content_names:
                    for candidate in output_dir.glob(f"**/{parse_subdir}/{content_name}"):
                        self.logger.info(f"[MinerU] Trying parse-method path: {candidate}")
                        attempted.append(candidate)
                        subdir = candidate.parent
                        json_file = candidate
                        break
                    if json_file:
                        break

        if not json_file:
            stem_dirs = tuple(dict.fromkeys((file_stem, safe_stem)))
            patterns = []
            if parse_subdir:
                for stem_dir in stem_dirs:
                    patterns.extend(
                        [
                            f"**/{stem_dir}/{parse_subdir}/content_list.json",
                            f"**/{stem_dir}/{parse_subdir}/*_content_list.json",
                        ]
                    )
                patterns.extend(
                    [
                        f"**/{parse_subdir}/content_list.json",
                        f"**/{parse_subdir}/*_content_list.json",
                    ]
                )
            for stem_dir in stem_dirs:
                patterns.extend(
                    [
                        f"**/{stem_dir}/content_list.json",
                        f"**/{stem_dir}/*_content_list.json",
                    ]
                )
            patterns.extend(["**/content_list.json", "**/*_content_list.json"])

            for pattern in patterns:
                for candidate in sorted(output_dir.glob(pattern)):
                    self.logger.info(f"[MinerU] Trying fallback path: {candidate}")
                    if candidate.name.endswith("_content_list.json"):
                        rel_parts = candidate.relative_to(output_dir).parts
                        in_stem_dir = any(stem_dir in rel_parts for stem_dir in stem_dirs)
                        stem_match = candidate.stem.startswith(file_stem) or candidate.stem.startswith(safe_stem)
                        if not (stem_match or in_stem_dir):
                            self.logger.info(f"[MinerU] Skip unrelated fallback candidate: {candidate}")
                            continue
                    attempted.append(candidate)
                    subdir = candidate.parent
                    json_file = candidate
                    break
                if json_file:
                    break

        if not json_file:
            raise FileNotFoundError(f"[MinerU] Missing output file, tried: {', '.join(str(p) for p in attempted)}")

        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        for item in data:
            for key in ("img_path", "table_img_path", "equation_img_path"):
                if key in item and item[key]:
                    item[key] = str((subdir / item[key]).resolve())
        return data

    @staticmethod
    def _image_semantic_text(output: dict[str, Any], *, include_vlm: bool = True) -> str:
        values = []
        for key in ("image_caption", "image_footnote", "chart_caption", "chart_footnote"):
            value = output.get(key, [])
            values.extend(value if isinstance(value, list) else [value])
        if output.get("type") == MinerUContentType.CHART:
            values.append(output.get("content", ""))
        if include_vlm:
            values.append(output.get("vlm_description", ""))

        unique = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in unique:
                unique.append(text)
        return "\n".join(unique)

    @staticmethod
    def _text_level(output: dict[str, Any]) -> int | None:
        level = output.get("text_level")
        if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 6:
            return level
        return None

    def _transfer_to_sections(self, outputs: list[dict[str, Any]], parse_method: str = None, table_enable: bool = False):
        sections = []
        projection_sections = []
        for source_index, output in enumerate(outputs):
            match output.get("type"):
                case MinerUContentType.TEXT:
                    section = output.get("text", "")
                case MinerUContentType.TABLE:
                    continue
                case MinerUContentType.IMAGE | MinerUContentType.CHART:
                    # Visual blocks with pixels are emitted through RAGFlow's
                    # media channel. If the ZIP image is missing, retain the
                    # semantic text as a searchable degradation.
                    if output.get("img_path") and os.path.isfile(output["img_path"]):
                        continue
                    section = self._image_semantic_text(output)
                case MinerUContentType.EQUATION:
                    section = output.get("text", "")
                case MinerUContentType.CODE:
                    section = output.get("code_body", "") + "\n".join(output.get("code_caption", []))
                case MinerUContentType.LIST:
                    section = "\n".join(output.get("list_items", []))
                case MinerUContentType.HEADER | MinerUContentType.FOOTER | MinerUContentType.PAGE_NUMBER | MinerUContentType.DISCARDED:
                    continue
                case _:
                    self.logger.debug("[MinerU] Skip unsupported section type=%s", output.get("type"))
                    continue

            if not table_enable:
                section = self._sanitize_section_text(section)
            if not section:
                self.logger.debug("[MinerU] Skip section after sanitization: type=%s", output.get("type"))
                continue

            level = self._text_level(output) if output.get("type") == MinerUContentType.TEXT else None
            layout_type = "title" if level is not None else output["type"]
            position_tag = self._line_tag(output)
            projection_section = {
                "text": section,
                "layout_type": layout_type,
                "position_tag": position_tag,
                "source_index": source_index,
            }
            if level is not None:
                projection_section["level"] = level
            projection_sections.append(projection_section)

            if section and parse_method in {"manual", "pipeline"}:
                typed_section = (section, layout_type, position_tag)
                sections.append((*typed_section, {"level": level}) if level is not None else typed_section)
            elif section and parse_method == "paper":
                sections.append((section + position_tag, layout_type))
            else:
                sections.append((section, position_tag))
        self.source_projection_sections = projection_sections
        return sections

    def _transfer_to_tables(self, outputs: list[dict[str, Any]]):
        media = []
        self.source_projection_media_indexes = []
        page_offset = int(getattr(self, "page_from", 0) or 0)
        for source_index, output in enumerate(outputs):
            output_type = output.get("type")
            if output_type not in {MinerUContentType.IMAGE, MinerUContentType.CHART, MinerUContentType.TABLE}:
                continue

            image = None
            image_path = (output.get("table_img_path") or output.get("img_path")) if output_type == MinerUContentType.TABLE else output.get("img_path")
            if image_path:
                if not os.path.isfile(image_path):
                    self.logger.warning("[MinerU] Image output is missing: %s", image_path)
                else:
                    try:
                        with Image.open(image_path) as source:
                            source.load()
                            image = source.copy()
                    except Exception as exc:
                        self.logger.warning("[MinerU] Cannot load image output %s: %s", image_path, exc)
            if output_type in {MinerUContentType.IMAGE, MinerUContentType.CHART} and image is None:
                continue

            try:
                local_page_idx = int(output.get("page_idx", 0))
                page_idx = local_page_idx + page_offset
                left, top, right, bottom = self._pdf_bbox(local_page_idx, output.get("bbox", (0, 0, 0, 0)))
            except (TypeError, ValueError):
                self.logger.warning("[MinerU] Invalid image position: page=%r bbox=%r", output.get("page_idx"), output.get("bbox"))
                continue

            if output_type == MinerUContentType.TABLE:
                values = [output.get("table_body", ""), *output.get("table_caption", []), *output.get("table_footnote", [])]
                text = "\n".join(str(value).strip() for value in values if str(value or "").strip())
                if not text:
                    text = "FAILED TO PARSE TABLE"
            else:
                text = self._image_semantic_text(output)
            media.append(((image, text), [[page_idx, left, right, top, bottom]]))
            self.source_projection_media_indexes.append(source_index)
        return media

    def _enhance_images_with_vlm(self, outputs: list[dict[str, Any]], vision_model, callback: Optional[Callable] = None):
        """Generate semantic descriptions for image blocks via the tenant's
        IMAGE2TEXT model, mirroring deepdoc's VisionFigureParser. Each
        IMAGE block with a readable img_path gets a ``vlm_description``
        field that ``_transfer_to_sections`` then folds into the chunk
        text — closing issue #14869.
        """
        from rag.app.picture import vision_llm_chunk
        from rag.prompts.generator import vision_llm_figure_describe_prompt

        image_jobs = [
            (idx, item)
            for idx, item in enumerate(outputs)
            if item.get("type") in {MinerUContentType.IMAGE, MinerUContentType.CHART}
            and item.get("img_path")
            and os.path.exists(item["img_path"])
            and not self._image_semantic_text(item, include_vlm=False)
        ]
        if not image_jobs:
            return

        if callback:
            callback(0.78, f"[MinerU] Generating VLM descriptions for {len(image_jobs)} images...")

        prompt = vision_llm_figure_describe_prompt()

        def worker(idx, item):
            try:
                def generate():
                    with Image.open(item["img_path"]) as img:
                        img.load()
                        return vision_llm_chunk(binary=img, vision_model=vision_model, prompt=prompt)

                desc = _cached_vlm_description(item["img_path"], vision_model, prompt, generate)
                return idx, desc
            except Exception as e:
                logging.warning(f"[MinerU] VLM description failed for image #{idx}: {e}")
                return idx, ""

        workers = min(len(image_jobs), _positive_env_int("VLM_PARALLEL_PER_DOC", 4))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, idx, item) for idx, item in image_jobs]
            for fut in as_completed(futures):
                idx, desc = fut.result()
                if desc:
                    outputs[idx]["vlm_description"] = desc

    def parse_pdf(
        self,
        filepath: str | PathLike[str],
        binary: BytesIO | bytes,
        callback: Optional[Callable] = None,
        *,
        output_dir: Optional[str] = None,
        backend: str = "hybrid-engine",
        server_url: Optional[str] = None,
        delete_output: bool = True,
        parse_method: str = "raw",
        from_page: int = 0,
        to_page: int = MAXIMUM_PAGE_NUMBER,
        **kwargs,
    ) -> tuple:
        if from_page < 0:
            raise ValueError("from_page must be non-negative")
        if to_page <= from_page:
            raise ValueError("to_page must be greater than from_page")

        if not is_mineru_file(str(filepath)):
            raise ValueError(f"Unsupported MinerU input format: {file_suffix(str(filepath)) or '<none>'}")

        suffix = file_suffix(str(filepath))
        is_pdf = suffix == "pdf"
        self.outlines = extract_pdf_outlines(binary if binary is not None else filepath) if is_pdf else []
        temp_input = None
        created_tmp_dir = False

        parser_cfg = kwargs.get("parser_config", {})
        lang = parser_cfg.get("mineru_lang") or kwargs.get("lang", "English")
        mineru_lang_code = LANGUAGE_TO_MINERU_MAP.get(lang, "ch")  # Defaults to Chinese if not matched
        mineru_method_raw_str = parser_cfg.get("mineru_parse_method", "auto")
        enable_formula = parser_cfg.get("mineru_formula_enable", True)
        enable_table = parser_cfg.get("mineru_table_enable", True)
        mineru_effort = parser_cfg.get("mineru_effort", "high")
        enable_image_analysis = parser_cfg.get("mineru_image_analysis", True)

        file_path = Path(filepath)
        input_file_name = file_path.stem.replace(" ", "") + file_path.suffix.lower()

        if binary:
            temp_dir = Path(tempfile.mkdtemp(prefix="mineru_input_"))
            temp_input = temp_dir / input_file_name
            with open(temp_input, "wb") as f:
                f.write(binary)
            input_file = temp_input
            self.logger.info(f"[MinerU] Received binary input -> {temp_input}")
            if callback:
                callback(0.15, f"[MinerU] Received binary input -> {temp_input}")
        else:
            input_file = file_path
            if not input_file.exists():
                if callback:
                    callback(-1, f"[MinerU] Input file not found: {input_file}")
                raise FileNotFoundError(f"[MinerU] Input file not found: {input_file}")

        if output_dir:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = Path(tempfile.mkdtemp(prefix="mineru_output_"))
            created_tmp_dir = True

        self.logger.info(f"[MinerU] Output directory: {out_dir} backend={backend} api={self.mineru_api} server_url={server_url or self.mineru_server_url}")
        if callback:
            callback(0.15, f"[MinerU] Output directory: {out_dir}")

        if is_pdf:
            self.__images__(input_file, zoomin=1, page_from=from_page, page_to=to_page)
        else:
            self.page_from = from_page
            self.page_to = to_page
            self.page_images = None
            self.page_sizes = []

        try:
            options = MinerUParseOptions(
                backend=MinerUBackend(backend),
                lang=MinerULanguage(mineru_lang_code),
                method=MinerUParseMethod(mineru_method_raw_str),
                server_url=server_url,
                delete_output=delete_output,
                parse_method=parse_method,
                formula_enable=enable_formula,
                table_enable=enable_table,
                effort=MinerUEffort(mineru_effort),
                image_analysis=enable_image_analysis,
                start_page_id=from_page,
                end_page_id=to_page - 1,
            )
            final_out_dir = self._run_mineru(input_file, out_dir, options, callback=callback)
            outputs = self._read_output(final_out_dir, input_file.stem, method=mineru_method_raw_str, backend=backend)
            self.logger.info(f"[MinerU] Parsed {len(outputs)} blocks from input.")
            if callback:
                callback(0.75, f"[MinerU] Parsed {len(outputs)} blocks from input.")

            vision_model = kwargs.get("vision_model")
            if vision_model is not None:
                try:
                    self._enhance_images_with_vlm(outputs, vision_model, callback=callback)
                except Exception as e:
                    self.logger.warning(f"[MinerU] VLM image enhancement failed: {e}. Continuing without descriptions.")

            return self._transfer_to_sections(outputs, parse_method, enable_table), self._transfer_to_tables(outputs)
        finally:
            if temp_input and temp_input.exists():
                try:
                    temp_input.unlink()
                    temp_input.parent.rmdir()
                except Exception:
                    pass
            if delete_output and created_tmp_dir and out_dir.exists():
                try:
                    shutil.rmtree(out_dir)
                except Exception:
                    pass


if __name__ == "__main__":
    parser = MinerUParser("mineru")
    ok, reason = parser.check_installation()
    print("MinerU available:", ok)

    filepath = ""
    with open(filepath, "rb") as file:
        outputs = parser.parse_pdf(filepath=filepath, binary=file.read())
        for output in outputs:
            print(output)
