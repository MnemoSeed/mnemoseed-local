"""PRD-448 §8.4 W4b pre-merge corpus sweep: the read-only real-corpus copies
in isolated homes, one run per tree (main arithmetic vs v3 arithmetic), the
per-query deltas and the pre-registered merge-gate bounds.

The sweep runs UNCHANGED on both trees (the main worktree and the v3 tree)
against file copies of the installed runtime's stores (never the live home,
never port 7788). Per query it records the wire recall (served ids, budget
metrics, coverage), route latency and — for the lexically-disjoint probes —
the exact zero-sparse signature (``gated == 0.5*dense`` row-by-row over the
top candidate window), which holds on BOTH fusion formulas and proves the
gate admits nothing the main floor would not have admitted.

The compare step (``compare_sweeps``) emits the bound verdicts:

- disjoint queries never serve new rows: the v3 served list reproduces the
  main served list as its exact prefix (same admitted set, same order; depth
  extends the tail only) and honest-empty queries stay empty;
- median ``tokens_used`` delta within the pre-registered +25% bound;
- recall-route p95 within 1.5x of main.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mnemoseed_local.eval.bridge_harness import stage_model_cache
from mnemoseed_local.eval.recall_harness import (
    _LOOPBACK_HOST,
    _point_config,
    _spare_loopback_port,
    release_daemon_log_handler,
    rig_client,
)
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.ports import ChunkFilter, Page

#: The pinned query sample. kind: paraphrase | disjoint | exact.
QUERY_SAMPLE: tuple[tuple[str, str, str], ...] = (
    ("paraphrase_zh_event_drift", "paraphrase", "之前我们有没有修过 opencode 事件漂移的那个缺陷"),
    (
        "paraphrase_en_suite_run",
        "paraphrase",
        "did we already run the full test suite for the config write repair?",
    ),
    ("paraphrase_zh_gate_checks", "paraphrase", "mnemoseed 的门禁脚本都包含哪几个检查"),
    ("paraphrase_en_verdict", "paraphrase", "what was the review verdict on the recall weakness design?"),
    ("paraphrase_zh_hook_watermark", "paraphrase", "hook 的水印文件是干什么用的"),
    ("paraphrase_en_worktree", "paraphrase", "how did we set up the isolated worktree for the batch?"),
    ("exact_branch_name", "exact", "fix/337-opencode-event-drift"),
    ("exact_worktree_path", "exact", "mnemoseeding/worktrees/w286"),
    ("exact_suite_line", "exact", "4565 passed, 9 skipped"),
    ("disjoint_zh_murals", "disjoint", "敦煌莫高窟的壁画保护技术有哪些"),
    ("disjoint_en_penguins", "disjoint", "penguin migration patterns in Antarctica"),
    ("disjoint_zh_hotpot", "disjoint", "周末去哪里吃正宗的四川火锅"),
)

#: The sweep's latency sample size per query.
_LATENCY_REPEATS = 10

#: The top-window width for the zero-sparse signature search.
_SIGNATURE_TOP_K = 100

_EXPECTED_HOME_ENTRIES = {"chunks.lance", "meta.db", "graph.db", "isolated.db", "models"}


@dataclass(frozen=True)
class QueryRecord:
    """One sweep query's wire recall + latency + signature evidence."""

    label: str
    kind: str
    text: str
    served_ids: tuple[str, ...]
    served_count: int
    tokens_used: int
    dropped_count: int
    budget_tokens: int
    coverage: dict[str, Any]
    latency: dict[str, Any]
    latency_samples_ms: tuple[float, ...]
    reinforcement_verified: bool
    zero_sparse_signature: bool | None  # disjoint probes only


