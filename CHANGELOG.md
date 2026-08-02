# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `docs/decisions.md`: the settled questions as one-line records, so they are not re-litigated.
- An `e2e` suite driving the console in chromium through playwright, against the real demo node:
  wallet and role filtering, the auth matrix grid, a tile's staleness countdown and expiry, a write
  round-tripping into its read tile, the gate flipping both ways with the call agreeing, a goal
  through feedback to a result, cancel and re-cancel, TTL eviction, both stream transports and the
  heartbeat difference between them, and slow-consumer mode. Deselected by default.
- Startup checks on anything derived from config or from another layer raise instead of asserting,
  so `python -O` is a supported way to run this. The full suite passes under it.

### Fixed

- The console reported a cancel's outcome in the goal's status line, where the next poll overwrote
  it -- so a second cancel's `ALREADY_TERMINAL` was invisible. It has its own line now, which is
  also the right place for it: the answer to a cancel request is not a goal status. Found by the
  browser suite, which is exactly the class of bug the other tests could not see.

- The console's request/response half, entirely discovered from the manifest and the served
  schemas: a labelled multi-token wallet in local storage with a visible warning, an identity chip
  from `whoami`, topic tiles with a staleness countdown that flips the tile, schema-driven forms
  for writes and services with a validated raw-JSON fallback for arrays and deeper structures,
  per-call latency and a 20-call hammer reporting p50/p95.
- The auth matrix fires each endpoint as nobody, as a bad token and as every saved token. Anything
  that is not a GET is probed with a body no converter accepts, so the authorised row reads 400 --
  the token and its roles were accepted and nothing was published, called or submitted. A gated
  endpoint refuses even earlier and reads 200 with `PRECONDITION_FAILED`.
- Response outcomes are rendered as the protocol's own distinctions: 4xx never reached ROS, 503 hit
  a capacity limit, 200 with `PRECONDITION_FAILED` was refused by a state gate before ROS, 200 with
  another `error_code` reached ROS and came back unhappy, and a plain 200 worked.
- Gated commands show the live value of their precondition's state topic beside the button and
  predict the decision the way the policy engine makes it, including its three refusals: unlisted
  parameter value, stale state, and nothing published yet.
- Staleness is measured from when the browser received a sample, never from the server's wall-clock
  stamp, so a skewed client clock cannot grey out a live tile. The server's own `stale` flag stays
  authoritative once it is set.
- The console's logic sits behind a DOM-free seam and is tested through node against the schemas
  the gateway really generates, since CI has no browser.
- Goal cards: status by polling, feedback over the goal's own stream (fetch, because that stream
  needs a bearer header and `EventSource` cannot send one), cancel, `ALREADY_TERMINAL` on a second
  cancel, the result, a countdown to `result_ttl_s` and then the 404 once evicted. Several at once.
- An event-stream panel with connect/disconnect, a per-endpoint filter, heartbeats rendered faintly,
  a seq-gap detector, and a transport toggle between `fetch` with header auth and `EventSource` with
  the query token. The `EventSource` path also makes visible that the browser swallows comment
  frames, so heartbeats are invisible on it.
- Slow-consumer mode stops draining the reader so the gateway's per-client queue fills and the
  configured `overflow` policy takes over -- `drop_oldest` as gaps in `seq`, `disconnect` as the
  stream ending.
- An audit hint under every response and on every auth-matrix row: the decision, outcome and reason
  the request will have written to the audit log, derived from the answer because the audit sink is
  deliberately not served. A test compares the derivation against real records.

- `GET <base>/console/manifest`: everything the calling token may touch, and nothing else --
  kind, ROS name and type, roles, path parameters, staleness bound or timeout, `request_map`,
  precondition summaries, stream membership, and an action's four derived routes. Schemas are
  referenced by component key plus document URL rather than embedded.
- The manifest is filtered by role: an endpoint a token cannot use is absent, and so are its ROS
  name, its type and its membership of the event stream. This is defence in depth and a
  correctness property for the console, not a security boundary -- roles gate action, not
  description, and the 403 is the boundary. Documented in docs/security.md.
- `GET <base>/whoami`: the caller's user and roles, for any valid token including one whose role
  no endpoint uses. Served by every deployment whether the console is enabled or not, and
  described in the OpenAPI document. The only route with no role requirement, so the only one
  that cannot answer 403; an empty role set is unreachable from a config, which is what makes
  that safe.
- `http.console` (default **false**) serves the test console from package data: one HTML, one CSS,
  one JS, no CDN and no build step. Off means the routes are not registered at all. Its three
  paths are reserved for collision checking regardless, so enabling it cannot break a config that
  already loaded.
