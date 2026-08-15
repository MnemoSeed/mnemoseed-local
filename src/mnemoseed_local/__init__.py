"""MnemoSeed Local — local single-user AI memory layer.

Core loop: capture -> dream --once (manual) -> decay -> retrieve. Dream
inference runs against a local model (ollama default, openai-compatible
fallback driver kept). No accounts, no console: profile is hardcoded to
``default`` at the application boundary.
"""

__version__ = "0.1.0"


def health() -> bool:
    """Minimal liveness check for the core package."""
    return True
