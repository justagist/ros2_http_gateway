"""The measurements themselves. Each returns rows for the results table.

Every scenario is self-contained: it brings up whatever ROS and HTTP it needs, measures, and
tears down. Thresholds live with the scenario that can breach them, so a regression names the
budget it broke.
"""

from __future__ import annotations

import contextlib
import functools
import json
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from harness import (
    OPERATOR_TOKEN,
    Gateway,
    load,
    ros_side,
    running,
)

# Asserted budgets, from the phases that asserted them. `None` means informational: reported,
# never a failure, because the figure is dominated by the machine and the middleware.
POLICY_P99_MS = 1.0
CONVERTER_SPEEDUP = 5.0
IDLE_CPU_PERCENT: float | None = 5.0


@dataclass
class Row:
    """One line of the results table."""

    scenario: str
    metric: str
    value: str
    budget: str = ""
    verdict: str = ""


@dataclass
class Failure:
    """A breached budget, reported at the end so every scenario still runs."""

    scenario: str
    detail: str


def topic_get(directory: Path, *, seconds: float, connections: int) -> Iterator[Row]:
    """Throughput and latency of a cached topic read: the hot path, memoised by seq."""
    with ros_side("talker", "200"), running(directory) as gateway:
        _wait_for_data(gateway, "/state")
        result = load(
            gateway.url("/state"), token=OPERATOR_TOKEN, seconds=seconds, connections=connections
        )

    yield Row("topic GET", "throughput", f"{result.rps:,.0f} req/s")
    yield Row("topic GET", "p50 latency", f"{result.percentile(0.50):.2f} ms")
    yield Row("topic GET", "p95 latency", f"{result.percentile(0.95):.2f} ms")
    yield Row("topic GET", "p99 latency", f"{result.percentile(0.99):.2f} ms")
    yield Row("topic GET", "non-2xx", f"{result.errors}")


def topic_get_unauthenticated(
    directory: Path, *, seconds: float, connections: int
) -> Iterator[Row]:
    """The same route refused, which is the cheapest possible path through the guard."""
    with running(directory) as gateway:
        result = load(gateway.url("/state"), token=None, seconds=seconds, connections=connections)
    yield Row("401 refusal", "throughput", f"{result.rps:,.0f} req/s")
    yield Row("401 refusal", "p99 latency", f"{result.percentile(0.99):.2f} ms")


def service_call(directory: Path, *, samples: int) -> Iterator[Row]:
    """Round-trip latency for a real service call, sequentially: this waits on ROS."""
    with ros_side("talker", "200", "service"), running(directory) as gateway:
        _wait_for_data(gateway, "/state")
        _wait_for_service(gateway, "/trigger")
        plain, gated = _interleaved_latencies(gateway, "/trigger", "/gated", samples)

    for name, latencies in (("service POST", plain), ("gated service POST", gated)):
        ordered = sorted(latencies)
        yield Row(name, "p50 latency", f"{_at(ordered, 0.50):.2f} ms")
        yield Row(name, "p99 latency", f"{_at(ordered, 0.99):.2f} ms")

    # Interleaved, so warm-up and scheduling noise land on both. Still an end-to-end delta and
    # therefore noisy: `policy evaluation` below measures the gate itself.
    overhead = statistics.median(gated) - statistics.median(plain)
    yield Row(
        "state precondition",
        "median added latency",
        f"{overhead * 1000:+.0f} us",
        f"< {POLICY_P99_MS:g} ms",
    )


