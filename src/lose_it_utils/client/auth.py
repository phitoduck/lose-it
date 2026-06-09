"""Token loading helpers.

Two sources, in priority order:

1. ``LOSEIT_TOKEN`` env var.
2. ``~/.config/loseit/token`` (plain text JWT, ``chmod 600`` recommended).

Optionally, :func:`refresh_token_from_chrome` decrypts Chrome's cookie store
(via ``browser-cookie3``) so the JWT can be refreshed silently from a live
browser session instead of doing the every-2-weeks DevTools dance.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TOKEN_FILE = Path("~/.config/loseit/token").expanduser()


def load_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    """Return the ``liauth`` JWT. Raises ``FileNotFoundError`` if missing."""
    env_token = os.environ.get("LOSEIT_TOKEN")
    if env_token:
        return env_token.strip()
    if token_file.exists():
        return token_file.read_text().strip()
    raise FileNotFoundError(
        f"No token: set LOSEIT_TOKEN env var or write JWT to {token_file}"
    )


def refresh_token_from_chrome(domain: str = "loseit.com") -> str | None:
    """Read ``liauth`` directly from Chrome's encrypted cookie store.

    Returns the cookie value or ``None`` if not found. Tries each Chrome
    profile in turn. First call may trigger a macOS Keychain prompt.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        return None

    import glob

    candidates = sorted(
        glob.glob(os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/*/Cookies"
        ))
    )
    for path in candidates:
        try:
            cj = browser_cookie3.chrome(cookie_file=path, domain_name=domain)
        except Exception:
            continue
        for c in cj:
            if c.name == "liauth" and c.value:
                return c.value
    return None
