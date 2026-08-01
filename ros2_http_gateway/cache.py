"""Latest-sample caches for topic read endpoints.

Subscriptions are created with ``raw=True``, so a message arriving costs one tuple
allocation and one attribute assignment: no deserialisation, no JSON, no lock. CPython
makes the assignment atomic, and the reader only ever sees a fully built sample or the
previous one, never a half-written mixture.

Deserialising and encoding happen on the asyncio side, when a request or a stream tick
actually needs the value, and the encoded bytes are memoised by sequence number. A topic
published at 1 kHz and read once a second is therefore deserialised once a second.

The response envelope is assembled by concatenating bytes around the memoised payload
rather than by re-encoding a dict, because ``stale`` depends on when the request arrived
while the payload does not.

**Deserialising and encoding run on the event loop, by design.** Control-plane payloads are
small enough that dispatching them to a thread pool would cost more than the encode, and the
memoisation is free of races *because* it is loop-confined: the check and the store in
:meth:`TopicCache.encoded_data` have no await between them, so no second reader can
interleave. That invariant is load-bearing. Nothing off the loop may call
:meth:`encoded_data` or :meth:`response_body`; the ROS side calls :meth:`offer` and nothing
else.

Staleness is measured on the monotonic clock, not the wall clock the envelope reports. An
NTP step would otherwise make fresh data look stale, or worse, stale data look fresh.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Final, NamedTuple

import orjson

from .codegen import MessageClass

__all__ = ["EMPTY_BODY", "Sample", "TopicCache"]

_ORJSON_OPTS: Final = orjson.OPT_SERIALIZE_NUMPY

# What a read returns before the first message arrives. A 200 with an explicit null beats a
# 404: the endpoint exists and is configured, there is simply nothing on the topic yet.
EMPTY_BODY: Final = b'{"data":null,"stamp":null,"seq":0,"stale":true}'


class Sample(NamedTuple):
    """One received message, still in its wire form.

    Two clocks on purpose: ``stamp`` is what the response reports, and ``monotonic`` is what
    staleness is judged against, so that resetting the system clock cannot make a fresh
    sample look stale or a stale one fresh.
    """

    raw: bytes
    seq: int
    stamp: float
    monotonic: float


class TopicCache:
    """Holds the newest sample of one topic and its memoised JSON encoding.

    Args:
        message_type: The generated message class carried on the topic.
        to_dict: Compiled converter for that type.
        stale_after_ms: Age past which a read reports ``"stale": true``; None never stales.
    """

    __slots__ = (
        "_encoded",
        "_encoded_seq",
        "_sample",
        "_seq",
        "_stale_after_s",
        "_to_dict",
        "message_type",
    )

    def __init__(
        self,
        message_type: MessageClass,
        to_dict: Callable[[Any], dict[str, Any]],
        stale_after_ms: int | None,
    ) -> None:
        self.message_type = message_type
        self._to_dict = to_dict
        self._stale_after_s = None if stale_after_ms is None else stale_after_ms / 1000.0
        self._sample: Sample | None = None
        self._seq = 0
        self._encoded: bytes = b""
        self._encoded_seq = -1

    # ---------------------------------------------------------------- ROS thread side

    def offer(self, raw: bytes) -> None:
        """Store a newly received raw message. Called from a ROS callback thread.

        The increment is safe without a lock because every subscription gets its own
        mutually exclusive callback group, so two callbacks for the same topic never
        overlap. The publish of the new tuple is a single attribute store, which is what
        makes a reader unable to observe a torn sample.

        Args:
            raw: The serialised CDR message as handed over by ``raw=True``.
        """
        self._seq += 1
        self._sample = Sample(raw, self._seq, time.time(), time.monotonic())

    # ------------------------------------------------------------- asyncio side

    @property
    def latest(self) -> Sample | None:
        """The newest sample, or None if nothing has arrived yet."""
        return self._sample

    @property
    def seq(self) -> int:
        """The sequence number of the newest sample, or 0 before the first one.

        Read from the sample rather than from the counter so that it can never run ahead of
        :attr:`latest`. :meth:`offer` increments the counter and then stores the sample, and a
        reader landing between the two would otherwise see a sequence number belonging to a
        message it cannot yet get at -- exactly the kind of thing a stream broadcaster would
        key off and then skip.
        """
        sample = self._sample
        return sample.seq if sample is not None else 0

    def encoded_data(self, sample: Sample) -> bytes:
        """Return the JSON encoding of ``sample``, deserialising at most once per sample.

        Must be called from the event loop thread; see the module docstring.

        Args:
            sample: The sample to encode, as returned by :attr:`latest`.

        Returns:
            The encoded message, without the surrounding response envelope.
        """
        if self._encoded_seq == sample.seq:
            return self._encoded

        from rclpy.serialization import deserialize_message

        message = deserialize_message(sample.raw, self.message_type)
        encoded = orjson.dumps(self._to_dict(message), option=_ORJSON_OPTS)
        self._encoded = encoded
        self._encoded_seq = sample.seq
        return encoded

    def is_stale(self, sample: Sample, now_monotonic: float) -> bool:
        """Whether ``sample`` is older than the configured staleness threshold.

        Args:
            sample: The sample to judge.
            now_monotonic: A :func:`time.monotonic` reading, not a wall-clock one.
        """
        return (
            self._stale_after_s is not None
            and (now_monotonic - sample.monotonic) > self._stale_after_s
        )

    def response_body(self, now_monotonic: float | None = None) -> bytes:
        """Build the full JSON body for a read of this endpoint.

        Must be called from the event loop thread; see the module docstring.

        Args:
            now_monotonic: Monotonic time to judge staleness against. Defaults to now.

        Returns:
            The encoded response body.
        """
        sample = self._sample
        if sample is None:
            return EMPTY_BODY

        payload = self.encoded_data(sample)
        now = time.monotonic() if now_monotonic is None else now_monotonic
        stale = self.is_stale(sample, now)
        return b"".join(
            (
                b'{"data":',
                payload,
                b',"stamp":',
                repr(sample.stamp).encode("ascii"),
                b',"seq":',
                b"%d" % sample.seq,
                b',"stale":',
                b"true" if stale else b"false",
                b"}",
            )
        )
