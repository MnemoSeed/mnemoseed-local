"""Best-effort process private-commit charge for the observability surfaces.

Private commit is the committed private byte accounting (Windows commit charge;
on Linux the closest per-process proxy is the smaps private resident plus the
swapped-out total). Every probe is dependency-free and never raises:
observability is advisory, so an unsupported platform or a failed read is
``None`` (unknown), never a guess.
"""

from __future__ import annotations

import sys

_BYTES_PER_MB = float(1024 * 1024)


def private_commit_bytes() -> int | None:
    """The current process's private commit in bytes, or ``None`` when unknown."""
    if sys.platform == "win32":
        return _private_commit_windows()
    if sys.platform == "linux":
        return _private_commit_linux()
    return None


def process_commit_mb() -> float | None:
    """The private-commit figure in MB (one decimal), or ``None`` when unknown."""
    value = private_commit_bytes()
    return None if value is None else round(value / _BYTES_PER_MB, 1)


def _private_commit_windows() -> int | None:
    """PrivateUsage from GetProcessMemoryInfo (the commit-charge figure)."""
    import ctypes

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        """Win32 PROCESS_MEMORY_COUNTERS_EX layout; only PrivateUsage is consumed."""

        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]
        PrivateUsage: int

    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        # windll only exists on Windows; this function runs behind the
        # sys.platform guard, so the ignore keeps linux/macOS mypy targets green.
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
        # explicit handle typing: the default int restype truncates the -1
        # pseudo-handle and GetProcessMemoryInfo rejects it (invalid handle)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return int(counters.PrivateUsage)
    except (AttributeError, OSError, ValueError):
        return None


def _private_commit_linux() -> int | None:
    """smaps_rollup private total (Private_Clean + Private_Dirty + Swap, KiB)."""
    total_kib = 0
    try:
        with open("/proc/self/smaps_rollup", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(("Private_Clean:", "Private_Dirty:", "Swap:")):
                    total_kib += int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return total_kib * 1024 if total_kib > 0 else None