- `ros2 run ros2_http_gateway ros2-http-gateway-demo`: one node with a mission state cycling
  IDLE/RUNNING, a 2 Hz chatter topic, a `std_srvs/Trigger` service and a deliberately slow
  Fibonacci action, plus `examples/demo.yaml` exposing all of it with an operator and a viewer
  token, a state precondition and a two-endpoint event stream.

## [0.1.0] - 2026-08-02

First release.

### Added

- `bench/`: one command (`python3 bench/run.py`) produces a markdown results table with the
  machine, the ROS distro, the RMW and the load driver recorded alongside the numbers, and exits
  non-zero if a budget asserted by an earlier phase is breached.
- Scenarios: cached topic `GET` throughput and latency percentiles, an unauthenticated refusal for
  comparison, service round trip gated and ungated (interleaved, so neither inherits the other's
  warm-up), precondition cost in process, converter speedup against `rosidl_runtime_py`, event
  stream fan-out at 1/8/32 clients, and the 1 kHz subscription soak measured both quiet and
  subscribed with per-thread attribution.
- `oha` or `wrk` are used when either is on `PATH`, otherwise a built-in asyncio driver spread over
  four worker processes. Which one ran is recorded, because the numbers are not comparable across
  drivers and the client shares the machine with the gateway either way.
- Reference numbers checked in as `bench/RESULTS.md`. Notably 32 stream clients cost 1.7x the CPU
  of one, which is what "encode once, fan out bytes" was for.

- OpenAPI 3.1 document, generated at startup from the config and the same JSON Schemas the
  converters were built from, served at `<base>/openapi.json`. Every configured endpoint with its
  methods, the response envelopes, the error codes each route can produce, the four action routes
  as the async-job pattern, and the event stream with its event names and `?access_token=`.
- 3.1 rather than 3.0 so the schema dialect is JSON Schema 2020-12: a nullable float, a wide
  integer accepted as string-or-number and a base64 byte array all survive unaltered. Nested type
  schemas are hoisted into `components/schemas` with their `$ref`s rewritten, because a `$ref`
  resolves against the document root and a schema-local `$defs` would silently dangle.
- The document is authenticated, like every route but `/health`: served openly it would hand an
  anonymous caller the whole surface. Any authenticated caller sees all of it, since 403-versus-404
  already lets them enumerate the same list.
- `--check --dump-openapi PATH` writes it without starting the server, key-sorted and indented so
  a CI job can diff it; `-` writes to stdout and suppresses the usual report.
- `servers` and the UI's asset references honour `root_path` and `X-Forwarded-Prefix`, so a gateway
  behind `location /cell3/` advertises prefix-correct URLs and "Try it out" reaches them.
- Swagger UI at `<base>/docs`, vendored (~1.7 MB, Apache-2.0, notices alongside it) with no CDN.
  The page is unauthenticated because a browser cannot set a header on a navigation; it asks for a
  token, fetches the document with it, and keeps it in the page rather than in storage.

- The global event stream is served at `sse.path` (default `/api/v1/events`): one snapshot per
  included endpoint on connect, then deltas. `data` is byte-identical to the endpoint's GET
  body, because the same memoised encoding backs both.
- Each client sees only the endpoints its own roles permit. One connection carries several
  endpoints, so filtering is per client; a token holding none of their roles gets 403. There is
  no `roles` key on `sse` -- adding an endpoint to `include` cannot widen who may read it.
- A sample is framed once on the loop and the same bytes object is handed to every client, so N
  listeners cost N queue appends. The ROS thread only flags the endpoint and wakes the loop, and
  does neither while nobody is connected.
- `sse.overflow` handles a client that stops keeping up: `drop_oldest` (default) discards its
  oldest queued events so the newest state still arrives, `disconnect` closes it. One slow
  client never slows a fast one. Past `sse.max_clients` the route answers 503 with
  `TOO_MANY_CLIENTS`.
- A comment frame every 15 seconds, per client and only while the stream is quiet, so a proxy
  cannot idle out a connection to a quiet robot.
- The stream accepts `?access_token=` because browser `EventSource` cannot set headers. It is
  the only route that does, and its audit record carries `"credential": "query"`; the query
  string is never logged.
- `SIGTERM` ends every open stream before uvicorn waits for in-flight requests. Without that a
  single connected stream would hold the process open until the client hung up, since a stream
  is an in-flight request that never completes on its own.

- Action endpoints are served, with all four routes: `POST` submits a goal and answers 202 with
  its id, `GET` reads status, result and newest feedback, `GET .../stream` streams feedback and
  then one terminal event before closing, and `DELETE` requests cancellation.
