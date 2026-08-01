"""Authentication and role checks.

v1 ships static bearer tokens and nothing else. :class:`Authenticator` is a protocol rather
than a concrete base class so that a JWT or OIDC implementation can be dropped in later
without touching the request path: it only has to turn a presented credential into an
:class:`Identity`, or None.

Comparison is against the SHA-256 digest with :func:`hmac.compare_digest`, never ``==``. A
plain string compare short-circuits at the first differing byte, which leaks the stored
digest one byte at a time to anyone who can measure response latency.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from .config import TokenConfig

__all__ = ["Authenticator", "Denial", "Identity", "StaticTokenAuthenticator", "bearer_token"]

_BEARER_PREFIX: Final = "bearer "


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is making the request, and what they are allowed to do."""

    user: str
    roles: frozenset[str]

    def may(self, required: frozenset[str]) -> bool:
        """Whether this identity holds at least one of the roles an endpoint requires."""
        return not required.isdisjoint(self.roles)


class Denial(str):
    """Why a request was refused, in a form suitable for an audit record."""

    __slots__ = ()


NO_CREDENTIAL: Final = Denial("no bearer token")
BAD_CREDENTIAL: Final = Denial("unknown bearer token")
MISSING_ROLE: Final = Denial("token lacks a required role")


@runtime_checkable
class Authenticator(Protocol):
    """Turns a presented credential into an identity.

    The seam a future JWT or OIDC authenticator plugs into. Implementations must not raise
    for a bad credential; returning None is how failure is reported, so that the request
    path has exactly one shape.
    """

    def authenticate(self, presented: str | None) -> Identity | None:
        """Return the identity for ``presented``, or None if it is not valid."""
        ...


class StaticTokenAuthenticator:
    """Checks a bearer token against the digests in the config file.

    Args:
        tokens: The configured token identities. Only their digests are retained.
    """

    __slots__ = ("_identities",)

    def __init__(self, tokens: Iterable[TokenConfig]) -> None:
        self._identities: tuple[tuple[str, Identity], ...] = tuple(
            (token.sha256, Identity(user=token.user, roles=token.roles)) for token in tokens
        )

    def authenticate(self, presented: str | None) -> Identity | None:
        """Return the identity holding ``presented``, or None.

        Every configured digest is compared even after a match, so the time taken does not
        depend on which token was presented or where it sits in the file.

        Args:
            presented: The raw bearer token from the request, or None if absent.

        Returns:
            The matching identity, or None.
        """
        if not presented:
            return None

        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        found: Identity | None = None
        for known, identity in self._identities:
            if hmac.compare_digest(digest, known):
                found = identity
        return found


def bearer_token(header: str | None) -> str | None:
    """Extract the credential from an ``Authorization`` header value.

    Args:
        header: The raw header value, or None if the header is absent.

    Returns:
        The token, or None if the header is missing or not a Bearer credential.
    """
    if not header or not header.lower().startswith(_BEARER_PREFIX):
        return None
    token = header[len(_BEARER_PREFIX) :].strip()
    return token or None
