import pytest

from api.apps.services.corpus_citation_service import CorpusCitationError, CorpusCitationService


def _target():
    return {
        "path": "/manual.pdf",
        "content_revision": 7,
        "projection_revision": 3,
        "page": 12,
        "block_ref": "41",
        "range": {"start": 120, "end": 188},
        "source_hash": "a" * 64,
    }


def test_citation_round_trip_preserves_versioned_target():
    service = CorpusCitationService(secret=b"test-secret")

    citation_ref = service.issue("tenant-1", "dataset-1", "document-1", _target())

    assert citation_ref.startswith("cit1.")
    assert service.resolve(citation_ref, "tenant-1", "dataset-1") == _target()
    assert service.resolve_locator(citation_ref, "tenant-1", "dataset-1") == ("document-1", _target())
    assert "manual.pdf" not in citation_ref


def test_citation_is_bound_to_tenant_and_dataset():
    service = CorpusCitationService(secret=b"test-secret")
    citation_ref = service.issue("tenant-1", "dataset-1", "document-1", _target())

    with pytest.raises(CorpusCitationError, match="not valid"):
        service.resolve(citation_ref, "tenant-2", "dataset-1")
    with pytest.raises(CorpusCitationError, match="not valid"):
        service.resolve(citation_ref, "tenant-1", "dataset-2")


def test_citation_rejects_tampering_and_invalid_contract():
    service = CorpusCitationService(secret=b"test-secret")
    citation_ref = service.issue("tenant-1", "dataset-1", "document-1", _target())

    with pytest.raises(CorpusCitationError, match="invalid citation"):
        service.resolve(citation_ref[:-1] + ("A" if citation_ref[-1] != "A" else "B"), "tenant-1", "dataset-1")
    with pytest.raises(CorpusCitationError, match="source_hash"):
        service.issue("tenant-1", "dataset-1", "document-1", {**_target(), "source_hash": ""})
