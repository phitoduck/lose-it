"""Unit tests for the JWT / cookie inspection helpers in ``client.auth``.

These cover the building blocks ``loseit login`` uses to populate the
YAML config so it doesn't need ``LOSEIT_USER_ID`` / ``LOSEIT_USER_NAME``
env vars: decoding the JWT payload, picking out the ``sub`` claim, and
scavenging an email/username from the browser's other ``loseit.com``
cookies.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from lose_it.core import auth as auth_module
from lose_it.core.auth import (
    _cookie_store_paths,
    decode_jwt_exp,
    decode_jwt_payload,
    extract_user_info_from_jwt,
    extract_user_name_from_cookies,
    list_browser_profiles,
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


# ── _cookie_store_paths: profile targeting ──────────────────────────────────


_FAKE_PROFILES = [
    "/Chrome/Default/Cookies",
    "/Chrome/Profile 2/Cookies",
    "/Chrome/Profile 10/Cookies",
]


@pytest.fixture
def _fake_chrome_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Chrome has several profiles so we can test path filtering.

    Forces the darwin glob branch and returns a fixed set of cookie-store
    paths regardless of the pattern, so the test is host-independent.
    """
    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(auth_module.glob, "glob", lambda _pat: list(_FAKE_PROFILES))


def test_cookie_store_paths_returns_all_profiles_by_default(
    _fake_chrome_profiles: None,
) -> None:
    paths = _cookie_store_paths("chrome")
    assert paths == _FAKE_PROFILES


def test_cookie_store_paths_filters_to_named_profile(
    _fake_chrome_profiles: None,
) -> None:
    # Only the matching profile dir survives — this is what collapses the
    # per-profile macOS Keychain prompt storm down to a single prompt.
    assert _cookie_store_paths("chrome", profile="Default") == ["/Chrome/Default/Cookies"]
    assert _cookie_store_paths("chrome", profile="Profile 10") == ["/Chrome/Profile 10/Cookies"]


def test_cookie_store_paths_unknown_profile_yields_nothing(
    _fake_chrome_profiles: None,
) -> None:
    assert _cookie_store_paths("chrome", profile="Profile 999") == []


# ── list_browser_profiles: filesystem-only enumeration (no Keychain) ────────


def test_list_browser_profiles_returns_directories_and_friendly_names(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-facing primitive: one entry per profile, friendly name when known.

    Simulates a real Chrome user-data root with two profiles, one of
    which has a friendly name in ``Local State`` and one of which
    doesn't. The key behavioural promise: this function reads
    filesystem only — no cookie decryption, no Keychain access.
    """
    user_data = tmp_path / "Chrome"
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Default" / "Cookies").write_text("")  # any non-empty placeholder is fine
    (user_data / "Profile 2").mkdir()
    (user_data / "Profile 2" / "Cookies").write_text("")
    (user_data / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Default": {"name": "Eric (Personal)"},
                        # Profile 2 intentionally absent — exercises the `None` branch.
                    }
                }
            }
        )
    )

    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        auth_module,
        "_COOKIE_GLOBS",
        {"darwin": {"chrome": (f"{user_data}/*/Cookies",)}},
    )
    monkeypatch.setattr(
        auth_module,
        "_USER_DATA_ROOTS",
        {"darwin": {"chrome": (str(user_data),)}},
    )

    profiles = list_browser_profiles("chrome")
    by_dir = {p["directory"]: p for p in profiles}

    assert set(by_dir) == {"Default", "Profile 2"}
    assert by_dir["Default"]["name"] == "Eric (Personal)"
    assert by_dir["Profile 2"]["name"] is None
    # cookie_store path round-trips for the caller (e.g. for debugging).
    assert by_dir["Default"]["cookie_store"].endswith("/Default/Cookies")


def test_list_browser_profiles_handles_missing_local_state(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``Local State`` is missing, profiles still enumerate (no friendly names)."""
    user_data = tmp_path / "Chrome"
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Default" / "Cookies").write_text("")

    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        auth_module,
        "_COOKIE_GLOBS",
        {"darwin": {"chrome": (f"{user_data}/*/Cookies",)}},
    )
    monkeypatch.setattr(
        auth_module,
        "_USER_DATA_ROOTS",
        {"darwin": {"chrome": (str(user_data),)}},
    )

    profiles = list_browser_profiles("chrome")
    assert [p["directory"] for p in profiles] == ["Default"]
    assert profiles[0]["name"] is None


def test_list_browser_profiles_rejects_unknown_browser() -> None:
    with pytest.raises(ValueError, match="Unsupported browser"):
        list_browser_profiles("safari")  # type: ignore[arg-type]


def test_list_browser_profiles_does_not_touch_cookie_store(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the listing primitive must not import browser_cookie3.

    Any decryption call here would trigger a macOS Keychain prompt —
    the entire point of `list-profiles` is that it avoids that. We
    sentinel the loader so a single call would explode the test.
    """
    user_data = tmp_path / "Chrome"
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Default" / "Cookies").write_text("")

    monkeypatch.setattr(auth_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        auth_module,
        "_COOKIE_GLOBS",
        {"darwin": {"chrome": (f"{user_data}/*/Cookies",)}},
    )
    monkeypatch.setattr(
        auth_module,
        "_USER_DATA_ROOTS",
        {"darwin": {"chrome": (str(user_data),)}},
    )

    def _explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("list_browser_profiles must not decrypt cookies")

    # ``load_cookies_from_browser`` is the only path into browser_cookie3 from
    # this module. If list_browser_profiles ever grows a call into it, this
    # test will catch the regression.
    monkeypatch.setattr(auth_module, "load_cookies_from_browser", _explode)

    list_browser_profiles("chrome")  # must not raise
