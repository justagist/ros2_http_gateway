"""Example configs must at least be well-formed YAML at every phase boundary.

Schema validation of these files is added in phase 1 alongside config.schema.json.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.yaml"))


def test_examples_exist() -> None:
    assert {p.name for p in EXAMPLES} == {"dev.yaml", "locked_down.yaml"}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_valid_yaml_with_endpoints(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert doc["endpoints"], "an example with no endpoints exposes nothing"


def test_locked_down_example_has_no_plaintext_tokens() -> None:
    doc = yaml.safe_load((EXAMPLES[1]).read_text(encoding="utf-8"))
    assert EXAMPLES[1].name == "locked_down.yaml"
    for token in doc["auth"]["tokens"]:
        assert "token" not in token, "locked_down.yaml must only use token_sha256"
        assert "token_sha256" in token


def test_locked_down_example_exposes_no_publish_endpoints() -> None:
    doc = yaml.safe_load((EXAMPLES[1]).read_text(encoding="utf-8"))
    for key, endpoint in doc["endpoints"].items():
        if "topic" in endpoint:
            assert endpoint.get("method", "GET") == "GET", f"{key} publishes"
