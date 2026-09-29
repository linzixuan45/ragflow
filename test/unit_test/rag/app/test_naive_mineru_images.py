#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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

"""MinerU 非 PDF 文档的媒体图片就近挂载回归测试。

MinerU 的 office（docx/pptx/xlsx）输出把图片写成独立 item，`image_caption` 为空且
无 bbox，因此 `tokenize_table` 的 ``if not rows: continue`` 会直接丢弃这些媒体，
chunk 拿不到 ``image``，最终 ``img_id`` 为空。这里断言图片被挂到最近的文本 section，
从而变成带图 chunk。
"""

from __future__ import annotations

import sys
from importlib import import_module, reload
from unittest.mock import MagicMock

import pytest
from PIL import Image

_POSITION_TAG = "@@1\t0.0\t0.0\t0.0\t0.0##"


@pytest.fixture(scope="module")
def naive_module():
    """Load rag.app.naive with heavy optional dependencies stubbed locally."""
    stub_names = [
        "deepdoc.vision.ocr",
        "deepdoc.parser.figure_parser",
        "deepdoc.parser.docling_parser",
        "deepdoc.parser.tcadp_parser",
        "rag.app.picture",
    ]
    original_modules = {name: sys.modules.get(name) for name in stub_names}

    try:
        for name in stub_names:
            sys.modules[name] = MagicMock()
        module = reload(import_module("rag.app.naive"))
        yield module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _image(size=(8, 6), color=(255, 0, 0)) -> Image.Image:
    return Image.new("RGB", size, color)


def _section(text: str, source_index: int) -> dict:
    return {"text": text, "layout_type": "text", "position_tag": _POSITION_TAG, "source_index": source_index}


def test_image_attaches_to_the_following_text_section(naive_module):
    image = _image()

    images = naive_module._mineru_section_images(
        [_section("Alpha", 0), _section("Beta", 2)],
        [((image, ""), [[0, 0.0, 0.0, 0.0, 0.0]])],
        [1],
    )

    assert images == [None, image]


def test_trailing_image_attaches_to_the_last_text_section(naive_module):
    image = _image()

    images = naive_module._mineru_section_images(
        [_section("Alpha", 0)],
        [((image, ""), [[0, 0.0, 0.0, 0.0, 0.0]])],
        [2],
    )

    assert images == [image]


def test_adjacent_images_stack_into_one_host_section(naive_module):
    first = _image(size=(8, 6), color=(255, 0, 0))
    second = _image(size=(10, 4), color=(0, 0, 255))

    images = naive_module._mineru_section_images(
        [_section("Alpha", 0), _section("Beta", 3)],
        [((first, ""), [[0, 0.0, 0.0, 0.0, 0.0]]), ((second, ""), [[0, 0.0, 0.0, 0.0, 0.0]])],
        [1, 2],
    )

    assert images[0] is None
    assert images[1].size == (10, 10)


def test_captioned_media_keeps_the_existing_tokenize_table_path(naive_module):
    image = _image()

    assert (
        naive_module._mineru_section_images(
            [_section("Alpha", 0)],
            [((image, "Figure 1: latency by quarter"), [[0, 0.0, 0.0, 0.0, 0.0]])],
            [1],
        )
        is None
    )


def test_images_without_a_text_host_stay_projection_only(naive_module):
    image = _image()

    assert naive_module._mineru_section_images([], [((image, ""), [[0, 0.0, 0.0, 0.0, 0.0]])], [0]) is None


def test_no_captionless_media_returns_none(naive_module):
    assert naive_module._mineru_section_images([_section("Alpha", 0)], [], []) is None


def test_missing_source_indexes_fall_back_to_list_order(naive_module):
    image = _image()

    images = naive_module._mineru_section_images(
        [{"text": "Alpha", "layout_type": "text", "position_tag": _POSITION_TAG}],
        [((image, ""), [[0, 0.0, 0.0, 0.0, 0.0]])],
        None,
    )

    assert images == [image]
