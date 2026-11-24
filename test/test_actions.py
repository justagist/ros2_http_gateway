"""Action endpoints against a real Fibonacci action server.

Covers the four routes and every way a goal can end: accepted then succeeded, rejected,
cancelled mid-flight, aborted, and evicted once its result has outlived the TTL.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import orjson
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import rclpy
from example_interfaces.action import Fibonacci
from gateway_fixtures import OPERATOR_TOKEN, VIEWER_TOKEN, resolved_config
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup

from ros2_http_gateway.actions import GoalRegistry, status_name
from ros2_http_gateway.app import build_app
from ros2_http_gateway.audit import AuditLogger
from ros2_http_gateway.auth import StaticTokenAuthenticator
from ros2_http_gateway.config import AuditConfig
from ros2_http_gateway.runtime import RosRuntime

pytestmark = pytest.mark.ros

BASE = "http://gateway/api/v1"
ACTION = "/gateway_test_fib"
ABSENT_ACTION = "/gateway_test_no_action_server"


class Server:
    """A Fibonacci action server whose behaviour the tests steer."""

    def __init__(self) -> None:
        self._context = rclpy.context.Context()
        self._context.init()
        self._node = rclpy.create_node("gateway_test_action_server", context=self._context)
        self.accept = True
        self.abort = False
        self.step_delay = 0.02
        # Set when a goal request arrives. Clear `release` to hold the answer back until set.
        self.received = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.cancellable = True
        self.outcomes: list[tuple[int, str]] = []
        ActionServer(
            self._node,
            Fibonacci,
            ACTION,
            execute_callback=self._execute,
            goal_callback=self._on_goal,
            cancel_callback=lambda _g: (
                CancelResponse.ACCEPT if self.cancellable else CancelResponse.REJECT
            ),
            callback_group=ReentrantCallbackGroup(),
        )
        self._executor = rclpy.executors.MultiThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._stopping = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception:
            if not self._stopping:
                raise

    def _on_goal(self, _request: Any) -> GoalResponse:
        self.received.set()
        self.release.wait(timeout=10)
        return GoalResponse.ACCEPT if self.accept else GoalResponse.REJECT

    def _execute(self, handle: Any) -> Any:
        feedback = Fibonacci.Feedback()
        sequence = [0, 1]
        for _ in range(handle.request.order):
            if handle.is_cancel_requested:
                handle.canceled()
                self.outcomes.append((handle.request.order, "canceled"))
                result = Fibonacci.Result()
                result.sequence = sequence
                return result
            sequence.append(sequence[-1] + sequence[-2])
            feedback.sequence = sequence
            handle.publish_feedback(feedback)
            time.sleep(self.step_delay)
        handle.abort() if self.abort else handle.succeed()
        self.outcomes.append((handle.request.order, "aborted" if self.abort else "succeeded"))
        result = Fibonacci.Result()
        result.sequence = sequence
        return result

    def close(self) -> None:
        self._stopping = True
        self._executor.shutdown()
        self._thread.join(timeout=5)
        self._node.destroy_node()
        self._context.try_shutdown()


def endpoints() -> dict[str, Any]:
    return {
        "/pick": {
            "action": ACTION,
            "type": "example_interfaces/action/Fibonacci",
            "roles": ["operator"],
        },
        "/other": {
            "action": ACTION,
            "type": "example_interfaces/action/Fibonacci",
            "roles": ["operator"],
        },
        "/hasty": {
            "action": ACTION,
            "type": "example_interfaces/action/Fibonacci",
            "roles": ["operator"],
            "timeout_ms": 200,
        },
        "/absent": {
            "action": ABSENT_ACTION,
            "type": "example_interfaces/action/Fibonacci",
            "roles": ["operator"],
        },
    }


class Harness:
    def __init__(self, client: httpx.AsyncClient, runtime: RosRuntime, app: Any) -> None:
        self.client = client
        self.runtime = runtime
        self.goals: GoalRegistry = app.state.goals

    def headers(self, token: str | None = OPERATOR_TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def submit(
        self, path: str = "/pick", order: int = 5, token: str | None = OPERATOR_TOKEN
    ) -> httpx.Response:
        return await self.client.post(
            f"{BASE}{path}", content=orjson.dumps({"order": order}), headers=self.headers(token)
        )

    async def status(
        self, goal_id: str, path: str = "/pick", token: str | None = OPERATOR_TOKEN
    ) -> httpx.Response:
        return await self.client.get(f"{BASE}{path}/{goal_id}", headers=self.headers(token))

    async def cancel(
        self, goal_id: str, path: str = "/pick", token: str | None = OPERATOR_TOKEN
    ) -> httpx.Response:
        return await self.client.delete(f"{BASE}{path}/{goal_id}", headers=self.headers(token))

    async def wait_for(
        self, goal_id: str, *, path: str = "/pick", terminal: bool = True, timeout: float = 20.0
    ) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = (await self.status(goal_id, path)).json()
            if terminal and payload["status"] in ("succeeded", "aborted", "canceled"):
                return payload
            if not terminal and payload["status"] != "accepted":
                return payload
            await asyncio.sleep(0.02)
        raise AssertionError(f"goal {goal_id} never settled: {payload}")


@pytest.fixture(scope="module")
def server() -> Any:
    instance = Server()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
async def gateway(tmp_path: Path, server: Server) -> AsyncIterator[Harness]:
    resolved = resolved_config(tmp_path, endpoints=endpoints(), audit={"sink": "stderr"})
    runtime = RosRuntime(resolved)
    runtime.start()
    audit = AuditLogger(AuditConfig(sink="stderr", path=None))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")

    deadline = time.monotonic() + 20
    while not runtime.action_server_is_ready("/pick") and time.monotonic() < deadline:
        time.sleep(0.05)
    if not runtime.action_server_is_ready("/pick"):
        await client.aclose()
        await audit.stop()
        runtime.stop()
        pytest.skip("the action server never appeared on the graph")

    try:
        yield Harness(client, runtime, app)
    finally:
        await client.aclose()
        await audit.stop()
        runtime.stop()


# --------------------------------------------------------------------------------------
# submit and status
# --------------------------------------------------------------------------------------


async def test_a_goal_is_accepted_with_202_and_an_id(gateway: Harness) -> None:
    response = await gateway.submit()
    assert response.status_code == 202, "the work is accepted, not performed"
    goal_id = response.json()["goal_id"]
    assert len(goal_id) == 32


async def test_a_goal_runs_to_success_with_a_result(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=5)).json()["goal_id"]
    payload = await gateway.wait_for(goal_id)
    assert payload["status"] == "succeeded"
    assert payload["result"] == {"sequence": [0, 1, 1, 2, 3, 5, 8]}


async def test_a_running_goal_reports_executing(gateway: Harness) -> None:
    """The protocol promises `executing`; the action client already tracks it.

    Without reading the handle a running goal stayed `accepted` right up to its result, which
    the documented status list does not describe.
    """
    goal_id = (await gateway.submit(order=60)).json()["goal_id"]
    seen = set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        seen.add((await gateway.status(goal_id)).json()["status"])
        if "executing" in seen:
            break
        await asyncio.sleep(0.02)
    await gateway.cancel(goal_id)
    assert "executing" in seen, f"only ever saw {sorted(seen)}"


async def test_a_terminal_status_always_comes_with_a_result(gateway: Harness) -> None:
    """The handle reports terminal before the result is collected; the status must not.

    Otherwise a poll can catch `succeeded` with `"result": null`, and a client that treats a
    terminal status as "the result is ready" reads nothing.
    """
    goal_id = (await gateway.submit(order=6)).json()["goal_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        payload = (await gateway.status(goal_id)).json()
        if payload["status"] in ("succeeded", "aborted", "canceled"):
            assert payload["result"] is not None, f"terminal status without a result: {payload}"
            return
        assert payload["result"] is None, f"a result before the goal finished: {payload}"
        await asyncio.sleep(0.01)
    raise AssertionError("the goal never finished")


async def test_a_cancelling_goal_keeps_reporting_canceling(gateway: Harness) -> None:
    """Live status must not contradict the answer the cancel route already gave."""
    goal_id = (await gateway.submit(order=200)).json()["goal_id"]
    await asyncio.sleep(0.15)
    assert (await gateway.cancel(goal_id)).json()["success"] is True
    status = (await gateway.status(goal_id)).json()["status"]
    assert status in ("canceling", "canceled"), status


async def test_feedback_is_recorded_while_the_goal_runs(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=8)).json()["goal_id"]
    payload = await gateway.wait_for(goal_id)
    assert payload["last_feedback"] is not None
    assert payload["last_feedback"]["sequence"][:2] == [0, 1]


async def test_an_unknown_goal_id_is_a_404(gateway: Harness) -> None:
    response = await gateway.status("0" * 32)
    assert response.status_code == 404
    assert response.json()["error_code"] == "UNKNOWN_GOAL"


async def test_a_goal_cannot_be_read_through_another_endpoint(gateway: Harness) -> None:
    """Two endpoints on the same action still have separate goal namespaces."""
    goal_id = (await gateway.submit("/pick")).json()["goal_id"]
    assert (await gateway.status(goal_id, path="/other")).status_code == 404
    assert (await gateway.status(goal_id, path="/pick")).status_code == 200


async def test_a_rejected_goal_is_reported_without_an_id(gateway: Harness, server: Server) -> None:
    server.accept = False
    try:
        response = await gateway.submit()
    finally:
        server.accept = True
    assert response.status_code == 200, "the request reached ROS, which said no"
    payload = response.json()
    assert payload["error_code"] == "GOAL_REJECTED"
    assert "goal_id" not in payload
    assert len(gateway.goals) == 0, "a refused goal must not be left in the registry"


async def test_no_action_server_is_reported_as_unavailable(gateway: Harness) -> None:
    response = await gateway.submit("/absent")
    assert response.status_code == 200
    assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"


async def test_an_aborted_goal_reports_aborted(gateway: Harness, server: Server) -> None:
    server.abort = True
    try:
        goal_id = (await gateway.submit(order=3)).json()["goal_id"]
        payload = await gateway.wait_for(goal_id)
    finally:
        server.abort = False
    assert payload["status"] == "aborted"


async def test_a_bad_goal_body_is_a_400(gateway: Harness) -> None:
    response = await gateway.client.post(
        f"{BASE}/pick", content=orjson.dumps({"nope": 1}), headers=gateway.headers()
    )
    assert response.status_code == 400
    assert "unknown field" in response.json()["message"]


async def wait_for_outcome(server: Server, order: int, timeout: float = 20.0) -> str:
    """Wait for the action server to finish the goal it received with this ``order``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for seen, outcome in server.outcomes:
            if seen == order:
                return outcome
        await asyncio.sleep(0.02)
    raise AssertionError(f"the server never finished the goal of order {order}")


