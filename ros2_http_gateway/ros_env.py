"""Guarded access to the ROS 2 Python runtime.

Everything that needs ``rclpy`` goes through here so that a missing ROS environment
produces one actionable message instead of a bare ``ModuleNotFoundError`` from deep
inside an import chain.
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
    """Return whether ``rclpy`` can be imported without importing it.

    Returns:
        True when an importable ``rclpy`` is on the module search path.
    """
    try:
        return importlib.util.find_spec("rclpy") is not None
    except (ImportError, ValueError):
        return False


def require_rclpy() -> None:
    """Fail fast with an actionable message when ``rclpy`` is unavailable.

    Raises:
        RosEnvironmentError: If ``rclpy`` cannot be imported.
    """
    if not rclpy_available():
        raise RosEnvironmentError(_HINT)
