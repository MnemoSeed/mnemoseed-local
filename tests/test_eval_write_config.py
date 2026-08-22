"""T4b — the recall calibration's --write-config landing step.

The effective defaults are the UPPERCASE module constants in config.py; the
lowercase ``auto_recall_*`` lines exist only inside the generated-toml
template comments. The writer must target the constants (the regex trap:
matching only the lowercase form silently updates just the documentation)
and keep the template comments in sync with them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mnemoseed_local.eval import __main__ as eval_main
from mnemoseed_local.eval.__main__ import write_calibration_defaults
from mnemoseed_local.eval.recall_matrix import CoordinateDescentOutcome

_SNIPPET = """\
#: B2.1 T2 mid-session auto-recall: the focal decay floor and the budget.
DEFAULT_AUTO_RECALL_FOCAL_FLOOR: float = 0.4
DEFAULT_AUTO_RECALL_BUDGET_CHARS: int = 1200

DEFAULT_DREAM_POOL_FORCED_CAP: float = 50.0

# [capture]
# auto_recall = false
# auto_recall_focal_floor = 0.4
# auto_recall_budget_chars = 1200
"""


def test_write_calibration_defaults_targets_the_constants(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text(_SNIPPET, encoding="utf-8")
    assert write_calibration_defaults(path, 0.55, 1600) is True
    content = path.read_text(encoding="utf-8")
    assert "DEFAULT_AUTO_RECALL_FOCAL_FLOOR: float = 0.55" in content
    assert "DEFAULT_AUTO_RECALL_BUDGET_CHARS: int = 1600" in content


def test_write_calibration_defaults_syncs_the_template_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text(_SNIPPET, encoding="utf-8")
    write_calibration_defaults(path, 0.55, 1600)
    content = path.read_text(encoding="utf-8")
    assert "# auto_recall_focal_floor = 0.55" in content
    assert "# auto_recall_budget_chars = 1600" in content


def test_write_calibration_defaults_leaves_unrelated_constants_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text(_SNIPPET, encoding="utf-8")
    write_calibration_defaults(path, 0.55, 1600)
    content = path.read_text(encoding="utf-8")
    assert "DEFAULT_DREAM_POOL_FORCED_CAP: float = 50.0" in content
    assert "# auto_recall = false" in content


def test_write_calibration_defaults_refuses_a_partial_match(tmp_path: Path) -> None:
    """No constant line, only the lowercase comment form: the historical
    silent-partial-write shape must report failure and change nothing."""
    path = tmp_path / "config.py"
    original = "# auto_recall_focal_floor = 0.4\n# auto_recall_budget_chars = 1200\n"
    path.write_text(original, encoding="utf-8")
    assert write_calibration_defaults(path, 0.55, 1600) is False
    assert path.read_text(encoding="utf-8") == original


def test_recall_command_exits_nonzero_when_the_writer_fails(monkeypatch, capsys) -> None:
    """--write-config over a FAILED write must exit 1 with the FAILED line —
    never a green exit over an unlanded calibration."""
    outcome = CoordinateDescentOutcome(
        groups=(), results=(), recommended=(0.5, 2400), demoted=False, demotion_path=()
    )
    monkeypatch.setattr(eval_main, "coordinate_descent", lambda *args, **kwargs: outcome)
    monkeypatch.setattr(eval_main, "write_calibration_defaults", lambda *args, **kwargs: False)
    rc = eval_main._recall_command(argparse.Namespace(workdir="unused", write_config=True))
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAILED" in captured.out
    assert "Updated" not in captured.out
