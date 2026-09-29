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

from pathlib import Path

PDF_SUFFIXES = ["pdf"]
IMAGE_SUFFIXES = ["png", "jpeg", "jp2", "webp", "gif", "bmp", "jpg", "tiff"]
DOCX_SUFFIXES = ["docx"]
PPTX_SUFFIXES = ["pptx"]
XLSX_SUFFIXES = ["xlsx"]
OFFICE_SUFFIXES = DOCX_SUFFIXES + PPTX_SUFFIXES + XLSX_SUFFIXES
MINERU_SUFFIXES = frozenset(PDF_SUFFIXES + IMAGE_SUFFIXES + OFFICE_SUFFIXES)

MINERU_MIME_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "jp2": "image/jp2",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def file_suffix(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


def is_mineru_file(filename: str) -> bool:
    return file_suffix(filename) in MINERU_SUFFIXES


def uses_source_projection(filename: str) -> bool:
    """Source projections are parser-owned, so every MinerU format carries one."""
    return is_mineru_file(filename)


def mineru_mime_type(filename: str) -> str:
    return MINERU_MIME_TYPES.get(file_suffix(filename), "application/octet-stream")
