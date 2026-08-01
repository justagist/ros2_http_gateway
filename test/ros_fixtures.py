"""Helpers for tests that need a sourced ROS 2 environment.

Importing this module without ROS raises ``pytest.skip``, so a module that imports it is
skipped during collection rather than erroring.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

pytest.importorskip("rclpy", reason="needs a sourced ROS 2 environment")

from rosidl_parser.definition import (
    AbstractGenericString,
    AbstractNestedType,
    Array,
    BasicType,
    BoundedSequence,
    BoundedString,
    BoundedWString,
    NamespacedType,
)
from rosidl_runtime_py.utilities import get_message

# The upstream package built to cover every field kind, plus the everyday types a
# gateway actually carries.
EDGE_CASE_TYPES = [
    "test_msgs/msg/BasicTypes",
    "test_msgs/msg/Arrays",
    "test_msgs/msg/BoundedSequences",
    "test_msgs/msg/UnboundedSequences",
    "test_msgs/msg/Strings",
    "test_msgs/msg/WStrings",
    "test_msgs/msg/Nested",
    "test_msgs/msg/MultiNested",
    "test_msgs/msg/Builtins",
    "test_msgs/msg/Defaults",
    "test_msgs/msg/Constants",
    "test_msgs/msg/Empty",
]

EVERYDAY_TYPES = [
    "std_msgs/msg/String",
    "std_msgs/msg/Header",
    "std_msgs/msg/ByteMultiArray",
    "std_msgs/msg/Int64MultiArray",
    "builtin_interfaces/msg/Time",
    "builtin_interfaces/msg/Duration",
    "geometry_msgs/msg/Twist",
    "geometry_msgs/msg/PoseStamped",
    "geometry_msgs/msg/TransformStamped",
    "sensor_msgs/msg/Imu",
    "sensor_msgs/msg/JointState",
    "sensor_msgs/msg/Image",
    "diagnostic_msgs/msg/DiagnosticArray",
]

ALL_TYPES = EDGE_CASE_TYPES + EVERYDAY_TYPES

# Above 2^53, so a JSON number would lose it. Present in every int64 field the populator
# touches, which is what makes the int64_as_string tests meaningful.
BIG_INT64 = 2**53 + 1

_SEQUENCE_LENGTH = 3


def populate(msg_type: type, counter: itertools.count[int] | None = None) -> Any:
    """Fill every field of a message with distinctive, in-range values.

    Deliberately avoids NaN and infinities: they have no JSON spelling other than
    ``null``, so they cannot round-trip and are tested separately.

    Args:
        msg_type: A generated ROS message class.
        counter: Shared counter, so that no two fields in a tree get the same value.

    Returns:
        A populated instance of ``msg_type``.
    """
    counter = itertools.count(1) if counter is None else counter
    msg = msg_type()
    for name, slot in zip(
        msg_type.get_fields_and_field_types().keys(), msg_type.SLOT_TYPES, strict=True
    ):
        setattr(msg, name, _value_for(slot, counter))
    return msg


def assert_messages_equal(left: Any, right: Any, where: str = "") -> None:
    """Assert two messages are field-for-field equal.

    Replaces comparing ``serialize_message`` output, which is unsound: CDR inserts alignment
    padding between fields of differing widths, and rosidl does not initialise it. Two
    independently constructed default ``test_msgs/BasicTypes`` messages were measured
    differing at bytes 26-27 roughly once in two hundred attempts, purely from whatever the
    heap happened to contain. That produced a rare, baffling failure in a full suite run.

    Walking the declared fields is also stronger than comparing the JSON encodings, because it
    reads the messages themselves: a converter that lost precision in both directions would
    still be caught here, where two equal JSON documents would hide it.

    Args:
        left: A message instance.
        right: Another instance of the same type.
        where: Dotted path used in the assertion message.

    Raises:
        AssertionError: On the first field that differs, naming its path.
    """
    for name, slot in zip(
        type(left).get_fields_and_field_types().keys(), type(left).SLOT_TYPES, strict=True
    ):
        path = f"{where}.{name}" if where else name
        _assert_value_equal(getattr(left, name), getattr(right, name), slot, path)


def _assert_value_equal(left: Any, right: Any, slot: Any, path: str) -> None:
    if isinstance(slot, NamespacedType):
        assert_messages_equal(left, right, path)
        return

    if isinstance(slot, AbstractNestedType):
        element = slot.value_type
        as_list_left, as_list_right = _as_list(left), _as_list(right)
        assert len(as_list_left) == len(as_list_right), f"{path}: different lengths"
        for index, (one, two) in enumerate(zip(as_list_left, as_list_right, strict=True)):
            _assert_value_equal(one, two, element, f"{path}[{index}]")
        return

    assert left == right, f"{path}: {left!r} != {right!r}"


def _as_list(value: Any) -> list[Any]:
    """Flatten a collection field, whatever container rclpy chose for it."""
    if hasattr(value, "tolist"):
        listed = value.tolist()
        return list(listed) if isinstance(listed, list) else [listed]
    return list(value)


def declared_field_tree(msg_type: type) -> dict[str, Any]:
    """Return the declared field names of a type tree, nested by field.

    Used to check converter coverage independently of any byte comparison: a field that
    both converters silently skipped would otherwise be invisible.

    Args:
        msg_type: A generated ROS message class.

    Returns:
        A nested mapping of field name to either None or the subtree of a message field.
    """
    tree: dict[str, Any] = {}
    for name, slot in zip(
        msg_type.get_fields_and_field_types().keys(), msg_type.SLOT_TYPES, strict=True
    ):
        nested = slot.value_type if isinstance(slot, AbstractNestedType) else slot
        if isinstance(nested, NamespacedType):
            tree[name] = declared_field_tree(
                get_message("/".join((*nested.namespaces, nested.name)))
            )
        else:
            tree[name] = None
    return tree


def _value_for(slot: Any, counter: itertools.count[int]) -> Any:
    if isinstance(slot, BasicType):
        return _basic_value(slot.typename, counter)
    if isinstance(slot, AbstractGenericString):
        limit = slot.maximum_size if isinstance(slot, BoundedString | BoundedWString) else None
        text = f"value-{next(counter)}"
        if isinstance(slot, BoundedWString) or type(slot).__name__.endswith("WString"):
            text = f"wide-ö-{next(counter)}"
        return text[:limit] if limit else text
    if isinstance(slot, NamespacedType):
        return populate(get_message("/".join((*slot.namespaces, slot.name))), counter)
    if isinstance(slot, AbstractNestedType):
        length = (
            slot.size
            if isinstance(slot, Array)
            else min(_SEQUENCE_LENGTH, getattr(slot, "maximum_size", _SEQUENCE_LENGTH))
            if isinstance(slot, BoundedSequence)
            else _SEQUENCE_LENGTH
        )
        return [_value_for(slot.value_type, counter) for _ in range(length)]
    raise AssertionError(f"populator has no case for {type(slot).__name__}")


_INT_RANGES = {
    "int8": (-(2**7), 2**7 - 1),
    "uint8": (0, 2**8 - 1),
    "char": (0, 2**8 - 1),
    "wchar": (0, 2**16 - 1),
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint64": (0, 2**64 - 1),
}


def _basic_value(typename: str, counter: itertools.count[int]) -> Any:
    index = next(counter)
    if typename == "boolean":
        return index % 2 == 1
    if typename == "octet":
        return bytes((index % 256,))
    if typename in ("float", "double"):
        # Halves are exact in both float32 and float64, so JSON cannot lose them.
        return index + 0.5
    low, high = _INT_RANGES[typename]
    if typename in ("int64", "uint64"):
        return BIG_INT64
    return max(low, min(high, index * 7))
