"""BGE-M3 embedder over ONNX Runtime (embedded default, prd-08 FR-8.3).

The model is not shipped with the package: the first instantiation downloads the
quantized XLM-R encoder ONNX model, the XLM-R tokenizer, and the tiny BGE-M3
sparse projection from Hugging Face (public, not gated) into the model cache
directory. Downloads are resumable (HTTP Range) and emit a progress callback so
a slow first boot is visible, never silent.

Inference reproduces the BGE-M3 heads on top of the encoder hidden states:
- dense: L2-normalized CLS token embedding (1024 dim);
- sparse: per-token scalar weight from the sparse projection, indexed by the
  token id, aggregated per id (prd-08 appendix A.1 structured sparse vector).
"""

from __future__ import annotations

import io
import logging
import os
import pickle
import threading
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.storage.ports import (
    Capability,
    DriverInfo,
    EmbeddingResult,
    SparseVector,
)
from mnemoseed_local.storage.registry import EMBED_DRIVERS, register

logger = logging.getLogger(__name__)

_CAPABILITIES = frozenset(
    {
        Capability.EMBED_LOCAL_INFERENCE,
        Capability.EMBED_BATCH,
        Capability.EMBED_SPARSE_OUTPUT,
    }
)

_ENCODER_REPO = "Xenova/bge-m3"
_SPARSE_REPO = "BAAI/bge-m3"
_ENCODER_MODEL = "onnx/model_quantized.onnx"
_ENCODER_TOKENIZER = "tokenizer.json"
_SPARSE_PROJECTION = "sparse_linear.pt"
_DEFAULT_DIMENSION = 1024
_CHUNK_BYTES = 1 << 20

ProgressCallback = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """A bge-m3 artifact could not be downloaded.

    Raised exactly where a bare httpx transport error would otherwise surface;
    the message names the exact local path and the retry action so a failing
    first boot is actionable instead of a traceback.
    """


def _model_url(repo: str, filename: str) -> str:
    """Hugging Face resolve URL for one model-file artifact."""
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


# torch storage classes -> numpy dtypes (torch-free state-dict reading)
_STORAGE_DTYPES: dict[str, np.dtype] = {
    "FloatStorage": np.dtype("float32"),
    "DoubleStorage": np.dtype("float64"),
    "HalfStorage": np.dtype("float16"),
    "LongStorage": np.dtype("int64"),
    "IntStorage": np.dtype("int32"),
    "ShortStorage": np.dtype("int16"),
    "CharStorage": np.dtype("int8"),
    "BoolStorage": np.dtype("bool"),
}


