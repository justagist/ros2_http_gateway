"""The ROS half of the gateway: node, executor, subscriptions, publishers.

Everything here runs on ROS-owned threads. Nothing in this module touches the asyncio loop,
and nothing on the asyncio side calls into rclpy except through :meth:`RosRuntime.publish`,
which hands the message to a worker thread and returns immediately.

``EventsExecutor`` raises ``RuntimeError: Guard conditions not supported``, so the obvious way
to wake the ROS side for a publish is unavailable; publishing goes through a worker thread
blocking on a queue instead, which behaves the same under both executors.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Final

from .cache import TopicCache
from .config import ConfigError, Executor, QosPreset
from .resolve import ResolvedConfig

_LOG = logging.getLogger(__name__)

__all__ = [
    "ActionTimeoutError",
    "ActionUnavailableError",
    "RosRuntime",
    "ServiceTimeoutError",
    "ServiceUnavailableError",
    "make_executor",
    "qos_profile",
]


class ServiceUnavailableError(RuntimeError):
    """No server is offering the service this endpoint points at."""


class ServiceTimeoutError(RuntimeError):
    """The service did not answer within the endpoint's timeout."""


class ActionUnavailableError(RuntimeError):
    """No action server is offering the action this endpoint points at."""


class ActionTimeoutError(RuntimeError):
    """The action server did not accept or reject a goal within the endpoint's timeout."""


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


def _settle(target: asyncio.Future[Any], source: Any) -> None:
    """Move an rclpy future's outcome onto an asyncio future, on the loop thread."""
    if target.done():  # already timed out and gave up on this response
        return
    try:
        target.set_result(source.result())
    except Exception as exc:
        target.set_exception(exc)


def _cancel_if_accepted(future: Any) -> None:
    """Cancel a goal whose acceptance arrived after its caller stopped waiting. On a ROS thread."""
    if future.cancelled() or future.exception() is not None:
        return
    handle = future.result()
    if handle.accepted:
        _LOG.warning("a goal was accepted after its submission was abandoned; cancelling it")
        handle.cancel_goal_async()


def qos_profile(preset: QosPreset) -> Any:
    """Map a config QoS preset onto an rclpy profile.

    Reliability is why this option exists: a RELIABLE subscription does not match a BEST_EFFORT
    publisher, which is how nearly every sensor publishes.
    """
    # Not QoSPresetProfiles: Humble has no DEFAULT member. QoSProfile(depth=10) is what DEFAULT
    # carries on the distros that do have it.
    from rclpy.qos import QoSProfile, qos_profile_sensor_data

    if preset == "sensor_data":
        return qos_profile_sensor_data
    return QoSProfile(depth=_DEFAULT_QOS_DEPTH)


