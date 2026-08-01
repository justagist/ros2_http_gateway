"""Test configuration.

The ROS-dependent modules open with ``pytest.importorskip("rclpy")`` so the suite runs on a
plain Python installation. The cost is that a *broken* ROS environment looks exactly like an
absent one: everything skips and the run is green. So a job that is supposed to have ROS sets
``GATEWAY_REQUIRE_ROS=1``, and the collection error names the module that would not import.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

_REQUIRE_ROS_ENV = "GATEWAY_REQUIRE_ROS"

# Enough of the stack that a partially installed environment cannot slip through: rclpy
# itself, the introspection the code generator reads, and one real interface package.
_REQUIRED_MODULES = (
    "rclpy",
    "rclpy.action",
    "rclpy.serialization",
    "rosidl_parser.definition",
    "rosidl_runtime_py.utilities",
    "rosidl_runtime_py.convert",
    "test_msgs.msg",
)


def pytest_configure(config: pytest.Config) -> None:
    """Fail collection if ROS is required but not fully importable.

    Failing rather than skipping is the point: a skipped ROS suite in a ROS job is a false pass.
    """
    if os.environ.get(_REQUIRE_ROS_ENV, "") not in ("1", "true", "yes"):
        return

    broken: list[str] = []
    for name in _REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        # Not just ImportError: a half-built interface package raises from its module body.
        except Exception as exc:
            broken.append(f"{name}: {type(exc).__name__}: {exc}")

    if broken:
        listed = "\n  ".join(broken)
        raise pytest.UsageError(
            f"{_REQUIRE_ROS_ENV} is set, so the ROS suite must run, but these imports "
            f"failed:\n  {listed}\n"
            "Source a ROS 2 environment, or unset the variable to allow the ROS tests to "
            "skip."
        )


@pytest.fixture
def talker() -> Iterator[Any]:
    """A ROS node publishing the gateway's state topic and listening for its commands.

    Lives here rather than in a helper module so that test files can take it as a parameter
    without importing the name, which the linter would otherwise read as a redefinition. The
    ROS import is inside the body, so collecting this file without ROS stays harmless.
    """
    from gateway_fixtures import Talker

    instance = Talker()
    try:
        yield instance
    finally:
        instance.close()
