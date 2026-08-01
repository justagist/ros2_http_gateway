# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Config engine: YAML, JSON Schema validation against the bundled `config.schema.json`,
  defaults merging, and a frozen typed model. Every problem in a file is reported at once,
  each naming its exact location. Runs without ROS.
- Interface type resolution. An omitted `type:` is resolved from the ROS graph with
  `--discovery-timeout`; an absent or ambiguous name fails at startup naming the endpoint
  and the candidates seen. A config that pins its types needs no graph at all, so `--check`
  works as a CI gate.
- `codegen.py`: generated message-to-dict and dict-to-message converters, compiled once at
  startup. Measured 9.9x to 48x faster than `rosidl_runtime_py.message_to_ordereddict`
  across the test battery, 48x on `sensor_msgs/Imu`.
- `schemagen.py`: JSON Schema per interface type, in read and write directions, tested to
  agree with what the converters actually produce and accept.
- `ros2-http-gateway --check` validates a config, resolves every type, compiles every
  converter and checks every dotted precondition field against a real message definition,
  then prints one line per endpoint. Exit 0 or 1.
- `docs/configuration.md`: the full config reference.

### Notes

- Plaintext `token:` entries are refused when the config file is world-readable. Both
  shipped examples therefore use `token_sha256`.
- `--dump-openapi` is not accepted yet; it arrives with OpenAPI generation in a later
  phase.

## Earlier unreleased work

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
