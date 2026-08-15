"""PRD-03 T2.5: single-instance sqlite drivers are safe under cross-thread use.

The sqlite drivers keep one connection per thread (threading.local) so parallel
track reads and transactional writes never touch another thread's handle. These
tests exercise one driver instance from many threads: every committed row must
be visible to every thread, concurrent writers must serialize to the exact
accumulation, and close() must release every per-thread connection so a driver
never silently reopens after teardown.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import NodeFilter, Page, PoolState, StoredProfile, TurnRange

_NODE_PROPS = {
    "domain": "coding",
    "statement": "thread safety",
    "valence": 0.5,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [],
}

_WORKER_COUNT = 8
_BARRIER_TIMEOUT = 30.0


def _node(node_id: str, profile: str = "p1") -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_NODE_PROPS),
        provenance=Provenance(asserted_by="test-agent", source="x", session_id="s1"),
        valid_from=100.0,
    )


class _Errors:
    """Thread-safe error bag: workers append, the main thread asserts empty."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[Exception] = []

    def add(self, exc: Exception) -> None:
        with self._lock:
            self.items.append(exc)


def _wait(barrier: threading.Barrier, errors: _Errors) -> None:
    """Join a start-gate barrier; a broken barrier records, never hangs."""
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT)
    except threading.BrokenBarrierError as exc:
        errors.add(exc)


def _run(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# ------------------------------------------------------------ graph driver


def test_graph_driver_supports_concurrent_writes_and_reads(tmp_path) -> None:
    """One shared instance: 8 threads upsert then read; every thread sees all rows."""
    driver = SqliteGraphDriver(path=tmp_path / "g.db")
    try:
        driver.upsert_node(_node("seed"))
        barrier = threading.Barrier(_WORKER_COUNT)
        errors = _Errors()

        def worker(index: int) -> None:
            try:
                driver.upsert_node(_node(f"n-{index:02d}"))
            except Exception as exc:
                errors.add(exc)
            _wait(barrier, errors)
            try:
                seen = {
                    node.node_id
                    for node in driver.list_nodes(NodeFilter(profile_id="p1"), Page(0, 100)).items
                }
                expected = {f"n-{j:02d}" for j in range(_WORKER_COUNT)} | {"seed"}
                if seen != expected:
                    raise AssertionError(f"thread {index} sees {sorted(seen)}")
            except Exception as exc:
                errors.add(exc)

        _run([threading.Thread(target=worker, args=(i,)) for i in range(_WORKER_COUNT)])
        assert errors.items == []
        final = driver.get_node("seed")
        assert final is not None and final.node_id == "seed"
    finally:
        asyncio.run(driver.close())


def test_graph_driver_after_close_rejects_every_thread(tmp_path) -> None:
    """close() releases per-thread handles; any later use on any thread raises."""
    driver = SqliteGraphDriver(path=tmp_path / "g.db")
    driver.upsert_node(_node("warm"))
    # open a connection on a worker thread
    worker = threading.Thread(target=lambda: driver.get_node("warm"))
    worker.start()
    worker.join()
    asyncio.run(driver.close())

    with pytest.raises(sqlite3.ProgrammingError):
        driver.get_node("warm")  # main-thread handle is closed
    fresh_errors: list[Exception] = []

    def fresh_thread() -> None:
        try:
            driver.get_node("warm")
        except Exception as exc:
            fresh_errors.append(exc)

    fresh = threading.Thread(target=fresh_thread)
    fresh.start()
    fresh.join()
    assert len(fresh_errors) == 1
    assert isinstance(fresh_errors[0], sqlite3.ProgrammingError)


# ------------------------------------------------------------ meta driver


def test_meta_driver_single_instance_concurrent_writers_accumulate(tmp_path) -> None:
    """One shared instance: 8 threads x 10 pool_adds serialize to exactly 80.0."""
    driver = SqliteMetaDriver(path=tmp_path / "m.db")
    try:
        driver.pool_add("race", 0.0, TurnRange(start=0, end=0))  # seed the row

        def worker(index: int, errors: _Errors) -> None:
            for j in range(10):
                try:
                    driver.pool_add("race", 1.0, TurnRange(start=index * 10 + j, end=index * 10 + j))
                except Exception as exc:
                    errors.add(exc)

        errors = _Errors()
        _run([threading.Thread(target=worker, args=(i, errors)) for i in range(_WORKER_COUNT)])
        assert errors.items == []
        state = driver.pool_state("race")
        assert state.balance == _WORKER_COUNT * 10
    finally:
        asyncio.run(driver.close())


def test_meta_driver_concurrent_readers_consistent(tmp_path) -> None:
    """Concurrent pool_state reads see the same committed row."""
    driver = SqliteMetaDriver(path=tmp_path / "m.db")
    try:
        driver.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
        driver.pool_add("u1", 7.0, TurnRange(start=0, end=3))
        barrier = threading.Barrier(_WORKER_COUNT)
        errors = _Errors()
        states: list[PoolState] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            _wait(barrier, errors)
            try:
                got = driver.pool_state("u1")
                with lock:
                    states.append(got)
            except Exception as exc:
                errors.add(exc)

        _run([threading.Thread(target=worker, args=(i,)) for i in range(_WORKER_COUNT)])
        assert errors.items == []
        assert len(states) == _WORKER_COUNT
        # pool_add alone never advances the watermark: every reader must agree
        # the row is exactly the committed (balance=7.0, no watermark) state.
        expected = PoolState(balance=7.0)
        assert all(state == expected for state in states)
    finally:
        asyncio.run(driver.close())
