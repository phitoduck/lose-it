"""Food ID encoding/decoding (16-byte SimplePrimaryKey <-> 32-char hex).

The SDK already represents PKs as ``list[int]`` in "response form"
(see ``FoodSearchResult.pk_bytes`` in ``_models.py``). This module is
the user-facing translation layer: humans see lowercase hex, the SDK
sees signed ints.
"""

from __future__ import annotations


def pk_to_hex(pk_bytes: list[int]) -> str:
    """Encode response-form PK bytes as 32-char lowercase hex."""
    if len(pk_bytes) != 16:
        raise ValueError(f"PK must be 16 bytes; got {len(pk_bytes)}.")
    return bytes((b & 0xFF) for b in pk_bytes).hex()


def hex_to_pk(food_id: str) -> list[int]:
    """Decode 32-char lowercase hex into response-form PK bytes.

    Accepts arbitrary case and trims whitespace. Raises ``ValueError``
    for non-hex input or length mismatches.
    """
    s = food_id.strip().lower()
    try:
        raw = bytes.fromhex(s)
    except ValueError as exc:
        raise ValueError(f"Food ID is not valid hex: {food_id!r}") from exc
    if len(raw) != 16:
        raise ValueError(f"Food ID must be 32 hex chars (16 bytes); got {len(raw)} bytes.")
    return [b - 256 if b >= 128 else b for b in raw]
