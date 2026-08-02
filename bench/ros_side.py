#!/usr/bin/env python3
"""The ROS half of a benchmark, in its own process.

Kept out of the driver so a scenario that only pushes HTTP is never slowed by an rclpy executor
sharing its interpreter. Prints one line, ``ready``, once its entities are up.

Usage:
    python3 bench/ros_side.py talker [rate_hz] [payload_bytes]
    python3 bench/ros_side.py service
    python3 bench/ros_side.py talker service
"""

from __future__ import annotations

import sys
import threading
import time

import rclpy
from example_interfaces.msg import String
from rclpy.callback_groups import ReentrantCallbackGroup
from std_srvs.srv import Trigger

STATE_TOPIC = "/bench_state"
TRIGGER_SERVICE = "/bench_trigger"


def main(argv: list[str]) -> int:
    roles = {argument for argument in argv if not argument.isdigit()}
    numbers = [int(argument) for argument in argv if argument.isdigit()]
    rate_hz = float(numbers[0]) if numbers else 200.0
    # A payload big enough to matter: demonstrating a slow consumer means overflowing the kernel's
    # socket buffers, which a handful of bytes per message never will.
    payload = numbers[1] if len(numbers) > 1 else 0

    context = rclpy.context.Context()
    context.init()
    node = rclpy.create_node("bench_ros_side", context=context)

    if "service" in roles:
        node.create_service(
            Trigger, TRIGGER_SERVICE, _answer, callback_group=ReentrantCallbackGroup()
        )

    executor = rclpy.executors.MultiThreadedExecutor(context=context)
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()

    publisher = node.create_publisher(String, STATE_TOPIC, 10) if "talker" in roles else None
    print("ready", flush=True)

    try:
        if publisher is None:
            threading.Event().wait()
        message = String()
        message.data = "RUNNING" if not payload else "R" * payload
        period = 1.0 / rate_hz
        while True:
            publisher.publish(message)
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        context.try_shutdown()
    return 0


def _answer(_request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
    response.success = True
    response.message = "ok"
    return response


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
