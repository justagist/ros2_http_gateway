"""The HTTP surface: routes built from the config, nothing discovered at runtime.

Auth, policy and audit wrap each handler in a closure holding its endpoint's configuration
rather than sitting in middleware, which would have to rediscover which endpoint a request
belongs to and then agree with the router about it.

Status codes follow the taxonomy in docs/protocol.md: 4xx means the request never crossed
into ROS, 200 means it did and the body carries the outcome. Both use one envelope shape.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Final

import orjson
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .actions import GoalRecord, GoalRegistry, encode_feedback, status_name, terminal_frame
from .audit import AuditLogger, Decision
from .auth import (
    BAD_CREDENTIAL,
    MISSING_ROLE,
    NO_CREDENTIAL,
    Authenticator,
    Identity,
    bearer_token,
)
from .codegen import ConversionError
from .console import build_manifest
from .openapi import build_document
from .params import apply_bindings
from .policy import PolicyEngine, build_policy
from .resolve import ResolvedConfig, ResolvedEndpoint
from .runtime import (
    ActionUnavailableError,
    RosRuntime,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from .sse import HEARTBEAT_FRAME, HEARTBEAT_S, SseBroadcaster, SseClient

_LOG = logging.getLogger(__name__)

__all__ = ["build_app"]

_JSON: Final = "application/json"
_ORJSON_OPTS: Final = orjson.OPT_SERIALIZE_NUMPY

# Vendored Swagger UI. Only these are served, by name: a directory served by pattern is one
# path-traversal bug away from serving the config file.
_DOCS_DIR: Final = Path(__file__).parent / "static" / "swagger"
_DOCS_ASSETS: Final = {
    "docs/swagger-ui.css": ("swagger-ui.css", "text/css"),
    "docs/swagger-ui-bundle.js": ("swagger-ui-bundle.js", "text/javascript"),
}

# The test console, served only when `http.console` is true. Same rule: named assets, no mount.
_CONSOLE_DIR: Final = Path(__file__).parent / "static" / "console"
_CONSOLE_ASSETS: Final = {
    "console/console.css": ("console.css", "text/css"),
    "console/console.js": ("console.js", "text/javascript"),
}

_SSE_HEADERS: Final = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # For nginx, which buffers streaming responses by default and would hold feedback until
    # the goal finished.
    "X-Accel-Buffering": "no",
}

Handler = Callable[[Request], Awaitable[Response]]


def _json(payload: Any, status: int = 200) -> Response:
    return Response(orjson.dumps(payload, option=_ORJSON_OPTS), status, media_type=_JSON)


class _ErrorResponse(Response):
    """A JSON error response that remembers its code.

    The audit wrapper needs the ``error_code`` the client was given; carrying it here beats
    decoding the body back out.
    """

    def __init__(self, status: int, error_code: str, payload: dict[str, Any]) -> None:
        super().__init__(orjson.dumps(payload, option=_ORJSON_OPTS), status, media_type=_JSON)
        self.error_code = error_code


def _error(status: int, error_code: str, message: str, **extra: Any) -> Response:
    """Build the one error envelope, used by 4xx and by ROS-side failures alike."""
    return _ErrorResponse(
        status,
        error_code,
        {"success": False, "error_code": error_code, "message": message, **extra},
    )


def _envelope(status: int, error_code: str, message: str) -> Response:
    """A service-shaped failure: still HTTP 200, because the request itself was fine."""
    return _ErrorResponse(
        status,
        error_code,
        {"success": False, "message": message, "error_code": error_code, "response": None},
    )


def build_app(
    resolved: ResolvedConfig,
    runtime: RosRuntime,
    audit: AuditLogger,
    authenticator: Authenticator,
    *,
    lifespan: Any | None = None,
) -> Starlette:
    """Build the ASGI application for a resolved configuration.

    The serving path passes a ``lifespan`` to start and stop the audit writer; tests manage the
    logger themselves.
    """
    policy = build_policy(
        {path: e.config.preconditions for path, e in resolved.endpoints.items()},
        runtime.caches,
    )
    goals = GoalRegistry(resolved.config.actions.result_ttl_s)
    base = resolved.config.http.base_path
    routes = [Route(f"{base}/health", _health(resolved, runtime), methods=["GET"])]
    routes.extend(_endpoint_routes(resolved, runtime, audit, authenticator, policy, goals, base))

    broadcaster: SseBroadcaster | None = None
    if resolved.config.sse is not None:
        sse = resolved.config.sse
        broadcaster = SseBroadcaster(
            runtime.caches,
            sse.include,
            overflow=sse.overflow,
            max_clients=sse.max_clients,
        )
        roles = frozenset[str]().union(
            *(resolved.endpoints[key].config.roles for key in sse.include)
        )
        routes.append(
            Route(
                f"{base}{sse.path}",
                _guard(
                    _events(broadcaster, resolved),
                    roles,
                    sse.path,
                    audit,
                    authenticator,
                    PolicyEngine({}),
                    allow_query_token=True,
                ),
                methods=["GET"],
            )
        )

    document = build_document(resolved)
    everyone = frozenset[str]().union(
        *(endpoint.config.roles for endpoint in resolved.endpoints.values())
    )
    routes.append(
        Route(
            f"{base}/openapi.json",
            _guard(
                _openapi(document, base), everyone, "/openapi.json", audit, authenticator, policy
            ),
            methods=["GET"],
        )
    )
    routes.extend(_docs_routes(base))
    # Unconditional, unlike the console: asking a gateway what a token is does not depend on a
    # testing UI being switched on, and it is described in the OpenAPI document as a route
    # every deployment serves.
    routes.append(
        Route(
            f"{base}/whoami",
            _guard(_whoami(), frozenset(), "/whoami", audit, authenticator, policy),
            methods=["GET"],
        )
    )
    if resolved.config.http.console:
        routes.extend(_console_routes(resolved, base, everyone, audit, authenticator, policy))

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.goals = goals
    app.state.sse = broadcaster
    app.state.openapi = document
    app.state.audit_path = resolved.config.audit.path
    return app


def _endpoint_routes(
    resolved: ResolvedConfig,
    runtime: RosRuntime,
    audit: AuditLogger,
    authenticator: Authenticator,
    policy: PolicyEngine,
    goals: GoalRegistry,
    base: str,
) -> Iterator[Route]:
    """Yield one route per configured endpoint, most specific path first.

    Literal segments register ahead of templated ones, so where the config engine allows both to
    coexist on different methods, matching does not depend on dict order.
    """
    ordered = sorted(
        resolved.endpoints.items(),
        key=lambda item: (item[1].config.path.count("{"), item[0]),
    )
    for path, endpoint in ordered:
        if endpoint.config.kind == "action":
            yield from _action_routes(endpoint, runtime, audit, authenticator, policy, goals, base)
            continue
        handler = _endpoint_handler(endpoint, runtime)
        guarded = _guard(
            handler, endpoint.config.roles, endpoint.config.path, audit, authenticator, policy
        )
        yield Route(f"{base}{path}", guarded, methods=[endpoint.config.method])


def _action_routes(
    endpoint: ResolvedEndpoint,
    runtime: RosRuntime,
    audit: AuditLogger,
    authenticator: Authenticator,
    policy: PolicyEngine,
    goals: GoalRegistry,
    base: str,
) -> Iterator[Route]:
    """The four routes an action endpoint generates.

    Only submit is gated by preconditions: a state gate able to stop an operator aborting a goal
    would be precisely backwards, and status and stream change nothing.
    """
    path = endpoint.config.path
    empty = PolicyEngine({})

    def guard(handler: Handler, with_policy: PolicyEngine) -> Handler:
        return _guard(handler, endpoint.config.roles, path, audit, authenticator, with_policy)

    yield Route(
        f"{base}{path}", guard(_goal_submit(endpoint, runtime, goals), policy), methods=["POST"]
    )
    yield Route(
        f"{base}{path}/{{goal_id}}", guard(_goal_status(endpoint, goals), empty), methods=["GET"]
    )
    yield Route(
        f"{base}{path}/{{goal_id}}/stream",
        guard(_goal_stream(endpoint, goals), empty),
        methods=["GET"],
    )
    yield Route(
        f"{base}{path}/{{goal_id}}",
        guard(_goal_cancel(endpoint, runtime, goals), empty),
        methods=["DELETE"],
    )


def _health(resolved: ResolvedConfig, runtime: RosRuntime) -> Handler:
    """The one unauthenticated route."""

    async def health(_: Request) -> Response:
        return _json(
            {
                "status": "ok",
                "executor": runtime.executor_name,
                "endpoints": len(resolved.endpoints),
            }
        )

    return health


def _guard(
    handler: Handler,
    required: frozenset[str],
    policy_path: str,
    audit: AuditLogger,
    authenticator: Authenticator,
    policy: PolicyEngine,
    *,
    allow_query_token: bool = False,
) -> Handler:
    """Wrap a handler with authentication, the role check, policy, timing and audit.

    Preconditions are evaluated here, not in the handler, so every refusal is recorded the same
    way: ``decision: deny`` with the reason. A precondition refusal is still a 200, because the
    request was well formed and authorised.

    One record per request reaching a configured route. A request matching no route is
    Starlette's 404 and is not audited -- a deliberate gap recorded in docs/security.md.

    An empty ``required`` means any authenticated caller will do, which is what ``/whoami``
    needs: a token holding a role no endpoint uses is still a valid token and is entitled to be
    told so.

    ``allow_query_token`` is for the SSE route alone: browser ``EventSource`` cannot set
    headers. The record says which way the credential arrived, never what it was.
    """

    async def guarded(request: Request) -> Response:
        started = time.perf_counter()
        identity: Identity | None = None
        denial: str | None = None
        credential: str | None = None
        response: Response

        token = bearer_token(request.headers.get("authorization"))
        if token is None and allow_query_token:
            token = request.query_params.get("access_token") or None
            credential = "query" if token else None
        if token is None:
            denial, response = NO_CREDENTIAL, _error(401, "UNAUTHENTICATED", "missing bearer token")
        else:
            identity = authenticator.authenticate(token)
            if identity is None:
                denial, response = (
                    BAD_CREDENTIAL,
                    _error(401, "UNAUTHENTICATED", "unknown bearer token"),
                )
            elif required and not identity.may(required):
                denial, response = (
                    MISSING_ROLE,
                    _error(
                        403,
                        "FORBIDDEN",
                        f"this endpoint requires one of: {', '.join(sorted(required))}",
                    ),
                )
            elif (refusal := policy.evaluate(policy_path, request.path_params)) is not None:
                denial, response = (
                    refusal,
                    _envelope(200, "PRECONDITION_FAILED", refusal),
                )
            else:
                request.state.identity = identity
                try:
                    response = await handler(request)
                except Exception:
                    # Letting this reach Starlette's error machinery would leave no audit record
                    # of the one case an incident review most wants, and answer with HTML.
                    _LOG.exception("handler for %s failed", request.url.path)
                    response = _error(500, "INTERNAL", "the gateway failed to handle this request")

        audit.record(
            method=request.method,
            path=request.url.path,
            decision=Decision.DENY if denial else Decision.ALLOW,
            reason=denial,
            status=response.status_code,
            outcome=_outcome(response),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            user=identity.user if identity else None,
            roles=identity.roles if identity else None,
            credential=credential,
        )
        return response

    return guarded


def _outcome(response: Response) -> str:
    """The error code the client was given, or ``ok``."""
    return response.error_code if isinstance(response, _ErrorResponse) else "ok"


def _endpoint_handler(endpoint: ResolvedEndpoint, runtime: RosRuntime) -> Handler:
    """Pick the handler for an endpoint's kind and method."""
    if endpoint.config.is_topic_read:
        return _topic_read(endpoint, runtime)
    if endpoint.config.is_topic_write:
        return _topic_write(endpoint, runtime)
    return _service_call(endpoint, runtime)


