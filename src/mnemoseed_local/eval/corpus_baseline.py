"""Seeded corpus baseline over a copied corpus home (eval only).

The baseline answers one question about the shipped recall surface: over the
REAL corpus, how often does a mechanically derived probe bring its source
chunk back within the served top-8, and when it does not, was the answer
already present under a duplicate sibling?

Sampling is deterministic: ``random.Random(seed)`` picks source chunks from a
stratum-interleaved order (host x recency tertile), and every probe family
carries a verifiable lexical invariant:

- ``paraphrase``: the source's top content tokens are stripped, the residual
  is framed by a fixed template in the chunk's language, and the probe shares
  no content token with its source;
- ``exact``: a verbatim source span carrying at least three content tokens;
- ``disjoint``: a seeded rotation over canned off-corpus topics, so an empty
  serve set is the honest answer.

Every probe runs the REAL daemon against the copy through ``SweepRig`` (spare
loopback port, never port 7788, never the installed runtime) and records the
wire recall plus two latency samples.

Command line: ``python -m mnemoseed_local.eval.corpus_baseline --home
eval/corpus-baseline-home --out eval --probes 90``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mnemoseed_local.eval.bridge_harness import stage_model_cache
from mnemoseed_local.eval.bridge_sweep import (
    _EXPECTED_HOME_ENTRIES,
    SweepRig,
    _config_toml,
    _env_home,
    _latency_stats,
    restore_env_home,
    set_env_home,
)
from mnemoseed_local.eval.recall_harness import _LOOPBACK_HOST, _spare_loopback_port
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.ports import ChunkFilter, Page

# ---------------------------------------------------------------- constants

#: The corpus copy's profile (the only profile a baseline copy carries).
_PROFILE_ID = "default"

#: Probe families, in the round-robin run order.
_FAMILIES: tuple[str, ...] = ("paraphrase", "exact", "disjoint")

#: Families whose probes carry a source_chunk_id (hit-rate oracle applies).
_SOURCE_FAMILIES: tuple[str, ...] = ("paraphrase", "exact")

#: Per-family probe budget; the total cap is the product over the families.
_PER_FAMILY = 30
_MAX_PROBES = _PER_FAMILY * len(_FAMILIES)

#: The served window the hit-rate is read over.
_TOP_K = 8

#: Latency samples per probe (beyond the measured recall itself).
_LATENCY_SAMPLES = 2

#: How many of a source's top content tokens a paraphrase must strip.
_TOP_CONTENT_TOKENS = 8

#: The paraphrase residual window, in characters.
_FRAGMENT_CHARS = 48

#: The exact-family verbatim window, in characters.
_EXACT_WINDOW_CHARS = 64

#: Seeded window attempts before the exact family falls back to the full text.
_EXACT_ATTEMPTS = 6

#: The exact family's minimum verbatim content-token hits.
_MIN_EXACT_TOKEN_HITS = 3

#: The shortest source text a probe family accepts.
_MIN_SOURCE_CHARS = 40

#: The source excerpt length recorded per probe.
_EXCERPT_CHARS = 120

#: Token-set Jaccard at or above which a served chunk is a near-duplicate.
_NEAR_DUPLICATE_JACCARD = 0.9

#: The number of recency bands the stratification uses.
_RECENCY_BANDS = 3

#: The band label a probe without a source chunk carries.
_NO_BAND = "n/a"

#: The canned off-corpus topics the disjoint family rotates over.
_DISJOINT_TOPICS: tuple[tuple[str, str], ...] = (
    ("zh", "敦煌莫高窟壁画的颜料成分"),
    ("zh", "四川火锅底料的炒制顺序"),
    ("zh", "西湖环湖骑行的路线安排"),
    ("zh", "番茄搭架子的绑蔓手法"),
    ("zh", "京剧脸谱颜色的寓意"),
    ("zh", "野生大熊猫的觅食习惯"),
    ("zh", "手冲咖啡壶的滤杯选择"),
    ("zh", "海水稻耐盐碱的生理机理"),
    ("zh", "汉服裁剪的放量计算"),
    ("zh", "陶艺拉坯的泥料含水率"),
    ("en", "penguin migration patterns in Antarctica"),
    ("en", "how do you proof sourdough bread overnight"),
    ("en", "what causes the northern lights"),
    ("en", "trail running shoes for rocky terrain"),
    ("en", "how to prune an old apple tree"),
    ("en", "why do cats knead blankets"),
    ("en", "history of the viola da gamba"),
    ("en", "volcanic soil and coffee flavour"),
    ("en", "frigate birds and long distance soaring"),
    ("en", "glacier lake outburst flood forecasting"),
)

#: The minimum number of off-corpus topics the disjoint family requires.
_MIN_DISJOINT_TOPICS = 12

#: The mechanical paraphrase frames, per chunk language.
_PARAPHRASE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "zh": (
        "你还记得{subject}这件事吗？",
        "之前{subject}那个问题后来怎么处理的？",
        "{subject}后来有没有再出现过？",
    ),
    "en": (
        "do you remember {subject}?",
        "what did we end up doing about {subject}?",
        "did {subject} come up again after that?",
    ),
}

_EN_STOPWORDS = frozenset(
    "a an and are as at be been but by can could do does did for from had has have how i if in "
    "into is it its me my of on or our so than that the their them then there these they this "
    "to was we were what when where which who why will with you your".split()
)
_ZH_STOPWORDS = frozenset(
    "的 了 是 在 和 与 也 就 都 而 及 或 一个 我们 你们 他们 这个 那个 这 那 有 没 没有 吗 呢 "
    "吧 啊 把 被 给 让 对 从 到 会 要 能 可以 怎么 什么 为什么 之前 后来 然后 因为 所以 但是 "
    "如果 还 又 再 已 已经 一 不 很 更 最 些 之 其 为 以 于 上 下 中 个".split()
)

#: The served entry keys that are not flags.
_SERVED_FIELDS = frozenset({"id", "kind", "score", "text"})

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WS_RE = re.compile(r"\s+")

#: The installed runtime's home and model cache (read-only; never booted).
_INSTALLED_HOME = Path.home() / ".mnemoseed-local"
_INSTALLED_MODEL_DIR = _INSTALLED_HOME / "models" / "bge-m3"

_DISCLOSURES: tuple[str, ...] = (
    "/memory/recall is fire-and-forget on the daemon: each probe serves and reinforces its "
    "served chunks on the copy, so decay weights and last_reinforced stamps drift while the "
    "run proceeds.",
    "corpus totals are read from the copy before the first probe runs (the ground-truth "
    "snapshot of the copy as handed to the run).",
    "the daemon boots against the copy on a spare loopback port; port 7788 and the installed "
    "runtime are never touched.",
    "paraphrase probes are mechanical (token stripping plus fixed templates), not model "
    "rewrites, so their lexical distance from the source is an upper bound of a paraphrase.",
)

# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class Probe:
    """One planned probe and the bands its source chunk sits in."""

    probe_id: str
    family: str
    source_chunk_id: str | None
    query: str
    host_band: str
    recency_band: str
    source_excerpt: str
    content_token_hits: int


@dataclass(frozen=True)
class ProbePlan:
    """The seeded plan: the interleaved run order plus its sampling evidence."""

    probes: tuple[Probe, ...]
    quotas: dict[str, int]
    selected: dict[str, int]
    verification: dict[str, Any]


@dataclass(frozen=True)
class ServedChunk:
    """One served chunk entry from the wire recall."""

    rank: int
    chunk_id: str
    score: float
    flags: dict[str, Any]


@dataclass(frozen=True)
class ProbeResult:
    """One probe's wire recall on the copy plus its latency samples."""

    probe_id: str
    family: str
    source_chunk_id: str | None
    query: str
    host_band: str
    recency_band: str
    served: tuple[ServedChunk, ...]
    served_count: int
    tokens_used: int
    dropped_count: int
    budget_tokens: int
    coverage: dict[str, Any]
    latency_samples_ms: tuple[float, ...]


