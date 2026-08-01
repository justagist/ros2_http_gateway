"""Goal registry for action endpoints.

An action is the one thing here that outlives its request, so it needs somewhere to live. A
submitted goal gets a record; later requests find it by id, and it is evicted once it has been
terminal for ``actions.result_ttl_s``.

Everything in this module is confined to the event loop. Feedback and results arrive on ROS
threads and are handed over with ``loop.call_soon_threadsafe``, so the registry itself needs no
locks -- the same arrangement the topic caches use in the other direction.

Eviction is lazy rather than timed: a sweep runs whenever a goal is created, which is the only
way the registry grows, and a lookup drops an expired record before answering. So no background
task, and an idle gateway does no work at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Iterator
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

# action_msgs/msg/GoalStatus, mapped onto the names the protocol uses. Read once here rather
# than imported at module scope so that this file stays importable without ROS.
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

# Per-goal stream backlog. A client that cannot keep up loses the oldest feedback rather than
# stalling the goal; order is preserved, gaps are visible in the sequence the server sends.
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
        """The status to report: live from the goal handle while the goal is still running.

        The action client already tracks status through its own status subscription, so
        ``executing`` need not be inferred -- reading it beats leaving a running goal reported
        as ``accepted`` until it finishes, which the protocol does not promise.

        The handle is only consulted for the non-terminal progression. It reaches a terminal
        code before the result task has collected the outcome, so trusting it there would answer
        ``succeeded`` with ``"result": null`` for a moment -- and a terminal status has to mean
        the result is there. Terminal therefore comes from the record alone, which is set at the
        same instant as the result.

        Two further cases keep the recorded value. Once terminal it is authoritative. And while
        a cancellation is in flight, ``canceling`` is what this gateway has already told the
        client, so reverting to ``executing`` because the server has not acted yet would
        contradict its own answer.
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

    Args:
        ttl_s: How long a terminal goal stays queryable.
        clock: Monotonic time source. Injectable so that eviction can be tested without
            sleeping through a five-minute TTL.
    """

    __slots__ = ("_clock", "_records", "_ttl_s")

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._records: dict[str, GoalRecord] = {}

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[GoalRecord]:
        return iter(list(self._records.values()))

    def create(self, endpoint: str) -> GoalRecord:
        """Register a new goal and return its record.

        Sweeps first: creation is the only way the registry grows, so this is where the
        expired records can be reclaimed without a timer.
        """
        self.sweep()
        record = GoalRecord(goal_id=uuid.uuid4().hex, endpoint=endpoint)
        self._records[record.goal_id] = record
        return record

    def get(self, endpoint: str, goal_id: str) -> GoalRecord | None:
        """Find a goal belonging to an endpoint, or None if unknown or expired.

        The endpoint is part of the lookup so that a goal id from one action cannot be read
        through another's routes.
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

        Encoded once by the caller and shared by reference, so N listeners cost N queue
        appends rather than N conversions.
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

    The callback receives the generated ``*_FeedbackMessage`` wrapper, which carries the goal
    id alongside the feedback itself, so the converter has to be applied to ``.feedback`` and
    not to the message. Measured, not assumed: the callback hands over
    ``Fibonacci_FeedbackMessage``, never ``Fibonacci.Feedback``.

    Args:
        converter: The compiled converter for the action's Feedback type.
        message: The wrapper as handed to the feedback callback.

    Returns:
        The encoded feedback payload.
    """
    return orjson.dumps(converter(message.feedback), option=orjson.OPT_SERIALIZE_NUMPY)
