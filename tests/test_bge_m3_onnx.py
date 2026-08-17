"""BgeM3OnnxEmbedder smoke test (prd-08 FR-8.3 / FR-8.7).

Real local inference is exercised only when the model is already present in the
model cache; otherwise the test skips. It never triggers a download — CI and
model-less machines must stay offline and fast. The model download itself was
proven working under the local environment (see the M0 delivery report).
"""

import math
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from mnemoseed_local.storage.drivers import bge_m3_onnx
from mnemoseed_local.storage.drivers.bge_m3_onnx import BgeM3OnnxEmbedder
from mnemoseed_local.storage.ports import Capability, EmbeddingResult
from mnemoseed_local.storage.registry import EMBED_DRIVERS, register

_MODEL_DIMENSION = 1024


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not EMBED_DRIVERS.contains("bge_m3_onnx"):
        register(EMBED_DRIVERS)(BgeM3OnnxEmbedder)
    yield


def _make_embedder() -> BgeM3OnnxEmbedder:
    return BgeM3OnnxEmbedder()


def test_registered_in_shared_registry():
    assert EMBED_DRIVERS.contains("bge_m3_onnx")


def test_capabilities_declared():
    caps = BgeM3OnnxEmbedder.info.capabilities
    assert Capability.EMBED_LOCAL_INFERENCE in caps
    assert Capability.EMBED_BATCH in caps
    assert Capability.EMBED_SPARSE_OUTPUT in caps


def test_dimension_constant():
    assert BgeM3OnnxEmbedder.info.description
    assert _MODEL_DIMENSION == 1024


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)))


def test_real_embedding_smoke():
    embedder = _make_embedder()
    if not embedder.model_path.exists():
        pytest.skip("bge-m3 model absent locally (skipping real-inference smoke test)")
    related_a = "The horse galloped across the meadow at dawn."
    related_b = "A horse ran through the field in the early morning light."
    unrelated = "The quarterly sales report was finalized by the finance team."

    result_a = embedder.embed(related_a)
    result_b = embedder.embed(related_b)
    result_u = embedder.embed(unrelated)

    assert isinstance(result_a, EmbeddingResult)
    assert len(result_a.dense) == _MODEL_DIMENSION
    norm = math.sqrt(sum(v * v for v in result_a.dense))
    assert norm == pytest.approx(1.0, abs=1e-2)

    assert result_a.sparse is not None
    assert len(result_a.sparse.indices) == len(result_a.sparse.values)
    assert all(index >= 0 for index in result_a.sparse.indices)
    assert all(value > 0.0 for value in result_a.sparse.values)

    related_sim = _cosine(result_a.dense, result_b.dense)
    unrelated_sim = _cosine(result_a.dense, result_u.dense)
    assert related_sim > unrelated_sim + 0.1, (
        f"related cohesion expected, got related={related_sim:.3f} unrelated={unrelated_sim:.3f}"
    )

    shared_sparse = set(result_a.sparse.indices) & set(result_b.sparse.indices)
    assert shared_sparse, "related sentences share sparse indices"


def test_embed_batch_matches_single_embeds():
    embedder = _make_embedder()
    if not embedder.model_path.exists():
        pytest.skip("bge-m3 model absent locally (skipping batch consistency test)")
    texts = ["first sentence for batch", "second sentence for batch"]
    batch = embedder.embed_batch(texts)
    assert len(batch) == 2
    for text, result in zip(texts, batch, strict=True):
        assert len(result.dense) == _MODEL_DIMENSION
        solo = embedder.embed(text)
        # batched inference pads the sequence; the quantized XLM-R graph does
        # not perfectly mask padded positions, so batch and solo embeddings
        # agree to ~0.98 cosine, not to the last bit. The sparse token scores
        # are unchanged (same token ids, same projection).
        assert _cosine(result.dense, solo.dense) > 0.95
        assert result.sparse is not None and solo.sparse is not None
        assert result.sparse.indices == solo.sparse.indices  # token identities never change
        for batch_value, solo_value in zip(result.sparse.values, solo.sparse.values, strict=True):
            scale = max(abs(batch_value), abs(solo_value))
            assert abs(batch_value - solo_value) <= max(3e-2, 0.4 * scale)


