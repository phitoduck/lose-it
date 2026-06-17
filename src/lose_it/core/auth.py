"""Token loading + browser-cookie import helpers.

The ``liauth`` JWT is the single credential the SDK needs. It can be sourced
from, in priority order when calling :func:`load_token`:

1. ``LOSEIT_TOKEN`` env var.
2. ``~/.config/loseit/token`` (plain text JWT, ``chmod 600`` recommended).

The ``refresh_token_from_*`` helpers decrypt a Chromium-family browser's
cookie store (via ``browser-cookie3``) so the JWT can be imported silently
from a live browser session instead of doing the every-2-weeks DevTools
copy/paste dance.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from .._logging import logger

DEFAULT_TOKEN_FILE = Path("~/.config/loseit/token").expanduser()
SIGNIN_URL = "https://www.loseit.com/"

BrowserName = Literal["chrome", "brave"]
SUPPORTED_BROWSERS: tuple[BrowserName, ...] = ("chrome", "brave")

# Profile-aware cookie-store globs. We iterate every match because the
# liauth cookie usually lives in a non-default profile (Chrome's user picker)
# and ``browser_cookie3.{chrome,brave}()`` only checks the default profile
# when called without ``cookie_file=``.
_COOKIE_GLOBS: dict[str, dict[BrowserName, tuple[str, ...]]] = {
    "darwin": {
        "chrome": ("~/Library/Application Support/Google/Chrome/*/Cookies",),
        "brave": (
            "~/Library/Application Support/BraveSoftware/Brave-Browser/*/Cookies",
            "~/Library/Application Support/BraveSoftware/Brave-Browser-*/*/Cookies",
        ),
    },
    "linux": {
        "chrome": (
            "~/.config/google-chrome/*/Cookies",
            "~/.config/google-chrome-*/*/Cookies",
        ),
        "brave": (
            "~/.config/BraveSoftware/Brave-Browser/*/Cookies",
            "~/.config/BraveSoftware/Brave-Browser-*/*/Cookies",
        ),
    },
}


def _cookie_store_paths(browser: BrowserName, profile: str | None = None) -> list[str]:
    """Expand the per-OS cookie-store globs for ``browser`` into real paths.

    When ``profile`` is given, only the cookie store under that profile
    directory is returned (e.g. ``"Default"`` or ``"Profile 2"``). This
    avoids walking every profile — useful when you know which account is
    signed into loseit.com, and on macOS it collapses the per-profile
    Keychain prompt storm down to a single prompt.
    """
    platform_key = "darwin" if sys.platform == "darwin" else "linux"
    patterns = _COOKIE_GLOBS.get(platform_key, {}).get(browser, ())
    paths: list[str] = []
    for pat in patterns:
        paths.extend(glob.glob(os.path.expanduser(pat)))
    if profile is not None:
        # The profile dir is the parent of the Cookies file.
        paths = [p for p in paths if os.path.basename(os.path.dirname(p)) == profile]
    seen: set[str] = set()
    return [p for p in paths if not (p in seen or seen.add(p))]


def load_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    """Return the ``liauth`` JWT. Raises ``FileNotFoundError`` if missing."""
    env_token = os.environ.get("LOSEIT_TOKEN")
    if env_token:
        logger.debug("auth.load_token: using LOSEIT_TOKEN env var ({n} chars)", n=len(env_token))
        return env_token.strip()
    if token_file.exists():
        token = token_file.read_text().strip()
        logger.debug("auth.load_token: read token from {p} ({n} chars)", p=token_file, n=len(token))
        return token
    logger.error(
        "auth.load_token: no token (env LOSEIT_TOKEN unset, file {p} missing)", p=token_file
    )
    raise FileNotFoundError(f"No token: set LOSEIT_TOKEN env var or write JWT to {token_file}")


def save_token(token: str, token_file: Path = DEFAULT_TOKEN_FILE) -> Path:
    """Atomically write ``token`` to ``token_file`` with ``chmod 600``."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(token_file.suffix + ".tmp")
    tmp.write_text(token.strip() + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(token_file)
    return token_file


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Return the JWT payload claims, or ``None`` if undecodable.

    Inspects the payload only — does NOT verify the signature. Callers use
    this to read ``sub`` (user id), ``exp`` (expiry), and any provider-
    specific username/email claim Lose It may include.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def decode_jwt_exp(token: str) -> int | None:
    """Return the JWT ``exp`` claim (unix seconds), or ``None`` if undecodable."""
    payload = decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, int | float) else None