def _require_manifest_root(home: Path) -> None:
    """The sweep's freshness analog: the home must carry exactly the copied
    store set (contamination evidence is refused, nothing is wiped)."""
    present = {entry.name for entry in home.iterdir()}
    unexpected = present - _EXPECTED_HOME_ENTRIES - {"config.toml"}
    missing = _EXPECTED_HOME_ENTRIES - present
    if unexpected or missing:
        raise RuntimeError(
            f"sweep home {home} does not match the copy manifest: missing={missing} unexpected={unexpected}"
        )


def _config_toml(home: Path, base_url: str) -> str:
    return (
        'preset = "embedded"\n'
        f'baseurl = "{base_url}"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 1024\n'
        f'[storage.graph]\npath = "{(home / "graph.db").as_posix()}"\n'
        "[storage.graph.instances.isolated]\n"
        'driver = "sqlite_graph"\n'
        f'path = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "bge_m3_onnx"\nmodel_dir = "{(home / "models" / "bge-m3").as_posix()}"\n'
        "[dream]\n"
        "auto_trigger = false\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\n"
        "auto_recall = false\n"
    )


def _latency_stats(latencies_ms: list[float]) -> dict[str, Any]:
    ordered = sorted(latencies_ms)
    if not ordered:
        return {"samples": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    return {
        "samples": len(ordered),
        "mean_ms": sum(ordered) / len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": ordered[-1],
    }


def _zero_sparse_signature(state: Any, profile_id: str, text: str) -> bool:
    """True when every top-window row carries the exact zero-sparse signature
    (``gated == 0.5*dense``): the sparse leg is exactly 0.0, so both fusion
    formulas score the row identically and the floor admits the same set main
    would have."""
    embedding = state.stores.embed.embed(text)
    store_filter = ChunkFilter(profile_id=profile_id)
    gated = state.stores.vector.search(embedding.dense, embedding.sparse, store_filter, _SIGNATURE_TOP_K)
    dense = state.stores.vector.search(embedding.dense, None, store_filter, _SIGNATURE_TOP_K)
    dense_by_id = {hit.chunk.chunk_id: hit.similarity for hit in dense}
    for gated_hit in gated:
        dense_similarity = dense_by_id.get(gated_hit.chunk.chunk_id)
        if dense_similarity is None:
            # the gated top window diverged from the dense-only window: some
            # row carries sparse mass, so the query is not lexically disjoint
            return False
        if gated_hit.similarity != 0.5 * dense_similarity:
            return False
    return True


def _reinforced(store: LanceDbEmbeddedStore, chunk_id: str) -> bool:
    chunk = store.get_chunk(chunk_id)
    if chunk is None:
        return False
    return chunk.last_reinforced is not None and chunk.last_reinforced != chunk.ingested_at


class SweepRig:
    """One corpus-sweep rig over a copied store home (the daemon app boots
    against the copy; the copy is never wiped)."""

    def __init__(self, home: Path, *, model_source: Path | None = None) -> None:
        self.home = home
        self.model_source = model_source
        self._stack: ExitStack | None = None
        self._client: Any = None

    def __enter__(self) -> SweepRig:
        stack = ExitStack()
        try:
            previous_home = _env_home()
            set_env_home(self.home)
            stack.callback(restore_env_home, previous_home)
            stack.enter_context(_point_config(self.home, self.home / "config.toml"))
            stage_model_cache(self.home / "models" / "bge-m3", source=self.model_source)
            self._client = stack.enter_context(rig_client())
        except BaseException:
            stack.close()
            release_daemon_log_handler(self.home)
            raise
        self._stack = stack
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self._stack is not None
        self._stack.__exit__(exc_type, exc, tb)
        self._stack = None
        self._client = None
        release_daemon_log_handler(self.home)

    @property
    def client(self) -> Any:
        assert self._client is not None, "SweepRig must be entered: with SweepRig(...) as rig"
        return self._client

    @property
    def state(self) -> Any:
        """The daemon app's live state (stores/memory/config), typed Any."""
        return cast(Any, self._client).app.state


def _env_home() -> str | None:
    import os

    return os.environ.get("MNEMOSEED_LOCAL_HOME")


def set_env_home(value: Path) -> None:
    import os

    os.environ["MNEMOSEED_LOCAL_HOME"] = str(value)


def restore_env_home(previous: str | None) -> None:
    import os

    if previous is None:
        os.environ.pop("MNEMOSEED_LOCAL_HOME", None)
    else:
        os.environ["MNEMOSEED_LOCAL_HOME"] = previous


def run_sweep(home: Path, *, tree: str, model_source: Path | None = None) -> dict[str, Any]:
    """One sweep pass over a corpus-copy home on the CURRENT tree."""
    _require_manifest_root(home)
    (home / "config.toml").write_text(
        _config_toml(home, f"http://{_LOOPBACK_HOST}:{_spare_loopback_port()}"), encoding="utf-8"
    )
    rig = SweepRig(home, model_source=model_source)
    with rig:
        state = rig.state
        store: LanceDbEmbeddedStore = state.stores.vector
        profile_id = sorted(store.distinct_profile_ids())[0]
        page = store.list_chunks(ChunkFilter(profile_id=profile_id), Page(offset=0, limit=1))
        total_chunks = page.total
        queries: list[QueryRecord] = []
        for label, kind, text in QUERY_SAMPLE:
            payload = rig.client.post(
                "/memory/recall",
                json={"profile_id": profile_id, "query": text},
            )
            assert payload.status_code == 200, payload.text
            memory = cast(dict[str, Any], payload.json())["memory"]
            served_ids = tuple(str(entry["id"]) for entry in memory["entries"] if entry["kind"] == "chunk")
            latencies: list[float] = []
            for _ in range(_LATENCY_REPEATS):
                start = time.perf_counter()
                repeat = rig.client.post(
                    "/memory/recall",
                    json={"profile_id": profile_id, "query": text},
                )
                assert repeat.status_code == 200, repeat.text
                latencies.append((time.perf_counter() - start) * 1000.0)
            queries.append(
                QueryRecord(
                    label=label,
                    kind=kind,
                    text=text,
                    served_ids=served_ids,
                    served_count=len(served_ids),
                    tokens_used=int(memory["tokens_used"]),
                    dropped_count=int(memory["dropped_count"]),
                    budget_tokens=int(memory["budget_tokens"]),
                    coverage=dict(memory["coverage"]),
                    latency=_latency_stats(latencies),
                    latency_samples_ms=tuple(latencies),
                    reinforcement_verified=all(_reinforced(store, chunk_id) for chunk_id in served_ids),
                    zero_sparse_signature=(
                        _zero_sparse_signature(state, profile_id, text) if kind == "disjoint" else None
                    ),
                )
            )
    record = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "tree": tree,
        "home": str(home),
        "total_chunks": total_chunks,
        "profile_id": profile_id,
        "queries": [asdict(query) for query in queries],
        "aggregate_latency": _latency_stats(
            [sample for query in queries for sample in query.latency_samples_ms]
        ),
    }
    return record