- Goal registry with TTL eviction. A terminal goal stays queryable for `actions.result_ttl_s`
  and then answers 404. Eviction is lazy -- a sweep on goal creation, the only way the registry
  grows -- so there is no timer and an idle gateway does no work.
- Goal ids are namespaced per endpoint, so two endpoints on the same action cannot read each
  other's goals.
- Feedback is encoded once per message and the same bytes are handed to every listener.
- Preconditions gate goal submission only. Status, stream and cancel are deliberately not
  gated: a state gate able to block a cancellation would be the opposite of a safety feature.

- Service endpoints are served. A call returns the response in the documented envelope; no
  server gives `SERVICE_UNAVAILABLE` and a slow one gives `TIMEOUT`, both as HTTP 200 because
  the request itself was fine. A timed-out request is withdrawn, so a late reply cannot be
  mistaken for the answer to a later call.
- `request_map` fills request fields from path parameters, including dotted targets. Path text
  is checked against a plain digit pattern before conversion, so `1_0` cannot silently mean 10
  and `nan` cannot become a float field's value. The text
  conversion for each target is chosen at startup from the field's declared type, so an
  impossible mapping fails before the port opens rather than as a confusing 400.
- An endpoint named by a precondition must set `stale_after_ms`. Without a bound the gate
  could never notice the state publisher had died and would authorise against the last sample
  it ever saw, so this is a startup error rather than a warning. Streaming an unbounded
  endpoint over SSE remains fine.
- State preconditions. A refusal is HTTP 200 with `PRECONDITION_FAILED` and is audited as a
  denial with its reason. Refused rather than waved through: an unlisted parameter value, a
  state endpoint that has received nothing yet, and a sample older than its own
  `stale_after_ms`. Measured p99 for evaluation is 2.1us against the 1ms budget.
- Auth and the role check run before policy, so a caller who may not use an endpoint learns
  nothing about the robot's state from the refusal.

- The gateway serves. `ros2-http-gateway config.yaml` brings up a ROS node and an HTTP
  listener: topic `GET` returns the latest cached sample, topic `POST` publishes, and
  `GET <base>/health` answers unauthenticated.
- Bearer-token authentication with per-endpoint role checks. Digests are compared with
  `hmac.compare_digest`, and every configured token is checked even after a match so that
  timing does not reveal which one was presented. `Authenticator` is a protocol, so JWT or
  OIDC can be added without touching the request path.
- Audit logging to stderr or a file: one JSON line per request that reached a configured
  route, denials included. Recording never blocks the request path, writes are batched, and
  there are no fields for a token or a body so neither can be logged by accident.
- Topic caches built on `raw=True` subscriptions. A received message costs one tuple store;
  deserialisation and JSON encoding happen when a read arrives and are memoised by sequence
  number, so a 1 kHz topic read once a second is decoded once a second.
- Executor selection per `ros.executor`, feature-detecting `EventsExecutor`. `auto` falls
  back to `MultiThreadedExecutor`; `events` on a distro without it is a startup error.
- Service and action endpoints are routed and enforce auth, but answer 501 naming the phase
  that will serve them, rather than 404 which reads like a config mistake.
- Per-endpoint `qos: default | sensor_data`. A reliable subscription never matches a
  best-effort publisher, which is how most sensors publish, so without this an endpoint
  pointed at a camera would stay empty forever. The chosen preset is logged, and a
  subscription that finds no publisher within a few seconds now produces a warning naming
  the topic and this setting instead of failing silently.
- `docs/security.md`: the security model, including the deliberate gap that unmatched routes
  leave no audit record, so path-scanning probes are invisible to the audit file.

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

- **Breaking, pre-release:** the `gateway` console-script alias is gone. Wheel installs
  generate console scripts from the same entry points, so it claimed `~/.local/bin/gateway`
  on every `pip install`. Use `ros2 run ros2_http_gateway ros2-http-gateway`.
- Request bodies are read strictly: a JSON number must be a number and a JSON boolean must
  be a boolean. Previously every scalar also accepted a string, which the generated write
  schemas did not admit, so an OpenAPI-validating client could be refused a request the
  gateway would have taken, or vice versa. `int64`/`uint64` still accept a string, and float
  fields now accept `null` as NaN, both so that a response body remains a legal request body.
- Two routes that could match the same request are now a startup error, not just the
  generated action subroutes: `/a/{x}` versus `/a/{y}`, a literal shadowed by a template on
  the same method, `sse.path` overlapping any endpoint template, and the built-in
  `/health`, `/openapi.json` and `/docs`.