def policy_cost(directory: Path) -> tuple[list[Row], list[Failure]]:
    """Precondition evaluation on its own, in process: the phase 3 budget is p99 under 1 ms."""
    from rclpy.serialization import serialize_message
    from rosidl_runtime_py.utilities import get_message

    from ros2_http_gateway.cache import TopicCache
    from ros2_http_gateway.codegen import build_msg_to_dict
    from ros2_http_gateway.config import Precondition
    from ros2_http_gateway.policy import build_policy

    del directory
    message_type = get_message("example_interfaces/msg/String")
    message = message_type()
    message.data = "RUNNING"
    cache = TopicCache(message_type, build_msg_to_dict(message_type), 5000)
    cache.offer(serialize_message(message))

    engine = build_policy(
        {
            "/gated": [
                Precondition(
                    state_endpoint="/state",
                    field_path=("data",),
                    key=None,
                    allow={"*": frozenset({"RUNNING"})},
                )
            ]
        },
        {"/state": cache},
    )
    engine.evaluate("/gated", {})  # warm the memoised decode, as a served request would find it

    samples = []
    for _ in range(20_000):
        started = time.perf_counter()
        engine.evaluate("/gated", {})
        samples.append(time.perf_counter() - started)
    samples.sort()
    p99_ms = samples[int(len(samples) * 0.99)] * 1000.0

    rows = [
        Row(
            "policy evaluation",
            "p99",
            f"{p99_ms * 1000:.1f} us",
            f"< {POLICY_P99_MS:g} ms",
            "ok" if p99_ms < POLICY_P99_MS else "FAIL",
        )
    ]
    failures = (
        []
        if p99_ms < POLICY_P99_MS
        else [Failure("policy evaluation", f"p99 {p99_ms:.3f} ms exceeds {POLICY_P99_MS} ms")]
    )
    return rows, failures


def _time(call: Callable[[], object], number: int = 2_000, repeat: int = 5) -> float:
    """Seconds per call, taking the best run: the others measured the machine's other work."""
    import timeit

    return min(timeit.repeat(call, number=number, repeat=repeat)) / number


def converters(directory: Path) -> tuple[list[Row], list[Failure]]:
    """Generated converter against `rosidl_runtime_py`: the phase 1 budget is 5x on Imu."""
    from rosidl_runtime_py.convert import message_to_ordereddict
    from rosidl_runtime_py.utilities import get_message

    from ros2_http_gateway.codegen import build_msg_to_dict

    del directory
    rows: list[Row] = []
    failures: list[Failure] = []
    for type_name in ("sensor_msgs/msg/Imu", "geometry_msgs/msg/Twist", "sensor_msgs/msg/Image"):
        message_type = get_message(type_name)
        message = message_type()
        generated = build_msg_to_dict(message_type)
        ours = _time(functools.partial(generated, message))
        theirs = _time(functools.partial(message_to_ordereddict, message))
        speedup = theirs / ours
        budget = f">= {CONVERTER_SPEEDUP:g}x" if type_name == "sensor_msgs/msg/Imu" else ""
        verdict = ""
        if budget:
            verdict = "ok" if speedup >= CONVERTER_SPEEDUP else "FAIL"
            if speedup < CONVERTER_SPEEDUP:
                failures.append(
                    Failure(
                        "converters",
                        f"{type_name} only {speedup:.1f}x, budget {CONVERTER_SPEEDUP}x",
                    )
                )
        short = type_name.split("/")[-1]
        rows.append(
            Row("msg -> dict", f"{short} speedup", f"{speedup:.1f}x faster", budget, verdict)
        )
        rows.append(Row("msg -> dict", f"{short} time", f"{ours * 1e6:.2f} us"))
    return rows, failures


def sse_fanout(directory: Path, *, clients: tuple[int, ...], seconds: float) -> Iterator[Row]:
    """Does one connection cost the same as thirty-two? Encode once, fan out bytes says yes."""
    import asyncio

    for count in clients:
        with (
            ros_side("talker", "200"),
            running(
                directory, sse={"include": ["/state"], "max_clients": max(clients) + 4}
            ) as gateway,
        ):
            _wait_for_data(gateway, "/state")
            before = gateway.cpu_seconds()
            started = time.monotonic()
            delivered, gaps = asyncio.run(_read_streams(gateway, count, seconds))
            elapsed = time.monotonic() - started
            used = gateway.cpu_seconds() - before

        yield Row(f"SSE {count} client(s)", "events delivered", f"{delivered / elapsed:,.0f} /s")
        yield Row(f"SSE {count} client(s)", "gateway CPU", f"{used / elapsed * 100:.1f}% of a core")
        yield Row(f"SSE {count} client(s)", "seq gaps", f"{gaps}")


