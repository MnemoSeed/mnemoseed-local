"""Hardware probing for the doctor's tier recommendation (A3 T5).

Design/01 §4.8 (decision 8): the daemon anchors three hardware tiers
(lite | standard | advanced) on the ``dream.hardware_tier`` config key; init
and doctor only RECOMMEND a tier from probed RAM / NVIDIA VRAM — a mismatch
with the configured tier is a hint, never a failure.

All probes are dependency-free and never raise: probing is advisory, so any
failure degrades to "unknown" (RAM: ``None``) or "no NVIDIA GPU" (VRAM: 0.0).
"""

from __future__ import annotations

import subprocess
import sys

#: Bytes per GiB (probed byte counts are reported in binary units).
_BYTES_PER_GIB = float(1024**3)


def probe_ram_gb() -> float | None:
    """Total physical RAM in GiB, or ``None`` on an unsupported platform."""
    if sys.platform == "win32":
        return _probe_ram_windows()
    if sys.platform == "linux":
        return _probe_ram_linux()
    if sys.platform == "darwin":
        return _probe_ram_macos()
    return None


def _probe_ram_windows() -> float | None:
    """RAM via GlobalMemoryStatusEx (ctypes; zero dependencies)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        """Win32 MEMORYSTATUSEX layout; only ullTotalPhys is consumed."""

        dwLength: int
        dwMemoryLoad: int
        ullTotalPhys: int
        ullAvailPhys: int
        ullTotalPageFile: int
        ullAvailPageFile: int
        ullTotalVirtual: int
        ullAvailVirtual: int
        ullAvailExtendedVirtual: int
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        # windll only exists on Windows; this function is called behind the
        # sys.platform guard, so the ignore keeps linux/macOS mypy targets green.
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        if not ok:
            return None
        return float(status.ullTotalPhys) / _BYTES_PER_GIB
    except (AttributeError, OSError, ValueError):
        return None


def _probe_ram_linux() -> float | None:
    """RAM via /proc/meminfo (MemTotal is reported in KiB)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / (1024**2)
    except (IndexError, OSError, ValueError):
        return None
    return None


def _probe_ram_macos() -> float | None:
    """RAM via `sysctl -n hw.memsize` (bytes), with a 2s subprocess timeout."""
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return float(proc.stdout.strip()) / _BYTES_PER_GIB
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def probe_max_vram_gb() -> float:
    """Largest NVIDIA GPU memory in GiB via nvidia-smi; ``0.0`` when absent.

    One bad line in the query output never poisons the other GPUs; any
    transport / parse failure simply means "no usable NVIDIA reading".
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    values: list[float] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(float(line))  # MiB per GPU
        except ValueError:
            continue
    return max(values) / 1024.0 if values else 0.0


def recommended_tier(vram_gb: float | None, ram_gb: float | None) -> str:
    """Three-way tier recommendation (design/01 §4.8 decision 8).

    VRAM >= 22 GiB -> ``advanced``; VRAM >= 7 GiB or RAM >= 30 GiB ->
    ``standard``; otherwise ``lite``. ``None`` (unknown probe) counts as 0 so a
    failed probe never inflates the recommendation.
    """
    vram = 0.0 if vram_gb is None else vram_gb
    ram = 0.0 if ram_gb is None else ram_gb
    if vram >= 22:
        return "advanced"
    if vram >= 7 or ram >= 30:
        return "standard"
    return "lite"
