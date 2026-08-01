"""Goal registry for action endpoints.

An action outlives its request, so a submitted goal gets a record; later requests find it by id,
and it is evicted once terminal for ``actions.result_ttl_s``.

Loop-confined: feedback and results arrive on ROS threads and are handed over with
``loop.call_soon_threadsafe``, so the registry needs no locks.

Eviction is lazy -- a sweep on creation, the only way the registry grows, plus a drop on lookup
-- so there is no timer and an idle gateway does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import orjson

_LOG = logging.getLogger(__name__)

__all__ = [
    "TERMINAL",
    "GoalRecord",
    "GoalRegistry",
    "feedback_frame",
    "status_name",
    "terminal_frame",
]

# action_msgs/msg/GoalStatus, transcribed rather than imported so this file needs no ROS.
_STATUS_NAMES: Final[dict[int, str]] = {
    0: "unknown",
    1: "accepted",
    2: "executing",
    3: "canceling",
    4: "succeeded",
    5: "canceled",
    6: "aborted",
}

TERMINAL: Final = frozenset({"succeeded", "canceled", "aborted"})

# Per-goal stream backlog: a slow client loses its oldest feedback rather than stalling the goal.
_STREAM_BACKLOG: Final = 64

_NULL: Final = b"null"


def status_name(code: int) -> str:
    """Map an ``action_msgs`` status code onto its protocol name."""
    return _STATUS_NAMES.get(code, "unknown")


def feedback_frame(encoded: bytes) -> bytes:
    """Frame one feedback message as an SSE event."""
    return b"event: feedback\ndata: " + encoded + b"\n\n"


def terminal_frame(status: str, result: bytes | None) -> bytes:
    """Frame the single terminal event that ends a per-goal stream."""
    return (
        b'event: terminal\ndata: {"status":"'
        + status.encode("ascii")
        + b'","result":'
        + (result or _NULL)
        + b"}\n\n"
    )


@dataclass(slots=True)
class GoalRecord:
    """One submitted goal and everything a later request might ask about it."""

    goal_id: str
    endpoint: str
    status: str = "accepted"
    result: bytes | None = None
    last_feedback: bytes | None = None
    terminal_at: float | None = None
    handle: Any = None
    result_task: asyncio.Task[None] | None = None
    subscribers: list[asyncio.Queue[bytes | None]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        """Whether this goal has finished, one way or another."""
        return self.status in TERMINAL

    def current_status(self) -> str:
        """Live status from the goal handle while running, recorded status once finished.

        The handle supplies only the non-terminal progression. It goes terminal before the result
        task has collected the outcome, and a terminal status must imply the result is present,
        so terminal comes from the record -- which is set together with the result. ``canceling``
        is likewise kept, since it is what this gateway already told the client.
        """
        if self.is_terminal or self.status == "canceling" or self.handle is None:
            return self.status
        live = status_name(self.handle.status)
        if live == "unknown" or live in TERMINAL:
            return self.status
        return live

    def status_body(self) -> bytes:
        """The JSON body for a status read, built without re-encoding the payloads."""
        return b"".join(
            (
                b'{"status":"',
                self.current_status().encode("ascii"),
                b'","result":',
                self.result or _NULL,
                b',"last_feedback":',
                self.last_feedback or _NULL,
                b"}",
            )
        )


class GoalRegistry:
    """Tracks live and recently finished goals.

    ``clock`` is injectable so eviction can be tested without sleeping through the TTL.
    """

    __slots__ = ("_clock", "_records", "_ttl_s")

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._records: dict[str, GoalRecord] = {}

    def __len__(self) -> int:
        return len(self._records)

    def create(self, endpoint: str) -> GoalRecord:
        """Register a new goal, sweeping expired ones first."""
        self.sweep()
        record = GoalRecord(goal_id=uuid.uuid4().hex, endpoint=endpoint)
        self._records[record.goal_id] = record
        return record

    def get(self, endpoint: str, goal_id: str) -> GoalRecord | None:
        """Find a goal belonging to an endpoint, or None if unknown or expired.

        Keyed by endpoint too, so an id issued by one cannot be read through another's routes.
        """
        record = self._records.get(goal_id)
        if record is None or record.endpoint != endpoint:
            return None
        if self._expired(record):
            self.discard(record)
            return None
        return record

    def discard(self, record: GoalRecord) -> None:
        """Drop a record and close any streams still attached to it."""
        self._records.pop(record.goal_id, None)
        self._close_subscribers(record)

    def sweep(self) -> int:
        """Evict every goal whose result has outlived the TTL. Returns how many went."""
        expired = [record for record in self._records.values() if self._expired(record)]
        for record in expired:
            self.discard(record)
        return len(expired)

    def _expired(self, record: GoalRecord) -> bool:
        return record.terminal_at is not None and (self._clock() - record.terminal_at) > self._ttl_s

    # ------------------------------------------------------------------ loop-side updates

    def on_feedback(self, record: GoalRecord, encoded: bytes) -> None:
        """Record feedback and fan the framed bytes out to every stream.

        Encoded once by the caller and shared by reference: N listeners cost N queue appends.
        """
        if record.is_terminal:
            # call_soon_threadsafe ordering allows a callback scheduled from the ROS thread to
            # land after on_terminal has run. Readers have already stopped at the sentinel, but
            # letting last_feedback change afterwards would make two reads of a finished goal
            # disagree.
            return
        record.last_feedback = encoded
        if not record.subscribers:
            return
        frame = feedback_frame(encoded)
        for queue in record.subscribers:
            _offer(queue, frame)

    def on_terminal(self, record: GoalRecord, status: str, result: bytes | None) -> None:
        """Mark a goal finished, then send the closing event to every stream."""
        # Status last: it is the flag readers key on, and "terminal" has to imply the result is
        # already there. Nothing can interleave between these lines today, since this runs on the
        # loop with no await, but ordering them this way means that stays true even if some
        # future caller yields.
        record.result = result
        record.terminal_at = self._clock()
        record.status = status
        frame = terminal_frame(status, result)
        for queue in record.subscribers:
            # Both through _offer: a queue sitting at maxsize accepts the frame by dropping its
            # oldest and is then full again, so a bare put_nowait for the sentinel raises
            # QueueFull -- which would escape into the result task, abandon every subscriber
            # after this one without a frame or a sentinel, and leave their streams open until
            # the connection died. The sentinel is the one frame that must never be the dropped
            # one, so it gets the same treatment with more reason, not less.
            try:
                _offer(queue, frame)
                _offer(queue, None)
            except Exception:
                _LOG.exception("failed to close a stream for goal %s", record.goal_id)

    def subscribe(self, record: GoalRecord) -> asyncio.Queue[bytes | None]:
        """Attach a stream to a goal."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_STREAM_BACKLOG)
        record.subscribers.append(queue)
        return queue

    def unsubscribe(self, record: GoalRecord, queue: asyncio.Queue[bytes | None]) -> None:
        """Detach a stream, whether it ended or the client went away."""
        try:
            record.subscribers.remove(queue)
        except ValueError:
            pass

    def _close_subscribers(self, record: GoalRecord) -> None:
        for queue in record.subscribers:
            _offer(queue, None)
        record.subscribers.clear()


def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
    """Append to a stream queue, discarding the oldest frame if it is full."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - it was full a moment ago
            pass
        queue.put_nowait(item)


def encode_feedback(converter: Callable[[Any], dict[str, Any]], message: Any) -> bytes:
    """Encode an action feedback message.

    The callback receives the generated ``*_FeedbackMessage`` wrapper, not the Feedback type, so
    the converter applies to ``.feedback``.
    """
    return orjson.dumps(converter(message.feedback), option=orjson.OPT_SERIALIZE_NUMPY)
