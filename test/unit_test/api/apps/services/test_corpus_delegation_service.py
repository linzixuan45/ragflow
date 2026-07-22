import base64
import hashlib
import hmac
import json
import time

import pytest

from api.apps.services.corpus_delegation_service import CorpusDelegationError, CorpusDelegationService


def _token(secret: str, payload: dict) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(encoded).rstrip(b'=').decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def test_delegation_requires_valid_signature_audience_tenant_and_expiry(monkeypatch):
    monkeypatch.setenv("RAGFLOW_CORPUS_DELEGATION_SECRET", "test-secret")
    payload = {"actor_id": "user-1", "dataset_ids": ["dataset-1"], "service_tenant_id": "service-tenant", "aud": CorpusDelegationService.audience, "exp": int(time.time()) + 60}

    delegation = CorpusDelegationService.verify(_token("test-secret", payload), "service-tenant")

    assert delegation.actor_id == "user-1"
    assert delegation.dataset_ids == {"dataset-1"}
    with pytest.raises(CorpusDelegationError):
        CorpusDelegationService.verify(_token("wrong-secret", payload), "service-tenant")
    with pytest.raises(CorpusDelegationError):
        CorpusDelegationService.verify(_token("test-secret", {**payload, "exp": 0}), "service-tenant")
