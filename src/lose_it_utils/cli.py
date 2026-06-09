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
    lose-it whoami                                         Print resolved config

Two global options are honored by every subcommand:

- ``--output text|json`` (alias ``-o``) — emit either the default human-friendly
  text or a JSON document suitable for piping into ``jq`` or another tool.
- ``--dry-run`` (applies to ``log`` and ``delete`` only) — perform the read-only
  lookups, then print what *would* be logged / deleted without making the
  mutating RPC call.

All commands honor the ``LOSEIT_*`` env vars for configuration (see
``Config.from_env``) and read the JWT token from ``~/.config/loseit/token``
(or ``$LOSEIT_TOKEN`` if set).
"""

from __future__ import annotations

import enum
import json
from datetime import date
from typing import Annotated, Any

import typer

from .client import Client, MissingConfigError, daily, entries, foods
from .client._config import MEAL_NAMES, MEAL_TYPES
from .client._dates import day_number_for, parse_date_arg
from .client.init import get_daydate_key


class OutputFormat(enum.StrEnum):
    """How to render a command's result."""

    text = "text"
    json = "json"


app = typer.Typer(
    name="lose-it",
    help="Unofficial Lose It! food logger / diary CLI.",
    no_args_is_help=True,
    add_completion=False,
)


# ── Top-level callback: --output / -o is a global option ─────────────────────


@app.callback()
def _root(
    ctx: typer.Context,
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            "-o",
            help="Output format. `text` (default) is human-friendly; "
            "`json` emits a script-friendly JSON document on stdout.",
        ),
    ] = OutputFormat.text,
) -> None:
    """Set up the per-invocation context (output format)."""
    ctx.ensure_object(dict)
    ctx.obj["output"] = output


# ── Helpers ──────────────────────────────────────────────────────────────────


def _output_format(ctx: typer.Context) -> OutputFormat:
    """Pull the resolved --output from the Typer context."""
    return ctx.obj.get("output", OutputFormat.text) if ctx.obj else OutputFormat.text


def _emit_json(data: Any) -> None:
    """Print ``data`` as a pretty-printed JSON document on stdout."""
    typer.echo(json.dumps(data, indent=2, default=_jsonable))


