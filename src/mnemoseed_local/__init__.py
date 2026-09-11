"""MnemoSeed Local — local single-user AI memory layer.

Core loop: capture -> dream (automatic by default, ``--once`` manual
fallback) -> decay -> retrieve. Dream
inference runs against a local model (ollama default, openai-compatible
fallback driver kept). No accounts: profile is hardcoded to ``default``
at the application boundary.
"""


def __dir__() -> list[str]:
    """Include attributes resolvable via ``__getattr__`` in directory listings."""
    names = {k for k in globals().keys() if not k.startswith("_")}
    return sorted(names | {"__doc__", "__version__"})


def __getattr__(name: str) -> str:
    """Resolve ``__version__`` lazily from the installed distribution metadata.

    ``pyproject.toml`` is the single authoring source; packaging writes it into
    the installed metadata, so bumping the project version is one edit. The
    lookup is deferred to first access because ``importlib.metadata`` imports
    ``socket``/``urllib``, which the eval isolation tests forbid on plain
    ``import mnemoseed_local``.
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("mnemoseed-local")
        except PackageNotFoundError:  # pragma: no cover - source tree without an install
            return "0.0.0+unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def health() -> bool:
    """Minimal liveness check for the core package."""
    return True
