"""The global server-sent-events stream: one connection carrying many endpoints.

A sample arrives on a ROS thread, which only flags the endpoint and wakes the loop. The loop
then builds the frame from the cache -- reusing the same memoised payload a GET would return,
so the two are byte-identical -- and hands the one bytes object to every client queue.

The frame is built on the loop because the cache's decode and encode are loop-confined; see
:mod:`ros2_http_gateway.cache`. The ROS thread does no work beyond a flag and a wakeup.

Only the newest sample is streamed. A topic published faster than the loop drains its wakeups
therefore shows gaps in ``seq``, exactly as a client polling the GET route would miss samples
between polls. This is a latest-value gateway, not a message bus.

One anomaly at connect, once per connection: a client is subscribed before its snapshot is
built, so a sample landing in between is both fanned into its queue and reflected in the
snapshot. The frame after the snapshot may therefore repeat its ``seq`` or carry a lower one.
``seq`` disambiguates. Subscribing after the snapshot instead would trade that duplicate for a
lost sample, which is the worse failure for a latest-value stream.

Each client sees only the endpoints its own roles permit, so including a restricted endpoint in
the stream cannot hand it to a caller who may not GET it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from .config import Overflow

if TYPE_CHECKING:  # importing the cache would pull in rosidl, and nothing here needs ROS
    from .cache import TopicCache

_LOG = logging.getLogger(__name__)

__all__ = ["HEARTBEAT_FRAME", "HEARTBEAT_S", "SseBroadcaster", "SseClient", "event_frame"]

# A comment frame, and how long a stream may say nothing before one is sent. Proxies and load
# balancers idle out a connection that says nothing, and an HMI watching a quiet robot can
# otherwise go minutes without an event.
#
# Both live here rather than beside the route that writes them, because the console manifest
# advertises the interval too: one definition, or the manifest lies the day it becomes
# configurable.
HEARTBEAT_FRAME: Final = b": keepalive\n\n"
HEARTBEAT_S: Final = 15.0

# Per-client backlog, matching the per-goal stream. Not configurable: `overflow` already says
# what should happen to a client that cannot keep up, which is the decision that matters.
_BACKLOG: Final = 64


def event_frame(endpoint: str, body: bytes) -> bytes:
    """Frame one endpoint's read body as a named SSE event."""
    return b"event: " + endpoint.encode("utf-8") + b"\ndata: " + body + b"\n\n"


@dataclass(slots=True)
class SseClient:
    """One connected stream and the endpoints it is allowed to see."""

    endpoints: frozenset[str]
    queue: asyncio.Queue[bytes | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_BACKLOG)
    )
    dropped: int = 0
    closed: bool = False


class SseBroadcaster:
    """Fans topic samples out to every connected stream.

    Loop-confined apart from :meth:`_notify`, which is the only method a ROS thread calls.
    """

    __slots__ = (
        "_caches",
        "_clients",
        "_include",
        "_loop",
        "_max_clients",
        "_overflow",
        "_pending",
        "_published",
    )

    def __init__(
        self,
        caches: Mapping[str, TopicCache],
        include: Sequence[str],
        *,
        overflow: Overflow,
        max_clients: int,
    ) -> None:
        self._caches = {key: caches[key] for key in include}
        self._include = tuple(include)
        self._overflow = overflow
        self._max_clients = max_clients
        self._clients: list[SseClient] = []
        self._pending: set[str] = set()
        self._published: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        for key, cache in self._caches.items():
            cache.on_offer = functools.partial(self._notify, key)

    @property
    def include(self) -> tuple[str, ...]:
        """The endpoint keys this stream carries, in config order."""
        return self._include

    @property
    def clients(self) -> int:
        """How many streams are connected."""
        return len(self._clients)

    def subscribe(self, allowed: frozenset[str]) -> SseClient | None:
        """Attach a stream limited to ``allowed``, or None if the client limit is reached."""
        if len(self._clients) >= self._max_clients:
            return None
        # Before the client is visible to _notify, which reads the loop without checking it.
        self._loop = asyncio.get_running_loop()
        client = SseClient(endpoints=allowed)
        self._clients.append(client)
        return client

    def unsubscribe(self, client: SseClient) -> None:
        """Detach a stream, whether it ended or the client went away."""
        client.closed = True
        try:
            self._clients.remove(client)
        except ValueError:  # pragma: no cover - unsubscribed twice
            pass

    def snapshot(self, client: SseClient) -> Iterator[bytes]:
        """Yield the current state of every endpoint this client may see.

        Endpoints that have received nothing yet are included, with the same
        ``"data": null`` body a GET would give, so a client learns the full endpoint set up
        front instead of discovering it as traffic happens to arrive.
        """
        for key in self._include:
            if key in client.endpoints:
                yield event_frame(key, self._caches[key].response_body())

    def close(self) -> None:
        """End every connected stream. For shutdown."""
        for client in tuple(self._clients):
            client.closed = True
            _offer(client.queue, None)
        self._clients.clear()

    # ---------------------------------------------------------------- ROS thread side

    def _notify(self, key: str) -> None:
        """Wake the loop for ``key``. Called from a ROS callback thread.

        Reads ``_clients`` without synchronisation: the list is only mutated on the loop, and
        both stale answers are harmless. A client that has just connected already has its
        snapshot and will be woken by the next sample; one that has just left costs an empty
        fan-out.
        """
        if not self._clients or key in self._pending:
            return
        self._pending.add(key)
        assert self._loop is not None  # set before the first client became visible
        self._loop.call_soon_threadsafe(self._drain, key)

    # ------------------------------------------------------------------- loop side

    def _drain(self, key: str) -> None:
        """Publish the newest sample of one endpoint to every interested client."""
        # Discarded before the sample is read, so a sample arriving during this call schedules
        # another drain rather than being swallowed by the flag.
        self._pending.discard(key)
        cache = self._caches[key]
        sample = cache.latest
        if sample is None or sample.seq == self._published.get(key):
            return
        # From the sample read above, not from whatever response_body finds a moment later: a
        # sample landing between these lines is carried by the frame but not recorded here, so
        # the next wakeup re-sends it. Recording the newer seq instead would suppress that
        # wakeup and lose a sample outright. Under-record for a possible duplicate rather than
        # over-record for a possible loss -- the same trade the connect window makes.
        self._published[key] = sample.seq

        frame = event_frame(key, cache.response_body())
        for client in self._clients:
            if not client.closed and key in client.endpoints:
                self._send(client, frame)

    def _send(self, client: SseClient, frame: bytes) -> None:
        try:
            client.queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        if self._overflow == "disconnect":
            client.closed = True
            _offer(client.queue, None)
            _LOG.warning("disconnecting an SSE client that fell %d frames behind", _BACKLOG)
            return

        client.dropped += 1
        _offer(client.queue, frame)
        if client.dropped == 1:
            # Once per client: the gap is visible in seq, but a silent one reads as a quiet
            # robot rather than a slow reader.
            _LOG.warning("an SSE client is not keeping up; dropping its oldest queued events")


def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
    """Append to a full queue by discarding its oldest entry.

    The sentinel goes through here too: a queue at maxsize accepts one frame by dropping its
    oldest and is then full again, so a bare ``put_nowait`` for the sentinel would raise and
    leave the stream open until the connection died.
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - it was full a moment ago
            pass
        queue.put_nowait(item)
