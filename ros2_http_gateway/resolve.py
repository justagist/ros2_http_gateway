"""Turn a validated config into resolved interface types and compiled converters.

This is the second half of the fail-fast doctrine: by the time :func:`resolve` returns,
every interface type is loaded, every converter is compiled, and every dotted precondition
field has been checked against a real message definition. Nothing about the shape of the
data is discovered after the port opens.

Needs a sourced ROS 2 environment. By default the ROS graph is only queried for endpoints
that omit ``type:``, which is what lets ``--check`` run hermetically in CI against a config
that pins its types. Pass ``verify_online=True`` (``--check --online``) to also check the
pinned ones against the live graph: a type string can be syntactically valid, importable
and still not be what the robot is actually publishing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from rosidl_parser.definition import AbstractGenericString, BasicType

from .codegen import MessageClass, build_dict_to_msg, build_msg_to_dict, iter_fields
from .config import ConfigError, EndpointConfig, GatewayConfig, Kind
from .params import ParamBinding, UnsupportedTargetError, coercer_for, template_parameters

_LOG = logging.getLogger(__name__)

_KIND_TO_NAMESPACE: Final[dict[Kind, str]] = {"topic": "msg", "service": "srv", "action": "action"}
_GRAPH_POLL_S: Final = 0.1

# Precondition states come from a config file, so they are strings. A field that cannot be
# compared with one as text is a config mistake worth catching at startup.
_COMPARABLE_BASICS: Final = frozenset(
    {
        "boolean",
        "int8",
        "uint8",
        "int16",
        "uint16",
        "int32",
        "uint32",
        "int64",
        "uint64",
        "char",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    """An endpoint with its interface type loaded and its converters compiled."""

    config: EndpointConfig
    type_name: str
    interface: MessageClass
    ros_name: str
    to_dict: Callable[[Any], dict[str, Any]] | None
    from_dict: Callable[[Any], Any] | None
    result_to_dict: Callable[[Any], dict[str, Any]] | None = None
    feedback_to_dict: Callable[[Any], dict[str, Any]] | None = None
    bindings: tuple[ParamBinding, ...] = ()

    @property
    def message_type(self) -> MessageClass:
        """The message class carried on the wire for a topic endpoint."""
        return self.interface


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """A config whose every interface reference has been resolved."""

    config: GatewayConfig
    endpoints: Mapping[str, ResolvedEndpoint]

    @property
    def topic_reads(self) -> Mapping[str, ResolvedEndpoint]:
        """Endpoints backed by a cached topic sample, keyed by HTTP path."""
        return {
            path: endpoint
            for path, endpoint in self.endpoints.items()
            if endpoint.config.is_topic_read
        }


def resolve(
    config: GatewayConfig,
    *,
    discovery_timeout: float = 5.0,
    node: Any | None = None,
    verify_online: bool = False,
) -> ResolvedConfig:
    """Resolve interface types and compile every converter.

    Args:
        config: A validated configuration.
        discovery_timeout: Seconds to wait for the ROS graph while resolving omitted
            types. Ignored when every endpoint pins its ``type:`` and ``verify_online`` is
            not set.
        node: An existing rclpy node to query the graph with. When None and graph access
            is needed, a short-lived context and node are created and torn down.
        verify_online: Also check pinned ``type:`` values against the live graph, and
            report an endpoint whose ROS name is absent from it. Turns a hermetic check
            into a deploy-time smoke test on the real robot.

    Returns:
        The resolved configuration.

    Raises:
        ConfigError: If any type is unresolvable, ambiguous, mismatched with its endpoint
            kind, contradicts the live graph under ``verify_online``, or if a precondition
            names a field that does not exist. Every problem is reported at once.
    """
    problems: list[str] = []
    discovered = _discover_types(config, discovery_timeout, node, problems, verify_online)

    endpoints: dict[str, ResolvedEndpoint] = {}
    for path, endpoint in config.endpoints.items():
        type_name = endpoint.type_name or discovered.get(path)
        if type_name is None:
            continue
        resolved = _load(endpoint, type_name, config, problems)
        if resolved is not None:
            endpoints[path] = resolved

    if verify_online:
        _verify_pinned_types(config, discovered, problems)
    _check_precondition_fields(config, endpoints, problems)

    if problems:
        raise ConfigError(config.source, problems)
    return ResolvedConfig(config=config, endpoints=endpoints)


def _verify_pinned_types(
    config: GatewayConfig, observed: Mapping[str, str], problems: list[str]
) -> None:
    """Compare each pinned ``type:`` with what the graph actually advertises."""
    for path, endpoint in config.endpoints.items():
        if endpoint.type_name is None:
            continue
        actual = observed.get(path)
        if actual is None:
            # _poll_graph already reported absence and ambiguity for this endpoint.
            continue
        if actual != endpoint.type_name:
            problems.append(
                f"endpoints.{path}: configured as {endpoint.type_name}, but "
                f"{_absolute(endpoint.ros_name)} on the graph is {actual}"
            )


def _discover_types(
    config: GatewayConfig,
    discovery_timeout: float,
    node: Any | None,
    problems: list[str],
    verify_online: bool = False,
) -> dict[str, str]:
    """Resolve types from the graph, returning HTTP path -> observed type name.

    Only endpoints that omit ``type:`` are looked up unless ``verify_online`` is set, in
    which case every endpoint is, so pinned types can be compared with reality.
    """
    pending = (
        tuple(config.endpoints.values())
        if verify_online
        else config.endpoints_needing_type_resolution()
    )
    if not pending:
        return {}

    if node is not None:
        return _poll_graph(node, pending, discovery_timeout, problems)

    import rclpy

    context = rclpy.context.Context()
    context.init()
    try:
        temporary = rclpy.create_node(
            "ros2_http_gateway_discovery", context=context, start_parameter_services=False
        )
        try:
            return _poll_graph(temporary, pending, discovery_timeout, problems)
        finally:
            temporary.destroy_node()
    finally:
        context.try_shutdown()


def _poll_graph(
    node: Any,
    pending: Sequence[EndpointConfig],
    discovery_timeout: float,
    problems: list[str],
) -> dict[str, str]:
    from rclpy.action import get_action_names_and_types

    # Only the kinds actually being looked up, so a config of topics does not poll the
    # service and action graphs ten times a second for nothing.
    all_lookups: dict[Kind, Callable[[], list[tuple[str, list[str]]]]] = {
        "topic": node.get_topic_names_and_types,
        "service": node.get_service_names_and_types,
        "action": lambda: get_action_names_and_types(node=node),
    }
    pending_kinds = {endpoint.kind for endpoint in pending}
    lookups = {kind: lookup for kind, lookup in all_lookups.items() if kind in pending_kinds}
    wanted = {endpoint.path: _absolute(endpoint.ros_name) for endpoint in pending}
    _LOG.info(
        "querying the ROS graph for %d name(s) (timeout %.1fs): %s",
        len(wanted),
        discovery_timeout,
        ", ".join(sorted(wanted.values())),
    )

    deadline = time.monotonic() + discovery_timeout
    found: dict[str, list[str]] = {}
    while True:
        graphs = {kind: dict(lookup()) for kind, lookup in lookups.items()}
        found = {
            endpoint.path: graphs[endpoint.kind][wanted[endpoint.path]]
            for endpoint in pending
            if wanted[endpoint.path] in graphs[endpoint.kind]
        }
        if len(found) == len(pending) or time.monotonic() >= deadline:
            break
        time.sleep(_GRAPH_POLL_S)

    resolved: dict[str, str] = {}
    for endpoint in pending:
        candidates = found.get(endpoint.path)
        name = wanted[endpoint.path]
        where = f"endpoints.{endpoint.path}"
        if not candidates:
            hint = (
                "Raise --discovery-timeout, or start the other end first."
                if endpoint.type_name
                else "Add an explicit `type:` here, raise --discovery-timeout, or start "
                "the other end first."
            )
            problems.append(
                f"{where}: {endpoint.kind} {name} was not on the ROS graph after "
                f"{discovery_timeout:.1f}s. {hint}"
            )
        elif len(candidates) > 1:
            problems.append(
                f"{where}: {endpoint.kind} {name} advertises {len(candidates)} types "
                f"({', '.join(sorted(candidates))}); add `type:` to say which one to expose."
            )
        else:
            resolved[endpoint.path] = candidates[0]
            if endpoint.type_name is None:
                _LOG.info("%s: resolved %s to %s", where, name, candidates[0])
    return resolved


def _absolute(ros_name: str) -> str:
    return ros_name if ros_name.startswith("/") else f"/{ros_name}"


def _load(
    endpoint: EndpointConfig,
    type_name: str,
    config: GatewayConfig,
    problems: list[str],
) -> ResolvedEndpoint | None:
    where = f"endpoints.{endpoint.path}"
    expected = _KIND_TO_NAMESPACE[endpoint.kind]
    parts = type_name.split("/")
    if len(parts) != 3 or parts[1] != expected:
        problems.append(
            f"{where}: a {endpoint.kind} endpoint needs a <pkg>/{expected}/<Name> type, "
            f"got {type_name!r}"
        )
        return None

    interface = _import_interface(endpoint.kind, type_name, where, problems)
    if interface is None:
        return None

    wide = config.json.int64_as_string
    ros_name = _absolute(endpoint.ros_name)
    try:
        if endpoint.kind == "topic":
            return ResolvedEndpoint(
                config=endpoint,
                type_name=type_name,
                interface=interface,
                ros_name=ros_name,
                to_dict=build_msg_to_dict(interface, int64_as_string=wide)
                if not endpoint.is_topic_write
                else None,
                from_dict=build_dict_to_msg(interface, int64_as_string=wide)
                if endpoint.is_topic_write
                else None,
            )
        if endpoint.kind == "service":
            return ResolvedEndpoint(
                config=endpoint,
                type_name=type_name,
                interface=interface,
                ros_name=ros_name,
                to_dict=build_msg_to_dict(interface.Response, int64_as_string=wide),
                from_dict=build_dict_to_msg(interface.Request, int64_as_string=wide),
                bindings=_bindings(endpoint, interface.Request, where, problems),
            )
        return ResolvedEndpoint(
            config=endpoint,
            type_name=type_name,
            interface=interface,
            ros_name=ros_name,
            to_dict=None,
            from_dict=build_dict_to_msg(interface.Goal, int64_as_string=wide),
            result_to_dict=build_msg_to_dict(interface.Result, int64_as_string=wide),
            feedback_to_dict=build_msg_to_dict(interface.Feedback, int64_as_string=wide),
        )
    except (TypeError, ValueError) as exc:
        problems.append(f"{where}: cannot build a JSON converter for {type_name}: {exc}")
        return None


def _bindings(
    endpoint: EndpointConfig,
    request_type: MessageClass,
    where: str,
    problems: list[str],
) -> tuple[ParamBinding, ...]:
    """Resolve each request_map entry to the field it fills and how to fill it.

    Every failure here is a config mistake worth catching before the port opens: a target
    field that does not exist, or one that text cannot sensibly fill.
    """
    built: list[ParamBinding] = []
    for target, template in endpoint.request_map.items():
        parts = tuple(target.split("."))
        spot = f"{where}.request_map.{target}"
        slot = _slot_at(request_type, parts, spot, problems)
        if slot is None:
            continue
        try:
            coerce = coercer_for(slot, target)
        except UnsupportedTargetError as exc:
            problems.append(f"{spot}: {exc}")
            continue
        # The config engine has already checked the referenced parameters exist.
        assert all(name in endpoint.path_params for name in template_parameters(template))
        built.append(ParamBinding(target=parts, template=template, coerce=coerce, label=target))
    return tuple(built)


def _import_interface(
    kind: Kind, type_name: str, where: str, problems: list[str]
) -> MessageClass | None:
    from rosidl_runtime_py.utilities import get_action, get_message, get_service

    loaders: dict[Kind, Callable[[str], MessageClass]] = {
        "topic": get_message,
        "service": get_service,
        "action": get_action,
    }
    try:
        return loaders[kind](type_name)
    except (ImportError, AttributeError, ValueError, ModuleNotFoundError) as exc:
        problems.append(
            f"{where}: cannot load interface type {type_name!r} ({exc}). Is the package "
            "installed and sourced?"
        )
        return None


def _check_precondition_fields(
    config: GatewayConfig,
    endpoints: Mapping[str, ResolvedEndpoint],
    problems: list[str],
) -> None:
    """Walk each precondition's dotted field against the real message definition."""
    for path, endpoint in config.endpoints.items():
        for index, precondition in enumerate(endpoint.preconditions):
            spot = f"endpoints.{path}.preconditions[{index}].field"
            state = endpoints.get(precondition.state_endpoint)
            if state is None:
                continue  # the missing reference was already reported
            slot = _slot_at(state.message_type, precondition.field_path, spot, problems)
            if slot is None:
                continue
            if isinstance(slot, AbstractGenericString):
                continue
            if isinstance(slot, BasicType) and slot.typename in _COMPARABLE_BASICS:
                continue
            dotted = ".".join(precondition.field_path)
            problems.append(
                f"{spot}: {dotted} is a {type(slot).__name__}, which cannot be compared "
                "with the strings in `allow`; point at a string or integer field"
            )


def _slot_at(
    msg_type: MessageClass, field_path: Sequence[str], spot: str, problems: list[str]
) -> Any | None:
    from rosidl_parser.definition import NamespacedType
    from rosidl_runtime_py.utilities import get_message

    current: MessageClass = msg_type
    slot: Any = None
    for depth, part in enumerate(field_path):
        fields = dict(iter_fields(current))
        slot = fields.get(part)
        if slot is None:
            walked = ".".join(field_path[:depth]) or "the message"
            available = ", ".join(sorted(fields)) or "none"
            problems.append(
                f"{spot}: {walked} has no field {part!r} "
                f"(fields of {current.__name__}: {available})"
            )
            return None
        if depth + 1 < len(field_path):
            if not isinstance(slot, NamespacedType):
                walked = ".".join(field_path[: depth + 1])
                problems.append(f"{spot}: {walked} is not a message, so it has no subfields")
                return None
            current = get_message("/".join((*slot.namespaces, slot.name)))
    return slot
