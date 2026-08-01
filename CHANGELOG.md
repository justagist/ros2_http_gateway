# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Package skeleton, dual-installable via `pip` (PEP 517, setuptools) and `colcon`
  (`ament_python`). Version single-sourced from `ros2_http_gateway/_version.py`.
- Console entry points `ros2-http-gateway` and `gateway`, the latter so that
  `ros2 run ros2_http_gateway gateway` resolves.
- `--help` and `--version` work without a ROS 2 environment; anything graph-touching
  fails with a single actionable "source a ROS 2 environment" message.
- Tooling: ruff, `mypy --strict`, pytest, pre-commit, GitHub Actions with a
  no-ROS lint/type/unit job and a ROS distro matrix job.
- Example configs `examples/dev.yaml` and `examples/locked_down.yaml`.

[Unreleased]: https://github.com/justagist/ros2_http_gateway/compare/main...HEAD
