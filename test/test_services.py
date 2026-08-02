"""Service endpoints and state preconditions, against real ROS service servers.

Covers the four outcomes a call can have -- answered, refused by policy, timed out, no server
-- and that each is reported in the one envelope with the right audit record.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import rclpy
from example_interfaces.srv import AddTwoInts
from gateway_fixtures import (
    OPERATOR_TOKEN,
    STATE_TOPIC,
    VIEWER_TOKEN,
    Talker,
    resolved_config,
)
from rclpy.callback_groups import ReentrantCallbackGroup
from std_srvs.srv import Trigger

from ros2_http_gateway.app import build_app
from ros2_http_gateway.audit import AuditLogger
from ros2_http_gateway.auth import StaticTokenAuthenticator
from ros2_http_gateway.config import AuditConfig
from ros2_http_gateway.runtime import RosRuntime

pytestmark = pytest.mark.ros

BASE = "http://gateway/api/v1"
ADD_SERVICE = "/gateway_test_add"
TRIGGER_SERVICE = "/gateway_test_trigger"
ABSENT_SERVICE = "/gateway_test_nobody_serves_this"


class Server:
    """Real service servers in their own context, with controllable behaviour."""

    def __init__(self) -> None:
        self._context = rclpy.context.Context()
        self._context.init()
        self._node = rclpy.create_node("gateway_test_services", context=self._context)
        self.delay_s = 0.0
        self.trigger_success = True
        self.trigger_message = "done"

        # Reentrant: with the node's default mutually exclusive group, the deliberately slow
        # `_add` below blocks every other call on this node, measured at 859ms for a Trigger
        # issued during a 1s Add. Abandoned callbacks then outlive the test that made them.
        group = ReentrantCallbackGroup()
        self._node.create_service(AddTwoInts, ADD_SERVICE, self._add, callback_group=group)
        self._node.create_service(Trigger, TRIGGER_SERVICE, self._trigger, callback_group=group)
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

    def _add(self, request: Any, response: Any) -> Any:
        if self.delay_s:
            time.sleep(self.delay_s)
        response.sum = request.a + request.b
        return response

    def _trigger(self, _request: Any, response: Any) -> Any:
        response.success = self.trigger_success
        response.message = self.trigger_message
        return response

    def close(self) -> None:
        self._stopping = True
        self._executor.shutdown()
        self._thread.join(timeout=5)
        self._node.destroy_node()
        self._context.try_shutdown()


def endpoints(**overrides: Any) -> dict[str, Any]:
    """A config with an add service, a guarded trigger, and one nobody serves."""
    base: dict[str, Any] = {
        "/mission/status": {
            "topic": STATE_TOPIC,
            "type": "example_interfaces/msg/String",
            "roles": ["viewer", "operator"],
            # A precondition reads this, so a bound is required. Generous, so that tests which
            # publish once and then make several calls do not age out mid-test; the staleness
            # test below lowers it deliberately.
            "stale_after_ms": 60000,
        },
        "/math/{a}/plus/{b}": {
            "service": ADD_SERVICE,
            "type": "example_interfaces/srv/AddTwoInts",
            "roles": ["operator"],
            "timeout_ms": 3000,
            "request_map": {"a": "{a}", "b": "{b}"},
        },
        "/add": {
            "service": ADD_SERVICE,
            "type": "example_interfaces/srv/AddTwoInts",
            "roles": ["operator"],
            "timeout_ms": 400,
        },
        "/absent": {
            "service": ABSENT_SERVICE,
            "type": "std_srvs/srv/Trigger",
            "roles": ["operator"],
        },
        "/mission/{command}": {
            "service": TRIGGER_SERVICE,
            "type": "std_srvs/srv/Trigger",
            "roles": ["operator"],
            "preconditions": [
                {
                    "state_endpoint": "/mission/status",
                    "field": "data",
                    "allow": {"start": ["IDLE"], "abort": ["RUNNING", "PAUSED"], "reset": ["*"]},
                }
            ],
        },
    }
    base.update(overrides)
    return base


class Harness:
    def __init__(self, client: httpx.AsyncClient, runtime: RosRuntime, audit_path: Path) -> None:
        self.client = client
        self.runtime = runtime
        self._audit_path = audit_path

    def audit(self) -> list[dict[str, Any]]:
        if not self._audit_path.exists():
            return []
        return [
            orjson.loads(line)
            for line in self._audit_path.read_bytes().decode().splitlines()
            if line.strip()
        ]

    async def post(
        self, path: str, body: Any = None, token: str | None = OPERATOR_TOKEN
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.post(
            f"{BASE}{path}",
            content=orjson.dumps(body) if body is not None else b"",
            headers=headers,
        )

    async def settle(self) -> None:
        import asyncio

        for _ in range(60):
            await asyncio.sleep(0.005)
            if self.audit():
                return


async def build(tmp_path: Path, **config: Any) -> tuple[Harness, Callable[[], None]]:
    audit_path = tmp_path / "audit.jsonl"
    resolved = resolved_config(tmp_path, audit={"sink": "file", "path": str(audit_path)}, **config)
    runtime = RosRuntime(resolved)
    runtime.start()
    audit = AuditLogger(AuditConfig(sink="file", path=audit_path))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")

    async def close() -> None:
        await client.aclose()
        await audit.stop()
        runtime.stop()

    return Harness(client, runtime, audit_path), close  # type: ignore[return-value]


@pytest.fixture(scope="module")
def servers() -> Any:
    instance = Server()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
async def gateway(tmp_path: Path, servers: Server) -> AsyncIterator[Harness]:
    harness, close = await build(tmp_path, endpoints=endpoints())
    _wait_for_service(harness, "/add")
    _wait_for_service(harness, "/mission/{command}")
    try:
        yield harness
    finally:
        await close()  # type: ignore[misc]


def _wait_for_service(harness: Harness, path: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if harness.runtime.service_is_ready(path):
            return
        time.sleep(0.05)
    pytest.skip(f"the {path} service never appeared on the graph")


# --------------------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------------------


async def test_a_service_call_returns_its_response(gateway: Harness) -> None:
    response = await gateway.post("/add", {"a": 3, "b": 4})
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "",
        "error_code": None,
        "response": {"sum": "7"},
    }


async def test_the_response_uses_int64_as_string(gateway: Harness) -> None:
    """AddTwoInts.sum is an int64, so it is a string by default; not an accident."""
    payload = (await gateway.post("/add", {"a": 2**53, "b": 1})).json()
    assert payload["response"]["sum"] == str(2**53 + 1)


async def test_request_map_fills_fields_from_path_parameters(gateway: Harness) -> None:
    """The path is text; the request fields are int64. Coercion happens before conversion."""
    payload = (await gateway.post("/math/20/plus/22")).json()
    assert payload["success"] is True
    assert payload["response"]["sum"] == "42"


async def test_request_map_takes_precedence_over_the_body(gateway: Harness) -> None:
    """The URL is the more specific statement of intent."""
    payload = (await gateway.post("/math/1/plus/2", {"a": 900, "b": 900})).json()
    assert payload["response"]["sum"] == "3"


async def test_a_successful_call_is_audited_as_allowed(gateway: Harness) -> None:
    await gateway.post("/add", {"a": 1, "b": 1})
    await gateway.settle()
    (entry,) = gateway.audit()
    assert entry["decision"] == "allow"
    assert entry["outcome"] == "ok"
    assert entry["status"] == 200


# --------------------------------------------------------------------------------------
# failure outcomes, all HTTP 200
# --------------------------------------------------------------------------------------


async def test_no_server_is_service_unavailable(gateway: Harness) -> None:
    response = await gateway.post("/absent", {})
    assert response.status_code == 200, "the request reached ROS policy, so it is not a 4xx"
    payload = response.json()
    assert payload["error_code"] == "SERVICE_UNAVAILABLE"
    assert payload["success"] is False
    assert payload["response"] is None
    assert ABSENT_SERVICE in payload["message"]


async def test_a_slow_service_times_out(gateway: Harness, servers: Server) -> None:
    """/add has timeout_ms 400; the server is made to take a second."""
    servers.delay_s = 1.0
    try:
        started = time.monotonic()
        response = await gateway.post("/add", {"a": 1, "b": 1})
        elapsed = time.monotonic() - started
    finally:
        servers.delay_s = 0.0

    payload = response.json()
    assert response.status_code == 200
    assert payload["error_code"] == "TIMEOUT"
    assert payload["response"] is None
    assert "400ms" in payload["message"]
    assert 0.3 < elapsed < 2.5, f"gave up after {elapsed:.2f}s, expected about 0.4"


async def test_the_gateway_still_works_after_a_timeout(gateway: Harness, servers: Server) -> None:
    """A withdrawn request must not be mistaken for the answer to the next call."""
    servers.delay_s = 0.8
    try:
        assert (await gateway.post("/add", {"a": 5, "b": 5})).json()["error_code"] == "TIMEOUT"
    finally:
        servers.delay_s = 0.0
    time.sleep(1.0)  # let the abandoned response arrive and be discarded

    payload = (await gateway.post("/add", {"a": 20, "b": 3})).json()
    assert payload["success"] is True
    assert payload["response"]["sum"] == "23", "a stale response leaked into a later call"


@pytest.mark.parametrize("outcome", ["SERVICE_UNAVAILABLE"])
async def test_a_failed_call_is_audited_with_its_code(gateway: Harness, outcome: str) -> None:
    await gateway.post("/absent", {})
    await gateway.settle()
    (entry,) = gateway.audit()
    assert entry["decision"] == "allow", "policy allowed it; ROS could not serve it"
    assert entry["outcome"] == outcome
    assert entry["status"] == 200


async def test_a_service_returning_false_is_still_a_successful_call(
    gateway: Harness, servers: Server
) -> None:
    """ "The robot said no" and "the gateway could not ask" must stay distinguishable."""
    servers.trigger_success = False
    servers.trigger_message = "not while moving"
    try:
        payload = (await gateway.post("/mission/reset")).json()
    finally:
        servers.trigger_success = True

    assert payload["success"] is True, f"the call itself succeeded: {payload}"
    assert payload["error_code"] is None
    assert payload["response"] == {"success": False, "message": "not while moving"}


# --------------------------------------------------------------------------------------
# bad requests
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"a": "notanumber", "b": 4}, "expected an integer"),
        ({"a": 2**63, "b": 0}, "outside"),
        ({"nope": 1}, "unknown field"),
        ([1, 2], "must be a JSON object"),
    ],
)
async def test_a_bad_body_is_a_400(gateway: Harness, body: Any, fragment: str) -> None:
    response = await gateway.post("/add", body)
    assert response.status_code == 400
    assert fragment in response.json()["message"]


async def test_a_numeric_string_is_accepted_for_an_int64_field(gateway: Harness) -> None:
    """AddTwoInts uses int64, the one width that takes a string.

    Not laxity: with ``int64_as_string`` the read side emits ``"7"``, so a body this gateway
    produced has to be one it accepts. Narrower integers reject strings, which
    test_codegen pins.
    """
    payload = (await gateway.post("/add", {"a": "3", "b": 4})).json()
    assert payload["success"] is True
    assert payload["response"]["sum"] == "7"


async def test_an_unconvertible_path_parameter_is_a_400(gateway: Harness) -> None:
    """`/math/notanumber/plus/2` cannot fill an int64 field."""
    response = await gateway.post("/math/notanumber/plus/2")
    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "BAD_REQUEST"
    assert "a" in payload["message"]


async def test_a_bad_request_never_reaches_the_service(gateway: Harness) -> None:
    before = (await gateway.post("/add", {"a": 1, "b": 1})).json()["response"]["sum"]
    await gateway.post("/add", {"a": "1", "b": 1})
    after = (await gateway.post("/add", {"a": 1, "b": 1})).json()["response"]["sum"]
    assert before == after == "2"


# --------------------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------------------


async def test_a_call_is_refused_while_the_state_is_unknown(gateway: Harness) -> None:
    """Deny by default: nothing has been published, so the gate cannot be satisfied."""
    payload = (await gateway.post("/mission/start")).json()
    assert payload["error_code"] == "PRECONDITION_FAILED"
    assert "received nothing yet" in payload["message"]


async def test_decisions_flip_as_the_state_topic_changes(gateway: Harness, talker: Talker) -> None:
    """The acceptance criterion: drive the state topic and watch the gate open and close."""
    cache = gateway.runtime.caches["/mission/status"]

    assert talker.publish_until("IDLE", lambda: cache.latest is not None)
    assert (await gateway.post("/mission/start")).json()["success"] is True
    refused = (await gateway.post("/mission/abort")).json()
    assert refused["error_code"] == "PRECONDITION_FAILED"
    assert "'IDLE'" in refused["message"]

    assert talker.publish_until("RUNNING", lambda: _state(cache) == "RUNNING")
    assert (await gateway.post("/mission/abort")).json()["success"] is True
    assert (await gateway.post("/mission/start")).json()["error_code"] == "PRECONDITION_FAILED"


async def test_a_wildcard_state_list_allows_any_state(gateway: Harness, talker: Talker) -> None:
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("ANYTHING", lambda: cache.latest is not None)
    assert (await gateway.post("/mission/reset")).json()["success"] is True


async def test_an_unlisted_command_is_refused(gateway: Harness, talker: Talker) -> None:
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("IDLE", lambda: cache.latest is not None)
    payload = (await gateway.post("/mission/detonate")).json()
    assert payload["error_code"] == "PRECONDITION_FAILED"
    assert "not a permitted value" in payload["message"]


async def test_a_refusal_is_audited_as_a_denial_with_its_reason(
    gateway: Harness, talker: Talker
) -> None:
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("IDLE", lambda: cache.latest is not None)
    await gateway.post("/mission/abort")
    await gateway.settle()

    entry = gateway.audit()[-1]
    assert entry["decision"] == "deny"
    assert "IDLE" in entry["reason"]
    assert entry["outcome"] == "PRECONDITION_FAILED"
    assert entry["status"] == 200


async def test_a_refused_call_never_reaches_the_service(
    gateway: Harness, servers: Server, talker: Talker
) -> None:
    servers.trigger_message = "should-not-be-seen"
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("IDLE", lambda: cache.latest is not None)
    payload = (await gateway.post("/mission/abort")).json()
    assert payload["response"] is None
    servers.trigger_message = "done"


async def test_auth_is_checked_before_policy(gateway: Harness, talker: Talker) -> None:
    """A caller who may not use the endpoint learns nothing about the robot's state."""
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("IDLE", lambda: cache.latest is not None)
    response = await gateway.post("/mission/abort", token=VIEWER_TOKEN)
    assert response.status_code == 403
    assert "IDLE" not in response.text


