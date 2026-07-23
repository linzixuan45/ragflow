"""Immutable parser-block snapshots for readable document source projections."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from api.db.db_models import DB, DocumentProjection
from common import settings
from common.misc_utils import get_uuid
from rag.flow.parser.pdf_chunk_metadata import extract_pdf_positions


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
        blocks: list[dict[str, Any]] = []
        image_parent: str | None = None
        for ordinal, item in enumerate(items):
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
            positions = extract_pdf_positions(item)
            page = positions[0][0] if positions else None
            block = {
                "block_ref": str(ordinal),
                "ordinal": ordinal,
                "kind": kind,
                "page": page,
                "positions": positions,
                "text": text,
                "parent_block_ref": image_parent if kind in {"image_ocr", "image_caption"} else None,
                "image_id": item.get("img_id") or None,
            }
            if kind == "image":
                image_parent = block["block_ref"]
            blocks.append(block)
        return blocks

    @classmethod
    @DB.connection_context()
    def write_pending(cls, document_id: str, dataset_id: str, source: bytes, parser_id: str, parser_config: dict[str, Any], items: list[dict[str, Any]]) -> int | None:
        blocks = cls.normalize_pdf_blocks(items)
        if not blocks:
            return None
        source_sha256 = hashlib.sha256(source).hexdigest()
        fingerprint = cls.parser_fingerprint(parser_id, parser_config)
        latest = (
            DocumentProjection.select(DocumentProjection.revision)
            .where(DocumentProjection.document_id == document_id)
            .order_by(DocumentProjection.revision.desc())
            .first()
        )
        revision = (latest.revision if latest else 0) + 1
        object_name = f"{cls._BUCKET_PREFIX}/{document_id}/{revision}.ndjson.gz"
        manifest = {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "parser_fingerprint": fingerprint,
            "projection_revision": revision,
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
        raw = settings.STORAGE_IMPL.get(row.dataset_id, row.object_name)
        if hasattr(raw, "read"):
            raw = raw.read()
        lines = gzip.decompress(raw).decode().splitlines()
        manifest = json.loads(lines[0])
        return manifest, [json.loads(line) for line in lines[1:]]
