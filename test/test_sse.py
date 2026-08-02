"""Broadcaster tests: fan-out, per-client filtering, backpressure. No ROS needed.

The cache is stubbed, so everything here is the asyncio half of the stream. ``test_events.py``
drives the same code over the HTTP route with a real ROS runtime behind it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ros2_http_gateway.sse import SseBroadcaster, event_frame

ALL = frozenset({"/a", "/b"})


class StubCache:
    """Enough of TopicCache for the broadcaster: a latest sample and a read body."""

    def __init__(self) -> None:
        self.on_offer: Any = None
        self.latest: Any = None
        self.seq = 0

    def offer(self, body: bytes) -> None:
        """Take a new sample and notify, exactly as the real cache does."""
        self.seq += 1
        self.latest = _Sample(self.seq)
        self._body = body
        if self.on_offer is not None:
            self.on_offer()

    def response_body(self) -> bytes:
        return self._body


class _Sample:
    __slots__ = ("seq",)

    def __init__(self, seq: int) -> None:
        self.seq = seq


def build(**overrides: Any) -> tuple[SseBroadcaster, dict[str, StubCache]]:
    caches = {"/a": StubCache(), "/b": StubCache()}
    settings: dict[str, Any] = {"overflow": "drop_oldest", "max_clients": 32}
    settings.update(overrides)
    return SseBroadcaster(caches, ["/a", "/b"], **settings), caches


def drain(client: Any) -> list[bytes | None]:
    frames: list[bytes | None] = []
    while True:
        try:
            frames.append(client.queue.get_nowait())
        except asyncio.QueueEmpty:
            return frames


def test_a_frame_names_the_endpoint_and_carries_the_read_body() -> None:
    assert event_frame("/mission/status", b'{"data":1}') == (
        b'event: /mission/status\ndata: {"data":1}\n\n'
    )


async def test_a_sample_reaches_every_client() -> None:
    broadcaster, caches = build()
    first = broadcaster.subscribe(ALL)
    second = broadcaster.subscribe(ALL)
    assert first is not None and second is not None

    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)

    assert drain(first) == [event_frame("/a", b'{"seq":1}')]
    assert drain(second) == [event_frame("/a", b'{"seq":1}')]


async def test_the_same_bytes_object_is_shared_by_every_client() -> None:
    """Encode once, fan out bytes: N listeners must cost N queue appends, not N encodes."""
    broadcaster, caches = build()
    first = broadcaster.subscribe(ALL)
    second = broadcaster.subscribe(ALL)
    assert first is not None and second is not None

    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)

    assert drain(first)[0] is drain(second)[0]


async def test_a_client_only_receives_the_endpoints_it_may_read() -> None:
    broadcaster, caches = build()
    limited = broadcaster.subscribe(frozenset({"/a"}))
    assert limited is not None

    caches["/b"].offer(b'{"secret":true}')
    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)

    assert drain(limited) == [event_frame("/a", b'{"seq":1}')]


async def test_the_snapshot_covers_only_permitted_endpoints_in_config_order() -> None:
    broadcaster, caches = build()
    caches["/a"].offer(b'{"a":1}')
    caches["/b"].offer(b'{"b":1}')

    both = broadcaster.subscribe(ALL)
    limited = broadcaster.subscribe(frozenset({"/b"}))
    assert both is not None and limited is not None

    assert list(broadcaster.snapshot(both)) == [
        event_frame("/a", b'{"a":1}'),
        event_frame("/b", b'{"b":1}'),
    ]
    assert list(broadcaster.snapshot(limited)) == [event_frame("/b", b'{"b":1}')]


async def test_one_sample_is_published_once_however_often_the_loop_is_woken() -> None:
    """The ROS side may schedule a drain that a previous drain has already covered."""
    broadcaster, caches = build()
    client = broadcaster.subscribe(ALL)
    assert client is not None

    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)
    broadcaster._drain("/a")
    broadcaster._drain("/a")

    assert drain(client) == [event_frame("/a", b'{"seq":1}')]


async def test_a_sample_landing_in_the_connect_window_appears_twice() -> None:
    """The documented one-frame anomaly: subscription precedes the snapshot, so both carry it.

    Pinned rather than fixed. Subscribing after the snapshot would lose the sample instead of
    repeating it, which is the worse failure for a latest-value stream.
    """
    broadcaster, caches = build()
    client = broadcaster.subscribe(ALL)
    assert client is not None

    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)

    assert next(iter(broadcaster.snapshot(client))) == event_frame("/a", b'{"seq":1}')
    assert drain(client) == [event_frame("/a", b'{"seq":1}')], "the queued copy is still there"


async def test_nothing_is_scheduled_while_nobody_is_connected() -> None:
    """An idle stream must cost the ROS thread one branch, not a loop wakeup per message."""
    broadcaster, caches = build()
    for _ in range(100):
        caches["/a"].offer(b'{"seq":1}')
    assert broadcaster._pending == set()


async def test_a_slow_client_loses_its_oldest_frames_not_its_newest() -> None:
    broadcaster, caches = build()
    client = broadcaster.subscribe(ALL)
    assert client is not None

    for index in range(200):
        caches["/a"].offer(b'{"seq":%d}' % index)
        await asyncio.sleep(0)

    frames = drain(client)
    assert len(frames) == 64, "the backlog is bounded"
    assert frames[-1] == event_frame("/a", b'{"seq":199}'), "the newest frame survives"
    assert client.dropped == 200 - 64
    assert not client.closed, "drop_oldest keeps the stream open"


async def test_disconnect_overflow_closes_the_stream_instead_of_dropping() -> None:
    broadcaster, caches = build(overflow="disconnect")
    client = broadcaster.subscribe(ALL)
    assert client is not None

    for index in range(200):
        caches["/a"].offer(b'{"seq":%d}' % index)
        await asyncio.sleep(0)

    frames = drain(client)
    assert client.closed
    assert frames[-1] is None, "the stream is sent the sentinel that ends it"
    assert client.dropped == 0


async def test_one_slow_client_does_not_cost_a_fast_one_anything() -> None:
    broadcaster, caches = build()
    slow = broadcaster.subscribe(ALL)
    fast = broadcaster.subscribe(ALL)
    assert slow is not None and fast is not None

    for index in range(200):
        caches["/a"].offer(b'{"seq":%d}' % index)
        await asyncio.sleep(0)
        drain(fast)

    assert slow.dropped > 0
    caches["/a"].offer(b'{"seq":200}')
    await asyncio.sleep(0)
    assert drain(fast) == [event_frame("/a", b'{"seq":200}')], "the fast client lost nothing"


async def test_subscribing_past_the_client_limit_is_refused() -> None:
    broadcaster, _ = build(max_clients=2)
    assert broadcaster.subscribe(ALL) is not None
    assert broadcaster.subscribe(ALL) is not None
    assert broadcaster.subscribe(ALL) is None
    assert broadcaster.clients == 2


async def test_a_departed_client_frees_its_slot() -> None:
    broadcaster, _ = build(max_clients=1)
    client = broadcaster.subscribe(ALL)
    assert client is not None
    assert broadcaster.subscribe(ALL) is None

    broadcaster.unsubscribe(client)
    assert broadcaster.clients == 0
    assert broadcaster.subscribe(ALL) is not None


async def test_an_unsubscribed_client_receives_nothing_further() -> None:
    broadcaster, caches = build()
    client = broadcaster.subscribe(ALL)
    assert client is not None
    broadcaster.unsubscribe(client)

    caches["/a"].offer(b'{"seq":1}')
    await asyncio.sleep(0)
    assert drain(client) == []


async def test_close_ends_every_stream() -> None:
    broadcaster, _ = build()
    first = broadcaster.subscribe(ALL)
    second = broadcaster.subscribe(ALL)
    assert first is not None and second is not None

    broadcaster.close()

    assert drain(first) == [None]
    assert drain(second) == [None]
    assert broadcaster.clients == 0


@pytest.mark.parametrize("overflow", ["drop_oldest", "disconnect"])
async def test_the_sentinel_is_delivered_even_to_a_full_queue(overflow: str) -> None:
    """A queue at maxsize accepts one frame by dropping its oldest and is then full again."""
    broadcaster, caches = build(overflow=overflow)
    client = broadcaster.subscribe(ALL)
    assert client is not None
    for index in range(64):
        caches["/a"].offer(b'{"seq":%d}' % index)
        await asyncio.sleep(0)
    assert client.queue.full()

    broadcaster.close()
    assert drain(client)[-1] is None
