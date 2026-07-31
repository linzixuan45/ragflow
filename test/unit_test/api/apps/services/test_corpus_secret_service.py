import pytest

from api.apps.services.corpus_secret_service import CorpusSecretError, corpus_secret


def test_corpus_secret_is_required_when_application_secret_is_unavailable(monkeypatch):
    monkeypatch.delenv("RAGFLOW_CORPUS_SECRET", raising=False)
    monkeypatch.setattr("api.apps.services.corpus_secret_service.settings.get_secret_key", lambda: None)

    with pytest.raises(CorpusSecretError, match="secret"):
        corpus_secret("citation")


def test_corpus_secret_derives_distinct_purpose_keys(monkeypatch):
    monkeypatch.setenv("RAGFLOW_CORPUS_SECRET", "test-corpus-secret-with-at-least-32-bytes")

    citation = corpus_secret("citation")
    cursor = corpus_secret("cursor")

    assert citation != cursor
    assert len(citation) == len(cursor) == 32
