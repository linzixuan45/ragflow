"""Authenticated opaque cursors used by Corpus FS continuations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from cryptography.fernet import Fernet, InvalidToken


class CorpusCursorError(ValueError):
    pass


class CorpusPageCursorService:
    """Encrypted continuations for pagination over a stable Corpus FS scope."""

    def __init__(self, secret: bytes, ttl_seconds: int = 300):
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))
        self._ttl_seconds = ttl_seconds

    def issue(self, context: dict, offset: int) -> str:
        if offset < 0:
            raise CorpusCursorError("invalid cursor offset")
        payload = {"c": context, "o": offset, "e": int(time.time()) + self._ttl_seconds}
        return self._fernet.encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode()

    def parse(self, token: str, context: dict) -> int:
        try:
            payload = json.loads(self._fernet.decrypt(token.encode(), ttl=self._ttl_seconds))
        except (InvalidToken, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise CorpusCursorError("invalid cursor") from None
        if payload.get("e", 0) < int(time.time()) or payload.get("c") != context:
            raise CorpusCursorError("cursor is expired or not valid for this request")
        if not isinstance(payload.get("o"), int) or payload["o"] < 0:
            raise CorpusCursorError("invalid cursor")
        return payload["o"]


class CorpusCursorService:
    def __init__(self, secret: bytes, ttl_seconds: int = 300):
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, tenant_id: str, dataset_id: str, document_id: str, content_hash: str, offset: int, *, byte_offset: int = 0) -> str:
        payload = {"t": tenant_id, "d": dataset_id, "doc": document_id, "h": content_hash, "o": offset, "b": byte_offset, "e": int(time.time()) + self._ttl_seconds}
        encoded = self._encode(payload)
        signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return f"{self._b64(encoded)}.{self._b64(signature)}"

    def parse(self, token: str, tenant_id: str, dataset_id: str, document_id: str, content_hash: str) -> dict:
        try:
            encoded_part, signature_part = token.split(".", 1)
            encoded, signature = self._unb64(encoded_part), self._unb64(signature_part)
            expected = hmac.new(self._secret, encoded, hashlib.sha256).digest()
            payload = json.loads(encoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CorpusCursorError("invalid cursor") from None
        if not hmac.compare_digest(signature, expected):
            raise CorpusCursorError("invalid cursor")
        if payload.get("e", 0) < int(time.time()) or (payload.get("t"), payload.get("d"), payload.get("doc"), payload.get("h")) != (tenant_id, dataset_id, document_id, content_hash):
            raise CorpusCursorError("cursor is expired or not valid for this document")
        if not isinstance(payload.get("o"), int) or payload["o"] < 0:
            raise CorpusCursorError("invalid cursor")
        byte_offset = payload.get("b", 0)
        if not isinstance(byte_offset, int) or isinstance(byte_offset, bool) or byte_offset < 0:
            raise CorpusCursorError("invalid cursor")
        return {"offset": payload["o"], "byte_offset": byte_offset}

    @staticmethod
    def _encode(payload: dict) -> bytes:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
