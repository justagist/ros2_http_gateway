"""Test configuration.

The one thing worth explaining here is the ROS integrity guard. The ROS-dependent modules
open with ``pytest.importorskip("rclpy")`` so that the suite runs on a plain Python
installation, which is a hard requirement. The cost is that a *broken* ROS environment
looks exactly like an absent one: the modules skip, the run is green, and nothing has been
tested. That happened -- an incomplete environment silently reduced a 485-test run to 96,
and only a separate CLI step noticed.

So in a job that is supposed to have ROS, set ``GATEWAY_REQUIRE_ROS=1``. The ROS imports
then have to succeed, and the collection error names the module that would not import.
"""

from __future__ import annotations

import importlib
import os

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

    Args:
        config: The pytest config object.

    Raises:
        pytest.UsageError: If ``GATEWAY_REQUIRE_ROS`` is set and any required ROS module
            cannot be imported. Failing here rather than skipping is the point: a skipped
            ROS suite in a ROS job is a false pass.
    """
    if os.environ.get(_REQUIRE_ROS_ENV, "") not in ("1", "true", "yes"):
        return

    broken: list[str] = []
    for name in _REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        # Any failure at all means the environment is unusable, not just ImportError:
        # a half-built interface package can raise from its own module body.
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
