"""Topic cache tests: memoisation, staleness on the monotonic clock, envelope shape."""

from __future__ import annotations

import time

import orjson
import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from rclpy.serialization import serialize_message
from rosidl_runtime_py.utilities import get_message

from ros2_http_gateway.cache import EMPTY_BODY, TopicCache
from ros2_http_gateway.codegen import build_msg_to_dict

pytestmark = pytest.mark.ros

STRING = "example_interfaces/msg/String"


def make_cache(stale_after_ms: int | None = None, type_name: str = STRING) -> TopicCache:
    cls = get_message(type_name)
    return TopicCache(cls, build_msg_to_dict(cls), stale_after_ms)


def raw_string(data: str, type_name: str = STRING) -> bytes:
    message = get_message(type_name)()
    message.data = data
    return serialize_message(message)


def test_an_empty_cache_reports_no_data() -> None:
    cache = make_cache()
    assert cache.latest is None
    assert cache.seq == 0
    assert cache.response_body() == EMPTY_BODY
    assert orjson.loads(EMPTY_BODY) == {"data": None, "stamp": None, "seq": 0, "stale": True}


def test_offer_stores_a_sample_with_both_clocks() -> None:
    cache = make_cache()
    before_wall, before_mono = time.time(), time.monotonic()
    cache.offer(raw_string("RUNNING"))

    sample = cache.latest
    assert sample is not None
    assert sample.seq == 1
    assert sample.stamp >= before_wall
    assert sample.monotonic >= before_mono


def test_the_sequence_number_increments_per_message() -> None:
    cache = make_cache()
    for expected in range(1, 6):
        cache.offer(raw_string("x"))
        assert cache.seq == expected


def test_seq_can_never_run_ahead_of_the_latest_sample() -> None:
    """A stream broadcaster keys off this; a seq with no reachable sample would skip a message."""
    cache = make_cache()
    assert cache.seq == 0 and cache.latest is None
    cache.offer(raw_string("x"))
    sample = cache.latest
    assert sample is not None
    assert cache.seq == sample.seq


def test_the_payload_is_encoded_once_per_sample() -> None:
    cache = make_cache()
    cache.offer(raw_string("RUNNING"))
    sample = cache.latest
    assert sample is not None

    first = cache.encoded_data(sample)
    assert first == b'{"data":"RUNNING"}'
    assert cache.encoded_data(sample) is first, "a second read must reuse the same bytes"


def test_a_new_sample_invalidates_the_memo() -> None:
    cache = make_cache()
    cache.offer(raw_string("FIRST"))
    first_sample = cache.latest
    assert first_sample is not None
    assert cache.encoded_data(first_sample) == b'{"data":"FIRST"}'

    cache.offer(raw_string("SECOND"))
    second_sample = cache.latest
    assert second_sample is not None
    assert cache.encoded_data(second_sample) == b'{"data":"SECOND"}'


def test_the_response_envelope_carries_data_stamp_seq_and_stale() -> None:
    cache = make_cache()
    cache.offer(raw_string("RUNNING"))
    body = orjson.loads(cache.response_body())
    sample = cache.latest
    assert sample is not None
    assert body == {
        "data": {"data": "RUNNING"},
        "stamp": sample.stamp,
        "seq": 1,
        "stale": False,
    }


# --------------------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------------------


def test_without_a_threshold_nothing_is_ever_stale() -> None:
    cache = make_cache(stale_after_ms=None)
    cache.offer(raw_string("x"))
    sample = cache.latest
    assert sample is not None
    assert cache.is_stale(sample, sample.monotonic + 10_000) is False


def test_staleness_is_judged_at_the_threshold() -> None:
    cache = make_cache(stale_after_ms=100)
    cache.offer(raw_string("x"))
    sample = cache.latest
    assert sample is not None
    assert cache.is_stale(sample, sample.monotonic + 0.05) is False
    assert cache.is_stale(sample, sample.monotonic + 0.10) is False, "exactly at the limit"
    assert cache.is_stale(sample, sample.monotonic + 0.11) is True


def test_staleness_ignores_the_wall_clock() -> None:
    """A clock step must not change whether data is stale.

    The envelope reports wall time because that is what a human wants to see, but comparing
    against it would mean an NTP correction could make a fresh sample look stale, or a stale
    one look fresh. So the arithmetic uses the monotonic reading instead.
    """
    cache = make_cache(stale_after_ms=1000)
    cache.offer(raw_string("x"))
    sample = cache.latest
    assert sample is not None

    # Pretend the wall clock jumped an hour backwards while no time really passed.
    shifted = sample._replace(stamp=sample.stamp - 3600.0)
    assert cache.is_stale(shifted, sample.monotonic) is False

    # And an hour forwards.
    shifted_forward = sample._replace(stamp=sample.stamp + 3600.0)
    assert cache.is_stale(shifted_forward, sample.monotonic) is False


def test_a_stale_sample_is_still_returned_just_flagged() -> None:
    """A greyed-out value is more use to an operator than a blank panel."""
    cache = make_cache(stale_after_ms=0)
    cache.offer(raw_string("LAST KNOWN"))
    time.sleep(0.01)
    body = orjson.loads(cache.response_body())
    assert body["stale"] is True
    assert body["data"] == {"data": "LAST KNOWN"}


def test_the_envelope_survives_a_payload_containing_braces() -> None:
    """The envelope is byte-concatenated, so a payload must not be able to break framing."""
    cache = make_cache()
    cache.offer(raw_string('}{"seq": 999, "stale": true, "'))
    body = orjson.loads(cache.response_body())
    assert body["seq"] == 1
    assert body["stale"] is False
    assert body["data"] == {"data": '}{"seq": 999, "stale": true, "'}


def test_a_numeric_array_payload_encodes_through_the_envelope() -> None:
    """Guards the concatenation against orjson's numpy path, which returns non-list types."""
    cache = make_cache(type_name="sensor_msgs/msg/Imu")
    message = get_message("sensor_msgs/msg/Imu")()
    message.orientation_covariance = [float(index) for index in range(9)]
    cache.offer(serialize_message(message))
    body = orjson.loads(cache.response_body())
    assert body["data"]["orientation_covariance"] == [float(index) for index in range(9)]
