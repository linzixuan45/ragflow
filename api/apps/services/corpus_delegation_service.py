"""Verification for Gateway-signed Corpus FS service delegation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


class CorpusDelegationError(ValueError):
    pass


@dataclass(frozen=True)
class CorpusDelegation:
    actor_id: str
    dataset_ids: frozenset[str]


class CorpusDelegationService:
    audience = "ragflow-corpus-keyword-search"

    @classmethod
    def verify(cls, token: str | None, service_tenant_id: str) -> CorpusDelegation:
        secret = os.environ.get("RAGFLOW_CORPUS_DELEGATION_SECRET")
        if not secret or not token:
            raise CorpusDelegationError("missing delegation")
        try:
            encoded_part, signature_part = token.split(".", 1)
            encoded = cls._unb64(encoded_part)
            signature = cls._unb64(signature_part)
            expected = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
            payload = json.loads(encoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CorpusDelegationError("invalid delegation") from None
        if not hmac.compare_digest(signature, expected):
            raise CorpusDelegationError("invalid delegation")
        actor_id = payload.get("actor_id")
        dataset_ids = payload.get("dataset_ids")
        if (
            payload.get("aud") != cls.audience
            or payload.get("service_tenant_id") != service_tenant_id
            or payload.get("exp", 0) < int(time.time())
            or not isinstance(actor_id, str)
            or not actor_id
            or not isinstance(dataset_ids, list)
            or not dataset_ids
            or not all(isinstance(dataset_id, str) and dataset_id for dataset_id in dataset_ids)
        ):
            raise CorpusDelegationError("invalid delegation")
        return CorpusDelegation(actor_id=actor_id, dataset_ids=frozenset(dataset_ids))

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