# Common JWT claim names that hold an email address or display username, in
# order of preference. Lose It may use any of these — we try them all rather
# than hard-coding one and silently failing if the schema changes.
_USERNAME_CLAIMS: tuple[str, ...] = (
    "email",
    "preferred_username",
    "username",
    "user_name",
    "name",
    "mail",
    "upn",
    "sub_email",
)

# Same idea for the user-id claim. ``sub`` is the JWT standard for "subject"
# (account identifier), but some providers ship a parallel custom claim too.
_USER_ID_CLAIMS: tuple[str, ...] = (
    "sub",
    "user_id",
    "userId",
    "uid",
    "id",
)


def extract_user_info_from_jwt(token: str) -> dict[str, str]:
    """Best-effort extraction of ``user_id`` / ``user_name`` from a JWT payload.

    Returns whatever it can find; the caller is expected to fall back to
    other sources (browser cookies, interactive prompt) for fields that
    aren't present in the payload.
    """
    payload = decode_jwt_payload(token)
    if not payload:
        return {}
    out: dict[str, str] = {}
    for claim in _USER_ID_CLAIMS:
        v = payload.get(claim)
        if v not in (None, ""):
            out["user_id"] = str(v)
            break
    for claim in _USERNAME_CLAIMS:
        v = payload.get(claim)
        if isinstance(v, str) and v:
            out["user_name"] = v
            break
    return out


def is_token_expired(token: str, *, leeway_seconds: int = 60) -> bool:
    """True if the JWT's ``exp`` is in the past (or within ``leeway_seconds``)."""
    exp = decode_jwt_exp(token)
    if exp is None:
        return False
    return exp - leeway_seconds <= time.time()


