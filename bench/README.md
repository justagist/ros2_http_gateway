# Benchmarks

```bash
python3 bench/run.py                      # everything, ~70 s
python3 bench/run.py --quick              # shorter, noisier
python3 bench/run.py --only topic,sse     # a subset
python3 bench/run.py --out bench/RESULTS.md
```

Needs a sourced ROS 2 environment. If `RMW_IMPLEMENTATION` is `rmw_zenoh_cpp`, start its router
first, or the ROS side of every scenario will fail to discover the gateway.

Exits non-zero when a budget asserted by an earlier phase is breached, and prints
`REGRESSION <scenario>: <detail>` for each. [RESULTS.md](RESULTS.md) holds the reference-machine
numbers, regenerated with `--out`.

## What is measured, and what each number is worth

| Scenario | Measures | Read it as |
|---|---|---|
| `converters` | generated `msg -> dict` against `rosidl_runtime_py.message_to_ordereddict` | in-process, low noise. The 5x budget on `Imu` is asserted |
| `policy` | one precondition evaluation, in process | the real cost of a state gate: a dict lookup and a set test |
| `topic` | cached topic `GET` throughput and latency, plus an unauthenticated refusal | a floor, not a ceiling -- see below |
| `service` | end-to-end round trip through a real ROS service, gated and ungated, interleaved | dominated by the ROS round trip |
| `sse` | one publisher, 1/8/32 stream clients: events delivered and gateway CPU | the "encode once, fan out bytes" claim, quantified |
| `cpu` | a 1 kHz subscription with no HTTP traffic, quiet and subscribed, attributed per thread | informational; see the note below |

## Caveats worth stating

**The load driver competes with the gateway.** `oha` or `wrk` are used when either is on `PATH`;
otherwise a built-in asyncio driver runs in four worker processes on the same machine. Either
way the client and the server share cores, so throughput and latency describe the *pair*. Treat
throughput as a floor. The driver in use is recorded in the results header, because the numbers
are not comparable across drivers.

**The 1 kHz CPU figure is informational and always will be.** It is dominated by the distro and
the middleware, not by this package:

- Humble spends most of a core in rclpy's per-message dispatch at ~900 msg/s, before any code
  here runs. Measured at 82% against under 5% on Jazzy.
- `rmw_zenoh_cpp` costs noticeably more than `rmw_fastrtps_cpp` on the same machine: 18% against
  13% for the same subscription.

So the number to watch is not the absolute percentage but whether it stays close to a
do-nothing subscriber *on the same distro and RMW pairing*. The quiet figure is the one that
would catch a real regression here: a gateway with no traffic should cost approximately nothing,
and anything above about 1% means something in this package is polling.

**Per-thread attribution is the diagnostic.** A `python` thread implicates this package or
rclpy; a `dds.*` or `zenoh` thread implicates the middleware. That distinction is what turned an
unexplained 111% CPU reading on Humble into a specific, upstream answer.

## Adding a scenario

Write a function in `scenarios.py` returning `Row`s, add it to `SCENARIOS` and `_run` in
`run.py`. Return `(rows, failures)` instead of yielding if it can breach a budget -- a `Failure`
is collected and reported at the end, so one breach does not stop the rest of the suite.
