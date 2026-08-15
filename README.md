# MnemoSeed Local

**A local, single-user AI memory layer for coding agents.**

MnemoSeed Local is the local-first edition of MnemoSeed: one profile
(`default`), one machine, CLI-first. No accounts, no console, no cloud
defaults. The core loop is capture -> dream --once -> decay -> retrieve, with
dream inference running against a local model (ollama by default, with an
OpenAI-compatible fallback driver).

Everything is local-first: chunks are stored verbatim, history is
append-only, and memory plaintext never leaves the machine.

## Status

Phase A (MVP core) shipped: config + secrets + storage ports + embedded
drivers (sqlite_meta / sqlite_graph / lancedb_embedded / bge_m3_onnx /
synthetic_embedder), schema, migrations, capture/retrieve/dream/decay
pipelines with a config-driven dream scheduler (pool-score floor + idle window
+ 24h hard deadline), no-accounts loopback daemon, and the `mnemoseed-local`
CLI. Install script + MCP gateway + packaging polish land in Phase A3.

## Development

Test-driven, with an adversarial verifier on every task: failing tests first.
Gates: `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy src`.

## License

AGPL-3.0.
