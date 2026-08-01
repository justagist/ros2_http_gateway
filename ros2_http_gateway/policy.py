"""State preconditions: refusing a call because of what the robot is currently doing.

A precondition reads one field of another endpoint's latest cached sample and checks it against
an allow-list keyed by a path parameter. Resolution happens at startup, so evaluating one is a
dict lookup and a set membership test.

A gate that fails open is not a gate, so three cases refuse: an unlisted parameter value, no
sample yet, and a sample past the state endpoint's ``stale_after_ms``. The third means the gate
depends on that topic still being published.
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

    Allow-lists come from YAML, so comparison is textual. Booleans use the JSON spelling, so a
    config says ``[true]`` and not ``[True]``.
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
        """Return None when the call may proceed, otherwise the reason it may not."""
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

    Endpoints without preconditions are absent from ``checks``, so the common case is one dict
    lookup that finds nothing.
    """

    __slots__ = ("_checks",)

    def __init__(self, checks: Mapping[str, tuple[Check, ...]]) -> None:
        self._checks = dict(checks)

    def evaluate(self, path: str, params: Mapping[str, str]) -> str | None:
        """Evaluate every precondition on an endpoint, stopping at the first refusal."""
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

    Raises KeyError if a precondition names an endpoint with no cache; the config engine has
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
