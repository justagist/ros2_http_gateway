"""Generated ROS message converters.

For every configured interface type this module walks the field tree once, at startup,
and emits Python source for a dedicated converter, which is then ``compile()``d into a
closure. Runtime conversion is therefore straight attribute access and literal dict
construction, with no introspection, no ``getattr`` chains and no per-field dispatch.

The generated source is deliberately ugly, and verified by golden-file and round-trip tests
rather than by reading it; it is exposed through :func:`msg_to_dict_source` and
:func:`dict_to_msg_source`.

What the JSON encoding looks like, and why:

* ``uint8[]``, ``octet[]`` and their sequence forms become base64 strings.
* A scalar ``octet`` becomes an integer 0-255. rclpy represents it as a one-byte
  ``bytes``, which is not JSON, and a one-character base64 string would be absurd.
* ``int64``/``uint64`` become JSON strings when ``int64_as_string`` is set (the default),
  because values above 2^53 do not survive a JavaScript ``number``. Collections of them
  become arrays of strings.
* Fixed-size arrays of numerics stay as the numpy arrays rclpy hands over, for orjson's
  ``OPT_SERIALIZE_NUMPY``. Sequences arrive as ``array.array``, which orjson cannot serialise,
  so they become a list or a zero-copy numpy view, whichever is faster at that length -- see
  ``_VIEW_FROM``.
* ``float32`` sequences always take the numpy view, because the two paths disagree:
  ``array('f').tolist()`` widens to double and prints ``1.8700000047683716`` where numpy prints
  ``1.87``. Formatting must not depend on sequence length, and fixed-size ``float32[N]`` already
  emits the numpy form.
* ``float``/``double`` NaN and infinities become JSON ``null``, which is what orjson
  does; ROS has no other way to spell them in JSON. On the way in ``null`` reads back as
  NaN, so a body this gateway produced is always a body it accepts. Not lossless in that
  direction: an infinity read as ``null`` returns as NaN.

Reading a body is otherwise strict: a JSON number must be a number, a boolean a boolean. The
one exception is ``int64``/``uint64``, which accept a string because ``int64_as_string`` emits
one. Path parameters are coerced against their target field before reaching a converter, so the
generated schemas never have to advertise string-for-number.

Measured on Jazzy: rclpy gives numpy arrays for fixed-size numeric arrays, ``array.array`` for
numeric sequences, ``list`` for boolean, string, octet and message collections, and a one-byte
``bytes`` for a scalar ``octet``. The generated setters validate nothing in a released build
(asserts compiled out), so dict-to-message validates and coerces here.
"""

from __future__ import annotations

import array
import base64
from collections.abc import Callable, Iterator
from typing import Any, Final, TypeAlias

import numpy
from rosidl_parser.definition import (
    AbstractGenericString,
    AbstractNestedType,
    AbstractSequence,
    Array,
    BasicType,
    BoundedSequence,
    NamespacedType,
)
from rosidl_runtime_py.utilities import get_message

__all__ = [
    "ConversionError",
    "MessageClass",
    "UnsupportedFieldError",
    "build_dict_to_msg",
    "build_msg_to_dict",
    "dict_to_msg_source",
    "iter_fields",
    "msg_to_dict_source",
    "type_name_of",
]


# rclpy ships no type information for its generated interface classes, so annotating
# them as `type` buys nothing but false attribute errors under --strict. The alias
# names the intent instead of pretending to a precision that is not available.
MessageClass: TypeAlias = Any


