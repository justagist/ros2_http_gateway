"""Command line entry point.

``--help`` and ``--version`` work without a ROS 2 environment; anything that touches the
graph checks for ``rclpy`` first and exits with a single actionable message.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ._version import __version__
from .ros_env import RosEnvironmentError, require_rclpy

if TYPE_CHECKING:
    from .resolve import ResolvedConfig

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_EPILOG = """\
examples:
  ros2-http-gateway config.yaml                  serve the gateway
  ros2-http-gateway --check config.yaml          validate and exit; no ROS graph needed
                                                 when every endpoint pins its `type:`
  ros2-http-gateway --check --online config.yaml also check pinned types against the
                                                 live graph, on the robot
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        A parser covering every documented flag. Kept free of ROS imports so that
        ``--help`` works on a plain Python installation.
    """
    parser = argparse.ArgumentParser(
        prog="ros2-http-gateway",
        description="Config-driven, authenticated HTTP + SSE gateway for ROS 2.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", nargs="?", help="path to the gateway YAML config file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config, including graph type resolution, then exit 0/1",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="with --check, also verify pinned `type:` values against the live ROS graph "
        "(a deploy-time smoke test; the default check is hermetic)",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how long to wait for the ROS graph while resolving types (default: 5)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("debug", "info", "warning", "error"),
        help="gateway log verbosity (default: info)",
    )
    parser.add_argument("--version", action="version", version=f"ros2-http-gateway {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Argument vector excluding the program name. Defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 1 on any handled failure, 2 on usage error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=_LOG_FORMAT, stream=sys.stderr)

    if args.config is None:
        parser.error("a config file is required")
    if args.online and not args.check:
        parser.error("--online only applies to --check; serving always uses the live graph")

    try:
        require_rclpy()
    except RosEnvironmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Imported here, not at module scope, so --help and --version stay ROS-free.
    from .config import ConfigError, load_config
    from .resolve import resolve

    try:
        config = load_config(args.config)
        resolved = resolve(
            config,
            discovery_timeout=args.discovery_timeout,
            verify_online=args.online,
        )
    except ConfigError as exc:
        print(f"error: {exc.format()}", file=sys.stderr)
        return 1

    if args.check:
        _report_check(resolved, online=args.online)
        return 0

    # The serve path lands in phase 2.
    print(
        "error: the HTTP server lands in phase 2; use --check to validate a config today.",
        file=sys.stderr,
    )
    return 1


def _report_check(resolved: ResolvedConfig, *, online: bool = False) -> None:
    """Print a one-line-per-endpoint summary of a successful validation."""
    config = resolved.config
    print(f"{config.source}: ok{' (verified against the live graph)' if online else ''}")
    for path in sorted(resolved.endpoints):
        endpoint = resolved.endpoints[path]
        roles = ",".join(sorted(endpoint.config.roles))
        base = config.http.base_path
        print(
            f"  {endpoint.config.method:4s} {base}{path:32s} "
            f"{endpoint.config.kind:7s} {endpoint.ros_name} [{endpoint.type_name}] roles={roles}"
        )
    if config.sse is not None:
        streamed = ", ".join(config.sse.include)
        print(f"  SSE  {config.http.base_path}{config.sse.path} -> {streamed}")
    print(
        f"  {len(config.tokens)} token(s), executor={config.ros.executor}, "
        f"audit={config.audit.sink}, int64_as_string={config.json.int64_as_string}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
