"""Drift guard: the shipped agent skill's verb manifest equals the gateway's tool surface."""

import re
from pathlib import Path

from mnemoseed_local.mcp_gateway.server import TOOLS

SKILL_PATH = Path(__file__).parents[1] / "skills" / "mnemoseed" / "SKILL.md"


def test_skill_verb_manifest_matches_gateway_tools() -> None:
    assert SKILL_PATH.exists(), f"missing skill artifact: {SKILL_PATH}"
    manifest_lines = [
        line for line in SKILL_PATH.read_text(encoding="utf-8").splitlines() if line.startswith("Verbs:")
    ]
    assert len(manifest_lines) == 1, f"expected one 'Verbs:' manifest line, got {len(manifest_lines)}"
    manifest_verbs = set(re.findall(r"`([^`]+)`", manifest_lines[0]))
    gateway_verbs = {tool["name"] for tool in TOOLS}
    assert manifest_verbs == gateway_verbs, (
        f"skill-only={sorted(manifest_verbs - gateway_verbs)} "
        f"gateway-only={sorted(gateway_verbs - manifest_verbs)}"
    )
