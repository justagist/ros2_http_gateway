"""OpenAPI 3.1 document generation.

Built once at startup from the resolved config and the same JSON Schemas
:mod:`ros2_http_gateway.schemagen` derives from the interface definitions, so the document
describes what the generated converters actually accept and emit rather than a hand-written
approximation of it. If the two disagree, the converters are right.

3.1 rather than 3.0 because its schema dialect *is* JSON Schema 2020-12: the generated
schemas drop in unaltered, keeping `null` for a float, `["string", "integer"]` for a wide int,
and `contentEncoding: base64` for a byte array. Under 3.0 every one of those would need a
lossy translation.

Schemas are hoisted into ``components/schemas`` and their ``$ref``s rewritten, because a
``$ref`` inside an OpenAPI document resolves against the document root, not against the schema
that contains it -- a nested ``#/$defs/...`` would silently dangle. Read and write forms of one
type differ, so the component key carries the direction.
"""

from __future__ import annotations

from typing import Any, Final

from .config import EndpointConfig, Precondition
from .resolve import ResolvedConfig, ResolvedEndpoint
from .schemagen import Direction, json_schema_for, schema_ref_name

__all__ = ["build_document"]

OPENAPI_VERSION: Final = "3.1.0"

_JSON: Final = "application/json"
_SSE: Final = "text/event-stream"
_BEARER: Final = "bearerAuth"

# The protocol taxonomy, as the document has to present it: a 4xx never reached ROS, a 503 hit a
# capacity limit, and a 200 carries the ROS-side outcome in `error_code`.
_UNAUTHENTICATED: Final = ("UNAUTHENTICATED",)
_FORBIDDEN: Final = ("FORBIDDEN",)
_BAD_REQUEST: Final = ("BAD_REQUEST",)
_UNKNOWN_GOAL: Final = ("UNKNOWN_GOAL",)


def build_document(resolved: ResolvedConfig) -> dict[str, Any]:
    """Generate the OpenAPI document for a resolved configuration.

    ``servers`` carries ``http.base_path``; the serving route rewrites it per request so a
    gateway behind a prefix advertises URLs that work.
    """
    return _Builder(resolved).build()