# ---------------------------------------------------------------- tokens


def _tokens(text: str) -> list[str]:
    """Lowercased tokens: latin word runs and single CJK characters."""
    return _TOKEN_RE.findall(text.lower())


def _template_vocabulary() -> frozenset[str]:
    """Every token the paraphrase templates contribute: the frame words are
    reserved, so a mechanical paraphrase never reintroduces them as source
    content."""
    words: set[str] = set()
    for templates in _PARAPHRASE_TEMPLATES.values():
        for template in templates:
            words.update(_tokens(template.replace("{subject}", " ")))
    return frozenset(words)


_STOPWORDS = frozenset(_EN_STOPWORDS | _ZH_STOPWORDS | _template_vocabulary())


def _content_tokens(text: str) -> list[str]:
    """The text's content tokens ranked by frequency then first occurrence
    (stopwords, template frame words and pure digits are not content)."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for index, token in enumerate(_tokens(text)):
        if token in _STOPWORDS or token.isdigit():
            continue
        if len(token) == 1 and token.isascii():
            continue
        counts[token] += 1
        first.setdefault(token, index)
    return sorted(counts, key=lambda token: (-counts[token], first[token]))


def _top_content(text: str) -> set[str]:
    """The source's top content tokens: the set a paraphrase must not carry."""
    return set(_content_tokens(text)[:_TOP_CONTENT_TOKENS])


def _lexical_units(text: str) -> set[str]:
    """Absence-test units: latin word tokens plus CJK bigrams (a single CJK
    character is too common to prove that a topic is off-corpus)."""
    units = {token for token in _tokens(text) if token.isascii() and len(token) >= 3}
    for run in _CJK_RUN_RE.findall(text):
        units.update(run[index : index + 2] for index in range(len(run) - 1))
    return units


