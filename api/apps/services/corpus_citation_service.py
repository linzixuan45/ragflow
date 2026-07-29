"""Authenticated, durable references to versioned Corpus FS evidence."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken


class CorpusCitationError(ValueError):
    pass


class CorpusCitationService:
    _PREFIX = "cit1."
    _TARGET_FIELDS: ClassVar[set[str]] = {
        "path",
        "content_revision",
        "projection_revision",
        "page",
        "block_ref",
        "range",
        "source_hash",
    }

    def __init__(self, secret: bytes):
        key = base64.urlsafe_b64encode(hashlib.sha256(b"corpus-citation-v1:" + secret).digest())
        self._fernet = Fernet(key)

    def issue(self, tenant_id: str, dataset_id: str, document_id: str, target: dict) -> str:
        if not isinstance(document_id, str) or not document_id or len(document_id.encode()) > 512:
            raise CorpusCitationError("invalid citation document locator")
        normalized = self._validate_target(target)
        payload = {"v": 1, "tenant_id": tenant_id, "dataset_id": dataset_id, "document_id": document_id, "target": normalized}
        token = self._fernet.encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode()
        return self._PREFIX + token

    def resolve(self, citation_ref: str, tenant_id: str, dataset_id: str) -> dict:
        return self.resolve_locator(citation_ref, tenant_id, dataset_id)[1]

    def resolve_locator(self, citation_ref: str, tenant_id: str, dataset_id: str) -> tuple[str, dict]:
        if not isinstance(citation_ref, str) or not citation_ref.startswith(self._PREFIX):
            raise CorpusCitationError("invalid citation")
        try:
            payload = json.loads(self._fernet.decrypt(citation_ref[len(self._PREFIX) :].encode()))
        except (InvalidToken, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise CorpusCitationError("invalid citation") from None
        if payload.get("v") != 1 or (payload.get("tenant_id"), payload.get("dataset_id")) != (tenant_id, dataset_id):
            raise CorpusCitationError("citation is not valid for this dataset")
        document_id = payload.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise CorpusCitationError("invalid citation")
        return document_id, self._validate_target(payload.get("target"))

    @classmethod
    def _validate_target(cls, target: object) -> dict:
        if not isinstance(target, dict) or set(target) != cls._TARGET_FIELDS:
            raise CorpusCitationError("invalid citation target")
        path = target.get("path")
        if not isinstance(path, str) or not path.startswith("/") or len(path.encode()) > 4096:
            raise CorpusCitationError("invalid citation path")
        content_revision = target.get("content_revision")
        if isinstance(content_revision, bool) or not isinstance(content_revision, int) or content_revision < 1:
            raise CorpusCitationError("invalid citation content_revision")
        projection_revision = target.get("projection_revision")
        if projection_revision is not None and (isinstance(projection_revision, bool) or not isinstance(projection_revision, int) or projection_revision < 1):
            raise CorpusCitationError("invalid citation projection_revision")
        page = target.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
            raise CorpusCitationError("invalid citation page")
        block_ref = target.get("block_ref")
        if block_ref is not None and (not isinstance(block_ref, str) or not block_ref or len(block_ref.encode()) > 512):
            raise CorpusCitationError("invalid citation block_ref")
        byte_range = target.get("range")
        if byte_range is not None:
            if not isinstance(byte_range, dict) or set(byte_range) != {"start", "end"}:
                raise CorpusCitationError("invalid citation range")
            start, end = byte_range.get("start"), byte_range.get("end")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)) or start < 0 or end < start:
                raise CorpusCitationError("invalid citation range")
        source_hash = target.get("source_hash")
        if not isinstance(source_hash, str) or len(source_hash) != 64 or any(character not in "0123456789abcdef" for character in source_hash.lower()):
            raise CorpusCitationError("invalid citation source_hash")
        return {field: target[field] for field in cls._TARGET_FIELDS}