class _Builder:
    """Accumulates paths and the component schemas they reference."""

    def __init__(self, resolved: ResolvedConfig) -> None:
        self.resolved = resolved
        self.config = resolved.config
        self.wide = self.config.json.int64_as_string
        self.schemas: dict[str, Any] = {}

    def build(self) -> dict[str, Any]:
        paths: dict[str, Any] = {"/health": _health_path()}
        for path, endpoint in self.resolved.endpoints.items():
            if endpoint.config.kind == "action":
                paths.update(self.action_paths(path, endpoint))
            elif endpoint.config.is_topic_read:
                paths[path] = {"get": self.topic_read(endpoint)}
            elif endpoint.config.is_topic_write:
                paths[path] = {"post": self.topic_write(endpoint)}
            else:
                paths[path] = {"post": self.service(endpoint)}

        if self.config.sse is not None:
            paths[self.config.sse.path] = {"get": self.event_stream()}

        self.schemas["Error"] = _ERROR_SCHEMA
        self.schemas["GoalAccepted"] = _GOAL_ACCEPTED_SCHEMA
        return {
            "openapi": OPENAPI_VERSION,
            "info": {
                "title": "ros2_http_gateway",
                "summary": "Config-driven, authenticated HTTP + SSE gateway for ROS 2.",
                "description": _DESCRIPTION,
                "version": _version(),
                "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
            },
            "servers": [{"url": self.config.http.base_path or "/"}],
            "security": [{_BEARER: []}],
            "paths": paths,
            "components": {
                "securitySchemes": {
                    _BEARER: {
                        "type": "http",
                        "scheme": "bearer",
                        "description": (
                            "A token from the gateway's `auth.tokens`. Compared against the "
                            "configured SHA-256 digests in constant time."
                        ),
                    }
                },
                "schemas": {key: self.schemas[key] for key in sorted(self.schemas)},
            },
        }

    # ------------------------------------------------------------------ schema plumbing

    def reference(self, endpoint_type: Any, direction: Direction) -> dict[str, Any]:
        """Register a type's schema under ``components`` and return a reference to it."""
        schema = json_schema_for(endpoint_type, direction=direction, int64_as_string=self.wide)
        key = _component_key(schema_ref_name(endpoint_type), direction)
        for nested_key, nested in schema.pop("$defs", {}).items():
            self.schemas.setdefault(
                _component_key(nested_key, direction), _rewrite(nested, direction)
            )
        self.schemas[key] = _rewrite(schema, direction)
        return {"$ref": f"#/components/schemas/{key}"}

    # ---------------------------------------------------------------------- topics

    def topic_read(self, endpoint: ResolvedEndpoint) -> dict[str, Any]:
        body = {
            "type": "object",
            "required": ["data", "stamp", "seq", "stale"],
            "properties": {
                "data": _nullable(self.reference(endpoint.message_type, "read")),
                "stamp": {
                    **_nullable({"type": "number"}),
                    "description": "Wall-clock time the sample arrived. Null before the first.",
                },
                "seq": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Monotonic per endpoint, 0 before the first sample.",
                },
                "stale": {
                    "type": "boolean",
                    "description": (
                        "True when the newest sample is older than `stale_after_ms`, and "
                        "always true before the first sample arrives."
                    ),
                },
            },
        }
        return {
            "summary": f"Read the latest sample of {endpoint.ros_name}",
            "description": _topic_read_description(endpoint),
            "operationId": _operation_id("read", endpoint.config.path),
            "tags": ["topics"],
            "parameters": self.parameters(endpoint.config),
            "responses": {
                "200": _json_response("The newest cached sample.", body),
                **_refusals(_UNAUTHENTICATED + _FORBIDDEN),
            },
        }

    def topic_write(self, endpoint: ResolvedEndpoint) -> dict[str, Any]:
        return {
            "summary": f"Publish to {endpoint.ros_name}",
            "description": (
                f"Publishes the request body to `{endpoint.ros_name}` "
                f"(`{endpoint.type_name}`). Returns once the message is queued for the ROS "
                "thread; a publisher cannot know whether anyone received it."
            ),
            "operationId": _operation_id("publish", endpoint.config.path),
            "tags": ["topics"],
            "parameters": self.parameters(endpoint.config),
            "requestBody": self.request_body(endpoint.message_type),
            "responses": {
                "200": _json_response("Queued for publication.", _SUCCESS_SCHEMA),
                **_refusals(_BAD_REQUEST + _UNAUTHENTICATED + _FORBIDDEN),
                "503": _error_response(
                    "The ROS side is not draining published messages fast enough.",
                    ("PUBLISH_QUEUE_FULL",),
                ),
            },
        }

    # -------------------------------------------------------------------- services

    def service(self, endpoint: ResolvedEndpoint) -> dict[str, Any]:
        outcomes = ["TIMEOUT", "SERVICE_UNAVAILABLE"]
        if endpoint.config.preconditions:
            outcomes.append("PRECONDITION_FAILED")
        body = {
            "type": "object",
            "required": ["success", "message", "error_code", "response"],
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": (
                        "Whether the gateway completed the call, not whether the robot "
                        "agreed with it. A service answering `success: false` in its own "
                        "response is still `true` here, with its fields under `response`."
                    ),
                },
                "message": {"type": "string"},
                "error_code": _enum_or_null(outcomes),
                "response": _nullable(self.reference(endpoint.interface.Response, "read")),
            },
        }
        return {
            "summary": f"Call {endpoint.ros_name}",
            "description": _service_description(endpoint),
            "operationId": _operation_id("call", endpoint.config.path),
            "tags": ["services"],
            "parameters": self.parameters(endpoint.config),
            "requestBody": self.request_body(endpoint.interface.Request),
            "responses": {
                "200": _json_response(
                    "The call was authorised and well formed. `error_code` carries the "
                    "ROS-side outcome.",
                    body,
                ),
                **_refusals(_BAD_REQUEST + _UNAUTHENTICATED + _FORBIDDEN),
            },
        }

    # --------------------------------------------------------------------- actions

    def action_paths(self, path: str, endpoint: ResolvedEndpoint) -> dict[str, Any]:
        goal_id = {
            "name": "goal_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "description": "The id returned when the goal was submitted.",
        }
        params = self.parameters(endpoint.config)
        status = {
            "type": "object",
            "required": ["status", "result", "last_feedback"],
            "properties": {
                "status": {
                    "enum": [
                        "accepted",
                        "executing",
                        "canceling",
                        "succeeded",
                        "aborted",
                        "canceled",
                    ],
                    "description": (
                        "A terminal status always comes with a `result`: the gateway keeps "
                        "reporting the running status until it holds both."
                    ),
                },
                "result": _nullable(self.reference(endpoint.interface.Result, "read")),
                "last_feedback": _nullable(self.reference(endpoint.interface.Feedback, "read")),
            },
        }
        return {
            path: {
                "post": {
                    "summary": f"Submit a goal to {endpoint.ros_name}",
                    "description": _action_description(endpoint, self.config.actions.result_ttl_s),
                    "operationId": _operation_id("submit", path),
                    "tags": ["actions"],
                    "parameters": params,
                    "requestBody": self.request_body(endpoint.interface.Goal),
                    "responses": {
                        "202": _json_response(
                            "The action server accepted the goal.",
                            {"$ref": "#/components/schemas/GoalAccepted"},
                        ),
                        "200": _error_response(
                            "The request reached ROS, which would not start the goal.",
                            ("GOAL_REJECTED", "SERVICE_UNAVAILABLE")
                            + (("PRECONDITION_FAILED",) if endpoint.config.preconditions else ()),
                        ),
                        **_refusals(_BAD_REQUEST + _UNAUTHENTICATED + _FORBIDDEN),
                    },
                }
            },
            f"{path}/{{goal_id}}": {
                "get": {
                    "summary": "Read a goal's status, result and newest feedback",
                    "operationId": _operation_id("status", path),
                    "tags": ["actions"],
                    "parameters": [*params, goal_id],
                    "responses": {
                        "200": _json_response("The goal as the gateway currently sees it.", status),
                        **_refusals(_UNAUTHENTICATED + _FORBIDDEN + _UNKNOWN_GOAL),
                    },
                },
                "delete": {
                    "summary": "Request cancellation of a goal",
                    "description": (
                        "Never gated by preconditions: a state gate able to stop an operator "
                        "aborting a goal would be the opposite of a safety feature."
                    ),
                    "operationId": _operation_id("cancel", path),
                    "tags": ["actions"],
                    "parameters": [*params, goal_id],
                    "responses": {
                        "200": _json_response(
                            "The server accepted the cancellation, or explained why not.",
                            {
                                "type": "object",
                                "required": ["success", "message", "error_code"],
                                "properties": {
                                    "success": {"type": "boolean"},
                                    "message": {"type": "string"},
                                    "error_code": _enum_or_null(
                                        ["CANCEL_REJECTED", "ALREADY_TERMINAL"]
                                    ),
                                },
                            },
                        ),
                        **_refusals(_UNAUTHENTICATED + _FORBIDDEN + _UNKNOWN_GOAL),
                    },
                },
            },
            f"{path}/{{goal_id}}/stream": {
                "get": {
                    "summary": "Stream a goal's feedback until it finishes",
                    "description": (
                        "Server-sent events: `event: feedback` frames carrying the feedback "
                        "message, then exactly one `event: terminal` frame carrying "
                        '`{"status": ..., "result": ...}`, then the stream closes.\n\n'
                        "The per-client backlog is 64 frames and a slow reader loses its "
                        "**oldest** feedback, so gaps are expected behaviour rather than a "
                        "fault. The terminal frame is never dropped."
                    ),
                    "operationId": _operation_id("stream", path),
                    "tags": ["actions"],
                    "parameters": [*params, goal_id],
                    "responses": {
                        "200": _stream_response("The feedback stream."),
                        **_refusals(_UNAUTHENTICATED + _FORBIDDEN + _UNKNOWN_GOAL),
                    },
                }
            },
        }

    # ------------------------------------------------------------------------- sse

    def event_stream(self) -> dict[str, Any]:
        sse = self.config.sse
        assert sse is not None
        events = "\n".join(
            f"* `event: {key}` -- the body of `GET {key}`, byte for byte." for key in sse.include
        )
        return {
            "summary": "Stream every included endpoint over one connection",
            "description": (
                "On connect, one snapshot per included endpoint, then deltas:\n\n"
                f"{events}\n\n"
                "A comment frame every 15 seconds keeps proxies from idling the connection "
                "out. Only the newest sample of an endpoint is streamed, so `seq` may skip; "
                "immediately after the snapshot one frame may repeat or precede a snapshot's "
                "`seq`, because subscription happens before the snapshot is built.\n\n"
                "**Each client sees only the endpoints its own roles permit.** A token "
                "holding none of them is refused.\n\n"
                f"Overflow policy is `{sse.overflow}`; past `max_clients` "
                f"({sse.max_clients}) the route answers 503."
            ),
            "operationId": "streamEvents",
            "tags": ["events"],
            "parameters": [
                {
                    "name": "access_token",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": (
                        "For browser `EventSource`, which cannot set headers. This is the "
                        "only route that accepts it. Requests authenticated this way are "
                        "marked in the audit record; the query string itself is never logged."
                    ),
                }
            ],
            "responses": {
                "200": _stream_response("The event stream."),
                **_refusals(_UNAUTHENTICATED + _FORBIDDEN),
                "503": _error_response(
                    "The stream is at its configured client limit.", ("TOO_MANY_CLIENTS",)
                ),
            },
        }

    # ------------------------------------------------------------------- shared bits

    def request_body(self, message_type: Any) -> dict[str, Any]:
        return {
            "required": True,
            "content": {_JSON: {"schema": self.reference(message_type, "write")}},
        }

    def parameters(self, endpoint: EndpointConfig) -> list[dict[str, Any]]:
        """Path parameters, with any that feed the request message noted as such."""
        filled = {
            target: template
            for target, template in endpoint.request_map.items()
            if template.startswith("{")
        }
        parameters = []
        for name in endpoint.path_params:
            targets = sorted(t for t, template in filled.items() if template == f"{{{name}}}")
            description = "Path parameter."
            if targets:
                description = f"Fills `{'`, `'.join(targets)}` in the request message."
            parameters.append(
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": description,
                }
            )
        return parameters


