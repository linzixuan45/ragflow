"""Purpose-separated secrets for Corpus FS opaque references."""

from __future__ import annotations

import hashlib
import hmac
import os

from common import settings


class CorpusSecretError(RuntimeError):
    pass


def corpus_secret(purpose: str) -> bytes:
    if not purpose:
        raise CorpusSecretError("Corpus FS secret purpose is required")
    root = os.environ.get("RAGFLOW_CORPUS_SECRET") or settings.get_secret_key()
    if isinstance(root, str):
        root = root.encode()
    if not isinstance(root, bytes) or len(root) < 32:
        raise CorpusSecretError("A stable Corpus FS secret of at least 32 bytes is required")
    return hmac.new(root, f"corpus-fs:{purpose}".encode(), hashlib.sha256).digest()