def load_cookies_from_browser(
    browser: BrowserName,
    domain: str = "loseit.com",
    profile: str | None = None,
) -> dict[str, str]:
    """Return every cookie ``browser`` has for ``domain`` as a name → value dict.

    Walks every profile under the browser's user-data root and merges them
    (last-write-wins on name collisions, but since each profile is
    typically a different account the values rarely conflict). Pass
    ``profile`` (e.g. ``"Default"``) to read just that one profile.
    Returns ``{}`` if browser-cookie3 isn't importable or no cookie is found.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        return {}

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise ValueError(f"Unsupported browser {browser!r}; expected one of {SUPPORTED_BROWSERS}")

    out: dict[str, str] = {}
    for path in _cookie_store_paths(browser, profile):
        try:
            cj = loader(cookie_file=path, domain_name=domain)
        except Exception:
            continue
        for c in cj:
            if c.value:
                out[c.name] = c.value
    return out


def refresh_token_from_browser(
    browser: BrowserName,
    domain: str = "loseit.com",
    profile: str | None = None,
) -> str | None:
    """Read ``liauth`` directly from ``browser``'s encrypted cookie store.

    Walks every profile under that browser's user-data root and returns the
    first ``liauth`` cookie it finds, or ``None`` if no profile has one for
    ``domain``. Pass ``profile`` (e.g. ``"Default"``) to read just that one
    profile. First call may trigger a macOS Keychain prompt so the OS
    can release the cookie-store master key.
    """
    logger.info(
        "auth.refresh_token_from_browser: browser={b} domain={d} profile={p}",
        b=browser,
        d=domain,
        p=profile,
    )
    cookies = load_cookies_from_browser(browser, domain, profile)
    token = cookies.get("liauth")
    logger.debug(
        "auth.refresh_token_from_browser: {n} total cookies, liauth={found}",
        n=len(cookies),
        found="present" if token else "missing",
    )
    return token


# Cookie names that some Lose It cookies have historically used to ship
# the signed-in account's email/username. Tried in order; first non-empty
# value wins. The names err on the side of "noisy" — false positives
# (e.g. a cookie literally called "email" but containing a tracker id)
# are reviewed before being written to disk in :func:`extract_user_name_from_cookies`.
_USERNAME_COOKIE_NAMES: tuple[str, ...] = (
    "loseit_email",
    "loseit_username",
    "li_username",
    "li_email",
    "user_email",
    "user_name",
    "email",
    "username",
)


def extract_user_name_from_cookies(cookies: dict[str, str]) -> str | None:
    """Pick out a Lose It username (typically an email) from a cookie jar.

    Conservative: only returns a value that looks like an email address or
    a short username-ish identifier (no spaces, no JWT-ish dots-with-base64
    structure). Returns ``None`` if nothing plausibly fits, so the caller
    falls back to an interactive prompt rather than persisting garbage.
    """
    for name in _USERNAME_COOKIE_NAMES:
        v = cookies.get(name)
        if not v:
            continue
        # JWT-shaped values (three dot-separated base64 chunks) are not
        # usernames even when the cookie name suggests they are.
        if v.count(".") >= 2 and all(len(p) >= 8 for p in v.split(".")[:3]):
            continue
        if " " in v or len(v) > 254:
            continue
        return v
    return None


def refresh_token_from_chrome(domain: str = "loseit.com") -> str | None:
    """Shorthand for ``refresh_token_from_browser('chrome', domain)``."""
    return refresh_token_from_browser("chrome", domain=domain)


def refresh_token_from_brave(domain: str = "loseit.com") -> str | None:
    """Shorthand for ``refresh_token_from_browser('brave', domain)``."""
    return refresh_token_from_browser("brave", domain=domain)


# Per-OS user-data roots (the directory that holds `Local State` and each
# profile's `<dir>/Cookies`). Mirrors the structure of ``_COOKIE_GLOBS`` —
# kept separate because the `Local State` lookup wants the *root*, not the
# fully-expanded cookie-store globs.
_USER_DATA_ROOTS: dict[str, dict[BrowserName, tuple[str, ...]]] = {
    "darwin": {
        "chrome": ("~/Library/Application Support/Google/Chrome",),
        "brave": (
            "~/Library/Application Support/BraveSoftware/Brave-Browser",
            "~/Library/Application Support/BraveSoftware/Brave-Browser-*",
        ),
    },
    "linux": {
        "chrome": ("~/.config/google-chrome", "~/.config/google-chrome-*"),
        "brave": (
            "~/.config/BraveSoftware/Brave-Browser",
            "~/.config/BraveSoftware/Brave-Browser-*",
        ),
    },
}


def _read_profile_friendly_names(user_data_root: str) -> dict[str, str]:
    """Parse ``Local State`` for ``profile.info_cache.<dir>.name`` entries.

    ``Local State`` is a plain-JSON file at the browser's user-data root.
    Reading it does **not** require the Keychain — the cookie-store
    decryption key is the only part of that store that's protected. If
    the file is missing or unparseable we return ``{}`` and the caller
    falls back to the bare directory name.
    """
    local_state = os.path.join(user_data_root, "Local State")
    if not os.path.isfile(local_state):
        return {}
    try:
        with open(local_state, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    info_cache = data.get("profile", {}).get("info_cache", {}) if isinstance(data, dict) else {}
    out: dict[str, str] = {}
    if isinstance(info_cache, dict):
        for dir_name, meta in info_cache.items():
            if isinstance(meta, dict):
                name = meta.get("name")
                if isinstance(name, str) and name:
                    out[dir_name] = name
    return out


def list_browser_profiles(browser: BrowserName) -> list[dict[str, str | None]]:
    """List the named ``browser``'s profiles by reading the filesystem only.

    Returns one entry per profile directory that has a ``Cookies`` store
    on disk. Each entry carries:

    - ``directory`` — the profile directory name as ``--profile`` expects
      it (e.g. ``"Default"``, ``"Profile 2"``).
    - ``name`` — the friendly display name from ``Local State``
      (e.g. ``"Eric (Work)"``) or ``None`` if it can't be resolved.
    - ``cookie_store`` — absolute path to the profile's ``Cookies`` file.

    **No cookie decryption happens here** — the call does not touch the
    macOS Keychain. Use this to show the user which profiles exist so
    they can pick one for ``loseit login --profile <directory>``.
    """
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError(f"Unsupported browser {browser!r}; expected one of {SUPPORTED_BROWSERS}")

    platform_key = "darwin" if sys.platform == "darwin" else "linux"

    friendly: dict[str, str] = {}
    for pat in _USER_DATA_ROOTS.get(platform_key, {}).get(browser, ()):
        for root in glob.glob(os.path.expanduser(pat)):
            friendly.update(_read_profile_friendly_names(root))

    out: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for path in _cookie_store_paths(browser):
        directory = os.path.basename(os.path.dirname(path))
        if directory in seen:
            continue
        seen.add(directory)
        out.append(
            {
                "directory": directory,
                "name": friendly.get(directory),
                "cookie_store": path,
            }
        )
    return out