async def wait_for_released_callbacks(gateway: Harness, path: str, timeout: float = 20.0) -> None:
    """Wait for the action client to drop every feedback callback, which holds a goal record.

    rclpy drops a callback only when a result request for its goal completes, so a late-accepted
    goal that nobody asked the result of would keep its discarded record forever.
    """
    callbacks = gateway.runtime._action_clients[path]._feedback_callbacks
    deadline = time.monotonic() + timeout
    while callbacks and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not callbacks, f"{len(callbacks)} feedback callback(s) still held for {path}"


async def test_a_handshake_past_the_timeout_answers_timeout(
    gateway: Harness, server: Server
) -> None:
    """No record is left, and the goal the server accepts late is cancelled, not orphaned."""
    server.release.clear()
    try:
        response = await gateway.submit("/hasty", order=151)
    finally:
        server.release.set()
    assert response.status_code == 200
    assert response.json()["error_code"] == "TIMEOUT"
    assert len(gateway.goals) == 0
    assert await wait_for_outcome(server, 151) == "canceled"
    await wait_for_released_callbacks(gateway, "/hasty")


async def test_a_late_goal_the_server_will_not_cancel_is_logged(
    gateway: Harness, server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    """No client holds its id, so the log is the only trace of a goal that runs on."""
    server.cancellable = False
    server.release.clear()
    try:
        response = await gateway.submit("/hasty", order=31)
        assert response.json()["error_code"] == "TIMEOUT"
        server.release.set()
        assert await wait_for_outcome(server, 31) == "succeeded"
    finally:
        server.release.set()
        server.cancellable = True
    assert "would not cancel a goal" in caplog.text


async def test_a_submission_cancelled_mid_handshake_leaves_nothing_behind(
    gateway: Harness, server: Server
) -> None:
    server.received.clear()
    server.release.clear()
    try:
        submission = asyncio.create_task(gateway.submit(order=152))
        assert await asyncio.to_thread(server.received.wait, 10), (
            "the goal never reached the server"
        )
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
    finally:
        server.release.set()
    assert len(gateway.goals) == 0
    assert await wait_for_outcome(server, 152) == "canceled"
    await wait_for_released_callbacks(gateway, "/pick")


async def test_a_goal_may_run_longer_than_the_timeout(gateway: Harness) -> None:
    """The timeout bounds the handshake only; 20 steps take about twice the 200 ms timeout."""
    response = await gateway.submit("/hasty", order=20)
    assert response.status_code == 202
    payload = await gateway.wait_for(response.json()["goal_id"], path="/hasty")
    assert payload["status"] == "succeeded"


# --------------------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------------------


async def test_a_goal_can_be_cancelled_mid_flight(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=200)).json()["goal_id"]
    await asyncio.sleep(0.2)

    response = await gateway.cancel(goal_id)
    assert response.status_code == 200
    assert response.json()["success"] is True

    payload = await gateway.wait_for(goal_id)
    assert payload["status"] == "canceled"


