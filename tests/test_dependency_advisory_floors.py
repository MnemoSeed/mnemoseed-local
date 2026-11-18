import tomllib
from pathlib import Path

from packaging.version import Version

FIXED_FLOORS = {
    "certifi": "2026.10.18",
    "filelock": "4.2.1",
    "urllib3": "2.8.0",
}


def _locked_versions() -> dict[str, str]:
    lock = tomllib.loads((Path(__file__).resolve().parents[1] / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


def test_locked_dependency_versions_meet_security_floors() -> None:
    locked = _locked_versions()
    for name, floor in FIXED_FLOORS.items():
        assert name in locked, f"{name} missing from uv.lock"
        assert Version(locked[name]) >= Version(floor), f"{name} {locked[name]} below required {floor}"
