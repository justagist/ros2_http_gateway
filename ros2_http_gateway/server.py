"""Serving: bring up ROS, run uvicorn, tear both down in the right order.

The ROS side is torn down *in the lifespan*, after the last request is answered and the audit
queue flushed. Cleaning up after ``server.run()`` returns instead silently does not work:
uvicorn restores the default signal handler and re-raises the captured signal as ``run()``
unwinds, so the process dies inside that call. The ``finally`` below is an idempotent safety
net for the paths that do return normally.

Event streams are ended earlier still, in :class:`_Server`, for the reason given there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator
from typing import Final

import uvicorn
from starlette.applications import Starlette

from .app import build_app
from .audit import AuditLogger
from .auth import StaticTokenAuthenticator
from .resolve import ResolvedConfig
from .runtime import RosRuntime
from .sse import SseBroadcaster

_LOG = logging.getLogger(__name__)

__all__ = ["serve"]

# Declared dependencies, named rather than "auto" so a missing one is loud instead of a silent
# fallback to the slower pure-Python implementations.
_LOOP: Final = "uvloop"
_HTTP: Final = "httptools"

# 128 + SIGINT, the shell convention for "terminated by Ctrl-C".
_EXIT_INTERRUPTED: Final = 130


class _Server(uvicorn.Server):
    """uvicorn, plus ending the event streams as shutdown begins.

    An SSE response is an in-flight request that never completes on its own, and uvicorn's
    graceful shutdown waits for in-flight requests *before* it runs the lifespan shutdown. So a
    single connected stream would hold the process open until the client hung up: `SIGTERM`
    would appear to be ignored. Closing them here, ahead of that wait, is the only point early
    enough -- the lifespan is already too late.
    """

    def __init__(self, config: uvicorn.Config, broadcaster: SseBroadcaster | None) -> None:
        super().__init__(config)
        self._broadcaster = broadcaster

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self._broadcaster is not None:
            self._broadcaster.close()
        await super().shutdown(sockets)


def serve(resolved: ResolvedConfig) -> int:
    """Run the gateway until interrupted, returning the process exit code."""
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
        server = _Server(
            uvicorn.Config(
                app,
                host=http.bind,
                port=http.port,
                loop=_LOOP,
                http=_HTTP,
                lifespan="on",
                # The audit log is the request record; a second, less informative one is not
                # worth the write.
                access_log=False,
                log_config=None,
            ),
            app.state.sse,
        )
        try:
            server.run()
        except KeyboardInterrupt:
            # uvicorn re-raises, so without this Ctrl-C prints a traceback out of
            # asyncio.runners for an ordinary way to stop a server.
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
            # In a thread: stopping the runtime joins the ROS threads.
            await asyncio.get_running_loop().run_in_executor(None, runtime.stop)

    return build_app(
        resolved,
        runtime,
        audit,
        StaticTokenAuthenticator(resolved.config.tokens),
        lifespan=lifespan,
    )
