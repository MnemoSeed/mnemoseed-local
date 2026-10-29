"""A launcher that starts a grandchild and exits before cleanup runs.

The verifier starts a launcher, so the process it owns can spawn a descendant
and leave on its own. The grandchild holds a loopback port and keeps a
heartbeat file current; the teardown oracles read both.

Usage: python atlas_teardown_launcher.py <workdir> <host> <port>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_GRANDCHILD = """
import os
import socket
import sys
import time
from pathlib import Path

host, port, workdir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
listener = socket.socket()
listener.bind((host, port))
listener.listen(1)
(workdir / "grandchild.pid").write_text(str(os.getpid()), encoding="utf-8")
while True:
    (workdir / "heartbeat").write_text(str(time.time()), encoding="utf-8")
    time.sleep(0.2)
"""


def main() -> int:
    workdir, host, port = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, "-c", _GRANDCHILD, host, port, str(workdir)])
    (workdir / "launcher.pid").write_text(str(os.getpid()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
