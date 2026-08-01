"""Authentication tests. No ROS needed, which is the point of the seam."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from ros2_http_gateway.auth import (
    Authenticator,
    Identity,
    StaticTokenAuthenticator,
    bearer_token,
)
from ros2_http_gateway.config import TokenConfig


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make(*tokens: tuple[str, str, list[str]]) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator(
        TokenConfig(user=user, roles=frozenset(roles), sha256=digest(secret))
        for secret, user, roles in tokens
    )


# --------------------------------------------------------------------------------------
# header parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("BEARER abc123", "abc123"),
        ("Bearer   abc123  ", "abc123"),
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic abc123", None),
        ("abc123", None),
    ],
)
def test_bearer_token_parsing(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected


# --------------------------------------------------------------------------------------
# static tokens
# --------------------------------------------------------------------------------------


def test_correct_token_yields_its_identity() -> None:
    auth = make(("secret-one", "hmi", ["operator"]))
    identity = auth.authenticate("secret-one")
    assert identity == Identity(user="hmi", roles=frozenset({"operator"}))


@pytest.mark.parametrize("presented", [None, "", "wrong", "secret-on", "secret-onee"])
def test_wrong_or_absent_token_yields_nothing(presented: str | None) -> None:
    auth = make(("secret-one", "hmi", ["operator"]))
    assert auth.authenticate(presented) is None


def test_the_right_identity_is_picked_from_several() -> None:
    auth = make(
        ("token-a", "alice", ["viewer"]),
        ("token-b", "bob", ["operator"]),
        ("token-c", "carol", ["viewer", "operator"]),
    )
    assert auth.authenticate("token-b") == Identity("bob", frozenset({"operator"}))
    assert auth.authenticate("token-c") == Identity("carol", frozenset({"viewer", "operator"}))


def test_comparison_uses_compare_digest_and_does_not_stop_at_the_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two properties in one, both security-relevant.

    ``==`` on a digest short-circuits at the first differing byte and leaks the stored value
    to anyone timing the response, so the comparison must go through
    :func:`hmac.compare_digest`. And returning as soon as a token matches would make the
    response time depend on the matching token's position in the file, so every entry is
    compared regardless.
    """
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def counting(left: object, right: object) -> bool:
        assert isinstance(left, str) and isinstance(right, str)
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr("ros2_http_gateway.auth.hmac.compare_digest", counting)
    auth = make(
        ("token-a", "alice", ["viewer"]),
        ("token-b", "bob", ["operator"]),
        ("token-c", "carol", ["viewer"]),
    )
    assert auth.authenticate("token-a") == Identity("alice", frozenset({"viewer"}))
    assert len(calls) == 3, "the loop must not stop early on a match"


def test_the_plaintext_token_is_never_stored() -> None:
    auth = make(("super-secret-value", "hmi", ["operator"]))
    assert "super-secret-value" not in repr(auth._identities)


def test_a_token_is_hashed_before_comparison() -> None:
    """A digest-shaped credential must not be accepted as if it were the token."""
    auth = make(("secret-one", "hmi", ["operator"]))
    assert auth.authenticate(digest("secret-one")) is None


# --------------------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "required", "allowed"),
    [
        (["operator"], ["operator"], True),
        (["viewer", "operator"], ["operator"], True),
        (["operator"], ["viewer", "operator"], True),
        (["viewer"], ["operator"], False),
        ([], ["operator"], False),
        (["operator"], [], False),
    ],
)
def test_role_check(held: list[str], required: list[str], allowed: bool) -> None:
    identity = Identity("u", frozenset(held))
    assert identity.may(frozenset(required)) is allowed


def test_static_authenticator_satisfies_the_plugin_protocol() -> None:
    """The seam a JWT or OIDC implementation will plug into in v1.1."""
    assert isinstance(make(("t-token", "u", ["r"])), Authenticator)