def _jsonable(obj: Any) -> Any:
    """Coerce dataclass-like objects to plain dicts for ``json.dumps``."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    raise TypeError(f"Unserializable type {type(obj).__name__}")


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


def _entry_to_dict(e) -> dict[str, Any]:
    """Project a FoodLogEntry into a JSON-safe dict."""
    return {
        "meal": MEAL_NAMES.get(e.meal_ordinal, f"meal{e.meal_ordinal}"),
        "meal_ordinal": e.meal_ordinal,
        "food_name": e.food_name,
        "food_brand": e.food_brand,
        "food_category": e.food_category,
        "food_identifier_code": e.food_identifier_code,
        "servings": e.servings,
        "calories": e.calories,
        "nutrients": {int(ord_): float(val) for ord_, val in (e.nutrients_ordered or [])},
        "entry_pk": list(e.entry_pk_response),
        "food_pk": list(e.food_pk_response),
        "entry_day_key": e.entry_day_key,
        "context_day_key": e.context_day_key,
        "day_num": e.day_num,
        "food_measure_ordinal": e.food_measure_ordinal,
    }


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
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Free-text search query")],
) -> None:
    """Search the LoseIt food database."""
    fmt = _output_format(ctx)
    with _open_client() as client:
        results = foods.search(client.http, query)
        if fmt is OutputFormat.json:
            _emit_json(
                {
                    "query": query,
                    "count": len(results),
                    "results": [
                        {
                            "name": r.name,
                            "brand": r.brand,
                            "category": r.category,
                            "pk_bytes": list(r.pk_bytes),
                        }
                        for r in results
                    ],
                }
            )
        else:
            _print_search_results(results)


@app.command()
def log(
    ctx: typer.Context,
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print what would be logged without sending the updateFoodLogEntry call.",
        ),
    ] = False,
) -> None:
    """Search for a food and log it to a meal."""
    fmt = _output_format(ctx)
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
        if fmt is OutputFormat.text:
            _print_search_results(results)
        if not results:
            if fmt is OutputFormat.json:
                _emit_json({"error": "no_results", "query": query})
            raise typer.Exit(code=1)
        idx = _resolve_pick(pick, "Select food #", len(results))
        selected = results[idx]
        unsaved = foods.get_unsaved_food_log_entry(client.http, selected)
        day_num = day_number_for(when)
        meal_ord = MEAL_TYPES[meal]
        per_serving_cal = (unsaved.nutrients or {}).get(0)
        scaled_cal = (per_serving_cal * servings) if per_serving_cal is not None else None

        if not dry_run:
            # The day_key lookup is only needed to construct an actual log payload;
            # skip it in dry-run mode so we don't make an unnecessary network call.
            day_key = get_daydate_key(client.http, day_num) or ""
            entries.log_food(client.http, unsaved, meal_ord, day_key, day_num, servings)

        if fmt is OutputFormat.json:
            _emit_json(
                {
                    "action": "log",
                    "dry_run": dry_run,
                    "date": when.isoformat(),
                    "meal": MEAL_NAMES[meal_ord],
                    "meal_ordinal": meal_ord,
                    "servings": servings,
                    "food": {
                        "name": selected.name,
                        "brand": selected.brand,
                        "category": selected.category,
                    },
                    "calories": scaled_cal,
                }
            )
        else:
            prefix = "🟡 DRY RUN — would log" if dry_run else "✅ Logged"
            cal_str = f" ({scaled_cal:.0f} cal)" if scaled_cal is not None else ""
            typer.secho(
                f"{prefix} {selected.name} → {MEAL_NAMES[meal_ord]} × {servings}{cal_str}",
                fg=typer.colors.YELLOW if dry_run else typer.colors.GREEN,
            )


@app.command()
def diary(
    ctx: typer.Context,
    on_date: Annotated[
        str | None, typer.Option("--date", help="Target date YYYY-MM-DD (default: today)")
    ] = None,
) -> None:
    """List the diary for a given date (default: today)."""
    fmt = _output_format(ctx)
    when = parse_date_arg(on_date)
    with _open_client() as client:
        es = daily.get_daily_details(client.http, when)
        if fmt is OutputFormat.json:
            _emit_json(
                {
                    "date": when.isoformat(),
                    "count": len(es),
                    "entries": [_entry_to_dict(e) for e in es],
                }
            )
        else:
            _print_diary(es, when)


@app.command()
def delete(
    ctx: typer.Context,
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print what would be deleted without sending the deleteFoodLogEntry call.",
        ),
    ] = False,
) -> None:
    """Delete a diary entry by meal + index."""
    fmt = _output_format(ctx)
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
            msg = f"No diary entries for {when.isoformat()}"
            if fmt is OutputFormat.json:
                _emit_json({"error": "empty_diary", "date": when.isoformat()})
            else:
                typer.secho(f"❌ {msg}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        meal_es = [e for e in es if e.meal_ordinal == meal_ord]
        if not meal_es:
            if fmt is OutputFormat.json:
                _emit_json(
                    {
                        "error": "empty_meal",
                        "date": when.isoformat(),
                        "meal": MEAL_NAMES[meal_ord],
                    }
                )
            else:
                typer.secho(
                    f"❌ No entries in {MEAL_NAMES[meal_ord]} on {when.isoformat()}",
                    fg=typer.colors.RED,
                    err=True,
                )
                _print_diary(es, when)
            raise typer.Exit(code=1)
        if pick is None:
            if fmt is OutputFormat.json:
                _emit_json(
                    {
                        "error": "missing_pick",
                        "meal": MEAL_NAMES[meal_ord],
                        "candidates": [_entry_to_dict(e) for e in meal_es],
                    }
                )
            else:
                _print_diary(es, when)
                typer.echo(
                    f"\nUse --pick N to choose an entry from "
                    f"{MEAL_NAMES[meal_ord]} (1..{len(meal_es)})"
                )
            raise typer.Exit(code=1)
        idx = _resolve_pick(pick, "Pick", len(meal_es))
        target = meal_es[idx]
        if fmt is OutputFormat.text:
            brand_str = f" ({target.food_brand})" if target.food_brand else ""
            prefix = "🟡 DRY RUN — would delete" if dry_run else "🗑️  Deleting"
            typer.echo(
                f"{prefix} from {MEAL_NAMES[meal_ord]}: "
                f"{target.food_name}{brand_str} × {target.servings}"
            )
        # In dry-run mode we skip the confirmation prompt and the actual delete.
        if not dry_run:
            if not yes and fmt is OutputFormat.text:
                ans = typer.prompt(
                    "Confirm? type 'delete' to proceed", default="", show_default=False
                )
                if ans.strip().lower() != "delete":
                    typer.echo("Cancelled.")
                    raise typer.Exit(code=0)
            entries.delete(client.http, target)
        if fmt is OutputFormat.json:
            _emit_json(
                {
                    "action": "delete",
                    "dry_run": dry_run,
                    "date": when.isoformat(),
                    "meal": MEAL_NAMES[meal_ord],
                    "target": _entry_to_dict(target),
                }
            )
        elif not dry_run:
            typer.secho("✅ Deleted", fg=typer.colors.GREEN)


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Print the resolved client configuration."""
    fmt = _output_format(ctx)
    with _open_client() as client:
        cfg = client.config
        if fmt is OutputFormat.json:
            _emit_json(
                {
                    "user_id": cfg.user_id,
                    "user_name": cfg.user_name,
                    "hours_from_gmt": cfg.hours_from_gmt,
                    "policy_hash": cfg.policy_hash,
                    "strong_name": cfg.strong_name,
                }
            )
        else:
            typer.echo(f"user_id        : {cfg.user_id}")
            typer.echo(f"user_name      : {cfg.user_name}")
            typer.echo(f"hours_from_gmt : {cfg.hours_from_gmt}")
            typer.echo(f"policy_hash    : {cfg.policy_hash}")
            typer.echo(f"strong_name    : {cfg.strong_name}")


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main() -> None:  # used by the `lose-it` script entry point
    app()


if __name__ == "__main__":
    main()
