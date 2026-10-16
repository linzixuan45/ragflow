"""Corpus FS error taxonomy and transient-I/O classification."""

from __future__ import annotations

import errno

from peewee import InterfaceError, OperationalError


class CorpusFsError(ValueError):
    code = "INVALID_ARGUMENT"
    status = 400


class NotFoundError(CorpusFsError):
    code = "NOT_FOUND"
    status = 404


class VersionNotFoundError(CorpusFsError):
    code = "VERSION_NOT_FOUND"
    status = 404


class DocumentNotReadyError(CorpusFsError):
    code = "DOCUMENT_NOT_READY"
    status = 409


class UnsupportedContentError(CorpusFsError):
    code = "UNSUPPORTED_CONTENT"
    status = 415


class ImageNotFoundError(CorpusFsError):
    code = "IMAGE_NOT_FOUND"
    status = 404


class MoveCycleError(CorpusFsError):
    code = "MOVE_CYCLE"
    status = 422


class EntryConflictError(CorpusFsError):
    code = "ENTRY_CONFLICT"
    status = 409


class InvalidConfirmationError(CorpusFsError):
    code = "INVALID_CONFIRMATION"
    status = 409


class RevisionConflictError(CorpusFsError):
    code = "REVISION_CONFLICT"
    status = 409


class PreconditionRequiredError(CorpusFsError):
    code = "PRECONDITION_REQUIRED"
    status = 428


class CorpusUnavailableError(CorpusFsError):
    """Transient Corpus FS I/O, not a client argument error."""

    code = "UNAVAILABLE"
    status = 503


_UNAVAILABLE_ERRNOS = {
    errno.EBADF,
    errno.EPIPE,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ENOTCONN,
    errno.ETIMEDOUT,
}


def is_unavailable_io(error: BaseException) -> bool:
    """True for closed/reset MySQL sockets, not missing files or permission errors."""
    if isinstance(error, (InterfaceError, OperationalError)):
        return True
    if isinstance(error, FileNotFoundError):
        return False
    return isinstance(error, OSError) and error.errno in _UNAVAILABLE_ERRNOS