@register(EMBED_DRIVERS)
class BgeM3OnnxEmbedder:
    """BGE-M3 (XLM-RoBERTa backbone) via ONNX Runtime on CPU."""

    info = DriverInfo(
        name="bge_m3_onnx",
        capabilities=_CAPABILITIES,
        description="BGE-M3 dense+sparse embeddings via ONNX Runtime (embedded default)",
    )

    def __init__(
        self,
        model_dir: str | os.PathLike[str] | None = None,
        encoder_repo: str = _ENCODER_REPO,
        encoder_model: str = _ENCODER_MODEL,
        encoder_tokenizer: str = _ENCODER_TOKENIZER,
        sparse_repo: str = _SPARSE_REPO,
        sparse_filename: str = _SPARSE_PROJECTION,
        dimension: int = _DEFAULT_DIMENSION,
        max_length: int = 8192,
        progress: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> None:
        self.params: dict[str, Any] = kwargs
        self.dimension = dimension
        self.max_length = max_length
        default_dir = CONFIG_DIR / "models" / "bge-m3"
        self._dir = Path(os.path.expanduser(str(model_dir))) if model_dir else default_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._encoder_repo = encoder_repo
        self._sparse_repo = sparse_repo
        self._model_file = self._dir / Path(encoder_model).name
        self._tokenizer_file = self._dir / Path(encoder_tokenizer).name
        self._sparse_file = self._dir / Path(sparse_filename).name
        self._progress = progress or _log_progress
        self._load_lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._sparse_weight: np.ndarray | None = None
        self._sparse_bias: np.ndarray | None = None

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    @property
    def model_path(self) -> Path:
        """Absolute path to the local encoder model file."""
        return self._model_file

    def ensure_downloaded(self) -> None:
        """Download encoder, tokenizer, and sparse projection (idempotent, resumable).

        A file counts as complete only when its ``.complete`` sidecar records
        its byte size. Anything else — absent, empty, or an interrupted partial
        file carrying a ``.partial`` sidecar — is (re)downloaded over HTTP
        Range, resuming from the bytes already on disk. A pre-marker file with
        no sidecar is honored as a legacy complete download, so an offline
        machine with the model cached never needs the network.
        """
        downloads = (
            (self._tokenizer_file.name, self._tokenizer_file, self._encoder_repo),
            (self._model_file.name, self._model_file, self._encoder_repo),
            (self._sparse_file.name, self._sparse_file, self._sparse_repo),
        )
        for filename, dest, repo in downloads:
            if not self._is_complete(dest):
                self._download(repo, filename, dest)

    def _download(self, repo: str, filename: str, dest: Path) -> None:
        url = _model_url(repo, filename)
        existing = dest.stat().st_size if dest.exists() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        self._partial_marker(dest).write_text(str(existing), encoding="utf-8")
        try:
            with httpx.stream(
                "GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60.0)
            ) as response:
                if response.status_code == 416:
                    self._mark_complete(dest)
                    return
                response.raise_for_status()
                resume = response.status_code == 206 and existing > 0
                total = existing + _content_length(response)
                mode = "ab" if resume else "wb"
                written = existing
                with open(dest, mode) as handle:
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        handle.write(chunk)
                        written += len(chunk)
                        self._progress(written, total)
        except httpx.HTTPError as exc:
            raise ModelDownloadError(
                f"could not download {filename} (from {url}) into {dest}. "
                "Check your network connection, then re-run `mnemoseed-local up` to "
                "resume; the partial file is kept."
            ) from exc
        self._mark_complete(dest)
        logger.info("downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / (1 << 20))

    # ------------------------------------------------------ download bookkeeping

    @staticmethod
    def _partial_marker(dest: Path) -> Path:
        """Sidecar recorded while a download is in flight."""
        return dest.with_name(dest.name + ".partial")

    @staticmethod
    def _complete_marker(dest: Path) -> Path:
        """Sidecar recording a finished file's byte size."""
        return dest.with_name(dest.name + ".complete")

    def _is_complete(self, dest: Path) -> bool:
        """True when the file may be used without re-downloading it."""
        if not dest.exists() or dest.stat().st_size == 0:
            return False
        complete = self._complete_marker(dest)
        if complete.exists():
            try:
                return complete.read_text(encoding="utf-8").strip() == str(dest.stat().st_size)
            except OSError:
                return False
        # a legacy pre-marker file counts as complete so an offline boot with
        # the model already cached never touches the network
        return not self._partial_marker(dest).exists()

    def _mark_complete(self, dest: Path) -> None:
        """Close a download: drop the in-flight sidecar, record the final size."""
        self._partial_marker(dest).unlink(missing_ok=True)
        self._complete_marker(dest).write_text(str(dest.stat().st_size), encoding="utf-8")

    def _load(self) -> None:
        if self._session is not None:
            return
        with self._load_lock:
            if self._session is not None:
                return
            self.ensure_downloaded()

            from onnxruntime import InferenceSession, SessionOptions

            options = SessionOptions()
            options.intra_op_num_threads = 2
            session = InferenceSession(
                str(self._model_file), sess_options=options, providers=["CPUExecutionProvider"]
            )
            inputs = {i.name for i in session.get_inputs()}
            if "input_ids" not in inputs or "attention_mask" not in inputs:
                raise RuntimeError(f"unexpected bge-m3 ONNX inputs: {sorted(inputs)}")

            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(self._tokenizer_file))
            weight, bias = _load_torch_linear(self._sparse_file)

            self._session = session
            self._tokenizer = tokenizer
            dim = session.get_outputs()[0].shape[-1]
            if dim not in (None, self.dimension):
                logger.warning("bge-m3 encoder output dim %s differs from configured %d", dim, self.dimension)
            self._sparse_weight = weight.astype(np.float32)
            self._sparse_bias = bias.astype(np.float32)

    def embed(self, text: str) -> EmbeddingResult:
        results = self.embed_batch([text])
        return results[0]

    def embed_batch(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        self._load()
        assert self._session is not None
        input_ids, attention_mask = self._tokenize(texts)
        outputs = self._session.run(None, {"input_ids": input_ids, "attention_mask": attention_mask})
        hidden = np.asarray(outputs[0], dtype=np.float32)  # [B, seq, dim]
        results: list[EmbeddingResult] = []
        for i, _text in enumerate(texts):
            dense = self._dense(hidden[i])
            sparse = self._sparse(hidden[i], input_ids[i]) if self._sparse_weight is not None else None
            results.append(EmbeddingResult(dense=dense, sparse=sparse))
        return results

    # ------------------------------------------------------------ internals

    def _tokenize(self, texts: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
        assert self._tokenizer is not None
        sequences = [self._tokenizer.encode(text, add_special_tokens=True).ids for text in texts]
        max_len = min(max(len(seq) for seq in sequences), self.max_length)
        batch = np.zeros((len(sequences), max_len), dtype=np.int64)
        mask = np.zeros((len(sequences), max_len), dtype=np.int64)
        for i, seq in enumerate(sequences):
            seq = seq[:max_len]
            batch[i, : len(seq)] = seq
            mask[i, : len(seq)] = 1
        return batch, mask

    def _dense(self, hidden: np.ndarray) -> list[float]:
        vector = np.asarray(hidden[0], dtype=np.float32)  # CLS pooling (BGE-M3)
        norm = float(np.linalg.norm(vector)) or 1.0
        return [float(value) for value in vector / norm]

    def _sparse(self, hidden: np.ndarray, token_ids: np.ndarray) -> SparseVector:
        assert self._sparse_weight is not None and self._sparse_bias is not None
        scores = np.maximum(hidden @ self._sparse_weight + self._sparse_bias, 0.0)
        aggregated: dict[int, float] = {}
        for token_id, score in zip(token_ids.tolist(), scores.tolist(), strict=False):
            if token_id <= 2 or score <= 0.0:
                continue
            aggregated[token_id] = max(aggregated.get(token_id, 0.0), score)
        indices = tuple(sorted(aggregated))
        values = tuple(aggregated[index] for index in indices)
        return SparseVector(indices=indices, values=values)


# ---------------------------------------------------------------- torch-free loader


class _Storage:
    """One raw tensor blob lifted from a torch zip archive."""

    def __init__(self, dtype: np.dtype, key: str, blobs: dict[str, bytes]) -> None:
        self.dtype = dtype
        self.key = key
        self.blobs = blobs

    def array(self) -> np.ndarray:
        return np.frombuffer(self.blobs[self.key], dtype=self.dtype)


class _TorchUnpickler(pickle.Unpickler):
    """Unpickles a torch-2.x zip state dict without importing torch."""

    def __init__(self, file: Any, blobs: dict[str, bytes]) -> None:
        super().__init__(file)
        self.blobs = blobs

    def find_class(self, module: str, name: str) -> Any:
        if module == "collections" and name == "OrderedDict":
            return dict
        if module == "torch._utils" and name == "_rebuild_tensor_v2":
            return _rebuild_tensor_v2
        if module == "torch" and name.endswith("Storage"):
            dtype = _STORAGE_DTYPES.get(name)
            if dtype is None:
                raise RuntimeError(f"unsupported torch storage {name}")
            return dtype
        if module == "torch" and name in {"Size", "SymInt"}:
            return int
        return super().find_class(module, name)

    def persistent_load(self, pid: Any) -> Any:
        dtype, key = pid[1], pid[2]
        return _Storage(dtype, key, self.blobs)


def _rebuild_tensor_v2(
    storage: _Storage,
    storage_offset: int,
    size: tuple[int, ...],
    stride: tuple[int, ...] | None = None,
    requires_grad: bool = False,
    backward_hooks: Any = None,
) -> np.ndarray:
    array = storage.array()
    count = int(np.prod(size)) if size else 0
    view = array[storage_offset : storage_offset + count]
    return view.reshape(size) if size else np.asarray([], dtype=storage.dtype)


def _load_torch_linear(path: os.PathLike[str]) -> tuple[np.ndarray, np.ndarray]:
    """Load a torch Linear state dict (weight/bias) as numpy arrays."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        pickle_name = next((n for n in names if n.endswith("/data.pkl")), None)
        if pickle_name is None:
            raise RuntimeError(f"no torch pickle found in {path}")
        prefix = pickle_name[: -len("/data.pkl")]
        blobs = {
            name.rsplit("/", 1)[1]: archive.read(name)
            for name in names
            if name.startswith(f"{prefix}/data/") and name.count("/") == 2
        }
        payload = archive.read(pickle_name)
    state = _TorchUnpickler(io.BytesIO(payload), blobs).load()
    weight = np.asarray(state["weight"], dtype=np.float32).reshape(-1)
    bias = np.asarray(state["bias"], dtype=np.float32).reshape(1)
    return weight, bias


def _content_length(response: httpx.Response) -> int:
    header = response.headers.get("content-range")
    if header and "/" in header:
        try:
            return max(int(header.rsplit("/", 1)[1]), 0)
        except ValueError:
            pass
    return max(int(response.headers.get("content-length", 0)), 0)


def _log_progress(downloaded: int, total: int) -> None:
    if total <= 0:
        return
    percent = downloaded * 100 // total
    if percent % 5 == 0 and downloaded % (5 * _CHUNK_BYTES) < _CHUNK_BYTES:
        logger.info("model download %d%% (%d/%d bytes)", percent, downloaded, total)
