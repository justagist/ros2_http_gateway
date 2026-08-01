"""Fixtures for the phase 2 runtime tests.

Importing this module without ROS skips the importing test module, same as
``ros_fixtures``.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import rclpy
from example_interfaces.msg import String

from ros2_http_gateway.config import load_config
from ros2_http_gateway.resolve import ResolvedConfig, resolve

EXECUTOR_ENV = "GATEWAY_TEST_EXECUTOR"


def events_executor_available() -> bool:
    """Whether this distro provides ``rclpy.experimental.EventsExecutor`` at all.

    Deliberately distinct from :func:`available_executors`, which the CI matrix narrows through
    the environment. A run restricted to ``multithreaded`` on a distro that does have the events
    executor must not conclude the distro lacks it.

    Returns:
        True when the module is importable.
    """
    return importlib.util.find_spec("rclpy.experimental") is not None


def available_executors() -> list[str]:
    """Which executors this distro can actually run.

    Humble has no ``rclpy.experimental``, so parametrising over ``events`` there would only
    assert that our own startup error fires -- which
    ``test_events_requested_but_missing_is_a_startup_error`` already does deliberately, with a
    simulated import failure, on every distro.

    ``GATEWAY_TEST_EXECUTOR`` narrows the list further. That is how the CI matrix runs a distro
    once per executor rather than both in a single job; a request for something this distro
    cannot provide is ignored rather than silently skipping the tests.

    Returns:
        Executor names to parametrise over, never empty.
    """
    available = ["multithreaded"]
    if events_executor_available():
        available.append("events")
    requested = os.environ.get(EXECUTOR_ENV)
    if requested and requested in available:
        return [requested]
    return available


STATE_TOPIC = "/gateway_test_state"
COMMAND_TOPIC = "/gateway_test_command"

OPERATOR_TOKEN = "operator-token-for-tests"
VIEWER_TOKEN = "viewer-token-for-tests"
# sha256 of the two literals above, for configs that want hashed tokens.
OPERATOR_SHA = "534125de141542e27a3668e21ce0ad7a4820c1a76d97a5d098b1c7df6eca3f1d"
VIEWER_SHA = "ff4ee565c99e7deabf6c6c09ed239861b144dbc0b6247c0c334726d6e30d49ee"


def config_document(**overrides: Any) -> dict[str, Any]:
    """A config exposing one topic read, one topic write, a service and an action."""
    document: dict[str, Any] = {
        "endpoints": {
            "/mission/status": {
                "topic": STATE_TOPIC,
                "type": "example_interfaces/msg/String",
                "roles": ["viewer", "operator"],
                "stale_after_ms": 300,
            },
            "/never/stales": {
                "topic": STATE_TOPIC,
                "type": "example_interfaces/msg/String",
                "roles": ["viewer", "operator"],
            },
            "/cmd": {
                "topic": COMMAND_TOPIC,
                "type": "example_interfaces/msg/String",
                "method": "POST",
                "roles": ["operator"],
            },
            "/call": {
                "service": "/gateway_test_service",
                "type": "std_srvs/srv/Trigger",
                "roles": ["operator"],
            },
            "/pick": {
                "action": "/gateway_test_action",
                "type": "example_interfaces/action/Fibonacci",
                "roles": ["operator"],
            },
        },
        "auth": {
            "tokens": [
                {"token": OPERATOR_TOKEN, "user": "operator-user", "roles": ["operator"]},
                {"token": VIEWER_TOKEN, "user": "viewer-user", "roles": ["viewer"]},
            ]
        },
        # Never bound by the app-level tests, which drive the ASGI app directly. The
        # serving test picks a free port for itself.
        "http": {"bind": "127.0.0.1", "port": 8080},
        "audit": {"sink": "stderr"},
    }
    document.update(copy.deepcopy(overrides))
    return document


def write_config(tmp_path: Path, document: dict[str, Any]) -> Path:
    """Write a config file with private permissions, so plaintext tokens are accepted."""
    import yaml

    target = tmp_path / "gateway.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    target.chmod(0o600)
    return target


def resolved_config(tmp_path: Path, **overrides: Any) -> ResolvedConfig:
    """Load and resolve a test config without touching the ROS graph."""
    return resolve(load_config(write_config(tmp_path, config_document(**overrides))))


class Talker:
    """A node in its own context that publishes state and listens for commands."""

    def __init__(self, name: str = "gateway_test_talker") -> None:
        self._context = rclpy.context.Context()
        self._context.init()
        self._node = rclpy.create_node(name, context=self._context)
        self._publisher = self._node.create_publisher(String, STATE_TOPIC, 10)
        self.heard: list[str] = []
        self._node.create_subscription(
            String, COMMAND_TOPIC, lambda msg: self.heard.append(msg.data), 10
        )
        self._executor = rclpy.executors.SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        import threading

        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._stopping = False
        self._thread.start()

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception:
            if not self._stopping:
                raise

    def publish(self, data: str) -> None:
        message = String()
        message.data = data
        self._publisher.publish(message)

    def publish_until(self, data: str, seen: Any, timeout: float = 15.0) -> bool:
        """Publish repeatedly until ``seen()`` is true, so discovery cannot flake the test."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.publish(data)
            if seen():
                return True
            time.sleep(0.02)
        return False

    def wait_for_command(self, timeout: float = 10.0) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.heard:
            time.sleep(0.02)
        return self.heard

    def close(self) -> None:
        self._stopping = True
        self._executor.shutdown()
        self._thread.join(timeout=5)
        self._node.destroy_node()
        self._context.try_shutdown()
