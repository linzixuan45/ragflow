#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import yaml

MERGE_PATTERNS = ("<<<<<<< ", "=======\n", ">>>>>>> ")

# Printable ASCII (0x20-0x7E) plus newline — matches the regex used by the
# historical check_comment_ascii.py.
_PRINTABLE_ASCII = re.compile(r"^[\n -~]*\Z")


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _staged_paths() -> list[Path]:
    """Paths to check: staged files locally, or an explicit list in CI.

    ``CHECK_FILES_FILES`` (newline separated) overrides the staged set so CI can
    reuse every check on a pull request's changed files.
    """
    override = os.environ.get("CHECK_FILES_FILES")
    if override is not None:
        return [Path(line) for line in override.splitlines() if line.strip()]
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(line) for line in proc.stdout.splitlines() if line]


def _report(errors: list[str]) -> int:
    if not errors:
        return 0
    for error in errors:
        print(error, file=sys.stderr)
    return 1


def check_json(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if path.suffix != ".json" or not path.is_file():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            errors.append(f"invalid json: {path}: {exc}")
    return _report(errors)


def check_yaml(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if path.suffix not in {".yaml", ".yml"} or not path.is_file():
            continue
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            errors.append(f"invalid yaml: {path}: {exc}")
    return _report(errors)


def check_eof(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        data = _read_bytes(path)
        if data and not data.endswith(b"\n"):
            if fix:
                with path.open("ab") as f:
                    f.write(b"\n")
                print(f"fixed missing-trailing-newline: {path}", file=sys.stderr)
            else:
                errors.append(f"missing trailing newline: {path}")
    return 0 if fix else _report(errors)


_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\r?\n|$)")


def check_trailing_whitespace(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        if not text:
            continue
        new_text = _TRAILING_WS_RE.sub("", text)
        if new_text == text:
            continue
        if fix:
            path.write_text(new_text, encoding="utf-8")
            print(f"fixed trailing-whitespace: {path}", file=sys.stderr)
        else:
            old_lines = text.splitlines()
            new_lines = new_text.splitlines()
            for i, (orig, new) in enumerate(zip(old_lines, new_lines), 1):
                if orig != new:
                    errors.append(f"trailing whitespace: {path}:{i}")
    return 0 if fix else _report(errors)


def check_mixed_line_endings(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        data = _read_bytes(path)
        has_crlf = b"\r\n" in data
        has_lf = b"\n" in data.replace(b"\r\n", b"")
        if has_crlf and has_lf:
            if fix:
                path.write_bytes(data.replace(b"\r\n", b"\n"))
                print(f"fixed mixed-line-ending: {path}", file=sys.stderr)
            else:
                errors.append(f"mixed line endings: {path}")
    return 0 if fix else _report(errors)


def check_merge_conflicts(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if all(pattern in text for pattern in MERGE_PATTERNS):
            errors.append(f"merge conflict markers: {path}")
    return _report(errors)


def check_symlinks(paths: list[Path], fix: bool = False) -> int:
    errors: list[str] = []
    for path in paths:
        if path.is_symlink() and not path.exists():
            errors.append(f"broken symlink: {path}")
    return _report(errors)


def check_case_conflicts(_: list[Path], fix: bool = False) -> int:
    proc = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    seen: dict[str, str] = {}
    errors: list[str] = []
    for path in proc.stdout.splitlines():
        lowered = path.lower()
        other = seen.get(lowered)
        if other and other != path:
            errors.append(f"case conflict: {other} <-> {path}")
        seen[lowered] = path
    return _report(errors)


def check_comment_ascii(paths: list[Path], fix: bool = False) -> int:
    """Ensure Python comments and docstrings contain only ASCII characters.

    Ported from the legacy check_comment_ascii.py. The fix flag is accepted
    for signature consistency but no auto-fix exists — non-ASCII comments
    must be rewritten by hand.
    """
    errors: list[str] = []
    for path in paths:
        if path.suffix != ".py" or not path.is_file():
            continue
        # A common comment begins with `#`
        try:
            with tokenize.open(path) as fp:
                for tk in tokenize.generate_tokens(fp.readline):
                    if tk.type == tokenize.COMMENT and not _PRINTABLE_ASCII.fullmatch(tk.string):
                        errors.append(f"non-ASCII comment: {path}:{tk.start[0]}: {tk.string}")
        except (OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
            # Skip files that can't be tokenised (binary, bad encoding decl,
            # syntax errors). Other tools (e.g. ruff) handle those separately.
            pass

        # A docstring begins and ends with `'''` (or `"""`)
        try:
            source = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # AsyncFunctionDef is included alongside FunctionDef so that
            # `async def` docstrings are also validated; without it, a
            # non-ASCII docstring on an async function would slip past
            # the scan silently.
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                continue
            doc = ast.get_docstring(node)
            if not doc or _PRINTABLE_ASCII.fullmatch(doc):
                continue
            first_line = doc.splitlines()[0] if doc.splitlines() else doc
            errors.append(f"non-ASCII docstring: {path}:{node.lineno}: {first_line}")
    return _report(errors)


def _repo_root() -> Path:
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip())


def _docs_set(root: Path, extra: list[Path]) -> list[Path]:
    """Curated Docs-as-Code set: the entry file plus engineering/ topic docs.

    Historical experiment records (records/, baseline/, annotations/) are data,
    not maintained prose, so they are excluded from the structure checks.
    """
    excluded = ("records", "baseline", "annotations")
    docs: list[Path] = []
    for candidate in [root / "AGENTS.md", root / "ARCHITECTURE.md", *sorted((root / "engineering").rglob("*.md"))]:
        if not candidate.is_file():
            continue
        if any(part in excluded for part in candidate.parts):
            continue
        docs.append(candidate)
    for path in extra:
        if path.suffix != ".md" or any(part in excluded for part in path.parts):
            continue
        if path.is_file() and path.resolve() not in {d.resolve() for d in docs}:
            docs.append(path)
    return docs


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(level, line_no, title) for every ATX heading outside fenced code."""
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for index, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            headings.append((len(match.group(1)), index, match.group(2).strip()))
    return headings


def _slug(title: str) -> str:
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def check_docs(paths: list[Path], fix: bool = False) -> int:
    """Enforce the Docs-as-Code rules recorded in AGENTS.md.

    Read-only. Checks the curated doc set (AGENTS.md, ARCHITECTURE.md and
    engineering/, minus historical experiment records) for:

    * local markdown links and `#anchor` targets that resolve;
    * `§N` / `§N.M` section references existing in the document they point at;
    * every engineering/*.md topic doc being reachable from AGENTS.md;
    * continuous section numbering (## N. and ### N.M);
    * inline-code citations (paths, functions, constants, test names) that still
      resolve — see ``_symbol_errors``.
    """
    root = _repo_root()
    docs = _docs_set(root, paths)
    entry = (root / "AGENTS.md").read_text(encoding="utf-8")
    readme = root / "engineering" / "README.md"
    readme_text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    errors: list[str] = []

    parsed: dict[Path, tuple[list[str], list[tuple[int, str]], set[str]]] = {}
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        headings = _headings(text)
        numbers: set[str] = set()
        for _, _, title in headings:
            dotted = re.match(r"^(\d+(?:\.\d+)*)", title)
            if dotted:
                numbers.add(dotted.group(1))
        parsed[doc] = (text.splitlines(), headings, numbers)

    def _label(doc: Path) -> str:
        return str(doc.relative_to(root)) if doc.is_relative_to(root) else str(doc)

    for doc, (lines, headings, numbers) in parsed.items():
        in_fence = False
        for line_no, line in enumerate(lines, start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # Inline code spans hold illustrative markup, not real links.
            prose = re.sub(r"`[^`]*`", "", line)
            targets: list[Path] = []
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", prose):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                file_part, _, anchor = target.partition("#")
                file_part = file_part.strip()
                if not file_part:
                    continue
                resolved = (doc.parent / file_part).resolve()
                targets.append(resolved)
                if not resolved.exists():
                    errors.append(f"dangling link: {_label(doc)}:{line_no}: {target}")
                    continue
                if anchor and resolved.suffix == ".md" and resolved in parsed:
                    titles = {_slug(title) for _, _, title in parsed[resolved][1]}
                    if anchor not in titles:
                        errors.append(f"dangling anchor: {_label(doc)}:{line_no}: {target}")

            # `§N` / `§N.M` must exist in the document the line links to, or in
            # this document when the line carries no markdown link.
            # A `§N` may point at a linked doc or at any doc named in the line.
            scope = set(numbers)
            for other, (_, _, other_numbers) in parsed.items():
                if other in targets or other.name in line:
                    scope |= other_numbers
            for ref in re.findall(r"§(\d+(?:\.\d+)*)", prose):
                parent = ref.rsplit(".", 1)[0] if "." in ref else ref
                if ref not in scope and parent not in scope:
                    errors.append(f"unknown section reference: {_label(doc)}:{line_no}: §{ref}")

        # Continuous numbering: ## N. and ### N.M.
        top_expected, sub_expected = 1, 1
        for level, line_no, title in headings:
            top = re.match(r"^(\d+)\.\s", title)
            sub = re.match(r"^(\d+)\.(\d+)\s", title)
            if level == 2 and top:
                found = int(top.group(1))
                if found != top_expected:
                    errors.append(f"section numbering gap: {_label(doc)}:{line_no}: expected ## {top_expected}, found ## {found}")
                top_expected, sub_expected = found + 1, 1
            elif level == 3 and sub:
                found_top, found_sub = int(sub.group(1)), int(sub.group(2))
                if (found_top, found_sub) != (top_expected - 1, sub_expected):
                    errors.append(f"section numbering gap: {_label(doc)}:{line_no}: expected ### {top_expected - 1}.{sub_expected}, found ### {found_top}.{found_sub}")
                sub_expected = found_sub + 1

    # Inline code citations: paths, functions, constants and test names.
    errors.extend(_symbol_errors(root, docs))

    # Index coverage: topic docs must be reachable from the entry file.
    for topic in sorted((root / "engineering").glob("*.md")):
        if topic.name != "README.md" and f"engineering/{topic.name}" not in entry:
            errors.append(f"topic doc not indexed from AGENTS.md: engineering/{topic.name}")
    for experiments in sorted((root / "engineering" / "experiments").glob("*/README.md")):
        relative = experiments.relative_to(root / "engineering")
        if str(relative) not in readme_text:
            errors.append(f"experiment set not indexed from engineering/README.md: {relative}")

    return _report(errors)


# Inline-code references whose absence from the checkout is expected, with the
# reason. Keep this list short: it is the escape hatch, not a dumping ground.
_REFERENCE_ALLOW = {
    # Named in the docs but living in another repository.
    "runtime/dev/config.yaml",  # deer-flow development config
    "config.example.yaml",  # deer-flow config template
    "config.yaml",  # deer-flow runtime config
    "../多库检索优化方案-2026-10-01.md",  # LocalAgentOps planning doc
    "../后续实验计划-修订版-2026-10-01.md",  # LocalAgentOps planning doc
    "../corpus_fs多库检索耗时分析-2026-10-01.md",  # LocalAgentOps analysis doc
    # Repository-relative, but produced by the build rather than checked in.
    "bin/ragflow_server",
    # Route fragments and HTTP header names, not filesystem paths.
    "datasets/search",
    "datasets/keyword-search",
    "fs/image",
    # Upstream-only packages: the docs name them to say they are absent here.
    "internal/ingestion/task/",
    "internal/ingestion/service/",
    "internal/service/document/",
    # Names owned by another component or protocol, not by this repository.
    "UPSTREAM_UNAVAILABLE",  # DeerFlow error envelope
    "XREADGROUP",  # Redis command
    "UV_CACHE_DIR",  # uv environment variable
}
# Opt a line out when it quotes markup or names that are deliberately not code.
_SYMBOL_SKIP_MARKER = "docs-symbols: skip-line"
# Suffix words that combine into extension lists such as `png/jpeg/gif`.
_SUFFIX_WORDS = {
    "pdf",
    "png",
    "jpeg",
    "jpg",
    "jp2",
    "webp",
    "gif",
    "bmp",
    "tiff",
    "doc",
    "docx",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "csv",
    "txt",
    "md",
    "json",
    "yaml",
    "yml",
    "py",
    "go",
    "ts",
    "tsx",
    "js",
    "sh",
    "sql",
    "html",
}
_BANNED_IN_TOKEN = set("<>*{}|~\"'`")
_PATH_TOKEN = re.compile(r"^[\w.\-/]+$")
_GIT_REF_TOKEN = re.compile(r"^(?:upstream|origin)/[\w.\-]+$")
_HTTP_VERBS_TOKEN = re.compile(r"^[A-Z]+(?:/[A-Z]+)+$")
_CALL_TOKEN = re.compile(r"^([A-Za-z_]\w*)\(")
_CONST_TOKEN = re.compile(r"^([A-Z][A-Z0-9_]{2,})(?:=|$)")
_TEST_TOKEN = re.compile(r"^(test_[a-z0-9_]{4,})$")


def _code_spans(line: str) -> list[str]:
    return re.findall(r"`([^`]+)`", line)


def _symbol_index(root: Path, names: set[str]) -> set[str]:
    """Which of ``names`` occur in tracked, non-documentation repository files.

    Two stages: one ``git grep -l`` narrows to the files that contain at least
    one candidate (fast), then those files are scanned in memory for the rest.
    A single ``git grep -o`` over every candidate is an order of magnitude
    slower because it emits each occurrence.
    """
    if not names:
        return set()
    args = ["git", "grep", "-l", "-I", "-F"]
    for name in sorted(names):
        args += ["-e", name]
    args += ["--", ".", ":(exclude)engineering/**", ":(exclude)*.md"]
    proc = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    blobs: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        try:
            blobs.append((root / line).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    haystack = "\n".join(blobs)
    return {name for name in names if name in haystack}


def _symbol_errors(root: Path, docs: list[Path]) -> list[str]:
    """Inline-code references to code that no longer exists.

    Curated docs cite files, functions, constants and test names inline. This
    check keeps those citations honest — the documentation drift that no other
    check catches. Historical experiment records are excluded (they cite the
    state of the tree at the time of the experiment).

    Candidates and their rules:

    * a token containing ``/`` must resolve as a path, relative to the document
      first and to the repository root second;
    * ``name(...)`` / ``name()`` must occur in tracked non-doc files;
    * ``ALL_CAPS`` (optionally ``=value``) must occur in tracked non-doc files;
    * ``test_x`` must occur likewise.

    Globs, angle-bracket placeholders, URLs, absolute paths and names listed in
    ``_REFERENCE_ALLOW`` are skipped; a line carrying
    ``docs-symbols: skip-line`` opts out entirely.
    """
    errors: list[str] = []
    symbols: dict[str, tuple[Path, int]] = {}

    for doc in docs:
        in_fence = False
        for line_no, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or _SYMBOL_SKIP_MARKER in line:
                continue
            for span in _code_spans(line):
                for token in (part.strip() for part in re.split(r"[\s,;]+", span)):
                    if not token:
                        continue
                    if any(char in _BANNED_IN_TOKEN for char in token) or "://" in token:
                        continue
                    if token.startswith(("/", "~")):
                        continue
                    if token in _REFERENCE_ALLOW:
                        continue
                    if "..." in token or _GIT_REF_TOKEN.match(token) or _HTTP_VERBS_TOKEN.match(token):
                        continue
                    segments = [part for part in token.rstrip("/").split("/") if part]
                    if segments and all(part in _SUFFIX_WORDS for part in segments):
                        continue
                    # Single-segment directory names are shorthand ("prompts/"),
                    # not repository paths; require at least two segments.
                    if "/" in token and len(segments) >= 2 and _PATH_TOKEN.match(token):
                        relative = token.rstrip("/")
                        if (doc.parent / relative).exists() or (root / relative).exists():
                            continue
                        errors.append(f"stale path reference: {doc.relative_to(root)}:{line_no}: {token}")
                        continue
                    name = None
                    for pattern in (_CALL_TOKEN, _CONST_TOKEN, _TEST_TOKEN):
                        match = pattern.match(token)
                        if match:
                            name = match.group(1)
                            break
                    if name:
                        symbols.setdefault(name, (doc, line_no))

    found = _symbol_index(root, set(symbols))
    for name, (doc, line_no) in sorted(symbols.items()):
        if name not in found:
            errors.append(f"stale symbol reference: {doc.relative_to(root)}:{line_no}: {name}")
    return errors


def check_symbols(paths: list[Path], fix: bool = False) -> int:
    """Standalone entry point for the symbol-drift check (see ``_symbol_errors``)."""
    root = _repo_root()
    return _report(_symbol_errors(root, _docs_set(root, paths)))


CHECKS = {
    "json": check_json,
    "yaml": check_yaml,
    "eof": check_eof,
    "trailing-whitespace": check_trailing_whitespace,
    "mixed-line-ending": check_mixed_line_endings,
    "merge-conflict": check_merge_conflicts,
    "symlinks": check_symlinks,
    "case-conflict": check_case_conflicts,
    "comment-ascii": check_comment_ascii,
    "docs": check_docs,
    "symbols": check_symbols,
}


def main() -> int:
    args = sys.argv[1:]
    valid = set(CHECKS)
    if not args or args[0] not in valid or len(args) > 2 or (len(args) == 2 and args[1] != "--fix"):
        print(f"usage: {sys.argv[0]} <{'|'.join(valid)}> [--fix]", file=sys.stderr)
        return 2
    fix = len(args) == 2
    paths = _staged_paths()
    return CHECKS[args[0]](paths, fix=fix)


if __name__ == "__main__":
    raise SystemExit(main())
