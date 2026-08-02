"""A single node with one of everything the gateway exposes, for trying it out.

    ros2 run ros2_http_gateway ros2-http-gateway-demo
    ros2-http-gateway examples/demo.yaml

Publishes a mission state cycling IDLE -> RUNNING -> IDLE, a chatter topic at 2 Hz, answers a
``std_srvs/Trigger`` service, and serves a deliberately slow Fibonacci action so feedback,
cancellation and the goal lifecycle are all visible at human speed.

Its interface packages are imported lazily: they are ordinary ROS dependencies of the demo, not
of the gateway, and a missing one should say so rather than break an import chain.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

_LOG = logging.getLogger("ros2_http_gateway.demo")

STATE_TOPIC = "/mission/state"
CHATTER_TOPIC = "/chatter"
TRIGGER_SERVICE = "/mission_control"
ACTION = "/fibonacci"

# Long enough to watch the gate flip in a browser, short enough not to test anyone's patience.
_STATE_HOLD_S = 8.0
_CHATTER_PERIOD_S = 0.5
_FIBONACCI_STEP_S = 0.6


def main(argv: list[str] | None = None) -> int:
    """Run the demo node until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    del argv

    try:
        import rclpy
        from example_interfaces.action import Fibonacci
        from example_interfaces.msg import String
        from rclpy.action import ActionServer, CancelResponse, GoalResponse
        from rclpy.callback_groups import ReentrantCallbackGroup
        from std_srvs.srv import Trigger
    except ImportError as exc:
        print(
            f"error: {exc.name} is not available ({exc}).\n"
            "The demo needs a sourced ROS 2 environment with example_interfaces and std_srvs:\n"
            "  source /opt/ros/$ROS_DISTRO/setup.bash\n"
            "  sudo apt install ros-$ROS_DISTRO-example-interfaces ros-$ROS_DISTRO-std-srvs",
            file=sys.stderr,
        )
        return 1

    context = rclpy.context.Context()
    context.init()
    node = rclpy.create_node("gateway_demo", context=context)
    state = _State()

    state_publisher = node.create_publisher(String, STATE_TOPIC, 10)
    chatter_publisher = node.create_publisher(String, CHATTER_TOPIC, 10)

    def on_trigger(_request: Any, response: Any) -> Any:
        # Answers whatever it is asked, so the gateway's precondition is the only thing gating
        # it: that is the behaviour the demo is meant to show.
        response.success = True
        response.message = f"acknowledged while {state.value}"
        _LOG.info("%s called while %s", TRIGGER_SERVICE, state.value)
        return response

    node.create_service(
        Trigger, TRIGGER_SERVICE, on_trigger, callback_group=ReentrantCallbackGroup()
    )

    def execute(goal_handle: Any) -> Any:
        order = max(1, int(goal_handle.request.order))
        sequence = [0, 1]
        _LOG.info("goal accepted, order %d", order)
        for _ in range(order - 1):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                _LOG.info("goal canceled after %d terms", len(sequence))
                result = Fibonacci.Result()
                result.sequence = sequence
                return result
            sequence.append(sequence[-1] + sequence[-2])
            feedback = Fibonacci.Feedback()
            feedback.sequence = sequence
            goal_handle.publish_feedback(feedback)
            time.sleep(_FIBONACCI_STEP_S)
        goal_handle.succeed()
        _LOG.info("goal succeeded with %d terms", len(sequence))
        result = Fibonacci.Result()
        result.sequence = sequence
        return result

    ActionServer(
        node,
        Fibonacci,
        ACTION,
        execute_callback=execute,
        goal_callback=lambda _request: GoalResponse.ACCEPT,
        cancel_callback=lambda _goal: CancelResponse.ACCEPT,
        callback_group=ReentrantCallbackGroup(),
    )

    executor = rclpy.executors.MultiThreadedExecutor(context=context)
    executor.add_node(node)
    spin = threading.Thread(target=executor.spin, daemon=True)
    spin.start()

    _LOG.info(
        "demo up: %s (state), %s (2 Hz), %s (service), %s (action)",
        STATE_TOPIC,
        CHATTER_TOPIC,
        TRIGGER_SERVICE,
        ACTION,
    )
    _LOG.info("now run: ros2-http-gateway examples/demo.yaml")

    message = String()
    counter = 0
    try:
        while True:
            message.data = state.value
            state_publisher.publish(message)
            counter += 1
            chatter = String()
            chatter.data = f"hello {counter}"
            chatter_publisher.publish(chatter)
            time.sleep(_CHATTER_PERIOD_S)
            state.tick(_CHATTER_PERIOD_S)
    except KeyboardInterrupt:
        _LOG.info("stopping")
    finally:
        executor.shutdown()
        spin.join(timeout=5)
        node.destroy_node()
        context.try_shutdown()
    return 0


class _State:
    """Cycles IDLE -> RUNNING -> IDLE, so a state precondition visibly opens and closes."""

    __slots__ = ("_elapsed", "value")

    def __init__(self) -> None:
        self.value = "IDLE"
        self._elapsed = 0.0

    def tick(self, seconds: float) -> None:
        self._elapsed += seconds
        if self._elapsed >= _STATE_HOLD_S:
            self._elapsed = 0.0
            self.value = "RUNNING" if self.value == "IDLE" else "IDLE"
            _LOG.info("state -> %s", self.value)


if __name__ == "__main__":
    raise SystemExit(main())
