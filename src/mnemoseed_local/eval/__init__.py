"""Eval harness (B3): canary factory, scratch rig, metrics/report, matrix runner.

Not a product surface: no CLI verb, no daemon endpoint. Unit tests drive the
whole harness over the deterministic stub seats; live ollama matrix runs are a
manual action via ``uv run python -m mnemoseed_local.eval``.
"""
