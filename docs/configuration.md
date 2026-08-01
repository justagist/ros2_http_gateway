# Configuration reference

One YAML file describes the entire HTTP surface. Nothing not named in it is reachable.

Validate a file without starting anything:

```bash
ros2-http-gateway --check gateway.yaml           # exit 0 or 1, one line per endpoint
ros2-http-gateway --check --online gateway.yaml  # also check against the live graph
ros2-http-gateway --check --discovery-timeout 15 gateway.yaml
```

`--check` reports every problem it finds at once, each one naming its exact location in
the file. A machine-readable JSON Schema for the whole format ships inside the package at
`ros2_http_gateway/config.schema.json`, so an editor can autocomplete and lint it.

## Top level

| Key | Required | Default | Purpose |
|---|---|---|---|
| `endpoints` | yes | | HTTP path to what it exposes |
| `auth` | yes | | Bearer token identities and their roles |
| `defaults` | no | | Values merged into every endpoint |
| `sse` | no | no stream | Global server-sent-events stream |
| `http` | no | see below | Listening socket and API prefix |
| `ros` | no | `executor: auto` | ROS-side runtime options |
| `actions` | no | `result_ttl_s: 300` | Goal registry behaviour |
| `audit` | no | `sink: stderr` | Where audit records go |
| `json` | no | `int64_as_string: true` | JSON encoding options |

Unknown keys are always an error, never ignored. A typo in a security-relevant key must
not fail open.

Two routes that could answer the same request are also an error. `/a/{x}` and `/a/{y}` on
the same method are indistinguishable, and so are `/mission/status` and `/mission/{command}`
if both are `GET`; which one won would otherwise depend on registration order. The same check
covers `sse.path`, the routes generated for an action endpoint (`/pick/{goal_id}` and
`/pick/{goal_id}/stream`), and the built-in `GET /health`, `GET /openapi.json` and
`GET /docs`. Differing methods are fine: a topic `GET` at `/mission/status` beside a service
`POST` at `/mission/{command}` is unambiguous.

## `endpoints`

The key is the HTTP path, relative to `http.base_path`. Path parameters use
`{braces}`. The kind is inferred from which of `topic`, `service` or `action` is
present; exactly one is required.

```yaml
endpoints:
  /mission/status:
    topic: /mission/state              # GET the latest cached sample
    type: example_interfaces/msg/String
    roles: [viewer, operator]
    stale_after_ms: 1000

  /cmd_vel:
    topic: /cmd_vel                    # POST publishes the request body
    method: POST
    roles: [operator]

  /math/{a}/plus/{b}:
    service: /add_two_ints
    timeout_ms: 3000
    request_map: { a: "{a}", b: "{b}" }

  /pick:
    action: /pick_place
    roles: [operator]
```

| Key | Applies to | Default | Notes |
|---|---|---|---|
| `topic` / `service` / `action` | | | Exactly one. Absolute or root-relative ROS name; `~` is not accepted |
| `type` | all | resolved from the graph | `<pkg>/msg\|srv\|action/<Name>`, and the middle segment must match the kind |
| `method` | topic | `GET` | `POST` on a topic publishes. Services and actions are always `POST` |
| `qos` | topic | `default` | `default` is reliable, depth 10. `sensor_data` is best-effort, depth 5 |
| `roles` | all | from `defaults` | At least one role, here or in `defaults`, or the endpoint is unreachable |
| `stale_after_ms` | topic `GET` | never stale | Past this age a read returns `"stale": true` |
| `timeout_ms` | service, action | `5000` | |
| `request_map` | service | | Request field to template, drawing on path parameters |
| `preconditions` | anything that changes state | | See below |

Setting `stale_after_ms` on anything but a topic `GET`, or `timeout_ms` on a topic, is an
error: it means the config does not say what its author thought. The same keys arriving
from `defaults` are simply ignored where they do not apply, so one `defaults:` block can
serve a config holding all three kinds.

### Omitting `type`

With no `type`, the gateway resolves it from the ROS graph at startup, waiting up to
`--discovery-timeout` seconds (default 5). If the name is absent, or advertises more than
one type, startup fails and names the endpoint and the candidates it saw.

Pinning `type` explicitly makes `--check` hermetic: no graph, no running system, so it
works as a CI gate on a pull request. Both shipped examples pin their types for exactly
that reason.

`--check --online` adds the other half. It looks up every endpoint, pinned ones included,
and reports any whose configured type is not what the graph advertises:

```
error: gateway.yaml: 1 problem(s)
  - endpoints./mission/status: configured as example_interfaces/msg/String, but
    /mission/state on the graph is std_msgs/msg/String
```

A type string can be syntactically valid, importable, and still not be the one the robot
is publishing. The hermetic check cannot catch that; run `--online` on the machine as a
deploy-time smoke test. It only applies to `--check`, since serving always uses the live
graph.

### Choosing `qos`

A **reliable** subscription does not match a **best-effort** publisher. That is the single
most likely reason a correctly-named topic endpoint stays empty forever, because nearly every
sensor publishes best-effort:

```yaml
  /camera/info:
    topic: /camera/camera_info
    qos: sensor_data          # matches a sensor publisher
    roles: [viewer]
```

If a subscription finds no publisher within a few seconds of startup, the gateway logs a
warning naming the topic and this setting, rather than leaving you with an endpoint that
answers `"stale": true` forever and no clue why.

Only these two presets are offered, and they are the two that matter. If you need a specific
depth, durability or deadline, say so on the issue tracker with the case.

## `defaults`

Merged into every endpoint that does not set the key itself. Only `roles`,
`stale_after_ms` and `timeout_ms` are accepted, and deliberately not `method`: a default
that silently turned topics into publishers would be a hazard, not a convenience.