class ConversionError(ValueError):
    """A JSON value could not be turned into the ROS field it targets.

    The message begins with the field's dotted path, so it can go straight into a BAD_REQUEST
    envelope; the path is also on :attr:`field`.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class UnsupportedFieldError(TypeError):
    """A field kind has no JSON representation in this gateway."""


# IDL basic type -> (numpy dtype, array.array typecode, inclusive integer range).
# Typecodes match rclpy's generated code, verified against test_msgs.
_NUMERIC: Final[dict[str, tuple[str, str, tuple[int, int] | None]]] = {
    "float": ("float32", "f", None),
    "double": ("float64", "d", None),
    "int8": ("int8", "b", (-(2**7), 2**7 - 1)),
    "uint8": ("uint8", "B", (0, 2**8 - 1)),
    # `char` in a .msg becomes IDL uint8, pinned by a test. Raw-IDL `wchar` is absent on
    # purpose: 16 bits, unverified representation, so it is refused rather than guessed.
    "char": ("uint8", "B", (0, 2**8 - 1)),
    "int16": ("int16", "h", (-(2**15), 2**15 - 1)),
    "uint16": ("uint16", "H", (0, 2**16 - 1)),
    "int32": ("int32", "i", (-(2**31), 2**31 - 1)),
    "uint32": ("uint32", "I", (0, 2**32 - 1)),
    "int64": ("int64", "q", (-(2**63), 2**63 - 1)),
    "uint64": ("uint64", "Q", (0, 2**64 - 1)),
}
_WIDE_INTS: Final = frozenset({"int64", "uint64"})
_FLOATS: Final = frozenset({"float", "double"})
_BYTE_LIKE: Final = frozenset({"uint8", "char"})

# Length at or above which a zero-copy numpy view beats building a list: below it
# numpy.frombuffer's ~0.45us fixed cost dominates, above it the view wins (3.3x at 8192 int32s).
# Measured at 64 on numpy 2.5; nearer 100 on numpy 1.26, where this costs 0.12us in between.
# Output is byte-identical either way, so the threshold is a tuning choice, not a contract.
_VIEW_FROM: Final = 64


def iter_fields(msg_type: MessageClass) -> Iterator[tuple[str, Any]]:
    """Yield ``(field_name, rosidl_slot_type)`` pairs in declaration order."""
    names = msg_type.get_fields_and_field_types().keys()
    yield from zip(names, msg_type.SLOT_TYPES, strict=True)


def type_name_of(msg_type: MessageClass) -> str:
    """Return the ``pkg/msg/Name`` string, as it appears in a config file."""
    parts = msg_type.__module__.split(".")
    return f"{parts[0]}/{parts[1]}/{msg_type.__name__}"


def _nested_type(slot: NamespacedType) -> MessageClass:
    return get_message("/".join((*slot.namespaces, slot.name)))


# --------------------------------------------------------------------------------------
# message -> dict
# --------------------------------------------------------------------------------------


class _ToDictEmitter:
    """Emits the body of a single message-to-dict function.

    Nested messages are inlined, so a conversion is one Python call however deep the type. A
    nested struct is hoisted into a local so it is read once -- except inside a comprehension,
    where the hoist would escape the loop variable's scope.
    """

    def __init__(self, *, int64_as_string: bool) -> None:
        self.int64_as_string = int64_as_string
        self.lines: list[str] = []
        self._tmp = 0
        self._depth = 0
        self._seq = 0

    def struct(self, msg_type: MessageClass, accessor: str) -> str:
        """Return an expression building the dict for ``msg_type`` read from ``accessor``."""
        parts = [
            f"{name!r}: {self.field(slot, f'{accessor}.{name}')}"
            for name, slot in iter_fields(msg_type)
        ]
        return "{" + ", ".join(parts) + "}"

    def field(self, slot: Any, accessor: str) -> str:
        """Return an expression converting one field."""
        if isinstance(slot, BasicType):
            return self._basic(slot.typename, accessor)
        if isinstance(slot, AbstractGenericString):
            return accessor
        if isinstance(slot, NamespacedType):
            return self._nested(slot, accessor)
        if isinstance(slot, AbstractNestedType):
            return self._collection(slot, accessor)
        raise UnsupportedFieldError(f"no JSON encoding for field kind {type(slot).__name__}")

    def _nested(self, slot: NamespacedType, accessor: str) -> str:
        nested = _nested_type(slot)
        if self._depth:
            # Inside a comprehension: a hoisted local would reference a loop variable
            # that does not exist at function scope.
            return self.struct(nested, accessor)
        self._tmp += 1
        tmp = f"_t{self._tmp}"
        self.lines.append(f"    {tmp} = {accessor}")
        return self.struct(nested, tmp)

    def _basic(self, typename: str, accessor: str) -> str:
        if typename == "boolean":
            return accessor
        if typename == "octet":
            # rclpy hands over a one-byte bytes object; index it into 0-255.
            return f"{accessor}[0]"
        if typename in _WIDE_INTS and self.int64_as_string:
            return f"str({accessor})"
        if typename in _NUMERIC:
            return accessor
        raise UnsupportedFieldError(f"no JSON encoding for basic type {typename!r}")

    def _collection(self, slot: AbstractNestedType, accessor: str) -> str:
        value = slot.value_type
        is_sequence = isinstance(slot, AbstractSequence)

        if isinstance(value, NamespacedType):
            self._depth += 1
            loop = f"_i{self._depth}"
            item = self.struct(_nested_type(value), loop)
            self._depth -= 1
            return f"[{item} for {loop} in {accessor}]"

        if isinstance(value, AbstractGenericString):
            return accessor

        if not isinstance(value, BasicType):
            raise UnsupportedFieldError(f"no JSON encoding for nested {type(value).__name__}")

        name = value.typename
        if name == "octet":
            # A list of one-byte bytes objects, whatever the collection kind.
            return f"_b64(b''.join({accessor}))"
        if name in _BYTE_LIKE:
            # numpy array for a fixed size, array.array for a sequence; both have tobytes.
            return f"_b64({accessor}.tobytes())"
        if name == "boolean":
            return accessor
        if name in _WIDE_INTS and self.int64_as_string:
            # Every element needs a Python-level str() anyway, so a view buys nothing.
            return f"[str(_w) for _w in {accessor}.tolist()]"
        if name not in _NUMERIC:
            raise UnsupportedFieldError(f"no JSON encoding for basic type {name!r}")
        if not is_sequence:
            # Already a numpy array; orjson takes it as is.
            return accessor

        dtype = _NUMERIC[name][0]
        if name == "float":
            # Not a performance choice: see the module docstring on float32 formatting.
            return f"_np.frombuffer({accessor}, dtype=_np.float32)"
        self._seq += 1
        tmp = f"_s{self._seq}"
        return (
            f"(_np.frombuffer({tmp}, dtype=_np.{dtype})"
            f" if len({tmp} := {accessor}) >= {_VIEW_FROM} else {tmp}.tolist())"
        )


def msg_to_dict_source(msg_type: MessageClass, *, int64_as_string: bool = True) -> str:
    """Generate the source of a message-to-dict converter, defining ``def to_dict(_m):``."""
    emitter = _ToDictEmitter(int64_as_string=int64_as_string)
    body = emitter.struct(msg_type, "_m")
    return "\n".join(["def to_dict(_m):", *emitter.lines, f"    return {body}"]) + "\n"


def build_msg_to_dict(
    msg_type: MessageClass, *, int64_as_string: bool = True
) -> Callable[[Any], dict[str, Any]]:
    """Compile a converter turning a message instance into a JSON-ready dict.

    Not plain JSON: fixed-size numeric arrays stay numpy so orjson needs no copy. The generated
    source is available as ``__ros_source__``.
    """
    source = msg_to_dict_source(msg_type, int64_as_string=int64_as_string)
    namespace: dict[str, Any] = {"_b64": _b64, "_np": numpy}
    exec(compile(source, f"<to_dict {type_name_of(msg_type)}>", "exec"), namespace)
    converter: Callable[[Any], dict[str, Any]] = namespace["to_dict"]
    converter.__ros_source__ = source  # type: ignore[attr-defined]
    return converter


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------------------
# dict -> message
# --------------------------------------------------------------------------------------


class _ToMsgEmitter:
    """Emits one builder function per (message type, dotted path) reachable from the root.

    Delegates instead of inlining: request bodies are cold, and one function per type keeps
    collection handling to a single comprehension.

    Keyed by dotted path as well as type so error messages name the field as the client wrote it
    (``status[].values[].key``) while every label stays a compile-time constant. Costs one extra
    function when a type appears at two paths.
    """

    def __init__(self, *, int64_as_string: bool) -> None:
        self.int64_as_string = int64_as_string
        self.namespace: dict[str, Any] = {
            "_MISS": _MISS,
            "_to_bool": _to_bool,
            "_to_int": _to_int,
            "_to_wide_int": _to_wide_int,
            "_to_float": _to_float,
            "_to_str": _to_str,
            "_to_octet": _to_octet,
            "_octet_list": _octet_list,
            "_bytes_of": _bytes_of,
            "_check_len": _check_len,
            "_check_max": _check_max,
            "_np": numpy,
            "_arr": array.array,
            "ConversionError": ConversionError,
        }
        self.functions: list[str] = []
        self._builders: dict[str, str] = {}
        self._loop_depth = 0

    def builder_for(self, msg_type: MessageClass, prefix: str = "") -> str:
        """Return the builder for ``msg_type`` at dotted path ``prefix``, emitting it once."""
        key = f"{msg_type.__module__}.{msg_type.__qualname__}@{prefix}"
        existing = self._builders.get(key)
        if existing is not None:
            return existing

        index = len(self._builders)
        name = f"_build{index}"
        self._builders[key] = name
        type_ref = f"_T{index}"
        self.namespace[type_ref] = msg_type
        label = prefix or "body"
        field_kwarg = f", field={prefix!r}" if prefix else ""

        fields = list(iter_fields(msg_type))
        known = "{" + ", ".join(repr(field_name) for field_name, _ in fields) + "}"
        lines = [
            f"def {name}(_d):",
            f"    # {type_name_of(msg_type)} at {label}",
            "    if type(_d) is not dict:",
            f"        raise ConversionError({label + ': expected an object, got '!r}"
            f" + repr(_d){field_kwarg})",
        ]
        present = "_d.keys() - " + known if fields else "set(_d)"
        lines += [
            f"    _unknown = {present}",
            "    if _unknown:",
            f"        raise ConversionError({label + ': unknown field(s) '!r}"
            f" + ', '.join(sorted(_unknown)){field_kwarg})",
        ]
        lines.append(f"    _m = {type_ref}()")
        for field_name, slot in fields:
            where = f"{prefix}.{field_name}" if prefix else field_name
            lines += [
                f"    _v = _d.get({field_name!r}, _MISS)",
                "    if _v is not _MISS:",
                f"        _m.{field_name} = {self.value(slot, '_v', where)}",
            ]
        lines.append("    return _m")
        self.functions.append("\n".join(lines))
        return name

    def value(self, slot: Any, expr: str, where: str) -> str:
        """Return an expression coercing ``expr`` into the value for ``slot``."""
        if isinstance(slot, BasicType):
            return self._basic(slot.typename, expr, where)
        if isinstance(slot, AbstractGenericString):
            return f"_to_str({expr}, {where!r})"
        if isinstance(slot, NamespacedType):
            return f"{self.builder_for(_nested_type(slot), where)}({expr})"
        if isinstance(slot, AbstractNestedType):
            return self._collection(slot, expr, where)
        raise UnsupportedFieldError(f"no JSON decoding for field kind {type(slot).__name__}")

    def _basic(self, typename: str, expr: str, where: str) -> str:
        if typename == "boolean":
            return f"_to_bool({expr}, {where!r})"
        if typename == "octet":
            return f"_to_octet({expr}, {where!r})"
        if typename in _FLOATS:
            return f"_to_float({expr}, {where!r})"
        spec = _NUMERIC.get(typename)
        if spec is None or spec[2] is None:
            raise UnsupportedFieldError(f"no JSON decoding for basic type {typename!r}")
        low, high = spec[2]
        coercer = "_to_wide_int" if typename in _WIDE_INTS else "_to_int"
        return f"{coercer}({expr}, {low}, {high}, {where!r})"

    def _collection(self, slot: AbstractNestedType, expr: str, where: str) -> str:
        value = slot.value_type
        size, maximum = self._bounds(slot)
        guarded = self._guard(slot, expr, where)
        # Errors about the collection itself keep the field's path; errors about one element
        # get the [] suffix, so "name[]" and "status[].values[].key" read the same way.
        item = f"{where}[]"

        if isinstance(value, NamespacedType):
            builder = self.builder_for(_nested_type(value), f"{where}[]")
            return self._comprehension(f"{builder}({{item}})", guarded)

        if isinstance(value, AbstractGenericString):
            return self._comprehension(f"_to_str({{item}}, {item!r})", guarded)

        if not isinstance(value, BasicType):
            raise UnsupportedFieldError(f"no JSON decoding for nested {type(value).__name__}")

        name = value.typename
        raw = f"_bytes_of({expr}, {size}, {maximum}, {where!r})"
        if name == "octet":
            return f"_octet_list({expr}, {size}, {maximum}, {where!r})"
        if name in _BYTE_LIKE:
            if isinstance(slot, Array):
                # .copy() because frombuffer over bytes yields a read-only array, and
                # anything that later mutates the message in place would hit an opaque
                # "assignment destination is read-only". Cold path; one copy costs nothing.
                return f"_np.frombuffer({raw}, dtype=_np.uint8).copy()"
            return f"_arr('B', {raw})"
        if name == "boolean":
            return self._comprehension(f"_to_bool({{item}}, {item!r})", guarded)

        dtype, code, bounds = _NUMERIC[name]
        coercer = "_to_wide_int" if name in _WIDE_INTS else "_to_int"
        element = (
            f"_to_float({{item}}, {item!r})"
            if bounds is None
            else f"{coercer}({{item}}, {bounds[0]}, {bounds[1]}, {item!r})"
        )
        items = self._comprehension(element, guarded)
        if isinstance(slot, Array):
            return f"_np.array({items}, dtype=_np.{dtype})"
        return f"_arr({code!r}, {items})"

    def _comprehension(self, element_template: str, iterable: str) -> str:
        self._loop_depth += 1
        loop = f"_j{self._loop_depth}"
        element = element_template.format(item=loop)
        self._loop_depth -= 1
        return f"[{element} for {loop} in {iterable}]"

    def _bounds(self, slot: AbstractNestedType) -> tuple[str, str]:
        if isinstance(slot, Array):
            return str(slot.size), "None"
        if isinstance(slot, BoundedSequence):
            return "None", str(slot.maximum_size)
        return "None", "None"

    def _guard(self, slot: AbstractNestedType, expr: str, where: str) -> str:
        if isinstance(slot, Array):
            return f"_check_len({expr}, {slot.size}, {where!r})"
        if isinstance(slot, BoundedSequence):
            return f"_check_max({expr}, {slot.maximum_size}, {where!r})"
        return f"_check_list({expr}, {where!r})"


def _emit_dict_to_msg(
    msg_type: MessageClass, *, int64_as_string: bool
) -> tuple[str, dict[str, Any]]:
    emitter = _ToMsgEmitter(int64_as_string=int64_as_string)
    root = emitter.builder_for(msg_type)
    emitter.namespace["_check_list"] = _check_list
    source = "\n\n".join([*emitter.functions, f"def from_dict(_d):\n    return {root}(_d)\n"])
    return source, emitter.namespace


def dict_to_msg_source(msg_type: MessageClass, *, int64_as_string: bool = True) -> str:
    """Generate the source of a dict-to-message converter.

    Defines ``def from_dict(_d):`` plus one builder per message type.
    """
    return _emit_dict_to_msg(msg_type, int64_as_string=int64_as_string)[0]


def build_dict_to_msg(
    msg_type: MessageClass, *, int64_as_string: bool = True
) -> Callable[[Any], Any]:
    """Compile a converter turning a JSON object into a message instance.

    Absent fields keep the ROS default. Unknown fields are rejected, not ignored: silently
    dropping a misspelled field is how an operator ends up believing a command was applied.
    Raises :class:`ConversionError` on bad input; source available as ``__ros_source__``.
    """
    source, namespace = _emit_dict_to_msg(msg_type, int64_as_string=int64_as_string)
    exec(compile(source, f"<from_dict {type_name_of(msg_type)}>", "exec"), namespace)
    converter: Callable[[Any], Any] = namespace["from_dict"]
    converter.__ros_source__ = source  # type: ignore[attr-defined]
    return converter


class _Missing:
    """Sentinel separating "field absent" from "field present and null"."""

    __slots__ = ()


_MISS: Final = _Missing()


def _to_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConversionError(f"{where}: expected a boolean, got {value!r}", field=where)


def _to_int(value: Any, low: int, high: int, where: str) -> int:
    """Coerce a JSON integer. A string is not one, whatever it happens to contain."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConversionError(f"{where}: expected an integer, got {value!r}", field=where)
    if not low <= value <= high:
        raise ConversionError(f"{where}: {value} is outside [{low}, {high}]", field=where)
    return value


