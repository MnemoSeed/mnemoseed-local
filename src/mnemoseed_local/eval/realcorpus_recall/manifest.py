"""Provenance pins for the real-corpus recall evaluation surface."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILENAME = "corpus_manifest.sha256"


class InvalidRun(Exception):
    """A run whose pinned inputs drifted; never a PASS or a FAIL."""


@dataclass(frozen=True)
class CorpusManifest:
    corpus_manifest_sha256: str
    config_sha256: str
    embedder_model_version: str
    code_revision: str

    def to_dict(self) -> dict[str, str]:
        return {
            "corpus_manifest_sha256": self.corpus_manifest_sha256,
            "config_sha256": self.config_sha256,
            "embedder_model_version": self.embedder_model_version,
            "code_revision": self.code_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> CorpusManifest:
        return cls(**data)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def corpus_digest(root: Path) -> str:
    """Content hash over every file, keyed by relative path and size."""
    entries = sorted(p for p in root.rglob("*") if p.is_file())
    aggregate = hashlib.sha256()
    for path in entries:
        relative = path.relative_to(root).as_posix()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(path.stat().st_size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(_sha256_file(path).encode("ascii"))
        aggregate.update(b"\n")
    return aggregate.hexdigest()


_EMBED_BY_PRESET = {"embedded": "bge_m3_onnx"}


def _embedder_version(root: Path) -> str:
    config = root / "config.toml"
    driver = "unknown"
    if config.exists():
        document = tomllib.loads(config.read_text(encoding="utf-8", errors="replace"))
        preset = str(document.get("preset", ""))
        driver = str(
            document.get("storage", {})
            .get("embed", {})
            .get("driver", _EMBED_BY_PRESET.get(preset, "unknown"))
        )
    models = root / "models"
    model_part = corpus_digest(models)[:16] if models.is_dir() else "none"
    return f"{driver}@{model_part}"


def _code_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return result.stdout.strip()


def compute_corpus_manifest(
    root: Path,
    *,
    code_revision: str | None = None,
    embedder_version: str | None = None,
) -> CorpusManifest:
    config = root / "config.toml"
    return CorpusManifest(
        corpus_manifest_sha256=corpus_digest(root),
        config_sha256=_sha256_file(config) if config.exists() else "",
        embedder_model_version=embedder_version or _embedder_version(root),
        code_revision=code_revision if code_revision is not None else _code_revision(),
    )


def write_manifest(manifest: CorpusManifest, path: Path) -> None:
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> CorpusManifest:
    return CorpusManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


def verify_manifest(actual: CorpusManifest, expected: CorpusManifest) -> None:
    if actual != expected:
        raise InvalidRun("corpus manifest drift")
