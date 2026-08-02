"""Latest-sample caches for topic read endpoints.

Subscriptions use ``raw=True``: arrival costs one tuple and one attribute store, atomic under
CPython, so a reader never sees a torn sample. Decoding and encoding are deferred to the read
side and memoised by sequence number.

Decode and encode run on the event loop. That is what makes the memoisation race-free -- check
and store have no await between them -- so nothing off the loop may call :meth:`encoded_data`,
:meth:`decoded` or :meth:`response_body`. The ROS side calls :meth:`offer` and nothing else.
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

    Two clocks: ``stamp`` is reported to clients, ``monotonic`` judges staleness, so a clock
    step cannot make fresh data look stale or stale data fresh.
    """

    raw: bytes
    seq: int
    stamp: float
    monotonic: float


class TopicCache:
    """Holds the newest sample of one topic and its memoised JSON encoding.

    ``stale_after_ms`` of None never stales.
    """

    __slots__ = (
        "_decoded",
        "_decoded_seq",
        "_encoded",
        "_encoded_seq",
        "_sample",
        "_seq",
        "_stale_after_s",
        "_to_dict",
        "message_type",
        "on_offer",
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
        self._decoded: dict[str, Any] = {}
        self._decoded_seq = -1
        self.on_offer: Callable[[], None] | None = None

    # ---------------------------------------------------------------- ROS thread side

    def offer(self, raw: bytes) -> None:
        """Store a newly received raw CDR message. Called from a ROS callback thread.

        Lock-free: each subscription has its own mutually exclusive callback group, so
        callbacks for one topic never overlap.

        ``on_offer``, if set, is called here and so also runs on the ROS thread. The SSE
        broadcaster uses it to wake the loop; it must not touch the loop-confined methods.
        """
        self._seq += 1
        self._sample = Sample(raw, self._seq, time.time(), time.monotonic())
        if self.on_offer is not None:
            self.on_offer()

    # ------------------------------------------------------------- asyncio side

    @property
    def latest(self) -> Sample | None:
        """The newest sample, or None if nothing has arrived yet."""
        return self._sample

    @property
    def seq(self) -> int:
        """Sequence number of the newest sample, or 0 before the first.

        Read from the sample, not the counter, so it can never run ahead of :attr:`latest` --
        a reader landing mid-:meth:`offer` would otherwise see a seq whose sample is not there.
        """
        sample = self._sample
        return sample.seq if sample is not None else 0

    def decoded(self, sample: Sample) -> dict[str, Any]:
        """Return ``sample`` as a converted dict, deserialising at most once per sample.

        Memoised separately from the encoded bytes so a policy check reading one field and a GET
        returning the whole message share one deserialisation, in either order. Numeric arrays
        are still numpy, so this is not plain JSON.
        """
        if self._decoded_seq == sample.seq:
            return self._decoded

        from rclpy.serialization import deserialize_message

        value = self._to_dict(deserialize_message(sample.raw, self.message_type))
        self._decoded = value
        self._decoded_seq = sample.seq
        return value

    def encoded_data(self, sample: Sample) -> bytes:
        """Return the JSON encoding of ``sample``, without the response envelope."""
        if self._encoded_seq == sample.seq:
            return self._encoded

        encoded = orjson.dumps(self.decoded(sample), option=_ORJSON_OPTS)
        self._encoded = encoded
        self._encoded_seq = sample.seq
        return encoded

    def is_stale(self, sample: Sample, now_monotonic: float) -> bool:
        """Whether ``sample`` is older than the threshold. ``now_monotonic`` is not wall clock."""
        return (
            self._stale_after_s is not None
            and (now_monotonic - sample.monotonic) > self._stale_after_s
        )

    def response_body(self, now_monotonic: float | None = None) -> bytes:
        """Build the JSON body for a read, concatenated around the memoised payload.

        Not re-encoded as a dict because ``stale`` depends on request time while the payload
        does not.
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
