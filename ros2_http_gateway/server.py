"""Serving: bring up ROS, run uvicorn, tear both down in the right order.

Shutdown order is the part worth stating, and it is not where you would first put it.
uvicorn owns SIGINT and SIGTERM; on either it stops accepting, lets in-flight requests
finish, then runs the lifespan shutdown. So the ROS side is torn down *in the lifespan*,
after the last request has been answered and the audit queue flushed.

The obvious arrangement -- clean up after ``server.run()`` returns -- silently does not work.
uvicorn restores the default signal handler and re-raises the captured signal as ``run()``
unwinds, so the process dies inside that call and anything after it never executes. Measured,
not assumed: the node was left undestroyed and no shutdown line was logged. The ``finally``
below is kept only as an idempotent safety net for the paths that do return normally.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Final

import uvicorn
from starlette.applications import Starlette

from .app import build_app
from .audit import AuditLogger
from .auth import StaticTokenAuthenticator
from .resolve import ResolvedConfig
from .runtime import RosRuntime

_LOG = logging.getLogger(__name__)

__all__ = ["serve"]

# Declared dependencies, so asking for them by name rather than "auto" makes a missing one
# a loud failure instead of a silent fallback to the slower pure-Python implementations.
_LOOP: Final = "uvloop"
_HTTP: Final = "httptools"

# 128 + SIGINT, the shell convention for "terminated by Ctrl-C".
_EXIT_INTERRUPTED: Final = 130


def serve(resolved: ResolvedConfig) -> int:
    """Run the gateway until interrupted.

    Args:
        resolved: A validated, type-resolved configuration.

    Returns:
        Process exit code.
    """
    http = resolved.config.http
    runtime = RosRuntime(resolved)
    runtime.start()
    try:
        audit = AuditLogger(resolved.config.audit)
        app = _build(resolved, runtime, audit)
        _LOG.info(
            "serving %d endpoint(s) on http://%s:%d%s",
            len(resolved.endpoints),
            http.bind,
            http.port,
            http.base_path or "/",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=http.bind,
                port=http.port,
                loop=_LOOP,
                http=_HTTP,
                lifespan="on",
                # The audit log is the request record; a second, less informative one that
                # cannot be pointed at a file is not worth the write.
                access_log=False,
                log_config=None,
            )
        )
        try:
            server.run()
        except KeyboardInterrupt:
            # uvicorn restores the default SIGINT handler and re-raises, so without this an
            # operator pressing Ctrl-C gets a traceback out of asyncio.runners for what is a
            # completely ordinary way to stop a server.
            _LOG.info("interrupted")
            return _EXIT_INTERRUPTED
    finally:
        # Normally already stopped by the lifespan; idempotent, and the only cleanup that
        # runs if bringing uvicorn up failed outright.
        runtime.stop()
    return 0


def _build(resolved: ResolvedConfig, runtime: RosRuntime, audit: AuditLogger) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await audit.start()
        try:
            yield
        finally:
            await audit.stop()
            # In a thread: stopping the runtime joins the ROS threads, and blocking the
            # event loop for that is unnecessary even during shutdown.
            await asyncio.get_running_loop().run_in_executor(None, runtime.stop)

    return build_app(
        resolved,
        runtime,
        audit,
        StaticTokenAuthenticator(resolved.config.tokens),
        lifespan=lifespan,
    )