# ------------------------------------------------------------------------ descriptions


def _topic_read_description(endpoint: ResolvedEndpoint) -> str:
    stale = endpoint.config.stale_after_ms
    bound = (
        f"Flagged `stale` once the newest sample is more than {stale} ms old."
        if stale is not None
        else "No staleness bound is configured, so `stale` is always false."
    )
    return (
        f"The latest cached sample of `{endpoint.ros_name}` (`{endpoint.type_name}`). "
        f'{bound}\n\nBefore any message arrives this answers 200 with `"data": null` '
        "rather than 404: the endpoint is configured and reachable, the topic is simply "
        "quiet. Stale data is returned flagged rather than withheld."
    )


def _service_description(endpoint: ResolvedEndpoint) -> str:
    parts = [
        f"Calls `{endpoint.ros_name}` (`{endpoint.type_name}`) and waits up to "
        f"{endpoint.config.timeout_ms} ms. A timed-out request is withdrawn, so a late reply "
        "cannot be mistaken for the answer to a later call."
    ]
    if endpoint.config.request_map:
        mapped = ", ".join(
            f"`{target}` from `{template}`"
            for target, template in sorted(endpoint.config.request_map.items())
        )
        parts.append(f"Filled from the URL, taking precedence over the body: {mapped}.")
    parts.extend(_precondition_notes(endpoint.config.preconditions))
    return "\n\n".join(parts)


