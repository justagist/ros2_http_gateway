# Decisions

Settled questions, one line each, so they are not re-litigated. Each says what was decided and the
reason that decided it; where the reason is long, it points at the doc that carries it.

Order is roughly the order they were settled in, not importance.

## Packaging and tooling

1. **One source of truth for the version**: `ros2_http_gateway/_version.py`. `pyproject.toml` reads
   it, `setup.py` executes it, `package.xml` mirrors it, and a test fails on drift.
2. **`requires-python` lives in `setup.py`, not `pyproject.toml`.** colcon identifies a package by
   `ast.literal_eval(repr(dist.__dict__))`, and setuptools turns a pyproject `requires-python` into
   a `SpecifierSet` whose repr is not a literal, so the build dies before it starts.
3. **Every console script is namespaced** (`ros2-http-gateway`, `ros2-http-gateway-demo`). Wheel
   installs generate scripts from the same entry points, so a friendly `gateway` or `demo_node`
   would claim a generic name in `~/.local/bin` on every `pip install`.
4. **`rclpy` is never a pip dependency**, only an `<exec_depend>`. It comes from the sourced ROS
   environment; `--help` and `--version` work without one and anything else gives a single
   actionable line.
5. **`python -O` is supported.** Startup checks on anything derived from config or from another
   layer raise rather than assert. The one remaining `assert`, in `sse.py`, guards an invariant that
   class itself guarantees on a per-message path, which is exactly what `assert` is for.

## Config engine

6. **Every problem in a config is reported, not just the first**, and every message names the
   location and the fix. This is the tool a CI job runs.
7. **Unknown keys are always an error.** A typo in a security-relevant key must not fail open.
8. **`--check` is hermetic by default**, needing no running system when a config pins its `type:`
   values, so it can gate CI. `--check --online` additionally verifies pinned types against the live
   graph, which is a deploy-time smoke test and a different question.
9. **`roles:` is required and an empty list is refused**, at both the schema and the model layer.
   An empty role set means "any authenticated caller" inside the request guard -- which is what
   `/whoami` needs -- and an author writing `roles: []` to mean "nobody" must never reach it.
10. **An endpoint named by a precondition must set `stale_after_ms`.** Without a bound the gate can
    never notice the state publisher died, and would authorise against the last sample it ever saw.
    An error rather than a warning: a log line is not a safety control.
11. **Any two routes that could match the same request are a startup error**, including the routes
    an action endpoint generates and the built-in ones. Otherwise which answers comes down to
    registration order.

## Wire protocol

12. **The status taxonomy is "did it reach ROS".** 4xx never did and retrying unchanged will fail
    the same way; 503 hit a capacity limit and may succeed later; 200 did reach ROS and
    `error_code` carries the outcome, because "the service timed out" is a result, not a protocol
    error. One envelope shape for all of them. See [protocol.md](protocol.md).
13. **`success` describes the call, not the robot's opinion.** A service answering
    `success: false` in its own response is still `"success": true` at this layer, with its fields
    under `response`, so a client can tell "could not ask" from "the robot said no".
14. **Request bodies are read strictly**: a JSON number must be a number. Path parameters, which
    are always text, are coerced against their target field *before* a converter sees them, so the
    generated schemas never have to advertise string-for-number and an OpenAPI-validating client is
    never misled.
15. **Unknown fields in a body are rejected, not ignored.** Silently dropping a misspelled field is
    how an operator ends up believing a command was applied.
16. **`int64_as_string` defaults to true.** Values above 2^53 do not survive a JavaScript `number`.
17. **Preconditions gate goal submission only.** A state gate able to stop an operator cancelling a
    goal would be the opposite of a safety feature.
18. **A terminal goal status always comes with its result.** The underlying handle reports terminal
    slightly before the result is collected, so the gateway keeps reporting the running status until
    it holds both.
19. **The event stream is latest-value.** A topic outrunning the loop shows gaps in `seq`, exactly
    as a client polling the GET route would miss samples. If you need every message, subscribe in
    ROS.
