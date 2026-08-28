"""Console C-3 polish - QA conditions pinned against the served static files.

Covers the two static HTML fixes (overview id names, dialog focus, atlas
roles), the badge token migration (class tokens instead of inline styles),
the filterbar focus selector, and the atlas kind wire contract (backend
accepts only chunks|nodes|both). Each pin is mutation-sensitive: reverting
any polish commit must fail its test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "mnemoseed_local" / "console" / "static"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


_ID_RE = re.compile(r'id="([^"]+)"')
_ARIA_LABELLEDBY_RE = re.compile(r'aria-labelledby="([^"]+)"')


# ------------------------------------------------------------------ IMPORTANT-1


def test_static_files_parse(index_html: str, style_css: str, app_js: str) -> None:
    assert index_html
    assert style_css
    assert app_js


def test_no_duplicate_element_ids(index_html: str) -> None:
    ids = _ID_RE.findall(index_html)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate element ids: {sorted(duplicates)}"


def test_overview_uses_own_config_ids(index_html: str) -> None:
    # Overview's config card must use the overview-prefixed ids, never the
    # Config tab's bare ids.
    overview = index_html.split('id="view-overview"', 1)[1].split("</section>", 1)[0]
    assert 'id="overview-config-body"' in overview
    assert 'id="overview-config-foot"' in overview
    assert 'id="config-body"' not in overview
    assert 'id="config-foot"' not in overview


def test_config_tab_keeps_bare_ids(index_html: str) -> None:
    config_tab = index_html.split('id="view-config"', 1)[1].split('id="view-profiles"', 1)[0]
    assert 'id="config-body"' in config_tab
    assert 'id="config-foot"' in config_tab


def test_filterbar_role_group(index_html: str) -> None:
    # The filterbar must be a group labelled "Filters" (not a toolbar).
    match = re.search(r'class="filterbar"[^>]*>', index_html)
    assert match, "filterbar element not found"
    tag = match.group(0)
    assert 'role="group"' in tag
    assert 'aria-label="Filters"' in tag


def test_canvas_wrap_role_region(index_html: str) -> None:
    match = re.search(r'<div id="atlas-canvas-wrap"[^>]*>', index_html)
    assert match, "atlas canvas wrap element not found"
    tag = match.group(0)
    assert 'role="region"' in tag
    assert 'role="toolbar"' not in tag
    assert 'role="img"' not in tag


def test_dialogs_aria_labelledby_resolve(index_html: str) -> None:
    ids = set(_ID_RE.findall(index_html))
    dialog_tags = re.findall(r"<dialog\b[^>]*>", index_html)
    assert dialog_tags, "no <dialog> elements found"
    for tag in dialog_tags:
        ref = _ARIA_LABELLEDBY_RE.search(tag)
        assert ref, f"dialog without aria-labelledby: {tag!r}"
        target = ref.group(1)
        assert target in ids, f"aria-labelledby references missing id {target!r}"


# ------------------------------------------------------------------ IMPORTANT-3


def test_badge_danger_token_in_css(style_css: str) -> None:
    assert ".badge-danger" in style_css
    block = _class_block(style_css, "badge-danger")
    assert "#FEF2F2" in block, "badge-danger must use danger background token"
    assert "#FECACA" in block, "badge-danger must use danger border token"
    assert "#7F1D1D" in block, "badge-danger must use danger text token"


def test_badge_fading_token_in_css(style_css: str) -> None:
    assert ".badge-fading" in style_css, "a fading badge token must exist in style.css"


def test_degraded_badge_uses_class_token(app_js: str, style_css: str) -> None:
    # The degraded status badge must draw from the badge-danger class token,
    # not an inline style override.
    assert "badge-ok' : 'badge-danger'" in app_js or '"badge-ok" : "badge-danger"' in app_js
    assert "background:#FEF2F2" not in app_js, "inline danger style must be removed from app.js"


def test_fading_label_uses_class_token(app_js: str, style_css: str) -> None:
    # The fading decay label must use the badge-fading class token, not an
    # inline background override.
    assert "badge-fading" in app_js, "fading label must reference the badge-fading token"
    assert "badge-fading" in style_css
    assert "labelEl.style.background" not in app_js, "inline fading background must be removed"


def _class_block(css: str, cls: str) -> str:
    start = css.index(f".{cls}")
    end = css.find("}", start)
    return css[start:end]


# ------------------------------------------------------------------ NIT-1


def test_filterbar_focus_selector(style_css: str) -> None:
    assert ".filterbar:has(:focus-visible)" in style_css, "filterbar focus must use :has(:focus-visible)"
    assert ".filterbar:focus-within" not in style_css, "redundant focus-within must be removed"


# ------------------------------------------------------------------ IMPORTANT-1 atlas wire


def _config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 8\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 8\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _config_path(tmp_path, monkeypatch)
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def test_atlas_rejects_kind_all(client: TestClient) -> None:
    # The backend accepts only chunks|nodes|both. The frontend translates
    # "all" to "both" before sending (pinned separately in the static file
    # check); a raw kind="all" is a validation error.
    resp = client.post("/memory/atlas", json={"profile_id": "default", "kind": "all"})
    assert resp.status_code == 422, resp.text


def test_atlas_accepts_kind_both(client: TestClient) -> None:
    resp = client.post("/memory/atlas", json={"profile_id": "default", "kind": "both"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_atlas_frontend_maps_all_to_both(app_js: str) -> None:
    # The frontend translates the "all" filter to the backend's "both" kind
    # before sending. Fails if the mapping is removed or flattened back to a
    # pass-through (kind: atlasState.kind).
    assert (
        'atlasState.kind==="all" ? "both" : atlasState.kind' in app_js
        or 'atlasState.kind === "all" ? "both" : atlasState.kind' in app_js
    ), "frontend must translate atlas kind 'all' to 'both'"
