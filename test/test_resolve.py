"""Type resolution tests: graph lookups, kind checks, precondition field walking.

Graph shapes that are awkward or flaky to create for real (an absent name, a topic
advertising two types) are driven through a stand-in node. One test resolves against a
real ROS graph so the stand-in cannot drift from the interface it imitates.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

import rclpy
import rclpy.action
from test_config import MINIMAL, write_config

from ros2_http_gateway.config import ConfigError, load_config
from ros2_http_gateway.resolve import resolve

pytestmark = pytest.mark.ros

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class FakeGraph:
    """Minimal stand-in for the node methods that type resolution calls."""

    def __init__(
        self,
        topics: dict[str, list[str]] | None = None,
        services: dict[str, list[str]] | None = None,
        actions: dict[str, list[str]] | None = None,
    ) -> None:
        self.topics = topics or {}
        self.services = services or {}
        self.actions = actions or {}

    def get_topic_names_and_types(self) -> list[tuple[str, list[str]]]:
        return list(self.topics.items())

    def get_service_names_and_types(self) -> list[tuple[str, list[str]]]:
        return list(self.services.items())


@pytest.fixture
def fake_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route rclpy.action graph queries at the stand-in node."""

    def lookup(*, node: Any) -> list[tuple[str, list[str]]]:
        return list(getattr(node, "actions", {}).items())

    monkeypatch.setattr(rclpy.action, "get_action_names_and_types", lookup)


@pytest.fixture(scope="module")
def ros_node() -> Iterator[Any]:
    """A real node in its own context, so the suite does not touch the global one."""
    context = rclpy.context.Context()
    context.init()
    node = rclpy.create_node("gateway_resolve_test", context=context)
    try:
        yield node
    finally:
        node.destroy_node()
        context.try_shutdown()


def load(tmp_path: Path, document: Any) -> Any:
    return load_config(write_config(tmp_path, document))


def resolve_problems(config: Any, **kwargs: Any) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as exc:
        resolve(config, **kwargs)
    return exc.value.problems


# --------------------------------------------------------------------------------------
# explicit types
# --------------------------------------------------------------------------------------


def test_pinned_types_resolve_without_touching_the_graph(tmp_path: Path) -> None:
    """What makes --check usable as a CI gate: no ROS graph required."""
    resolved = resolve(load(tmp_path, MINIMAL), node=None)
    endpoint = resolved.endpoints["/state"]
    assert endpoint.type_name == "example_interfaces/msg/String"
    assert endpoint.to_dict is not None
    assert endpoint.from_dict is None, "a topic GET endpoint never decodes a body"


def test_topic_write_gets_a_decoder_not_an_encoder(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/cmd"] = {
        "topic": "/cmd_vel",
        "type": "geometry_msgs/msg/Twist",
        "method": "POST",
        "roles": ["operator"],
    }
    endpoint = resolve(load(tmp_path, document)).endpoints["/cmd"]
    assert endpoint.from_dict is not None
    assert endpoint.to_dict is None


def test_service_gets_a_request_decoder_and_response_encoder(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/add"] = {
        "service": "/add_two_ints",
        "type": "example_interfaces/srv/AddTwoInts",
        "roles": ["operator"],
    }
    endpoint = resolve(load(tmp_path, document)).endpoints["/add"]
    assert endpoint.from_dict is not None and endpoint.to_dict is not None
    assert endpoint.from_dict({"a": "3", "b": 4}).a == 3
    response = endpoint.interface.Response()
    response.sum = 7
    assert endpoint.to_dict(response) == {"sum": "7"}


def test_action_gets_goal_result_and_feedback_converters(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/pick"] = {
        "action": "/fibonacci",
        "type": "example_interfaces/action/Fibonacci",
        "roles": ["operator"],
    }
    endpoint = resolve(load(tmp_path, document)).endpoints["/pick"]
    assert endpoint.from_dict({"order": 5}).order == 5
    assert endpoint.result_to_dict is not None
    assert endpoint.feedback_to_dict is not None
    assert endpoint.result_to_dict(endpoint.interface.Result()) == {"sequence": []}


def test_topic_with_a_service_type_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["type"] = "std_srvs/srv/Trigger"
    (message,) = resolve_problems(load(tmp_path, document))
    assert "needs a <pkg>/msg/<Name> type" in message


def test_service_with_a_message_type_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/call"] = {
        "service": "/srv",
        "type": "example_interfaces/msg/String",
        "roles": ["operator"],
    }
    (message,) = resolve_problems(load(tmp_path, document))
    assert "needs a <pkg>/srv/<Name> type" in message