20. **`?access_token=` is accepted on the event stream and nowhere else**, because browser
    `EventSource` cannot set headers. Such requests are marked `"credential": "query"` in the audit
    record and the query string is never logged. The trade is written down in
    [security.md](security.md).

## Security posture

21. **Roles gate action, not description. The 403 is the boundary, and the only boundary.** An
    authenticated caller can enumerate the configured routes by asking -- unknown paths answer 404,
    forbidden ones 403 -- so no listing pretends otherwise.
22. **The OpenAPI document requires a token but is not filtered by role.** Served openly it would
    hand an anonymous caller the whole surface; filtered per role it would only hide what (21) says
    is already discoverable.
23. **The console manifest *is* filtered by role.** That is defence in depth and a correctness
    property -- the console must not offer a button that can only 403 -- and explicitly not a
    boundary. Do not design against it.
24. **A precondition keeps naming its state endpoint even when the caller cannot read it**: the
    one reference the manifest does not filter. `policy.py` already puts that path in the refusal
    message the same caller receives, so withholding it here would hide in one place what the
    gateway volunteers in another, and a caller entitled to use a gated endpoint is entitled to know
    what gates it.
25. **The two UI shells (`/docs`, `/console`) are unauthenticated; everything with information in
    them is not.** A browser cannot attach a header to a top-level navigation, so a guarded page
    could never be opened. Both shells are inert and asserted to be: the console's own three files
    and the Swagger UI page are each tested to mention no ROS name, interface type or endpoint path.
    Each asks for a token before fetching anything.
26. **The gateway does not terminate TLS**, binds localhost unless told otherwise, and has no rate
    limit or body size limit. All of that belongs in the reverse proxy, whose configuration is in
    the README.
27. **The audit sink is never served over HTTP.** Exposing it would hand one caller another's
    activity. The console derives the audit line it expects from the response instead, and a test
    compares that derivation against the real records.
28. **The console is off by default** (`http.console: false`), and off means the routes are not
    registered at all.

## Architecture

29. **Two runtimes with a hard boundary.** ROS callbacks never run on the asyncio loop; the crossing
    is `loop.call_soon_threadsafe` and thread-safe queues only.
30. **All expensive work happens at startup** -- config validation, type resolution, converter
    generation, route and policy tables, the OpenAPI document -- so a request is lookups and
    precompiled code.
31. **Converters are generated Python, compiled at startup.** `codegen.py` is allowed to be
    unreadable and is held to golden-file and round-trip tests instead; it measured 44x
    `rosidl_runtime_py` on `sensor_msgs/Imu`.
32. **Publishing goes through a worker thread, not a guard condition**, because
    `EventsExecutor` raises `RuntimeError: Guard conditions not supported`.
33. **Topic samples are cached raw and encoded lazily, memoised by `seq`.** The SSE frame reuses
    that same encoding, which is what makes a streamed `data` byte-identical to the GET body.
34. **Event stream frames are built on the loop, not the ROS thread**, because the cache's decode
    and encode are loop-confined -- and building them there is what makes the bytes identical to a
    GET.
35. **Streams are ended in `Server.shutdown`, before uvicorn waits for in-flight requests.** A
    stream never completes on its own, so the lifespan is too late and `SIGTERM` would appear to be
    ignored.

## Process

36. **The publish workflow exists and is triggered only by pushing a `v*` tag.** No
    `workflow_dispatch`, no branch trigger, and no agent or automation pushes tags, creates
    releases or uploads to PyPI. That act is the maintainer's, always. See
    [release.md](release.md).
37. **Humble stays supported.** It costs a handful of comments and a 14-line mypy stub. Its 1 kHz
    CPU cost (~80% of a core against under 5% on Jazzy) is rclpy's per-message dispatch, upstream
    and unavoidable from here, and is documented rather than worked around.
38. **Benchmarks record the machine, the distro, the RMW and the load driver**, because every
    number depends on all four, and throughput is reported as a floor since the driver shares the
    machine.
