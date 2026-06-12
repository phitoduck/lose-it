"""Unit-table conformance tests for ``lose_it.client._units``.

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

from lose_it.client._units import (
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


def test_resolve_unit_accepts_integer_ordinal_as_escape_hatch() -> None:
    """For units the CLI doesn't yet label, the user can pass a raw ordinal.

    Example: ``FoodMeasurement.PIE = 46`` exists in the enum but if it
    didn't, the user could still log against a pizza-stored food with
    ``--serving-unit 46``. This is an escape hatch — it requires knowing
    Lose It!'s internal enum values, but it unblocks new ordinals
    without a CLI release.
    """
    assert resolve_unit("46") == 46
    assert resolve_unit("99") == 99  # totally unmapped
    assert resolve_unit(" 27 ") == 27  # tolerates whitespace


def test_resolve_unit_unknown_error_message_lists_known_values_and_escape_hatch() -> None:
    """The error message tells the user the known names AND the integer escape hatch."""
    with pytest.raises(ValueError) as excinfo:
        resolve_unit("xyz")
    msg = str(excinfo.value)
    # Known names enumerated:
    assert "cup" in msg and "tbsp" in msg and "scoop" in msg
    # Escape hatch documented:
    assert "ordinal" in msg.lower() or "integer" in msg.lower()


# ── conversion_factor ───────────────────────────────────────────────────────


def test_resolve_unit_teaspoon() -> None:
    """Teaspoon (ord=1) — confirmed via Raw Honey '1 Teaspoon' food."""
    assert resolve_unit("tsp") == 1
    assert resolve_unit("teaspoon") == 1
    assert resolve_unit("teaspoons") == 1


def test_resolve_unit_can_and_container_and_bottle() -> None:
    """Discrete vessel units confirmed via wire probe.

    - can (ord=21):   Pepsi/Mountain Dew per_serving_ml=355 (12 fl oz US can)
    - container (ord=45): Chobani/Fage 'Indiv. Container' / 'Single Serve'
    - bottle (ord=19):    Slimfast shake bottle, Diet Coke can
    """
    assert resolve_unit("can") == 21
    assert resolve_unit("cans") == 21
    assert resolve_unit("container") == 45
    assert resolve_unit("containers") == 45
    assert resolve_unit("bottle") == 19
    assert resolve_unit("bottles") == 19


def test_resolve_unit_piece_and_pie() -> None:
    assert resolve_unit("piece") == 4
    assert resolve_unit("pieces") == 4
    assert resolve_unit("pie") == 46


def test_conversion_factor_cup_to_mL() -> None:
    """The volumetric constant for 1 US cup → mL is 236.5882365."""
    factor = conversion_factor(3, 11)
    assert factor is not None
    assert math.isclose(factor, 236.5882365, rel_tol=1e-9)


def test_conversion_factor_teaspoon_to_mL() -> None:
    """1 US teaspoon = 4.92892159375 mL (confirmed via Raw Honey wire payload)."""
    factor = conversion_factor(1, 11)
    assert factor is not None
    assert math.isclose(factor, 4.92892159375, rel_tol=1e-9)


def test_conversion_factor_tsp_to_tbsp_to_cup() -> None:
    """Chained tsp ↔ tbsp ↔ cup ratios are consistent.

    1 cup = 16 tbsp = 48 tsp. Each leg must round-trip.
    """
    # 1 tbsp = 3 tsp
    assert conversion_factor(2, 1) == 3.0
    # 1 cup = 48 tsp
    assert conversion_factor(3, 1) == 48.0
    # 1 tsp = 1/3 tbsp
    factor_tsp_to_tbsp = conversion_factor(1, 2)
    assert factor_tsp_to_tbsp is not None
    assert math.isclose(factor_tsp_to_tbsp, 1.0 / 3.0, rel_tol=1e-9)


def test_conversion_factor_fl_oz_to_tsp() -> None:
    """1 US fl oz = 6 tsp (= 29.5735 mL / 4.9289 mL)."""
    assert conversion_factor(10, 1) == 6.0
    factor_tsp_to_floz = conversion_factor(1, 10)
    assert factor_tsp_to_floz is not None
    assert math.isclose(factor_tsp_to_floz, 1.0 / 6.0, rel_tol=1e-9)


def test_conversion_factor_cup_to_grams_returns_none() -> None:
    """Cup → grams is deliberately absent (no per-food density info)."""
    assert conversion_factor(3, 8) is None


def test_conversion_factor_mL_to_cup_reciprocal() -> None:
    """The mL → cup factor should be the reciprocal of cup → mL."""
    factor = conversion_factor(11, 3)
    assert factor is not None
    assert math.isclose(factor, 1.0 / 236.5882365, rel_tol=1e-9)


def test_conversion_factor_diagonal_is_one() -> None:
    """Every (ord, ord) diagonal entry exists and equals 1.0.

    Post-fix all units (grams included) are 1.0 on the diagonal —
    cross-unit conversions are derived by combining these generic
    factors with the food's stored per-serving qty.
    """
    for ord_ in (1, 2, 3, 4, 5, 8, 10, 11, 19, 21, 26, 27, 33, 45, 46):
        assert conversion_factor(ord_, ord_) == 1.0


def test_conversion_factor_unsupported_pair_is_none() -> None:
    """Cross-class conversions (e.g. ``serving`` → ``mL``) return None.

    No physical answer is possible without per-food density data; the
    caller exits with ``unit_not_supported`` in this case.
    """
    assert conversion_factor(27, 11) is None
    assert conversion_factor(5, 8) is None
    assert conversion_factor(3, 8) is None
    assert conversion_factor(8, 11) is None


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
