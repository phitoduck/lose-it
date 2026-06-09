"""Unit tests for the JWT / cookie inspection helpers in ``client.auth``.

These cover the building blocks ``lose-it login`` uses to populate the
YAML config so it doesn't need ``LOSEIT_USER_ID`` / ``LOSEIT_USER_NAME``
env vars: decoding the JWT payload, picking out the ``sub`` claim, and
scavenging an email/username from the browser's other ``loseit.com``
cookies.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from lose_it_utils.client.auth import (
    decode_jwt_exp,
    decode_jwt_payload,
    extract_user_info_from_jwt,
    extract_user_name_from_cookies,
)


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build a syntactically valid JWT with arbitrary payload claims.

    The header and signature are fake — the helpers under test inspect the
    payload only and don't verify the signature.
    """
    header = {"alg": "ES384", "typ": "JWT", "kid": "TESTKID"}

    def b64(d: dict[str, Any]) -> str:
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64(header)}.{b64(payload)}.fake-signature"


# ── decode_jwt_payload ──────────────────────────────────────────────────────


def test_decode_jwt_payload_returns_claims() -> None:
    jwt = _make_jwt({"sub": "12345678", "exp": 1700000000, "email": "alice@example.com"})
    payload = decode_jwt_payload(jwt)
    assert payload == {"sub": "12345678", "exp": 1700000000, "email": "alice@example.com"}


def test_decode_jwt_payload_handles_malformed() -> None:
    assert decode_jwt_payload("not-a-jwt") is None
    assert decode_jwt_payload("") is None
    assert decode_jwt_payload("only.one") is None


def test_decode_jwt_exp_still_works() -> None:
    """The pre-existing ``decode_jwt_exp`` API must not regress."""
    jwt = _make_jwt({"sub": "1", "exp": 1700000000})
    assert decode_jwt_exp(jwt) == 1700000000
    assert decode_jwt_exp("garbage") is None


# ── extract_user_info_from_jwt ──────────────────────────────────────────────


def test_extract_user_info_prefers_sub_for_user_id() -> None:
    jwt = _make_jwt({"sub": "9999", "email": "bob@example.com"})
    info = extract_user_info_from_jwt(jwt)
    assert info["user_id"] == "9999"
    assert info["user_name"] == "bob@example.com"


def test_extract_user_info_falls_back_through_username_claims() -> None:
    """If no ``email`` claim, the next claim in the preference list wins."""
    jwt = _make_jwt({"sub": "1", "preferred_username": "alice"})
    info = extract_user_info_from_jwt(jwt)
    assert info["user_name"] == "alice"


def test_extract_user_info_returns_empty_when_no_claims_match() -> None:
    jwt = _make_jwt({"foo": "bar"})
    info = extract_user_info_from_jwt(jwt)
    assert info == {}  # no user_id, no user_name found


def test_extract_user_info_coerces_numeric_user_id() -> None:
    """Some providers ship ``sub`` as a JSON number; we want a string."""
    jwt = _make_jwt({"sub": 12345678})
    info = extract_user_info_from_jwt(jwt)
    assert info["user_id"] == "12345678"
    assert isinstance(info["user_id"], str)


# ── extract_user_name_from_cookies ──────────────────────────────────────────


def test_extract_user_name_from_cookies_prefers_known_names() -> None:
    cookies = {"loseit_email": "alice@example.com", "random_tracker": "abc"}
    assert extract_user_name_from_cookies(cookies) == "alice@example.com"


def test_extract_user_name_from_cookies_rejects_jwt_shaped_values() -> None:
    """A cookie literally named ``email`` may carry a JWT — don't trust it.

    Real JWTs are three dot-separated chunks, each typically ≥ 16 chars; the
    heuristic in ``extract_user_name_from_cookies`` rejects anything with
    that shape rather than persisting the whole token as a username. The
    fixture below is intentionally not a real base64-JWT prefix so the
    secret scanner doesn't flag this test file — the heuristic only cares
    about the dot/length shape, not the content.
    """
    three_segment_shaped = "headerseg.payloadseg.signatureseg"
    cookies = {"email": three_segment_shaped}
    assert extract_user_name_from_cookies(cookies) is None


def test_extract_user_name_from_cookies_rejects_values_with_spaces() -> None:
    cookies = {"username": "Alice From Wonderland"}
    assert extract_user_name_from_cookies(cookies) is None


def test_extract_user_name_from_cookies_returns_none_when_nothing_plausible() -> None:
    cookies = {"session": "deadbeef", "ga": "GA1.2.123"}
    assert extract_user_name_from_cookies(cookies) is None
