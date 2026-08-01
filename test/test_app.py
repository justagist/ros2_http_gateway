"""HTTP surface tests, driven through the ASGI app with a real ROS runtime behind it.

The app is exercised directly rather than over a socket: these tests are about routing,
authorisation, envelopes and audit records, and a real listener would only add ports and
flakiness. ``test_serve.py`` covers the uvicorn path separately.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    Talker,
    resolved_config,
)

from ros2_http_gateway.app import build_app
from ros2_http_gateway.audit import AuditLogger
from ros2_http_gateway.auth import StaticTokenAuthenticator
from ros2_http_gateway.config import AuditConfig
from ros2_http_gateway.runtime import RosRuntime

pytestmark = pytest.mark.ros

BASE = "http://gateway/api/v1"


class Harness:
    """A running gateway plus a client and access to the audit trail."""

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

    async def get(self, path: str, token: str | None = OPERATOR_TOKEN) -> httpx.Response:
        return await self.client.get(f"{BASE}{path}", headers=_headers(token))

    async def post(
        self, path: str, body: Any = None, token: str | None = OPERATOR_TOKEN
    ) -> httpx.Response:
        return await self.client.post(
            f"{BASE}{path}",
            content=orjson.dumps(body) if body is not None else b"",
            headers=_headers(token),
        )


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[Harness]:
    """Build a gateway over a real ROS runtime, with audit going to a file."""
    audit_path = tmp_path / "audit.jsonl"
    resolved = resolved_config(tmp_path, audit={"sink": "file", "path": str(audit_path)})
    runtime = RosRuntime(resolved)
    runtime.start()
    audit = AuditLogger(AuditConfig(sink="file", path=audit_path))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        try:
            yield Harness(client, runtime, audit_path)
        finally:
            await audit.stop()
            runtime.stop()


# --------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------


async def test_health_needs_no_token(gateway: Harness) -> None:
    response = await gateway.client.get(f"{BASE}/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["executor"]


async def test_health_is_not_audited(gateway: Harness) -> None:
    """It is unauthenticated, so there is no identity or policy decision to record."""
    await gateway.client.get(f"{BASE}/health")
    await _settle(gateway)
    assert not [entry for entry in gateway.audit() if entry["path"].endswith("/health")]


# --------------------------------------------------------------------------------------
# the auth matrix
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "status", "reason"),
    [
        (None, 401, "no bearer token"),
        ("not-a-real-token", 401, "unknown bearer token"),
        (VIEWER_TOKEN, 403, "token lacks a required role"),
        (OPERATOR_TOKEN, 200, None),
    ],
    ids=["no-token", "bad-token", "wrong-role", "ok"],
)
async def test_auth_matrix_and_its_audit_records(
    gateway: Harness, token: str | None, status: int, reason: str | None
) -> None:
    """/cmd requires the operator role, so the viewer token is the wrong-role case."""
    response = await gateway.post("/cmd", {"data": "go"}, token=token)
    assert response.status_code == status

    await _settle(gateway)
    (entry,) = gateway.audit()
    assert entry["status"] == status
    assert entry["reason"] == reason
    assert entry["decision"] == ("allow" if reason is None else "deny")
    assert entry["method"] == "POST"
    assert entry["path"] == "/api/v1/cmd"
    assert entry["latency_ms"] >= 0
    if status == 403:
        assert entry["user"] == "viewer-user", "a role failure still identifies the caller"
    elif status == 401:
        assert entry["user"] is None


async def test_a_role_the_endpoint_allows_is_accepted(gateway: Harness) -> None:
    """The viewer may read status even though it may not publish."""
    assert (await gateway.get("/mission/status", token=VIEWER_TOKEN)).status_code == 200


async def test_denials_never_reach_ros(gateway: Harness, talker: Talker) -> None:
    for token in (None, "wrong", VIEWER_TOKEN):
        await gateway.post("/cmd", {"data": "should-not-arrive"}, token=token)
    time.sleep(0.5)
    assert talker.heard == []


async def test_an_unknown_route_is_a_404_and_is_not_audited(gateway: Harness) -> None:
    """No endpoint means no policy, so there is no decision to record."""
    assert (await gateway.get("/nope")).status_code == 404
    await _settle(gateway)
    assert gateway.audit() == []


# --------------------------------------------------------------------------------------
# topic reads
# --------------------------------------------------------------------------------------


async def test_read_before_any_message(gateway: Harness) -> None:
    response = await gateway.get("/mission/status")
    assert response.status_code == 200
    assert response.json() == {"data": None, "stamp": None, "seq": 0, "stale": True}


async def test_read_returns_fresh_data_then_goes_stale(gateway: Harness, talker: Talker) -> None:
    """The acceptance criterion: fresh while publishing, stale once it stops."""
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("RUNNING", lambda: cache.latest is not None)

    fresh = (await gateway.get("/mission/status")).json()
    assert fresh["data"] == {"data": "RUNNING"}
    assert fresh["stale"] is False
    assert fresh["seq"] >= 1
    assert fresh["stamp"] > 0

    # stale_after_ms is 300 for this endpoint and the publisher has stopped.
    time.sleep(0.5)
    stale = (await gateway.get("/mission/status")).json()
    assert stale["stale"] is True
    assert stale["seq"] == fresh["seq"], "no new message, so the sample must not change"
    assert stale["data"] == fresh["data"], "stale data is still returned, just flagged"


async def test_an_endpoint_without_a_threshold_never_goes_stale(
    gateway: Harness, talker: Talker
) -> None:
    cache = gateway.runtime.caches["/never/stales"]
    assert talker.publish_until("RUNNING", lambda: cache.latest is not None)
    time.sleep(0.5)
    assert (await gateway.get("/never/stales")).json()["stale"] is False


async def test_reads_at_200_hz_keep_up(gateway: Harness, talker: Talker) -> None:
    """Publisher at 200 Hz; every read must see a sequence number that is still advancing."""
    cache = gateway.runtime.caches["/mission/status"]
    assert talker.publish_until("RUNNING", lambda: cache.latest is not None)

    seqs = []
    for _ in range(10):
        for _ in range(5):
            talker.publish("RUNNING")
            time.sleep(0.005)
        seqs.append((await gateway.get("/mission/status")).json()["seq"])

    assert seqs == sorted(seqs), "sequence numbers must never go backwards"
    assert seqs[-1] > seqs[0], "the cache is not being updated while reads happen"


async def test_repeated_reads_between_messages_re_encode_nothing(
    gateway: Harness, talker: Talker
) -> None:
    """What the memoisation is for: a 1 kHz topic read once must decode once."""
    cache = gateway.runtime.caches["/never/stales"]
    assert talker.publish_until("RUNNING", lambda: cache.latest is not None)

    sample = cache.latest
    assert sample is not None
    first = cache.encoded_data(sample)
    assert cache.encoded_data(sample) is first, "the encoded payload should be reused verbatim"


# --------------------------------------------------------------------------------------
# topic writes
# --------------------------------------------------------------------------------------


async def test_publish_reaches_ros(gateway: Harness, talker: Talker) -> None:
    deadline = time.monotonic() + 15
    while not talker.heard and time.monotonic() < deadline:
        response = await gateway.post("/cmd", {"data": "go"})
        assert response.status_code == 200
        assert response.json() == {"success": True}
        time.sleep(0.05)
    assert "go" in talker.wait_for_command()


async def test_an_empty_body_publishes_the_default_message(gateway: Harness) -> None:
    """Absent fields keep their ROS defaults, so an empty body is a legal publish."""
    assert (await gateway.post("/cmd")).status_code == 200


@pytest.mark.parametrize(
    ("body", "fragment", "field"),
    [
        ({"nope": 1}, "unknown field(s) nope", None),
        ({"data": 5}, "expected a string", "data"),
    ],
)
async def test_a_bad_body_is_a_400_with_the_field_named(
    gateway: Harness, body: Any, fragment: str, field: str | None
) -> None:
    response = await gateway.post("/cmd", body)
    assert response.status_code == 400
    payload = response.json()
    assert payload == {
        "success": False,
        "error_code": "BAD_REQUEST",
        "message": pytest.approx(payload["message"]),
        "field": field,
    }
    assert fragment in payload["message"]


async def test_malformed_json_is_a_400(gateway: Harness) -> None:
    response = await gateway.client.post(
        f"{BASE}/cmd", content=b"{not json", headers=_headers(OPERATOR_TOKEN)
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "BAD_REQUEST"


async def test_a_bad_body_is_audited_as_allowed_but_failed(gateway: Harness) -> None:
    """Policy said yes; the request was still refused. Both facts belong in the record."""
    await gateway.post("/cmd", {"nope": 1})
    await _settle(gateway)
    (entry,) = gateway.audit()
    assert entry["decision"] == "allow"
    assert entry["status"] == 400
    assert entry["outcome"] == "BAD_REQUEST"


# --------------------------------------------------------------------------------------
# a handler that blows up
# --------------------------------------------------------------------------------------


async def test_a_failing_handler_is_still_audited(
    gateway: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 500 path is the one an incident review most wants, so it cannot be the silent one.

    Today's handlers are hard to make raise, which is exactly why this is pinned now rather
    than after phase 3 and 4 handlers grow teeth.
    """
    from ros2_http_gateway.cache import TopicCache

    def explode(self: TopicCache, now_monotonic: float | None = None) -> bytes:
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr(TopicCache, "response_body", explode)

    response = await gateway.get("/mission/status")
    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "error_code": "INTERNAL",
        "message": "the gateway failed to handle this request",
    }

    await _settle(gateway)
    (entry,) = gateway.audit()
    assert entry["status"] == 500
    assert entry["outcome"] == "INTERNAL"
    assert entry["decision"] == "allow", "policy did allow it; the handler is what failed"
    assert entry["user"] == "operator-user"