def _language(text: str) -> str:
    """zh when the text's CJK characters dominate its latin letters, else en."""
    cjk = len(_CJK_RUN_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return "zh" if cjk * 2 >= latin else "en"


def _jaccard(left: set[str], right: set[str]) -> float:
    """Token-set Jaccard similarity (0.0 when either side is empty)."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _median(values: Sequence[float]) -> float | None:
    """The median of the values, or None for an empty sample."""
    return float(statistics.median(values)) if values else None


def _histogram(values: Sequence[Any]) -> dict[str, int]:
    """Value -> count over JSON-friendly keys (a missing value is 'none')."""
    counts: Counter[str] = Counter("none" if value is None else str(value) for value in values)
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------- corpus snapshot


def _as_epoch(value: Any) -> float:
    """Epoch seconds for a float, int, datetime or ISO string stamp."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value).timestamp()
            except ValueError:
                return 0.0
    return 0.0


def _chunk_host(chunk: ChunkStamp) -> str:
    """The chunk's capture host as a stable band label."""
    host = getattr(chunk.provenance, "host", None)
    return str(getattr(host, "value", host) or "unknown")


def _chunk_epoch(chunk: ChunkStamp) -> float:
    """The chunk's capture time in epoch seconds: the provenance stamp when the
    schema carries one, else the store's ingest stamp."""
    stamp = getattr(chunk.provenance, "ts", None)
    if stamp is None:
        stamp = getattr(chunk, "ingested_at", None)
    return _as_epoch(stamp)


def _recency_cuts(epochs: Sequence[float]) -> tuple[float, ...]:
    """Tertile cut points over the corpus epochs (data-derived, not clock-derived)."""
    ordered = sorted(epochs)
    if not ordered:
        return ()
    last = len(ordered) - 1
    return tuple(
        ordered[min(last, max(0, round(len(ordered) * (index + 1) / _RECENCY_BANDS) - 1))]
        for index in range(_RECENCY_BANDS - 1)
    )


def _recency_label(epoch: float, cuts: Sequence[float]) -> str:
    """The recency band label for one epoch (bands run oldest to newest)."""
    for index, cut in enumerate(cuts):
        if epoch <= cut:
            return f"r{index}"
    return f"r{len(cuts)}"


def _page_all(store: LanceDbEmbeddedStore, *, page_size: int = 500) -> list[ChunkStamp]:
    """Every chunk of the corpus profile, paged to completion and ordered by
    chunk id (a stable base order for the seeded sampling)."""
    by_id: dict[str, ChunkStamp] = {}
    offset = 0
    while True:
        page = store.list_chunks(
            ChunkFilter(profile_id=_PROFILE_ID),
            Page(offset=offset, limit=page_size),
        )
        items = list(page.items)
        for chunk in items:
            by_id.setdefault(chunk.chunk_id, chunk)
        offset += len(items)
        if not items or offset >= int(page.total):
            return [by_id[chunk_id] for chunk_id in sorted(by_id)]


def _corpus_totals(
    chunks: Sequence[ChunkStamp],
    epochs: Sequence[float],
    cuts: Sequence[float],
) -> dict[str, Any]:
    """Corpus totals read from the copy itself (the ground-truth snapshot)."""
    hosts: Counter[str] = Counter(_chunk_host(chunk) for chunk in chunks)
    bands: Counter[str] = Counter(_recency_label(epoch, cuts) for epoch in epochs)
    return {
        "total_chunks": len(chunks),
        "total_chars": sum(len(chunk.text) for chunk in chunks),
        "hosts": dict(sorted(hosts.items())),
        "recency_bands": dict(sorted(bands.items())),
        "earliest_epoch": min(epochs) if epochs else 0.0,
        "latest_epoch": max(epochs) if epochs else 0.0,
    }


# ---------------------------------------------------------------- home


def _require_manifest_root(home: Path) -> None:
    """Refuse a home that does not carry exactly the corpus-copy manifest (the
    copy set plus an optional config.toml) and a staged embedder cache."""
    present = {entry.name for entry in home.iterdir()}
    unexpected = present - _EXPECTED_HOME_ENTRIES - {"config.toml"}
    missing = _EXPECTED_HOME_ENTRIES - present
    if unexpected or missing:
        raise RuntimeError(
            f"corpus home {home} does not match the copy manifest: missing={missing} unexpected={unexpected}"
        )
    if not (home / "models" / "bge-m3").is_dir():
        raise RuntimeError(f"corpus home {home} carries no embedder cache under models/bge-m3")


def _require_copy_home(home: Path) -> None:
    """Refuse a home that resolves onto the installed runtime: the baseline
    boots a daemon, and only a copy may be written by it."""
    if home.resolve() == _INSTALLED_HOME.resolve():
        raise RuntimeError(f"corpus home {home} is the installed runtime; refusing to boot it")


@contextmanager
def _home_scope(home: Path) -> Iterator[None]:
    """Pin MNEMOSEED_LOCAL_HOME at the corpus copy for the whole run, so no
    step of the baseline resolves against the installed runtime."""
    previous = _env_home()
    set_env_home(home)
    try:
        yield
    finally:
        restore_env_home(previous)


def _stage_models(home: Path) -> dict[str, Any]:
    """Stage the embedder cache into the copy from the installed runtime only
    when the copy carries none (the source is read, never written)."""
    target = home / "models" / "bge-m3"
    cached = target.is_dir() and any(entry.is_file() for entry in target.iterdir())
    source = None if cached else (_INSTALLED_MODEL_DIR if _INSTALLED_MODEL_DIR.is_dir() else None)
    return asdict(stage_model_cache(target, source=source))


# ---------------------------------------------------------------- sampling


def _family_quotas(probes: int) -> dict[str, int]:
    """Per-family probe counts: 30 each, trimmed round-robin to the cap."""
    quotas = dict.fromkeys(_FAMILIES, 0)
    for index in range(max(0, min(probes, _MAX_PROBES))):
        quotas[_FAMILIES[index % len(_FAMILIES)]] += 1
    return quotas


def _stratified_order(
    rng: random.Random,
    chunks: Sequence[ChunkStamp],
    cuts: Sequence[float],
) -> list[ChunkStamp]:
    """A seeded order that visits every (host, recency band) stratum in turn,
    so the sample spreads across hosts and ages."""
    strata: dict[tuple[str, str], list[ChunkStamp]] = {}
    for chunk in chunks:
        key = (_chunk_host(chunk), _recency_label(_chunk_epoch(chunk), cuts))
        strata.setdefault(key, []).append(chunk)
    keys = sorted(strata)
    for key in keys:
        rng.shuffle(strata[key])
    ordered: list[ChunkStamp] = []
    cursor: dict[tuple[str, str], int] = dict.fromkeys(keys, 0)
    while len(ordered) < len(chunks):
        progressed = False
        for key in keys:
            bucket = strata[key]
            index = cursor[key]
            if index >= len(bucket):
                continue
            ordered.append(bucket[index])
            cursor[key] = index + 1
            progressed = True
        if not progressed:
            break
    return ordered


def _family_eligible(family: str, chunk: ChunkStamp) -> bool:
    """Whether a chunk can source a probe of the given family."""
    if len(chunk.text) < _MIN_SOURCE_CHARS:
        return False
    content = _content_tokens(chunk.text)
    if family == "exact":
        return len(content) >= _MIN_EXACT_TOKEN_HITS
    return bool(content)


def _select_sources(
    ordered: Sequence[ChunkStamp],
    quotas: Mapping[str, int],
) -> dict[str, list[ChunkStamp]]:
    """Walk the stratified order once, filling the source quotas in turn (one
    chunk sources at most one probe; the first pick alternates by position)."""
    selected: dict[str, list[ChunkStamp]] = {family: [] for family in _SOURCE_FAMILIES}
    for index, chunk in enumerate(ordered):
        for offset in range(len(_SOURCE_FAMILIES)):
            family = _SOURCE_FAMILIES[(index + offset) % len(_SOURCE_FAMILIES)]
            if len(selected[family]) >= quotas[family]:
                continue
            if _family_eligible(family, chunk):
                selected[family].append(chunk)
                break
    return selected


def _mask_tokens(text: str, tokens: Sequence[str]) -> str:
    """Remove every occurrence of the given tokens from the text."""
    masked = text
    for token in sorted(tokens, key=len, reverse=True):
        masked = re.sub(re.escape(token), " ", masked, flags=re.IGNORECASE)
    return masked


def _fragment(text: str, rng: random.Random, width: int) -> str:
    """A seeded contiguous window of the text, collapsed to one line."""
    collapsed = _WS_RE.sub(" ", text).strip()
    if len(collapsed) <= width:
        return collapsed
    start = rng.randrange(len(collapsed) - width + 1)
    return collapsed[start : start + width].strip(" .,;:，。；：、")


def _excerpt(text: str) -> str:
    """The first ``_EXCERPT_CHARS`` characters of a source text, one line."""
    return _WS_RE.sub(" ", text).strip()[:_EXCERPT_CHARS]


def _paraphrase_query(rng: random.Random, chunk: ChunkStamp) -> tuple[str, int]:
    """A mechanical paraphrase: the source's top content tokens are stripped
    and the residual is framed as a question in the chunk's language. The
    returned hit count is the probe's overlap with the stripped set."""
    stripped = _content_tokens(chunk.text)[:_TOP_CONTENT_TOKENS]
    residual = _fragment(_mask_tokens(chunk.text, stripped), rng, _FRAGMENT_CHARS)
    language = _language(chunk.text)
    if not residual:
        residual = "那个改动" if language == "zh" else "that change"
    query = rng.choice(_PARAPHRASE_TEMPLATES[language]).format(subject=residual)
    return query, len(set(_tokens(query)) & set(stripped))


def _exact_query(rng: random.Random, chunk: ChunkStamp) -> tuple[str, int]:
    """A verbatim source span carrying at least three of its top content
    tokens (the exact-family invariant)."""
    content = _top_content(chunk.text)
    collapsed = _WS_RE.sub(" ", chunk.text).strip()
    if len(collapsed) > _EXACT_WINDOW_CHARS:
        for _ in range(_EXACT_ATTEMPTS):
            start = rng.randrange(len(collapsed) - _EXACT_WINDOW_CHARS + 1)
            window = collapsed[start : start + _EXACT_WINDOW_CHARS].strip()
            hits = len(set(_tokens(window)) & content)
            if hits >= _MIN_EXACT_TOKEN_HITS:
                return window, hits
    return collapsed, len(set(_tokens(collapsed)) & content)


def _probe_for(
    family: str,
    chunk: ChunkStamp,
    rng: random.Random,
    cuts: Sequence[float],
    *,
    ordinal: int,
) -> Probe:
    """One source-backed probe record for the given family."""
    if family == "paraphrase":
        query, hits = _paraphrase_query(rng, chunk)
    else:
        query, hits = _exact_query(rng, chunk)
    return Probe(
        probe_id=f"{family}-{ordinal:03d}",
        family=family,
        source_chunk_id=chunk.chunk_id,
        query=query,
        host_band=_chunk_host(chunk),
        recency_band=_recency_label(_chunk_epoch(chunk), cuts),
        source_excerpt=_excerpt(chunk.text),
        content_token_hits=hits,
    )


def _absent_topics(chunks: Sequence[ChunkStamp]) -> tuple[tuple[str, str], ...]:
    """Canned disjoint topics, preferring the ones whose lexical units the
    corpus never carries. A 20k-chunk dev corpus collides with almost every
    common unit, so zero-collision topics are preferred but not required: the
    disjoint family's honest-empty property is measured empirically per probe
    (served set must be empty), not guaranteed by this pre-check."""
    corpus: set[str] = set()
    for chunk in chunks:
        corpus |= _lexical_units(chunk.text)
    absent = [(lang, text) for lang, text in _DISJOINT_TOPICS if not _lexical_units(text) & corpus]
    if len(absent) >= _MIN_DISJOINT_TOPICS:
        return tuple(absent)
    return tuple(_DISJOINT_TOPICS)


def _disjoint_probes(topics: Sequence[tuple[str, str]], rng: random.Random, count: int) -> list[Probe]:
    """A seeded rotation over the absent topics (every topic is visited before
    any repeat)."""
    start = rng.randrange(len(topics))
    rotation = tuple(topics[(start + index) % len(topics)] for index in range(len(topics)))
    return [
        Probe(
            probe_id=f"disjoint-{ordinal:03d}",
            family="disjoint",
            source_chunk_id=None,
            query=rotation[ordinal % len(rotation)][1],
            host_band=_NO_BAND,
            recency_band=_NO_BAND,
            source_excerpt="",
            content_token_hits=0,
        )
        for ordinal in range(count)
    ]


def _interleave(planned: Mapping[str, Sequence[Probe]]) -> tuple[Probe, ...]:
    """Round-robin the families into the run order."""
    ordered: list[Probe] = []
    total = sum(len(bucket) for bucket in planned.values())
    cursor = dict.fromkeys(_FAMILIES, 0)
    while len(ordered) < total:
        for family in _FAMILIES:
            index = cursor[family]
            bucket = planned[family]
            if index < len(bucket):
                ordered.append(bucket[index])
                cursor[family] = index + 1
    return tuple(ordered)


def _source_of(probe: Probe, chunks: Mapping[str, ChunkStamp]) -> ChunkStamp:
    """The probe's source chunk (source families only)."""
    if probe.source_chunk_id is None:
        raise RuntimeError(f"probe {probe.probe_id} is not source-backed")
    return chunks[probe.source_chunk_id]


def _verify_families(
    planned: Mapping[str, Sequence[Probe]],
    chunks: Mapping[str, ChunkStamp],
) -> dict[str, Any]:
    """Assert the family invariants: a paraphrase probe shares no content
    token with its source, an exact probe carries at least three verbatim
    content-token hits."""
    failures: list[str] = []
    overlaps: list[int] = []
    hits: list[int] = []
    for probe in planned["paraphrase"]:
        overlap = len(set(_tokens(probe.query)) & _top_content(_source_of(probe, chunks).text))
        overlaps.append(overlap)
        if overlap != 0:
            failures.append(f"{probe.probe_id}: paraphrase shares {overlap} source content tokens")
    for probe in planned["exact"]:
        count = len(set(_tokens(probe.query)) & _top_content(_source_of(probe, chunks).text))
        hits.append(count)
        if count < _MIN_EXACT_TOKEN_HITS:
            failures.append(f"{probe.probe_id}: exact carries {count} verbatim content-token hits")
    if failures:
        raise RuntimeError("probe family invariants broken: " + "; ".join(failures))
    return {
        "paraphrase_max_source_overlap": max(overlaps, default=0),
        "exact_min_token_hits": min(hits, default=0),
        "checked": {family: len(planned[family]) for family in _FAMILIES},
    }


def _plan_probes(
    chunks: Sequence[ChunkStamp],
    rng: random.Random,
    cuts: Sequence[float],
    *,
    probes: int,
) -> ProbePlan:
    """The seeded probe plan: quotas, stratified sources, the disjoint
    rotation, the interleaved run order and the verifier evidence."""
    quotas = _family_quotas(probes)
    sources = _select_sources(_stratified_order(rng, chunks, cuts), quotas)
    planned: dict[str, list[Probe]] = {family: [] for family in _FAMILIES}
    for family in _SOURCE_FAMILIES:
        for chunk in sources[family]:
            planned[family].append(_probe_for(family, chunk, rng, cuts, ordinal=len(planned[family])))
    planned["disjoint"] = _disjoint_probes(_absent_topics(chunks), rng, quotas["disjoint"])
    verification = _verify_families(planned, {chunk.chunk_id: chunk for chunk in chunks})
    return ProbePlan(
        probes=_interleave(planned),
        quotas=quotas,
        selected={family: len(planned[family]) for family in _FAMILIES},
        verification=verification,
    )


# ---------------------------------------------------------------- probe run


def _recall(rig: SweepRig, query: str) -> dict[str, Any]:
    """One POST /memory/recall and its ``memory`` payload."""
    response = rig.client.post("/memory/recall", json={"profile_id": _PROFILE_ID, "query": query})
    assert response.status_code == 200, response.text
    payload = cast(dict[str, Any], response.json())
    return cast(dict[str, Any], payload["memory"])


def _served_chunks(memory: Mapping[str, Any]) -> tuple[ServedChunk, ...]:
    """The served chunk entries with their wire scores and flags."""
    served: list[ServedChunk] = []
    for entry in memory["entries"]:
        if entry["kind"] != "chunk":
            continue
        served.append(
            ServedChunk(
                rank=len(served) + 1,
                chunk_id=str(entry["id"]),
                score=float(entry["score"]),
                flags={key: value for key, value in entry.items() if key not in _SERVED_FIELDS},
            )
        )
    return tuple(served)


def _run_probe(rig: SweepRig, probe: Probe) -> ProbeResult:
    """One probe's wire recall on the live daemon plus its latency samples."""
    memory = _recall(rig, probe.query)
    served = _served_chunks(memory)
    latencies: list[float] = []
    for _ in range(_LATENCY_SAMPLES):
        start = time.perf_counter()
        _recall(rig, probe.query)
        latencies.append((time.perf_counter() - start) * 1000.0)
    return ProbeResult(
        probe_id=probe.probe_id,
        family=probe.family,
        source_chunk_id=probe.source_chunk_id,
        query=probe.query,
        host_band=probe.host_band,
        recency_band=probe.recency_band,
        served=served,
        served_count=len(served),
        tokens_used=int(memory["tokens_used"]),
        dropped_count=int(memory["dropped_count"]),
        budget_tokens=int(memory["budget_tokens"]),
        coverage=dict(memory["coverage"]),
        latency_samples_ms=tuple(latencies),
    )


# ---------------------------------------------------------------- metrics


def _hit_rank(result: ProbeResult) -> int | None:
    """The served rank of the probe's source within the top-k window."""
    if result.source_chunk_id is None:
        return None
    for entry in result.served[:_TOP_K]:
        if entry.chunk_id == result.source_chunk_id:
            return entry.rank
    return None


def _dedup_sibling(
    result: ProbeResult,
    source: ChunkStamp | None,
    chunks: Mapping[str, ChunkStamp],
) -> dict[str, Any] | None:
    """The first served chunk that is a byte-level or near-duplicate sibling of
    the source (the dedup-collision evidence for a miss)."""
    if source is None:
        return None
    source_tokens = set(_tokens(source.text))
    for entry in result.served:
        candidate = chunks.get(entry.chunk_id)
        if candidate is None:
            continue
        if candidate.text == source.text:
            return {
                "served_chunk_id": entry.chunk_id,
                "rank": entry.rank,
                "kind": "byte_identical",
                "jaccard": 1.0,
            }
        similarity = _jaccard(source_tokens, set(_tokens(candidate.text)))
        if similarity >= _NEAR_DUPLICATE_JACCARD:
            return {
                "served_chunk_id": entry.chunk_id,
                "rank": entry.rank,
                "kind": "near_duplicate",
                "jaccard": similarity,
            }
    return None


def _family_metrics(
    family: str,
    results: Sequence[ProbeResult],
    chunks: Mapping[str, ChunkStamp],
) -> dict[str, Any]:
    """One family's hit-rate, rank distribution, dedup collisions, budget and
    served-count statistics."""
    ranks: Counter[int] = Counter()
    served_counts: Counter[int] = Counter()
    collisions: list[dict[str, Any]] = []
    misses = 0
    for result in results:
        served_counts[result.served_count] += 1
        rank = _hit_rank(result)
        if rank is not None:
            ranks[rank] += 1
            continue
        if result.source_chunk_id is None:
            continue
        misses += 1
        sibling = _dedup_sibling(result, chunks.get(result.source_chunk_id), chunks)
        if sibling is not None:
            collisions.append({"probe_id": result.probe_id, **sibling})
    source_probes = sum(1 for result in results if result.source_chunk_id is not None)
    hits = sum(ranks.values())
    floor_dropped = _histogram([result.coverage.get("floor_dropped") for result in results])
    latency = _latency_stats([sample for result in results for sample in result.latency_samples_ms])
    return {
        "probes": len(results),
        "source_probes": source_probes,
        "hits_at_8": hits,
        "hit_rate_at_8": hits / source_probes if source_probes else None,
        "hit_rank_distribution": {str(rank): count for rank, count in sorted(ranks.items())},
        "misses": misses,
        "dedup_collisions": collisions,
        "dedup_collision_rate": len(collisions) / misses if misses else None,
        "median_served_count": _median([float(result.served_count) for result in results]),
        "median_tokens_used": _median([float(result.tokens_used) for result in results]),
        "served_count_distribution": {str(count): n for count, n in sorted(served_counts.items())},
        "honest_empty_rate": (
            served_counts.get(0, 0) / len(results) if family == "disjoint" and results else None
        ),
        "graph_hits": sum(int(result.coverage.get("graph_hits", 0)) for result in results),
        "floor_dropped_histogram": floor_dropped,
        "aggregate_latency": latency,
    }


def _aggregate_metrics(
    results: Sequence[ProbeResult],
    families: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The run-level roll-up over the per-family metrics."""
    hits = sum(int(families[family]["hits_at_8"]) for family in _SOURCE_FAMILIES)
    source_probes = sum(int(families[family]["source_probes"]) for family in _SOURCE_FAMILIES)
    floor_dropped = _histogram([result.coverage.get("floor_dropped") for result in results])
    latency = _latency_stats([sample for result in results for sample in result.latency_samples_ms])
    return {
        "probes": len(results),
        "hit_rate_at_8": hits / source_probes if source_probes else None,
        "total_graph_hits": sum(int(families[family]["graph_hits"]) for family in _FAMILIES),
        "median_served_count": _median([float(result.served_count) for result in results]),
        "median_tokens_used": _median([float(result.tokens_used) for result in results]),
        "floor_dropped_histogram": floor_dropped,
        "aggregate_latency": latency,
    }


# ---------------------------------------------------------------- run


def run_baseline(home: Path, *, probes: int = 90, seed: int = 20261127) -> dict[str, Any]:
    """One baseline pass over a corpus-copy home: seeded probe sampling, the
    real daemon's wire recall per probe, and the family metrics."""
    _require_manifest_root(home)
    _require_copy_home(home)
    staging = _stage_models(home)
    (home / "config.toml").write_text(
        _config_toml(home, f"http://{_LOOPBACK_HOST}:{_spare_loopback_port()}"),
        encoding="utf-8",
    )
    rng = random.Random(seed)
    with _home_scope(home), SweepRig(home) as rig:
        store = cast(LanceDbEmbeddedStore, rig.state.stores.vector)
        chunks = _page_all(store)
        epochs = [_chunk_epoch(chunk) for chunk in chunks]
        cuts = _recency_cuts(epochs)
        corpus = _corpus_totals(chunks, epochs, cuts)
        plan = _plan_probes(chunks, rng, cuts, probes=probes)
        results = [_run_probe(rig, probe) for probe in plan.probes]
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    families: dict[str, dict[str, Any]] = {}
    for family in _FAMILIES:
        family_results = [result for result in results if result.family == family]
        families[family] = _family_metrics(family, family_results, by_id)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "home": str(home),
        "profile_id": _PROFILE_ID,
        "seed": seed,
        "probes_requested": probes,
        "probes_run": len(results),
        "family_quotas": plan.quotas,
        "family_selected": plan.selected,
        "model_staging": staging,
        "corpus": corpus,
        "verification": plan.verification,
        "families": families,
        "aggregate": _aggregate_metrics(results, families),
        "probes": [asdict(result) for result in results],
        "disclosures": list(_DISCLOSURES),
    }


# ---------------------------------------------------------------- report


def _render_markdown(record: Mapping[str, Any]) -> str:
    """The human-readable baseline summary (JSON carries the full detail)."""
    lines: list[str] = []
    append = lines.append
    staging = record["model_staging"]
    corpus = record["corpus"]
    verification = record["verification"]
    append("# Corpus baseline record")
    append("")
    append(f"- generated: {record['generated_at']}")
    append(f"- home: {record['home']}")
    append(f"- profile: {record['profile_id']} (seed {record['seed']})")
    append(f"- probes: {record['probes_run']} of {record['probes_requested']} requested")
    append(f"- family quotas: {record['family_quotas']} selected: {record['family_selected']}")
    append(f"- model staging: mode={staging['mode']} source={staging.get('source')}")
    append("")
    append("## Corpus totals (read from the copy before the first probe)")
    append("")
    append(f"- chunks: {corpus['total_chunks']} ({corpus['total_chars']} chars)")
    append(f"- hosts: {corpus['hosts']}")
    append(f"- recency bands: {corpus['recency_bands']}")
    append(f"- epoch range: {corpus['earliest_epoch']} .. {corpus['latest_epoch']}")
    append("")
    append("## Probe family invariants")
    append("")
    append(f"- paraphrase max source-content overlap: {verification['paraphrase_max_source_overlap']}")
    append(f"- exact min verbatim content-token hits: {verification['exact_min_token_hits']}")
    append(f"- checked: {verification['checked']}")
    append("")
    append("## Per-family metrics")
    append("")
    for family, metrics in record["families"].items():
        append(f"### {family} ({metrics['probes']} probes)")
        append("")
        append(
            f"- hit-rate@8: {metrics['hit_rate_at_8']} ({metrics['hits_at_8']}/{metrics['source_probes']})"
        )
        append(f"- hit rank distribution: {metrics['hit_rank_distribution']}")
        append(f"- misses: {metrics['misses']}, dedup-collision rate: {metrics['dedup_collision_rate']}")
        append(
            f"- median served: {metrics['median_served_count']},"
            f" median tokens: {metrics['median_tokens_used']}"
        )
        append(f"- served-count distribution: {metrics['served_count_distribution']}")
        append(f"- honest-empty rate: {metrics['honest_empty_rate']}")
        append(f"- graph hits: {metrics['graph_hits']}, floor dropped: {metrics['floor_dropped_histogram']}")
        append(f"- latency: {metrics['aggregate_latency']}")
        for collision in metrics["dedup_collisions"]:
            append(f"  - dedup collision {collision}")
        append("")
    append("## Aggregate")
    append("")
    for key, value in record["aggregate"].items():
        append(f"- {key}: {value}")
    append("")
    append("## Disclosures")
    append("")
    for disclosure in record["disclosures"]:
        append(f"- {disclosure}")
    append("")
    append("## Probes (run order)")
    append("")
    append("| order | probe | family | host | recency | served | tokens | source |")
    append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, probe in enumerate(record["probes"], start=1):
        append(
            f"| {index} | {probe['probe_id']} | {probe['family']} | {probe['host_band']}"
            f" | {probe['recency_band']} | {probe['served_count']} | {probe['tokens_used']}"
            f" | {probe['source_chunk_id']} |"
        )
    append("")
    return "\n".join(lines)


def write_report(record: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write the JSON record and the Markdown summary (timestamped names, the
    eval artifact convention: <UTC-timestamp>-corpus-baseline.{json,md})."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{record['generated_at']}-corpus-baseline"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(record), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seeded corpus baseline (eval only)")
    parser.add_argument(
        "--home",
        type=Path,
        required=True,
        help="corpus-copy home (never the installed runtime)",
    )
    parser.add_argument("--out", type=Path, default=Path("eval"), help="report output dir")
    parser.add_argument("--probes", type=int, default=90, help="probe cap across the families")
    parser.add_argument("--seed", type=int, default=20261127, help="sampling seed (recorded)")
    args = parser.parse_args(argv)
    record = run_baseline(args.home, probes=args.probes, seed=args.seed)
    json_path, md_path = write_report(record, args.out)
    print(f"baseline record: {json_path}")
    print(f"summary: {md_path}")
    print(f"probes run: {record['probes_run']} (seed {record['seed']})")
    for family, metrics in record["families"].items():
        print(
            f"{family}: probes={metrics['probes']} hit-rate@8={metrics['hit_rate_at_8']}"
            f" median-served={metrics['median_served_count']}"
            f" median-tokens={metrics['median_tokens_used']}"
            f" dedup-collision-rate={metrics['dedup_collision_rate']}"
        )
    print(f"aggregate latency: {record['aggregate']['aggregate_latency']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
