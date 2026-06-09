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
from typing import Literal

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
        return env_token.strip()
    if token_file.exists():
        return token_file.read_text().strip()
    raise FileNotFoundError(f"No token: set LOSEIT_TOKEN env var or write JWT to {token_file}")


def save_token(token: str, token_file: Path = DEFAULT_TOKEN_FILE) -> Path:
    """Atomically write ``token`` to ``token_file`` with ``chmod 600``."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(token_file.suffix + ".tmp")
    tmp.write_text(token.strip() + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(token_file)
    return token_file


def decode_jwt_exp(token: str) -> int | None:
    """Return the JWT ``exp`` claim (unix seconds), or ``None`` if undecodable.

    Inspects the payload only — does NOT verify the signature.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, int | float) else None


def is_token_expired(token: str, *, leeway_seconds: int = 60) -> bool:
    """True if the JWT's ``exp`` is in the past (or within ``leeway_seconds``)."""
    exp = decode_jwt_exp(token)
    if exp is None:
        return False
    return exp - leeway_seconds <= time.time()


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
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        return None

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise ValueError(f"Unsupported browser {browser!r}; expected one of {SUPPORTED_BROWSERS}")

    for path in _cookie_store_paths(browser):
        try:
            cj = loader(cookie_file=path, domain_name=domain)
        except Exception:
            continue
        for c in cj:
            if c.name == "liauth" and c.value:
                return c.value
    return None


def refresh_token_from_chrome(domain: str = "loseit.com") -> str | None:
    """Shorthand for ``refresh_token_from_browser('chrome', domain)``."""
    return refresh_token_from_browser("chrome", domain=domain)


def refresh_token_from_brave(domain: str = "loseit.com") -> str | None:
    """Shorthand for ``refresh_token_from_browser('brave', domain)``."""
    return refresh_token_from_browser("brave", domain=domain)
