"""Offline checks on `manifest.json` that hassfest would otherwise catch late.

hassfest runs only in CI. These cover the two rules that already failed there
or would fail silently: a requirement Home Assistant itself ships, and the
runtime requirements drifting from their mirror in `requirements-dev.txt`.
"""

from __future__ import annotations

from importlib.metadata import requires
import json
from pathlib import Path
import re

ROOT = Path(__file__).parent.parent.parent
MANIFEST = json.loads(
    (ROOT / "custom_components" / "sendspin" / "manifest.json").read_text()
)


def _name(requirement: str) -> str:
    """The normalised distribution name of a requirement line."""
    name = re.split(r"[<>=!~;\[ ]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_no_requirement_duplicates_one_of_home_assistant_core() -> None:
    """hassfest refuses a requirement that Home Assistant already installs.

    It failed on `pillow` from 2026-10-05. Pillow is a dependency of Home
    Assistant itself, so a custom integration must not list it.
    """
    core = {_name(line) for line in requires("homeassistant") or []}

    listed = {_name(line) for line in MANIFEST["requirements"]}

    assert listed.isdisjoint(core), sorted(listed & core)


def test_runtime_requirements_are_mirrored_for_the_test_suite() -> None:
    """Every manifest requirement is in `requirements-dev.txt`, verbatim."""
    mirror = {
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert set(MANIFEST["requirements"]) <= mirror


def test_manifest_keys_are_in_hassfest_order() -> None:
    """hassfest wants `domain`, `name`, then the other keys in alphabetical order."""
    keys = list(MANIFEST)

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
