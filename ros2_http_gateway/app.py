"""The HTTP surface: routes built from the config, nothing discovered at runtime.

Auth, policy and audit are applied by wrapping each handler in a closure that already holds
its endpoint's configuration, rather than by middleware. Middleware would have to work out
which endpoint a request belongs to in order to know the required roles, and would then have
to agree with the router about it; a closure per route simply knows.

Status codes follow the taxonomy in docs/protocol.md: 4xx means the request never crossed
into ROS, 200 means it did and the body carries the outcome. Both use one envelope shape.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Final

import orjson
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

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
from .resolve import ResolvedConfig, ResolvedEndpoint
from .runtime import RosRuntime

_LOG = logging.getLogger(__name__)

__all__ = ["build_app"]

_JSON: Final = "application/json"
_ORJSON_OPTS: Final = orjson.OPT_SERIALIZE_NUMPY

# Phase 2 serves topics. Service and action endpoints are configured, validated and routed,
# but answering them lands in the next phases; a 501 saying so beats a 404 that looks like a
# config mistake.
_NOT_IMPLEMENTED: Final = {
    "service": "service endpoints are answered from phase 3",
    "action": "action endpoints are answered from phase 4",
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
    base = resolved.config.http.base_path
    routes = [Route(f"{base}/health", _health(resolved, runtime), methods=["GET"])]
    routes.extend(_endpoint_routes(resolved, runtime, audit, authenticator, base))
    return Starlette(routes=routes, lifespan=lifespan)


def _endpoint_routes(
    resolved: ResolvedConfig,
    runtime: RosRuntime,
    audit: AuditLogger,
    authenticator: Authenticator,
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
        handler = _endpoint_handler(endpoint, runtime)
        guarded = _guard(handler, endpoint, audit, authenticator)
        yield Route(f"{base}{path}", guarded, methods=[endpoint.config.method])


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
) -> Handler:
    """Wrap a handler with authentication, the role check, timing and audit.

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
    return _not_implemented(endpoint)


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


def _not_implemented(endpoint: ResolvedEndpoint) -> Handler:
    """Answer a configured-but-not-yet-served endpoint honestly."""
    message = _NOT_IMPLEMENTED[endpoint.config.kind]

    async def unimplemented(_: Request) -> Response:
        return _error(501, "NOT_IMPLEMENTED", message)

    return unimplemented
