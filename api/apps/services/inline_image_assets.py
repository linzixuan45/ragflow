"""Materialize inline ``data:image`` payloads into HMAC-bound corpus image assets.

Chunk text written by ``deepdoc.parser.mineru_parser._inline_local_image_refs`` may carry
``data:image/<mime>;base64,<b64>`` URLs. They are heavy and untraceable, so the read path
swaps each whole image for a ``[[image:dataset/<id><fs_path>|image_ref=<hex>]]`` placeholder
that the existing ``GET /datasets/<id>/fs/image`` contract can serve. The module is pure
logic plus injected storage access so it can be unit tested in isolation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Callable

# 单次调用允许资产化的 base64 字符预算：与 MinerU 内联上限 32MB 同量级，避免一个超大
# chunk 把所有内联图都搬进对象存储后撑爆响应体与写入耗时；超出部分按整张图保留。
DEFAULT_BUDGET_BYTES = 32 * 1024 * 1024

# 仅 base64 data URL 支持资产化；svg 等其它类型无法安全推导扩展名，一律保留原样。
_EXT_BY_MIME = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
}

# 匹配**整张图**（HTML 的 `<img ...>` 或 markdown 的 `![](...)`），且 src/目标必须是
# `data:image/...;base64,...`。替换产物是给前端 `convertBracketImages` 消费的独立 token，
# 因此必须整张替换；只替换 src 属性值会被包进属性里而失效。
# `data:text/html` 与 `data:image/svg+xml;charset=...` 这类形态不会命中。
_DATA_IMAGE_PATTERNS = (
    re.compile(
        r"<img\b[^>]*?\bsrc\s*=\s*[\"']data:image/(?P<mime>[a-zA-Z0-9.+-]+);base64,"
        r"(?P<b64>[A-Za-z0-9+/=]+)[\"'][^>]*>"
    ),
    re.compile(
        r"!\[[^\]]*\]\(\s*data:image/(?P<mime>[a-zA-Z0-9.+-]+);base64,"
        r"(?P<b64>[A-Za-z0-9+/=]+)\s*\)"
    ),
)

_HEX_REF_RE = re.compile(r"[0-9a-f]{64}")

_PREFIX = "inline/"


def _safe_object_name(object_name: str) -> bool:
    """Reject any constructed key that could escape the ``inline/`` namespace."""
    return object_name.startswith(_PREFIX) and ".." not in object_name and not os.path.isabs(object_name)


def _manifest_object_name(document_id: str) -> str:
    return f"{_PREFIX}{document_id}.index.json"


def _read_manifest_ids(dataset_id: str, document_id: str, get: Callable[[str, str], object]) -> list[str]:
    try:
        raw = get(dataset_id, _manifest_object_name(document_id))
        if hasattr(raw, "read"):
            raw = raw.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        ids = payload.get("image_ids") if isinstance(payload, dict) else None
        if isinstance(ids, list):
            return [item for item in ids if isinstance(item, str)]
    except Exception:  # noqa: BLE001 - Missing or unreadable optional storage must not break document reads.
        return []
    return []


def load_manifest(*, dataset_id: str, document_id: str, get: Callable[[str, str], object]) -> list[str]:
    """Return the document's registered image ids; missing or corrupt manifests read as ``[]``."""
    return _read_manifest_ids(dataset_id, document_id, get)


def _register_images(
    *,
    dataset_id: str,
    document_id: str,
    image_ids: list[str],
    get: Callable[[str, str], object],
    put: Callable[[str, str, bytes], object],
) -> dict:
    if not image_ids:
        return {}
    try:
        existing = _read_manifest_ids(dataset_id, document_id, get)
        merged = list(dict.fromkeys([*existing, *image_ids]))
        if set(merged) != set(existing):
            object_name = _manifest_object_name(document_id)
            if _safe_object_name(object_name):
                put(dataset_id, object_name, json.dumps({"image_ids": merged}).encode("utf-8"))
        return {"image_ids": merged}
    except Exception:  # noqa: BLE001 - Manifest registration is a best-effort storage boundary.
        return {}


def materialize_inline_images(
    text: str,
    *,
    dataset_id: str,
    document_id: str,
    fs_path: str,
    image_ref_of: Callable[[str], str],
    put: Callable[[str, str, bytes], object],
    exists: Callable[[str, str], bool],
    get: Callable[[str, str], object],
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> tuple[str, dict]:
    """Swap inline ``data:image`` URLs for ``[[image:<fs_path>|image_ref=<hex>]]`` placeholders.

    Best-effort: a decode, storage or manifest failure keeps that image's original data URL
    and the function never raises. Images are stored once per content hash under ``inline/``
    and registered in the document's manifest.
    """
    if "data:image" not in text:
        return text, {}

    registered: list[str] = []
    used = 0
    for pattern in _DATA_IMAGE_PATTERNS:
        text, used = _substitute_images(
            text,
            pattern,
            dataset_id=dataset_id,
            fs_path=fs_path,
            image_ref_of=image_ref_of,
            put=put,
            exists=exists,
            registered=registered,
            used=used,
            budget_bytes=budget_bytes,
        )

    manifest = _register_images(dataset_id=dataset_id, document_id=document_id, image_ids=registered, get=get, put=put)
    if manifest:
        manifest["materialized_image_ids"] = list(dict.fromkeys(registered))
    return text, manifest


def _substitute_images(
    text: str,
    pattern: re.Pattern[str],
    *,
    dataset_id: str,
    fs_path: str,
    image_ref_of: Callable[[str], str],
    put: Callable[[str, str, bytes], object],
    exists: Callable[[str, str], bool],
    registered: list[str],
    used: int,
    budget_bytes: int,
) -> tuple[str, int]:
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(text[cursor : match.start()])
        cursor = match.end()
        replacement = match.group(0)
        try:
            encoded = match.group("b64")
            data = base64.b64decode(encoded, validate=True)
            ext = _EXT_BY_MIME.get(match.group("mime").lower())
            if ext is None or used + len(encoded) > budget_bytes:
                parts.append(replacement)
                continue
            object_name = f"{_PREFIX}{hashlib.sha256(data).hexdigest()}.{ext}"
            if not _safe_object_name(object_name):
                parts.append(replacement)
                continue
            if not exists(dataset_id, object_name):
                put(dataset_id, object_name, data)
            image_id = f"{dataset_id}-{object_name}"
            image_ref = image_ref_of(image_id)
            if not _HEX_REF_RE.fullmatch(image_ref):
                parts.append(replacement)
                continue
            used += len(encoded)
            registered.append(image_id)
            # 标记自带 dataset 限定符：前端据此生成认证取图 URL，无需额外上下文。
            # 路径归一化为单前导斜杠：投影里的 entry 路径可能是 `//name`，而取图路由拒绝双斜杠。
            marker_path = "/" + fs_path.lstrip("/")
            replacement = f"[[image:dataset/{dataset_id}{marker_path}|image_ref={image_ref}]]"
        except Exception:  # noqa: BLE001 - Preserve the original image when any injected storage operation fails.
            replacement = match.group(0)
        parts.append(replacement)
    parts.append(text[cursor:])
    return "".join(parts), used
