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
- `docs/configuration.md` and `docs/protocol.md`: the full config reference, and the wire
  protocol including the status-code taxonomy (4xx never reached ROS, 200 carries the
  ROS-side outcome, one error envelope for both).
- `--check --online` additionally verifies pinned `type:` values against the live ROS graph
  and reports any that disagree with what is actually being published. The default check
  stays hermetic so it can gate a pull request.

### Changed

- Numeric sequences take a zero-copy numpy view of the `array.array` buffer instead of
  building a list, once they are long enough for that to pay. Measured crossover is 64
  elements for every dtype; at 8192 the view is 1.6x faster for doubles and 3.3x for int32.
- `float32` sequences always take the view, at any length. The two paths do not produce the
  same JSON (`1.87` versus `1.8700000047683716`), and formatting that varied with the length
  of a sequence would be a hostile API. This matches what fixed-size `float32[N]` already
  emitted.
- Conversion errors now name the field by the dotted path the client wrote
  (`header.stamp.sec`, `status[].values[].key`) rather than by the ROS type that declared
  it, and carry it separately as `ConversionError.field` for the `BAD_REQUEST` envelope.

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
