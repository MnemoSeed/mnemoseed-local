"""Appendix B method-coverage gate (prd-08 FR-8.5 / AC-3).

Introspects the four storage Protocols and verifies that every public method has
at least one contract test mapped in ``method_mapping`` -- and that every mapped
name really is a protocol method (no phantom entries). A missing method is a
HARD FAILURE per the M0 task AC-3 requirement ("Flag any Appendix B method with
NO test as a hard failure"). Also verifies the reference method counts from
appendix B (12 / 20 / 32 / 3) and that each referenced test actually exists,
then regenerates the checked-in mapping report artifact.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from method_mapping import COVERAGE, EXPECTED_METHOD_COUNTS, PORT_ORDER

from mnemoseed_local.storage.ports import Embedder, GraphStore, MetaStore, VectorStore

_REPORT = Path(__file__).parent / "REPORT-method-mapping.md"
_PROTOCOLS = {
    "VectorStore": VectorStore,
    "GraphStore": GraphStore,
    "MetaStore": MetaStore,
    "Embedder": Embedder,
}


def _protocol_methods(proto: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(proto)
        if inspect.isfunction(member) and not name.startswith("_")
    }


def test_every_appendix_b_method_has_a_contract_test() -> None:
    missing: dict[str, list[str]] = {}
    for layer in PORT_ORDER:
        proto = _PROTOCOLS[layer]
        exposed = _protocol_methods(proto)
        mapped = set(COVERAGE[layer])
        absent = sorted(exposed - mapped)
        if absent:
            missing[layer] = absent
    # HARD FAILURE: any public Appendix B method without a mapped contract test.
    assert not missing, f"Protocol method(s) with no contract test: {missing}"


def test_phantom_entries_are_rejected() -> None:
    phantom: dict[str, list[str]] = {}
    for layer in PORT_ORDER:
        exposed = _protocol_methods(_PROTOCOLS[layer])
        not_methods = sorted(set(COVERAGE[layer]) - exposed)
        if not_methods:
            phantom[layer] = not_methods
    assert not phantom, f"mapped name(s) are not protocol methods: {phantom}"


def test_reference_method_counts_match_appendix_b() -> None:
    for layer in PORT_ORDER:
        exposed = _protocol_methods(_PROTOCOLS[layer])
        assert layer in EXPECTED_METHOD_COUNTS
        assert len(exposed) == EXPECTED_METHOD_COUNTS[layer], (
            f"{layer} exposes {len(exposed)} methods but appendix B defines "
            f"{EXPECTED_METHOD_COUNTS[layer]} -- the protocol changed, update "
            "the contract suite deliberately"
        )
        assert len(COVERAGE[layer]) == EXPECTED_METHOD_COUNTS[layer]


def test_every_mapped_test_exists() -> None:
    broken: list[str] = []
    for layer in PORT_ORDER:
        for method, ref in COVERAGE[layer].items():
            module_name, _, func_name = ref.rpartition("::")
            if module_name.endswith(".py"):
                module_name = module_name[:-3]
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - report the broken ref
                broken.append(f"{layer}.{method} -> {ref} (import failed: {exc})")
                continue
            if not callable(getattr(module, func_name, None)):
                broken.append(f"{layer}.{method} -> {ref} (no such test function)")
    assert not broken, "broken mapping reference(s):\n" + "\n".join(broken)


def test_report_artifact_is_generated() -> None:
    """The mapping table is emitted next to the suite (AC-3 '映射表随测试报告输出')."""
    lines = [
        "# Method -> contract test mapping (prd-08 FR-8.5 / AC-3)",
        "",
        "Generated automatically by `tests/contract/test_contract_coverage.py`. Every",
        "public method of the four storage Protocols has at least one contract test",
        "that runs against the embedded driver family.",
        "",
        "Driver family covered by the `stack` fixture:",
        "",
        "- embedded: lancedb_embedded + sqlite_graph + sqlite_meta + synthetic embedder",
        "",
    ]
    for layer in PORT_ORDER:
        methods = _protocol_methods(_PROTOCOLS[layer])
        lines.append(f"## {layer} ({len(methods)} methods)")
        lines.append("")
        lines.append("| Method | Contract test |")
        lines.append("|---|---|")
        for method in sorted(methods):
            lines.append(f"| {method} | `{COVERAGE[layer][method]}` |")
        lines.append("")
    _REPORT.write_text("\n".join(lines), encoding="utf-8")
