"""The console's server side: manifest, whoami, and serving that is off unless asked for.

The load-bearing property is that the manifest is filtered by role. It is the console's only
source of truth, so an endpoint a token cannot use must not appear in it -- deny by default
applied to knowledge, not just to access.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
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
OUTSIDER_TOKEN = "outsider-token-for-tests"


def document(*, console: bool = True) -> dict[str, Any]:
    """The shared config plus a viewer-visible read, an operator-only read, and the console on."""
    base = config_document()
    base["endpoints"]["/secret/state"] = {
        "topic": STATE_TOPIC,
        "type": "example_interfaces/msg/String",
        "roles": ["operator"],
        "stale_after_ms": 1000,
    }
    base["endpoints"]["/mission/{command}"] = {
        "service": "/gateway_test_service",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "timeout_ms": 1500,
        "preconditions": [
            {
                "state_endpoint": "/mission/status",
                "field": "data",
                "allow": {"start": ["IDLE"], "abort": ["*"]},
            }
        ],
    }
    # Trigger has an empty request, so the mapping goes on a service that has fields to fill.
    base["endpoints"]["/math/{a}/plus/{b}"] = {
        "service": "/gateway_test_add",
        "type": "example_interfaces/srv/AddTwoInts",
        "roles": ["operator"],
        "request_map": {"a": "{a}", "b": "{b}"},
    }
    base["auth"]["tokens"].append(
        {"token": OUTSIDER_TOKEN, "user": "outsider-user", "roles": ["nobody"]}
    )
    base["sse"] = {"include": ["/mission/status", "/secret/state"]}
    base["http"] = {"bind": "127.0.0.1", "port": 8080, "console": console}
    return base


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """One ROS runtime for the module; contexts are not free, especially on Humble."""
    tmp_path = tmp_path_factory.mktemp("console")
    audit_path = tmp_path / "audit.jsonl"
    config = document()
    config["audit"] = {"sink": "file", "path": str(audit_path)}
    resolved = resolve(load_config(write_config(tmp_path, config)))
    runtime = RosRuntime(resolved)
    runtime.start()
    try:
        yield resolved, runtime, audit_path
    finally:
        runtime.stop()


@pytest.fixture
async def gateway(served: Any) -> AsyncIterator[httpx.AsyncClient]:
    resolved, runtime, audit_path = served
    audit = AuditLogger(AuditConfig(sink="file", path=audit_path))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        try:
            yield client
        finally:
            app.state.sse.close()
            await audit.stop()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def manifest_for(gateway: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await gateway.get(f"{BASE}/console/manifest", headers=bearer(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def paths_in(manifest: dict[str, Any]) -> set[str]:
    return {endpoint["path"] for endpoint in manifest["endpoints"]}


# --------------------------------------------------------------------------------------
# the manifest is filtered by role
# --------------------------------------------------------------------------------------


async def test_the_manifest_lists_what_the_token_may_touch(gateway: httpx.AsyncClient) -> None:
    manifest = await manifest_for(gateway, OPERATOR_TOKEN)
    assert manifest["user"] == "operator-user"
    assert manifest["roles"] == ["operator"]
    assert paths_in(manifest) == {
        "/mission/status",
        "/never/stales",
        "/secret/state",
        "/cmd",
        "/call",
        "/mission/{command}",
        "/math/{a}/plus/{b}",
        "/pick",
    }


async def test_a_viewer_is_not_told_about_operator_only_endpoints(
    gateway: httpx.AsyncClient,
) -> None:
    """The whole point: a token never sees an endpoint it cannot use."""
    manifest = await manifest_for(gateway, VIEWER_TOKEN)
    assert paths_in(manifest) == {"/mission/status", "/never/stales"}
    assert "/secret/state" not in json.dumps(manifest), "not even in a stream list or a schema"
    assert "/gateway_test_service" not in json.dumps(manifest), "nor the ROS names behind them"


async def test_a_token_with_no_useful_role_is_forbidden(gateway: httpx.AsyncClient) -> None:
    response = await gateway.get(f"{BASE}/console/manifest", headers=bearer(OUTSIDER_TOKEN))
    assert response.status_code == 403


async def test_the_manifest_needs_a_token(gateway: httpx.AsyncClient) -> None:
    assert (await gateway.get(f"{BASE}/console/manifest")).status_code == 401


async def test_the_stream_section_lists_only_visible_members(gateway: httpx.AsyncClient) -> None:
    """The stream filters per client, so advertising a member they cannot see would be a lie."""
    operator = await manifest_for(gateway, OPERATOR_TOKEN)
    assert operator["sse"]["include"] == ["/mission/status", "/secret/state"]

    viewer = await manifest_for(gateway, VIEWER_TOKEN)
    assert viewer["sse"]["include"] == ["/mission/status"]


# --------------------------------------------------------------------------------------
# the manifest carries enough to need no hardcoding
# --------------------------------------------------------------------------------------


async def test_each_kind_is_named_and_distinguishable(gateway: httpx.AsyncClient) -> None:
    manifest = await manifest_for(gateway, OPERATOR_TOKEN)
    kinds = {endpoint["path"]: endpoint["kind"] for endpoint in manifest["endpoints"]}
    assert kinds["/mission/status"] == "topic_read"
    assert kinds["/cmd"] == "topic_write"
    assert kinds["/call"] == "service"
    assert kinds["/pick"] == "action"


async def test_a_topic_read_carries_its_staleness_bound(gateway: httpx.AsyncClient) -> None:
    entry = await _entry(gateway, "/mission/status")
    assert entry["stale_after_ms"] == 1000
    assert entry["url"] == "/api/v1/mission/status"
    assert entry["ros_name"] == STATE_TOPIC
    assert entry["type"] == "example_interfaces/msg/String"
    assert entry["sse"] is True


async def test_an_unbounded_topic_read_says_so_rather_than_omitting_it(
    gateway: httpx.AsyncClient,
) -> None:
    """A console has to tell "no bound" from "unknown", so the key is present and null."""
    entry = await _entry(gateway, "/never/stales")
    assert entry["stale_after_ms"] is None


async def test_a_service_carries_its_mapping(gateway: httpx.AsyncClient) -> None:
    """Which path parameter fills which request field, so a form can prefill and grey them out."""
    entry = await _entry(gateway, "/math/{a}/plus/{b}")
    assert entry["path_params"] == ["a", "b"]
    assert entry["request_map"] == {"a": "{a}", "b": "{b}"}


async def test_a_service_carries_its_timeout_and_gate(gateway: httpx.AsyncClient) -> None:
    entry = await _entry(gateway, "/mission/{command}")
    assert entry["timeout_ms"] == 1500
    assert entry["path_params"] == ["command"]

    (gate,) = entry["preconditions"]
    assert gate["state_endpoint"] == "/mission/status"
    assert gate["field"] == "data"
    assert gate["key"] == "command"
    assert gate["allow"] == {"start": ["IDLE"], "abort": ["*"]}


async def test_an_action_carries_its_derived_routes(gateway: httpx.AsyncClient) -> None:
    """Derived by the protocol, so the console must be told rather than reconstruct them."""
    entry = await _entry(gateway, "/pick")
    assert entry["routes"] == {
        "submit": "/api/v1/pick",
        "status": "/api/v1/pick/{goal_id}",
        "stream": "/api/v1/pick/{goal_id}/stream",
        "cancel": "/api/v1/pick/{goal_id}",
    }


async def test_schema_links_resolve_in_the_openapi_document(gateway: httpx.AsyncClient) -> None:
    """The links are a component key plus a document URL; both halves have to be real."""
    manifest = await manifest_for(gateway, OPERATOR_TOKEN)
    document_url = manifest["openapi"]
    schemas = (await gateway.get(document_url, headers=bearer(OPERATOR_TOKEN))).json()[
        "components"
    ]["schemas"]

    for endpoint in manifest["endpoints"]:
        links = endpoint["schemas"]
        assert links["document"] == document_url
        for direction in ("read", "write", "result", "feedback"):
            if direction in links:
                assert links[direction] in schemas, f"{endpoint['path']}: {links[direction]}"


async def test_the_manifest_follows_a_forwarded_prefix(gateway: httpx.AsyncClient) -> None:
    manifest = dict(
        (
            await gateway.get(
                f"{BASE}/console/manifest",
                headers={**bearer(OPERATOR_TOKEN), "X-Forwarded-Prefix": "/cell3"},
            )
        ).json()
    )
    assert manifest["base_path"] == "/cell3/api/v1"
    assert manifest["openapi"] == "/cell3/api/v1/openapi.json"
    entry = next(e for e in manifest["endpoints"] if e["path"] == "/pick")
    assert entry["routes"]["stream"] == "/cell3/api/v1/pick/{goal_id}/stream"


# --------------------------------------------------------------------------------------
# whoami
# --------------------------------------------------------------------------------------


async def test_whoami_answers_for_any_valid_token(gateway: httpx.AsyncClient) -> None:
    """Including one whose role no endpoint uses: it is still a token the gateway accepts."""
    for token, user, roles in (
        (OPERATOR_TOKEN, "operator-user", ["operator"]),
        (VIEWER_TOKEN, "viewer-user", ["viewer"]),
        (OUTSIDER_TOKEN, "outsider-user", ["nobody"]),
    ):
        response = await gateway.get(f"{BASE}/whoami", headers=bearer(token))
        assert response.status_code == 200, token
        assert response.json() == {"user": user, "roles": roles}


async def test_whoami_still_needs_a_token(gateway: httpx.AsyncClient) -> None:
    assert (await gateway.get(f"{BASE}/whoami")).status_code == 401
    assert (await gateway.get(f"{BASE}/whoami", headers=bearer("nope"))).status_code == 401


# --------------------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------------------


async def test_the_console_routes_are_audited_like_any_other(
    gateway: httpx.AsyncClient, served: Any
) -> None:
    audit_path: Path = served[2]
    before = _records(audit_path)
    await gateway.get(f"{BASE}/whoami", headers=bearer(VIEWER_TOKEN))
    await gateway.get(f"{BASE}/console/manifest", headers=bearer(OUTSIDER_TOKEN))

    for _ in range(80):
        await asyncio.sleep(0.005)
        fresh = _records(audit_path)[len(before) :]
        by_path = {record["path"]: record for record in fresh}
        if len(by_path) >= 2:
            assert by_path["/api/v1/whoami"]["decision"] == "allow"
            denied = by_path["/api/v1/console/manifest"]
            assert denied["decision"] == "deny"
            assert denied["status"] == 403
            return
    raise AssertionError("the console routes were not audited")


# --------------------------------------------------------------------------------------
# serving, and being off by default
# --------------------------------------------------------------------------------------


async def test_the_console_is_off_unless_asked_for(tmp_path: Path) -> None:
    """Off means absent, not a route that answers 403."""
    resolved = resolve(load_config(write_config(tmp_path, document(console=False))))
    runtime = RosRuntime(resolved)
    runtime.start()
    try:
        app = build_app(
            resolved,
            runtime,
            AuditLogger(AuditConfig(sink="stderr", path=None)),
            StaticTokenAuthenticator(resolved.config.tokens),
        )
        paths = {route.path for route in app.routes}
        assert not [path for path in paths if "console" in path or path.endswith("/whoami")]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            for path in ("/console", "/console/manifest", "/whoami"):
                response = await client.get(f"{BASE}{path}", headers=bearer(OPERATOR_TOKEN))
                assert response.status_code == 404, path
    finally:
        runtime.stop()


def test_the_default_is_off() -> None:
    from ros2_http_gateway.config import HttpConfig

    assert HttpConfig(bind="127.0.0.1", port=8080, base_path="/api/v1").console is False


async def test_the_console_page_and_its_assets_are_served(gateway: httpx.AsyncClient) -> None:
    page = await gateway.get(f"{BASE}/console")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")

    for asset in ("console.css", "console.js"):
        response = await gateway.get(f"{BASE}/console/{asset}")
        assert response.status_code == 200, asset


async def test_every_url_the_console_page_names_is_served(gateway: httpx.AsyncClient) -> None:
    """The same trap the Swagger UI fell into: a relative asset resolves above /console."""
    page = (await gateway.get(f"{BASE}/console")).text
    referenced = re.findall(r'(?:href|src)="(/[^"]+)"', page)
    assert len(referenced) == 2, f"expected the css and the js, got {referenced}"
    for url in referenced:
        assert (await gateway.get(url)).status_code == 200, url


async def test_the_console_page_follows_a_forwarded_prefix(gateway: httpx.AsyncClient) -> None:
    page = (await gateway.get(f"{BASE}/console", headers={"X-Forwarded-Prefix": "/cell3"})).text
    assert sorted(re.findall(r'(?:href|src)="(/[^"]+)"', page)) == [
        "/cell3/api/v1/console/console.css",
        "/cell3/api/v1/console/console.js",
    ]
    assert 'data-api="/cell3/api/v1"' in page


async def test_the_console_page_references_nothing_external(gateway: httpx.AsyncClient) -> None:
    """No CDN, no bundler, no node: the assets are three files in package data."""
    page = (await gateway.get(f"{BASE}/console")).text
    assert "http://" not in page and "https://" not in page


async def test_the_console_page_is_reachable_without_a_token(gateway: httpx.AsyncClient) -> None:
    """A browser cannot put a header on a navigation, so the shell has to be open.

    It carries no information about the API: everything it renders comes from the manifest and
    whoami, both of which are guarded and audited.
    """
    page = await gateway.get(f"{BASE}/console")
    assert page.status_code == 200
    for leaked in (STATE_TOPIC, "/gateway_test_service", "example_interfaces"):
        assert leaked not in page.text


async def _entry(gateway: httpx.AsyncClient, path: str) -> dict[str, Any]:
    manifest = await manifest_for(gateway, OPERATOR_TOKEN)
    return next(entry for entry in manifest["endpoints"] if entry["path"] == path)


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_bytes().decode().splitlines() if line.strip()]
