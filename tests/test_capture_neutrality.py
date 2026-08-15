"""FR-1.6b capture-neutrality guard: a CI-style static scan over capture/.

The capture funnel (F1-F3) must never import or read anima state or preference
nodes. This test statically scans every capture/ source for violations through
the AST (identifiers and attribute reads), so a regression fails the suite the
way a linter gate would.
"""

from __future__ import annotations

import ast
from pathlib import Path

CAPTURE_DIR = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "capture"

FORBIDDEN = ("anima", "preference", "persona")

# FR-1.6: capture writes these stamp metadata labels (provenance labels, not
# anima/preference state); assigning them is allowed, reading is not.
_ALLOWED_STAMP_FIELD_WRITES = frozenset({"anima_id", "persona_id"})


def _forbidden_part(ident: str) -> str:
    lowered = ident.lower()
    for token in FORBIDDEN:
        if token in lowered:
            return ident
    return ""


def _record_violation(violations: list[str], path: str, line: int, what: str) -> None:
    violations.append(f"{path}:{line}: forbidden capture read of {what!r}")


def scan_source(source: str, path: str) -> list[str]:
    """Return forbidden identifier or attribute reads, or a syntax error marker."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{path}: syntax error in scan fixture: {exc}"]
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Dotted imports hide the forbidden module in a later segment
                # (`import mnemoseed_local.anima as am`), so every segment is scanned.
                bad = next((part for part in alias.name.split(".") if _forbidden_part(part)), "")
                if not bad and alias.asname:
                    bad = _forbidden_part(alias.asname)
                if bad:
                    _record_violation(violations, path, node.lineno, bad)
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                bad = _forbidden_part(part)
                if bad:
                    _record_violation(violations, path, node.lineno, f"module {bad}")
            for alias in node.names:
                bad = alias.asname and _forbidden_part(alias.asname)
                if bad:
                    _record_violation(violations, path, node.lineno, bad)
        elif isinstance(node, ast.Name):
            bad = _forbidden_part(node.id)
            if bad:
                _record_violation(violations, path, node.lineno, bad)
        elif isinstance(node, ast.Attribute):
            # A Store-context attribute whose name is a mandated stamp metadata
            # label is a field WRITE, not a read of anima/preference state.
            if isinstance(node.ctx, ast.Store) and node.attr in _ALLOWED_STAMP_FIELD_WRITES:
                continue
            parts = [node.attr]
            value = node.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
            for part in parts:
                bad = _forbidden_part(part)
                if bad:
                    _record_violation(violations, path, node.lineno, bad)
    return violations


def scan_capture_sources() -> list[str]:
    violations: list[str] = []
    for source in sorted(CAPTURE_DIR.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        violations.extend(scan_source(text, str(source)))
    return violations


# ---------------------------------------------------------------- the guard


def test_capture_sources_never_read_anima_or_preferences() -> None:
    violations = scan_capture_sources()
    assert violations == [], "capture neutrality violated:\n" + "\n".join(violations)


# ---------------------------------------------------------------- self-checks


def test_scanner_flags_import_of_forbidden_module() -> None:
    bad = "from mnemoseed_local.anima import state\n"
    assert scan_source(bad, "f.py")


def test_scanner_flags_dotted_import_under_forbidden_submodule() -> None:
    # Regression: `import mnemoseed_local.anima as am` evaded the Import branch
    # because it only checked the top-level package segment ("mnemoseed").
    assert scan_source("import mnemoseed_local.anima as am\n", "f.py")
    assert scan_source("import mnemoseed_local.anima.state\n", "f.py")


def test_scanner_flags_forbidden_identifier() -> None:
    bad = "preference_score = 1.0\n"
    assert scan_source(bad, "f.py")


def test_scanner_flags_forbidden_attribute_read() -> None:
    bad = "score = anima.current().calm\n"
    assert scan_source(bad, "f.py")


def test_scanner_flags_persona_field_read() -> None:
    bad = "sent = chunk.persona_id\n"
    assert scan_source(bad, "f.py")


def test_scanner_accepts_neutral_source() -> None:
    good = "free_text = turn.text\ntones = lexicon.scan(free_text)\n"
    assert scan_source(good, "f.py") == []


def test_scanner_keeps_flagging_anima_read_calls() -> None:
    # The red line is about READING anima state; reads stay flagged.
    bad = "state = anima.current().calm\n"
    assert scan_source(bad, "f.py")


def test_scanner_allows_anima_id_field_assignment_on_stamp() -> None:
    # FR-1.6 requires capture to WRITE the anima_id label onto a stamp; that is
    # metadata assignment, not reading anima state.
    good = "stamp.anima_id = 'x'\n"
    assert scan_source(good, "f.py") == []


def test_scanner_allows_persona_id_field_assignment_on_stamp() -> None:
    good = "chunk.persona_id = 'prof-1'\n"
    assert scan_source(good, "f.py") == []