async def test_cancelling_a_finished_goal_says_so(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=2)).json()["goal_id"]
    await gateway.wait_for(goal_id)
    payload = (await gateway.cancel(goal_id)).json()
    assert payload["error_code"] == "ALREADY_TERMINAL"


async def test_cancelling_an_unknown_goal_is_a_404(gateway: Harness) -> None:
    assert (await gateway.cancel("f" * 32)).status_code == 404


async def test_a_goal_finishing_during_its_cancel_request_keeps_its_result(
    gateway: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result can land while the cancel request waits for the server's answer.

    The finished goal must not go back to ``canceling``: status reads would disagree with the
    stored result, and a new stream would wait for a terminal event that already happened.
    """
    goal_id = (await gateway.submit(order=10)).json()["goal_id"]
    record = gateway.goals.get("/pick", goal_id)
    assert record is not None

    async def answer_after_the_result(_runtime: RosRuntime, _handle: Any) -> Any:
        deadline = time.monotonic() + 20
        while not record.is_terminal and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return SimpleNamespace(goals_canceling=[object()], return_code=0)

    monkeypatch.setattr(RosRuntime, "cancel_goal", answer_after_the_result)
    assert (await gateway.cancel(goal_id)).json()["success"] is True

    payload = (await gateway.status(goal_id)).json()
    assert payload["status"] == "succeeded"
    assert payload["result"] is not None
    async with gateway.client.stream(
        "GET", f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers()
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert [name for name, _ in parse_events(body)] == ["terminal"]


# --------------------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------------------


def parse_events(text: str) -> list[tuple[str, Any]]:
    """Split an SSE body into (event, data) pairs."""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((name, orjson.loads(data) if data else None))
    return events


async def test_a_stream_delivers_feedback_then_one_terminal_event_then_closes(
    gateway: Harness,
) -> None:
    goal_id = (await gateway.submit(order=6)).json()["goal_id"]

    async with gateway.client.stream(
        "GET", f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers()
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = parse_events(body)
    assert events, "the stream produced nothing"
    assert [name for name, _ in events][-1] == "terminal", "the terminal event must come last"
    assert sum(1 for name, _ in events if name == "terminal") == 1
    terminal = events[-1][1]
    assert terminal["status"] == "succeeded"
    assert terminal["result"] == {"sequence": [0, 1, 1, 2, 3, 5, 8, 13]}


async def test_feedback_order_is_preserved(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=12)).json()["goal_id"]
    async with gateway.client.stream(
        "GET", f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers()
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    lengths = [len(data["sequence"]) for name, data in parse_events(body) if name == "feedback"]
    assert lengths, "no feedback was streamed"
    assert lengths == sorted(lengths), f"feedback arrived out of order: {lengths}"


async def test_streaming_an_already_finished_goal_returns_just_the_terminal_event(
    gateway: Harness,
) -> None:
    goal_id = (await gateway.submit(order=2)).json()["goal_id"]
    await gateway.wait_for(goal_id)

    async with gateway.client.stream(
        "GET", f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers()
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    events = parse_events(body)
    assert [name for name, _ in events] == ["terminal"]


async def test_streaming_an_unknown_goal_is_a_404(gateway: Harness) -> None:
    response = await gateway.client.get(f"{BASE}/pick/{'a' * 32}/stream", headers=gateway.headers())
    assert response.status_code == 404


async def test_two_streams_on_one_goal_both_see_the_terminal_event(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=10)).json()["goal_id"]

    async def read() -> list[str]:
        async with gateway.client.stream(
            "GET", f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers()
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
        return [name for name, _ in parse_events(body)]

    first, second = await asyncio.gather(read(), read())
    assert first[-1] == "terminal"
    assert second[-1] == "terminal"


# --------------------------------------------------------------------------------------
# concurrency and eviction
# --------------------------------------------------------------------------------------


async def test_concurrent_goals_on_one_endpoint_stay_separate(gateway: Harness) -> None:
    orders = [3, 5, 7, 9]
    ids = [(await gateway.submit(order=order)).json()["goal_id"] for order in orders]
    assert len(set(ids)) == len(ids), "goal ids must be unique"

    results = [await gateway.wait_for(goal_id) for goal_id in ids]
    for order, payload in zip(orders, results, strict=True):
        assert payload["status"] == "succeeded"
        assert len(payload["result"]["sequence"]) == order + 2


async def test_a_result_is_evicted_once_it_has_outlived_the_ttl(gateway: Harness) -> None:
    """Reaching in to age the record beats sleeping through a five-minute TTL."""
    goal_id = (await gateway.submit(order=2)).json()["goal_id"]
    await gateway.wait_for(goal_id)
    assert (await gateway.status(goal_id)).status_code == 200

    record = gateway.goals.get("/pick", goal_id)
    assert record is not None and record.terminal_at is not None
    record.terminal_at -= 10_000

    response = await gateway.status(goal_id)
    assert response.status_code == 404
    assert response.json()["error_code"] == "UNKNOWN_GOAL"
    assert len(gateway.goals) == 0


# --------------------------------------------------------------------------------------
# auth on every route
# --------------------------------------------------------------------------------------


async def test_every_action_route_requires_the_role(gateway: Harness) -> None:
    goal_id = (await gateway.submit(order=2)).json()["goal_id"]
    for token, expected in ((None, 401), (VIEWER_TOKEN, 403)):
        assert (await gateway.submit(token=token)).status_code == expected
        assert (await gateway.status(goal_id, token=token)).status_code == expected
        assert (await gateway.cancel(goal_id, token=token)).status_code == expected
        stream = await gateway.client.get(
            f"{BASE}/pick/{goal_id}/stream", headers=gateway.headers(token)
        )
        assert stream.status_code == expected


# --------------------------------------------------------------------------------------
# the registry on its own
# --------------------------------------------------------------------------------------


def test_status_names_cover_every_goal_status_code() -> None:
    from action_msgs.msg import GoalStatus

    for name in dir(GoalStatus):
        if name.startswith("STATUS_"):
            assert status_name(getattr(GoalStatus, name)) != "unknown" or name == "STATUS_UNKNOWN"


def test_the_registry_evicts_on_a_fake_clock() -> None:
    now = [1000.0]
    registry = GoalRegistry(ttl_s=300.0, clock=lambda: now[0])
    record = registry.create("/pick")
    registry.on_terminal(record, "succeeded", b"{}")

    now[0] += 299.0
    assert registry.get("/pick", record.goal_id) is not None
    now[0] += 2.0
    assert registry.get("/pick", record.goal_id) is None
    assert len(registry) == 0


def test_a_running_goal_is_never_evicted() -> None:
    now = [0.0]
    registry = GoalRegistry(ttl_s=1.0, clock=lambda: now[0])
    record = registry.create("/pick")
    now[0] += 100_000.0
    assert registry.get("/pick", record.goal_id) is record


def test_creating_a_goal_sweeps_the_expired_ones() -> None:
    """The only way the registry grows is the only place it needs to shrink."""
    now = [0.0]
    registry = GoalRegistry(ttl_s=10.0, clock=lambda: now[0])
    for _ in range(5):
        registry.on_terminal(registry.create("/pick"), "succeeded", None)
    assert len(registry) == 5

    now[0] += 100.0
    registry.create("/pick")
    assert len(registry) == 1, "the sweep should have reclaimed the five expired records"


def test_a_full_queue_still_receives_the_terminal_sentinel() -> None:
    """A full queue must still close its stream.

    The sentinel once used a bare put_nowait: a queue at maxsize accepts the terminal frame by
    dropping its oldest and is full again, so the sentinel raised and every later subscriber got
    neither frame nor sentinel. Two subscribers, the first deliberately full: both must close.
    """
    registry = GoalRegistry(ttl_s=300.0)
    record = registry.create("/pick")
    full = registry.subscribe(record)
    healthy = registry.subscribe(record)

    for index in range(200):  # well past the backlog, so `full` is saturated
        registry.on_feedback(record, orjson.dumps({"n": index}))
    assert full.full()

    registry.on_terminal(record, "succeeded", b'{"sequence":[0,1]}')

    for name, queue in (("full", full), ("healthy", healthy)):
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
        assert frames[-1] is None, f"the {name} subscriber never got a sentinel: {frames[-2:]}"
        assert any(f is not None and b"terminal" in f for f in frames), (
            f"the {name} subscriber never got the terminal frame"
        )


def test_feedback_arriving_after_terminal_is_ignored() -> None:
    """Scheduling order allows a ROS-side callback to land after the goal finished.

    Two reads of a finished goal must not disagree, so late feedback is dropped rather than
    overwriting what the terminal state recorded.
    """
    registry = GoalRegistry(ttl_s=300.0)
    record = registry.create("/pick")
    registry.on_feedback(record, orjson.dumps({"n": 1}))
    registry.on_terminal(record, "succeeded", b"{}")

    before = record.last_feedback
    registry.on_feedback(record, orjson.dumps({"n": 999}))
    assert record.last_feedback == before


def test_a_full_stream_queue_drops_the_oldest_frame() -> None:
    """A slow client loses old feedback rather than stalling the goal."""
    registry = GoalRegistry(ttl_s=300.0)
    record = registry.create("/pick")
    queue = registry.subscribe(record)

    for index in range(200):
        registry.on_feedback(record, orjson.dumps({"n": index}))

    assert queue.qsize() <= 64
    first = orjson.loads(queue.get_nowait().split(b"data: ")[1])
    assert first["n"] > 0, "the oldest frames should have been discarded, not the newest"