async def test_a_query_string_never_reaches_the_audit_record(gateway: Harness) -> None:
    """Guards phase 5: the SSE route will accept ?access_token=, and it must not be logged.

    The record carries request.url.path deliberately, not the full URL. If that ever changes,
    the credential lands in the audit file in plain text.
    """
    await gateway.client.get(
        f"{BASE}/mission/status?access_token=super-secret&other=1",
        headers=_headers(OPERATOR_TOKEN),
    )
    await _settle(gateway)
    (entry,) = gateway.audit()
    assert entry["path"] == "/api/v1/mission/status"
    assert "?" not in entry["path"]
    assert "super-secret" not in orjson.dumps(entry).decode()


# --------------------------------------------------------------------------------------
# not yet implemented kinds
# --------------------------------------------------------------------------------------


async def test_action_endpoints_say_they_are_not_served_yet(gateway: Harness) -> None:
    """A 501 naming the phase beats a 404 that reads like a config mistake."""
    response = await gateway.post("/pick", {})
    assert response.status_code == 501
    payload = response.json()
    assert payload["error_code"] == "NOT_IMPLEMENTED"
    assert "phase 4" in payload["message"]


async def test_a_service_endpoint_is_served_and_reports_a_missing_server(
    gateway: Harness,
) -> None:
    """Nothing offers /gateway_test_service, so this exercises the unavailable path.

    Service behaviour proper lives in test_services.py; this only pins that a configured
    service endpoint is wired up rather than answering 501.
    """
    response = await gateway.post("/call", {})
    assert response.status_code == 200
    assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"


async def test_a_not_implemented_endpoint_still_enforces_auth(gateway: Harness) -> None:
    assert (await gateway.post("/pick", {}, token=None)).status_code == 401
    assert (await gateway.post("/pick", {}, token=VIEWER_TOKEN)).status_code == 403


# --------------------------------------------------------------------------------------
# base_path
# --------------------------------------------------------------------------------------


async def test_routes_live_under_the_configured_base_path(tmp_path: Path) -> None:
    resolved = resolved_config(
        tmp_path, http={"bind": "127.0.0.1", "port": 8080, "base_path": "/g"}
    )
    runtime = RosRuntime(resolved)
    runtime.start()
    audit = AuditLogger(AuditConfig(sink="stderr", path=None))
    await audit.start()
    try:
        app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            assert (await client.get("http://gateway/g/health")).status_code == 200
            assert (await client.get(f"{BASE}/health")).status_code == 404
    finally:
        await audit.stop()
        runtime.stop()


async def _settle(gateway: Harness) -> None:
    """Give the audit writer a chance to flush before the trail is inspected."""
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0.005)
        if gateway.audit():
            return