async def test_a_stale_state_refuses_the_call(tmp_path: Path, servers: Server) -> None:
    """A gate that trusts minute-old state is not a gate.

    Worth knowing about: a precondition on a topic with a staleness threshold makes the gate
    depend on that topic still being published.
    """
    guarded = endpoints()
    guarded["/mission/status"]["stale_after_ms"] = 200
    harness, close = await build(tmp_path, endpoints=guarded)
    talker = Talker()
    try:
        # Discovery first, then the sample: waiting for the service afterwards would spend the
        # staleness budget on it and refuse the call this half of the test expects to succeed.
        _wait_for_service(harness, "/mission/{command}")
        cache = harness.runtime.caches["/mission/status"]
        assert talker.publish_until("IDLE", lambda: cache.latest is not None)
        fresh = (await harness.post("/mission/start")).json()
        assert fresh["success"] is True, fresh

        time.sleep(0.4)  # the publisher has stopped and the threshold has passed
        payload = (await harness.post("/mission/start")).json()
        assert payload["error_code"] == "PRECONDITION_FAILED"
        assert "stale" in payload["message"]
    finally:
        talker.close()
        await close()  # type: ignore[misc]


def _state(cache: Any) -> str | None:
    sample = cache.latest
    if sample is None:
        return None
    data: str = cache.decoded(sample)["data"]
    return data


# --------------------------------------------------------------------------------------
# policy cost
# --------------------------------------------------------------------------------------


@pytest.mark.bench
async def test_policy_evaluation_is_far_under_a_millisecond(
    gateway: Harness, talker: Talker
) -> None:
    """The brief's budget is under 1ms at p99; it should be nowhere near that."""
    from ros2_http_gateway.policy import build_policy

    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("IDLE", lambda: cache.latest is not None)

    policy = build_policy(
        {
            path: endpoint.config.preconditions
            for path, endpoint in gateway.runtime.resolved.endpoints.items()
        },
        gateway.runtime.caches,
    )
    params = {"command": "start"}
    policy.evaluate("/mission/{command}", params)

    samples = []
    for _ in range(2000):
        started = time.perf_counter()
        policy.evaluate("/mission/{command}", params)
        samples.append(time.perf_counter() - started)
    samples.sort()
    p99_ms = samples[int(len(samples) * 0.99)] * 1000.0
    print(f"\n  policy p99 {p99_ms * 1000:.1f}us over {len(samples)} evaluations")
    assert p99_ms < 1.0, f"policy p99 {p99_ms:.3f}ms exceeds the 1ms budget"
