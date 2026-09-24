"""Verify the shipped Atlas page against an isolated local daemon."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

RESERVED_PORTS = (7788, 4096)
REMEMBER_TEXT = "Atlas verification memory for isolated browser proof"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def default_browsers_path(repo_root: Path) -> Path:
    return repo_root / ".verification-runs" / "playwright-browsers"


_BROWSER_LAYOUTS = {
    "nt": ("chrome-win/headless_shell.exe", "chrome-win/chrome.exe"),
    "posix": ("chrome-linux/headless_shell", "chrome-linux/chrome"),
}


def find_browser_executable(candidate: Path, os_name: str | None = None) -> Path | None:
    layouts = _BROWSER_LAYOUTS.get(os_name if os_name is not None else os.name, _BROWSER_LAYOUTS["posix"])
    for build in sorted(candidate.glob("chromium*")):
        if not build.is_dir():
            continue
        for relative in layouts:
            executable = build / relative
            if executable.is_file():
                return executable
    return None


def preflight_validators():
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.atlas_preflight import validate_config, validate_run_root

    return validate_config, validate_run_root


def ensure_browsers_path(repo_root: Path) -> str:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        candidate = Path(configured)
    else:
        candidate = default_browsers_path(repo_root)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
    if find_browser_executable(candidate) is None:
        raise RuntimeError(
            "Playwright Chromium is missing; "
            "run `$env:PLAYWRIGHT_BROWSERS_PATH = '.verification-runs/playwright-browsers'; "
            "uv run playwright install --with-deps chromium` from the repo root"
        )
    return str(candidate)


def venv_python(repo_root: Path) -> str:
    candidate = repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
    if os.name == "nt":
        candidate = candidate.with_suffix(".exe")
    if candidate.exists():
        return str(candidate)
    import sys

    return sys.executable


def stop_owned_daemon(process: subprocess.Popen[bytes], base_url: str) -> None:
    print(f"Cleanup target: {process.pid} (owned child) at {base_url}")
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None and os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        raise RuntimeError(f"owned daemon {process.pid} did not stop")
    for _ in range(40):
        try:
            httpx.get(base_url + "/healthz", timeout=1.0)
        except httpx.HTTPError:
            return
        time.sleep(0.25)
    raise RuntimeError(f"owned daemon port still answers: {base_url}")


def wait_for_count(page: object, timeout_ms: int = 15000) -> str:
    count = page.locator("#atlas-count")  # type: ignore[attr-defined]
    count.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_function(  # type: ignore[attr-defined]
        "() => { const el = document.querySelector('#atlas-count');"
        " return el && !/Loading/.test(el.textContent || ''); }",
        timeout=timeout_ms,
    )
    return str(count.inner_text()).strip()


def main() -> int:
    repo_root = Path(__file__).parent.parent.resolve()
    browsers_path = ensure_browsers_path(repo_root)
    run_root = repo_root / ".verification-runs"
    run_root.mkdir(exist_ok=True)
    run_dir = run_root / f"atlas-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    if run_dir.exists():
        raise RuntimeError(f"Refusing to reuse existing run directory: {run_dir}")
    run_dir.mkdir()
    home = run_dir / "MNEMOSEED_LOCAL_HOME"
    home.mkdir()
    evidence = run_dir / "evidence.txt"
    process: subprocess.Popen[bytes] | None = None
    print(f"Verification run: {run_dir}")
    print(f"MNEMOSEED_LOCAL_HOME: {home}")
    print(f"PLAYWRIGHT_BROWSERS_PATH: {browsers_path}")
    print("Cleanup target: child daemon only; retained run artifacts are not deleted")
    try:
        port = free_port()
        if port in RESERVED_PORTS:
            raise RuntimeError(f"Refusing reserved port: {port}")
        base_url = f"http://127.0.0.1:{port}"
        print(f"Base URL for every operation: {base_url}")
        env = os.environ.copy()
        env["MNEMOSEED_LOCAL_HOME"] = str(home)
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["HOMEDRIVE"] = home.drive
        env["HOMEPATH"] = str(home)[len(home.drive) :]
        subprocess.run(
            ["uv", "run", "mnemoseed-local", "init", "--force"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        config = home / "config.toml"
        config_text = config.read_text(encoding="utf-8")
        config_text = config_text.replace('baseurl = "http://localhost:7788"', f'baseurl = "{base_url}"', 1)
        config_text = config_text.replace(
            'path = "~/.mnemoseed-local/isolated.db"',
            f'path = "{(home / "isolated.db").as_posix()}"',
            1,
        )
        config.write_text(config_text, encoding="utf-8")
        validate_config, validate_run_root = preflight_validators()
        guard_errors = validate_run_root(repo_root, run_dir, home, port, env)
        guard_errors += validate_config(config_text, home, base_url)
        if guard_errors:
            raise RuntimeError("isolation guard failed: " + "; ".join(guard_errors))
        with config.open("a", encoding="utf-8") as handle:
            handle.write('\n[storage.vector]\ndriver = "lancedb_embedded"\ndimensions = 64\n')
            handle.write('\n[storage.embed]\ndriver = "synthetic"\ndimension = 64\n')
            handle.write('\n[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\n')
            handle.write('\n[dream.llm.dream_verifier]\ndriver = "stub"\nmodel = "stub"\n')
        subprocess.run(
            ["uv", "run", "mnemoseed-local", "doctor"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        daemon = venv_python(repo_root)
        print(f"Daemon executable (owned): {daemon}")
        process = subprocess.Popen(
            [
                daemon,
                "-c",
                "import sys;"
                " sys.argv=['mnemoseed-local','up','--host','127.0.0.1','--port',"
                f" '{port}'];"
                " from mnemoseed_local.cli import main;"
                " raise SystemExit(main())",
            ],
            env=env,
            cwd=repo_root,
        )
        try:
            for _ in range(120):
                try:
                    if httpx.get(base_url + "/healthz").is_success:
                        break
                except httpx.HTTPError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("isolated daemon did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    page.goto(base_url + "/#/memory/atlas?profile=default&mode=list")
                    empty_count = wait_for_count(page)
                    if "0 items" not in empty_count:
                        raise AssertionError(f"Atlas empty state missing: {empty_count!r}")
                    page.locator("#atlas-filtered-empty").wait_for(state="visible", timeout=15000)
                finally:
                    browser.close()
            remember = httpx.post(
                base_url + "/memory/remember",
                json={"profile_id": "default", "text": REMEMBER_TEXT},
                timeout=30.0,
            )
            remember.raise_for_status()
            listed = httpx.post(
                base_url + "/memory/atlas",
                json={"profile_id": "default", "kind": "both"},
                timeout=30.0,
            )
            listed.raise_for_status()
            bodies = listed.json()
            if not bodies["items"]:
                raise AssertionError("remembered Atlas row missing from API")
            if not any(REMEMBER_TEXT[:24] in item.get("text_head", "") for item in bodies["items"]):
                raise AssertionError("remembered Atlas row text mismatch in API")
            isolated = httpx.post(
                base_url + "/memory/atlas",
                json={"profile_id": "other"},
                timeout=30.0,
            )
            isolated.raise_for_status()
            if isolated.json()["items"]:
                raise AssertionError("profile isolation failed")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    page.goto(base_url + "/#/memory/atlas?profile=default&mode=list")
                    filled_count = wait_for_count(page)
                    if "0 items" in filled_count:
                        raise AssertionError(f"Atlas count did not update: {filled_count!r}")
                    page.locator("#atlas-list-wrap").wait_for(state="visible", timeout=15000)
                    page.wait_for_function(
                        "(text) => { const el = document.querySelector('#list-viewport');"
                        " return el && (el.innerText || '').includes(text); }",
                        arg=REMEMBER_TEXT[:24],
                        timeout=15000,
                    )
                finally:
                    browser.close()
            pending_evidence = (
                f"PASS\nbase_url={base_url}\nempty_count={empty_count}\n"
                f"filled_count={filled_count}\n"
                "profile_isolation=PASS\nremember_to_atlas=PASS\n"
            )
        finally:
            if process is not None:
                stop_owned_daemon(process, base_url)
        evidence.write_text(pending_evidence, encoding="utf-8")
        print(f"Atlas verification passed at {base_url}; evidence retained at {evidence}")
        return 0
    except Exception as exc:
        evidence.write_text(f"FAIL\n{exc}\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
