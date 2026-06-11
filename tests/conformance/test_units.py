"""Unit-table conformance tests for ``lose_it_utils.client._units``.

Covers the Phase 1 cases from ``docs/serving-unit-spec.md``:

| Case                                | Expected                            |
|-------------------------------------|-------------------------------------|
| ``resolve_unit("mL")``              | ``11``                              |
| ``resolve_unit("g")``               | ``8``                               |
| ``resolve_unit("CUPS")``            | ``3`` (case-insensitive)            |
| ``resolve_unit("fl_oz")``           | ``10``                              |
| ``resolve_unit("oz")``              | ``ValueError("ambiguous")``         |
| ``resolve_unit("unicorn")``         | ``ValueError("Unknown")``           |
| ``conversion_factor(3, 11)``        | ``≈ 236.588``                       |
| ``conversion_factor(3, 8)``         | ``None`` (no cup→grams)             |
| ``conversion_factor(11, 3)``        | ``≈ 0.00423`` (reciprocal)          |
"""

from __future__ import annotations

import math

import pytest

from lose_it_utils.client._units import (
    CANONICAL_UNIT_NAMES,
    CONVERSIONS,
    UNIT_ALIASES,
    conversion_factor,
    resolve_unit,
)


# ── resolve_unit ────────────────────────────────────────────────────────────


def test_resolve_unit_mL() -> None:
    assert resolve_unit("mL") == 11


def test_resolve_unit_g() -> None:
    assert resolve_unit("g") == 8


def test_resolve_unit_cups_case_insensitive() -> None:
    """The resolver should normalize case before looking up aliases."""
    assert resolve_unit("CUPS") == 3
    assert resolve_unit("Cups") == 3
    assert resolve_unit("cup") == 3


def test_resolve_unit_fl_oz() -> None:
    assert resolve_unit("fl_oz") == 10
    # Common typings of fluid ounce should all resolve to ord 10.
    assert resolve_unit("floz") == 10
    assert resolve_unit("fl-oz") == 10
    assert resolve_unit("fluid_oz") == 10


def test_resolve_unit_oz_ambiguous() -> None:
    """Bare ``oz`` is deliberately rejected: it could mean weight or fluid."""
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_unit("oz")


def test_resolve_unit_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        resolve_unit("unicorn")


def test_resolve_unit_strips_whitespace_and_spaces() -> None:
    """Surrounding whitespace + ``space → underscore`` normalization."""
    assert resolve_unit("  cup  ") == 3
    assert resolve_unit("fluid oz") == 10


# ── conversion_factor ───────────────────────────────────────────────────────


def test_conversion_factor_cup_to_mL() -> None:
    """The volumetric constant for 1 US cup → mL is 236.5882365."""
    factor = conversion_factor(3, 11)
    assert factor is not None
    assert math.isclose(factor, 236.5882365, rel_tol=1e-9)


def test_conversion_factor_cup_to_grams_returns_none() -> None:
    """Cup → grams is deliberately absent (no per-food density info)."""
    assert conversion_factor(3, 8) is None


def test_conversion_factor_mL_to_cup_reciprocal() -> None:
    """The mL → cup factor should be the reciprocal of cup → mL."""
    factor = conversion_factor(11, 3)
    assert factor is not None
    assert math.isclose(factor, 1.0 / 236.5882365, rel_tol=1e-9)


def test_conversion_factor_diagonal_is_one() -> None:
    """Every (ord, ord) diagonal entry exists and equals 1.0 — except grams,
    which is the special case described in ``_units.py`` (1 serving = 100 g)."""
    for ord_ in (3, 11, 10, 2):
        assert conversion_factor(ord_, ord_) == 1.0
    # The grams special case lives in the same table; it's 100.0 by design
    # (mirrors the existing ``--grams`` flag's "1 serving = 100 g" convention).
    assert conversion_factor(8, 8) == 100.0


def test_conversion_factor_unsupported_pair_is_none() -> None:
    """Combinations not in the table (e.g. ``serving`` → ``mL``) return None."""
    # Serving ord (27) has no entries at all.
    assert conversion_factor(27, 11) is None
    assert conversion_factor(5, 8) is None


# ── Sanity checks on the data tables ────────────────────────────────────────


def test_canonical_unit_names_cover_known_ords() -> None:
    """Every CANONICAL_UNIT_NAMES key should be a value in UNIT_ALIASES."""
    aliased_ords = set(UNIT_ALIASES.values())
    assert set(CANONICAL_UNIT_NAMES.keys()).issubset(aliased_ords)


def test_conversions_table_only_uses_known_ords() -> None:
    """Every ord in CONVERSIONS should be present in UNIT_ALIASES.values()."""
    aliased_ords = set(UNIT_ALIASES.values())
    for canonical, chosen in CONVERSIONS:
        assert canonical in aliased_ords, f"{canonical} not in UNIT_ALIASES"
        assert chosen in aliased_ords, f"{chosen} not in UNIT_ALIASES"