# ------------------------------------------------------------ model bootstrap (PRD-06 FR-6.2)
#
# First-run model bootstrap is pinned against a local HTTP server with RFC 7233
# Range support. No test here ever touches the real Hugging Face endpoint.


class _ArtifactHandler(BaseHTTPRequestHandler):
    """Minimal artifact server: in-memory payloads with RFC 7233 Range support
    (206 with Content-Range, 416 when the range starts at the full size)."""

    artifacts: dict[str, bytes] = {}
    requests: list[str] = []
    ranges: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:
        name = urlparse(self.path).path.lstrip("/")
        _ArtifactHandler.requests.append(name)
        _ArtifactHandler.ranges.append((name, self.headers.get("Range")))
        data = _ArtifactHandler.artifacts.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else len(data) - 1
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.end_headers()
                    return
                end = min(end, len(data) - 1)
                body = data[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def artifact_server():
    _ArtifactHandler.artifacts = {}
    _ArtifactHandler.requests = []
    _ArtifactHandler.ranges = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArtifactHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _local_url(server: ThreadingHTTPServer, monkeypatch) -> None:
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(bge_m3_onnx, "_model_url", lambda repo, filename: f"{base}/{filename}")


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_bootstrap_requests_repo_relative_artifact_names(tmp_path, monkeypatch) -> None:
    """Wire contract: each artifact is fetched under its repo-relative path.

    The Xenova encoder lives at ``onnx/model_quantized.onnx`` on Hugging Face;
    requesting the bare basename 404s (found by live drain probing: every
    embedding call failed at the lazy bootstrap until this was fixed). The
    local cache directory still keys files by basename (resume layout is
    unchanged).
    """
    requested: list[tuple[str, str]] = []

    def _fake_url(repo: str, filename: str) -> str:
        requested.append((repo, filename))
        return "http://127.0.0.1:1/unroutable"

    monkeypatch.setattr(bge_m3_onnx, "_model_url", _fake_url)
    embedder = BgeM3OnnxEmbedder(model_dir=tmp_path / "models")

    with pytest.raises(bge_m3_onnx.ModelDownloadError):
        embedder.ensure_downloaded()

    # stops at the first failing artifact — and that artifact must already be
    # addressed by its repo-relative name, not the flattened basename
    assert requested == [("Xenova/bge-m3", "tokenizer.json")]

    def _seed_complete(name: str) -> None:
        # a cached file carrying no sidecars counts as a legacy complete
        # download (the failed fetches above left .partial markers behind)
        path = tmp_path / "models" / name
        path.write_bytes(b"cached")
        path.with_name(name + ".partial").unlink(missing_ok=True)

    requested.clear()
    _seed_complete("tokenizer.json")
    with pytest.raises(bge_m3_onnx.ModelDownloadError):
        embedder.ensure_downloaded()
    assert requested == [("Xenova/bge-m3", "onnx/model_quantized.onnx")]

    requested.clear()
    _seed_complete("model_quantized.onnx")
    with pytest.raises(bge_m3_onnx.ModelDownloadError):
        embedder.ensure_downloaded()
    assert requested == [("BAAI/bge-m3", "sparse_linear.pt")]


def test_bootstrap_fresh_download_from_local_server(artifact_server, tmp_path, monkeypatch) -> None:
    artifacts = {
        "tokenizer.json": b'{"tokenizer": true}',
        # repo-relative path: the quantized encoder lives under the repo's
        # onnx/ subdirectory on Hugging Face, never at the repo root
        "onnx/model_quantized.onnx": b"model-bytes-" * 4096,
        "sparse_linear.pt": b"sparse-state",
    }
    _ArtifactHandler.artifacts = artifacts
    _local_url(artifact_server, monkeypatch)
    progress: list[tuple[int, int]] = []
    embedder = BgeM3OnnxEmbedder(model_dir=tmp_path / "models", progress=lambda d, t: progress.append((d, t)))

    embedder.ensure_downloaded()

    for name, payload in artifacts.items():
        # the local cache keeps every artifact at the top level, keyed by its
        # basename regardless of the repo-relative path it was fetched from
        local_name = name.rsplit("/", 1)[-1]
        path = tmp_path / "models" / local_name
        assert path.read_bytes() == payload
        assert (tmp_path / "models" / (local_name + ".complete")).exists()
        assert (tmp_path / "models" / (local_name + ".partial")).exists() is False
    model = artifacts["onnx/model_quantized.onnx"]
    assert (len(model), len(model)) in progress, "progress callback must report the full model size"

    # complete artifacts skip any re-request on the next boot
    requests_seen = len(_ArtifactHandler.requests)
    embedder.ensure_downloaded()
    assert len(_ArtifactHandler.requests) == requests_seen


def test_bootstrap_resumes_interrupted_download(artifact_server, tmp_path, monkeypatch) -> None:
    """A partial file left by a killed first run (interrupted after 64 bytes,
    carrying the ``.partial`` sidecar the driver records while a download is in
    flight) is completed over a Range request on the retry."""
    payload = b"model-bytes-" * 8192
    _ArtifactHandler.artifacts = {
        "tokenizer.json": b'{"tokenizer": true}',
        "onnx/model_quantized.onnx": payload,
        "sparse_linear.pt": b"sparse-state",
    }
    _local_url(artifact_server, monkeypatch)
    parts = tmp_path / "models"
    parts.mkdir(parents=True)
    partial = parts / "model_quantized.onnx"
    partial.write_bytes(payload[:64])
    (parts / "model_quantized.onnx.partial").write_text("64", encoding="utf-8")

    embedder = BgeM3OnnxEmbedder(model_dir=tmp_path / "models")
    embedder.ensure_downloaded()

    assert partial.read_bytes() == payload
    assert (parts / "model_quantized.onnx.complete").exists()
    assert ("onnx/model_quantized.onnx", "bytes=64-") in _ArtifactHandler.ranges


def test_bootstrap_complete_file_without_marker_is_not_redownloaded(
    artifact_server, tmp_path, monkeypatch
) -> None:
    """A pre-marker (legacy) complete file is honored offline as-is: the boot
    never re-requests it, so a machine with the model cached boots with zero
    network traffic."""
    payload = b"model-bytes-" * 4096
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "model_quantized.onnx").write_bytes(payload)
    _ArtifactHandler.artifacts = {
        "tokenizer.json": b'{"tokenizer": true}',
        "onnx/model_quantized.onnx": payload,
        "sparse_linear.pt": b"sparse-state",
    }
    _local_url(artifact_server, monkeypatch)
    embedder = BgeM3OnnxEmbedder(model_dir=tmp_path / "models")

    embedder.ensure_downloaded()

    assert (tmp_path / "models" / "model_quantized.onnx").read_bytes() == payload
    assert "model_quantized.onnx" not in _ArtifactHandler.requests


def test_bootstrap_offline_error_is_typed_and_actionable(tmp_path, monkeypatch) -> None:
    """Model missing + no network: the driver raises a typed, actionable error
    naming the exact local path and the retry command — never a bare transport
    traceback."""
    closed = _closed_port()
    monkeypatch.setattr(
        bge_m3_onnx, "_model_url", lambda repo, filename: f"http://127.0.0.1:{closed}/{filename}"
    )
    embedder = BgeM3OnnxEmbedder(model_dir=tmp_path / "models")

    with pytest.raises(bge_m3_onnx.ModelDownloadError) as excinfo:
        embedder.ensure_downloaded()

    message = str(excinfo.value)
    assert "tokenizer.json" in message  # first artifact being fetched
    assert str(tmp_path / "models") in message  # exact local path
    assert "mnemoseed-local up" in message  # retry command
