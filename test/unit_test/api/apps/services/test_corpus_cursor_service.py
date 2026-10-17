import json

import pytest

from api.apps.services.corpus_cursor_service import CorpusCursorError, CorpusCursorService, CorpusPageCursorService


def test_cursor_is_bound_to_tenant_dataset_document_and_content_hash():
    service = CorpusCursorService(secret=b"test-secret", ttl_seconds=60)
    token = service.issue("tenant-a", "dataset-a", "doc-a", "hash-a", 12, byte_offset=6)

    assert service.parse(token, "tenant-a", "dataset-a", "doc-a", "hash-a") == {"offset": 12, "byte_offset": 6}
    with pytest.raises(CorpusCursorError):
        service.parse(token, "tenant-b", "dataset-a", "doc-a", "hash-a")
    with pytest.raises(CorpusCursorError):
        service.parse(token, "tenant-a", "dataset-a", "doc-b", "hash-a")
    with pytest.raises(CorpusCursorError):
        service.parse(token + "x", "tenant-a", "dataset-a", "doc-a", "hash-a")
    encoded, signature = token.split(".")
    payload = {**json.loads(service._unb64(encoded)), "b": 3}
    tampered = f"{service._b64(service._encode(payload))}.{signature}"
    with pytest.raises(CorpusCursorError):
        service.parse(tampered, "tenant-a", "dataset-a", "doc-a", "hash-a")


@pytest.mark.parametrize("byte_offset", [-1, True, "6", 1.5])
def test_cursor_rejects_invalid_signed_block_byte_offsets(byte_offset):
    service = CorpusCursorService(secret=b"test-secret", ttl_seconds=60)
    token = service.issue("tenant-a", "dataset-a", "doc-a", "hash-a", 12, byte_offset=byte_offset)
    with pytest.raises(CorpusCursorError):
        service.parse(token, "tenant-a", "dataset-a", "doc-a", "hash-a")


def test_page_cursor_is_opaque_and_bound_to_request_scope():
    service = CorpusPageCursorService(secret=b"test-secret", ttl_seconds=60)
    context = {"actor": "actor", "dataset": "dataset", "operation": "grep", "revision": {"content_revision": 3}, "scope": {"query": "VPN", "path": "/"}}

    token = service.issue(context, 20)

    assert "dataset" not in token
    assert service.parse(token, context) == 20
    with pytest.raises(CorpusCursorError):
        service.parse(token, {**context, "scope": {"query": "other", "path": "/"}})