def _action_description(endpoint: ResolvedEndpoint, result_ttl_s: float) -> str:
    parts = [
        f"Submits a goal to `{endpoint.ros_name}` (`{endpoint.type_name}`). 202 means the "
        "server accepted it, not that it finished: poll the status route or read the feedback "
        f"stream. The {endpoint.config.timeout_ms} ms timeout bounds the accept/reject "
        "handshake only, never the goal itself.\n\nA terminal goal stays queryable for "
        f"{result_ttl_s:g} s and then answers 404, the same as an id that never existed."
    ]
    parts.extend(_precondition_notes(endpoint.config.preconditions))
    return "\n\n".join(parts)


def _precondition_notes(preconditions: tuple[Precondition, ...]) -> list[str]:
    notes = []
    for item in preconditions:
        allowed = "; ".join(
            f"`{key}` requires {'any state' if '*' in states else ', '.join(sorted(states))}"
            for key, states in sorted(item.allow.items())
        )
        notes.append(
            f"**Gated on state.** `{'.'.join(item.field_path)}` of `{item.state_endpoint}` "
            f"must permit the call: {allowed}. A refusal is 200 with "
            "`PRECONDITION_FAILED`, as is a state endpoint that is stale or has never "
            "published -- a gate that fails open is not a gate."
        )
    return notes


# ----------------------------------------------------------------------- fixed pieces


_DESCRIPTION: Final = """\
Every route below is generated from one YAML file: nothing is exposed unless it is configured.

**Status codes.** A 4xx means the request never crossed into ROS -- retrying it unchanged will
fail the same way. A 503 means a capacity limit was reached and retrying later may not. A 200
means the request did reach ROS, and `error_code` carries the outcome, because "the service
timed out" is a result rather than a protocol error.

**Authentication.** `Authorization: Bearer <token>`, except for `GET /health`. The event
stream additionally accepts `?access_token=` for browser `EventSource`.
"""

