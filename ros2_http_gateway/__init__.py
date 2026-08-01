"""Config-driven, authenticated HTTP + SSE gateway for ROS 2.

Importing this package never imports ``rclpy``, so the config engine, code generator
and HTTP layer stay unit-testable on a plain Python installation.
"""

from ._version import __version__

__all__ = ["__version__"]
