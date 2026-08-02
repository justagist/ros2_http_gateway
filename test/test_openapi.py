"""OpenAPI document tests: validity, coverage, and that it describes what the code does.

A document that merely parses is worse than none, since it is what clients are entitled to
believe. So these check three things: that it satisfies the official OpenAPI 3.1 meta-schema,
that every served route appears in it, and that the schemas it references actually accept what
the converters emit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    config_document,
    resolved_config,
    write_config,
)

from ros2_http_gateway.config import load_config
from ros2_http_gateway.openapi import OPENAPI_VERSION, build_document
from ros2_http_gateway.resolve import resolve

pytestmark = pytest.mark.ros

META_SCHEMA = Path(__file__).parent / "openapi-3.1-schema.json"
GOLDEN = Path(__file__).parent / "golden" / "openapi.json"
EXAMPLES = Path(__file__).parents[1] / "examples"
BASE = "http://gateway/api/v1"

# What the golden document is generated from. Only interface types whose definitions have been
# stable for several distros, so a diff means the generator changed rather than the distro.
GOLDEN_SSE = {"include": ["/mission/status"]}


def golden_bytes(tmp_path: Path) -> bytes:
    """The document as ``--dump-openapi`` writes it, with the version normalised.

    ``info.version`` tracks the package, so leaving it in would make every release bump look
    like a generator change.
    """
    from ros2_http_gateway.cli import dump_bytes

    document = build_document(resolved_config(tmp_path, sse=GOLDEN_SSE))
    document["info"]["version"] = "0.0.0-golden"
    return dump_bytes(document)


@pytest.fixture(scope="module")
def meta_validator() -> jsonschema.Draft202012Validator:
    """The published OpenAPI 3.1 meta-schema, vendored so CI needs no network."""
    with META_SCHEMA.open("rb") as handle:
        return jsonschema.Draft202012Validator(json.load(handle))


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The document for the shared test config: a topic read, a write, a service, an action."""
    tmp_path = tmp_path_factory.mktemp("openapi")
    return build_document(resolved_config(tmp_path, sse={"include": ["/mission/status"]}))


def assert_valid(validator: jsonschema.Draft202012Validator, document: dict[str, Any]) -> None:
    problems = [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    ]
    assert not problems, "the document is not valid OpenAPI 3.1:\n  " + "\n  ".join(problems[:10])


# --------------------------------------------------------------------------------------
# validity
# --------------------------------------------------------------------------------------


def test_the_document_is_valid_openapi(
    document: dict[str, Any], meta_validator: jsonschema.Draft202012Validator
) -> None:
    assert document["openapi"] == OPENAPI_VERSION
    assert_valid(meta_validator, document)


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_every_shipped_example_generates_a_valid_document(
    example: Path, meta_validator: jsonschema.Draft202012Validator
) -> None:
    assert_valid(meta_validator, build_document(resolve(load_config(example))))


def test_every_reference_resolves(document: dict[str, Any]) -> None:
    """A dangling $ref is the failure mode of hoisting schemas into components."""
    available = set(document["components"]["schemas"])
    for ref in _references(document):
        assert ref.startswith("#/components/schemas/"), f"{ref} does not point at components"
        assert ref.removeprefix("#/components/schemas/") in available, f"{ref} dangles"


def test_no_schema_keeps_a_local_defs_block(document: dict[str, Any]) -> None:
    """`$defs` inside a component would resolve against the document root, so it must be gone."""
    assert "$defs" not in json.dumps(document)


# --------------------------------------------------------------------------------------
# coverage: everything served is described
# --------------------------------------------------------------------------------------


def test_every_configured_endpoint_appears(document: dict[str, Any]) -> None:
    paths = document["paths"]
    assert set(paths) >= {
        "/health",
        "/mission/status",
        "/never/stales",
        "/cmd",
        "/call",
        "/pick",
        "/pick/{goal_id}",
        "/pick/{goal_id}/stream",
        "/events",
    }
    assert list(paths["/mission/status"]) == ["get"]
    assert list(paths["/cmd"]) == ["post"]
    assert sorted(paths["/pick/{goal_id}"]) == ["delete", "get"]