_ERROR_SCHEMA: Final = {
    "type": "object",
    "required": ["success", "error_code", "message"],
    "properties": {
        "success": {"const": False},
        "error_code": {"type": "string"},
        "message": {"type": "string"},
        "field": {
            "type": "string",
            "description": (
                "The dotted path of the offending field, as the client wrote it, when a "
                "`BAD_REQUEST` came from converting the body. Collection elements are marked "
                "with `[]`, as in `status[].values[].key`."
            ),
        },
    },
}

_SUCCESS_SCHEMA: Final = {
    "type": "object",
    "required": ["success"],
    "properties": {"success": {"const": True}},
}

_GOAL_ACCEPTED_SCHEMA: Final = {
    "type": "object",
    "required": ["goal_id"],
    "properties": {
        "goal_id": {
            "type": "string",
            "description": (
                "Namespaced to this endpoint: an id issued by one endpoint cannot be read "
                "through another's routes."
            ),
        }
    },
}


def _health_path() -> dict[str, Any]:
    return {
        "get": {
            "summary": "Liveness, and which executor was chosen",
            "description": "The only unauthenticated route.",
            "operationId": "health",
            "tags": ["health"],
            "security": [],
            "responses": {
                "200": _json_response(
                    "The gateway is serving.",
                    {
                        "type": "object",
                        "required": ["status", "executor", "endpoints"],
                        "properties": {
                            "status": {"const": "ok"},
                            "executor": {"type": "string"},
                            "endpoints": {"type": "integer", "minimum": 0},
                        },
                    },
                )
            },
        }
    }


# --------------------------------------------------------------------------- helpers


def _json_response(description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"description": description, "content": {_JSON: {"schema": schema}}}


def _stream_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {_SSE: {"schema": {"type": "string"}}},
    }


def _error_response(description: str, codes: tuple[str, ...]) -> dict[str, Any]:
    listed = ", ".join(f"`{code}`" for code in codes)
    return {
        "description": f"{description} `error_code` is one of {listed}.",
        "content": {_JSON: {"schema": {"$ref": "#/components/schemas/Error"}}},
    }


_REFUSAL_DESCRIPTIONS: Final = {
    "UNAUTHENTICATED": ("401", "Missing, malformed or unknown bearer token."),
    "FORBIDDEN": ("403", "The token's roles do not include one this endpoint requires."),
    "BAD_REQUEST": ("400", "Malformed body, or a value the target field cannot hold."),
    "UNKNOWN_GOAL": ("404", "No such goal, or its result has been evicted."),
}


def _refusals(codes: tuple[str, ...]) -> dict[str, Any]:
    """The 4xx responses for a route, keyed by status."""
    return {
        _REFUSAL_DESCRIPTIONS[code][0]: _error_response(_REFUSAL_DESCRIPTIONS[code][1], (code,))
        for code in codes
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Widen a schema to accept null, which is what an absent sample or result reads as."""
    if set(schema) == {"type"} and isinstance(schema["type"], str):
        return {"type": [schema["type"], "null"]}
    return {"anyOf": [schema, {"type": "null"}]}


def _enum_or_null(codes: list[str]) -> dict[str, Any]:
    return {
        "type": ["string", "null"],
        "enum": [*codes, None],
        "description": "Null on success.",
    }


def _component_key(type_name: str, direction: Direction) -> str:
    # A message name is CamelCase by convention, so a lowercase suffix cannot collide with one.
    return f"{type_name}.{direction}"


def _rewrite(node: Any, direction: Direction) -> Any:
    """Point every ``$ref`` at ``components/schemas`` instead of a schema-local ``$defs``."""
    if isinstance(node, dict):
        return {
            key: (
                f"#/components/schemas/{_component_key(value.removeprefix('#/$defs/'), direction)}"
                if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/")
                else _rewrite(value, direction)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite(item, direction) for item in node]
    return node


def _operation_id(verb: str, path: str) -> str:
    """A stable, unique operationId built from the route, which is already unique."""
    cleaned = "".join(
        part.capitalize() for part in path.strip("/").replace("{", "").replace("}", "").split("/")
    )
    return f"{verb}{cleaned}"


def _version() -> str:
    from ._version import __version__

    return __version__
