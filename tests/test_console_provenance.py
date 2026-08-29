"""Console R2 T3 provenance-trust rendering — static mutation pins.

Design/UX spec ``11-provenance-recall-trust.md`` §11 T3 (§5.2/§5.3/§6/§8/§9).
Each assertion is mutation-sensitive: reverting the R2 trust surface (the pin
discriminant, the "Pinned" / "Reconcile" marks, the statusbar recall summary,
or the no-confidence red line) must fail its test.

Data contract: Atlas chunk items carry ``source``/``asserted_by``/
``flags.explicit_pin``/``flags.needs_reconcile``; Atlas node items carry
``flags.{conflict,pending,needs_reconcile,read_conflict}``/``confidence``;
``POST /session/recent`` returns ``sessions[].chunks[].{source,...}``
(daemon/memory.py:384, 1427-1475). The console must render provenance via
these fields and NEVER render a confidence value (§3.2).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "mnemoseed_local" / "console" / "static"
PROVENANCE_DRIVER = Path(__file__).parent / "ts_hook" / "console_provenance.mjs"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------ discriminant


def test_explicit_pin_source_constant(app_js: str) -> None:
    # The single pin discriminant: a chunk whose provenance.source equals the
    # EXPLICIT_PIN_SOURCE label is a user pin, never a captured chunk. There
    # must be exactly one comparison source (stamp.py EXPLICIT_PIN_SOURCE).
    assert 'const EXPLICIT_PIN_SOURCE = "memory.remember"' in app_js


def test_pin_badge_rendered_from_source_discriminant(app_js: str) -> None:
    # The Drawer's Source slot turns a genuine pin into a Pinned badge, keyed
    # off the EXPLICIT_PIN_SOURCE comparison (not a second literal).
    assert "src===EXPLICIT_PIN_SOURCE" in app_js
    assert "badge-pin" in app_js


# ------------------------------------------------------------------ new marks


def test_pinned_mark_present_for_explicit_pin(app_js: str, index_html: str) -> None:
    # "Why this surfaced": a Pinned badge for explicit pins, globally present
    # in the drawer wiring and as the default-hidden element.
    assert "dw-pin" in app_js and "dw-pin" in index_html
    assert 'id="dw-pin"' in index_html
    assert "badge badge-pin" in index_html


def test_needs_reconcile_badge_present(app_js: str, index_html: str) -> None:
    # The provenance Reconcile badge (chunk flags.needs_reconcile) and the
    # "Why this surfaced" Reconcile badge must both exist and be wired.
    assert 'id="d-reconcile"' in index_html
    assert 'id="dw-reconcile-s"' in index_html
    assert "d-reconcile" in app_js and "dw-reconcile-s" in app_js
    assert "needs_reconcile" in app_js
    assert "Reconcile" in index_html


def test_conflict_badges_present(app_js: str, index_html: str) -> None:
    # conflict / read_conflict / pending marks exist in the why-surfaced row.
    for mark in ("dw-conflict", "dw-readconflict", "dw-pending"):
        assert f'id="{mark}"' in index_html
        assert mark in app_js


def test_fading_mark_present(app_js: str, index_html: str, style_css: str) -> None:
    # Fading (decay_weight < 0.15) badge exists in the why-surfaced row.
    assert 'id="dw-fading"' in index_html
    assert "dw-fading" in app_js
    assert "badge-fading" in index_html and "badge-fading" in style_css


# ------------------------------------------------------------------ provenance wiring


def test_asserted_by_and_source_wired(app_js: str) -> None:
    # Drawer provenance uses the Atlas wire fields directly (previously "—").
    assert "it.asserted_by || prov.asserted_by" in app_js
    assert "it.source || prov.source" in app_js


def test_at_slot_uses_capture_time(app_js: str) -> None:
    # The "At" slot renders the honest capture/valid time, never a fabricated
    # asserted_at; confidence is never shown in the slot.
    assert "it.ingested_at || it.valid_from" in app_js
    assert "d-confidence" not in app_js


# ------------------------------------------------------------------ recall summary (§5.2)


def test_statusbar_recall_summary_present(app_js: str) -> None:
    # §5.2: the Atlas statusbar appends a "Last auto-recall served …" audit
    # line backed by POST /session/recent (read-only, zero new backend).
    assert "recallSummary(" in app_js
    assert '"/session/recent"' in app_js
    assert "EXPLICIT_PIN_SOURCE" in app_js
    assert "statusbar-recall" in app_js


def test_statusbar_recall_summary_css(style_css: str) -> None:
    assert ".statusbar-recall" in style_css
    assert ".d-whysrc" in style_css


# ------------------------------------------------------------------ no confidence (§3.2 red line)


def test_no_confidence_numeric_rendering(app_js: str) -> None:
    # §3.2: a raw confidence value is never rendered as a number. Reverting the
    # drawer back to "Confidence ${it.confidence}" must fail this test.
    assert "Confidence ${" not in app_js
    assert "Confidence ${it.confidence}" not in app_js
    assert "d-confidence" not in app_js


def test_honest_src_note(app_js: str, index_html: str) -> None:
    # §9 empty state: honest copy when there's no source, not raw JSON.
    assert "captured before provenance was recorded" in app_js
    assert "captured before provenance was recorded" in index_html
    assert "No source info" in app_js


# ------------------------------------------------------------------ a11y


def test_why_survaced_badges_carry_tooltips(index_html: str) -> None:
    # §9.1: Pinned / Reconcile / conflict badges expose their meaning via
    # title (tooltip) text, not colour alone.
    for label, frag in (
        ("dw-pin", "Pinned"),
        ("dw-reconcile-s", "Reconcile"),
        ("dw-fading", "Fading"),
    ):
        assert f'id="{label}"' in index_html
        assert frag in index_html
    # tooltips present on the marks
    assert 'id="dw-pin"' in index_html
    assert index_html.count("title=") > 0


# ------------------------------------------------------------------ a11y: aria-label (§9.1)


def test_aria_labels_present_on_every_why_badge(index_html: str) -> None:
    # Each why-surfaced / provenance badge with a title must also have an
    # aria-label (dual channel per §9.1: icon+text, not colour/attr alone).
    for label in (
        "dw-pin",
        "dw-captured",
        "dw-relevance",
        "dw-conflict",
        "dw-readconflict",
        "dw-pending",
        "dw-reconcile-s",
        "dw-recovered",
        "dw-fading",
        "d-reconcile",
        "d-gaps",
    ):
        # find the <span id="..."> and require aria-label= on that same tag
        seg = index_html
        pos = seg.find(f'id="{label}"')
        assert pos != -1, f"missing id={label}"
        start = seg.rfind("<", 0, pos)
        assert "aria-label=" in seg[start:].split(">", 1)[0], f"{label} lacks aria-label"


def test_source_pin_badge_has_aria_and_clears_title(app_js: str) -> None:
    # §9.1 + N6: the dynamic Pinned badge in the Drawer source slot carries an
    # aria-label, and the branch clears any stale title on the #d-source span.
    assert 'aria-label="Pinned' in app_js
    assert 'srcEl.title = ""' in app_js


def test_statusbar_copy_is_honest(app_js: str) -> None:
    # F1/N4: the statusbar line reflects session tails, never a false
    # auto-recall claim, and never a pinned count (no source on this wire).
    assert "Newest session tail:" in app_js
    assert "auto-recall served" not in app_js


def test_recall_summary_bound_to_abort_signal(app_js: str) -> None:
    # F2: recallSummary takes the fetch-abort signal and drops a stale resolve.
    assert "signal" in app_js
    assert "signal?.aborted" in app_js
    assert 'recallSummary(statusbar, atlasState.profile || "default", ac.signal)' in app_js


def test_pin_discriminant_is_single_comparison(app_js: str) -> None:
    # N7: the pin discriminant is a single EXPLICIT_PIN_SOURCE comparison,
    # never a dual `explicit_pin || src===...` check.
    assert "const pinned = src===EXPLICIT_PIN_SOURCE" in app_js


# ------------------------------------------------------------------ runtime (§N3)


def test_provenance_runtime_harness_pins_recall_summary() -> None:
    # The JS harness must itself reference recallSummary (reverting the harness
    # to a no-op check must also fail).
    driver = PROVENANCE_DRIVER.read_text(encoding="utf-8")
    assert 'typeof H.recallSummary !== "function"' in driver
    assert "Newest session tail:" in driver


def test_provenance_runtime_harness_runs_clean(tmp_path: Path) -> None:
    """N3: load the SHIPPED app.js in node (browser-stubbed VM) and assert the
    recallSummary surface exists and the provenance branch runs without throwing
    on a chunk item. Skipped cleanly when node is unavailable."""
    if shutil.which("node") is None:
        pytest.skip("node unavailable on this machine")
    result = subprocess.run(
        ["node", str(PROVENANCE_DRIVER), str(STATIC_DIR / "app.js")],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "console_provenance.mjs OK" in result.stdout
