"""Canonical path validation and display-name disambiguation for Corpus FS."""

from __future__ import annotations

from typing import Any


class CorpusPathError(ValueError):
    code = "INVALID_ARGUMENT"


class InvalidPathError(CorpusPathError):
    pass


class AmbiguousPathError(CorpusPathError):
    code = "AMBIGUOUS_PATH"


class CorpusPathResolver:
    @staticmethod
    def normalize(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or len(path.encode("utf-8")) > 1024:
            raise InvalidPathError("path must be an absolute UTF-8 path no longer than 1024 bytes")
        if path == "/":
            return path
        segments = path.split("/")[1:]
        if not segments or any(not part or part in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in part) for part in segments):
            raise InvalidPathError("path contains an invalid segment")
        return "/" + "/".join(segments)

    @classmethod
    def display_names(cls, parent_path: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parent_path = cls.normalize(parent_path)
        by_name: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            by_name.setdefault(entry["name"], []).append(entry)
        result = []
        for entry in entries:
            duplicate = by_name[entry["name"]]
            label = entry["name"]
            if len(duplicate) > 1:
                discriminant = entry.get("updated_at") or entry.get("create_date")
                if not discriminant:
                    raise AmbiguousPathError("duplicate paths require listing their parent directory")
                label = f"{label} ({discriminant})"
            result.append({**entry, "path": f"{parent_path.rstrip('/')}/{label}"})
        return result

    @staticmethod
    def resolve_name(name: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        matches = [entry for entry in entries if entry.get("name") == name]
        if len(matches) != 1:
            raise AmbiguousPathError("path is ambiguous")
        return matches[0]
