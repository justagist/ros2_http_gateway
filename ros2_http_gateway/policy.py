"""State preconditions: refusing a call because of what the robot is currently doing.

A precondition reads one field of another endpoint's latest cached sample and checks it
against an allow-list keyed by a path parameter. Everything expensive is done at startup --
the endpoint reference is resolved to its cache, the dotted field path to a tuple, the
allow-list to frozensets -- so evaluating one is a dict lookup and a set membership test.

Three things are refused rather than waved through, because a precondition is a safety gate
and a gate that fails open is not a gate:

* a parameter value the allow-list does not mention;
* no sample yet, so the state is unknown;
* a sample older than the state endpoint's own ``stale_after_ms``. Deciding that it is safe
  to start because the robot was idle a minute ago is exactly the mistake this exists to
  prevent.

The last one is worth knowing about: a precondition on a topic with a staleness threshold
makes the gate depend on that topic still being published.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .cache import TopicCache
from .config import Precondition

__all__ = ["Check", "PolicyEngine", "state_text"]

_WILDCARD: Final = "*"


def state_text(value: Any) -> str:
    """Render a state field value the way an allow-list spells it.

    Allow-lists come from YAML and are therefore strings, so the field value has to be
    rendered as one. Booleans use the JSON spelling rather than Python's, so a config says
    ``[true]`` and not ``[True]``.

    Args:
        value: The extracted field value.

    Returns:
        The comparable text.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True, slots=True)
class Check:
    """One precondition, resolved against the cache it reads."""

    endpoint: str
    state_endpoint: str
    cache: TopicCache
    field_path: tuple[str, ...]
    key: str | None
    allow: Mapping[str, frozenset[str]]

    @property
    def field_label(self) -> str:
        """The dotted field path, for messages."""
        return ".".join(self.field_path)

    def permitted(self, param_value: str | None) -> frozenset[str] | None:
        """The states allowed for this parameter value, or None if it is not listed."""
        if param_value is not None:
            exact = self.allow.get(param_value)
            if exact is not None:
                return exact
        return self.allow.get(_WILDCARD)

    def evaluate(self, params: Mapping[str, str], now_monotonic: float) -> str | None:
        """Check this precondition.

        Args:
            params: Path parameters from the request.
            now_monotonic: Monotonic reading used to judge staleness.

        Returns:
            None when the call may proceed, otherwise the reason it may not.
        """
        param_value = params.get(self.key) if self.key else None
        allowed = self.permitted(param_value)
        if allowed is None:
            listed = ", ".join(sorted(self.allow)) or "nothing"
            return (
                f"{param_value!r} is not a permitted value here (allowed: {listed})"
                if param_value is not None
                else f"this endpoint permits no unconditional call (allowed: {listed})"
            )
        if _WILDCARD in allowed:
            return None

        sample = self.cache.latest
        if sample is None:
            return (
                f"{self.state_endpoint} has received nothing yet, so {self.field_label} is "
                "unknown and the call cannot be authorised"
            )
        if self.cache.is_stale(sample, now_monotonic):
            age_ms = (now_monotonic - sample.monotonic) * 1000.0
            return (
                f"{self.state_endpoint} is stale ({age_ms:.0f}ms old), so {self.field_label} "
                "cannot be trusted to authorise the call"
            )

        current = state_text(_extract(self.cache.decoded(sample), self.field_path))
        if current not in allowed:
            return (
                f"{self.field_label} is {current!r}, which does not permit this call "
                f"(allowed: {', '.join(sorted(allowed))})"
            )
        return None


def _extract(decoded: Mapping[str, Any], field_path: Sequence[str]) -> Any:
    value: Any = decoded
    for part in field_path:
        value = value[part]
    return value


class PolicyEngine:
    """Evaluates the preconditions configured for each endpoint.

    Args:
        checks: Endpoint key to its resolved checks. Endpoints without preconditions are
            absent, so the common case is one dict lookup that finds nothing.
    """

    __slots__ = ("_checks",)

    def __init__(self, checks: Mapping[str, tuple[Check, ...]]) -> None:
        self._checks = dict(checks)

    def __bool__(self) -> bool:
        return bool(self._checks)

    def checks_for(self, path: str) -> tuple[Check, ...]:
        """The checks configured for an endpoint, possibly empty."""
        return self._checks.get(path, ())

    def evaluate(self, path: str, params: Mapping[str, str]) -> str | None:
        """Evaluate every precondition on an endpoint, stopping at the first refusal.

        Args:
            path: The endpoint key.
            params: Path parameters from the request.

        Returns:
            None when the call may proceed, otherwise the reason it may not.
        """
        checks = self._checks.get(path)
        if not checks:
            return None
        now = time.monotonic()
        for check in checks:
            reason = check.evaluate(params, now)
            if reason is not None:
                return reason
        return None


def build_policy(
    preconditions: Mapping[str, Sequence[Precondition]], caches: Mapping[str, TopicCache]
) -> PolicyEngine:
    """Resolve configured preconditions against the live caches.

    Args:
        preconditions: Endpoint key to its configured preconditions.
        caches: Endpoint key to the cache backing it, for topic read endpoints.

    Returns:
        The engine.

    Raises:
        KeyError: If a precondition names an endpoint with no cache. The config engine has
            already rejected that, so reaching here means the two disagree.
    """
    checks: dict[str, tuple[Check, ...]] = {}
    for path, configured in preconditions.items():
        if not configured:
            continue
        checks[path] = tuple(
            Check(
                endpoint=path,
                state_endpoint=item.state_endpoint,
                cache=caches[item.state_endpoint],
                field_path=item.field_path,
                key=item.key,
                allow=item.allow,
            )
            for item in configured
        )
    return PolicyEngine(checks)
