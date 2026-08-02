"""The console's decisions, tested against the schemas the gateway really generates.

There is no browser in CI, and the parts of the console worth testing are not the DOM: they are
which schema earns a form, what a response means, when a tile goes stale, and what a gate will
decide. `console.js` keeps those behind a DOM-free seam, and this drives them through node with
real schemas taken from the generated OpenAPI document.

Skipped where node is absent, so it never blocks a run; the properties it covers are otherwise
unverifiable without a browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import resolved_config

from ros2_http_gateway.openapi import build_document

pytestmark = pytest.mark.ros

CONSOLE_JS = Path(__file__).parents[1] / "ros2_http_gateway" / "static" / "console" / "console.js"
RUNNER = Path(__file__).parent / "console_logic.mjs"

node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the console logic"
)


@pytest.fixture(scope="module")
def schemas(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every component schema the gateway generates for the shared test config."""
    document = build_document(resolved_config(tmp_path_factory.mktemp("console-logic")))
    return dict(document["components"]["schemas"])


@pytest.fixture(scope="module")
def results(schemas: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run every check in one node invocation, since starting node is the slow part."""
    string_write = "example_interfaces.msg.String.write"
    trigger_read = "std_srvs.srv.Trigger_Response.read"
    fibonacci_result = "example_interfaces.action.Fibonacci_Result.read"

    fixtures = {
        "script": str(CONSOLE_JS),
        "schemas": schemas,
        "plan_for": {
            "flat_string": string_write,
            "two_scalars": trigger_read,
            "array_field": fibonacci_result,
        },
        "validate": [
            {"label": "good", "schema": string_write, "value": {"data": "hello"}},
            {"label": "unknown field", "schema": string_write, "value": {"nope": 1}},
            {"label": "wrong type", "schema": string_write, "value": {"data": 7}},
            {"label": "not an object", "schema": string_write, "value": []},
            {"label": "array of ints", "schema": fibonacci_result, "value": {"sequence": [1, 2]}},
            {
                "label": "array of wrong type",
                "schema": fibonacci_result,
                "value": {"sequence": ["x"]},
            },
        ],
        "classify": [
            {"label": "success", "status": 200, "body": {"success": True, "error_code": None}},
            {
                "label": "ros outcome",
                "status": 200,
                "body": {"success": False, "error_code": "TIMEOUT"},
            },
            {"label": "unauthenticated", "status": 401, "body": {"error_code": "UNAUTHENTICATED"}},
            {"label": "forbidden", "status": 403, "body": {"error_code": "FORBIDDEN"}},
            {"label": "bad request", "status": 400, "body": {"error_code": "BAD_REQUEST"}},
            {"label": "capacity", "status": 503, "body": {"error_code": "PUBLISH_QUEUE_FULL"}},
            {"label": "no answer", "status": 0, "body": None},
        ],
        "staleness": [
            {
                "label": "nothing yet",
                "sample": {"seq": 0, "stale": True},
                "stale_after_ms": 1000,
                "elapsed_ms": 0,
            },
            {
                "label": "fresh",
                "sample": {"seq": 4, "stale": False},
                "stale_after_ms": 1000,
                "elapsed_ms": 100,
            },
            {
                "label": "ageing",
                "sample": {"seq": 4, "stale": False},
                "stale_after_ms": 1000,
                "elapsed_ms": 900,
            },
            {
                "label": "past the bound locally",
                "sample": {"seq": 4, "stale": False},
                "stale_after_ms": 1000,
                "elapsed_ms": 1500,
            },
            {
                "label": "server says stale",
                "sample": {"seq": 4, "stale": True},
                "stale_after_ms": 1000,
                "elapsed_ms": 0,
            },
            {
                "label": "no bound configured",
                "sample": {"seq": 4, "stale": False},
                "stale_after_ms": None,
                "elapsed_ms": 999_999,
            },
        ],
        "gates": [
            {
                "label": "permitted state",
                "precondition": {"field": "data", "key": "command", "allow": {"start": ["IDLE"]}},
                "sample": {"seq": 3, "stale": False, "data": {"data": "IDLE"}},
                "param": "start",
            },
            {
                "label": "forbidden state",
                "precondition": {"field": "data", "key": "command", "allow": {"start": ["IDLE"]}},
                "sample": {"seq": 3, "stale": False, "data": {"data": "RUNNING"}},
                "param": "start",
            },
            {
                "label": "unlisted command",
                "precondition": {"field": "data", "key": "command", "allow": {"start": ["IDLE"]}},
                "sample": {"seq": 3, "stale": False, "data": {"data": "IDLE"}},
                "param": "explode",
            },
            {
                "label": "wildcard state",
                "precondition": {"field": "data", "key": "command", "allow": {"reset": ["*"]}},
                "sample": None,
                "param": "reset",
            },
            {
                "label": "stale state",
                "precondition": {"field": "data", "key": "command", "allow": {"start": ["IDLE"]}},
                "sample": {"seq": 3, "stale": True, "data": {"data": "IDLE"}},
                "param": "start",
            },
            {
                "label": "nothing published",
                "precondition": {"field": "data", "key": "command", "allow": {"start": ["IDLE"]}},
                "sample": {"seq": 0, "stale": True, "data": None},
                "param": "start",
            },
        ],
        "assemble": [
            {
                "label": "blank fields are omitted",
                "schema": string_write,
                "values": {"data": ""},
            },
            {
                "label": "a value is sent",
                "schema": string_write,
                "values": {"data": "hello"},
            },
        ],
        "fill": [
            {"url": "/api/v1/mission/{command}", "params": {"command": "start"}},
            {"url": "/api/v1/math/{a}/plus/{b}", "params": {"a": "2", "b": "3"}},
            {"url": "/api/v1/mission/{command}", "params": {}},
        ],
        "percentiles": [5.0, 1.0, 3.0, 2.0, 4.0],
        "frames": [
            {
                "label": "whole frames",
                "chunks": ['event: /a\ndata: {"seq":1}\n\nevent: /b\ndata: {"seq":2}\n\n'],
            },
            {
                "label": "split mid-frame",
                "chunks": ["event: /a\nda", 'ta: {"seq":1}\n', "\n"],
            },
            {
                "label": "heartbeat between events",
                "chunks": ['event: /a\ndata: {"seq":1}\n\n: keepalive\n\n'],
            },
            {"label": "nothing complete yet", "chunks": ["event: /a\ndata: {"]},
        ],
        "seq": [
            {"label": "contiguous", "observed": [["/a", 1], ["/a", 2], ["/a", 3]]},
            {"label": "one gap of three", "observed": [["/a", 1], ["/a", 5]]},
            {
                "label": "two endpoints interleaved",
                "observed": [["/a", 1], ["/b", 9], ["/a", 2], ["/b", 10]],
            },
        ],
        "audit": [
            {
                "label": "no token",
                "status": 401,
                "body": {"error_code": "UNAUTHENTICATED"},
                "token": None,
            },
            {
                "label": "bad token",
                "status": 401,
                "body": {"error_code": "UNAUTHENTICATED"},
                "token": "nope",
            },
            {
                "label": "wrong role",
                "status": 403,
                "body": {"error_code": "FORBIDDEN"},
                "token": "t",
            },
            {
                "label": "gated",
                "status": 200,
                "body": {"error_code": "PRECONDITION_FAILED", "message": "data is 'IDLE'"},
                "token": "t",
            },
            {"label": "allowed", "status": 200, "body": {"error_code": None}, "token": "t"},
            {
                "label": "ros failure",
                "status": 200,
                "body": {"error_code": "TIMEOUT"},
                "token": "t",
            },
        ],
    }

    path = tmp_path_factory.mktemp("fixtures") / "fixtures.json"
    path.write_text(json.dumps(fixtures), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(RUNNER), str(path)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


# --------------------------------------------------------------------------------------
# schema to form
# --------------------------------------------------------------------------------------


@node
def test_a_flat_message_gets_a_form(results: dict[str, Any]) -> None:
    plan = results["plans"]["flat_string"]
    assert plan["mode"] == "form"
    assert [field["name"] for field in plan["fields"]] == ["data"]
    assert plan["fields"][0]["widget"] == "text"


@node
def test_scalar_widgets_follow_the_schema(results: dict[str, Any]) -> None:
    """A boolean gets a checkbox and a bounded integer a number input, from the schema alone."""
    fields = {field["name"]: field for field in results["plans"]["two_scalars"]["fields"]}
    assert fields["success"]["widget"] == "boolean"
    assert fields["message"]["widget"] == "text"


@node
def test_an_array_falls_back_to_the_raw_editor(results: dict[str, Any]) -> None:
    """Guessing a widget for an array is how a form starts lying about what it will send."""
    plan = results["plans"]["array_field"]
    assert plan["mode"] == "raw"
    assert "sequence" in plan["reason"]


@node
def test_a_form_omits_blank_fields(results: dict[str, Any]) -> None:
    """An absent field keeps the ROS default, which is not the same as sending an empty string."""
    assembled = {check["label"]: check["body"] for check in results["assembled"]}
    assert assembled["blank fields are omitted"] == {}
    assert assembled["a value is sent"] == {"data": "hello"}


# --------------------------------------------------------------------------------------
# client-side validation, against the served schemas
# --------------------------------------------------------------------------------------


@node
def test_validation_accepts_what_the_gateway_accepts(results: dict[str, Any]) -> None:
    problems = {check["label"]: check["problems"] for check in results["validations"]}
    assert problems["good"] == []
    assert problems["array of ints"] == []


@node
def test_validation_refuses_what_the_gateway_would_refuse(results: dict[str, Any]) -> None:
    """Catching these in the browser saves a round trip to learn the same thing."""
    problems = {check["label"]: check["problems"] for check in results["validations"]}
    assert any("unknown field" in problem for problem in problems["unknown field"])
    assert any("expected string" in problem for problem in problems["wrong type"])
    assert problems["not an object"], "an array is not a message"
    assert problems["array of wrong type"], "element types are checked too"


# --------------------------------------------------------------------------------------
# the taxonomy
# --------------------------------------------------------------------------------------


@node
def test_the_four_outcomes_are_distinguished(results: dict[str, Any]) -> None:
    """The protocol's own distinction: did it reach ROS, and did ROS like it."""
    outcomes = {check["label"]: check["outcome"] for check in results["classifications"]}
    assert outcomes["success"] == "ok"
    assert outcomes["ros outcome"] == "ros_error"
    assert outcomes["unauthenticated"] == "refused"
    assert outcomes["forbidden"] == "refused"
    assert outcomes["bad request"] == "refused"
    assert outcomes["capacity"] == "capacity"
    assert outcomes["no answer"] == "unreachable"


@node
def test_a_ros_outcome_reads_differently_from_a_refusal(results: dict[str, Any]) -> None:
    explanations = {check["label"]: check["explanation"] for check in results["classifications"]}
    assert "never reached ROS" in explanations["forbidden"]
    assert "reached ROS" in explanations["ros outcome"]
    assert "never" not in explanations["ros outcome"]


# --------------------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------------------


@node
def test_a_tile_ages_and_then_goes_stale(results: dict[str, Any]) -> None:
    states = {check["label"]: check["state"] for check in results["staleness"]}
    assert states["nothing yet"] == "empty"
    assert states["fresh"] == "fresh"
    assert states["ageing"] == "ageing"
    assert states["past the bound locally"] == "stale"


@node
def test_the_server_has_the_last_word_on_staleness(results: dict[str, Any]) -> None:
    """Its `stale` flag wins immediately, whatever the local clock thinks."""
    states = {check["label"]: check["state"] for check in results["staleness"]}
    assert states["server says stale"] == "stale"


@node
def test_an_unbounded_endpoint_never_goes_stale(results: dict[str, Any]) -> None:
    entry = next(check for check in results["staleness"] if check["label"] == "no bound configured")
    assert entry["state"] == "fresh"
    assert entry["remainingMs"] is None


# --------------------------------------------------------------------------------------
# gates, decided the way the gateway decides them
# --------------------------------------------------------------------------------------


@node
def test_the_console_predicts_the_gateways_gate_decision(results: dict[str, Any]) -> None:
    """`gateVerdict` reimplements the policy engine's rules in JavaScript, so it can drift.

    This is the fence around that: do not simplify it away. Together with
    `test_the_audit_hint_matches_what_the_gateway_writes`, which closes the same loop against the
    running gateway, it is the only thing keeping the console's predictions honest.
    """
    verdicts = {check["label"]: check["verdict"] for check in results["gates"]}
    assert verdicts["permitted state"] == "allowed"
    assert verdicts["forbidden state"] == "blocked"
    assert verdicts["unlisted command"] == "blocked"
    assert verdicts["wildcard state"] == "allowed"


@node
def test_a_gate_that_cannot_be_trusted_reads_as_blocked(results: dict[str, Any]) -> None:
    """The same three refusals the policy engine makes: unlisted, stale, and never published."""
    entries = {check["label"]: check for check in results["gates"]}
    assert entries["stale state"]["verdict"] == "blocked"
    assert "stale" in entries["stale state"]["reason"]
    assert entries["nothing published"]["verdict"] == "blocked"
    assert "published nothing" in entries["nothing published"]["reason"]


# --------------------------------------------------------------------------------------
# odds and ends
# --------------------------------------------------------------------------------------


@node
def test_path_parameters_are_substituted_and_escaped(results: dict[str, Any]) -> None:
    filled = results["filled"]
    assert filled[0] == "/api/v1/mission/start"
    assert filled[1] == "/api/v1/math/2/plus/3"
    assert filled[2] == "/api/v1/mission/{command}", "an unfilled parameter stays visible"


@node
def test_the_hammer_reports_percentiles(results: dict[str, Any]) -> None:
    assert results["percentiles"] == {"p50": 3.0, "p95": 5.0, "count": 5}


@node
def test_the_matrix_probe_is_a_body_no_converter_accepts(
    results: dict[str, Any], schemas: dict[str, Any]
) -> None:
    """Which is what makes the authorised column safe: it proves auth without executing anything.

    Asserted against the real write schemas rather than assumed: every one of them sets
    `additionalProperties: false`, so an unknown field is a 400 after the role check has passed.
    """
    probe = results["probe"]
    assert probe == {"__console_probe__": True}
    writes = [key for key in schemas if key.endswith(".write")]
    assert writes, "the test config has no write schemas"
    for key in writes:
        assert schemas[key].get("additionalProperties") is False, key
        assert "__console_probe__" not in (schemas[key].get("properties") or {})


# --------------------------------------------------------------------------------------
# server-sent events, as a reader delivers them
# --------------------------------------------------------------------------------------


@node
def test_complete_frames_are_parsed(results: dict[str, Any]) -> None:
    entry = _labelled(results["frames"], "whole frames")
    assert [(frame["event"], frame["data"]) for frame in entry["frames"]] == [
        ("/a", '{"seq":1}'),
        ("/b", '{"seq":2}'),
    ]
    assert entry["rest"] == ""


@node
def test_a_frame_split_across_reads_survives(results: dict[str, Any]) -> None:
    """A chunk boundary can fall anywhere, including mid-word in a field name."""
    entry = _labelled(results["frames"], "split mid-frame")
    assert [(frame["event"], frame["data"]) for frame in entry["frames"]] == [("/a", '{"seq":1}')]


@node
def test_a_heartbeat_is_kept_as_itself(results: dict[str, Any]) -> None:
    """Dropping comment frames would make a quiet stream indistinguishable from a dead one."""
    entry = _labelled(results["frames"], "heartbeat between events")
    assert [frame["comment"] for frame in entry["frames"]] == [None, "keepalive"]


@node
def test_an_unterminated_frame_is_held_back(results: dict[str, Any]) -> None:
    entry = _labelled(results["frames"], "nothing complete yet")
    assert entry["frames"] == []
    assert entry["rest"].startswith("event: /a")


@node
def test_gaps_are_counted_per_endpoint(results: dict[str, Any]) -> None:
    """Each endpoint on the stream has its own counter, so interleaving is not a gap."""
    by_label = {entry["label"]: entry for entry in results["seq"]}
    assert by_label["contiguous"]["gaps"] == 0
    assert by_label["one gap of three"]["gaps"] == 3
    assert by_label["two endpoints interleaved"]["gaps"] == 0
    assert by_label["two endpoints interleaved"]["firsts"] == 2


# --------------------------------------------------------------------------------------
# audit hints
# --------------------------------------------------------------------------------------


@node
def test_the_audit_hint_names_the_decision_and_its_reason(results: dict[str, Any]) -> None:
    by_label = {entry["label"]: entry for entry in results["audit"]}
    assert by_label["no token"]["reason"] == "no bearer token"
    assert by_label["bad token"]["reason"] == "unknown bearer token"
    assert by_label["wrong role"]["reason"] == "token lacks a required role"
    assert all(
        by_label[name]["decision"] == "deny" for name in ("no token", "bad token", "wrong role")
    )


@node
def test_a_gate_refusal_is_a_denial_with_the_gateways_own_message(results: dict[str, Any]) -> None:
    entry = _labelled(results["audit"], "gated")
    assert entry["decision"] == "deny"
    assert entry["outcome"] == "PRECONDITION_FAILED"
    assert "IDLE" in entry["reason"]


@node
def test_a_ros_failure_is_still_an_allowed_request(results: dict[str, Any]) -> None:
    """The gateway allowed it; ROS is what went wrong. The audit records both facts separately."""
    entry = _labelled(results["audit"], "ros failure")
    assert entry["decision"] == "allow"
    assert entry["outcome"] == "TIMEOUT"
    assert entry["reason"] is None

    allowed = _labelled(results["audit"], "allowed")
    assert allowed["decision"] == "allow"
    assert allowed["outcome"] == "ok"


def _labelled(entries: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return next(entry for entry in entries if entry["label"] == label)
