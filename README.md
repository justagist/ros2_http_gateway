# ros2_http_gateway

A config-driven, authenticated HTTP + SSE gateway for ROS 2.

One YAML file declares exactly which topics, services and actions are reachable over
HTTP, with per-endpoint role-based authorisation, state-based preconditions, audit
logging, server-sent events and an auto-generated OpenAPI 3.1 document.

**Deny by default: nothing is exposed unless it appears in the config.**

> Status: under construction. Topics, services, actions, the global SSE stream and the generated
> OpenAPI document with its Swagger UI all work end to end today, with bearer auth, role checks,
> state preconditions and audit logging. Still to come: the bench suite and the release. See
> [CHANGELOG.md](CHANGELOG.md) for what is real, [docs/configuration.md](docs/configuration.md)
> for the config format, [docs/protocol.md](docs/protocol.md) for the wire protocol and
> [docs/security.md](docs/security.md) for the security model and its known gaps.

## Why not rosbridge

`rosbridge_suite` exposes the whole ROS graph over a WebSocket with, at best, a shared
secret. That is the right tool for a debugging UI on a bench, and the wrong tool for a
machine that has to pass a security review.

| | rosbridge | ros2_http_gateway |
|---|---|---|
| Exposed surface | whole graph, discovered at runtime | only what the config names |
| Transport | WebSocket, custom protocol | plain HTTP + SSE, documented in OpenAPI |
| Auth | optional shared secret | per-token identity, per-endpoint roles |
| Authorisation | none | roles plus state-based preconditions |
| Audit | none | one JSON line per authenticated request |
| Clients | needs roslibjs | anything that speaks HTTP |

If you want the whole graph, use rosbridge. If you want a curated, reviewable,
policy-checked surface for an HMI, a fleet dashboard or a customer integration, this is
that.

## Install

### From PyPI

```bash
pip install ros2-http-gateway
```

`rclpy` is deliberately not a pip dependency; it comes from your sourced ROS 2
environment. `--help` and `--version` work without ROS, everything else needs
`source /opt/ros/$ROS_DISTRO/setup.bash`.

On a Debian-managed Python (which is what a ROS container gives you), install into a
virtualenv rather than fighting PEP 668. A plain venv is enough: `rclpy` and the interface
packages stay importable through the `PYTHONPATH` that `setup.bash` exports.

```bash
python3 -m venv .venv && .venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install ros2-http-gateway
```

Upgrading pip inside the venv matters on Humble: pip 22 installs an editable package with
`setup.py develop`, and the setuptools it pairs with ignores `pyproject.toml` metadata
entirely, so dependencies and extras are silently skipped.

### In a colcon workspace

```bash
cd ~/ws/src && git clone https://github.com/justagist/ros2_http_gateway.git
cd ~/ws && colcon build --packages-select ros2_http_gateway
source install/setup.bash
ros2 run ros2_http_gateway ros2-http-gateway --help
```

There is deliberately no shorter `gateway` alias: console scripts are generated from the
same entry points for wheel installs, so an alias would claim `~/.local/bin/gateway` on
every `pip install`. Typing the full name beats grabbing a generic one.

## Quickstart

```bash
ros2-http-gateway --check examples/dev.yaml   # validate, exit 0/1, no server started
ros2-http-gateway examples/dev.yaml           # serve on http://127.0.0.1:8080/api/v1
```

`--check` resolves every interface type, compiles every converter and checks every
precondition field against the real message definition, then prints one line per endpoint.
It needs no running system when the config pins its types, which is what makes it usable
as a CI gate; add `--online` on the robot to also verify those pinned types against the
live graph. Full format in [docs/configuration.md](docs/configuration.md), wire protocol in
[docs/protocol.md](docs/protocol.md).

```bash
curl -H 'Authorization: Bearer dev-token-change-me' \
  http://127.0.0.1:8080/api/v1/mission/status
```

Interactive docs are served from `/api/v1/docs`, the raw document from
`/api/v1/openapi.json`.

## Supported versions

