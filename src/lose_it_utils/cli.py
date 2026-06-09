"""Typer-based CLI for the Lose It! SDK.

The CLI is a thin wrapper around :class:`lose_it_utils.Client` and the
``lose_it_utils.client.*`` modules. Subcommands::

    lose-it search "tortilla"                              List candidate foods
    lose-it log "tortilla" --meal lunch --servings 1.0     Search + log
    lose-it log "tortilla" --meal lunch --pick 2           Skip the interactive prompt
    lose-it diary                                          Show today's diary
    lose-it diary --date 2026-06-08                        Show another day's diary
    lose-it delete --meal lunch --pick 1                   Delete an entry by index
    lose-it delete --meal lunch --pick 1 --yes             Skip the confirmation prompt
    lose-it whoami                                         Print resolved config (user_id, timezone, etc.)

All commands honor the ``LOSEIT_*`` env vars for configuration (see
``Config.from_env``) and read the JWT token from ``~/.config/loseit/token``
(or ``$LOSEIT_TOKEN`` if set).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

import typer

from .client import Client, MissingConfigError, daily, entries, foods
from .client._config import MEAL_NAMES, MEAL_TYPES
from .client._dates import day_number_for, parse_date_arg
from .client.init import get_daydate_key

app = typer.Typer(
    name="lose-it",
    help="Unofficial Lose It! food logger / diary CLI.",
    no_args_is_help=True,
    add_completion=False,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _open_client() -> Client:
    """Build a Client from env vars; print a friendly error if config / token missing."""
    try:
        return Client.from_env()
    except FileNotFoundError as e:
        typer.secho(f"❌ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    except MissingConfigError as e:
        typer.secho(f"❌ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e


def _print_search_results(results) -> None:
    if not results:
        typer.echo("  No results.")
        return
    typer.echo(f"\n{'#':>3}  {'Food':50} {'Brand'}")
    typer.echo(f"{'─' * 3}  {'─' * 50} {'─' * 20}")
    for i, f in enumerate(results[:15]):
        name = (f.name or "")[:50]
        brand = (f.brand or "")[:20]
        typer.echo(f"{i + 1:>3}  {name:50} {brand}")


def _print_diary(entries_, when: date) -> None:
    if not entries_:
        typer.echo(f"  (no entries for {when.isoformat()})")
        return
    by_meal: dict[int, list] = {0: [], 1: [], 2: [], 3: []}
    for e in entries_:
        by_meal.setdefault(e.meal_ordinal, []).append(e)
    typer.echo(f"\n📅 Diary for {when.isoformat()}:")
    for m_ord in sorted(by_meal):
        items = by_meal[m_ord]
        if not items:
            continue
        typer.echo(f"\n  {MEAL_NAMES.get(m_ord, f'meal{m_ord}').capitalize()}:")
        for i, e in enumerate(items):
            brand = f" ({e.food_brand})" if e.food_brand else ""
            cal = e.calories
            cal_str = f"  [{cal:.0f} cal]" if cal is not None else ""
            typer.echo(f"    {i + 1}. {e.food_name}{brand}  × {e.servings}{cal_str}")


def _resolve_pick(picked: int | None, prompt: str, n: int) -> int:
    """Resolve a 1-based pick value (CLI arg or interactive prompt) to a 0-based idx."""
    if picked is None:
        choice = typer.prompt(prompt, default="", show_default=False)
        if not choice or choice.lower() in ("q", "quit"):
            raise typer.Exit(code=0)
        try:
            picked = int(choice)
        except ValueError as exc:
            typer.secho("Invalid number.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    idx = picked - 1
    if not 0 <= idx < n:
        typer.secho(f"--pick must be 1..{n}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return idx


# ── Commands ─────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Free-text search query")],
) -> None:
    """Search the LoseIt food database."""
    with _open_client() as client:
        results = foods.search(client.http, query)
        _print_search_results(results)


@app.command()
def log(
    query: Annotated[str, typer.Argument(help="Food to search for")],
    meal: Annotated[
        str, typer.Option("--meal", "-m", help="Meal (breakfast/lunch/dinner/snacks)")
    ] = "snacks",
    servings: Annotated[float, typer.Option(help="Number of servings")] = 1.0,
    pick: Annotated[
        int | None, typer.Option(help="Auto-pick the Nth search result (1-indexed)")
    ] = None,
    on_date: Annotated[
        str | None, typer.Option("--date", help="Target date YYYY-MM-DD (default: today)")
    ] = None,
) -> None:
    """Search for a food and log it to a meal."""
    if meal not in MEAL_TYPES:
        typer.secho(
            f"meal must be one of {sorted(MEAL_TYPES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    when = parse_date_arg(on_date)
    with _open_client() as client:
        results = foods.search(client.http, query)
        _print_search_results(results)
        if not results:
            raise typer.Exit(code=1)
        idx = _resolve_pick(pick, "Select food #", len(results))
        selected = results[idx]
        unsaved = foods.get_unsaved_food_log_entry(client.http, selected)
        day_num = day_number_for(when)
        day_key = get_daydate_key(client.http, day_num) or ""
        meal_ord = MEAL_TYPES[meal]
        entries.log_food(
            client.http,
            unsaved,
            meal_ord,
            day_key,
            day_num,
            servings,
        )
        typer.secho(
            f"✅ Logged {selected.name} → {MEAL_NAMES[meal_ord]} × {servings}",
            fg=typer.colors.GREEN,
        )


@app.command()
def diary(
    on_date: Annotated[
        str | None, typer.Option("--date", help="Target date YYYY-MM-DD (default: today)")
    ] = None,
) -> None:
    """List the diary for a given date (default: today)."""
    when = parse_date_arg(on_date)
    with _open_client() as client:
        es = daily.get_daily_details(client.http, when)
        _print_diary(es, when)


@app.command()
def delete(
    meal: Annotated[str, typer.Option("--meal", "-m", help="Meal to delete from")],
    pick: Annotated[
        int | None,
        typer.Option(help="1-based index of entry within the meal (run `diary` to list)"),
    ] = None,
    on_date: Annotated[
        str | None, typer.Option("--date", help="Target date YYYY-MM-DD (default: today)")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the type-to-confirm prompt"),
    ] = False,
) -> None:
    """Delete a diary entry by meal + index."""
    if meal not in MEAL_TYPES:
        typer.secho(
            f"meal must be one of {sorted(MEAL_TYPES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    when = parse_date_arg(on_date)
    meal_ord = MEAL_TYPES[meal]
    with _open_client() as client:
        es = daily.get_daily_details(client.http, when)
        if not es:
            typer.secho(
                f"❌ No diary entries for {when.isoformat()}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(code=1)
        meal_es = [e for e in es if e.meal_ordinal == meal_ord]
        if not meal_es:
            typer.secho(
                f"❌ No entries in {MEAL_NAMES[meal_ord]} on {when.isoformat()}",
                fg=typer.colors.RED,
                err=True,
            )
            _print_diary(es, when)
            raise typer.Exit(code=1)
        if pick is None:
            _print_diary(es, when)
            typer.echo(
                f"\nUse --pick N to choose an entry from {MEAL_NAMES[meal_ord]} (1..{len(meal_es)})"
            )
            raise typer.Exit(code=1)
        idx = _resolve_pick(pick, "Pick", len(meal_es))
        target = meal_es[idx]
        brand_str = f" ({target.food_brand})" if target.food_brand else ""
        typer.echo(
            f"🗑️  Deleting from {MEAL_NAMES[meal_ord]}: "
            f"{target.food_name}{brand_str} × {target.servings}"
        )
        if not yes:
            ans = typer.prompt("Confirm? type 'delete' to proceed", default="", show_default=False)
            if ans.strip().lower() != "delete":
                typer.echo("Cancelled.")
                raise typer.Exit(code=0)
        entries.delete(client.http, target)
        typer.secho("✅ Deleted", fg=typer.colors.GREEN)


@app.command()
def whoami() -> None:
    """Print the resolved client configuration."""
    with _open_client() as client:
        cfg = client.config
        typer.echo(f"user_id        : {cfg.user_id}")
        typer.echo(f"user_name      : {cfg.user_name}")
        typer.echo(f"hours_from_gmt : {cfg.hours_from_gmt}")
        typer.echo(f"policy_hash    : {cfg.policy_hash}")
        typer.echo(f"strong_name    : {cfg.strong_name}")


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main() -> None:  # used by the `lose-it-utils` script entry point
    app()


if __name__ == "__main__":
    main()
