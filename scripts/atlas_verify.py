"""Verify the shipped Atlas page against an isolated local daemon."""

from __future__ import annotations

import ctypes
import functools
import os
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import sync_playwright

from mnemoseed_local.credential import DaemonCredentialError, credential_path, read_credential_at
from mnemoseed_local.rest_client import credential_headers

RESERVED_PORTS = (7788, 4096)
REMEMBER_TEXT = "Atlas verification memory for isolated browser proof"
#: Bounded wait for the isolated daemon's credential: its lifespan issues the
#: file before serving, so this only covers the write landing.
CREDENTIAL_WAIT_SECONDS = 30.0
#: Bounded wait for the owned tree to leave, and the interval at which its
#: members are asked again while the wait runs.
STOP_GRACE_SECONDS = 15.0
SURVIVOR_POLL_SECONDS = 0.1
PORT_RELEASE_ATTEMPTS = 40
PORT_RELEASE_INTERVAL_SECONDS = 0.25

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INVALID_PARAMETER = 87
_JOB_MEMBER_CAPACITY = 64


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


def issued_credential(home: Path) -> str:
    """The credential the isolated daemon issued, read from that daemon's home.

    The daemon lifespan writes the credential file before it serves, so the
    bounded wait only covers the write landing; a daemon that never issues one
    is a boot failure and is reported as such.
    """
    deadline = time.monotonic() + CREDENTIAL_WAIT_SECONDS
    while True:
        try:
            return read_credential_at(home)
        except DaemonCredentialError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the isolated daemon issued no credential at {credential_path(home)}"
                ) from None
            time.sleep(0.25)


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
    """The kernel library, with the handle-returning prototypes declared."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    kernel32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


@functools.lru_cache(maxsize=1)
def _job_structures() -> tuple[Any, Any]:
    """The job-object structures: its extended limit, and its member pid list."""

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _MemberIds(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", ctypes.c_ulong),
            ("NumberOfProcessIdsInList", ctypes.c_ulong),
            ("ProcessIdList", ctypes.c_size_t * 1),
        ]

    return _ExtendedLimit, _MemberIds


def _create_job() -> int:
    """A job object that ends every member it holds when its last handle closes."""
    kernel32 = _kernel32()
    extended_limit, _ = _job_structures()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise RuntimeError("could not create the daemon ownership job object")
    limits = extended_limit()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    armed = kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not armed:
        kernel32.CloseHandle(handle)
        raise RuntimeError("could not arm the daemon ownership job object")
    return int(handle)


def _close_job(job: int) -> None:
    """Release a job handle; the job ends its members as the last handle closes."""
    _kernel32().CloseHandle(job)


def _assign_to_job(job: int, pid: int) -> bool:
    """Put a process inside the job, so every descendant it creates is held too."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)


def _terminate_job(job: int) -> None:
    """End every process the ownership job holds."""
    if not _kernel32().TerminateJobObject(job, 1):
        raise RuntimeError("could not end the daemon ownership job object")


def _job_member_pids(job: int) -> tuple[int, ...]:
    """The pids the ownership job holds, asked of the job rather than of a list."""
    kernel32 = _kernel32()
    _, member_ids = _job_structures()
    capacity = _JOB_MEMBER_CAPACITY
    while True:
        offset = member_ids.ProcessIdList.offset
        size = offset + capacity * ctypes.sizeof(ctypes.c_size_t)
        buffer = ctypes.create_string_buffer(size)
        queried = kernel32.QueryInformationJobObject(
            job, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, buffer, size, None
        )
        if not queried:
            raise RuntimeError("could not read the process list of the ownership job object")
        head = ctypes.cast(buffer, ctypes.POINTER(member_ids)).contents
        assigned = int(head.NumberOfAssignedProcesses)
        listed = int(head.NumberOfProcessIdsInList)
        if listed < assigned:
            capacity = assigned
            continue
        held = (ctypes.c_size_t * listed).from_buffer(buffer, offset)
        return tuple(int(pid) for pid in held)


def _windows_process_gone(pid: int) -> bool:
    """Whether a pid has left, asked of a handle rather than of a process list."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SYNCHRONIZE, False, pid)
    if not handle:
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            return True
        raise RuntimeError(f"could not open owned process {pid} to confirm it left")
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) != _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _signal_group(group: int, sig: int) -> None:
    """Signal the group a spawned process leads; an emptied group needs nothing."""
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        return


def _group_is_populated(group: int) -> bool:
    """Whether the group a spawned process led still holds a process."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def _leading_group(process: subprocess.Popen[bytes]) -> int:
    """The process group the spawned tree lives in, asked of the kernel at spawn."""
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return process.pid


