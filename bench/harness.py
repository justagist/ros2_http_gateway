"""Plumbing the benchmarks share: a gateway process, a load driver, machine facts.

The load driver prefers `oha` or `wrk` when either is on PATH and otherwise uses a built-in
asyncio one. Which was used is recorded with the results, because the number depends on it: a
Python driver competes with the gateway for the same cores and reports the pair, not the server.

Nothing here imports rclpy. The ROS side of a scenario runs in its own process (`ros_side.py`),
so a benchmark that only drives HTTP cannot be slowed by a node in the driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_TOKEN = "bench-operator-token"
VIEWER_TOKEN = "bench-viewer-token"

STATE_TOPIC = "/bench_state"
COMMAND_TOPIC = "/bench_command"
TRIGGER_SERVICE = "/bench_trigger"

_STARTUP_TIMEOUT_S = 40.0


# --------------------------------------------------------------------------------- facts


def hardware() -> dict[str, str]:
    """What the numbers below are numbers *for*. A benchmark without this is an anecdote."""
    return {
        "cpu": _cpu_model(),
        "cores": str(os.cpu_count() or 0),
        "memory": _total_memory(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "ros_distro": os.environ.get("ROS_DISTRO", "unknown"),
        "rmw": os.environ.get("RMW_IMPLEMENTATION", "distro default (rmw_fastrtps_cpp)"),
        "gateway": _gateway_version(),
        "driver": describe_driver(),
    }


def _cpu_model() -> str:
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _total_memory() -> str:
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return f"{int(line.split()[1]) / 1024 / 1024:.0f} GiB"
    return "unknown"


def _gateway_version() -> str:
    source = REPO_ROOT / "ros2_http_gateway" / "_version.py"
    for line in source.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


# ------------------------------------------------------------------------------- gateway


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def write_config(directory: Path, **overrides: Any) -> Path:
    """Write a benchmark config with private permissions, so plaintext tokens are accepted."""
    import yaml

    document: dict[str, Any] = {
        "endpoints": {
            "/state": {
                "topic": STATE_TOPIC,
                "type": "example_interfaces/msg/String",
                "roles": ["viewer", "operator"],
                "stale_after_ms": 5000,
            },
            "/command": {
                "topic": COMMAND_TOPIC,
                "type": "example_interfaces/msg/String",
                "method": "POST",
                "roles": ["operator"],
            },
            "/trigger": {
                "service": TRIGGER_SERVICE,
                "type": "std_srvs/srv/Trigger",
                "roles": ["operator"],
                "timeout_ms": 2000,
            },
            "/gated": {
                "service": TRIGGER_SERVICE,
                "type": "std_srvs/srv/Trigger",
                "roles": ["operator"],
                "timeout_ms": 2000,
                "preconditions": [
                    {"state_endpoint": "/state", "field": "data", "allow": {"*": ["RUNNING"]}}
                ],
            },
        },
        "auth": {
            "tokens": [
                {"token": OPERATOR_TOKEN, "user": "bench-operator", "roles": ["operator"]},
                {"token": VIEWER_TOKEN, "user": "bench-viewer", "roles": ["viewer"]},
            ]
        },
        "audit": {"sink": "file", "path": str(directory / "audit.jsonl")},
    }
    document.update(overrides)
    target = directory / "bench.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    target.chmod(0o600)
    return target


class Gateway:
    """The gateway as a child process, talked to over a real socket."""

    def __init__(self, config_path: Path, port: int) -> None:
        self.port = port
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ros2_http_gateway", str(config_path), "--log-level", "warning"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    def request(self, path: str, token: str | None = OPERATOR_TOKEN, body: bytes | None = None):
        request = urllib.request.Request(
            self.url(path), data=body, method="GET" if body is None else "POST"
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def wait_until_up(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"the gateway exited early:\n{self._output()}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                if self.request("/health", token=None)[0] == 200:
                    return
            time.sleep(0.2)
        raise RuntimeError(f"the gateway never came up:\n{self._output()}")

    def cpu_seconds(self) -> float:
        raw = Path(f"/proc/{self.process.pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2 :].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    def thread_cpu(self) -> dict[str, float]:
        ticks = os.sysconf("SC_CLK_TCK")
        usage: dict[str, float] = {}
        for task in Path(f"/proc/{self.process.pid}/task").iterdir():
            try:
                raw = (task / "stat").read_text()
                fields = raw[raw.rindex(")") + 2 :].split()
                name = (task / "comm").read_text().strip()
            except OSError:
                continue
            usage[f"{name}[{task.name}]"] = (int(fields[11]) + int(fields[12])) / ticks
        return usage

    def _output(self) -> str:
        if self.process.stdout is None:
            return ""
        with contextlib.suppress(ValueError):
            return self.process.stdout.read() or ""
        return ""

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


@contextlib.contextmanager
def running(directory: Path, **overrides: Any):
    """A gateway serving a benchmark config, torn down afterwards."""
    port = free_port()
    config = write_config(directory, http={"bind": "127.0.0.1", "port": port}, **overrides)
    gateway = Gateway(config, port)
    try:
        gateway.wait_until_up()
        yield gateway
    finally:
        gateway.close()


@contextlib.contextmanager
def ros_side(*roles: str):
    """Run the ROS half of a scenario in its own process: `talker`, `service`, or both."""
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "ros_side.py"), *roles],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        if process.stdout.readline().strip() != "ready":
            raise RuntimeError("the ROS side never came up")
        yield process
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


# ---------------------------------------------------------------------------- load driver


@dataclass
class LoadResult:
    """One load run, as the table reports it."""

    requests: int
    seconds: float
    errors: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def rps(self) -> float:
        return self.requests / self.seconds if self.seconds else 0.0

    def percentile(self, fraction: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * fraction))
        return ordered[index]


def describe_driver() -> str:
    for tool in ("oha", "wrk"):
        if shutil.which(tool):
            return tool
    return f"built-in asyncio ({_worker_count()} worker processes)"


def _worker_count() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def load(
    url: str,
    *,
    token: str | None,
    seconds: float,
    connections: int,
    body: bytes | None = None,
) -> LoadResult:
    """Drive `url` as hard as the available driver manages, and report what came back."""
    if shutil.which("oha") and body is None:
        return _load_with_oha(url, token, seconds, connections)
    return _load_builtin(url, token, seconds, connections, body)


def _load_with_oha(url: str, token: str | None, seconds: float, connections: int) -> LoadResult:
    command = ["oha", "--no-tui", "-j", "-z", f"{seconds}s", "-c", str(connections)]
    if token:
        command += ["-H", f"Authorization: Bearer {token}"]
    completed = subprocess.run([*command, url], capture_output=True, text=True, check=True)
    report = json.loads(completed.stdout)
    summary = report["summary"]
    percentiles = report.get("latencyPercentiles", {})
    return LoadResult(
        requests=int(summary["successRate"] * summary["total"])
        if summary.get("successRate", 1) <= 1
        else int(summary["total"]),
        seconds=float(summary["total"]) / float(summary["requestsPerSec"])
        if summary.get("requestsPerSec")
        else seconds,
        errors=sum(
            int(count)
            for code, count in report.get("statusCodeDistribution", {}).items()
            if not code.startswith("2")
        ),
        latencies_ms=[float(percentiles.get(key, 0.0)) * 1000.0 for key in ("p50", "p95", "p99")],
    )


def _load_builtin(
    url: str, token: str | None, seconds: float, connections: int, body: bytes | None
) -> LoadResult:
    """Fan the connections across processes, because one Python process is the bottleneck."""
    workers = _worker_count()
    per_worker = max(1, connections // workers)
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        results = pool.map(
            _worker_entry,
            [(url, token, seconds, per_worker, body)] * workers,
        )
    merged = LoadResult(requests=0, seconds=seconds, errors=0)
    for requests, errors, latencies in results:
        merged.requests += requests
        merged.errors += errors
        merged.latencies_ms.extend(latencies)
    return merged


def _worker_entry(
    args: tuple[str, str | None, float, int, bytes | None],
) -> tuple[int, int, list[float]]:
    return asyncio.run(_worker(*args))


async def _worker(
    url: str, token: str | None, seconds: float, connections: int, body: bytes | None
) -> tuple[int, int, list[float]]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    method = "GET" if body is None else "POST"
    headers = [f"Host: {host}:{port}", "Connection: keep-alive"]
    if token:
        headers.append(f"Authorization: Bearer {token}")
    if body is not None:
        headers += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    request = (
        f"{method} {parts.path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
    ).encode() + (body or b"")

    deadline = time.monotonic() + seconds
    outcomes = await asyncio.gather(
        *(_connection(host, port, request, deadline) for _ in range(connections)),
        return_exceptions=True,
    )
    requests = errors = 0
    latencies: list[float] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            errors += 1
            continue
        count, failed, samples = outcome
        requests += count
        errors += failed
        latencies.extend(samples)
    return requests, errors, latencies


async def _connection(
    host: str, port: int, request: bytes, deadline: float
) -> tuple[int, int, list[float]]:
    """One keep-alive connection issuing requests back to back until the deadline."""
    reader, writer = await asyncio.open_connection(host, port)
    requests = errors = 0
    latencies: list[float] = []
    try:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            writer.write(request)
            await writer.drain()
            status, length = await _read_head(reader)
            if length:
                await reader.readexactly(length)
            latencies.append((time.perf_counter() - started) * 1000.0)
            requests += 1
            if status < 200 or status >= 300:
                errors += 1
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        errors += 1
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return requests, errors, latencies


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, int]:
    """Read one response head, returning its status and body length.

    Only good enough for this gateway, which always sends Content-Length and never chunks.
    """
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value)
    return status, length
