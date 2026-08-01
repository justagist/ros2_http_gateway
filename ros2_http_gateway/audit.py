"""Audit logging: one JSON line per request that reached a configured route.

Recording is non-blocking: :meth:`AuditLogger.record` appends to an in-memory queue and a
background task drains it, batching whatever accumulated into one write.

There is no field for a token or a request body, which is the point: they cannot be logged by
accident.

The queue is bounded, and an overflow is never silent: drops are counted, warned about, and the
count rides on the next record written, so the trail itself shows where the gap is.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import IO, Any, Final

import orjson

from .config import AuditConfig

_LOG = logging.getLogger(__name__)

__all__ = ["AuditLogger", "Decision"]

DEFAULT_QUEUE_SIZE: Final = 10_000


class Decision:
    """The two values the ``decision`` field can take."""

    ALLOW: Final = "allow"
    DENY: Final = "deny"


class AuditLogger:
    """Writes audit records to stderr or a file, off the request path."""

    __slots__ = ("_config", "_dropped", "_queue", "_stream", "_task")

    def __init__(self, config: AuditConfig, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._config = config
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stream: IO[bytes] | None = None
        self._dropped = 0

    async def start(self) -> None:
        """Open the sink and start the writer task."""
        if self._config.sink == "file":
            path = self._config.path
            assert path is not None, "the config schema requires a path when sink is file"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("ab")
        else:
            self._stream = sys.stderr.buffer
        self._task = asyncio.create_task(self._drain(), name="audit-writer")

    async def stop(self) -> None:
        """Flush everything queued, then stop the writer and close the sink."""
        if self._task is not None:
            await self._queue.join()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._stream is not None:
            self._stream.flush()
            if self._config.sink == "file":
                self._stream.close()
            self._stream = None

    def record(
        self,
        *,
        method: str,
        path: str,
        decision: str,
        status: int,
        latency_ms: float,
        user: str | None = None,
        roles: frozenset[str] | None = None,
        reason: str | None = None,
        outcome: str = "ok",
    ) -> None:
        """Queue one audit record. Never blocks, never raises."""
        entry: dict[str, Any] = {
            # timezone.utc, not datetime.UTC: the latter is 3.11+ and Humble ships 3.10.
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "user": user,
            "roles": sorted(roles) if roles else [],
            "method": method,
            "path": path,
            "decision": decision,
            "reason": reason,
            "status": status,
            "outcome": outcome,
            "latency_ms": round(latency_ms, 3),
        }
        # Cleared only once a record carrying it is queued: clearing on attach would lose the
        # tally whenever that record is itself the one dropped.
        if self._dropped:
            entry["dropped"] = self._dropped
        try:
            self._queue.put_nowait(orjson.dumps(entry))
        except asyncio.QueueFull:
            self._dropped += 1
            _LOG.warning(
                "audit queue full, dropped a record (%d dropped since the last written one)",
                self._dropped,
            )
        else:
            self._dropped = 0

    async def _drain(self) -> None:
        """Write queued records, batching whatever is already waiting."""
        while True:
            first = await self._queue.get()
            batch = [first]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                self._write(batch)
            except OSError as exc:  # pragma: no cover - a broken sink must not kill the app
                _LOG.error("audit sink write failed: %s", exc)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write(self, batch: Sequence[bytes]) -> None:
        stream = self._stream
        if stream is None:  # pragma: no cover - stop() raced with a queued record
            return
        stream.write(b"\n".join(batch) + b"\n")
        stream.flush()
