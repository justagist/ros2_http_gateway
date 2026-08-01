"""The HTTP surface: routes built from the config, nothing discovered at runtime.

Auth, policy and audit are applied by wrapping each handler in a closure that already holds
its endpoint's configuration, rather than by middleware. Middleware would have to work out
which endpoint a request belongs to in order to know the required roles, and would then have
to agree with the router about it; a closure per route simply knows.

Status codes follow the taxonomy in docs/protocol.md: 4xx means the request never crossed
into ROS, 200 means it did and the body carries the outcome. Both use one envelope shape.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
from .params import apply_bindings
from .policy import PolicyEngine, build_policy
from .resolve import ResolvedConfig, ResolvedEndpoint
from .runtime import (
    ActionUnavailableError,
    RosRuntime,
    ServiceTimeoutError,
    ServiceUnavailableError,
)

_LOG = logging.getLogger(__name__)

__all__ = ["build_app"]

_JSON: Final = "application/json"
_ORJSON_OPTS: Final = orjson.OPT_SERIALIZE_NUMPY

_SSE_HEADERS: Final = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Squarely aimed at nginx, which buffers a streaming response by default and would hold
    # feedback until the goal finished.
    "X-Accel-Buffering": "no",
}

Handler = Callable[[Request], Awaitable[Response]]


def _json(payload: Any, status: int = 200) -> Response:
    return Response(orjson.dumps(payload, option=_ORJSON_OPTS), status, media_type=_JSON)


class _ErrorResponse(Response):
    """A JSON error response that remembers its code.

    The audit wrapper needs the ``error_code`` it gave the client. Carrying it on the
    response beats parsing the body back out, which would cost a JSON decode per failed
    request and would quietly break the day a handler returns a body shaped differently.
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

    Args:
        resolved: The validated, type-resolved config.
        runtime: The started ROS runtime holding the caches and publishers.
        audit: Audit logger; already started, or started by ``lifespan``.
        authenticator: Turns bearer tokens into identities.
        lifespan: Optional Starlette lifespan context manager. The serving path uses it to
            start and stop the audit writer; tests manage the logger themselves.

    Returns:
        The application, with every configured route registered.
    """
    policy = build_policy(
        {path: e.config.preconditions for path, e in resolved.endpoints.items()},
        runtime.caches,
    )
    goals = GoalRegistry(resolved.config.actions.result_ttl_s)
    base = resolved.config.http.base_path
    routes = [Route(f"{base}/health", _health(resolved, runtime), methods=["GET"])]
    routes.extend(_endpoint_routes(resolved, runtime, audit, authenticator, policy, goals, base))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.goals = goals
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

    Literal segments are registered ahead of templated ones so that, where the config
    engine has allowed both to coexist on different methods, matching does not depend on
    dictionary order.
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
        guarded = _guard(handler, endpoint, audit, authenticator, policy)
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

    Only the submit route is subject to preconditions. Applying them to cancel would let a
    state gate stop an operator aborting a goal, which is precisely backwards; status and
    stream change nothing.
    """
    path = endpoint.config.path
    empty = PolicyEngine({})

    def guard(handler: Handler, with_policy: PolicyEngine) -> Handler:
        return _guard(handler, endpoint, audit, authenticator, with_policy)

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
    endpoint: ResolvedEndpoint,
    audit: AuditLogger,
    authenticator: Authenticator,
    policy: PolicyEngine,
) -> Handler:
    """Wrap a handler with authentication, the role check, policy, timing and audit.

    Preconditions are evaluated here rather than inside the handler so that every refusal --
    unauthenticated, wrong role, or forbidden by the robot's state -- is recorded the same
    way: ``decision: deny`` with the reason. A precondition refusal is still a 200, because
    the request was well formed and authorised; it simply must not proceed.

    One audit record per request that reaches a configured route: allowed, denied, or failed
    inside the handler. A request that matches no route is Starlette's 404 and is not audited,
    which is a deliberate gap recorded in docs/security.md -- path-scanning probes therefore
    leave no trace in the audit file.
    """
    required = endpoint.config.roles

    async def guarded(request: Request) -> Response:
        started = time.perf_counter()
        identity: Identity | None = None
        denial: str | None = None
        response: Response

        token = bearer_token(request.headers.get("authorization"))
        if token is None:
            denial, response = NO_CREDENTIAL, _error(401, "UNAUTHENTICATED", "missing bearer token")
        else:
            identity = authenticator.authenticate(token)
            if identity is None:
                denial, response = (
                    BAD_CREDENTIAL,
                    _error(401, "UNAUTHENTICATED", "unknown bearer token"),
                )
            elif not identity.may(required):
                denial, response = (
                    MISSING_ROLE,
                    _error(
                        403,
                        "FORBIDDEN",
                        f"this endpoint requires one of: {', '.join(sorted(required))}",
                    ),
                )
            elif (
                refusal := policy.evaluate(endpoint.config.path, request.path_params)
            ) is not None:
                denial, response = (
                    refusal,
                    _envelope(200, "PRECONDITION_FAILED", refusal),
                )
            else:
                try:
                    response = await handler(request)
                except Exception:
                    # The 500 path is the one an incident review most wants to see, and
                    # letting the exception reach Starlette's error machinery would leave no
                    # record at all. Logged with its traceback, audited, and answered with the
                    # same envelope as everything else rather than an HTML error page.
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

    ``success`` describes the call, not the robot's opinion of it: a service that answers
    ``success: false`` in its own response still produces a 200 with ``"success": true`` here
    and its own fields under ``response``. Conflating the two would make it impossible for a
    client to tell "the gateway could not ask" from "the robot said no".
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

    The whole body comes from the cache, which memoises the encoded payload by sequence
    number, so repeated reads between two messages re-encode nothing.
    """
    path = endpoint.config.path

    async def read(_: Request) -> Response:
        return Response(runtime.caches[path].response_body(), 200, media_type=_JSON)

    return read


def _topic_write(endpoint: ResolvedEndpoint, runtime: RosRuntime) -> Handler:
    """POST a body to be published.

    Returns as soon as the message is queued for the ROS side. Waiting for subscribers is
    not something a publisher can do, so pretending otherwise would be a lie.
    """
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

    202 rather than 200 because the work has been accepted and not performed; the id is the
    client's handle on it. A server that refuses the goal is a 200 with ``GOAL_REJECTED``: the
    request was fine and reached ROS, which answered no.
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
            # A ROS callback thread. Encoding happens here, once per message however many
            # listeners there are; only the handover is scheduled onto the loop.
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

    Held on the record so the task is not garbage collected mid-flight, which is the classic
    way a fire-and-forget asyncio task disappears.
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
            # Nothing more will ever arrive, so answer with the ending and be done.
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


def _goal_cancel(endpoint: ResolvedEndpoint, runtime: RosRuntime, goals: GoalRegistry) -> Handler:
    """DELETE a goal: ask the server to cancel it.

    Deliberately not gated by preconditions. A state gate that could stop an operator aborting
    a goal would be the opposite of a safety feature.
    """
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
