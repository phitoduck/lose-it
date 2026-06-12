"""Pure rendering of SDK models to ``str`` / ``dict``.

Two parallel renderers per model:

- ``render_*`` returns a multi-line string suitable for ``typer.echo``
  / ``print`` (CLI text mode).
- ``*_to_dict`` returns a JSON-safe dict suitable for ``json.dumps`` or
  ``toon_format.encode`` (CLI ``--output json``/``toon`` modes).

Keeping both shapes here — instead of inlining ``typer.echo`` calls in
the CLI — means the same rendering can power non-typer surfaces (the
``log-food`` skill, future TUI, notebook helpers) without dragging in a
CLI dependency. No function in this module imports ``typer``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ._models import (
    FoodDescription,
    FoodLogEntry,
    FoodSearchResult,
    LoggedFood,
    LoginResult,
)

__all__ = [
    "entry_to_dict",
    "food_description_to_dict",
    "logged_food_to_dict",
    "login_result_to_dict",
    "render_diary",
    "render_food_description",
    "render_logged_food",
    "render_login_result",
    "render_search_results",
    "search_results_to_dict",
]


# ── Search results ──────────────────────────────────────────────────────────


def render_search_results(results: list[FoodSearchResult], *, limit: int = 15) -> str:
    """Format up to ``limit`` rows as the CLI's aligned ``# Food / Brand / Food ID`` table.

    Returns the empty-state line (``"  No results."``) when the list is
    empty. Names + brands are truncated at the column widths the CLI uses
    today (50 / 20 chars). Hex food IDs are abbreviated to 10 chars + ``…``.
    """
    raise NotImplementedError


def search_results_to_dict(
    results: list[FoodSearchResult], query: str, *, verbose: bool = False
) -> dict[str, Any]:
    """Project search results into a JSON-safe dict.

    Shape matches today's ``--output json`` envelope::

        {"query": str, "count": int, "results": [
            {"name", "brand", "category", "food_id", ?"pk_bytes"}, …
        ]}

    ``verbose=True`` includes the raw 16-int ``pk_bytes`` array on each
    result — useful for SDK callers driving the Python API but noisy in
    the default CLI surface.
    """
    raise NotImplementedError


# ── Diary entries ───────────────────────────────────────────────────────────


def render_diary(entries: list[FoodLogEntry], when: date) -> str:
    """Format a day's entries grouped by meal.

    Empty-state: ``"  (no entries for YYYY-MM-DD)"``. Otherwise renders
    the header line + per-meal blocks with index, food name, brand, and
    calories (when present).
    """
    raise NotImplementedError


def entry_to_dict(entry: FoodLogEntry) -> dict[str, Any]:
    """Project a :class:`FoodLogEntry` into a JSON-safe dict.

    Includes both raw-ordinal nutrients (``nutrients``) and labeled
    nutrients (``nutrients_by_label``) so the document is both
    human-readable and machine-parseable. Mirrors the shape today's
    ``--output json`` produces (lifted from ``cli._entry_to_dict``).
    """
    raise NotImplementedError


# ── Food description (describe-food) ────────────────────────────────────────


def render_food_description(desc: FoodDescription) -> str:
    """Format a :class:`FoodDescription` as the CLI's text block.

    Matches the old ``describe-food`` text output: cyan name banner,
    brand/category line, food_id, primary serving line, per-class
    conversion values when present, then a nutrients table.
    """
    raise NotImplementedError


def food_description_to_dict(desc: FoodDescription) -> dict[str, Any]:
    """JSON-safe projection of a :class:`FoodDescription`."""
    raise NotImplementedError


# ── log-food result ─────────────────────────────────────────────────────────


def render_logged_food(logged: LoggedFood) -> str:
    """Format the ``log`` command's confirmation line.

    Prefix is ``"🟡 DRY RUN — would log"`` when ``logged.dry_run`` else
    ``"✅ Logged"``. Embeds the food name, abbreviated id, meal name,
    portion, and calories (when present). The CLI is responsible for
    picking the colour (green/yellow) since that's a typer concern.
    """
    raise NotImplementedError


def logged_food_to_dict(logged: LoggedFood) -> dict[str, Any]:
    """JSON-safe projection of a :class:`LoggedFood`."""
    raise NotImplementedError


# ── login result ────────────────────────────────────────────────────────────


def render_login_result(result: LoginResult) -> str:
    """Format the ``login`` command's status output.

    On ``status="ok"``: success banner + JWT exp + (when present) the
    written config path and key/value pairs. On ``"missing"`` /
    ``"expired"``: the error message + hint + signin URL (and a tail
    suggesting the re-run command). The CLI applies colour separately.
    """
    raise NotImplementedError


def login_result_to_dict(result: LoginResult) -> dict[str, Any]:
    """JSON-safe projection of a :class:`LoginResult`."""
    raise NotImplementedError