def test_unknown_interface_package_is_rejected_with_a_hint(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["type"] = "no_such_pkg/msg/Thing"
    (message,) = resolve_problems(load(tmp_path, document))
    assert "cannot load interface type 'no_such_pkg/msg/Thing'" in message
    assert "installed and sourced" in message


def test_int64_as_string_setting_reaches_the_converters(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["json"] = {"int64_as_string": False}
    document["endpoints"]["/big"] = {
        "topic": "/big",
        "type": "std_msgs/msg/Int64MultiArray",
        "roles": ["viewer"],
    }
    endpoint = resolve(load(tmp_path, document)).endpoints["/big"]
    message = endpoint.interface()
    message.data = [2**53 + 1]
    assert endpoint.to_dict(message)["data"] == [2**53 + 1]


# --------------------------------------------------------------------------------------
# graph resolution
# --------------------------------------------------------------------------------------


def test_omitted_type_resolves_from_a_real_graph(tmp_path: Path, ros_node: Any) -> None:
    from example_interfaces.msg import String

    publisher = ros_node.create_publisher(String, "/live_state", 1)
    try:
        deadline = time.monotonic() + 10.0
        while "/live_state" not in dict(ros_node.get_topic_names_and_types()):
            if time.monotonic() > deadline:
                pytest.skip("the ROS graph never showed the test publisher")
            time.sleep(0.1)

        document = copy.deepcopy(MINIMAL)
        document["endpoints"] = {"/state": {"topic": "/live_state", "roles": ["viewer"]}}
        resolved = resolve(load(tmp_path, document), node=ros_node, discovery_timeout=10.0)
        assert resolved.endpoints["/state"].type_name == "example_interfaces/msg/String"
    finally:
        ros_node.destroy_publisher(publisher)


def test_omitted_type_that_is_not_on_the_graph_says_what_to_do(
    tmp_path: Path, fake_actions: None
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {"/state": {"topic": "/absent", "roles": ["viewer"]}}
    (message,) = resolve_problems(load(tmp_path, document), node=FakeGraph(), discovery_timeout=0.0)
    assert "topic /absent was not on the ROS graph" in message
    assert "Add an explicit `type:`" in message
    assert "--discovery-timeout" in message


def test_ambiguous_topic_type_lists_the_candidates(tmp_path: Path, fake_actions: None) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {"/state": {"topic": "/mixed", "roles": ["viewer"]}}
    graph = FakeGraph(topics={"/mixed": ["std_msgs/msg/String", "example_interfaces/msg/String"]})
    (message,) = resolve_problems(load(tmp_path, document), node=graph, discovery_timeout=0.0)
    assert "advertises 2 types" in message
    assert "example_interfaces/msg/String, std_msgs/msg/String" in message


def test_relative_ros_names_are_made_absolute(tmp_path: Path, fake_actions: None) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {"/state": {"topic": "mission/state", "roles": ["viewer"]}}
    graph = FakeGraph(topics={"/mission/state": ["example_interfaces/msg/String"]})
    resolved = resolve(load(tmp_path, document), node=graph, discovery_timeout=0.0)
    assert resolved.endpoints["/state"].ros_name == "/mission/state"


def test_service_and_action_types_resolve_from_the_graph(
    tmp_path: Path, fake_actions: None
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/call"] = {"service": "/trigger", "roles": ["operator"]}
    document["endpoints"]["/pick"] = {"action": "/fib", "roles": ["operator"]}
    graph = FakeGraph(
        services={"/trigger": ["std_srvs/srv/Trigger"]},
        actions={"/fib": ["example_interfaces/action/Fibonacci"]},
    )
    resolved = resolve(load(tmp_path, document), node=graph, discovery_timeout=0.0)
    assert resolved.endpoints["/call"].type_name == "std_srvs/srv/Trigger"
    assert resolved.endpoints["/pick"].type_name == "example_interfaces/action/Fibonacci"


def test_every_unresolvable_endpoint_is_reported_at_once(
    tmp_path: Path, fake_actions: None
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {
        "/a": {"topic": "/absent_a", "roles": ["viewer"]},
        "/b": {"topic": "/absent_b", "roles": ["viewer"]},
        "/c": {"topic": "/mixed", "roles": ["viewer"]},
    }
    graph = FakeGraph(topics={"/mixed": ["std_msgs/msg/String", "std_msgs/msg/Int32"]})
    assert len(resolve_problems(load(tmp_path, document), node=graph, discovery_timeout=0.0)) == 3


# --------------------------------------------------------------------------------------
# --check --online
# --------------------------------------------------------------------------------------


def test_online_verification_accepts_a_pinned_type_the_graph_agrees_with(
    tmp_path: Path, fake_actions: None
) -> None:
    graph = FakeGraph(topics={"/mission/state": ["example_interfaces/msg/String"]})
    resolved = resolve(
        load(tmp_path, MINIMAL), node=graph, discovery_timeout=0.0, verify_online=True
    )
    assert resolved.endpoints["/state"].type_name == "example_interfaces/msg/String"


def test_online_verification_catches_a_pinned_type_that_is_not_what_is_published(
    tmp_path: Path, fake_actions: None
) -> None:
    """The whole point of the flag: a typo that is syntactically valid and importable."""
    graph = FakeGraph(topics={"/mission/state": ["std_msgs/msg/String"]})
    (message,) = resolve_problems(
        load(tmp_path, MINIMAL), node=graph, discovery_timeout=0.0, verify_online=True
    )
    assert "configured as example_interfaces/msg/String" in message
    assert "/mission/state on the graph is std_msgs/msg/String" in message


def test_online_verification_reports_a_pinned_name_missing_from_the_graph(
    tmp_path: Path, fake_actions: None
) -> None:
    (message,) = resolve_problems(
        load(tmp_path, MINIMAL), node=FakeGraph(), discovery_timeout=0.0, verify_online=True
    )
    assert "was not on the ROS graph" in message
    assert "Add an explicit `type:`" not in message, "the type is already pinned"


def test_offline_check_ignores_the_graph_entirely(tmp_path: Path, fake_actions: None) -> None:
    """A wrong graph must not fail the hermetic check; that is what makes it a CI gate."""
    graph = FakeGraph(topics={"/mission/state": ["std_msgs/msg/String"]})
    assert resolve(load(tmp_path, MINIMAL), node=graph, discovery_timeout=0.0).endpoints


def test_online_verification_covers_services_and_actions(
    tmp_path: Path, fake_actions: None
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/call"] = {
        "service": "/trigger",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    document["endpoints"]["/pick"] = {
        "action": "/fib",
        "type": "example_interfaces/action/Fibonacci",
        "roles": ["operator"],
    }
    graph = FakeGraph(
        topics={"/mission/state": ["example_interfaces/msg/String"]},
        services={"/trigger": ["example_interfaces/srv/Trigger"]},
        actions={"/fib": ["example_interfaces/action/Fibonacci"]},
    )
    (message,) = resolve_problems(
        load(tmp_path, document), node=graph, discovery_timeout=0.0, verify_online=True
    )
    assert "endpoints./call" in message and "std_srvs/srv/Trigger" in message


# --------------------------------------------------------------------------------------
# precondition fields
# --------------------------------------------------------------------------------------


def _with_state_field(field: str, state_type: str = "example_interfaces/msg/String") -> Any:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["type"] = state_type
    document["endpoints"]["/mission/{command}"] = {
        "service": "/mission_control",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [
            {"state_endpoint": "/state", "field": field, "allow": {"start": ["IDLE"]}}
        ],
    }
    return document


def test_precondition_field_on_a_string_message(tmp_path: Path) -> None:
    resolved = resolve(load(tmp_path, _with_state_field("data")))
    assert resolved.endpoints["/mission/{command}"].config.preconditions


def test_dotted_precondition_field_walks_nested_messages(tmp_path: Path) -> None:
    document = _with_state_field("header.frame_id", "geometry_msgs/msg/PoseStamped")
    assert resolve(load(tmp_path, document)).endpoints["/state"]


def test_precondition_field_may_be_an_integer(tmp_path: Path) -> None:
    """State machines often publish an enum constant rather than a name."""
    document = _with_state_field("header.stamp.sec", "geometry_msgs/msg/PoseStamped")
    assert resolve(load(tmp_path, document)).endpoints["/state"]


def test_unknown_precondition_field_lists_the_real_fields(tmp_path: Path) -> None:
    (message,) = resolve_problems(load(tmp_path, _with_state_field("state")))
    assert "the message has no field 'state'" in message
    assert "fields of String: data" in message


def test_unknown_nested_precondition_field_names_the_path_walked(tmp_path: Path) -> None:
    document = _with_state_field("header.nope", "geometry_msgs/msg/PoseStamped")
    (message,) = resolve_problems(load(tmp_path, document))
    assert "header has no field 'nope'" in message


def test_dotted_path_through_a_scalar_is_rejected(tmp_path: Path) -> None:
    (message,) = resolve_problems(load(tmp_path, _with_state_field("data.inner")))
    assert "data is not a message, so it has no subfields" in message


def test_precondition_field_pointing_at_a_message_is_rejected(tmp_path: Path) -> None:
    document = _with_state_field("pose", "geometry_msgs/msg/PoseStamped")
    (message,) = resolve_problems(load(tmp_path, document))
    assert "cannot be compared with the strings in `allow`" in message


def test_precondition_field_pointing_at_an_array_is_rejected(tmp_path: Path) -> None:
    document = _with_state_field("position", "sensor_msgs/msg/JointState")
    (message,) = resolve_problems(load(tmp_path, document))
    assert "cannot be compared with the strings in `allow`" in message


# --------------------------------------------------------------------------------------
# the shipped examples
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["dev.yaml", "locked_down.yaml"])
def test_shipped_examples_resolve(name: str) -> None:
    """The same check CI runs; keeps the examples from rotting."""
    resolved = resolve(load_config(EXAMPLES / name))
    assert resolved.endpoints
    assert all(endpoint.type_name for endpoint in resolved.endpoints.values())


def test_topic_reads_helper_selects_only_cached_endpoints() -> None:
    resolved = resolve(load_config(EXAMPLES / "dev.yaml"))
    assert set(resolved.topic_reads) == {"/mission/status", "/odom"}
