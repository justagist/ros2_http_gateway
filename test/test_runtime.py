"""ROS runtime tests: executor selection, subscriptions, publishing, shutdown.

The executor is parametrised rather than taken from the config default, because the brief
requires clean startup and shutdown under both, and because the events executor turned out
to differ from the threaded one in ways that matter (no guard-condition support).
"""

from __future__ import annotations

import builtins
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from gateway_fixtures import (
    Talker,
    available_executors,
    events_executor_available,
    resolved_config,
)

from ros2_http_gateway.config import ConfigError
from ros2_http_gateway.runtime import RosRuntime, make_executor

pytestmark = pytest.mark.ros

EXECUTORS = available_executors()
# What the distro provides, not what this run happens to be exercising: the CI matrix narrows
# EXECUTORS through the environment, and a multithreaded-only run on a distro that has the
# events executor must not be mistaken for a distro that lacks it.
HAS_EVENTS = events_executor_available()


@pytest.fixture(params=EXECUTORS)
def runtime(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RosRuntime]:
    """A started runtime, once per executor."""
    resolved = resolved_config(tmp_path, ros={"executor": request.param})
    instance = RosRuntime(resolved)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


# --------------------------------------------------------------------------------------
# executor selection
# --------------------------------------------------------------------------------------


def test_multithreaded_is_selected_when_asked_for() -> None:
    import rclpy

    context = rclpy.context.Context()
    context.init()
    try:
        executor, name = make_executor("multithreaded", context)
        assert name == "MultiThreadedExecutor"
        executor.shutdown()
    finally:
        context.try_shutdown()


@pytest.mark.skipif(not HAS_EVENTS, reason="this distro has no EventsExecutor")
def test_auto_prefers_the_events_executor_where_it_exists() -> None:
    import rclpy

    context = rclpy.context.Context()
    context.init()
    try:
        _, name = make_executor("auto", context)
        assert name == "EventsExecutor"
    finally:
        context.try_shutdown()


@pytest.mark.skipif(HAS_EVENTS, reason="this distro has an EventsExecutor")
def test_auto_uses_the_threaded_executor_where_events_is_absent() -> None:
    """The Humble side of the same coin, asserted for real rather than by monkeypatch."""
    import rclpy

    context = rclpy.context.Context()
    context.init()
    try:
        executor, name = make_executor("auto", context)
        assert name == "MultiThreadedExecutor"
        executor.shutdown()
    finally:
        context.try_shutdown()


def test_auto_falls_back_when_the_events_executor_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Humble is the feature floor, so `auto` has to degrade rather than fail."""
    import rclpy

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "rclpy.experimental":
            raise ImportError("simulating a distro without EventsExecutor")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
    context = rclpy.context.Context()
    context.init()
    try:
        executor, name = make_executor("auto", context)
        assert name == "MultiThreadedExecutor"
        executor.shutdown()
    finally:
        context.try_shutdown()


def test_events_requested_but_missing_is_a_startup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`events` is the explicit spelling, so it must not silently degrade."""
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "rclpy.experimental":
            raise ImportError("simulating a distro without EventsExecutor")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    resolved = resolved_config(tmp_path, ros={"executor": "events"})
    monkeypatch.setattr(builtins, "__import__", blocked)
    runtime = RosRuntime(resolved)
    with pytest.raises(ConfigError, match=r"no rclpy\.experimental\.EventsExecutor"):
        runtime.start()
    # The failed start must not leave a node or context behind.
    assert runtime.node is None


# --------------------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("executor", EXECUTORS)
def test_start_and_stop_are_clean(tmp_path: Path, executor: str) -> None:
    import threading

    before = {thread.name for thread in threading.enumerate()}
    runtime = RosRuntime(resolved_config(tmp_path, ros={"executor": executor}))
    runtime.start()
    assert runtime.executor_name
    runtime.stop()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        leaked = {t.name for t in threading.enumerate()} - before
        if not leaked:
            break
        time.sleep(0.05)
    assert not {t.name for t in threading.enumerate()} - before, "runtime leaked a thread"
    assert runtime.node is None


@pytest.mark.parametrize("executor", EXECUTORS)
def test_stop_is_idempotent(tmp_path: Path, executor: str) -> None:
    """serve() stops the runtime in the lifespan and again in a finally."""
    runtime = RosRuntime(resolved_config(tmp_path, ros={"executor": executor}))
    runtime.start()
    runtime.stop()
    runtime.stop()


def test_caches_exist_only_for_topic_reads(runtime: RosRuntime) -> None:
    assert set(runtime.caches) == {"/mission/status", "/never/stales"}


# --------------------------------------------------------------------------------------
# QoS
# --------------------------------------------------------------------------------------


def test_qos_presets_map_to_the_rclpy_profiles() -> None:
    """A reliable subscription never matches a best-effort publisher, hence the option."""
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSReliabilityPolicy

    from ros2_http_gateway.runtime import qos_profile

    default = qos_profile("default")
    assert default.reliability == QoSReliabilityPolicy.RELIABLE
    assert default.durability == DurabilityPolicy.VOLATILE
    assert default.history == HistoryPolicy.KEEP_LAST
    assert default.depth == 10

    assert qos_profile("sensor_data").reliability == QoSReliabilityPolicy.BEST_EFFORT


