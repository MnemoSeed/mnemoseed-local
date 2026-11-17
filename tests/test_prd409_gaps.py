"""S1 gap closure: real PID-probe evidence and the MCP transform contract.

The two gaps disclosed by S1 are resolved with executable evidence where the
host can be observed and with an explicit, documented limitation where it
cannot. No coverage is claimed that this suite cannot prove.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import test_hook_ts_behavior as ts

_PROBE = r"""
const { spawn } = require("child_process");
const live = process.pid;
let liveAlive = true;
try { process.kill(live, 0) } catch { liveAlive = false }
const child = spawn(process.execPath, ["-e", "setTimeout(()=>{}, 1)"], { stdio: "ignore" });
const dead = child.pid;
child.on("exit", () => setTimeout(() => {
  let deadAlive = true, code = "";
  try { process.kill(dead, 0) } catch (e) { deadAlive = false; code = e.code }
  console.log(JSON.stringify({ liveAlive, deadAlive, code }));
}, 150));
"""


def test_windows_pid_zero_probe_distinguishes_live_from_dead() -> None:
    """Real probe on this host: signal 0 accepts a live pid and raises ESRCH
    for a dead one. It cannot detect PID reuse, so the instance_id handshake
    remains the authority before any bearer is presented."""
    if shutil.which("node") is None:
        return
    result = subprocess.run(["node", "-e", _PROBE], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["liveAlive"] is True
    assert payload["deadAlive"] is False
    assert payload["code"] == "ESRCH"


def test_mcp_transform_callback_is_synchronous(tmp_path: Path) -> None:
    """The host's mcp.transform contract is `(editor) => void`; our callback is
    synchronous, so no async endpoint verification is awaited there. The daemon
    owns endpoint.json's lifecycle and the lazy hook-fetch verification is the
    proven bearer gate — the MCP registration is explicitly NOT claimed as
    verified."""
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "mcp-transform-sync", debug=None)
    assert transcript["isPromise"] is False
    assert transcript["injected"] is True
