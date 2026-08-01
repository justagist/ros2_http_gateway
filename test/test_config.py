"""Config engine tests. These run without ROS, by design."""

from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from ros2_http_gateway.config import (
    DEFAULT_BASE_PATH,
    DEFAULT_MAX_CLIENTS,
    DEFAULT_PORT,
    DEFAULT_RESULT_TTL_S,
    DEFAULT_TIMEOUT_MS,
    ConfigError,
    load_config,
    load_schema,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

MINIMAL: dict[str, Any] = {
    "endpoints": {
        "/state": {
            "topic": "/mission/state",
            "type": "example_interfaces/msg/String",
            "roles": ["viewer"],
        }
    },
    "auth": {
        "tokens": [
            {"token_sha256": "a" * 64, "user": "hmi", "roles": ["viewer"]},
        ]
    },
}


def write_config(tmp_path: Path, document: Any, *, mode: int = 0o600) -> Path:
    """Write a config file, private by default so plaintext tokens are not refused."""
    target = tmp_path / "gateway.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    target.chmod(mode)
    return target


def load(tmp_path: Path, document: Any, **kwargs: Any) -> Any:
    return load_config(write_config(tmp_path, document, **kwargs))


def problems(tmp_path: Path, document: Any, **kwargs: Any) -> tuple[str, ...]:
    """Load a config expecting failure and return the reported problems."""
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, document, **kwargs)
    return exc.value.problems


def mutate(**endpoint: Any) -> dict[str, Any]:
    """Return MINIMAL with its single endpoint replaced."""
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {"/thing": endpoint}
    return document


# --------------------------------------------------------------------------------------
# the schema itself
# --------------------------------------------------------------------------------------


def test_bundled_schema_is_a_valid_json_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(load_schema())


def test_schema_is_installed_as_package_data() -> None:
    """Editors and CI fetch it from the installed package, not the source tree."""
    import ros2_http_gateway

    assert (Path(ros2_http_gateway.__file__).parent / "config.schema.json").is_file()


@pytest.mark.parametrize("name", ["dev.yaml", "locked_down.yaml"])
def test_shipped_examples_satisfy_the_schema(name: str) -> None:
    document = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(load_schema()).validate(document)


# --------------------------------------------------------------------------------------
# file level failures
# --------------------------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no such file"):
        load_config(tmp_path / "absent.yaml")


def test_malformed_yaml(tmp_path: Path) -> None:
    target = tmp_path / "bad.yaml"
    target.write_text("endpoints: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(target)


def test_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.yaml"
    target.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="file is empty"):
        load_config(target)


def test_non_mapping_top_level(tmp_path: Path) -> None:
    target = tmp_path / "list.yaml"
    target.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_config(target)


def test_missing_endpoints_and_auth(tmp_path: Path) -> None:
    reported = problems(tmp_path, {"http": {"port": 9000}})
    assert any("endpoints" in problem for problem in reported)
    assert any("auth" in problem for problem in reported)


