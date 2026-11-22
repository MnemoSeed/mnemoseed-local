"""Measured corpus copy and immutable pristine baseline."""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mnemoseed_local.eval.realcorpus_recall.manifest import (
    CorpusManifest,
    compute_corpus_manifest,
    corpus_digest,
)


def default_temp_root() -> Path:
    preferred = Path("C:/Users/Little Star/AppData/Local/Temp/opencode")
    if preferred.is_dir():
        return preferred
    return Path(tempfile.gettempdir()) / "opencode"


def directory_digest(root: Path) -> str:
    return corpus_digest(root)


@dataclass(frozen=True)
class CorpusCopy:
    source_home: Path
    home: Path
    file_count: int
    total_bytes: int
    elapsed_seconds: float
    manifest: CorpusManifest


def copy_corpus(
    source_home: Path,
    *,
    temp_root: Path | None = None,
    code_revision: str | None = None,
    embedder_version: str | None = None,
) -> CorpusCopy:
    root = temp_root or default_temp_root()
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="s0-recall-corpus-", dir=root))
    home = temp_dir / "corpus"
    started = time.perf_counter()
    shutil.copytree(source_home, home, copy_function=shutil.copy2)
    elapsed = time.perf_counter() - started
    files = [p for p in home.rglob("*") if p.is_file()]
    manifest = compute_corpus_manifest(
        home,
        code_revision=code_revision,
        embedder_version=embedder_version,
    )
    return CorpusCopy(
        source_home=source_home,
        home=home,
        file_count=len(files),
        total_bytes=sum(p.stat().st_size for p in files),
        elapsed_seconds=elapsed,
        manifest=manifest,
    )


def snapshot_pristine(home: Path, pristine_root: Path) -> Path:
    pristine_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(home, pristine_root, dirs_exist_ok=True, copy_function=shutil.copy2)
    return pristine_root


def restore_writable_state(pristine: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(target.rglob("*"), reverse=True):
        if not path.is_file():
            continue
        relative = path.relative_to(target)
        if not (pristine / relative).is_file():
            path.unlink()
    for path in sorted(p for p in pristine.rglob("*") if p.is_file()):
        relative = path.relative_to(pristine)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
