"""Daemon-thread executor: a bounded worker pool that never blocks exit.

ThreadPoolExecutor workers are non-daemon threads registered in the
interpreter's atexit join set (``threading._shutdown``), so a wedged worker
keeps the process alive forever — the F2 root cause (PRD-B2.3 根因定案). The
workers here are plain daemon threads on a queue: they are never registered
anywhere and die with the process, so a wedged worker cannot hold the process
hostage. ``close`` is bounded by design — idle workers exit on sentinels and
running+queued futures are awaited for at most ``timeout``; anything still
unresolved at the deadline is abandoned (the caller's recovery mechanism owns
the fallout).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any


@dataclass
class _WorkItem:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any]


class DaemonExecutor:
    """Bounded pool of daemon worker threads consuming a shared queue.

    ``submit`` returns a :class:`concurrent.futures.Future` whose state is set
    from the worker thread — safe for ``asyncio.wrap_future`` — and raises
    ``RuntimeError`` once the executor is closed (ThreadPoolExecutor-compatible
    contract). ``close`` is terminal: no restart after close, no join of
    wedged workers.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._inflight: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            work = _WorkItem(fn, args, kwargs, Future())
            self._queue.put(work)
            while len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._run,
                    name=f"{self._thread_name_prefix}-{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
        return work.future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return  # sentinel: an idle worker exits
            if not item.future.set_running_or_notify_cancel():
                continue  # cancelled while queued: drop silently (TPE semantics)
            with self._lock:
                self._inflight.add(item.future)
            try:
                result = item.fn(*item.args, **item.kwargs)
            except BaseException as exc:
                item.future.set_exception(exc)
            else:
                item.future.set_result(result)
            finally:
                with self._lock:
                    self._inflight.discard(item.future)

    def close(self, timeout: float) -> None:
        """Close the executor: queued work is drained into the wait set,
        sentinels free idle workers, and running+queued futures are awaited for
        at most ``timeout``; then the executor is marked closed and further
        submits raise ``RuntimeError`` (lock-ordered against a racing submit).
        A future still unresolved at the deadline is abandoned — its caller
        stays blocked and the work is dropped rather than running silently
        after close returned."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            wait: list[Future[Any]] = list(self._inflight)
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                wait.append(item.future)
            for _ in range(self._max_workers):
                self._queue.put(None)
        deadline = time.monotonic() + timeout
        for future in wait:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                future.result(timeout=remaining)
            except BaseException:
                pass  # completed, failed, or abandoned at the deadline
