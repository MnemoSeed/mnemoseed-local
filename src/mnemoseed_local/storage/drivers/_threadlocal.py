"""Per-thread SQLite connections for the embedded sqlite drivers.

A shared ``sqlite3`` connection is single-threaded by default
(``check_same_thread``), which forced the hybrid retriever's two tracks to run
sequentially rather than concurrently. WAL allows concurrent readers plus a
serialized writer across separate connections, so each thread lazily opens and
keeps its own connection. Every open handle is tracked so ``close_all``
releases all of them, and a closed pool refuses new connections so a driver
never silently reopens after teardown.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import cast


class ThreadLocalConnections:
    """One lazily opened SQLite connection per thread, tracked for close."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._open: set[sqlite3.Connection] = set()
        self._closed = False

    def get(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on first use per thread."""
        conn = self._local_conn()
        if conn is not None:
            return conn
        with self._lock:
            conn = self._local_conn()
            if conn is not None:
                return conn
            if self._closed:
                raise sqlite3.ProgrammingError("cannot operate on a closed database")
            conn = self._connect()
            self._local.conn = conn
            return conn

    def _local_conn(self) -> sqlite3.Connection | None:
        return cast("sqlite3.Connection | None", getattr(self._local, "conn", None))

    def _connect(self) -> sqlite3.Connection:
        # ``check_same_thread=False`` only permits close_all() to release a
        # handle from another thread at teardown; each handle is still opened
        # and used solely by one thread (``get`` keeps one per ``threading.local``),
        # so no two threads ever execute on the same connection.
        #
        # The PRAGMAs are ordered so a busy database can never hang a connect:
        # busy_timeout is armed before journal_mode, and a connect that loses the
        # brief write-lock contend is retried for a bounded window (the holder
        # commits quickly). Without this, two threads lazily opening their first
        # connection at the same time -- e.g. concurrent /api/v1/setup requests
        # on a Windows host -- could block on ``PRAGMA journal_mode=WAL`` far
        # past busy_timeout, turning the setup race into a hang.
        for attempt in range(10):
            try:
                conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                self._open.add(conn)
                return conn
            except sqlite3.OperationalError:
                if attempt < 9:
                    time.sleep(0.05)
                    continue
                raise
        raise sqlite3.OperationalError("could not open the database after 10 attempts")

    def close_all(self) -> None:
        """Close every connection the pool has opened (safe from any thread)."""
        with self._lock:
            self._closed = True
            conns = list(self._open)
            self._open.clear()
        for conn in conns:
            conn.close()
