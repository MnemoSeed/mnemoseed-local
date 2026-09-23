"""Reject prohibited narrative in newly added source comments."""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path

COMMENT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".html",
    ".htm",
    ".ps1",
    ".psm1",
    ".sh",
    ".yml",
    ".yaml",
}
HASH_COMMENT_EXTENSIONS = frozenset({".ps1", ".psm1", ".sh", ".yml", ".yaml"})
FORBIDDEN = re.compile(
    r"\b(?:todo|hack|fixme|workaround|temporary|issue|pr|qa|incident|history|"
    r"background|rationale|attribution|person|people|names?|context)\b",
    re.I,
)
_RUFF_CODE = r"[A-Za-z]+\d+"
_RUFF_CODES = _RUFF_CODE + r"(?:\s*,\s*" + _RUFF_CODE + r")*"
DIRECTIVE = re.compile(
    r"(?:noqa(?::\s*" + _RUFF_CODES + r")?"
    r"|type:\s*ignore(?:\[[^\]]+\])?"
    r"|pragma:\s*no\s+(?:cover|branch)"
    r"|pyright|mypy|cspell|ts-ignore|@ts-ignore|eslint(?:-disable(?:-next-line)?)?"
    r")\s*",
    re.I,
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    text: str


def _check_comment(path: str, line: int, text: str) -> list[Violation]:
    stripped = text.strip()
    if DIRECTIVE.fullmatch(stripped) or not FORBIDDEN.search(text):
        return []
    return [Violation(path, line, text.strip())]


def _python_comments(text: str, path: str) -> list[Violation]:
    found: list[Violation] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            found.extend(_check_comment(path, token.start[0], token.string[1:]))
    return found


def _c_comments(text: str, path: str) -> list[Violation]:
    found: list[Violation] = []
    hash_comments = Path(path).suffix.lower() in HASH_COMMENT_EXTENSIONS
    index = 0
    line = 1
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if char == "\n":
            line += 1
        if quote:
            if char == "\n" and quote != "`":
                quote = None
                index += 1
                continue
            if char == "\\":
                if index + 1 < len(text) and text[index + 1] == "\n":
                    line += 1
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if Path(path).suffix.lower() in {".jsx", ".tsx"} and text.startswith("{/*", index):
            end = text.find("*/}", index + 3)
            end = len(text) - 3 if end < 0 else end
            body = text[index + 3 : end]
            for offset, comment_line in enumerate(body.splitlines(), 0):
                found.extend(_check_comment(path, line + offset, comment_line.lstrip(" *")))
            line += body.count("\n")
            index = end + 3
            continue
        if (
            char == "'"
            and index
            and text[index - 1].isalnum()
            and index + 1 < len(text)
            and text[index + 1].isalpha()
        ):
            index += 1
            continue
        if char == "'" and Path(path).suffix.lower() in {".jsx", ".tsx"}:
            previous = text[index - 1] if index else ""
            if previous not in "=([{,":
                index += 1
                continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            found.extend(_check_comment(path, line, text[index + 2 : end]))
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) - 2 if end < 0 else end
            body = text[index + 2 : end]
            for offset, comment_line in enumerate(body.splitlines(), 0):
                found.extend(_check_comment(path, line + offset, comment_line.lstrip(" *")))
            line += body.count("\n")
            index = end + 2
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            end = len(text) - 3 if end < 0 else end
            body = text[index + 4 : end]
            for offset, comment_line in enumerate(body.splitlines(), 0):
                found.extend(_check_comment(path, line + offset, comment_line))
            line += body.count("\n")
            index = end + 3
            continue
        if hash_comments and char == "#":
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            found.extend(_check_comment(path, line, text[index + 1 : end]))
            index = end
            continue
        index += 1
    return found


def violations_for_text(text: str, path: str) -> list[Violation]:
    """Return policy violations while leaving strings and docstrings opaque."""
    if Path(path).suffix.lower() == ".py":
        return _python_comments(text, path)
    return _c_comments(text, path)


def added_lines(base: str) -> dict[str, set[int]]:
    subprocess.run(["git", "rev-parse", "--verify", base], capture_output=True, check=True)
    result = subprocess.run(
        ["git", "diff", "--unified=0", base, "--"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    files: dict[str, set[int]] = {}
    current = ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.setdefault(current, set())
        elif line.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                files[current].update(range(start, start + count))
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, check=True
    )
    for path in untracked.stdout.splitlines():
        if Path(path).suffix.lower() in COMMENT_EXTENSIONS and Path(path).is_file():
            files[path] = set(range(1, len(Path(path).read_text(encoding="utf-8").splitlines()) + 1))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    violations: list[Violation] = []
    try:
        changed = added_lines(args.base)
    except subprocess.CalledProcessError:
        print(f"missing base or invalid git diff: {args.base}", file=__import__("sys").stderr)
        return 2
    for path, lines in changed.items():
        if Path(path).suffix.lower() not in COMMENT_EXTENSIONS:
            continue
        file_path = Path(path)
        if not file_path.exists():
            continue
        violations.extend(
            item
            for item in violations_for_text(file_path.read_text(encoding="utf-8"), path)
            if item.line in lines
        )
    for item in violations:
        print(f"{item.path}:{item.line}: prohibited narrative comment: {item.text}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