def _to_wide_int(value: Any, low: int, high: int, where: str) -> int:
    """Coerce a 64-bit integer, which the read side may have written as a string.

    The one scalar that accepts a string, and only because ``int64_as_string`` puts one
    there: a body read from this gateway must be a legal body to send back to it.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConversionError(f"{where}: expected an integer, got {value!r}", field=where)
    try:
        number = int(value)
    except ValueError:
        raise ConversionError(f"{where}: expected an integer, got {value!r}", field=where) from None
    if not low <= number <= high:
        raise ConversionError(f"{where}: {number} is outside [{low}, {high}]", field=where)
    return number


def _to_float(value: Any, where: str) -> float:
    """Coerce a JSON number, reading null as NaN.

    null is accepted because the read side emits it for NaN and the infinities, so
    rejecting it would mean a body this gateway produced could not be sent back to it. Not
    lossless in that direction: an infinity read as null returns as NaN.
    """
    if value is None:
        return float("nan")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConversionError(f"{where}: expected a number, got {value!r}", field=where)
    return float(value)


def _to_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConversionError(f"{where}: expected a string, got {value!r}", field=where)
    return value


def _to_octet(value: Any, where: str) -> bytes:
    return bytes((_to_int(value, 0, 255, where),))


def _bytes_of(value: Any, size: int | None, maximum: int | None, where: str) -> bytes:
    """Decode base64 or a list of integers into bytes, enforcing any declared bound."""
    if isinstance(value, str):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ConversionError(f"{where}: expected base64, got {value!r}", field=where) from None
    elif isinstance(value, list):
        item_where = f"{where}[]"
        raw = bytes(_to_int(item, 0, 255, item_where) for item in value)
    else:
        raise ConversionError(
            f"{where}: expected a base64 string or a list of integers, got {value!r}", field=where
        )
    if size is not None and len(raw) != size:
        raise ConversionError(
            f"{where}: expected exactly {size} bytes, got {len(raw)}", field=where
        )
    if maximum is not None and len(raw) > maximum:
        raise ConversionError(
            f"{where}: expected at most {maximum} bytes, got {len(raw)}", field=where
        )
    return raw


def _octet_list(value: Any, size: int | None, maximum: int | None, where: str) -> list[bytes]:
    """Build the list-of-one-byte-objects layout rclpy uses for octet collections."""
    return [bytes((byte,)) for byte in _bytes_of(value, size, maximum, where)]


def _check_list(value: Any, where: str) -> Any:
    if not isinstance(value, list):
        raise ConversionError(f"{where}: expected an array, got {value!r}", field=where)
    return value


def _check_len(value: Any, size: int, where: str) -> Any:
    if not isinstance(value, list):
        raise ConversionError(f"{where}: expected an array, got {value!r}", field=where)
    if len(value) != size:
        raise ConversionError(
            f"{where}: expected exactly {size} items, got {len(value)}", field=where
        )
    return value


def _check_max(value: Any, maximum: int, where: str) -> Any:
    if not isinstance(value, list):
        raise ConversionError(f"{where}: expected an array, got {value!r}", field=where)
    if len(value) > maximum:
        raise ConversionError(
            f"{where}: expected at most {maximum} items, got {len(value)}", field=where
        )
    return value