class OwnedDaemon:
    """A daemon this run started, with the container that holds its tree.

    Ownership is taken at spawn: a job object on Windows, a session and process
    group elsewhere. Teardown ends that container and confirms every process it
    held has left, so a process this run never started stays out of reach.
    """

    def __init__(
        self, process: subprocess.Popen[bytes], job: int | None = None, pgid: int | None = None
    ) -> None:
        self.process = process
        self.job = job
        self.pgid = process.pid if pgid is None else pgid

    @property
    def pid(self) -> int:
        return self.process.pid

    def owned_pids(self) -> tuple[int, ...]:
        """The pids this run owns: the process it started and its job members."""
        members = () if self.job is None else _job_member_pids(self.job)
        return (self.pid, *(member for member in members if member != self.pid))

    def stop(self) -> None:
        """End the owned tree, asking first and forcing it after the grace period."""
        self._end_tree(signal.SIGTERM)
        if self.survivors(STOP_GRACE_SECONDS):
            self._end_tree(signal.SIGKILL)

    def survivors(self, timeout: float) -> tuple[int, ...]:
        """The run-owned processes still present after a bounded wait.

        Windows asks the job for its members and waits on each one by handle,
        which reaches a descendant whose launcher already left. Elsewhere the
        process this run started is collected as this process's own child and
        the group recorded at spawn is polled until the deadline, so a group
        that is still emptying does not report as survivors; a live group
        reports as its leader.
        """
        deadline = time.monotonic() + timeout
        if self.job is not None:
            alive = self.owned_pids()
            while alive and time.monotonic() < deadline:
                alive = tuple(pid for pid in alive if not _windows_process_gone(pid))
                time.sleep(SURVIVOR_POLL_SECONDS)
            return alive
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        while True:
            if not _group_is_populated(self.pgid):
                return ()
            if time.monotonic() >= deadline:
                return (self.pid,)
            time.sleep(SURVIVOR_POLL_SECONDS)

    def _end_tree(self, sig: int) -> None:
        """End the whole owned tree through the container that holds it."""
        if self.job is not None:
            _terminate_job(self.job)
            return
        _signal_group(self.pgid, sig)


def spawn_owned_daemon(argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> OwnedDaemon:
    """Start a daemon and take ownership of its tree as that tree is created.

    What teardown acts on later is the record taken here: the pid this run
    started and the process group that holds its descendants, both captured
    before the spawner can exit. Nothing is selected afterwards from a process
    list read back out of the operating system, so only this run's own tree can
    be reached.
    """
    if os.name != "nt":
        process = subprocess.Popen(list(argv), env=dict(env), cwd=cwd, start_new_session=True)
        return OwnedDaemon(process, pgid=_leading_group(process))
    job = _create_job()
    try:
        process = subprocess.Popen(list(argv), env=dict(env), cwd=cwd)
    except BaseException:
        _close_job(job)
        raise
    if not _assign_to_job(job, process.pid):
        process.kill()
        process.wait()
        _close_job(job)
        raise RuntimeError("could not take ownership of the isolated daemon tree")
    return OwnedDaemon(process, job)


def stop_owned_daemon(daemon: OwnedDaemon, base_url: str) -> tuple[int, ...]:
    """Stop the tree this run started, confirm every part of it left, free the port.

    The owned pids are reported before the tree is ended and confirmed gone
    afterwards. A descendant whose launcher already exited is still held by the
    ownership container, which is the only reason it is reachable at all.
    """
    pids = daemon.owned_pids()
    print(f"Run-owned PIDs: {', '.join(str(pid) for pid in pids)} (root {daemon.pid})")
    daemon.stop()
    survivors = daemon.survivors(STOP_GRACE_SECONDS)
    if survivors:
        raise RuntimeError(f"owned daemon processes survived teardown: {survivors}")
    print(f"Run-owned PIDs confirmed gone: {', '.join(str(pid) for pid in pids)}")
    _await_port_release(base_url)
    return pids


def _await_port_release(base_url: str) -> None:
    """Refuse a run whose port still answers, however the tree was ended."""
    for _ in range(PORT_RELEASE_ATTEMPTS):
        try:
            httpx.get(base_url + "/healthz", timeout=1.0)
        except httpx.HTTPError:
            return
        time.sleep(PORT_RELEASE_INTERVAL_SECONDS)
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
        process = spawn_owned_daemon(
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
            credential = issued_credential(home)
            headers = credential_headers(credential)
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
                headers=headers,
                timeout=30.0,
            )
            remember.raise_for_status()
            listed = httpx.post(
                base_url + "/memory/atlas",
                json={"profile_id": "default", "kind": "both"},
                headers=headers,
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
                headers=headers,
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
            stop_owned_daemon(process, base_url)
        evidence.write_text(pending_evidence, encoding="utf-8")
        print(f"Atlas verification passed at {base_url}; evidence retained at {evidence}")
        return 0
    except Exception as exc:
        evidence.write_text(f"FAIL\n{exc}\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
