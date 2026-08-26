"""T4a recall harness (PRD-B2.1-T4): the T2-pipeline rig + the needle oracle.

The rig boots a REAL daemon (``daemon.app.create_app``) whose config lives
entirely under the caller-given root, and drives the T2 pipeline over HTTP:

    POST /ingest (session A facts+noises) -> POST /ingest (session B cue,
    which parks the focal slot) -> POST /session/recall-pending (the hook
    pull) -> simulated assistant replies -> POST /memory/reinforce (the
    needle consumption matcher) -> read ``last_reinforced`` back.

The armed∧acked gate is hook-internal state and never touches the serve set —
a pull here is equivalent to one non-empty pull. No LLM anywhere: replies are
the material's templates, the budget/tie-break are the daemon's deterministic
keys.

Isolation: materialization is FAIL-LOUD — construction refuses a root that
already carries state (fresh = absent or an empty directory; kept forensics
are never wiped), and callers scope each rig under
``root / "runs" / <run-id> / <point_id>`` for per-point namespaces. The whole
daemon world lives under the root — config, stores, journal, daemon.log.

The needle oracle reproduces the shipped TS hook byte-for-byte
(plugin.ts:58-60,221-253): JS string semantics are UTF-16 CODE UNITS, so the
oracle counts/slices in unit space — a non-BMP emoji is 2 units in JS but 1
code point in Python, pinned by tests/test_needle_oracle.py.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.eval.recall_metrics import RecallRunResult, ReplyObservation

# re-export: the shared freshness contract keeps its historical import path
from mnemoseed_local.eval.rig_freshness import RigRootNotFresh as RigRootNotFresh
from mnemoseed_local.eval.rig_freshness import require_fresh_root
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.ports import ChunkFilter, Page, WeightUpdate

if TYPE_CHECKING:
    from mnemoseed_local.eval.recall_materials import RecallMaterial

# ---------------------------------------------------------------- needle oracle
# Mirror of plugin.ts lines 58-60. Pinned byte-for-byte by test_needle_oracle.

NEEDLE_HEAD_LEN = 24
NEEDLE_MIN_CONTENT = 32
NEEDLE_MID_THRESHOLD = 48
RECALL_FENCE_SANITIZED = "‹mnemoseed-memory-recall›"

_ROLE_PREFIX_RE = re.compile(r"^(user|assistant|tool|system):\s*")
_WS_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"</?mnemoseed-memory-recall>")


def _utf16_units(text: str) -> list[int]:
    """The JS string view: UTF-16 code units (a surrogate pair = 2 units).
    surrogatepass keeps lone surrogates (the oracle's unit slices) intact."""
    data = text.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _utf16_slice(text: str, start: int, end: int) -> str:
    units = _utf16_units(text)
    return "".join(chr(unit) for unit in units[start:end])


def _unit_includes(text: str, needle: str) -> bool:
    """JS ``String.prototype.includes`` over the UTF-16 unit view: a
    contiguous unit-sequence match. Code-point containment would split a
    surrogate pair into a false negative (emoji in a reply vs its needle)."""
    if not needle:
        return True
    hay = _utf16_units(text)
    needle_units = _utf16_units(needle)
    width = len(needle_units)
    return any(hay[i : i + width] == needle_units for i in range(len(hay) - width + 1))


def normalize_recall_text(text: str) -> str:
    """Mirror plugin.ts normalizeRecallText: one role-prefix strip, then
    whitespace collapse + lowercase — needle building and the consumption
    matcher share this exact shape."""
    return _WS_RE.sub(" ", _ROLE_PREFIX_RE.sub("", text)).lower()


def consumption_normalize(text: str) -> str:
    """Mirror plugin.ts noteConsumption: collapse + lowercase, NO role-prefix
    strip (the reply text is raw) — the asymmetric half of the oracle."""
    return _WS_RE.sub(" ", text).lower()


def sanitize_recall_text(text: str) -> str:
    """Mirror plugin.ts sanitizeRecallText: replace BOTH fence literals with
    the ‹› form so an assembled block carries exactly one fence pair."""
    return _FENCE_RE.sub(RECALL_FENCE_SANITIZED, text)


def needles_of(text: str) -> tuple[str, ...]:
    """Mirror plugin.ts needlesOf: a 24-unit head window once the content is
    long enough, plus a centered 24-unit window for longer content (mid-window
    offset = center - 12, JS Math.floor semantics). Dedupe like the JS Set."""
    normalized = normalize_recall_text(text)
    units = _utf16_units(normalized)
    length = len(units)
    if length < NEEDLE_MIN_CONTENT:
        return ()
    needles: list[str] = []
    needles.append(_utf16_slice(normalized, 0, NEEDLE_HEAD_LEN))
    if length >= NEEDLE_MID_THRESHOLD:
        center = length // 2
        start = max(0, center - NEEDLE_HEAD_LEN // 2)
        needles.append(_utf16_slice(normalized, start, start + NEEDLE_HEAD_LEN))
    seen: set[str] = set()
    out: list[str] = []
    for needle in needles:
        if needle not in seen:
            seen.add(needle)
            out.append(needle)
    return tuple(out)


def build_needle_registry(items: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Mirror plugin.ts registerNeedles: needle -> chunk ids over the
    sanitized served texts (needles derive from the EXACT text that would
    enter the injected block)."""
    registry: dict[str, set[str]] = {}
    for item in items:
        text = sanitize_recall_text(str(item.get("text", "")))
        chunk_id = str(item.get("id", ""))
        if not chunk_id:
            continue
        for needle in needles_of(text):
            registry.setdefault(needle, set()).add(chunk_id)
    return registry


def cited_chunk_ids(reply: str, registry: Mapping[str, Iterable[str]]) -> list[str]:
    """Mirror plugin.ts noteConsumption: which registered chunk ids the
    reply's normalized text needle-matches (first-seen order, per-chunk
    dedupe — the hook's citedChunks set)."""
    normalized = consumption_normalize(reply)
    seen: set[str] = set()
    hits: list[str] = []
    for needle, chunk_ids in registry.items():
        if not _unit_includes(normalized, needle):
            continue
        for chunk_id in chunk_ids:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            hits.append(chunk_id)
    return hits


# ---------------------------------------------------------------- config seam


@contextmanager
def _point_config(root: Path, config_path: Path) -> Iterator[None]:
    """Point the config globals at the rig's files for the app's lifespan."""
    import mnemoseed_local.config as config_module
    import mnemoseed_local.dream.snapshot as snapshot_module

    saved = (config_module.CONFIG_PATH, config_module.CONFIG_DIR, snapshot_module.CONFIG_DIR)
    config_module.CONFIG_PATH = config_path
    config_module.CONFIG_DIR = root
    snapshot_module.CONFIG_DIR = root
    try:
        yield
    finally:
        config_module.CONFIG_PATH, config_module.CONFIG_DIR, snapshot_module.CONFIG_DIR = saved


def _config_toml(root: Path, focal_floor: float, budget_chars: int) -> str:
    stores = root / "stores"
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(stores / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(stores / "cortex.db").as_posix()}"\n'
        f"[storage.graph.instances.isolated]\n"
        f'driver = "sqlite_graph"\npath = "{(stores / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(stores / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream]\n"
        "auto_trigger = false\n"
        "floor_pool_points = 1000000\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\n"
        "auto_recall = true\n"
        f"auto_recall_focal_floor = {focal_floor}\n"
        f"auto_recall_budget_chars = {budget_chars}\n"
    )


def release_daemon_log_handler(root: Path) -> None:
    """Hand the process-global daemon logger back to rigs rooted at ``root``.

    The lifespan attaches ONE named FileHandler per process (idempotent by
    name), so a rig's handler would otherwise outlive the rig — holding its
    daemon.log open past teardown (blocking artifact deletion) and making
    later boots log into this root's file. Shared by every rig that boots the
    daemon app under its own root.
    """
    target = logging.getLogger("mnemoseed_local")
    for handler in list(target.handlers):
        filename = getattr(handler, "baseFilename", None)
        if filename is not None and Path(filename).is_relative_to(root):
            target.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------- the rig


class RecallRig:
    """One T2-pipeline rig over a disposable daemon app, driven over HTTP.

    Materialization is fail-loud: construction refuses a root that already
    carries state (fresh = absent or an empty directory — kept forensics are
    never wiped) and writes the rig's own config there; the daemon boots with
    ``capture.auto_recall=True`` and the given (focal_floor, budget_chars).
    ``run_material`` walks the full pipeline and returns the RecallRunResult
    the metrics consume. Per-point isolation comes from the caller rooting
    each rig under ``root / "runs" / <run-id> / <point_id>``.
    """

    def __init__(
        self,
        root: Path,
        *,
        # Rig start anchor (T4a-era baseline), NOT the shipped default — the
        # product defaults live in config.py (DEFAULT_AUTO_RECALL_*).
        focal_floor: float = 0.4,
        budget_chars: int = 1200,
        profile_id: str = "t4a",
    ) -> None:
        require_fresh_root(root)
        self.root = root
        self.focal_floor = focal_floor
        self.budget_chars = budget_chars
        self.profile_id = profile_id
        (root / "config.toml").write_text(_config_toml(root, focal_floor, budget_chars), encoding="utf-8")
        self._stack: ExitStack | None = None
        self._client: TestClient | None = None

    @property
    def client(self) -> TestClient:
        assert self._client is not None, "RecallRig must be entered: with RecallRig(...) as rig"
        return self._client

    @property
    def _state(self) -> Any:
        """The daemon app's live state (stores/memory/config), typed Any —
        fastapi's TestClient stub types ``app`` as a callable."""
        return cast(Any, self.client).app.state

    def __enter__(self) -> RecallRig:
        stack = ExitStack()
        try:
            stack.enter_context(_point_config(self.root, self.root / "config.toml"))
            self._client = stack.enter_context(TestClient(create_app()))
        except BaseException:
            # Startup can fail after the lifespan attached the global daemon.log
            # handler: unwind the seam and release the pin before propagating,
            # or the dead rig's root stays undeletable and later boots bleed
            # into its file (the normal __exit__ never runs on a failed enter).
            stack.close()
            self._release_daemon_log_handler()
            raise
        self._stack = stack
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self._stack is not None
        self._stack.__exit__(exc_type, exc, tb)
        self._stack = None
        self._client = None
        self._release_daemon_log_handler()

    def _release_daemon_log_handler(self) -> None:
        release_daemon_log_handler(self.root)

    # ------------------------------------------------------------ pipeline

    def run_material(self, material: RecallMaterial) -> RecallRunResult:
        """Walk the T2 pipeline for one material point:
        session-A ingest -> session-B cue -> pull -> replies -> reinforce ->
        read-back."""
        ts = 1_700_000_000.0
        for index, (_label, text) in enumerate(material.stored_turns):
            session_id = f"{material.point_id}-a{index:02d}"
            self._ingest(session_id, text, ts + index)
            self._settle(session_id)
        label_to_id = self._label_chunk_ids(material)
        # The materials declare per-turn decay weights; ingest stamps 1.0, so
        # only the deviations need a write BEFORE the cue scan runs (the
        # layering is what makes the focal-floor axis discriminative). Every
        # declared write lands — deduped twins included, both labels resolving
        # onto the one carrier chunk (later declaration wins).
        updates = [
            WeightUpdate(chunk_id=label_to_id[label], decay_weight=decay)
            for label, decay in zip(
                (label for label, _ in material.stored_turns), material.turn_decays, strict=True
            )
            if decay < 1.0
        ]
        if updates:
            self._state.stores.vector.update_weights(updates)
        bsid = f"{material.point_id}-b00"
        self._ingest(bsid, material.cue_turn, ts + len(material.stored_turns) + 1)
        pull = self._pull(bsid)
        items = list(pull["items"])
        registry = build_needle_registry(items)
        observations: list[ReplyObservation] = []
        reply_ts = ts + len(material.stored_turns) + 2
        for template in material.reply_templates:
            self._ingest(bsid, template.text, reply_ts, event="assistant_message")
            reply_ts += 1
            # each reply is an INDEPENDENT detector pass: the hook's
            # per-chunk-per-session dedupe would mask a template's signal
            # (a re-cited chunk shows no new reinforce), so the needle hits
            # are recorded unfiltered — the per-observation FN/Detector-FP
            # math needs every template's detection visible.
            hits = cited_chunk_ids(template.text, registry)
            if hits:
                self._reinforce(hits)
            observations.append(
                ReplyObservation(
                    template_name=template.name,
                    reinforced=tuple(hits),
                    referenced=tuple(
                        label_to_id[label] for label in template.references if label in label_to_id
                    ),
                )
            )
        self._settle(bsid)
        served = tuple(str(item["id"]) for item in items)
        candidates = self._candidate_ids(material, bsid)
        noise_ids = {label_to_id[n.label] for n in material.noise if n.label in label_to_id}
        return RecallRunResult(
            point_id=material.point_id,
            served=served,
            candidate_pool=len(candidates),
            noise_pool=len(noise_ids & candidates),
            served_noise=len(set(served) & noise_ids),
            observations=tuple(observations),
            injected_chars=sum(len(str(item["text"])) + 1 for item in items),
            budget_chars=int(pull.get("budget_chars", self.budget_chars)),
            non_focal_above_floor=int(pull.get("non_focal_above_floor", 0)),
        )

    def chunk_ids_by_label(self, material: RecallMaterial) -> dict[str, str]:
        """label -> carrier chunk id for the material's stored turns (read
        from the store; call after run_material has drained them)."""
        return self._label_chunk_ids(material)

    def consumption_evidence(self) -> tuple[str, ...]:
        """Chunks the run actually reinforced — the consumption evidence read
        back from the store (PRD: 读 last_reinforced 判定消费证据). The vector
        driver materializes ``last_reinforced = ingested_at`` for chunks that
        never saw a reinforce (the stamp's documented fallback), so a
        reinforce is exactly a chunk whose ``last_reinforced`` was REFRESHED to
        a later epoch than its ``ingested_at``."""
        page = self._state.stores.vector.list_chunks(ChunkFilter(profile_id=self.profile_id), Page(0, 100))
        return tuple(
            sorted(
                chunk.chunk_id
                for chunk in page.items
                if chunk.last_reinforced is not None and chunk.last_reinforced != chunk.ingested_at
            )
        )

    # ------------------------------------------------------------ internals

    def _ingest(self, session_id: str, text: str, ts: float, *, event: str = "user_prompt") -> None:
        response = self.client.post(
            "/ingest",
            json={
                "host": HostId.GENERIC.value,
                "event": event,
                "session_id": session_id,
                "profile_id": self.profile_id,
                "ts": ts,
                "content": {"text": text},
            },
        )
        assert response.status_code == 202, response.text

    def _settle(self, session_id: str) -> None:
        response = self.client.post(
            "/session/end", json={"session_id": session_id, "profile_id": self.profile_id}
        )
        assert response.status_code == 200, response.text

    def _pull(self, session_id: str) -> dict[str, Any]:
        response = self.client.post(
            "/session/recall-pending",
            json={"profile_id": self.profile_id, "session_id": session_id},
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    def _reinforce(self, chunk_ids: list[str]) -> None:
        response = self.client.post(
            "/memory/reinforce",
            json={
                "profile_id": self.profile_id,
                "chunk_ids": chunk_ids,
                "node_ids": [],
            },
        )
        assert response.status_code == 200, response.text

    def _label_chunk_ids(self, material: RecallMaterial) -> dict[str, str]:
        """label -> carrier chunk id for every stored turn. A turn whose text
        the daemon deduped onto an earlier twin has no chunk of its own (the
        profile-scoped near-duplicate branch reinforces the carrier instead),
        so resolution falls back to exact text and fails loud when a label
        stays unresolvable — a declared write is never silently dropped.
        Stored turns arrive as user_prompt events, whose canonical chunk text
        is the writer's single ``user:``-prefixed line."""
        page = self._state.stores.vector.list_chunks(ChunkFilter(profile_id=self.profile_id), Page(0, 100))
        by_sid = {chunk.provenance.session_id: chunk.chunk_id for chunk in page.items}
        by_text = {chunk.text: chunk.chunk_id for chunk in page.items}
        resolved: dict[str, str] = {}
        for index, (label, text) in enumerate(material.stored_turns):
            chunk_id = by_sid.get(f"{material.point_id}-a{index:02d}") or by_text.get(f"user: {text}")
            if chunk_id is None:
                raise KeyError(
                    f"material {material.point_id}: stored turn {index} ({label}) resolved to no "
                    "chunk — neither its session nor its text matches a stored carrier"
                )
            resolved[label] = chunk_id
        return resolved

    def _candidate_ids(self, material: RecallMaterial, bsid: str) -> set[str]:
        """The focal candidate pool the scan could have served: chunks whose
        stored entity cues overlap the material's entity at or above the
        focal floor, outside the requesting session."""
        entity = material.entity.casefold()
        candidates: set[str] = set()
        page = self._state.stores.vector.list_chunks(ChunkFilter(profile_id=self.profile_id), Page(0, 100))
        for chunk in page.items:
            if chunk.provenance.session_id == bsid:
                continue
            stored = {entity_name.casefold() for entity_name in chunk.cues.entities}
            if not (stored & {entity}):
                continue
            if chunk.decay_weight < self.focal_floor:
                continue
            candidates.add(chunk.chunk_id)
        return candidates
