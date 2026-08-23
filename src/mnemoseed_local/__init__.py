"""MnemoSeed Local — local single-user AI memory layer.

Core loop: capture -> dream (automatic by default, ``--once`` manual
fallback) -> decay -> retrieve. Dream
inference runs against a local model (ollama default, openai-compatible
fallback driver kept). No accounts, no console: profile is hardcoded to
``default`` at the application boundary.
"""

__version__ = "0.0.1"


def health() -> bool:
    """Minimal liveness check for the core package."""
    return True