def compare_sweeps(main: dict[str, Any], v3: dict[str, Any]) -> dict[str, Any]:
    """The §8.4 bound verdicts between the main-tree and v3-tree records."""
    main_by_label = {query["label"]: query for query in main["queries"]}
    v3_by_label = {query["label"]: query for query in v3["queries"]}
    findings: list[dict[str, Any]] = []
    bound_failures: list[str] = []

    for label, kind, _text in QUERY_SAMPLE:
        served_main = main_by_label[label]["served_ids"]
        served_v3 = v3_by_label[label]["served_ids"]
        finding = {
            "label": label,
            "kind": kind,
            "main_served": len(served_main),
            "v3_served": len(served_v3),
            "main_tokens_used": main_by_label[label]["tokens_used"],
            "v3_tokens_used": v3_by_label[label]["tokens_used"],
            "main_floor_dropped": main_by_label[label]["coverage"].get("floor_dropped"),
            "v3_floor_dropped": v3_by_label[label]["coverage"].get("floor_dropped"),
            "main_pool_size": main_by_label[label]["coverage"].get("pool_size"),
            "v3_pool_size": v3_by_label[label]["coverage"].get("pool_size"),
        }
        if kind == "disjoint":
            finding["zero_sparse_signature"] = v3_by_label[label]["zero_sparse_signature"]
            prefix_ok = list(served_v3[: len(served_main)]) == list(served_main)
            finding["prefix_reproduces_main"] = prefix_ok
            finding["honest_empty_stays_empty"] = (len(served_main) == 0) == (len(served_v3) == 0)
            if v3_by_label[label]["zero_sparse_signature"] is not True:
                bound_failures.append(f"{label}: zero-sparse signature broken")
            if not prefix_ok:
                bound_failures.append(f"{label}: disjoint served list diverges from main")
            if len(served_main) == 0 and len(served_v3) > 0:
                bound_failures.append(f"{label}: honest-empty query now serves rows")
        findings.append(finding)

    deltas = [finding["v3_tokens_used"] - finding["main_tokens_used"] for finding in findings]
    serving_main = [finding["main_tokens_used"] for finding in findings if finding["main_tokens_used"] > 0]
    median_delta = statistics.median(deltas)
    median_main = statistics.median(serving_main) if serving_main else 0
    tokens_bound = median_main > 0 and median_delta <= 0.25 * median_main
    main_p95 = main["aggregate_latency"]["p95_ms"]
    v3_p95 = v3["aggregate_latency"]["p95_ms"]
    latency_bound = main_p95 > 0 and v3_p95 <= 1.5 * main_p95
    if not tokens_bound:
        bound_failures.append(f"median tokens_used delta {median_delta} exceeds the +25% bound")
    if not latency_bound:
        bound_failures.append(
            f"p95 ratio {v3_p95 / main_p95 if main_p95 else float('inf'):.2f}x exceeds 1.5x"
        )
    return {
        "per_query": findings,
        "median_tokens_delta": median_delta,
        "median_main_tokens": median_main,
        "tokens_bound_ok": tokens_bound,
        "main_p95_ms": main_p95,
        "v3_p95_ms": v3_p95,
        "latency_bound_ok": latency_bound,
        "bound_failures": bound_failures,
        "verdict": "PASS" if not bound_failures else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PRD-448 W4b corpus sweep (one tree per invocation)")
    parser.add_argument("--home", type=Path, required=True, help="corpus-copy home for THIS tree's run")
    parser.add_argument("--tree", required=True, help="tree label recorded into the sweep (main | v3)")
    parser.add_argument("--out", type=Path, default=Path("eval"), help="record output dir")
    parser.add_argument("--model-source", type=Path, default=None)
    parser.add_argument(
        "--compare-with", type=Path, default=None, help="the other tree's sweep record to compare against"
    )
    args = parser.parse_args(argv)
    record = run_sweep(args.home, tree=args.tree, model_source=args.model_source)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"{record['generated_at']}-bridge-sweep-{args.tree}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"sweep record: {out_path}")
    print(f"aggregate latency: {record['aggregate_latency']}")
    if args.compare_with is not None:
        other = json.loads(args.compare_with.read_text(encoding="utf-8"))
        main_record, v3_record = (other, record) if other["tree"] == "main" else (record, other)
        verdict = compare_sweeps(main_record, v3_record)
        verdict_path = args.out / f"{v3_record['generated_at']}-bridge-sweep-verdict.json"
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"verdict record: {verdict_path}")
        print(f"W4b verdict: {verdict['verdict']} failures={verdict['bound_failures']}")
        print(
            "tokens: median delta "
            f"{verdict['median_tokens_delta']} vs main median {verdict['median_main_tokens']}"
        )
        print(f"latency: main p95 {verdict['main_p95_ms']:.1f}ms v3 p95 {verdict['v3_p95_ms']:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