def test_every_schema_problem_is_reported_at_once(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["method"] = "PATCH"
    document["http"] = {"port": 0}
    document["ros"] = {"executor": "quantum"}
    reported = problems(tmp_path, document)
    assert len(reported) >= 3


def test_unknown_key_is_named_with_a_hint(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["stale_after_msec"] = 100
    (message,) = problems(tmp_path, document)
    assert "endpoints./state" in message
    assert "spelling" in message


def test_endpoint_with_two_kinds_is_rejected(tmp_path: Path) -> None:
    reported = problems(
        tmp_path, mutate(topic="/a", service="/b", type="example_interfaces/msg/String")
    )
    assert any("exactly one of topic, service or action" in problem for problem in reported)


def test_endpoint_with_no_kind_is_rejected(tmp_path: Path) -> None:
    reported = problems(tmp_path, mutate(roles=["viewer"]))
    assert any("exactly one of topic, service or action" in problem for problem in reported)


@pytest.mark.parametrize("bad", ["no-leading-slash", "/trailing/", "/has space", ""])
def test_bad_endpoint_paths_are_rejected(tmp_path: Path, bad: str) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {bad: {"topic": "/a", "roles": ["viewer"]}}
    assert problems(tmp_path, document)


@pytest.mark.parametrize("bad", ["std_msgs/String", "std_msgs/msg", "Std_Msgs/msg/String"])
def test_bad_interface_type_strings_are_rejected(tmp_path: Path, bad: str) -> None:
    assert problems(tmp_path, mutate(topic="/a", type=bad, roles=["viewer"]))


def test_private_ros_names_are_rejected(tmp_path: Path) -> None:
    assert problems(tmp_path, mutate(topic="~/state", roles=["viewer"]))


# --------------------------------------------------------------------------------------
# defaults and per-endpoint rules
# --------------------------------------------------------------------------------------


def test_defaults_merge_into_endpoints(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["defaults"] = {"roles": ["operator"], "stale_after_ms": 2000}
    del document["endpoints"]["/state"]["roles"]
    endpoint = load(tmp_path, document).endpoints["/state"]
    assert endpoint.roles == frozenset({"operator"})
    assert endpoint.stale_after_ms == 2000


def test_endpoint_overrides_defaults(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["defaults"] = {"roles": ["operator"], "stale_after_ms": 2000}
    document["endpoints"]["/state"]["stale_after_ms"] = 250
    endpoint = load(tmp_path, document).endpoints["/state"]
    assert endpoint.roles == frozenset({"viewer"})
    assert endpoint.stale_after_ms == 250


def test_endpoint_without_roles_anywhere_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    del document["endpoints"]["/state"]["roles"]
    (message,) = problems(tmp_path, document)
    assert "no roles" in message and "unreachable" in message


def test_defaults_that_do_not_apply_are_silently_ignored(tmp_path: Path) -> None:
    """A single `defaults:` block has to work for a config holding all three kinds."""
    document = copy.deepcopy(MINIMAL)
    document["defaults"] = {"roles": ["operator"], "stale_after_ms": 2000, "timeout_ms": 1000}
    document["endpoints"]["/call"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    config = load(tmp_path, document)
    assert config.endpoints["/state"].stale_after_ms == 2000
    assert config.endpoints["/state"].timeout_ms == DEFAULT_TIMEOUT_MS
    assert config.endpoints["/call"].stale_after_ms is None
    assert config.endpoints["/call"].timeout_ms == 1000


def test_explicit_stale_after_ms_on_a_service_is_rejected(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path,
        mutate(service="/srv", type="std_srvs/srv/Trigger", roles=["operator"], stale_after_ms=100),
    )
    assert "stale_after_ms only applies to a topic GET" in message


def test_explicit_timeout_ms_on_a_topic_is_rejected(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path,
        mutate(topic="/a", type="example_interfaces/msg/String", roles=["viewer"], timeout_ms=100),
    )
    assert "timeout_ms only applies to a service or action" in message


def test_topic_defaults_to_get_and_post_publishes(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/cmd"] = {
        "topic": "/cmd_vel",
        "type": "geometry_msgs/msg/Twist",
        "method": "POST",
        "roles": ["operator"],
    }
    config = load(tmp_path, document)
    assert config.endpoints["/state"].is_topic_read
    assert config.endpoints["/cmd"].is_topic_write


def test_service_endpoint_is_always_post(tmp_path: Path) -> None:
    endpoint = load(
        tmp_path, mutate(service="/srv", type="std_srvs/srv/Trigger", roles=["operator"])
    ).endpoints["/thing"]
    assert endpoint.method == "POST"


def test_get_on_a_service_is_rejected(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path,
        mutate(service="/srv", type="std_srvs/srv/Trigger", roles=["operator"], method="GET"),
    )
    assert "always POST" in message


def test_duplicate_path_parameter_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {
        "/a/{x}/b/{x}": {"service": "/srv", "type": "std_srvs/srv/Trigger", "roles": ["op"]}
    }
    (message,) = problems(tmp_path, document)
    assert "more than once" in message


# --------------------------------------------------------------------------------------
# request_map
# --------------------------------------------------------------------------------------


def test_request_map_accepts_known_path_parameters(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {
        "/math/{a}/plus/{b}": {
            "service": "/add",
            "type": "example_interfaces/srv/AddTwoInts",
            "roles": ["operator"],
            "request_map": {"a": "{a}", "b": "{b}"},
        }
    }
    endpoint = load(tmp_path, document).endpoints["/math/{a}/plus/{b}"]
    assert endpoint.path_params == ("a", "b")
    assert endpoint.request_map == {"a": "{a}", "b": "{b}"}


def test_request_map_referencing_an_unknown_parameter_lists_the_real_ones(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"] = {
        "/math/{a}": {
            "service": "/add",
            "type": "example_interfaces/srv/AddTwoInts",
            "roles": ["operator"],
            "request_map": {"b": "{nope}"},
        }
    }
    (message,) = problems(tmp_path, document)
    assert "{nope}" in message and "available: {a}" in message


def test_request_map_on_a_topic_is_rejected(tmp_path: Path) -> None:
    (message,) = problems(
        tmp_path,
        mutate(
            topic="/a",
            type="geometry_msgs/msg/Twist",
            method="POST",
            roles=["operator"],
            request_map={"linear.x": "1.0"},
        ),
    )
    assert "only supported on a service endpoint" in message


# --------------------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------------------


def _with_precondition(**precondition: Any) -> dict[str, Any]:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/mission/{command}"] = {
        "service": "/mission_control",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [{"state_endpoint": "/state", "field": "data", **precondition}],
    }
    return document


def test_precondition_key_defaults_to_the_sole_path_parameter(tmp_path: Path) -> None:
    document = _with_precondition(allow={"start": ["IDLE"]})
    (precondition,) = load(tmp_path, document).endpoints["/mission/{command}"].preconditions
    assert precondition.key == "command"
    assert precondition.field_path == ("data",)
    assert precondition.permitted_states("start") == frozenset({"IDLE"})
    assert precondition.permitted_states("stop") is None


def test_explicit_key_selects_among_several_path_parameters(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/cell/{a}/mode/{b}"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [
            {
                "state_endpoint": "/state",
                "field": "data",
                "key": "{b}",
                "allow": {"run": ["IDLE"]},
            }
        ],
    }
    (precondition,) = load(tmp_path, document).endpoints["/cell/{a}/mode/{b}"].preconditions
    assert precondition.key == "b"


def test_several_path_parameters_without_a_key_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/cell/{a}/mode/{b}"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [
            {"state_endpoint": "/state", "field": "data", "allow": {"run": ["IDLE"]}}
        ],
    }
    (message,) = problems(tmp_path, document)
    assert "add `key:`" in message and "{a}, {b}" in message


def test_key_naming_a_non_parameter_is_rejected(tmp_path: Path) -> None:
    document = _with_precondition(key="{other}", allow={"start": ["IDLE"]})
    (message,) = problems(tmp_path, document)
    assert "is not a path parameter" in message and "{command}" in message


def test_endpoint_without_path_parameters_may_only_use_the_wildcard_key(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/abort"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
        "preconditions": [
            {"state_endpoint": "/state", "field": "data", "allow": {"abort": ["RUNNING"]}}
        ],
    }
    (message,) = problems(tmp_path, document)
    assert 'only usable allow key is "*"' in message


def test_wildcard_key_matches_any_parameter_value(tmp_path: Path) -> None:
    document = _with_precondition(allow={"start": ["IDLE"], "*": ["RUNNING"]})
    (precondition,) = load(tmp_path, document).endpoints["/mission/{command}"].preconditions
    assert precondition.permitted_states("start") == frozenset({"IDLE"})
    assert precondition.permitted_states("anything-else") == frozenset({"RUNNING"})


def test_wildcard_state_list_means_any_state(tmp_path: Path) -> None:
    document = _with_precondition(allow={"reset": ["*"]})
    (precondition,) = load(tmp_path, document).endpoints["/mission/{command}"].preconditions
    assert precondition.permitted_states("reset") == frozenset({"*"})


def test_precondition_on_a_topic_read_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/state"]["preconditions"] = [
        {"state_endpoint": "/state", "field": "data", "allow": {"*": ["IDLE"]}}
    ]
    (message,) = problems(tmp_path, document)
    assert "not a topic GET" in message


def test_precondition_referencing_an_unknown_endpoint_lists_the_known_ones(tmp_path: Path) -> None:
    document = _with_precondition(allow={"start": ["IDLE"]})
    document["endpoints"]["/mission/{command}"]["preconditions"][0]["state_endpoint"] = "/nope"
    (message,) = problems(tmp_path, document)
    assert "unknown endpoint '/nope'" in message
    assert "configured endpoints: /mission/{command}, /state" in message


def test_precondition_referencing_a_service_endpoint_is_rejected(tmp_path: Path) -> None:
    document = _with_precondition(allow={"start": ["IDLE"]})
    document["endpoints"]["/mission/{command}"]["preconditions"][0]["state_endpoint"] = (
        "/mission/{command}"
    )
    (message,) = problems(tmp_path, document)
    assert "only a topic GET endpoint has a cached sample" in message


# --------------------------------------------------------------------------------------
# sse
# --------------------------------------------------------------------------------------


def test_sse_defaults(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["sse"] = {"include": ["/state"]}
    sse = load(tmp_path, document).sse
    assert sse is not None
    assert (sse.path, sse.overflow, sse.max_clients) == (
        "/events",
        "drop_oldest",
        DEFAULT_MAX_CLIENTS,
    )


def test_no_sse_section_means_no_stream(tmp_path: Path) -> None:
    assert load(tmp_path, MINIMAL).sse is None


def test_sse_include_must_name_a_topic_read_endpoint(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/call"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    document["sse"] = {"include": ["/call"]}
    (message,) = problems(tmp_path, document)
    assert "sse.include[0]" in message
    assert "only a topic GET endpoint has a cached sample" in message


def test_sse_include_of_an_unknown_endpoint_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["sse"] = {"include": ["/not-configured"]}
    (message,) = problems(tmp_path, document)
    assert "unknown endpoint '/not-configured'" in message


def test_sse_path_colliding_with_an_endpoint_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["sse"] = {"include": ["/state"], "path": "/state"}
    (message,) = problems(tmp_path, document)
    assert "sse.path" in message
    assert "the event stream GET /state can match the same request as GET /state" in message


def test_sse_path_shadowed_by_a_template_endpoint_is_rejected(tmp_path: Path) -> None:
    """The old check was exact-string, so a template covering the stream path slipped past."""
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/{anything}"] = {
        "topic": "/a",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    document["sse"] = {"include": ["/state"], "path": "/events"}
    reported = problems(tmp_path, document)
    assert any("event stream GET /events" in message for message in reported)


def test_sse_without_include_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["sse"] = {"max_clients": 4}
    assert problems(tmp_path, document)


# --------------------------------------------------------------------------------------
# action route derivation
# --------------------------------------------------------------------------------------


def _action_endpoint(**extra: Any) -> dict[str, Any]:
    return {
        "action": "/pick_place",
        "type": "example_interfaces/action/Fibonacci",
        "roles": ["operator"],
        **extra,
    }


def test_action_route_shadowing_another_endpoint_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/pick"] = _action_endpoint()
    document["endpoints"]["/pick/history"] = {
        "topic": "/pick_history",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    (message,) = problems(tmp_path, document)
    assert "generated action route GET /pick/{goal_id}" in message
    assert "GET /pick/history" in message


def test_two_templates_of_the_same_shape_and_method_are_rejected(tmp_path: Path) -> None:
    """Which one answers would otherwise come down to route registration order."""
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/a/{x}"] = {
        "service": "/one",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    document["endpoints"]["/a/{y}"] = {
        "service": "/two",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    (message,) = problems(tmp_path, document)
    assert "POST /a/{y} can match the same request as POST /a/{x}" in message


def test_literal_shadowed_by_a_template_on_the_same_method_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/m/status"] = {
        "topic": "/a",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    document["endpoints"]["/m/{command}"] = {
        "topic": "/b",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    (message,) = problems(tmp_path, document)
    assert "GET /m/{command} can match the same request as GET /m/status" in message


def test_literal_and_template_on_different_methods_are_allowed(tmp_path: Path) -> None:
    """The shape the brief's own example uses: a topic GET beside a service POST."""
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/m/status"] = {
        "topic": "/a",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    document["endpoints"]["/m/{command}"] = {
        "service": "/mission_control",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    assert len(load(tmp_path, document).endpoints) == 3


@pytest.mark.parametrize("reserved", ["/health", "/openapi.json", "/docs"])
def test_endpoint_colliding_with_a_built_in_route_is_rejected(
    tmp_path: Path, reserved: str
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"][reserved] = {
        "topic": "/a",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    (message,) = problems(tmp_path, document)
    assert "the built-in" in message and reserved in message


def test_a_catch_all_template_is_rejected_because_it_swallows_the_built_ins(
    tmp_path: Path,
) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/{anything}"] = {
        "topic": "/a",
        "type": "example_interfaces/msg/String",
        "roles": ["viewer"],
    }
    reported = problems(tmp_path, document)
    assert any("/health" in message for message in reported)


def test_action_derived_delete_does_not_collide_with_a_configured_post(tmp_path: Path) -> None:
    """Same shape, different method, so both can be served unambiguously."""
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/pick"] = _action_endpoint()
    document["endpoints"]["/pick/{item}"] = {
        "service": "/pick_one",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator"],
    }
    assert len(load(tmp_path, document).endpoints) == 3


def test_unrelated_paths_do_not_conflict(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/pick"] = _action_endpoint()
    document["endpoints"]["/place"] = _action_endpoint(action="/place_only")
    assert len(load(tmp_path, document).endpoints) == 3


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


def test_plaintext_token_is_hashed_and_not_retained(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [{"token": "secret-token-value", "user": "hmi", "roles": ["op"]}]
    (token,) = load(tmp_path, document, mode=0o600).tokens
    assert token.sha256 == hashlib.sha256(b"secret-token-value").hexdigest()
    assert "secret-token-value" not in repr(token)


def test_plaintext_token_warns_loudly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [{"token": "secret-token-value", "user": "hmi", "roles": ["op"]}]
    with caplog.at_level(logging.WARNING):
        load(tmp_path, document, mode=0o600)
    assert "SECURITY" in caplog.text and "hmi" in caplog.text


def test_plaintext_token_in_a_world_readable_file_is_refused(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [{"token": "secret-token-value", "user": "hmi", "roles": ["op"]}]
    (message,) = problems(tmp_path, document, mode=0o644)
    assert "world-readable" in message and "chmod o-r" in message


def test_hashed_token_in_a_world_readable_file_is_fine(tmp_path: Path) -> None:
    assert load(tmp_path, MINIMAL, mode=0o644).tokens


def test_token_sha256_is_lowercased(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [{"token_sha256": "A" * 64, "user": "hmi", "roles": ["op"]}]
    (token,) = load(tmp_path, document).tokens
    assert token.sha256 == "a" * 64


def test_duplicate_token_across_users_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [
        {"token_sha256": "b" * 64, "user": "one", "roles": ["op"]},
        {"token_sha256": "B" * 64, "user": "two", "roles": ["op"]},
    ]
    (message,) = problems(tmp_path, document)
    assert "already assigned to 'one'" in message


@pytest.mark.parametrize(
    "token",
    [
        {"user": "hmi", "roles": ["op"]},
        {"token": "plaintext-value", "token_sha256": "a" * 64, "user": "hmi", "roles": ["op"]},
    ],
    ids=["neither", "both"],
)
def test_token_must_give_exactly_one_secret(tmp_path: Path, token: dict[str, Any]) -> None:
    """Also pins the message: matched on document path, not a schema-path substring."""
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = [token]
    (message,) = problems(tmp_path, document, mode=0o600)
    assert message == "auth.tokens.0: must give exactly one of token or token_sha256"


def test_empty_token_list_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["auth"]["tokens"] = []
    assert problems(tmp_path, document)


# --------------------------------------------------------------------------------------
# remaining sections
# --------------------------------------------------------------------------------------


def test_http_defaults_to_localhost(tmp_path: Path) -> None:
    http = load(tmp_path, MINIMAL).http
    assert (http.bind, http.port, http.base_path) == ("127.0.0.1", DEFAULT_PORT, DEFAULT_BASE_PATH)


def test_binding_to_all_interfaces_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    document = copy.deepcopy(MINIMAL)
    document["http"] = {"bind": "0.0.0.0"}
    with caplog.at_level(logging.WARNING):
        load(tmp_path, document)
    assert "does not terminate TLS" in caplog.text


def test_trailing_slash_is_stripped_from_base_path(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["http"] = {"base_path": "/api/v2/"}
    assert load(tmp_path, document).http.base_path == "/api/v2"


def test_root_base_path_becomes_empty(tmp_path: Path) -> None:
    """Mounting at the root means routes get no prefix at all."""
    document = copy.deepcopy(MINIMAL)
    document["http"] = {"base_path": "/"}
    assert load(tmp_path, document).http.base_path == ""


def test_audit_defaults_to_stderr(tmp_path: Path) -> None:
    audit = load(tmp_path, MINIMAL).audit
    assert (audit.sink, audit.path) == ("stderr", None)


def test_audit_file_sink_requires_a_path(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["audit"] = {"sink": "file"}
    assert problems(tmp_path, document)


def test_audit_file_sink_with_a_path(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["audit"] = {"sink": "file", "path": "/var/log/gw.jsonl"}
    audit = load(tmp_path, document).audit
    assert audit.sink == "file"
    assert audit.path == Path("/var/log/gw.jsonl")


def test_ros_and_actions_and_json_defaults(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL)
    assert config.ros.executor == "auto"
    assert config.actions.result_ttl_s == DEFAULT_RESULT_TTL_S
    assert config.json.int64_as_string is True


def test_int64_as_string_can_be_disabled(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["json"] = {"int64_as_string": False}
    assert load(tmp_path, document).json.int64_as_string is False


def test_roles_property_collects_every_role(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/call"] = {
        "service": "/srv",
        "type": "std_srvs/srv/Trigger",
        "roles": ["operator", "maintainer"],
    }
    assert load(tmp_path, document).roles == frozenset({"viewer", "operator", "maintainer"})


def test_endpoints_needing_type_resolution(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    document["endpoints"]["/other"] = {"topic": "/other", "roles": ["viewer"]}
    pending = load(tmp_path, document).endpoints_needing_type_resolution()
    assert [endpoint.path for endpoint in pending] == ["/other"]


def test_config_model_is_frozen(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL)
    with pytest.raises((AttributeError, TypeError)):
        config.endpoints["/state"].method = "POST"  # type: ignore[misc]


def test_error_formatting_lists_every_problem(tmp_path: Path) -> None:
    document = copy.deepcopy(MINIMAL)
    del document["endpoints"]["/state"]["roles"]
    document["endpoints"]["/other"] = {"topic": "/other"}
    with pytest.raises(ConfigError) as exc:
        load(tmp_path, document)
    rendered = exc.value.format()
    assert rendered.count("  - ") == len(exc.value.problems) >= 2
