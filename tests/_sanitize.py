"""Redact account-identifying values from captured GWT-RPC fixtures.

The fixtures are checked into a public repo. The reverse-engineered protocol
uses the account's numeric user ID and username in plaintext, so any captured
request/response includes them. We replace both with stable test placeholders
so the conformance tests are reproducible without leaking real account info.
"""

from __future__ import annotations

import os

PLACEHOLDER_USER_ID = "12345678"
PLACEHOLDER_USER_NAME = "test.user"


def sanitize(text: str) -> str:
    """Replace the live account's user_id + user_name with test placeholders."""
    real_uid = os.environ.get("LOSEIT_USER_ID", "")
    real_uname = os.environ.get("LOSEIT_USER_NAME", "")
    if real_uid:
        text = text.replace(real_uid, PLACEHOLDER_USER_ID)
    if real_uname:
        text = text.replace(real_uname, PLACEHOLDER_USER_NAME)
    return text
