"""Pure text-rendering of SDK models for CLI ``--output text`` mode.

Each ``render_*`` returns a multi-line string suitable for
``print``/``typer.echo``. No function in this module imports ``typer``
or writes to stdout — that lets non-typer surfaces (the ``log-food``
skill, future TUI, notebook helpers) reuse the same formatting.

The companion *machine-readable* projection (for ``--output json``/
``toon``) lives on the dataclasses themselves: each model in
:mod:`lose_it.models` has a ``.to_dict()`` method that returns the same
shape the CLI used to build inline. Keeping those projections on the
model means SDK callers get them for free::

    desc = li.describe_food(food_id)
    json.dumps(desc.to_dict(), indent=2)

…without needing a parallel formatter module.
"""

from __future__ import annotations

from datetime import date

from ..models import (
    FoodDescription,
    FoodLogEntry,
    FoodSearchResult,
    LoggedFood,
    LoginResult,
)

__all__ = [
    "render_diary",
    "render_food_description",
    "render_logged_food",
    "render_login_result",
    "render_search_results",
]


def render_search_results(results: list[FoodSearchResult], *, limit: int = 15) -> str:
    """Format up to ``limit`` rows as the CLI's aligned ``# Food / Brand / Food ID`` table.

    Returns the empty-state line (``"  No results."``) when the list is
    empty. Names + brands are truncated at the column widths the CLI uses
    today (50 / 20 chars). Hex food IDs are abbreviated to 10 chars + ``…``.
    """
    raise NotImplementedError


def render_diary(entries: list[FoodLogEntry], when: date) -> str:
    """Format a day's entries grouped by meal.

    Empty-state: ``"  (no entries for YYYY-MM-DD)"``. Otherwise renders
    the header line + per-meal blocks with index, food name, brand, and
    calories (when present).
    """
    raise NotImplementedError


def render_food_description(desc: FoodDescription) -> str:
    """Format a :class:`FoodDescription` as the CLI's text block.

    Matches the old ``describe-food`` text output: cyan name banner,
    brand/category line, food_id, primary serving line, per-class
    conversion values when present, then a nutrients table.
    """
    raise NotImplementedError


def render_logged_food(logged: LoggedFood) -> str:
    """Format the ``log`` command's confirmation line.

    Prefix is ``"🟡 DRY RUN — would log"`` when ``logged.dry_run`` else
    ``"✅ Logged"``. Embeds the food name, abbreviated id, meal name,
    portion, and calories (when present). The CLI is responsible for
    picking the colour (green/yellow) since that's a typer concern.
    """
    raise NotImplementedError


def render_login_result(result: LoginResult) -> str:
    """Format the ``login`` command's status output.

    On ``status="ok"``: success banner + JWT exp + (when present) the
    written config path and key/value pairs. On ``"missing"`` /
    ``"expired"``: the error message + hint + signin URL (and a tail
    suggesting the re-run command). The CLI applies colour separately.
    """
    raise NotImplementedError