def _service_call(endpoint: ResolvedEndpoint, runtime: RosRuntime) -> Handler:
    """POST a body, call the service, return its response.

    ``success`` describes the call, not the robot's opinion of it: a service answering
    ``success: false`` still produces ``"success": true`` here with its own fields under
    ``response``, so a client can tell "could not ask" from "the robot said no".
    """
    path = endpoint.config.path
    from_dict = endpoint.from_dict
    to_dict = endpoint.to_dict
    bindings = endpoint.bindings
    timeout_s = endpoint.config.timeout_ms / 1000.0
    assert from_dict is not None and to_dict is not None

    async def call(request: Request) -> Response:
        raw = await request.body()
        try:
            payload = orjson.loads(raw) if raw else {}
        except orjson.JSONDecodeError as exc:
            return _error(400, "BAD_REQUEST", f"body is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            return _error(400, "BAD_REQUEST", "body must be a JSON object")

        try:
            payload = apply_bindings(bindings, payload, request.path_params)
        except ValueError as exc:
            return _error(400, "BAD_REQUEST", str(exc))

        try:
            message = from_dict(payload)
        except ConversionError as exc:
            return _error(400, "BAD_REQUEST", str(exc), field=exc.field)

        try:
            response = await runtime.call_service(path, message, timeout_s)
        except ServiceUnavailableError:
            return _envelope(
                200,
                "SERVICE_UNAVAILABLE",
                f"no server is offering {endpoint.ros_name}",
            )
        except ServiceTimeoutError:
            return _envelope(
                200,
                "TIMEOUT",
                f"{endpoint.ros_name} did not answer within {endpoint.config.timeout_ms}ms",
            )

        return _json(
            {
                "success": True,
                "message": "",
                "error_code": None,
                "response": to_dict(response),
            }
        )

    return call


def _topic_read(endpoint: ResolvedEndpoint, runtime: RosRuntime) -> Handler:
    """GET the newest cached sample.

    The whole body comes from the cache, which memoises by sequence number, so repeated reads
    between two messages re-encode nothing.
    """
    path = endpoint.config.path

    async def read(_: Request) -> Response:
        return Response(runtime.caches[path].response_body(), 200, media_type=_JSON)

    return read


def _topic_write(endpoint: ResolvedEndpoint, runtime: RosRuntime) -> Handler:
    """POST a body to be published, returning once it is queued for the ROS side."""
    path = endpoint.config.path
    from_dict = endpoint.from_dict
    assert from_dict is not None, "a topic write endpoint always has a decoder"

    async def write(request: Request) -> Response:
        raw = await request.body()
        try:
            payload = orjson.loads(raw) if raw else {}
        except orjson.JSONDecodeError as exc:
            return _error(400, "BAD_REQUEST", f"body is not valid JSON: {exc}")

        try:
            message = from_dict(payload)
        except ConversionError as exc:
            return _error(400, "BAD_REQUEST", str(exc), field=exc.field)

        if not runtime.publish(path, message):
            return _error(
                503,
                "PUBLISH_QUEUE_FULL",
                "the ROS side is not draining published messages fast enough",
            )
        return _json({"success": True})

    return write


# --------------------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------------------


def _goal_submit(endpoint: ResolvedEndpoint, runtime: RosRuntime, goals: GoalRegistry) -> Handler:
    """POST a goal. 202 with its id once the server accepts it.

    202 because the work is accepted, not performed. A refused goal is a 200 with
    ``GOAL_REJECTED``: the request reached ROS, which answered no.
    """
    path = endpoint.config.path
    from_dict = endpoint.from_dict
    feedback_to_dict = endpoint.feedback_to_dict
    result_to_dict = endpoint.result_to_dict
    assert from_dict is not None and feedback_to_dict is not None and result_to_dict is not None

    async def submit(request: Request) -> Response:
        raw = await request.body()
        try:
            payload = orjson.loads(raw) if raw else {}
        except orjson.JSONDecodeError as exc:
            return _error(400, "BAD_REQUEST", f"body is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            return _error(400, "BAD_REQUEST", "body must be a JSON object")
        try:
            goal = from_dict(payload)
        except ConversionError as exc:
            return _error(400, "BAD_REQUEST", str(exc), field=exc.field)

        loop = asyncio.get_running_loop()
        record = goals.create(path)

        def on_feedback(message: Any) -> None:
            # A ROS callback thread. Encode here, once per message however many listeners;
            # only the handover crosses to the loop.
            encoded = encode_feedback(feedback_to_dict, message)
            loop.call_soon_threadsafe(goals.on_feedback, record, encoded)

        try:
            handle = await runtime.send_goal(path, goal, on_feedback)
        except ActionUnavailableError:
            goals.discard(record)
            return _envelope(
                200, "SERVICE_UNAVAILABLE", f"no server is offering {endpoint.ros_name}"
            )

        if not handle.accepted:
            goals.discard(record)
            return _envelope(200, "GOAL_REJECTED", f"{endpoint.ros_name} refused the goal")

        record.handle = handle
        record.status = "accepted"
        record.result_task = asyncio.create_task(
            _await_result(goals, record, runtime, result_to_dict),
            name=f"goal-result-{record.goal_id}",
        )
        return _json({"goal_id": record.goal_id}, 202)

    return submit


async def _await_result(
    goals: GoalRegistry,
    record: GoalRecord,
    runtime: RosRuntime,
    result_to_dict: Callable[[Any], dict[str, Any]],
) -> None:
    """Wait for a goal to finish, then record it and close its streams.

    The task is held on the record so it is not garbage collected mid-flight.
    """
    try:
        outcome = await runtime.goal_result(record.handle)
    except Exception:
        _LOG.exception("waiting for the result of %s failed", record.goal_id)
        goals.on_terminal(record, "aborted", None)
        return
    encoded = orjson.dumps(result_to_dict(outcome.result), option=_ORJSON_OPTS)
    goals.on_terminal(record, status_name(outcome.status), encoded)


def _goal_status(endpoint: ResolvedEndpoint, goals: GoalRegistry) -> Handler:
    """GET the status, result and newest feedback of a goal."""
    path = endpoint.config.path

    async def status(request: Request) -> Response:
        record = goals.get(path, request.path_params["goal_id"])
        if record is None:
            return _error(404, "UNKNOWN_GOAL", "no such goal, or its result has been evicted")
        return Response(record.status_body(), 200, media_type=_JSON)

    return status


def _goal_stream(endpoint: ResolvedEndpoint, goals: GoalRegistry) -> Handler:
    """Stream a goal's feedback, then one terminal event, then close."""
    path = endpoint.config.path

    async def stream(request: Request) -> Response:
        record = goals.get(path, request.path_params["goal_id"])
        if record is None:
            return _error(404, "UNKNOWN_GOAL", "no such goal, or its result has been evicted")

        if record.is_terminal:
            return StreamingResponse(
                _single_frame(terminal_frame(record.status, record.result)),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        queue = goals.subscribe(record)

        async def frames() -> AsyncIterator[bytes]:
            try:
                while True:
                    frame = await queue.get()
                    if frame is None:  # the sentinel written by on_terminal
                        return
                    yield frame
            finally:
                goals.unsubscribe(record, queue)

        return StreamingResponse(frames(), media_type="text/event-stream", headers=_SSE_HEADERS)

    return stream


async def _single_frame(frame: bytes) -> AsyncIterator[bytes]:
    yield frame


# --------------------------------------------------------------------------------------
# the global event stream
# --------------------------------------------------------------------------------------


def _events(broadcaster: SseBroadcaster, resolved: ResolvedConfig) -> Handler:
    """GET the global stream: a snapshot of every readable endpoint, then deltas.

    Filtered per client by role. The stream is one connection carrying several endpoints, so
    without that a viewer could read an operator-only endpoint here that they cannot GET.
    """
    readable = {key: resolved.endpoints[key].config.roles for key in broadcaster.include}

    async def events(request: Request) -> Response:
        identity = request.state.identity
        allowed = frozenset(key for key, roles in readable.items() if identity.may(roles))
        client = broadcaster.subscribe(allowed)
        if client is None:
            return _error(
                503,
                "TOO_MANY_CLIENTS",
                "the event stream is at its configured client limit",
            )
        return StreamingResponse(
            _frames(broadcaster, client), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    return events


async def _frames(broadcaster: SseBroadcaster, client: SseClient) -> AsyncIterator[bytes]:
    """Snapshot, then queued events, with a comment frame whenever the stream goes quiet."""
    try:
        for frame in broadcaster.snapshot(client):
            yield frame
        while True:
            try:
                item = await asyncio.wait_for(client.queue.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield HEARTBEAT_FRAME
                continue
            if item is None:  # the sentinel written by close() or an overflow disconnect
                return
            yield item
    finally:
        broadcaster.unsubscribe(client)


def _goal_cancel(endpoint: ResolvedEndpoint, runtime: RosRuntime, goals: GoalRegistry) -> Handler:
    """DELETE a goal: ask the server to cancel it. Never gated by preconditions."""
    path = endpoint.config.path

    async def cancel(request: Request) -> Response:
        record = goals.get(path, request.path_params["goal_id"])
        if record is None:
            return _error(404, "UNKNOWN_GOAL", "no such goal, or its result has been evicted")
        if record.is_terminal:
            return _envelope(200, "ALREADY_TERMINAL", f"this goal has already {record.status}")

        response = await runtime.cancel_goal(record.handle)
        accepted = bool(response.goals_canceling)
        if accepted:
            record.status = "canceling"
            return _json({"success": True, "message": "cancellation requested", "error_code": None})
        return _envelope(
            200,
            "CANCEL_REJECTED",
            f"{endpoint.ros_name} would not cancel this goal (return code {response.return_code})",
        )

    return cancel


# --------------------------------------------------------------------------------------
# the generated description of everything above
# --------------------------------------------------------------------------------------


def _openapi(document: dict[str, Any], base: str) -> Handler:
    """Serve the document generated at startup, with ``servers`` correct for this request.

    Authenticated like every other route. Unauthenticated it would hand an anonymous caller the
    whole surface -- paths, ROS names, types and required roles -- which is the one thing a
    deny-by-default gateway should not volunteer. Any authenticated caller sees all of it,
    including endpoints their roles cannot use; that is deliberate, since 403-versus-404 already
    lets them enumerate the same list by asking.
    """
    encoded: dict[str, bytes] = {}

    async def openapi(request: Request) -> Response:
        prefix = _external_prefix(request)
        body = encoded.get(prefix)
        if body is None:
            served = dict(document)
            served["servers"] = [{"url": f"{prefix}{base}" or "/"}]
            body = encoded[prefix] = orjson.dumps(served, option=_ORJSON_OPTS)
        return Response(body, 200, media_type=_JSON)

    return openapi


def _external_prefix(request: Request) -> str:
    """The path prefix the client reached this gateway through, if it is behind one.

    ``X-Forwarded-Prefix`` first, because a proxy asserting it knows better than the ASGI
    ``root_path``, which is only set when the server was told about the mount at startup.
    """
    forwarded = request.headers.get("x-forwarded-prefix")
    prefix = forwarded if forwarded is not None else request.scope.get("root_path", "")
    return prefix.rstrip("/")


def _docs_routes(base: str) -> Iterator[Route]:
    """Swagger UI, served asset by named asset.

    Unauthenticated, unlike the document it renders: the page itself describes nothing, and a
    browser cannot send an Authorization header for a top-level navigation anyway. It asks for a
    token and fetches the document with it.
    """
    yield Route(f"{base}/docs", _docs_page(base), methods=["GET"])
    for route, (name, media_type) in _DOCS_ASSETS.items():
        yield Route(f"{base}/{route}", _asset(_DOCS_DIR / name, media_type), methods=["GET"])


def _docs_page(base: str) -> Handler:
    """The UI shell, with its asset and document URLs made absolute for this request.

    They cannot be relative. A browser resolves a relative reference from a page at
    ``<base>/docs`` against ``<base>/``, so ``swagger-ui.css`` would be fetched as a *sibling* of
    ``docs`` rather than a child, and the styling and the whole bundle would 404 -- differently
    again depending on whether the URL happened to end in a slash. Substituting the prefix the
    request actually arrived through gets it right at ``/api/v1/docs`` and at
    ``/cell3/api/v1/docs`` alike.
    """
    template = (_DOCS_DIR / "index.html").read_text(encoding="utf-8")
    rendered: dict[str, bytes] = {}

    async def page(request: Request) -> Response:
        prefix = _external_prefix(request)
        body = rendered.get(prefix)
        if body is None:
            body = rendered[prefix] = (
                template.replace("__ASSETS__", f"{prefix}{base}/docs")
                .replace("__SPEC__", f"{prefix}{base}/openapi.json")
                .encode("utf-8")
            )
        return Response(body, 200, media_type="text/html; charset=utf-8")

    return page


def _asset(path: Path, media_type: str) -> Handler:
    body = path.read_bytes()

    async def asset(_: Request) -> Response:
        return Response(body, 200, media_type=media_type)

    return asset


# --------------------------------------------------------------------------------------
# the test console
# --------------------------------------------------------------------------------------


def _console_routes(
    resolved: ResolvedConfig,
    base: str,
    everyone: frozenset[str],
    audit: AuditLogger,
    authenticator: Authenticator,
    policy: PolicyEngine,
) -> Iterator[Route]:
    """The console's server side, registered only when ``http.console`` is true.

    Off by default, and off means absent: a disabled console is not a route that answers 403, it
    is a route that does not exist.
    """

    yield Route(
        f"{base}/console/manifest",
        _guard(_manifest(resolved), everyone, "/console/manifest", audit, authenticator, policy),
        methods=["GET"],
    )
    yield Route(f"{base}/console", _console_page(base), methods=["GET"])
    for route, (name, media_type) in _CONSOLE_ASSETS.items():
        yield Route(f"{base}/{route}", _asset(_CONSOLE_DIR / name, media_type), methods=["GET"])


def _whoami() -> Handler:
    """Who this token is, for any token the gateway accepts.

    No role requirement, which is what makes it useful: a caller can tell a wrong token from a
    token with the wrong roles without probing an endpoint. That distinction is only safe because
    an empty role set is unreachable from a config -- `roles:` is required and rejects an empty
    list at both the schema and the model layer -- so this is the one route that can hold it.
    """

    async def whoami(request: Request) -> Response:
        identity: Identity = request.state.identity
        return _json({"user": identity.user, "roles": sorted(identity.roles)})

    return whoami


def _manifest(resolved: ResolvedConfig) -> Handler:
    """Everything this caller may touch, filtered by role.

    Built per request rather than at startup: it depends on the caller's roles and on the prefix
    the request arrived through, and it is a page-load path, not a hot one.
    """

    async def manifest(request: Request) -> Response:
        identity: Identity = request.state.identity
        return _json(build_manifest(resolved, identity, prefix=_external_prefix(request)))

    return manifest


def _console_page(base: str) -> Handler:
    """The console shell, with its asset and API URLs made absolute for this request.

    Unauthenticated, exactly like the Swagger UI shell and for the same reason: a browser cannot
    attach an Authorization header to a top-level navigation, so a guarded page could never be
    opened. The shell holds no information about the API -- it asks for a token and then fetches
    `/whoami` and the manifest, both of which are guarded and audited.
    """
    template = (_CONSOLE_DIR / "index.html").read_text(encoding="utf-8")
    rendered: dict[str, bytes] = {}

    async def page(request: Request) -> Response:
        prefix = _external_prefix(request)
        body = rendered.get(prefix)
        if body is None:
            body = rendered[prefix] = (
                template.replace("__ASSETS__", f"{prefix}{base}/console")
                .replace("__API__", f"{prefix}{base}")
                .encode("utf-8")
            )
        return Response(body, 200, media_type="text/html; charset=utf-8")

    return page
