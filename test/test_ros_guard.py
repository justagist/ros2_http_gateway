"""Tests for the ROS integrity guard in conftest.

Regression cover for a false pass: a broken ROS environment made every ROS-marked module
skip, so a 485-test run silently became 96 and the job went green. The guard turns that
into a collection error, and these tests keep the guard itself from being hollowed out.
"""

from __future__ import annotations

import conftest
import pytest


def test_guard_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must stay runnable on a plain Python installation."""
    monkeypatch.delenv("GATEWAY_REQUIRE_ROS", raising=False)
    conftest.pytest_configure(pytest.Config.__new__(pytest.Config))


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_guard_raises_when_a_required_module_is_missing(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GATEWAY_REQUIRE_ROS", value)
    monkeypatch.setattr(conftest, "_REQUIRED_MODULES", ("definitely_not_a_real_module",))
    with pytest.raises(pytest.UsageError, match="definitely_not_a_real_module"):
        conftest.pytest_configure(pytest.Config.__new__(pytest.Config))


def test_guard_ignores_unrecognised_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REQUIRE_ROS", "0")
    monkeypatch.setattr(conftest, "_REQUIRED_MODULES", ("definitely_not_a_real_module",))
    conftest.pytest_configure(pytest.Config.__new__(pytest.Config))


def test_guard_covers_the_whole_stack_not_just_rclpy() -> None:
    """rclpy alone importing is not proof the environment can load an interface type."""
    required = set(conftest._REQUIRED_MODULES)
    assert "rclpy" in required
    assert any(name.startswith("rosidl_runtime_py") for name in required)
    assert any(name.startswith("rosidl_parser") for name in required)
    assert "test_msgs.msg" in required, "a real interface package is the end-to-end check"


def test_guard_reports_every_broken_module_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REQUIRE_ROS", "1")
    monkeypatch.setattr(conftest, "_REQUIRED_MODULES", ("no_module_a", "no_module_b"))
    with pytest.raises(pytest.UsageError) as exc:
        conftest.pytest_configure(pytest.Config.__new__(pytest.Config))
    assert "no_module_a" in str(exc.value)
    assert "no_module_b" in str(exc.value)