async def _read_streams(gateway: Gateway, clients: int, seconds: float) -> tuple[int, int]:
    """Read the event stream from `clients` connections at once, counting frames and gaps."""
    import asyncio

    async def one() -> tuple[int, int]:
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
        writer.write(
            f"GET /api/v1/events HTTP/1.1\r\nHost: bench\r\n"
            f"Authorization: Bearer {OPERATOR_TOKEN}\r\nAccept: text/event-stream\r\n\r\n".encode()
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        deadline = time.monotonic() + seconds
        frames = gaps = 0
        previous = 0
        try:
            while time.monotonic() < deadline:
                block = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=20)
                # A streamed response is chunked, so the block carries hex chunk sizes around
                # the frame. Look for the data line rather than the start of the block, which
                # also skips the heartbeat comment frames.
                seq = _seq_of(block)
                if seq == 0:
                    continue
                frames += 1
                if previous and seq > previous + 1:
                    gaps += 1
                previous = seq
        except (TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
        return frames, gaps

    results = await asyncio.gather(*(one() for _ in range(clients)))
    return sum(frames for frames, _ in results), sum(gaps for _, gaps in results)


def _seq_of(block: bytes) -> int:
    """The `seq` of a streamed frame, or 0 for a heartbeat or a chunk boundary."""
    marker = block.find(b"data: ")
    if marker < 0:
        return 0
    line = block[marker + len(b"data: ") :].split(b"\n", 1)[0]
    with contextlib.suppress(ValueError, KeyError):
        return int(json.loads(line)["seq"])
    return 0


def idle_cpu(directory: Path, *, seconds: float) -> tuple[list[Row], list[Failure]]:
    """A 1 kHz subscription with no HTTP traffic, quiet and loaded, attributed per thread."""
    with running(directory) as gateway:
        # Quiet first, with nobody publishing at all: this is the figure that catches a
        # busy-wait, and it cannot be measured while a talker is running.
        time.sleep(0.5)
        quiet_before, quiet_start = gateway.cpu_seconds(), time.monotonic()
        time.sleep(min(2.0, seconds))
        quiet = (gateway.cpu_seconds() - quiet_before) / (time.monotonic() - quiet_start) * 100

        with ros_side("talker", "1000"):
            _wait_for_data(gateway, "/state")
            time.sleep(0.5)
            before, threads_before, start = (
                gateway.cpu_seconds(),
                gateway.thread_cpu(),
                time.monotonic(),
            )
            time.sleep(seconds)
            elapsed = time.monotonic() - start
            percent = (gateway.cpu_seconds() - before) / elapsed * 100
            threads = gateway.thread_cpu()

    busiest = sorted(
        ((after - threads_before.get(name, 0.0)) / elapsed * 100, name)
        for name, after in threads.items()
    )[::-1]
    rows = [
        Row("1 kHz subscription", "CPU, quiet", f"{quiet:.1f}% of a core"),
        Row(
            "1 kHz subscription",
            "CPU, subscribed",
            f"{percent:.1f}% of a core",
            f"< {IDLE_CPU_PERCENT:g}% (informational)",
            "ok" if IDLE_CPU_PERCENT is None or percent < IDLE_CPU_PERCENT else "over",
        ),
    ]
    rows += [
        Row("1 kHz subscription", f"thread {name}", f"{share:.1f}%")
        for share, name in busiest[:3]
        if share >= 1.0
    ]
    # Informational by design: the figure is dominated by the distro and the RMW, and Humble
    # spends ~80% of a core in rclpy's dispatch before any of our code runs.
    return rows, []


# --------------------------------------------------------------------------------- helpers


def _interleaved_latencies(
    gateway: Gateway, first: str, second: str, samples: int
) -> tuple[list[float], list[float]]:
    """Alternate between two endpoints so neither gets the other's warm-up as an advantage."""
    collected: dict[str, list[float]] = {first: [], second: []}
    for _ in range(samples):
        for path in (first, second):
            started = time.perf_counter()
            status, _ = gateway.request(path, body=b"{}")
            elapsed = time.perf_counter() - started
            if status == 200:
                collected[path].append(elapsed * 1000.0)
    for path, latencies in collected.items():
        if not latencies:
            raise RuntimeError(f"every call to {path} failed")
    return collected[first], collected[second]


def _at(ordered: list[float], fraction: float) -> float:
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _wait_for_data(gateway: Gateway, path: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if '"seq":0' not in gateway.request(path)[1]:
            return
        time.sleep(0.05)
    raise RuntimeError(f"no sample ever reached {path}")


def _wait_for_service(gateway: Gateway, path: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = gateway.request(path, body=b"{}")
        if status == 200 and "SERVICE_UNAVAILABLE" not in body:
            return
        time.sleep(0.1)
    raise RuntimeError(f"the service behind {path} never appeared")
