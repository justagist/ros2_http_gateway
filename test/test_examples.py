"""The shipped example configs, checked for the properties each one claims.

Their schema and type validity is covered by `test_resolve.py`, which runs `--check` over every
one of them. This file is about the promises the files make in prose: that the locked-down one is
actually locked down, and that none of them ships a token the gateway would refuse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.yaml"))


def load(name: str) -> dict[str, Any]:
    """Read one example by name.

    By name, never by position: a sorted glob reorders the moment an example is added, and
    indexing into it once made three unrelated tests fail when `demo.yaml` landed first.
    """
    document = yaml.safe_load((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict), name
    return dict(document)


def test_examples_exist() -> None:
    assert {path.name for path in EXAMPLES} == {"dev.yaml", "locked_down.yaml", "demo.yaml"}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_valid_yaml_with_endpoints(path: Path) -> None:
    document = load(path.name)
    assert document["endpoints"], "an example with no endpoints exposes nothing"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_no_example_ships_a_token_the_gateway_would_refuse(path: Path) -> None:
    """A checkout is world-readable, and a plaintext token in a world-readable file is refused.

    So an example carrying one would fail `--check` for everyone who cloned the repository.
    """
    for token in load(path.name)["auth"]["tokens"]:
        assert "token" not in token, f"{path.name} would be refused on a fresh checkout"
        assert "token_sha256" in token


def test_locked_down_example_exposes_no_publish_endpoints() -> None:
    for key, endpoint in load("locked_down.yaml")["endpoints"].items():
        if "topic" in endpoint:
            assert endpoint.get("method", "GET") == "GET", f"{key} publishes"


def test_only_the_demo_enables_the_console() -> None:
    """It is a testing tool, so the reviewable example must not switch it on."""
    assert load("demo.yaml")["http"]["console"] is True
    for name in ("dev.yaml", "locked_down.yaml"):
        assert "console" not in load(name).get("http", {}), name