async def test_the_routes_in_the_document_are_the_routes_the_app_serves(
    gateway: Any, served: Any
) -> None:
    """The document is generated separately from the router, so they can drift."""
    resolved = served[0]
    app = gateway._transport.app
    base = resolved.config.http.base_path
    routes = {
        (method, route.path.removeprefix(base))
        for route in app.routes
        for method in route.methods or ()
        if method not in ("HEAD", "OPTIONS")
    }

    described = {
        (method.upper(), path)
        for path, operations in build_document(resolved)["paths"].items()
        for method in operations
    }
    # The UI assets are deliberately absent: they are static files, not part of the API.
    assets = {
        ("GET", "/docs"),
        ("GET", "/docs/swagger-ui.css"),
        ("GET", "/docs/swagger-ui-bundle.js"),
    }
    assert routes - assets - {("GET", "/openapi.json")} == described


def test_the_action_routes_describe_the_async_job_pattern(document: dict[str, Any]) -> None:
    submit = document["paths"]["/pick"]["post"]
    assert "202" in submit["responses"], "202 means accepted, not performed"
    assert "GoalAccepted" in json.dumps(submit["responses"]["202"])
    assert "GOAL_REJECTED" in submit["responses"]["200"]["description"]

    status = document["paths"]["/pick/{goal_id}"]["get"]
    body = status["responses"]["200"]["content"]["application/json"]["schema"]
    assert sorted(body["required"]) == ["last_feedback", "result", "status"]
    assert "404" in status["responses"]

    stream = document["paths"]["/pick/{goal_id}/stream"]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]

    cancel = document["paths"]["/pick/{goal_id}"]["delete"]
    assert "ALREADY_TERMINAL" in json.dumps(cancel["responses"]["200"])


def test_the_event_stream_documents_its_events_and_query_token(document: dict[str, Any]) -> None:
    stream = document["paths"]["/events"]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    assert "TOO_MANY_CLIENTS" in json.dumps(stream["responses"]["503"])
    assert "event: /mission/status" in stream["description"]
    (parameter,) = stream["parameters"]
    assert parameter["name"] == "access_token" and parameter["in"] == "query"


# --------------------------------------------------------------------------------------
# the taxonomy and the security model
# --------------------------------------------------------------------------------------


def test_health_is_the_only_operation_without_security(document: dict[str, Any]) -> None:
    unsecured = [
        f"{method} {path}"
        for path, operations in document["paths"].items()
        for method, operation in operations.items()
        if operation.get("security") == []
    ]
    assert unsecured == ["get /health"]
    assert document["security"] == [{"bearerAuth": []}]
    assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"


def test_every_authenticated_operation_documents_401_and_403(document: dict[str, Any]) -> None:
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if operation.get("security") == []:
                continue
            assert "401" in operation["responses"], f"{method} {path} omits 401"
            assert "403" in operation["responses"], f"{method} {path} omits 403"


def test_a_write_route_documents_400_and_a_read_route_does_not(document: dict[str, Any]) -> None:
    """400 means the body would not convert, so it only exists where there is a body."""
    assert "400" in document["paths"]["/cmd"]["post"]["responses"]
    assert "400" in document["paths"]["/call"]["post"]["responses"]
    assert "400" not in document["paths"]["/mission/status"]["get"]["responses"]


def test_a_publish_route_documents_the_queue_full_503(document: dict[str, Any]) -> None:
    assert "PUBLISH_QUEUE_FULL" in json.dumps(document["paths"]["/cmd"]["post"]["responses"]["503"])


def test_a_gated_service_names_its_precondition(tmp_path: Path) -> None:
    """The gate is invisible in the schemas, so it has to be in the prose."""
    config = config_document()
    config["endpoints"]["/mission/{command}"] = {
        "service": "/gateway_test_service",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [
            {
                "state_endpoint": "/mission/status",
                "field": "data",
                "allow": {"start": ["IDLE"], "abort": ["*"]},
            }
        ],
    }
    document = build_document(resolve(load_config(write_config(tmp_path, config))))
    operation = document["paths"]["/mission/{command}"]["post"]

    assert "PRECONDITION_FAILED" in json.dumps(operation["responses"]["200"])
    assert "/mission/status" in operation["description"]
    assert "IDLE" in operation["description"]
    assert "any state" in operation["description"], "a wildcard should read as a wildcard"


