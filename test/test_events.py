"""The global event stream over HTTP, with a real ROS runtime behind it.

Covers what only the route can show: the snapshot, deltas that match the GET body byte for
byte, role filtering across one shared connection, the query-parameter credential, and the
client limit. ``test_sse.py`` covers the broadcaster's own logic without ROS.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import (
    OPERATOR_TOKEN,
    STATE_TOPIC,
    VIEWER_TOKEN,
    Talker,
    config_document,
    write_config,
)

from ros2_http_gateway.app import build_app
from ros2_http_gateway.audit import AuditLogger
from ros2_http_gateway.auth import StaticTokenAuthenticator
from ros2_http_gateway.config import AuditConfig, load_config
from ros2_http_gateway.resolve import resolve
from ros2_http_gateway.runtime import RosRuntime

pytestmark = pytest.mark.ros

BASE = "http://gateway/api/v1"
STATE = "/mission/status"
SECRET = "/secret/state"
OUTSIDER_TOKEN = "outsider-token-for-tests"


def document(**sse: Any) -> dict[str, Any]:
    """The shared test config plus an operator-only topic read and a stream over both."""
    base = config_document()
    base["endpoints"][SECRET] = {
        "topic": STATE_TOPIC,
        "type": "example_interfaces/msg/String",
        "roles": ["operator"],
        "stale_after_ms": 1000,
    }
    base["auth"]["tokens"].append(
        {"token": OUTSIDER_TOKEN, "user": "outsider-user", "roles": ["nobody"]}
    )
    base["sse"] = {"include": [STATE, SECRET], **sse}
    return base


class Stream:
    """An open event stream, driven straight through the ASGI interface.

    Not httpx: its ASGITransport awaits the whole application call and buffers every body part
    before returning a response, so a stream that never ends is never seen at all. One-shot
    requests still go through the client; only the streams come through here.
    """

    def __init__(self, app: Any, path: str, token: str | None) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._hung_up = asyncio.Event()
        self._buffer = b""
        target, _, query = path.partition("?")
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "GET",
            "path": target,
            "raw_path": target.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(b"host", b"gateway")]
            + ([(b"authorization", f"Bearer {token}".encode())] if token else []),
            "client": ("127.0.0.1", 54321),
            "server": ("gateway", 80),
        }
        self._task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> dict[str, Any]:
        await self._hung_up.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                self._chunks.put_nowait(body)
            if not message.get("more_body", False):
                self._chunks.put_nowait(None)

    async def start(self, timeout: float = 5.0) -> Stream:
        await asyncio.wait_for(self._started.wait(), timeout)
        return self

    async def take(self, count: int, timeout: float = 10.0) -> list[bytes]:
        """Return the next ``count`` complete frames."""
        frames: list[bytes] = []

        async def pump() -> None:
            while True:
                while b"\n\n" in self._buffer:
                    block, self._buffer = self._buffer.split(b"\n\n", 1)
                    frames.append(block + b"\n\n")
                    if len(frames) == count:
                        return
                chunk = await self._chunks.get()
                assert chunk is not None, "the server ended the stream"
                self._buffer += chunk

        await asyncio.wait_for(pump(), timeout)
        return frames

    async def ended(self, timeout: float = 5.0) -> None:
        """Wait for the server to end the stream of its own accord."""
        await asyncio.wait_for(self._task, timeout)

    async def close(self) -> None:
        """Hang up, the way a browser closing its tab does."""
        self._hung_up.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class Harness:
    def __init__(
        self, client: httpx.AsyncClient, app: Any, runtime: RosRuntime, audit_path: Path
    ) -> None:
        self.client = client
        self.app = app
        self.runtime = runtime
        self.broadcaster = app.state.sse
        self._audit_path = audit_path

    def audit(self) -> list[dict[str, Any]]:
        if not self._audit_path.exists():
            return []
        return [
            orjson.loads(line)
            for line in self._audit_path.read_bytes().decode().splitlines()
            if line.strip()
        ]

    async def settle(self) -> None:
        for _ in range(60):
            await asyncio.sleep(0.005)
            if self.audit():
                return

    async def stream(self, token: str | None = OPERATOR_TOKEN, query: str = "") -> Stream:
        """Open the event stream and wait for its response head."""
        return await Stream(self.app, f"/api/v1/events{query}", token).start()

    async def connect(self, token: str | None = OPERATOR_TOKEN) -> httpx.Response:
        """A one-shot request to the stream route, for the answers that are not a stream."""
        return await self.client.get(f"{BASE}/events", headers=_headers(token))

    async def get(self, path: str, token: str = OPERATOR_TOKEN) -> httpx.Response:
        return await self.client.get(f"{BASE}{path}", headers=_headers(token))


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def build(tmp_path: Path, **sse: Any) -> AsyncIterator[Harness]:
    audit_path = tmp_path / "audit.jsonl"
    config = document(**sse)
    config["audit"] = {"sink": "file", "path": str(audit_path)}
    resolved = resolve(load_config(write_config(tmp_path, config)))
    runtime = RosRuntime(resolved)
    runtime.start()
    audit = AuditLogger(AuditConfig(sink="file", path=audit_path))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        try:
            yield Harness(client, app, runtime, audit_path)
        finally:
            app.state.sse.close()
            await audit.stop()
            runtime.stop()


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[Harness]:
    async for harness in build(tmp_path):
        yield harness


@pytest.fixture
async def talker() -> AsyncIterator[Talker]:
    instance = Talker()
    try:
        yield instance
    finally:
        instance.close()


def named(frames: list[bytes]) -> list[str]:
    return [
        line[len("event: ") :].decode()
        for frame in frames
        for line in frame.splitlines()
        if line.startswith(b"event: ")
    ]


def data_of(frame: bytes) -> bytes:
    for line in frame.splitlines():
        if line.startswith(b"data: "):
            return line[len("data: ") :]
    raise AssertionError(f"no data line in {frame!r}")


# --------------------------------------------------------------------------------------
# snapshot and deltas
# --------------------------------------------------------------------------------------


async def test_the_stream_opens_with_one_snapshot_per_included_endpoint(gateway: Harness) -> None:
    stream = await gateway.stream()
    try:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        frames = await stream.take(2)
    finally:
        await stream.close()

    assert named(frames) == [STATE, SECRET], "in config order"


async def test_an_endpoint_with_no_data_yet_still_appears_in_the_snapshot(
    gateway: Harness,
) -> None:
    """A client learns the endpoint set up front rather than as traffic happens to arrive."""
    stream = await gateway.stream()
    try:
        first = (await stream.take(1))[0]
    finally:
        await stream.close()

    assert orjson.loads(data_of(first)) == {"data": None, "stamp": None, "seq": 0, "stale": True}


async def test_a_new_sample_arrives_as_a_delta(gateway: Harness, talker: Talker) -> None:
    cache = gateway.runtime.caches[STATE]
    stream = await gateway.stream()
    try:
        await stream.take(2)  # the snapshot
        assert talker.publish_until("RUNNING", lambda: cache.latest is not None)
        delta = (await stream.take(1))[0]
    finally:
        await stream.close()

    assert named([delta]) == [STATE]
    body = orjson.loads(data_of(delta))
    assert body["data"] == {"data": "RUNNING"}
    assert body["seq"] >= 1


async def test_the_streamed_body_is_byte_identical_to_the_get_body(
    gateway: Harness, talker: Talker
) -> None:
    """The promise that lets a client use one parser for both routes."""
    cache = gateway.runtime.caches[STATE]
    talker.publish_until("IDLE", lambda: cache.latest is not None)
    talker.wait_until_quiet(cache)

    stream = await gateway.stream()
    try:
        snapshot = (await stream.take(1))[0]
    finally:
        await stream.close()

    assert data_of(snapshot) == (await gateway.get(STATE)).content


async def test_the_stream_carries_no_action_feedback(gateway: Harness, talker: Talker) -> None:
    """Feedback is streamed per goal; mixing it in here would tie one connection to two ideas."""
    cache = gateway.runtime.caches[STATE]
    stream = await gateway.stream()
    try:
        await stream.take(2)
        assert talker.publish_until("RUNNING", lambda: cache.latest is not None)
        frames = await stream.take(1)
    finally:
        await stream.close()

    assert set(named(frames)) <= {STATE, SECRET}


# --------------------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------------------


async def test_a_client_is_shown_only_the_endpoints_its_roles_permit(
    gateway: Harness, talker: Talker
) -> None:
    """One connection carries several endpoints, so filtering has to be per client."""
    cache = gateway.runtime.caches[STATE]
    stream = await gateway.stream(token=VIEWER_TOKEN)
    try:
        frames = await stream.take(1)
        assert talker.publish_until("RUNNING", lambda: cache.latest is not None)
        frames += await stream.take(1)
    finally:
        await stream.close()

    assert named(frames) == [STATE, STATE], "the operator-only endpoint never appears"


async def test_a_token_that_may_read_nothing_in_the_stream_is_forbidden(gateway: Harness) -> None:
    response = await gateway.connect(OUTSIDER_TOKEN)
    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"


async def test_an_unauthenticated_stream_is_a_401(gateway: Harness) -> None:
    assert (await gateway.connect(None)).status_code == 401


# --------------------------------------------------------------------------------------
# the query credential
# --------------------------------------------------------------------------------------


async def test_the_stream_accepts_a_query_token_because_eventsource_cannot_set_headers(
    gateway: Harness,
) -> None:
    stream = await gateway.stream(token=None, query=f"?access_token={OPERATOR_TOKEN}")
    try:
        assert stream.status == 200
        await stream.take(1)
    finally:
        await stream.close()

    await gateway.settle()
    (entry,) = gateway.audit()
    assert entry["credential"] == "query", "the record says how the credential arrived"
    assert entry["user"] == "operator-user"
    assert OPERATOR_TOKEN not in orjson.dumps(entry).decode(), "never the token itself"
    assert "?" not in entry["path"]


async def test_a_header_authenticated_stream_is_not_marked_as_a_query_credential(
    gateway: Harness,
) -> None:
    stream = await gateway.stream()
    try:
        await stream.take(1)
    finally:
        await stream.close()

    await gateway.settle()
    (entry,) = gateway.audit()
    assert "credential" not in entry


async def test_a_bad_query_token_is_still_a_401(gateway: Harness) -> None:
    response = await gateway.client.get(f"{BASE}/events?access_token=not-a-token")
    assert response.status_code == 401


async def test_no_other_route_accepts_a_query_token(gateway: Harness) -> None:
    """The exception exists for EventSource, which is only used by the stream."""
    response = await gateway.client.get(f"{BASE}{STATE}?access_token={OPERATOR_TOKEN}")
    assert response.status_code == 401


# --------------------------------------------------------------------------------------
# limits and liveness
# --------------------------------------------------------------------------------------


async def test_past_max_clients_the_route_is_a_503(tmp_path: Path) -> None:
    async for gateway in build(tmp_path, max_clients=1):
        stream = await gateway.stream()
        try:
            await stream.take(1)
            refused = await gateway.connect()
            assert refused.status_code == 503
            assert refused.json()["error_code"] == "TOO_MANY_CLIENTS"
        finally:
            await stream.close()


async def test_a_client_that_hangs_up_frees_its_slot(tmp_path: Path) -> None:
    async for gateway in build(tmp_path, max_clients=1):
        stream = await gateway.stream()
        await stream.take(1)
        assert gateway.broadcaster.clients == 1
        await stream.close()

        assert gateway.broadcaster.clients == 0, "the generator never ran its finally"
        second = await gateway.stream()
        try:
            assert second.status == 200
        finally:
            await second.close()


async def test_a_quiet_stream_is_kept_alive_with_a_comment_frame(
    gateway: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proxies idle out a connection that says nothing, and a quiet robot says nothing."""
    monkeypatch.setattr("ros2_http_gateway.app._HEARTBEAT_S", 0.05)
    stream = await gateway.stream()
    try:
        frames = await stream.take(3)
    finally:
        await stream.close()

    assert frames[2] == b": keepalive\n\n"


async def test_closing_the_broadcaster_ends_a_connected_stream(gateway: Harness) -> None:
    """What a SIGTERM has to do: a stream never ends on its own and would hold shutdown open."""
    stream = await gateway.stream()
    await stream.take(2)
    gateway.broadcaster.close()
    await stream.ended()
