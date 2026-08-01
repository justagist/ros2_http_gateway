"""Guarded access to the ROS 2 Python runtime.

Everything needing ``rclpy`` goes through here, so a missing ROS environment gives one
actionable message rather than a bare ``ModuleNotFoundError`` from deep in an import chain.
"""

from __future__ import annotations

import importlib.util
from typing import Final

_HINT: Final = (
    "rclpy not found -- source a ROS 2 environment first, "
    "e.g. `source /opt/ros/$ROS_DISTRO/setup.bash`."
)


class RosEnvironmentError(RuntimeError):
    """Raised when the ROS 2 Python runtime is required but unavailable."""


def rclpy_available() -> bool:
    """Return whether ``rclpy`` can be imported, without importing it."""
    try:
        return importlib.util.find_spec("rclpy") is not None
    except (ImportError, ValueError):
        return False


def require_rclpy() -> None:
    """Raise :class:`RosEnvironmentError` with an actionable message if ``rclpy`` is absent."""
    if not rclpy_available():
        raise RosEnvironmentError(_HINT)
