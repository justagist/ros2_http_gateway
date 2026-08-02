# ros2_http_gateway

A config-driven, authenticated HTTP + SSE gateway for ROS 2.

One YAML file declares exactly which topics, services and actions are reachable over
HTTP, with per-endpoint role-based authorisation, state-based preconditions, audit
logging, server-sent events and an auto-generated OpenAPI 3.1 document.

**Deny by default: nothing is exposed unless it appears in the config.**

> Status: feature-complete for 0.1.0 and not yet released. Topics, services, actions, the global
> SSE stream, the generated OpenAPI document with its Swagger UI, and the built-in test console all
> work end to end, with bearer auth, role checks, state preconditions and audit logging. Benchmarks
> are in [bench/RESULTS.md](bench/RESULTS.md) and the release workflow is written; what remains
> before a tag is listed in [docs/release.md](docs/release.md). See
> [CHANGELOG.md](CHANGELOG.md) for what is real, [docs/configuration.md](docs/configuration.md)
> for the config format, [docs/protocol.md](docs/protocol.md) for the wire protocol,
> [docs/security.md](docs/security.md) for the security model and its known gaps, and
> [docs/decisions.md](docs/decisions.md) for the settled questions and why.

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

## Quickstart: the two-terminal tour

The demo node brings up one of everything -- a mission state cycling IDLE/RUNNING, a chatter
topic at 2 Hz, a `std_srvs/Trigger` service and a deliberately slow Fibonacci action -- and
`examples/demo.yaml` exposes all of it with an operator and a viewer token.

```bash
# terminal 1
ros2 run ros2_http_gateway ros2-http-gateway-demo

# terminal 2
ros2-http-gateway examples/demo.yaml
```

Then either poke it with curl:

```bash
curl -H 'Authorization: Bearer demo-operator-token' \
  http://127.0.0.1:8080/api/v1/mission/status
```

or open <http://127.0.0.1:8080/api/v1/console> and paste `demo-operator-token`. Paste
`demo-viewer-token` instead and the write, the service and the action vanish from the page,
because the console is built from a manifest filtered to the token's roles.

The service is gated on the live mission state, so `POST /api/v1/mission/start` flips between
success and `PRECONDITION_FAILED` every eight seconds as the demo cycles, and the console shows
the state it is gating on beside the button.

Interactive API docs are at `/api/v1/docs`, the OpenAPI document at `/api/v1/openapi.json`.

### Validating a config without starting anything

```bash
ros2-http-gateway --check examples/demo.yaml               # exit 0/1
ros2-http-gateway --check --dump-openapi api.json examples/demo.yaml
```

`--check` resolves every interface type, compiles every converter and checks every
precondition field against the real message definition, then prints one line per endpoint.
It needs no running system when the config pins its types, which is what makes it usable
as a CI gate; add `--online` on the robot to also verify those pinned types against the
live graph. Full format in [docs/configuration.md](docs/configuration.md), wire protocol in
[docs/protocol.md](docs/protocol.md).

## The test console

Off unless `http.console: true`, and deliberately a testing tool rather than an HMI: every widget
maps to one protocol feature, and there is no dashboard, chart or saved layout anywhere in it.
Everything is discovered from `<base>/console/manifest` and the schemas in the OpenAPI document,
so nothing about an endpoint is hardcoded.

- A **token wallet**: several labelled tokens, one active, switchable in place. They live in the
  browser's local storage, which the page says plainly.
- An **auth matrix** per endpoint: fire as nobody, as a bad token, and as each saved token, and
  read the 401/401/403/2xx grid. Anything that is not a GET is probed with a body no converter
  accepts, so an authorised token shows 400 -- proof the token and its roles passed, with nothing
  published, called or submitted.
- **Topic tiles** with the sample, its `seq` and stamp, a countdown to `stale_after_ms` and a tile
  that visibly flips when it expires. Manual GET or auto-poll.
- **Schema-driven forms** for writes and services, built from the served write schemas. Flat fields
  and one level of nesting get widgets; arrays and anything deeper fall back to a raw JSON editor
  validated in the browser against the same schema.
- **The protocol taxonomy, visibly**: a 4xx that never reached ROS, a 503 capacity limit, a 200
  refused by a state gate, a 200 carrying a ROS-side error, and a plain success all look different.
- **Gated commands** show the live value of their precondition's state topic beside the button, and
  predict the gateway's decision the way the gateway makes it.
