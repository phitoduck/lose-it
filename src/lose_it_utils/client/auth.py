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


def _cookie_store_paths(browser: BrowserName) -> list[str]:
    """Expand the per-OS cookie-store globs for ``browser`` into real paths."""
    platform_key = "darwin" if sys.platform == "darwin" else "linux"
    patterns = _COOKIE_GLOBS.get(platform_key, {}).get(browser, ())
    paths: list[str] = []
    for pat in patterns:
        paths.extend(glob.glob(os.path.expanduser(pat)))
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
) -> dict[str, str]:
    """Return every cookie ``browser`` has for ``domain`` as a name → value dict.

    Walks every profile under the browser's user-data root and merges them
    (last-write-wins on name collisions, but since each profile is
    typically a different account the values rarely conflict). Returns
    ``{}`` if browser-cookie3 isn't importable or no cookie is found.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        return {}

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise ValueError(f"Unsupported browser {browser!r}; expected one of {SUPPORTED_BROWSERS}")

    out: dict[str, str] = {}
    for path in _cookie_store_paths(browser):
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
) -> str | None:
    """Read ``liauth`` directly from ``browser``'s encrypted cookie store.

    Walks every profile under that browser's user-data root and returns the
    first ``liauth`` cookie it finds, or ``None`` if no profile has one for
    ``domain``. First call may trigger a macOS Keychain prompt so the OS
    can release the cookie-store master key.
    """
    logger.info("auth.refresh_token_from_browser: browser={b} domain={d}", b=browser, d=domain)
    cookies = load_cookies_from_browser(browser, domain)
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
