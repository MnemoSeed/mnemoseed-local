"""ConfigWriteService (PRD-07 FR-7.11 / design/07 section 9, W1.1).

The daemon's single config writer: key-path registry -> validate -> surgical
TOML patch -> versioned meta-store record -> audit (actor attributed) ->
live-apply. The REST surface (configwrite.routes) rides the daemon and is not
part of the local MVP substrate; the service itself is ported as-is.
"""

from mnemoseed_local.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)

__all__ = ["CONFIG_KEY_REGISTRY", "ConfigWriteError", "ConfigWriteService"]