def test_qos_does_not_read_distro_specific_preset_members() -> None:
    """Regression cover for a startup AttributeError on Humble.

    ``QoSPresetProfiles`` membership varies by distro -- Humble has no ``DEFAULT`` -- so the
    profiles are built from ``QoSProfile`` and the long-standing module-level constant
    instead. This is the whole reason the Humble leg of CI is worth having.
    """
    source = (Path(__file__).resolve().parents[1] / "ros2_http_gateway" / "runtime.py").read_text()
    body = source[source.index("def qos_profile") : source.index("def make_executor")]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "QoSPresetProfiles" not in code, "the comment may mention it; the code may not"


def test_a_sensor_data_endpoint_receives_from_a_best_effort_publisher(tmp_path: Path) -> None:
    """The failure this option exists to prevent, exercised end to end."""
    import rclpy
    from example_interfaces.msg import String
    from rclpy.qos import qos_profile_sensor_data

    endpoints = {
        "/sensor": {
            "topic": "/gateway_qos_sensor",
            "type": "example_interfaces/msg/String",
            "roles": ["viewer"],
            "qos": "sensor_data",
        }
    }
    runtime = RosRuntime(resolved_config(tmp_path, endpoints=endpoints))
    runtime.start()

    context = rclpy.context.Context()
    context.init()
    publisher_node = rclpy.create_node("qos_best_effort_talker", context=context)
    publisher = publisher_node.create_publisher(
        String, "/gateway_qos_sensor", qos_profile_sensor_data
    )
    try:
        cache = runtime.caches["/sensor"]
        message = String()
        message.data = "sensor"
        deadline = time.monotonic() + 15
        while cache.latest is None and time.monotonic() < deadline:
            publisher.publish(message)
            time.sleep(0.02)
        assert cache.latest is not None, "a best-effort publisher did not reach the endpoint"
    finally:
        publisher_node.destroy_node()
        context.try_shutdown()
        runtime.stop()


def test_an_unmatched_subscription_is_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty cache is otherwise silent: no publisher, no message, no explanation."""
    import ros2_http_gateway.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_MATCH_WARN_AFTER_S", 0.2)
    endpoints = {
        "/nobody": {
            "topic": "/gateway_topic_with_no_publisher",
            "type": "example_interfaces/msg/String",
            "roles": ["viewer"],
        }
    }
    runtime = RosRuntime(resolved_config(tmp_path, endpoints=endpoints))
    with caplog.at_level("WARNING"):
        runtime.start()
        deadline = time.monotonic() + 10
        while "nothing is publishing" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        runtime.stop()

    assert "nothing is publishing /gateway_topic_with_no_publisher" in caplog.text
    assert "qos: sensor_data" in caplog.text, "the warning should name the likely cause"


# --------------------------------------------------------------------------------------
# data flow
# --------------------------------------------------------------------------------------


def test_a_published_message_reaches_the_cache(runtime: RosRuntime, talker: Talker) -> None:
    cache = runtime.caches["/mission/status"]
    assert talker.publish_until("RUNNING", lambda: cache.latest is not None), (
        "the gateway never received a message"
    )

    sample = cache.latest
    assert sample is not None
    assert cache.encoded_data(sample) == b'{"data":"RUNNING"}'
    assert sample.seq >= 1


def test_the_sequence_number_counts_every_message(runtime: RosRuntime, talker: Talker) -> None:
    cache = runtime.caches["/mission/status"]
    assert talker.publish_until("first", lambda: cache.latest is not None)

    start = cache.seq
    for _ in range(10):
        talker.publish("again")
    deadline = time.monotonic() + 5
    while cache.seq < start + 10 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cache.seq >= start + 10


def test_two_endpoints_on_one_topic_each_get_their_own_cache(
    runtime: RosRuntime, talker: Talker
) -> None:
    """They differ in staleness policy, so they cannot share a slot."""
    fast = runtime.caches["/mission/status"]
    never = runtime.caches["/never/stales"]
    assert talker.publish_until(
        "RUNNING", lambda: fast.latest is not None and never.latest is not None
    )
    assert fast is not never


def test_publish_reaches_a_real_subscriber(runtime: RosRuntime, talker: Talker) -> None:
    from example_interfaces.msg import String

    message = String()
    message.data = "go"
    deadline = time.monotonic() + 15
    while not talker.heard and time.monotonic() < deadline:
        assert runtime.publish("/cmd", message)
        time.sleep(0.05)
    assert "go" in talker.wait_for_command()


def test_publish_reports_a_full_queue_rather_than_blocking(runtime: RosRuntime) -> None:
    """A client publishing faster than ROS drains must get an answer, not a stall."""
    from example_interfaces.msg import String

    message = String()
    message.data = "flood"
    # The worker drains continuously, so fill the queue directly to make the state reachable.
    runtime._outbox.maxsize = 1
    while not runtime._outbox.full():
        runtime._outbox.put_nowait(None)
    assert runtime.publish("/cmd", message) is False
