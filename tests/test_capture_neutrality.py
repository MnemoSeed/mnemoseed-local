"""FR-1.6b capture-neutrality guard: a CI-style static scan over capture/.

The capture funnel (F1-F3) must never import or read anima state or preference
nodes. This test statically scans every capture/ source for violations through
the AST (identifiers and attribute reads), so a regression fails the suite the
way a linter gate would.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

CAPTURE_DIR = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "capture"
RETRIEVE_DIR = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "retrieve"
DECAY_DIR = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "decay"
DRIVERS_DIR = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "storage" / "drivers"

FORBIDDEN = ("anima", "preference", "persona")

# FR-1.6: capture writes these stamp metadata labels (provenance labels, not
# anima/preference state); assigning them is allowed, reading is not.
_ALLOWED_STAMP_FIELD_WRITES = frozenset({"anima_id", "persona_id"})

# origin_agent is inert provenance metadata (source monitoring, write-time
# attribution): scoring, decay, and retrieval RANKING must never read it. The
# capture write path (segmenter/stamper/pipeline) propagates the label; the
# read-face serializers are sanctioned readers (per-function allowlist below).
INERT_METADATA_FIELD = "origin_agent"
_INERT_SCAN_PATHS = (
    CAPTURE_DIR / "scorer.py",
    CAPTURE_DIR / "pool.py",
    RETRIEVE_DIR / "hybrid.py",
    RETRIEVE_DIR / "cues.py",
    RETRIEVE_DIR / "assemble.py",
    DRIVERS_DIR / "lancedb_embedded.py",
    *sorted(DECAY_DIR.glob("*.py")),
)

# Sanctioned readers per scanned file: the serializer functions whose contract
# IS serving the label on the read face. Any other read inside these files
# (a ranking/decision leak) fails the guard; files absent from the map flag
# every read.
_SANCTIONED_INERT_READERS: dict[str, frozenset[str]] = {
    "assemble.py": frozenset({"_entry"}),
    "lancedb_embedded.py": frozenset({"_to_row", "_to_stamp"}),
}


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


def _enclosing_function_name(tree: ast.AST, node: ast.AST) -> str | None:
    """Innermost function enclosing ``node`` (None at module level)."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    current: ast.AST | None = node
    while current is not None and current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return None


def scan_source_for_inert_reads(source: str, path: str) -> list[str]:
    """Return unsanctioned reads of the inert metadata field (syntax-error
    marker on unparseable fixtures). Serializer files allowlist their serving
    functions; every other read in any scanned file is a violation."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{path}: syntax error in scan fixture: {exc}"]
    sanctioned = _SANCTIONED_INERT_READERS.get(Path(path).name, frozenset())
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == INERT_METADATA_FIELD
            and isinstance(node.ctx, ast.Load)
            and _enclosing_function_name(tree, node) not in sanctioned
        ):
            _record_violation(violations, path, node.lineno, INERT_METADATA_FIELD)
    return violations


def scan_inert_metadata_sources() -> list[str]:
    # A target that drifted off disk must fail loud here, never scan silently.
    missing = (
        f"{path}: inert-scan target missing from the tree" for path in _INERT_SCAN_PATHS if not path.is_file()
    )
    violations = list(missing)
    for source_path in _INERT_SCAN_PATHS:
        if not source_path.is_file():
            continue
        text = source_path.read_text(encoding="utf-8")
        violations.extend(scan_source_for_inert_reads(text, str(source_path)))
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


# --------------------------------------------------- inert origin_agent guard


def test_scoring_decay_and_retrieval_never_read_origin_agent() -> None:
    """origin_agent is write-time provenance only: no scoring, decay, or
    ranking surface may ever read it (capture neutrality extends to the new
    column)."""
    violations = scan_inert_metadata_sources()
    assert violations == [], "inert metadata read by a ranking surface:\n" + "\n".join(violations)


def test_inert_scanner_flags_field_reads_but_not_writes() -> None:
    bad = "label = chunk.origin_agent\nscore += len(label)\n"
    assert scan_source_for_inert_reads(bad, "f.py")
    good = "chunk.origin_agent = 'build'\n"
    assert scan_source_for_inert_reads(good, "f.py") == []


def test_inert_scanner_allows_sanctioned_serializer_reads() -> None:
    """The read-face serializers expose the label by contract: their serving
    functions are the ONLY legal readers inside the scanned serializer files."""
    assemble = (
        "class Assembler:\n"
        "    def _entry(self, candidate):\n"
        "        return {'origin_agent': candidate.item.origin_agent}\n"
    )
    lance = (
        "class Store:\n"
        "    def _to_row(self, chunk):\n"
        "        return {'origin_agent': chunk.origin_agent}\n"
        "\n"
        "    def _to_stamp(self, row):\n"
        "        return row.get('origin_agent')\n"
    )
    assert scan_source_for_inert_reads(assemble, "assemble.py") == []
    assert scan_source_for_inert_reads(lance, "lancedb_embedded.py") == []


def test_inert_scanner_flags_ranking_read_inside_serializer_files() -> None:
    """A ranking/decision read inside a serializer file is still a leak — the
    allowlist covers only the sanctioned serving functions."""
    bad = "def boost(candidates):\n    return [c for c in candidates if c.origin_agent]\n"
    assert scan_source_for_inert_reads(bad, "assemble.py")
    assert scan_source_for_inert_reads(bad, "lancedb_embedded.py")


def test_inert_scan_fails_loud_when_a_target_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename that orphans a scan target must fail the guard, never shrink
    the scanned surface silently."""
    ghost = Path("does/not/exist/scorer.py")
    monkeypatch.setattr(sys.modules[__name__], "_INERT_SCAN_PATHS", (ghost,))
    violations = scan_inert_metadata_sources()
    assert any("inert-scan target missing" in v for v in violations)