- Raw-IDL `wchar` is refused rather than guessed at; no interface in the test battery has
  one, so its Python representation was never verified. `.msg` `char` is unaffected (it is
  IDL `uint8`, now pinned by a test).
- `package.xml` declares the interface packages the tests and examples actually need
  (`test_msgs`, `example_interfaces`, `std_srvs`, `diagnostic_msgs`), so a rosdep-driven
  build no longer depends on CI having apt-installed them by hand.

- `numpy` is now a declared runtime dependency. It always was one in practice: `codegen.py`
  imports it and every generated message module does too, but a pip install into a plain
  virtualenv would not inherit the Debian `python3-numpy` that a ROS install relies on.
- CI installs into a virtualenv inside the ROS containers instead of the container's
  Debian-managed interpreter, and type-checks on one interpreter rather than the whole
  version matrix. Both were breaking on the dependency toolchain rather than on this
  package.
- The test suite now fails, rather than skipping, when `GATEWAY_REQUIRE_ROS=1` and the ROS
  stack will not import. A broken environment previously looked identical to a deliberately
  ROS-free one, so a distro whose interface types could not load reported a green run having
  executed 96 tests instead of 485.
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

### Fixed

- A per-goal stream whose client had fallen behind could strand every other listener on that
  goal. The terminal sentinel used an unguarded put and raised `QueueFull` on a saturated queue,
  which abandoned the remaining subscribers without a closing event.
- A running goal now reports `executing` rather than staying `accepted` until it finishes. A
  terminal status is still only reported once the result is available, so it always arrives with
  one.
- Feedback arriving after a goal finished no longer changes its recorded last feedback.

- A racing staleness test: it read the cache while publishes were still in flight, so the
  sample could change under an assertion that it would not. Tests now wait for the publisher
  to go quiet first.

- The test suite no longer assumes every distro has an events executor, and now honours the
  `GATEWAY_TEST_EXECUTOR` the CI matrix has been setting since the start but nothing read, so
  each distro job exercises the executor it was asked for instead of both twice.
- QoS profiles are built from `QoSProfile` and `qos_profile_sensor_data` rather than read off
  `QoSPresetProfiles`, whose membership varies by distro: Humble has no `DEFAULT` member, so
  every ROS test there failed at setup with `AttributeError: DEFAULT`.
- CI constrains numpy in the ROS venv to the major version the distro ships. Installing
  numpy 2 alongside Humble's numpy-1-built rosidl extensions is the documented incompatible
  direction and showed up as a segfault plus a scatter of errors.
- Round-trip tests no longer compare serialised CDR bytes. CDR pads between fields of
  differing widths and rosidl leaves that padding uninitialised, so two independently built
  messages could differ in bytes no field maps to; measured about once in two hundred
  attempts on `test_msgs/BasicTypes`. They now compare field by field, which is also stronger
  than comparing the JSON encodings.
- A handler that raises is now audited (status 500, `outcome: INTERNAL`) and answered with the
  standard error envelope. It previously escaped to Starlette's error machinery, leaving no
  record of exactly the request an incident review would care about.
- Staleness is measured on the monotonic clock. It used the same wall-clock reading the
  response reports, so an NTP step could make fresh data look stale or stale data look fresh.
- `TopicCache.seq` is read from the newest sample rather than from the counter, so it can no
  longer briefly report a sequence number whose sample is not yet reachable.
- `datetime.UTC` in the audit timestamp broke Python 3.10, which is what Humble ships; now
  `timezone.utc`. mypy is configured to target the 3.10 floor again (with numpy's PEP 695
  stubs shadowed by a local shim) so this class of mistake is a type error rather than an
  ImportError in CI.
- The ROS node is destroyed on shutdown. It was not: uvicorn restores the default signal
  handler and re-raises the captured signal as `run()` unwinds, so teardown placed after
  that call never executed. Teardown now happens in the lifespan, which is also the correct
  ordering (after HTTP has drained).
- Ctrl-C no longer prints a `KeyboardInterrupt` traceback; the gateway exits 130 quietly.

- A fixed-size `uint8[N]` built from a request body was a read-only numpy view, so anything
  mutating the message in place hit `assignment destination is read-only`.
- Element errors inside a byte collection given as an array of integers reported the field
  as `data` rather than `data[]`.
- `_schema_message` matched the endpoint `oneOf` by substring-searching the schema path;
  it now matches the document location, and covers `auth.tokens` too.
- Type resolution polled all three graph APIs on every tick even when only topics were
  pending.

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

[Unreleased]: https://github.com/justagist/ros2_http_gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/justagist/ros2_http_gateway/releases/tag/v0.1.0