Python 3.10+, ROS 2 Humble, Jazzy, Kilted, Lyrical and Rolling. Humble is the feature
floor: `ros.executor: auto` uses `rclpy.experimental.EventsExecutor` where it exists and
falls back to `MultiThreadedExecutor` where it does not.

One performance caveat, measured rather than assumed: on Humble a 1 kHz topic costs about
80% of a core, against under 5% on Jazzy and newer. The cost is rclpy's per-message dispatch
on that distro -- an idle gateway still costs ~1%, and the money is spent before any code in
this package runs -- so nothing here can avoid it. Everything works on Humble; if you intend
to stream kilohertz topics, budget for it or use a newer distro.

## Design in one picture

```
+- rclpy thread(s) -----------------+      +- asyncio thread -----------------+
| MultiThreadedExecutor / Events    |      | Starlette + uvicorn + uvloop     |
| raw=True subscriptions (CDR)      |      | auth -> policy -> handler        |
|   -> atomic ref swap into caches  |=====>| SSE broadcaster (encode once,    |
| service clients / action clients  |queue |   fan out bytes)                 |
+-----------------------------------+      +----------------------------------+
```

ROS callbacks never run on the asyncio loop, and the loop never blocks on ROS. All
expensive work (config validation, type resolution, converter code generation, route and
policy table construction, OpenAPI generation) happens at startup, so a request is
lookups and precompiled code only.

## Performance

Measured with `python3 bench/run.py` on a 24-core i7-14650HX, Jazzy, `rmw_fastrtps_cpp`; the
full table and its caveats are in [bench/RESULTS.md](bench/RESULTS.md).

| | |
|---|---|
| Cached topic `GET` | 11.8k req/s, p99 3.8 ms |
| Service round trip | p50 0.91 ms, p99 1.65 ms |
| State precondition | +10 us end to end, 1.1 us p99 measured on its own |
| `msg -> dict` (`sensor_msgs/Imu`) | 1.2 us, 44x `rosidl_runtime_py` |
| Event stream, 32 clients | 6.2k events/s for 18.7% of one core |
| Idle, no traffic | 0.5% of one core |

The event-stream row is the point of "encode once, fan out bytes": 32 clients cost 1.7x the CPU
of one, because a sample is converted and framed once and the same `bytes` object goes to every
queue. Throughput figures are a floor -- the load driver shares the machine with the gateway.

One caveat worth knowing before you pick a distro: on Humble a 1 kHz topic costs about 80% of a
core in rclpy's per-message dispatch, against under 5% on Jazzy and newer. See
[bench/README.md](bench/README.md).

## JSON representation

- `uint8[]` and `byte[]` fields become base64 strings.
- `int64`/`uint64` become JSON strings by default (`int64_as_string`), because values
  above 2^53 do not survive a JavaScript `number`.
- Numeric arrays are serialised straight from numpy, without an intermediate list. Numeric
  sequences, which rclpy hands over as `array.array`, become a zero-copy numpy view of the
  same buffer once they are long enough for that to pay (measured crossover: 64 elements).

## Status codes

4xx means the request never reached ROS: 401/403 auth, 400 malformed body or a value out of
range, 404 unknown route or goal. 200 means it did reach ROS and the body carries the
outcome, success or not (`TIMEOUT`, `SERVICE_UNAVAILABLE`, `PRECONDITION_FAILED`,
`GOAL_REJECTED`). Both use the same error envelope. See
[docs/protocol.md](docs/protocol.md).

## Non-goals for v1

- **Bulk or binary data planes.** Images, point clouds and other large blobs are not a
  target; this is a JSON control surface. Stream those over a purpose-built transport.
- **Graph auto-discovery or auto-exposure.** If it is not in the config, it is not
  reachable. That is the whole point.
- **TLS termination.** Run it behind nginx, Caddy or Traefik. A reverse-proxy cookbook
  ships with the docs.
- **Being an identity provider.** v1 ships static bearer tokens; the authenticator is a
  plugin interface so JWT/OIDC can land later without a redesign.
- WebSocket transport, parameter endpoints and multi-node federation.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