def make_executor(kind: Executor, context: Any) -> tuple[Any, str]:
    """Build the executor named by the config, feature-detecting the events one.

    Returns the executor and the name to log. Raises ImportError if ``events`` was asked for on a
    distro without it; ``auto`` falls back instead.
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
        # A factory function, not a class: this is a call, not a constructor.
        return EventsExecutor(context=context), "EventsExecutor"

    return MultiThreadedExecutor(context=context), "MultiThreadedExecutor"


class RosRuntime:
    """Owns the rclpy context, node, executor and the threads they run on."""

    __slots__ = (
        "_action_clients",
        "_clients",
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
        self._clients: dict[str, Any] = {}
        self._action_clients: dict[str, Any] = {}
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
        """Create everything and begin spinning."""
        import rclpy
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup

        self._context = rclpy.context.Context()
        self._context.init()
        self._node = rclpy.create_node(_NODE_NAME, context=self._context)

        for path, endpoint in self.resolved.endpoints.items():
            if endpoint.config.is_topic_read:
                if endpoint.to_dict is None:
                    raise RuntimeError(f"{path}: a topic read resolved without an encoder")
                cache = TopicCache(
                    endpoint.message_type, endpoint.to_dict, endpoint.config.stale_after_ms
                )
                self.caches[path] = cache
                # One group per subscription: same-topic callbacks stay serialised, which is
                # what lets the cache swap without a lock, while topics still run in parallel.
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
            elif endpoint.config.kind == "service":
                self._clients[path] = self._node.create_client(
                    endpoint.interface,
                    endpoint.ros_name,
                    callback_group=MutuallyExclusiveCallbackGroup(),
                )
                _LOG.info(
                    "service client %s <- %s [%s]", endpoint.ros_name, path, endpoint.type_name
                )
            elif endpoint.config.kind == "action":
                from rclpy.action import ActionClient
                from rclpy.callback_groups import ReentrantCallbackGroup

                # Reentrant, not mutually exclusive: serialising a goal's feedback, status and
                # result callbacks deadlocks the result future behind arriving feedback.
                self._action_clients[path] = ActionClient(
                    self._node,
                    endpoint.interface,
                    endpoint.ros_name,
                    callback_group=ReentrantCallbackGroup(),
                )
                _LOG.info(
                    "action client %s <- %s [%s]", endpoint.ros_name, path, endpoint.type_name
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

        False means the queue is full, i.e. a client is publishing faster than ROS drains.
        """
        try:
            self._outbox.put_nowait(_Outgoing(self._publishers[path], message))
        except queue.Full:
            return False
        return True

    def service_is_ready(self, path: str) -> bool:
        """Whether a server is currently offering this endpoint's service."""
        client = self._clients[path]
        ready: bool = client.service_is_ready()
        return ready

    async def call_service(self, path: str, request: Any, timeout_s: float) -> Any:
        """Call a service from the event loop without blocking it.

        On timeout the pending request is withdrawn, so a late reply cannot be mistaken for the
        answer to a later call.
        """
        client = self._clients[path]
        if not client.service_is_ready():
            raise ServiceUnavailableError(path)

        loop = asyncio.get_running_loop()
        settled: asyncio.Future[Any] = loop.create_future()
        ros_future = client.call_async(request)

        def on_done(future: Any) -> None:
            # A ROS callback thread: nothing here may touch the loop directly.
            loop.call_soon_threadsafe(_settle, settled, future)

        ros_future.add_done_callback(on_done)
        try:
            return await asyncio.wait_for(settled, timeout_s)
        except asyncio.TimeoutError:
            client.remove_pending_request(ros_future)
            raise ServiceTimeoutError(path) from None

    def action_server_is_ready(self, path: str) -> bool:
        """Whether an action server is currently offering this endpoint's action."""
        ready: bool = self._action_clients[path].server_is_ready()
        return ready

    async def send_goal(self, path: str, goal: Any, feedback: Any, timeout_s: float) -> Any:
        """Submit a goal and wait up to ``timeout_s`` for the server to accept or reject it.

        ``feedback`` is invoked on a ROS thread. Returns the goal handle, whose ``accepted``
        says whether the server took it.

        rclpy cannot withdraw a goal request. If the wait times out or is cancelled, a goal the
        server accepts later is cancelled: no caller holds its id, so nobody could follow or
        stop it.
        """
        client = self._action_clients[path]
        if not client.server_is_ready():
            raise ActionUnavailableError(path)
        ros_future = client.send_goal_async(goal, feedback_callback=feedback)
        try:
            return await asyncio.wait_for(self._bridge(ros_future), timeout_s)
        except asyncio.TimeoutError:
            ros_future.add_done_callback(_cancel_if_accepted)
            raise ActionTimeoutError(path) from None
        except asyncio.CancelledError:
            ros_future.add_done_callback(_cancel_if_accepted)
            raise

    async def goal_result(self, handle: Any) -> Any:
        """Wait for a goal to finish. No timeout: an action is long-running by definition."""
        return await self._bridge(handle.get_result_async())

    async def cancel_goal(self, handle: Any) -> Any:
        """Ask the server to cancel a goal and wait for its answer."""
        return await self._bridge(handle.cancel_goal_async())

    async def _bridge(self, ros_future: Any) -> Any:
        """Await an rclpy future on the event loop.

        rclpy futures are not awaitable, so a done callback schedules the outcome onto the loop.
        """
        loop = asyncio.get_running_loop()
        settled: asyncio.Future[Any] = loop.create_future()
        ros_future.add_done_callback(
            lambda future: loop.call_soon_threadsafe(_settle, settled, future)
        )
        return await settled

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

        An unmatched subscription is otherwise silent: the cache stays empty and every read
        answers ``"stale": true`` with nothing saying why. Waits on the stop event rather than
        sleeping so shutdown need not wait out the delay.
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

        A thread rather than an executor wakeup, because the events executor has no guard
        conditions; blocking on the queue also means an idle gateway does no work at all.
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