## `preconditions`

A precondition compares a field of another endpoint's latest cached sample against an
allow-list before the call is forwarded. A rejection returns HTTP 200 with
`"success": false` and `"error_code": "PRECONDITION_FAILED"`, and is recorded in the audit
log with its reason.

```yaml
  /mission/{command}:
    service: /mission_control
    preconditions:
      - state_endpoint: /mission/status   # must be a configured topic GET endpoint
        field: data                       # dotted paths allowed: status.state.id
        key: "{command}"                  # optional; defaults to the sole path parameter
        allow:
          start: [IDLE]                   # start only from IDLE
          abort: [RUNNING, PAUSED]
          reset: ["*"]                    # reset from any state
          "*": [IDLE]                     # any other command, only while IDLE
```

- `state_endpoint` must name a configured topic `GET` endpoint. Anything else has no
  cached sample to read.
- `field` is checked against the real message definition at startup. A field that does not
  exist, a dotted path through a scalar, or a field that is not a string or integer (and so
  cannot be compared with the strings in `allow`) all fail before the port opens.
- `key` names the path parameter whose value keys `allow`. With exactly one path parameter
  it is inferred; with several it is required; with none, the only usable key is `"*"`.
- `"*"` works in both positions: as a key it matches any parameter value, and as the whole
  state list it matches any state. A parameter value matching no key is denied.

## `auth`

```yaml
auth:
  tokens:
    - token_sha256: "f01d24...06607"     # lowercase hex sha256 of the bearer token
      user: hmi-station-1
      roles: [operator]
```

Clients send `Authorization: Bearer <token>`. Comparison is against the hash, in constant
time. A missing or unknown token is 401; a known token without the endpoint's role is 403.

A plaintext `token:` is accepted and hashed on load, but it warns loudly and is **refused
outright if the config file is world-readable**, which a git checkout always is. Generate
the hash instead:

```bash
printf %s "$TOKEN" | sha256sum
```

The same token cannot be assigned to two users; that is a config error, not a merge of
their roles.

## `sse`

```yaml
sse:
  path: /events
  include: [/mission/status]      # endpoint keys; topic endpoints only
  overflow: drop_oldest           # or disconnect
  max_clients: 32
```

`include` is required and accepts topic `GET` endpoints only. Action feedback is streamed
per goal from `/<action>/{goal_id}/stream`, not through this global stream.

## `http`

```yaml
http:
  bind: 127.0.0.1        # default; 0.0.0.0 must be explicit and logs a warning
  port: 8080
  base_path: /api/v1     # trailing slash accepted and stripped; "/" means no prefix
```

The gateway does not terminate TLS. Bind it to localhost and put a reverse proxy in front.

## `ros`

```yaml
ros:
  executor: auto         # auto | events | multithreaded
```

`auto` uses `rclpy.experimental.EventsExecutor` where it exists (Kilted and newer) and
`MultiThreadedExecutor` otherwise, logging which one it chose. Asking for `events` on a
distro without it is a startup error rather than a silent downgrade.

## `audit`

```yaml
audit:
  sink: stderr
# or
audit:
  sink: file
  path: /var/log/ros2_http_gateway/audit.jsonl
```

One JSON line per authenticated request: timestamp, user, roles, method, path, decision
and its reason, outcome, latency. Never tokens, never request bodies.

## `json`

```yaml
json:
  int64_as_string: true
```

On by default. `int64` and `uint64` are encoded as JSON strings because values above 2^53
do not survive a JavaScript `number`. The write direction accepts either a string or a
number regardless, since a path parameter is always a string.

## How ROS types become JSON

| ROS | JSON | Notes |
|---|---|---|
| `bool` | boolean | |
| `int8`..`uint32` | number | Range checked on the way in |
| `int64`, `uint64` | string | Number when `int64_as_string: false` |
| `float32`, `float64` | number or `null` | NaN and the infinities become `null`; JSON has no other spelling |
| `float32` collections | number | Printed as the shortest text that round-trips as `float32`, so `1.87` not `1.8700000047683716` |
| `string`, `wstring` | string | Bounded lengths are enforced |
| `byte` (scalar) | number 0-255 | |
| `uint8[]`, `byte[]` and sequences | base64 string | Also accepted as an array of integers on the way in |
| other arrays and sequences | array | Fixed-size lengths and sequence bounds are enforced |
| nested message | object | |

On the way in, a field that is absent keeps the default from the ROS message definition.
An **unknown** field is rejected rather than ignored, because silently dropping a
misspelled field is how an operator ends up believing a command was applied.

Bodies are read strictly: a JSON number must be a number and a JSON boolean must be a
boolean. `"1.5"` is not a double. The exceptions both exist so that a body this gateway
produced is a body it will accept:

- `int64`/`uint64` accept a string as well as a number, because `int64_as_string` emits one.
- `float32`/`float64` accept `null`, read back as NaN. Not lossless the other way: an
  infinity read as `null` returns as NaN.

Path parameters are always strings, and are coerced against the field they target before
reaching the converter, rather than by loosening every scalar. That is what keeps the
generated JSON Schemas usable: they never have to tell a client that any number anywhere
might arrive as text.

Ranges, fixed array lengths and sequence bounds are all enforced, and a violation returns
HTTP 400 quoting the dotted path of the field:

```json
{
  "success": false,
  "error_code": "BAD_REQUEST",
  "message": "header.stamp.sec: 2147483648 is outside [-2147483648, 2147483647]",
  "field": "header.stamp.sec"
}
```

The full status taxonomy is in [protocol.md](protocol.md).

Large binary payloads (images, point clouds) are a non-goal: base64 in JSON is the wrong
transport for them. See the non-goals in the README.
