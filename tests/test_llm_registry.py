"""LLM driver registry (PRD-02 T6; FR-2.14).

One table for the whole llm layer, mirroring the storage registry idiom: drivers
register by class, resolve by ``LLMDriverInfo.name``, and construction errors
stay typed. The storage DriverRegistry is deliberately not reused (its identity
contract and errors are storage-specific), so these tests pin the LLM analog.
"""

from __future__ import annotations

import pytest

from mnemoseed_local.llm import LLMDriverInfo, LLMError, UnknownLLMDriverError
from mnemoseed_local.llm.registry import LLMRegistry, register


class _FakeD:
    info = LLMDriverInfo(name="fake_d", description="test double")

    def __init__(self, **params):
        self.params = params


class _NoInfo:
    pass


def _registry() -> LLMRegistry:
    return LLMRegistry("test-llm")


def test_add_and_names() -> None:
    reg = _registry()
    register(reg)(_FakeD)
    assert reg.contains("fake_d")
    assert reg.names() == ("fake_d",)
    assert reg.layer == "test-llm"


def test_duplicate_registration_rejected() -> None:
    reg = _registry()
    register(reg)(_FakeD)
    with pytest.raises(LLMError, match="duplicate"):
        register(reg)(_FakeD)


def test_register_requires_info_name() -> None:
    reg = _registry()
    with pytest.raises(LLMError, match="must declare a non-empty LLMDriverInfo.name"):
        reg.add(_NoInfo)


def test_build_returns_instance_with_params() -> None:
    reg = _registry()
    register(reg)(_FakeD)
    instance = reg.build("fake_d", {"a": 1, "b": "x"})
    assert instance.params == {"a": 1, "b": "x"}


def test_build_unknown_driver_is_typed() -> None:
    reg = _registry()
    register(reg)(_FakeD)
    with pytest.raises(UnknownLLMDriverError, match=r"unknown llm driver 'nope' \(available: fake_d\)"):
        reg.build("nope", {})


def test_build_wraps_construction_errors() -> None:
    reg = _registry()

    class _Broken:
        info = LLMDriverInfo(name="broken", description="")

        def __init__(self, **params):
            del params
            raise RuntimeError("boom")

    register(reg)(_Broken)
    with pytest.raises(LLMError, match="failed to construct"):
        reg.build("broken", {})


def test_clear_drops_registrations() -> None:
    reg = _registry()
    register(reg)(_FakeD)
    reg.clear()
    assert not reg.contains("fake_d")
    with pytest.raises(UnknownLLMDriverError, match="no test-llm drivers registered"):
        reg.build("fake_d", {})