def test_a_mapped_path_parameter_says_which_field_it_fills(tmp_path: Path) -> None:
    config = config_document()
    config["endpoints"]["/math/{a}/plus/{b}"] = {
        "service": "/gateway_test_add",
        "type": "example_interfaces/srv/AddTwoInts",
        "roles": ["operator"],
        "request_map": {"a": "{a}", "b": "{b}"},
    }
    document = build_document(resolve(load_config(write_config(tmp_path, config))))
    parameters = document["paths"]["/math/{a}/plus/{b}"]["post"]["parameters"]

    assert {parameter["name"] for parameter in parameters} == {"a", "b"}
    assert all(parameter["in"] == "path" and parameter["required"] for parameter in parameters)
    assert "Fills `a`" in next(p["description"] for p in parameters if p["name"] == "a")


# --------------------------------------------------------------------------------------
# the schemas are the converters' schemas
# --------------------------------------------------------------------------------------


def test_a_topic_read_envelope_wraps_the_types_read_schema(document: dict[str, Any]) -> None:
    body = document["paths"]["/mission/status"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert sorted(body["required"]) == ["data", "seq", "stale", "stamp"]
    reference, null = body["properties"]["data"]["anyOf"]
    assert null == {"type": "null"}, "data is null before the first message arrives"
    assert reference["$ref"] == "#/components/schemas/example_interfaces.msg.String.read"


def test_read_and_write_schemas_are_kept_apart(document: dict[str, Any]) -> None:
    """They differ -- a read has every field required -- so one key cannot serve both."""
    schemas = document["components"]["schemas"]
    assert "required" in schemas["example_interfaces.msg.String.read"]
    assert "required" not in schemas["example_interfaces.msg.String.write"]


def test_an_encoded_message_validates_against_its_documented_schema(
    document: dict[str, Any],
) -> None:
    """The load-bearing agreement: what the converter emits is what the document promises."""
    from rosidl_runtime_py.utilities import get_message

    from ros2_http_gateway.codegen import build_msg_to_dict

    from ros_fixtures import populate

    message_type = get_message("example_interfaces/msg/String")
    encoded = json.loads(
        json.dumps(build_msg_to_dict(message_type, int64_as_string=True)(populate(message_type)))
    )
    schema = dict(document["components"]["schemas"]["example_interfaces.msg.String.read"])
    schema["$defs"] = document["components"]["schemas"]
    jsonschema.Draft202012Validator(_localise(schema)).validate(encoded)


def _localise(node: Any) -> Any:
    """Point component references at a `$defs` copy, so a schema validates standalone."""
    if isinstance(node, dict):
        return {
            key: (
                value.replace("#/components/schemas/", "#/$defs/")
                if key == "$ref" and isinstance(value, str)
                else _localise(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_localise(item) for item in node]
    return node


def _references(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        for value in node.values():
            found.extend(_references(value))
        return found
    if isinstance(node, list):
        return [ref for item in node for ref in _references(item)]
    return []


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_the_dump_is_byte_stable_across_runs(tmp_path: Path) -> None:
    """Anything derived from a set would reorder between processes and defeat a diff."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert golden_bytes(first) == golden_bytes(second)


def test_the_dump_matches_the_golden_document(tmp_path: Path) -> None:
    """Regenerate with `python3 scripts/update_golden.py` and read the diff."""
    assert golden_bytes(tmp_path).decode() == GOLDEN.read_text(), (
        "the generated OpenAPI document changed; run scripts/update_golden.py and review it"
    )


# --------------------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """One ROS runtime for the whole module.

    Deliberately not per test: each context created and destroyed is another chance to trip
    Humble's teardown crash, which took a whole CI run down once. Nothing here mutates ROS state,
    so one runtime serves every test in this file.
    """
    from ros2_http_gateway.runtime import RosRuntime

    tmp_path = tmp_path_factory.mktemp("serving")
    audit_path = tmp_path / "audit.jsonl"
    resolved = resolved_config(
        tmp_path, sse=GOLDEN_SSE, audit={"sink": "file", "path": str(audit_path)}
    )
    runtime = RosRuntime(resolved)
    runtime.start()
    try:
        yield resolved, runtime, audit_path
    finally:
        runtime.stop()


@pytest.fixture
async def gateway(served: Any) -> AsyncIterator[Any]:
    """A fresh app and client over the shared runtime."""
    import httpx

    from ros2_http_gateway.app import build_app
    from ros2_http_gateway.audit import AuditLogger
    from ros2_http_gateway.auth import StaticTokenAuthenticator
    from ros2_http_gateway.config import AuditConfig

    resolved, runtime, audit_path = served
    audit = AuditLogger(AuditConfig(sink="file", path=audit_path))
    await audit.start()
    app = build_app(resolved, runtime, audit, StaticTokenAuthenticator(resolved.config.tokens))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        try:
            yield client
        finally:
            app.state.sse.close()
            await audit.stop()


async def test_the_document_route_needs_a_token(gateway: Any) -> None:
    """Unauthenticated it would hand an anonymous caller the whole attack surface."""
    assert (await gateway.get(f"{BASE}/openapi.json")).status_code == 401
    assert (await gateway.get(f"{BASE}/openapi.json", headers=_bearer("nope"))).status_code == 401


async def test_the_document_route_serves_the_document(gateway: Any) -> None:
    response = await gateway.get(f"{BASE}/openapi.json", headers=_bearer(OPERATOR_TOKEN))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["openapi"] == OPENAPI_VERSION


async def test_a_viewer_may_read_the_document(gateway: Any) -> None:
    """Any authenticated caller, since 403-versus-404 already reveals the same route list."""
    response = await gateway.get(f"{BASE}/openapi.json", headers=_bearer(VIEWER_TOKEN))
    assert response.status_code == 200


async def test_reading_the_document_is_audited(gateway: Any) -> None:
    await gateway.get(f"{BASE}/openapi.json", headers=_bearer(OPERATOR_TOKEN))
    for _ in range(60):
        await asyncio.sleep(0.005)
        allowed = [
            record
            for record in _audit_records(gateway)
            if record["path"].endswith("/openapi.json") and record["decision"] == "allow"
        ]
        if allowed:
            assert allowed[-1]["user"] == "operator-user"
            return
    raise AssertionError("the document route was not audited")


async def test_servers_defaults_to_the_configured_base_path(gateway: Any) -> None:
    response = await gateway.get(f"{BASE}/openapi.json", headers=_bearer(OPERATOR_TOKEN))
    assert response.json()["servers"] == [{"url": "/api/v1"}]


async def test_servers_honours_a_forwarded_prefix(gateway: Any) -> None:
    """Mounted at `location /cell3/` in nginx, try-it-out has to target /cell3/api/v1."""
    response = await gateway.get(
        f"{BASE}/openapi.json",
        headers={**_bearer(OPERATOR_TOKEN), "X-Forwarded-Prefix": "/cell3/"},
    )
    assert response.json()["servers"] == [{"url": "/cell3/api/v1"}]


async def test_the_docs_page_is_served_with_its_assets(gateway: Any) -> None:
    """No token: a browser cannot send a header on a navigation, and the page tells nothing."""
    page = await gateway.get(f"{BASE}/docs")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")

    for asset in ("swagger-ui.css", "swagger-ui-bundle.js"):
        response = await gateway.get(f"{BASE}/docs/{asset}")
        assert response.status_code == 200, asset
        assert len(response.content) > 10_000, f"{asset} looks truncated"


async def test_the_docs_page_references_nothing_external(gateway: Any) -> None:
    """`no CDN` is the requirement, and it is one careless edit away from being false."""
    page = (await gateway.get(f"{BASE}/docs")).text
    assert "http://" not in page and "https://" not in page
    assert 'src="swagger-ui-bundle.js"' in page, "assets must be referenced relatively"


async def test_the_docs_page_fetches_the_document_with_a_token(gateway: Any) -> None:
    """The shell has to authenticate itself, because the document route is not public."""
    page = (await gateway.get(f"{BASE}/docs")).text
    assert 'fetch("openapi.json"' in page
    assert "Bearer" in page


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit_records(client: Any) -> list[dict[str, Any]]:
    path = Path(client._transport.app.state.audit_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------------------
# the offline dump
# --------------------------------------------------------------------------------------


def test_dump_openapi_writes_the_document_without_serving(tmp_path: Path) -> None:
    from ros2_http_gateway import cli

    config = write_config(tmp_path, config_document())
    target = tmp_path / "nested" / "api.json"
    assert cli.main(["--check", "--dump-openapi", str(target), str(config)]) == 0

    document = json.loads(target.read_text())
    assert document["openapi"] == OPENAPI_VERSION
    assert "/mission/status" in document["paths"]


def test_dumping_to_stdout_emits_the_document_and_nothing_else(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """So it pipes into jq without a check report to strip off first."""
    from ros2_http_gateway import cli

    config = write_config(tmp_path, config_document())
    assert cli.main(["--check", "--dump-openapi", "-", str(config)]) == 0

    document = json.loads(capsysbinary.readouterr().out)
    assert document["openapi"] == OPENAPI_VERSION
