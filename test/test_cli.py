"""CLI behaviour that must hold with and without a ROS 2 environment."""

from __future__ import annotations

import pytest

from ros2_http_gateway import __version__, cli, ros_env


def test_help_needs_no_ros(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "config-driven" in capsys.readouterr().out.lower()


def test_version_needs_no_ros(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_missing_config_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_missing_rclpy_gives_one_actionable_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No traceback, no ModuleNotFoundError leaking out of an import chain."""
    monkeypatch.setattr(ros_env, "rclpy_available", lambda: False)
    assert cli.main(["--check", "examples/dev.yaml"]) == 1
    err = capsys.readouterr().err
    assert "rclpy not found" in err
    assert "setup.bash" in err
    assert "Traceback" not in err


def test_require_rclpy_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ros_env, "rclpy_available", lambda: False)
    with pytest.raises(ros_env.RosEnvironmentError, match="rclpy not found"):
        ros_env.require_rclpy()


def test_importing_the_package_does_not_import_rclpy() -> None:
    """The unit suite must run on a plain Python install, so keep the seam clean."""
    import subprocess
    import sys

    probe = (
        "import sys; import ros2_http_gateway, ros2_http_gateway.cli; "
        "assert 'rclpy' not in sys.modules, sorted(m for m in sys.modules if 'rclpy' in m)"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_online_without_check_is_a_usage_error() -> None:
    """Serving always uses the live graph, so the flag would be meaningless there."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--online", "examples/dev.yaml"])
    assert exc.value.code == 2


def test_online_is_documented_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "--online" in capsys.readouterr().out
