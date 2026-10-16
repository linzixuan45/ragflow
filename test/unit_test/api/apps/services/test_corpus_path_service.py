import pytest

from api.apps.services.corpus_path_service import (
    AmbiguousPathError,
    CorpusPathResolver,
    InvalidPathError,
)


@pytest.mark.parametrize("path", ["relative", "/a//b", "/a/./b", "/a/../b", "/a/\x00b", "/" + "a" * 1025])
def test_normalize_rejects_non_canonical_paths(path):
    with pytest.raises(InvalidPathError):
        CorpusPathResolver.normalize(path)


def test_duplicate_display_name_requires_a_discriminant():
    resolver = CorpusPathResolver()
    entries = [
        {"name": "policy.pdf", "document_id": "one", "updated_at": "2026-01-01"},
        {"name": "policy.pdf", "document_id": "two", "updated_at": "2026-02-01"},
    ]

    paths = resolver.display_names("/files/hr", entries)
    assert paths[0]["path"] != paths[1]["path"]
    assert all("document_id" not in value["path"] for value in paths)
    with pytest.raises(AmbiguousPathError):
        resolver.resolve_name("policy.pdf", entries)


def test_normalize_round_trip_preserves_utf8_names():
    assert CorpusPathResolver.normalize("/files/员工手册.md") == "/files/员工手册.md"
