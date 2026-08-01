"""The ROS half of the gateway: node, executor, subscriptions, publishers.

Everything here runs on ROS-owned threads. Nothing in this module touches the asyncio loop,
and nothing on the asyncio side calls into rclpy except through :meth:`RosRuntime.publish`,
which hands the message to a worker thread and returns immediately.

Two findings from measuring the runtime shaped this file:

* ``rclpy.experimental.EventsExecutor`` is a *factory function*, not a class, and it takes
  a keyword-only ``context``. Both executors are therefore constructed the same way.
* That executor raises ``RuntimeError: Guard conditions not supported``, which kills the
  spin thread. A guard condition is the obvious way to wake the ROS side when the asyncio
  side wants something published, so publishing instead goes through a dedicated worker
  thread blocking on a queue. That works identically under both executors and keeps the
  publish call off the event loop, which was the actual requirement.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Final

from .cache import TopicCache
from .config import ConfigError, Executor, QosPreset
from .resolve import ResolvedConfig

_LOG = logging.getLogger(__name__)

__all__ = ["RosRuntime", "make_executor"]

_NODE_NAME: Final = "ros2_http_gateway"
_QUEUE_TIMEOUT_S: Final = 0.1
_PUBLISH_QUEUE_SIZE: Final = 1024
_SHUTDOWN_JOIN_S: Final = 5.0
# How long to give discovery before complaining that a subscription found nobody. Long
# enough not to cry wolf while a system is still coming up.
_MATCH_WARN_AFTER_S: Final = 8.0
# Matches what QoSPresetProfiles.DEFAULT carries on the distros that define it.
_DEFAULT_QOS_DEPTH: Final = 10


@dataclass(frozen=True, slots=True)
class _Outgoing:
    """One message waiting to be published from the worker thread."""

    publisher: Any
    message: Any


def qos_profile(preset: QosPreset) -> Any:
    """Map a config QoS preset onto an rclpy profile.

    Reliability is the reason this option exists: a RELIABLE subscription simply does not
    match a BEST_EFFORT publisher, which is how nearly every sensor publishes. Without a way
    to say ``sensor_data``, pointing an endpoint at a camera would give an endlessly empty
    cache with nothing anywhere explaining why.

    Args:
        preset: ``default`` or ``sensor_data``.

    Returns:
        The rclpy QoS profile to subscribe or publish with.
    """
    # Built from the primitives rather than from QoSPresetProfiles, whose membership varies
    # by distro: Humble has no DEFAULT member, so reading it there is an AttributeError at
    # startup. QoSProfile(depth=10) is byte-for-byte what DEFAULT is on the distros that do
    # have it (verified: reliable, volatile, keep-last, depth 10), and
    # qos_profile_sensor_data has existed for as long as rclpy has.
    from rclpy.qos import QoSProfile, qos_profile_sensor_data

    if preset == "sensor_data":
        return qos_profile_sensor_data
    return QoSProfile(depth=_DEFAULT_QOS_DEPTH)


def make_executor(kind: Executor, context: Any) -> tuple[Any, str]:
    """Build the executor named by the config, feature-detecting the events one.

    Args:
        kind: ``auto``, ``events`` or ``multithreaded``.
        context: The rclpy context the executor belongs to.

    Returns:
        The executor and the name to log.

    Raises:
        ImportError: If ``events`` was asked for on a distro without it. ``auto`` falls back
            instead, which is the whole point of the two spellings.
    """
    from rclpy.executors import MultiThreadedExecutor

    if kind in ("auto", "events"):
        try:
            from rclpy.experimental import EventsExecutor
        except ImportError:
            if kind == "events":
                raise
            _LOG.info("EventsExecutor is not available on this distro, using MultiThreadedExecutor")
            return MultiThreadedExecutor(context=context), "MultiThreadedExecutor"
        # A factory function on the distros that have it, so this is a call and not a
        # constructor; do not be tempted to subclass it.
        return EventsExecutor(context=context), "EventsExecutor"

    return MultiThreadedExecutor(context=context), "MultiThreadedExecutor"


class RosRuntime:
    """Owns the rclpy context, node, executor and the threads they run on.

    Args:
        resolved: A fully resolved configuration.
    """

    __slots__ = (
        "_context",
        "_executor",
        "_executor_name",
        "_match_thread",
        "_node",
        "_outbox",
        "_publishers",
        "_spin_thread",
        "_stopping",
        "_subscribed",
        "_worker_thread",
        "caches",
        "resolved",
    )

    def __init__(self, resolved: ResolvedConfig) -> None:
        self.resolved = resolved
        self.caches: dict[str, TopicCache] = {}
        self._publishers: dict[str, Any] = {}
        self._subscribed: dict[str, str] = {}
        self._context: Any = None
        self._node: Any = None
        self._executor: Any = None
        self._executor_name = ""
        self._spin_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._match_thread: threading.Thread | None = None
        self._outbox: queue.Queue[_Outgoing | None] = queue.Queue(maxsize=_PUBLISH_QUEUE_SIZE)
        self._stopping = threading.Event()

    @property
    def executor_name(self) -> str:
        """Which executor was chosen, for the health route and the startup log."""
        return self._executor_name

    @property
    def node(self) -> Any:
        """The gateway's rclpy node."""
        return self._node

    def start(self) -> None:
        """Create everything and begin spinning.

        Raises:
            ConfigError: If ``ros.executor: events`` was requested on a distro that has no
                EventsExecutor.
        """
        import rclpy
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup

        self._context = rclpy.context.Context()
        self._context.init()
        self._node = rclpy.create_node(_NODE_NAME, context=self._context)

        for path, endpoint in self.resolved.endpoints.items():
            if endpoint.config.is_topic_read:
                assert endpoint.to_dict is not None
                cache = TopicCache(
                    endpoint.message_type, endpoint.to_dict, endpoint.config.stale_after_ms
                )
                self.caches[path] = cache
                # One callback group per subscription: callbacks for the same topic stay
                # serialised, which is what lets the cache increment its sequence number
                # without a lock, while different topics still run in parallel.
                self._node.create_subscription(
                    endpoint.message_type,
                    endpoint.ros_name,
                    cache.offer,
                    qos_profile(endpoint.config.qos),
                    raw=True,
                    callback_group=MutuallyExclusiveCallbackGroup(),
                )
                self._subscribed[path] = endpoint.ros_name
                _LOG.info(
                    "subscribed %s -> %s [%s] qos=%s",
                    endpoint.ros_name,
                    path,
                    endpoint.type_name,
                    endpoint.config.qos,
                )
            elif endpoint.config.is_topic_write:
                self._publishers[path] = self._node.create_publisher(
                    endpoint.message_type, endpoint.ros_name, qos_profile(endpoint.config.qos)
                )
                _LOG.info(
                    "publisher %s <- %s [%s] qos=%s",
                    endpoint.ros_name,
                    path,
                    endpoint.type_name,
                    endpoint.config.qos,
                )

        try:
            self._executor, self._executor_name = make_executor(
                self.resolved.config.ros.executor, self._context
            )
        except ImportError as exc:
            self._tear_down()
            raise ConfigError(
                self.resolved.config.source,
                [
                    f"ros.executor: `events` was requested but this distro has no "
                    f"rclpy.experimental.EventsExecutor ({exc}). Use `auto` to fall back "
                    "automatically, or `multithreaded` to be explicit."
                ],
            ) from None

        self._executor.add_node(self._node)
        _LOG.info("ROS executor: %s", self._executor_name)

        self._spin_thread = threading.Thread(target=self._spin, name="ros-executor", daemon=True)
        self._spin_thread.start()
        self._worker_thread = threading.Thread(
            target=self._publish_worker, name="ros-publisher", daemon=True
        )
        self._worker_thread.start()
        if self._subscribed:
            self._match_thread = threading.Thread(
                target=self._warn_about_unmatched, name="ros-match-check", daemon=True
            )
            self._match_thread.start()

    def publish(self, path: str, message: Any) -> bool:
        """Hand a message to the ROS side for publishing. Returns immediately.

        Args:
            path: The endpoint key, which selects the publisher.
            message: The message instance to publish.

        Returns:
            True if the message was queued, False if the queue is full, which means a
            client is publishing faster than the ROS side can drain.
        """
        try:
            self._outbox.put_nowait(_Outgoing(self._publishers[path], message))
        except queue.Full:
            return False
        return True

    def stop(self) -> None:
        """Stop spinning, drain the publish queue and destroy the node."""
        if self._stopping.is_set():
            return
        self._stopping.set()

        if self._match_thread is not None:
            # It waits on the stop event, so setting it above already released the thread.
            self._match_thread.join(timeout=_SHUTDOWN_JOIN_S)
            self._match_thread = None

        if self._worker_thread is not None:
            self._outbox.put(None)
            self._worker_thread.join(timeout=_SHUTDOWN_JOIN_S)
            self._worker_thread = None

        if self._executor is not None:
            self._executor.shutdown()
        if self._spin_thread is not None:
            self._spin_thread.join(timeout=_SHUTDOWN_JOIN_S)
            if self._spin_thread.is_alive():
                _LOG.warning("the ROS executor thread did not stop within %.0fs", _SHUTDOWN_JOIN_S)
            self._spin_thread = None

        self._tear_down()
        _LOG.info("ROS runtime stopped")

    def _tear_down(self) -> None:
        if self._node is not None:
            self._node.destroy_node()
            self._node = None
        if self._context is not None:
            self._context.try_shutdown()
            self._context = None
        self._executor = None

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception:
            # Only interesting if it happens before a deliberate shutdown; afterwards a
            # raise out of spin() is just the executor noticing it was torn down.
            if not self._stopping.is_set():
                _LOG.exception("the ROS executor stopped unexpectedly")

    def _warn_about_unmatched(self) -> None:
        """Say something once if a subscription has found no publisher.

        An unmatched subscription is silent by nature: the cache stays empty, every read
        answers ``"stale": true``, and nothing anywhere says why. The usual causes are a
        misspelled topic name and a QoS mismatch, so both get named.

        Waits on the stop event rather than sleeping, so shutting down does not have to wait
        for the delay to elapse.
        """
        if self._stopping.wait(timeout=_MATCH_WARN_AFTER_S):
            return
        node = self._node
        if node is None:  # pragma: no cover - stop() raced with the delay
            return
        for path, topic in self._subscribed.items():
            if node.count_publishers(topic) == 0:
                _LOG.warning(
                    "%s: nothing is publishing %s after %.0fs, so reads will stay stale. "
                    "Check the topic name, and whether the publisher offers best-effort QoS "
                    "(set `qos: sensor_data` on this endpoint if so).",
                    path,
                    topic,
                    _MATCH_WARN_AFTER_S,
                )

    def _publish_worker(self) -> None:
        """Drain the outbox onto ROS publishers.

        A dedicated thread rather than an executor wakeup, because the events executor does
        not support guard conditions. Blocking on the queue also means an idle gateway does
        no work at all, which a polling timer could not manage.
        """
        while True:
            try:
                item = self._outbox.get(timeout=_QUEUE_TIMEOUT_S)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if item is None:
                return
            try:
                item.publisher.publish(item.message)
            except Exception:  # pragma: no cover - a publish failure must not kill the thread
                _LOG.exception("publishing to ROS failed")
