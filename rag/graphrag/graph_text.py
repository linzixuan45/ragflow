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
"""Sanitize GraphRAG LLM text: language helpers, think-tag strip, noise filter."""

from __future__ import annotations

import re

GRAPH_FIELD_SEP = "<SEP>"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_PREFIX_RE = re.compile(r"^.*</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*$", re.IGNORECASE | re.DOTALL)
_NOISE_RE = re.compile(
    r"(?is)"
    r"\b("
    r"in thought"
    r"|keeping entity name"
    r"|skip duplicate"
    r"|already listed as"
    r"|the prompt asks"
    r"|i have already captured"
    r"|i will not duplicate"
    r"|type mapping"
    r"|wait,\s+already"
    r"|i will skip"
    r")\b"
    r"|跳过重复"
    r"|已经列出"
    r"|提示(词|要求)"
    r"|我已经(捕获|记录)"
    r"|类型映射"
    r"|不再重复"
)


def is_chinese_language(language: str | None) -> bool:
    value = (language or "").strip().lower().replace("_", "-")
    return value in {"chinese", "zh", "zh-cn", "zh-hans", "zh-sg", "中文"} or value.startswith("zh-")


def text_is_mostly_cjk(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha() or "\u3400" <= ch <= "\u9fff"]
    if not letters:
        return False
    cjk = sum(1 for ch in letters if "\u3400" <= ch <= "\u9fff")
    return cjk / len(letters) >= 0.3


def strip_graph_think(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_PREFIX_RE.sub("", cleaned)
    cleaned = _THINK_OPEN_RE.sub("", cleaned)
    return cleaned.strip()


def is_extraction_noise(text: str | None) -> bool:
    if not text or not text.strip():
        return True
    return bool(_NOISE_RE.search(text))


def sanitize_graph_description(text: str | None) -> str:
    cleaned = strip_graph_think(text)
    if not cleaned or is_extraction_noise(cleaned):
        return ""
    return cleaned.strip()


def join_graph_descriptions(descriptions: list[str] | tuple[str, ...]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in descriptions:
        cleaned = sanitize_graph_description(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        parts.append(cleaned)
    return GRAPH_FIELD_SEP.join(sorted(parts))


def split_graph_descriptions(text: str | None) -> list[str]:
    if not text:
        return []
    parts = []
    for item in re.split(r"<SEP>", text, flags=re.IGNORECASE):
        cleaned = sanitize_graph_description(item)
        if cleaned:
            parts.append(cleaned)
    return parts


def needs_description_summary(fragments: list[str], language: str | None) -> bool:
    if len(fragments) > 12:
        return True
    if not is_chinese_language(language):
        return False
    if len(fragments) > 1:
        return True
    return bool(fragments) and not text_is_mostly_cjk(fragments[0])