- Per-call latency, and a hammer button (20 sequential calls, p50/p95).
- **Goal cards** for actions: status by polling, feedback over the goal's own stream, cancel,
  `ALREADY_TERMINAL` on a second cancel, the result, a countdown to `result_ttl_s` and then the
  404 once it is evicted. Several goals at once.
- **An event-stream panel**: connect and disconnect, filter by endpoint, heartbeats rendered
  faintly, a seq-gap detector, and a transport toggle between `fetch` with header auth and
  `EventSource` with the query token -- which also makes visible that `EventSource` cannot show
  you a heartbeat, because the browser swallows comment frames.
- **Slow-consumer mode**: stop draining the reader and watch the configured `overflow` policy
  happen -- `drop_oldest` as gaps in `seq`, `disconnect` as the stream ending.
- **An audit hint** under every response: the `decision`, `outcome` and reason this request will
  have written to the audit log, derived from the answer rather than read back, since the audit
  sink is deliberately not served over HTTP.

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

## Security model

Deny by default: an endpoint that is not in the config does not exist, and there is no
discovery path that could add one at runtime.

- **Tokens** are compared as SHA-256 digests with `hmac.compare_digest`, and every configured
  digest is compared even after a match, so neither the token nor its position in the file can
  be timed. Prefer `token_sha256:` in the config; plaintext `token:` is hashed on load, warned
  about loudly, and refused outright if the config file is world-readable.
- **Roles** are per endpoint. A valid token with the wrong role is 403, and authentication and
  the role check both run *before* any state precondition, so a caller who may not use an
  endpoint learns nothing about the robot from being refused.
- **Roles gate action, not description.** An authenticated caller can already enumerate the
  configured routes by asking -- an unknown path answers 404 and a known one they may not use
  answers 403 -- so the OpenAPI document is served whole to any of them. The console manifest
  does filter itself to the caller's roles, but that is defence in depth and a convenience, not
  a boundary: do not design against it, and do not treat an endpoint as hidden because a
  manifest omits it.
- **Audit**: one JSON line per authenticated request -- who, what, the decision and its reason,
  the outcome, the latency. Never tokens, never request bodies, never the query string.
- **The gateway does not terminate TLS.** It binds `127.0.0.1` unless you say otherwise, and
  binding `0.0.0.0` logs a warning. Put a reverse proxy in front of it; see below.
- **No rate limiting and no request size limit.** Both belong in the proxy.

Known gaps, and why, are written down in [docs/security.md](docs/security.md) -- including the
one place a credential may appear in a URL (`?access_token=` on the event stream, because
browser `EventSource` cannot set headers) and what that costs you.

## Behind a reverse proxy

The proxy terminates TLS and, if you want, mounts several cells under one host. Tell the
gateway what prefix it was reached through and its OpenAPI `servers`, its Swagger UI asset
URLs and its "Try it out" requests all come out prefix-correct.

```nginx
upstream cell3 { server 127.0.0.1:8081; keepalive 8; }
upstream cell4 { server 127.0.0.1:8082; keepalive 8; }

server {
    listen 443 ssl;
    server_name cells.example.com;

    ssl_certificate     /etc/ssl/certs/cells.pem;
    ssl_certificate_key /etc/ssl/private/cells.key;

    # One block per gateway. The trailing slash on both sides strips the prefix before it
    # reaches the gateway, and X-Forwarded-Prefix hands it back so generated URLs keep it.
    location /cell3/ {
        proxy_pass http://cell3/;
        proxy_set_header X-Forwarded-Prefix /cell3;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Connection "";     # keep the upstream connection alive

        # The event stream and the per-goal feedback streams need all three. Without
        # `proxy_buffering off` nginx holds events until the response ends, which for a
        # stream is never; the gateway also sends `X-Accel-Buffering: no`, but do not rely
        # on one of the two.
        proxy_buffering off;
        gzip off;
        proxy_read_timeout 1h;

        client_max_body_size 256k;         # the gateway has no limit of its own
        limit_req zone=api burst=20 nodelay;
    }

    location /cell4/ {
        proxy_pass http://cell4/;
        proxy_set_header X-Forwarded-Prefix /cell4;
        include /etc/nginx/snippets/gateway-common.conf;
    }
}
```

`https://cells.example.com/cell3/api/v1/docs` then loads Swagger UI, whose document says
`servers: [{"url": "/cell3/api/v1"}]`, so every call it makes lands on the right gateway.

Two things to get right: the heartbeat is every 15 s, so `proxy_read_timeout` must be well
above that; and `limit_req`/`client_max_body_size` are the proxy's job because the gateway
deliberately has neither.

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
