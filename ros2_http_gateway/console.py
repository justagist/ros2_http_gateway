"""The manifest the test console discovers everything from.

One document describing what this caller may touch: kinds, ROS names and types, roles, the
timings that matter, the derived action routes, stream membership, and where each endpoint's
JSON Schemas live in the OpenAPI document. The console hardcodes nothing; if an endpoint is not
in here, the console cannot know about it.

**Filtered by role**, so a token is shown only the endpoints at least one of its roles permits.
That is defence in depth and a correctness property for the console -- what it renders is what the
caller can actually operate -- and *not* a security boundary. Roles gate action, not description:
an authenticated caller can enumerate the configured routes anyway, since an unknown path answers
404 and a known one they may not use answers 403. The 403 is the boundary. Nothing here should be
relied on to hide an endpoint's existence.

Schemas are referenced rather than embedded: a component key plus the document URL, so the
console fetches the OpenAPI document once and resolves pointers into it. Embedding them would
duplicate every nested type per endpoint.

**One deliberate exception to the filtering**: a precondition keeps naming its state endpoint even
when the caller cannot read it. That endpoint's path is not withheld anywhere else -- the policy
engine puts it in the refusal message the caller gets when the gate fires ("<endpoint> is stale
(...)", "<endpoint> has received nothing yet") -- so removing it here would hide in one place what
the gateway volunteers in another the moment the gate matters. A caller entitled to use a gated
endpoint is also entitled to know what gates it. The console handles the unreadable case on its
own side by rendering such a gate as blocked.
"""

from __future__ import annotations

from typing import Any

from .auth import Identity
from .config import EndpointConfig, Precondition
from .resolve import ResolvedConfig, ResolvedEndpoint
from .schemagen import Direction, schema_ref_name
from .sse import HEARTBEAT_S

__all__ = ["build_manifest", "endpoint_kind"]


def endpoint_kind(endpoint: EndpointConfig) -> str:
    """The four shapes a console has to render differently."""
    if endpoint.kind == "topic":
        return "topic_read" if endpoint.is_topic_read else "topic_write"
    return endpoint.kind


def build_manifest(
    resolved: ResolvedConfig, identity: Identity, *, prefix: str = ""
) -> dict[str, Any]:
    """Describe everything ``identity`` may reach, with URLs correct for this request's prefix."""
    config = resolved.config
    base = f"{prefix}{config.http.base_path}"
    streamed = frozenset(config.sse.include if config.sse else ())

    endpoints = [
        _describe(path, endpoint, base, path in streamed)
        for path, endpoint in resolved.endpoints.items()
        if identity.may(endpoint.config.roles)
    ]
    manifest: dict[str, Any] = {
        "user": identity.user,
        "roles": sorted(identity.roles),
        "base_path": base,
        "openapi": f"{base}/openapi.json",
        "endpoints": endpoints,
        "actions": {"result_ttl_s": config.actions.result_ttl_s},
        "json": {"int64_as_string": config.json.int64_as_string},
    }
    visible = {endpoint["path"] for endpoint in endpoints}
    if config.sse is not None and streamed & visible:
        # Filtered against what this caller can see, not against the configured list, because the
        # stream itself filters per client: naming a member they will never receive would
        # misdescribe what the connection delivers. That is a correctness reason rather than a
        # disclosure one -- see the note about preconditions in the module docstring.
        manifest["sse"] = {
            "url": f"{base}{config.sse.path}",
            "include": [key for key in config.sse.include if key in visible],
            "overflow": config.sse.overflow,
            "max_clients": config.sse.max_clients,
            "heartbeat_s": HEARTBEAT_S,
        }
    return manifest


def _describe(path: str, endpoint: ResolvedEndpoint, base: str, streamed: bool) -> dict[str, Any]:
    configured = endpoint.config
    kind = endpoint_kind(configured)
    described: dict[str, Any] = {
        "path": path,
        "url": f"{base}{path}",
        "kind": kind,
        "method": configured.method,
        "ros_name": endpoint.ros_name,
        "type": endpoint.type_name,
        "roles": sorted(configured.roles),
        "path_params": list(configured.path_params),
        "sse": streamed,
        "schemas": _schemas(endpoint, kind, base),
    }
    if kind == "topic_read":
        described["stale_after_ms"] = configured.stale_after_ms
    if kind in ("service", "action"):
        described["timeout_ms"] = configured.timeout_ms
    if configured.request_map:
        described["request_map"] = dict(configured.request_map)
    if configured.preconditions:
        described["preconditions"] = [_precondition(item) for item in configured.preconditions]
    if kind == "action":
        # Derived by the protocol, not written by the config author, so the console must be told
        # rather than left to reconstruct them.
        described["routes"] = {
            "submit": f"{base}{path}",
            "status": f"{base}{path}/{{goal_id}}",
            "stream": f"{base}{path}/{{goal_id}}/stream",
            "cancel": f"{base}{path}/{{goal_id}}",
        }
    return described


def _schemas(endpoint: ResolvedEndpoint, kind: str, base: str) -> dict[str, Any]:
    """Component keys in the OpenAPI document, by direction, plus how to resolve them."""
    keys: dict[str, str] = {}
    if kind == "topic_read":
        keys["read"] = _key(endpoint.message_type, "read")
    elif kind == "topic_write":
        keys["write"] = _key(endpoint.message_type, "write")
    elif kind == "service":
        keys["write"] = _key(endpoint.interface.Request, "write")
        keys["read"] = _key(endpoint.interface.Response, "read")
    else:
        keys["write"] = _key(endpoint.interface.Goal, "write")
        keys["result"] = _key(endpoint.interface.Result, "read")
        keys["feedback"] = _key(endpoint.interface.Feedback, "read")
    return {"document": f"{base}/openapi.json", "pointer": "#/components/schemas", **keys}


def _key(message_type: Any, direction: Direction) -> str:
    return f"{schema_ref_name(message_type)}.{direction}"


def _precondition(item: Precondition) -> dict[str, Any]:
    """Enough for a console to show the gate's live state beside the button it guards.

    ``state_endpoint`` is kept even when this caller cannot read it: see the module docstring for
    why that is the one reference this manifest does not filter.
    """
    return {
        "state_endpoint": item.state_endpoint,
        "field": ".".join(item.field_path),
        "key": item.key,
        "allow": {value: sorted(states) for value, states in item.allow.items()},
    }
