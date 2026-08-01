"""JSON Schema generation for ROS interface types.

The schemas describe exactly what the generated converters in
:mod:`ros2_http_gateway.codegen` produce and accept, which is what makes the OpenAPI
document assembled in phase 6 trustworthy rather than decorative. If the two ever
disagree, the converters are right and this module is wrong.

Two directions, because they are genuinely different documents:

* ``read`` describes a response body. Every field is present, ``int64`` is a string, and
  a ``double`` may be ``null`` because NaN and the infinities have no JSON spelling.
* ``write`` describes a request body. Every field is optional (an omitted field keeps the
  ROS default), a ``double`` may be ``null`` (read back as NaN, which is what makes a
  response body a legal request body), 64-bit integers are accepted as either a number or a
  string because the read side emits a string, and byte collections are accepted as base64
  or as an array of integers.

Nothing else is widened. In particular a number is not accepted as a string: path
parameters, which are always strings, are coerced against their target field before a
converter sees them, so that this schema does not have to tell every client that any number
anywhere may arrive as text.

Repeated nested types are emitted once into ``$defs`` and referenced, so a type like
``test_msgs/MultiNested`` produces a readable document instead of a megabyte of nesting.
"""

from __future__ import annotations

from typing import Any, Final, Literal

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

from .codegen import MessageClass, UnsupportedFieldError, iter_fields, type_name_of

__all__ = ["Direction", "json_schema_for", "schema_ref_name"]

Direction = Literal["read", "write"]

_INT_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "int8": (-(2**7), 2**7 - 1),
    "uint8": (0, 2**8 - 1),
    # No wchar: see the note in codegen._NUMERIC. An unverified representation gets refused
    # rather than described.
    "char": (0, 2**8 - 1),
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint64": (0, 2**64 - 1),
}
_WIDE_INTS: Final = frozenset({"int64", "uint64"})
_BYTE_LIKE: Final = frozenset({"uint8", "char"})
_DIGITS_PATTERN: Final = r"^-?[0-9]+$"


def schema_ref_name(msg_type: MessageClass) -> str:
    """Return the ``$defs`` key used for a message type.

    Args:
        msg_type: A generated ROS message class.

    Returns:
        The type name with slashes replaced, e.g. ``geometry_msgs.msg.Twist``.
    """
    return type_name_of(msg_type).replace("/", ".")


def json_schema_for(
    msg_type: MessageClass, *, direction: Direction = "read", int64_as_string: bool = True
) -> dict[str, Any]:
    """Generate a self-contained JSON Schema for a message type.

    Args:
        msg_type: A generated ROS message class.
        direction: ``read`` for a response body, ``write`` for a request body.
        int64_as_string: Must match the converter setting for the same type.

    Returns:
        A JSON Schema document. Nested types appear under ``$defs``.

    Raises:
        UnsupportedFieldError: If the type contains a field kind with no JSON encoding.
    """
    builder = _Builder(direction=direction, int64_as_string=int64_as_string)
    root = builder.struct(msg_type)
    if builder.defs:
        root["$defs"] = builder.defs
    root["title"] = type_name_of(msg_type)
    return root


class _Builder:
    """Walks a type tree once, interning nested types into ``$defs``."""

    def __init__(self, *, direction: Direction, int64_as_string: bool) -> None:
        self.direction = direction
        self.int64_as_string = int64_as_string
        self.defs: dict[str, Any] = {}
        self._in_progress: set[str] = set()

    def struct(self, msg_type: MessageClass) -> dict[str, Any]:
        """Return the object schema for a message type."""
        properties = {name: self.field(slot) for name, slot in iter_fields(msg_type)}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if properties and self.direction == "read":
            # A response always carries every field, which lets a client skip presence checks.
            schema["required"] = list(properties)
        return schema

    def reference(self, msg_type: MessageClass) -> dict[str, Any]:
        """Intern a nested type into ``$defs`` and return a reference to it."""
        key = schema_ref_name(msg_type)
        if key not in self.defs and key not in self._in_progress:
            self._in_progress.add(key)
            self.defs[key] = self.struct(msg_type)
            self._in_progress.discard(key)
        return {"$ref": f"#/$defs/{key}"}

    def field(self, slot: Any) -> dict[str, Any]:
        """Return the schema for one field."""
        if isinstance(slot, BasicType):
            return self._basic(slot.typename)
        if isinstance(slot, AbstractGenericString):
            return self._string(slot)
        if isinstance(slot, NamespacedType):
            return self.reference(_nested(slot))
        if isinstance(slot, AbstractNestedType):
            return self._collection(slot)
        raise UnsupportedFieldError(f"no JSON schema for field kind {type(slot).__name__}")

    def _basic(self, typename: str) -> dict[str, Any]:
        if typename == "boolean":
            return {"type": "boolean"}
        if typename == "octet":
            return {"type": "integer", "minimum": 0, "maximum": 255}
        if typename in ("float", "double"):
            # Nullable in both directions: NaN and the infinities are written as null, and
            # null is read back as NaN, so a response body round-trips as a request body.
            return {
                "type": ["number", "null"],
                "format": "float" if typename == "float" else "double",
            }
        bounds = _INT_BOUNDS.get(typename)
        if bounds is None:
            raise UnsupportedFieldError(f"no JSON schema for basic type {typename!r}")
        low, high = bounds
        if typename in _WIDE_INTS and self.int64_as_string:
            if self.direction == "read":
                return {"type": "string", "pattern": _DIGITS_PATTERN}
            # A path parameter is always a string, so the write side takes both.
            return {"type": ["string", "integer"], "pattern": _DIGITS_PATTERN}
        return {"type": "integer", "minimum": low, "maximum": high}

    def _string(self, slot: AbstractGenericString) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string"}
        if isinstance(slot, BoundedString | BoundedWString):
            schema["maxLength"] = slot.maximum_size
        return schema

    def _collection(self, slot: AbstractNestedType) -> dict[str, Any]:
        value = slot.value_type
        if isinstance(value, BasicType) and (
            value.typename == "octet" or value.typename in _BYTE_LIKE
        ):
            return self._bytes(slot)

        items = self.field(value)
        schema: dict[str, Any] = {"type": "array", "items": items}
        if isinstance(slot, Array):
            schema["minItems"] = slot.size
            schema["maxItems"] = slot.size
        elif isinstance(slot, BoundedSequence):
            schema["maxItems"] = slot.maximum_size
        return schema

    def _bytes(self, slot: AbstractNestedType) -> dict[str, Any]:
        size = slot.size if isinstance(slot, Array) else None
        maximum = slot.maximum_size if isinstance(slot, BoundedSequence) else None
        base64_schema: dict[str, Any] = {"type": "string", "contentEncoding": "base64"}
        if size is not None:
            base64_schema["description"] = f"base64 of exactly {size} bytes"
        elif maximum is not None:
            base64_schema["description"] = f"base64 of at most {maximum} bytes"
        if self.direction == "read":
            return base64_schema

        array_schema: dict[str, Any] = {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 255},
        }
        if size is not None:
            array_schema["minItems"] = array_schema["maxItems"] = size
        elif maximum is not None:
            array_schema["maxItems"] = maximum
        return {"oneOf": [base64_schema, array_schema]}


def _nested(slot: NamespacedType) -> MessageClass:
    return get_message("/".join((*slot.namespaces, slot.name)))
