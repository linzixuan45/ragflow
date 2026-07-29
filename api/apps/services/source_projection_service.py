"""Immutable parser-block snapshots for readable document source projections."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from typing import Any

from api.db.db_models import DB, DocumentProjection, Task
from common import settings
from common.misc_utils import get_uuid


def _extract_pdf_positions(item: dict[str, Any]) -> list[list[int | float]]:
    positions = item.get("_pdf_positions") or item.get("positions") or item.get("position_int") or []
    if not positions and isinstance(item.get("position_tag"), str):
        positions = [
            [int(page), float(left), float(right), float(top), float(bottom)]
            for page, left, right, top, bottom in re.findall(
                r"@@(\d+)\t([\d.]+)\t([\d.]+)\t([\d.]+)\t([\d.]+)",
                item["position_tag"],
            )
        ]
    normalized = []
    for position in positions:
        if not isinstance(position, (list, tuple)) or len(position) < 5:
            continue
        try:
            page = int(position[0][-1] if isinstance(position[0], list) else position[0])
            normalized.append([page if page > 0 else page + 1, *[float(value) for value in position[1:5]]])
        except (TypeError, ValueError):
            continue
    return normalized


class SourceProjectionService:
    _BUCKET_PREFIX = "source-projections"

    @staticmethod
    def parser_fingerprint(parser_id: str, parser_config: dict[str, Any], implementation_version: str = "parser-blocks-v1") -> str:
        payload = {
            "implementation_version": implementation_version,
            "parser_id": parser_id,
            "parser_config": parser_config,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def normalize_pdf_blocks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for source_index, item in enumerate(items):
            text = item.get("text")
            if not isinstance(text, str):
                continue
            layout = str(item.get("layout_type") or "").strip().lower()
            kind = {
                "title": "heading",
                "heading": "heading",
                "table": "table",
                "figure": "image",
                "image": "image",
                "picture": "image",
                "ocr": "image_ocr",
                "image_ocr": "image_ocr",
                "caption": "image_caption",
                "figure caption": "image_caption",
                "image caption": "image_caption",
                "header": "header_footer",
                "footer": "header_footer",
            }.get(layout, "paragraph")
            positions = _extract_pdf_positions(item)
            page = positions[0][0] if positions else None
            block = {
                "kind": kind,
                "page": page,
                "positions": positions,
                "text": text,
                "parent_block_ref": None,
                "image_id": item.get("img_id") or None,
                "_source_index": source_index,
                "_source_ref": item.get("source_ref"),
                "_parent_source_ref": item.get("parent_block_ref"),
            }
            level = item.get("level")
            if kind == "heading" and isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 6:
                block["level"] = level
            if item.get("image") is not None:
                block["_image"] = item["image"]
            candidates.append(block)

        candidates.sort(
            key=lambda block: (
                0,
                block["positions"][0][0],
                block["positions"][0][3],
                block["positions"][0][1],
                block["_source_index"],
            )
            if block["positions"]
            else (1, block["_source_index"], 0, 0, 0)
        )
        refs = {
            block["_source_ref"]: str(ordinal)
            for ordinal, block in enumerate(candidates)
            if isinstance(block["_source_ref"], str)
        }
        blocks: list[dict[str, Any]] = []
        image_parent_by_page: dict[int | float | None, str] = {}
        for ordinal, block in enumerate(candidates):
            block["block_ref"] = str(ordinal)
            block["ordinal"] = ordinal
            parent_ref = block.pop("_parent_source_ref")
            if block["kind"] in {"image_ocr", "image_caption"}:
                block["parent_block_ref"] = refs.get(parent_ref) if isinstance(parent_ref, str) else image_parent_by_page.get(block["page"])
            if block["kind"] == "image":
                image_parent_by_page[block["page"]] = block["block_ref"]
            block.pop("_source_index")
            block.pop("_source_ref")
            blocks.append(block)
        return blocks

    @staticmethod
    def _image_bytes(image: Any) -> bytes:
        if isinstance(image, bytes):
            return image
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def merge_projection_blocks(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for block in [*existing, *incoming]:
            identity = json.dumps(
                {
                    "kind": block.get("kind"),
                    "level": block.get("level"),
                    "positions": block.get("positions") or [],
                    "text": block.get("text") or "",
                    "image_id": block.get("image_id"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            unique[identity] = block.copy()
        blocks = sorted(
            unique.values(),
            key=lambda block: (
                0,
                block["positions"][0][0],
                block["positions"][0][3],
                block["positions"][0][1],
            )
            if block.get("positions")
            else (1, block.get("ordinal", 0), 0, 0),
        )
        image_parent_by_page: dict[int | float | None, str] = {}
        for ordinal, block in enumerate(blocks):
            block["block_ref"] = str(ordinal)
            block["ordinal"] = ordinal
            if block.get("kind") == "image":
                image_parent_by_page[block.get("page")] = block["block_ref"]
            elif block.get("kind") in {"image_ocr", "image_caption"}:
                block["parent_block_ref"] = image_parent_by_page.get(block.get("page"))
        return blocks

    @classmethod
    def _read_row(cls, row: DocumentProjection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raw = settings.STORAGE_IMPL.get(row.dataset_id, row.object_name)
        if hasattr(raw, "read"):
            raw = raw.read()
        lines = gzip.decompress(raw).decode().splitlines()
        return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]

    @classmethod
    def _store_block_images(cls, dataset_id: str, document_id: str, revision: int, blocks: list[dict[str, Any]]) -> None:
        for block in blocks:
            image = block.pop("_image", None)
            image_data: bytes | None = cls._image_bytes(image) if image is not None else None
            image_id = block.get("image_id")
            if image_data is None and isinstance(image_id, str) and "-" in image_id:
                bucket, source_name = image_id.split("-", 1)
                source = settings.STORAGE_IMPL.get(bucket, source_name)
                image_data = source.read() if hasattr(source, "read") else source
            if image_data is None:
                continue
            image_name = f"{cls._BUCKET_PREFIX}/{document_id}/{revision}/images/{block['block_ref']}.png"
            settings.STORAGE_IMPL.put(dataset_id, image_name, image_data)
            block["image_id"] = f"{dataset_id}-{image_name}"

    @classmethod
    @DB.connection_context()
    def write_pending(
        cls,
        document_id: str,
        dataset_id: str,
        source: bytes,
        parser_id: str,
        parser_config: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> int | None:
        blocks = cls.normalize_pdf_blocks(items)
        if not blocks:
            return None
        source_sha256 = hashlib.sha256(source).hexdigest()
        fingerprint = cls.parser_fingerprint(parser_id, parser_config)
        latest_task = Task.select(Task.begin_at).where(Task.doc_id == document_id).order_by(Task.begin_at.desc()).first()
        generation = latest_task.begin_at.isoformat() if latest_task and latest_task.begin_at else "standalone"
        latest = (
            DocumentProjection.select()
            .where(DocumentProjection.document_id == document_id)
            .order_by(DocumentProjection.revision.desc())
            .first()
        )
        revision = (latest.revision if latest else 0) + 1
        object_name = f"{cls._BUCKET_PREFIX}/{document_id}/{revision}.ndjson.gz"
        cls._store_block_images(dataset_id, document_id, revision, blocks)
        if latest and latest.source_sha256 == source_sha256 and latest.parser_fingerprint == fingerprint:
            previous_manifest, previous_blocks = cls._read_row(latest)
            if previous_manifest.get("generation") == generation:
                blocks = cls.merge_projection_blocks(previous_blocks, blocks)
            else:
                blocks = cls.merge_projection_blocks([], blocks)
        else:
            blocks = cls.merge_projection_blocks([], blocks)
        manifest = {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "parser_fingerprint": fingerprint,
            "projection_revision": revision,
            "generation": generation,
            "created_at": datetime.now(UTC).isoformat(),
            "block_count": len(blocks),
        }
        payload = "\n".join([json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))] + [json.dumps(block, ensure_ascii=False, separators=(",", ":")) for block in blocks]).encode()
        settings.STORAGE_IMPL.put(dataset_id, object_name, gzip.compress(payload))
        DocumentProjection.create(
            id=get_uuid(),
            document_id=document_id,
            dataset_id=dataset_id,
            revision=revision,
            state="PENDING",
            source_sha256=source_sha256,
            parser_fingerprint=fingerprint,
            object_name=object_name,
            block_count=len(blocks),
        )
        return revision

    @classmethod
    @DB.connection_context()
    def publish(cls, document_id: str) -> None:
        pending = (
            DocumentProjection.select()
            .where((DocumentProjection.document_id == document_id) & (DocumentProjection.state == "PENDING"))
            .order_by(DocumentProjection.revision.desc())
            .first()
        )
        if not pending:
            return
        with DB.atomic():
            DocumentProjection.update(state="RETIRED").where((DocumentProjection.document_id == document_id) & (DocumentProjection.state == "ACTIVE")).execute()
            DocumentProjection.update(state="ACTIVE").where(DocumentProjection.id == pending.id).execute()

    @classmethod
    @DB.connection_context()
    def fail_pending(cls, document_id: str) -> None:
        DocumentProjection.update(state="FAILED").where((DocumentProjection.document_id == document_id) & (DocumentProjection.state == "PENDING")).execute()

    @classmethod
    @DB.connection_context()
    def active(cls, document: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        row = (
            DocumentProjection.select()
            .where((DocumentProjection.document_id == document["id"]) & (DocumentProjection.state == "ACTIVE"))
            .order_by(DocumentProjection.revision.desc())
            .first()
        )
        if not row:
            return None
        return cls._read_row(row)

    @classmethod
    @DB.connection_context()
    def revision(cls, document_id: str, projection_revision: int, source_hash: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        row = (
            DocumentProjection.select()
            .where(
                (DocumentProjection.document_id == document_id)
                & (DocumentProjection.revision == projection_revision)
                & (DocumentProjection.source_sha256 == source_hash)
                & (DocumentProjection.state.in_(["ACTIVE", "RETIRED"]))
            )
            .first()
        )
        if not row:
            return None
        return cls._read_row(row)
