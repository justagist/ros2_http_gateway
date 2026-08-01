"""``request_map``: filling service request fields from path parameters.

A path parameter is always text. Request bodies, on the other hand, are read strictly -- a
JSON number has to be a number -- so the substituted text is coerced against the field it
targets *before* the body converter sees it. Doing it here rather than by loosening every
scalar is what lets the generated JSON Schemas stay honest: they never have to tell a client
that any number anywhere might arrive as a string.

The coercion for each target is chosen once, at startup, from the field's declared type. By
request time this is a substitution and a call.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from rosidl_parser.definition import (
    AbstractGenericString,
    AbstractNestedType,
    BasicType,
    NamespacedType,
)

__all__ = [
    "ParamBinding",
    "UnsupportedTargetError",
    "apply_bindings",
    "coercer_for",
    "template_parameters",
]

_PARAM_RE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TRUE: Final = frozenset({"true", "1", "yes", "on"})
_FALSE: Final = frozenset({"false", "0", "no", "off"})

# int() and float() accept more than a URL segment should be allowed to mean: "1_000" becomes
# 1000, full-width Unicode digits parse, and float() returns NaN for "nan" and infinity for
# "inf". None of it is unsafe, since the body converter still range-checks, but /math/1_0/plus/2
# silently meaning 10 is a surprise and a NaN arriving from a URL is worse.
_INTEGER_RE: Final = re.compile(r"^[+-]?[0-9]+$")
_NUMBER_RE: Final = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


class UnsupportedTargetError(TypeError):
    """A ``request_map`` entry targets a field that text cannot sensibly fill."""


@dataclass(frozen=True, slots=True)
class ParamBinding:
    """One ``request_map`` entry, resolved and ready to apply."""

    target: tuple[str, ...]
    template: str
    coerce: Callable[[str], Any]
    label: str


def template_parameters(template: str) -> tuple[str, ...]:
    """Return the path parameter names a template refers to."""
    return tuple(_PARAM_RE.findall(template))


def coercer_for(slot: Any, label: str) -> Callable[[str], Any]:
    """Build the text-to-value conversion for a request field.

    Args:
        slot: The field's ``rosidl_parser.definition`` type.
        label: Dotted field path, used in error messages.

    Returns:
        A callable turning the substituted text into a value the body converter accepts.

    Raises:
        UnsupportedTargetError: If the field is a message, a collection, or a type that text
            cannot fill unambiguously. Failing at startup beats a confusing 400 later.
    """
    if isinstance(slot, AbstractGenericString):
        return lambda text: text
    if isinstance(slot, NamespacedType):
        raise UnsupportedTargetError(
            f"{label} is a message; request_map can only fill a single scalar field, so "
            "target its members individually"
        )
    if isinstance(slot, AbstractNestedType):
        raise UnsupportedTargetError(
            f"{label} is an array; request_map fills one scalar field, not a collection"
        )
    if not isinstance(slot, BasicType):
        raise UnsupportedTargetError(f"{label} has a type request_map cannot fill from text")

    name = slot.typename
    if name == "boolean":
        return _to_bool_text
    if name in ("float", "double"):
        return _to_float_text
    if name == "octet":
        return _to_int_text
    return _to_int_text


def _to_bool_text(text: str) -> bool:
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"expected a boolean, got {text!r}")


def _to_int_text(text: str) -> int:
    if not _INTEGER_RE.match(text):
        raise ValueError(f"expected an integer, got {text!r}")
    return int(text)


def _to_float_text(text: str) -> float:
    if not _NUMBER_RE.match(text):
        raise ValueError(f"expected a number, got {text!r}")
    return float(text)


def apply_bindings(
    bindings: Iterable[ParamBinding], body: dict[str, Any], params: Mapping[str, str]
) -> dict[str, Any]:
    """Fill mapped fields into a request body from the path parameters.

    Mutates and returns ``body``: a ``request_map`` entry takes precedence over the same field
    given in the body, because the URL is the more specific statement of intent.

    Args:
        bindings: The endpoint's resolved bindings.
        body: The decoded request body, which may be empty.
        params: Path parameters as matched by the router.

    Returns:
        The same dict, with mapped fields set.

    Raises:
        ValueError: If a substituted value will not convert to the target field's type.
    """
    for binding in bindings:
        # Raises rather than substituting "": resolve has already checked every template
        # parameter is a path parameter and the router supplies all of them, so this cannot
        # trigger. If that invariant ever breaks a 500 is the honest answer, not a field
        # quietly filled with the coercion of an empty string.
        text = _PARAM_RE.sub(lambda match: params[match.group(1)], binding.template)
        try:
            value = binding.coerce(text)
        except ValueError as exc:
            raise ValueError(f"{binding.label}: {exc}") from None
        target = body
        for part in binding.target[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                nested = {}
                target[part] = nested
            target = nested
        target[binding.target[-1]] = value
    return body
