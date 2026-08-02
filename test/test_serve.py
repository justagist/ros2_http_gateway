"""Serving tests: a real socket, real uvloop, real signals, a real subprocess.

Everything else drives the ASGI app in-process. This module exists for the parts that only
appear when the gateway is a process: uvicorn with uvloop and httptools, signal handling, and
whether the ROS node is actually destroyed on the way out.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import (
    OPERATOR_TOKEN,
    STATE_TOPIC,
    Talker,
    available_executors,
    config_document,
    write_config,
)

pytestmark = pytest.mark.ros

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT_S = 40.0
EXECUTORS = available_executors()
# Informational, per the brief: CI runners share cores, so this warns rather than fails.
_CPU_WARN_PERCENT = 5.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


class Gateway:
    """The gateway as a child process, talked to over a real socket."""

    def __init__(self, config_path: Path, port: int) -> None:
        self.port = port
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ros2_http_gateway", str(config_path), "--log-level", "info"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.output = ""

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1{path}"

    def request(self, path: str, token: str | None = OPERATOR_TOKEN) -> tuple[int, str]:
        request = urllib.request.Request(self.url(path))
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def wait_until_up(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"the gateway exited early: {self.collect()}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                if self.request("/health", token=None)[0] == 200:
                    return
            time.sleep(0.2)
        raise AssertionError(f"the gateway never came up: {self.collect()}")

    def signal_and_wait(self, sig: int, timeout: float = 25.0) -> int:
        self.process.send_signal(sig)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise AssertionError(f"the gateway ignored {sig}: {self.collect()}") from None
        self.collect()
        return int(self.process.returncode)

    def collect(self) -> str:
        if self.process.stdout is not None and not self.output:
            with contextlib.suppress(ValueError):
                self.output = self.process.stdout.read() or ""
        return self.output

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.collect()


@contextlib.contextmanager
def running(tmp_path: Path, executor: str, **overrides: object) -> Iterator[Gateway]:
    port = free_port()
    document = config_document(
        http={"bind": "127.0.0.1", "port": port}, ros={"executor": executor}, **overrides
    )
    gateway = Gateway(write_config(tmp_path, document), port)
    try:
        gateway.wait_until_up()
        yield gateway
    finally:
        gateway.close()


@pytest.mark.parametrize("executor", EXECUTORS)
def test_it_serves_over_a_real_socket(tmp_path: Path, executor: str) -> None:
    """Covers uvicorn, uvloop and httptools, which the ASGI-level tests never touch."""
    with running(tmp_path, executor) as gateway:
        status, body = gateway.request("/health", token=None)
        assert status == 200
        payload = json.loads(body)
        assert payload["status"] == "ok"
        expected = "EventsExecutor" if executor == "events" else "MultiThreadedExecutor"
        assert payload["executor"] == expected

        assert gateway.request("/mission/status", token=None)[0] == 401
        assert gateway.request("/mission/status")[0] == 200


@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_a_signal_shuts_down_cleanly_and_destroys_the_node(
    tmp_path: Path, executor: str, sig: int
) -> None:
    """The node must be destroyed before the process goes, under both executors.

    Regression cover for a real bug: uvicorn re-raises the captured signal as ``run()``
    unwinds, so teardown placed after that call never ran and the node leaked. The ordering
    of the two log lines is the evidence, not just their presence.
    """
    with running(tmp_path, executor) as gateway:
        gateway.signal_and_wait(sig)
        output = gateway.collect()

    assert "ROS runtime stopped" in output, f"the node was never destroyed:\n{output}"
    # The node is destroyed in the lifespan shutdown, which uvicorn enters only after draining,
    # so the ordering that matters is "waiting for shutdown" -> destroyed -> "shutdown complete".
    for marker in ("Waiting for application shutdown", "Application shutdown complete"):
        assert marker in output, f"missing {marker!r}:\n{output}"
    assert (
        output.index("Waiting for application shutdown")
        < output.index("ROS runtime stopped")
        < output.index("Application shutdown complete")
    ), f"ROS came down outside the HTTP drain window:\n{output}"

    assert "Traceback" not in output, (
        f"stopping the gateway should not print a traceback:\n{output}"
    )


def test_an_open_event_stream_does_not_hold_shutdown_open(tmp_path: Path) -> None:
    """An SSE response is an in-flight request that never completes on its own.

    uvicorn's graceful shutdown waits for in-flight requests *before* it runs the lifespan
    shutdown, so one connected stream would make SIGTERM look ignored until the client hung up.
    Only a real server shows this; the ASGI-level tests cannot.
    """
    with running(tmp_path, EXECUTORS[0], sse={"include": ["/mission/status"]}) as gateway:
        request = urllib.request.Request(gateway.url("/events"))
        request.add_header("Authorization", f"Bearer {OPERATOR_TOKEN}")
        with urllib.request.urlopen(request, timeout=10) as stream:
            assert stream.status == 200
            assert stream.read(1), "the snapshot never arrived"

            # Raises rather than returning if the process outlives the timeout, which is the
            # whole point: the stream is still open and unread at this moment.
            gateway.signal_and_wait(signal.SIGTERM, timeout=20.0)

    output = gateway.collect()
    assert "ROS runtime stopped" in output, f"the node was never destroyed:\n{output}"


@pytest.mark.bench
def test_idle_cpu_with_a_1khz_topic(tmp_path: Path, talker: Talker) -> None:
    """Informational soak: a 1 kHz subscription with no HTTP traffic.

    Warns rather than fails, because the figure is dominated by the distro, the RMW and the
    machine, not by this package. Measured at ~900 msg/s on CI's default `rmw_fastrtps_cpp`:
    under 5% of a core on Jazzy and newer, meeting the brief's target, and **82% on Humble**.
    On a busy workstation, 13% under fastrtps and 18% under `rmw_zenoh_cpp`.

    Humble's cost is rclpy's per-message dispatch, not anything here and not a busy-wait: the
    quiet measurement is 1.0% there, so it scales with traffic. It works out at roughly 780us of
    Python per message on the spin thread against under 55us on Jazzy. Nothing in this package
    can avoid it -- the cost is spent rebuilding a wait set and constructing a handler before our
    callback is reached, and our callback is one tuple and one attribute store.

    So the number to watch is "no worse than a do-nothing subscriber on the same distro and RMW",
    which is what raw=True and the memoised encoding exist to hold, not the absolute percentage.
    The two measurements together are what make a regression attributable: a quiet figure that
    climbs means a busy-wait somewhere in this package, a loaded one that climbs means per-message
    work. The per-thread split says whose -- a Python thread is this package or rclpy, a `dds.*`
    or `zenoh` thread is the middleware.
    """
    ticks = os.sysconf("SC_CLK_TCK")

    def cpu_seconds(path: Path) -> float:
        raw = path.read_text()
        # Fields are 1-based and `comm` may contain spaces, so index from after its closing
        # bracket: utime is field 14 and stime field 15, which puts them at 11 and 12 here.
        fields = raw[raw.rindex(")") + 2 :].split()
        return (int(fields[11]) + int(fields[12])) / ticks

    def per_thread(pid: int) -> dict[str, float]:
        """CPU seconds by thread, named. A process total says nothing about what is spending it."""
        usage: dict[str, float] = {}
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                name = (task / "comm").read_text().strip()
                # Python does not name its threads to the OS, so every one of ours reads as
                # `python`; the main thread is the one whose tid is the pid.
                label = f"{name}[{task.name}]" + ("(main)" if task.name == str(pid) else "")
                usage[label] = cpu_seconds(task / "stat")
            except OSError:  # the thread exited mid-scan
                continue
        return usage

    # One subscription, not the fixture's two on the same topic: two callbacks per message
    # would answer a different question.
    single_endpoint = {
        "/mission/status": {
            "topic": STATE_TOPIC,
            "type": "example_interfaces/msg/String",
            "roles": ["viewer", "operator"],
        }
    }
    with running(tmp_path, "auto", endpoints=single_endpoint) as gateway:
        # Let discovery settle so it is not counted as steady-state work.
        assert talker.publish_until(
            "RUNNING", lambda: gateway.request("/mission/status")[1].find('"seq":0') == -1
        )
        time.sleep(0.5)

        # Truly idle first, with nobody publishing. This is the measurement that catches a
        # busy-wait: a gateway with no traffic should cost approximately nothing, and whether the
        # cost under load is per-message work or a spin is only visible by comparing the two.
        pid = gateway.process.pid
        quiet_cpu = cpu_seconds(Path(f"/proc/{pid}/stat"))
        quiet_start = time.monotonic()
        time.sleep(2.0)
        quiet_elapsed = time.monotonic() - quiet_start
        quiet_percent = (cpu_seconds(Path(f"/proc/{pid}/stat")) - quiet_cpu) / quiet_elapsed * 100

        start_cpu = cpu_seconds(Path(f"/proc/{pid}/stat"))
        start_threads = per_thread(pid)
        start = time.monotonic()
        deadline = start + 4.0
        published = 0
        while time.monotonic() < deadline:
            talker.publish("RUNNING")
            published += 1
            time.sleep(0.001)
        elapsed = time.monotonic() - start
        used = cpu_seconds(Path(f"/proc/{pid}/stat")) - start_cpu
        end_threads = per_thread(pid)

    rate = published / elapsed
    percent = used / elapsed * 100
    busiest = sorted(
        ((after - start_threads.get(thread, 0.0)) / elapsed * 100, thread)
        for thread, after in end_threads.items()
    )[::-1]
    breakdown = ", ".join(f"{thread} {share:.0f}%" for share, thread in busiest[:4] if share >= 1.0)
    print(
        f"\n  quiet: {quiet_percent:.1f}% of one core with nothing publishing"
        f"\n  published {rate:.0f} msg/s, gateway used {percent:.1f}% of one core"
    )
    for share, thread in busiest:
        if share >= 1.0:
            print(f"    {thread:28s} {share:5.1f}%")
    if percent >= _CPU_WARN_PERCENT:
        # The breakdown rides on the warning because a quiet run prints nothing: attributing the
        # cost to a thread is the difference between a number and a lead.
        warnings.warn(
            f"idle CPU {percent:.1f}% of one core at {rate:.0f} msg/s ({quiet_percent:.1f}% "
            f"with nothing publishing), above the {_CPU_WARN_PERCENT}% informational threshold; "
            f"busiest threads: {breakdown}",
            stacklevel=1,
        )
