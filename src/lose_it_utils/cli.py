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
import webbrowser
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from ._debug_cli import debug_app
from ._logging import configure as _configure_logging
from ._logging import logger
from .client import Client, MissingConfigError, daily, entries, foods
from .client._config import (
    DEFAULT_SERVING_SIZE_GRAMS,
    GRAMS_MEASURE_ORDINAL,
    MEAL_NAMES,
    MEAL_TYPES,
    measure_name,
)
from .client._dates import day_number_for, parse_date_arg
from .client._settings import DEFAULT_CONFIG_FILE, write_yaml_config
from .client.auth import (
    DEFAULT_TOKEN_FILE,
    SIGNIN_URL,
    decode_jwt_exp,
    extract_user_info_from_jwt,
    extract_user_name_from_cookies,
    is_token_expired,
    load_cookies_from_browser,
    refresh_token_from_browser,
    save_token,
)
from .client.init import get_daydate_key


class OutputFormat(enum.StrEnum):
    """How to render a command's result."""

    text = "text"
    json = "json"


class Browser(enum.StrEnum):
    """Browsers we can import the ``liauth`` cookie from."""

    chrome = "chrome"
    brave = "brave"


class LogLevel(enum.StrEnum):
    """Verbosity levels accepted by ``--log-level``.

    ``trace`` is the loudest: every GWT-RPC request and response — full
    headers, cookies, and bodies — is dumped to the sink. Use it to
    reverse-engineer the API or to capture a session for offline replay.
    """

    trace = "trace"
    debug = "debug"
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"
    critical = "critical"


app = typer.Typer(
    name="lose-it",
    help="Unofficial Lose It! food logger / diary CLI.",
    no_args_is_help=True,
    add_completion=False,
)

# `lose-it debug parse-response …` — replay captured GWT-RPC bodies
# through the parser without re-firing the RPC. See ``_debug_cli.py``.
app.add_typer(debug_app)


# ── Top-level callback: global config + output options ──────────────────────


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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            help=(
                "Path to the YAML config file. Default: "
                "~/.config/loseit/config.yaml. Also overridable via "
                "LOSEIT_CONFIG_FILE."
            ),
        ),
    ] = None,
    user_id: Annotated[
        str | None,
        typer.Option(
            "--user-id",
            help='Override LOSEIT_USER_ID / YAML "user_id".',
        ),
    ] = None,
    user_name: Annotated[
        str | None,
        typer.Option(
            "--user-name",
            help='Override LOSEIT_USER_NAME / YAML "user_name".',
        ),
    ] = None,
    hours_from_gmt: Annotated[
        int | None,
        typer.Option(
            "--hours-from-gmt",
            help='Override LOSEIT_HOURS_FROM_GMT / YAML "hours_from_gmt".',
        ),
    ] = None,
    policy_hash: Annotated[
        str | None,
        typer.Option(
            "--policy-hash",
            help='Override LOSEIT_POLICY_HASH / YAML "policy_hash".',
        ),
    ] = None,
    strong_name: Annotated[
        str | None,
        typer.Option(
            "--strong-name",
            help='Override LOSEIT_STRONG_NAME / YAML "strong_name".',
        ),
    ] = None,
    log_level: Annotated[
        LogLevel | None,
        typer.Option(
            "--log-level",
            help=(
                "Verbosity for logs emitted on stderr. Default: muted. "
                "`trace` dumps every GWT-RPC request + response, including "
                "headers, cookies, and full payloads — useful for mapping "
                "the API surface. `debug` keeps one-liners per call; `info` "
                "logs high-level CLI events only."
            ),
            envvar="LOSEIT_LOG_LEVEL",
        ),
    ] = None,
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            help=(
                "Write a full TRACE-level log of the session to this file. "
                "Captures every request and response payload regardless of "
                "the console --log-level — designed for offline analysis "
                "and reverse-engineering."
            ),
            envvar="LOSEIT_LOG_FILE",
        ),
    ] = None,
) -> None:
    """Set up the per-invocation context.

    The global config flags here form the highest-priority layer
    (CLI > env > YAML > defaults). Only flags the user explicitly passes
    are forwarded to ``Config.from_env`` so that unset flags do not shadow
    lower-priority sources.
    """
    _configure_logging(
        level=log_level.value if log_level is not None else None,
        log_file=log_file,
    )
    if log_level is not None or log_file is not None:
        logger.debug(
            "cli invoked: command={cmd!r} log_level={lvl} log_file={lf}",
            cmd=ctx.invoked_subcommand,
            lvl=log_level.value if log_level else None,
            lf=str(log_file) if log_file else None,
        )
    ctx.ensure_object(dict)
    ctx.obj["output"] = output
    ctx.obj["config_overrides"] = {
        "config_file": config_file,
        "user_id": user_id,
        "user_name": user_name,
        "hours_from_gmt": hours_from_gmt,
        "policy_hash": policy_hash,
        "strong_name": strong_name,
    }


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


def _open_client(ctx: typer.Context | None = None) -> Client:
    """Build a Client from the layered sources; print a friendly error if missing.

    When called with a Typer context, any ``--user-id``/``--user-name``/…
    CLI flags stashed in ``ctx.obj["config_overrides"]`` are forwarded as
    the highest-priority config layer.
    """
    overrides: dict[str, object] = {}
    if ctx is not None and ctx.obj:
        overrides = ctx.obj.get("config_overrides") or {}
    try:
        return Client.from_env(**overrides)
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
    logger.info("cli.search: query={q!r} output={o}", q=query, o=fmt.value)
    with _open_client(ctx) as client:
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
    servings: Annotated[
        float,
        typer.Option(
            help=(
                "Number of servings (multiplier on the food's default serving "
                "size). For gram-measured foods (ord=8) 1 serving = 100 g — "
                "use --grams instead for a more natural interface."
            ),
        ),
    ] = 1.0,
    grams: Annotated[
        float | None,
        typer.Option(
            "--grams",
            "-g",
            help=(
                "Quantity in grams. Only valid when the picked food's measure "
                "unit is grams; equivalent to --servings (grams / 100)."
            ),
        ),
    ] = None,
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
    logger.info(
        "cli.log: query={q!r} meal={m} servings={s} grams={g} pick={p} date={d!r} dry_run={dr}",
        q=query,
        m=meal,
        s=servings,
        g=grams,
        p=pick,
        d=on_date,
        dr=dry_run,
    )
    if meal not in MEAL_TYPES:
        typer.secho(
            f"meal must be one of {sorted(MEAL_TYPES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    when = parse_date_arg(on_date)
    with _open_client(ctx) as client:
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

        # Resolve servings vs grams. --grams requires the food to be
        # gram-measured; otherwise log to a different food entry.
        measure_ord = unsaved.food_measure_ordinal
        if grams is not None:
            if measure_ord != GRAMS_MEASURE_ORDINAL:
                msg = (
                    f"--grams was passed but {selected.name!r} is measured in "
                    f"'{measure_name(measure_ord)}', not grams. Pick a "
                    f"gram-measured entry (use --pick after `lose-it search` "
                    f"to inspect candidates) or drop --grams."
                )
                if fmt is OutputFormat.json:
                    _emit_json(
                        {
                            "error": "not_gram_measured",
                            "food": selected.name,
                            "measure_unit": measure_name(measure_ord),
                            "message": msg,
                        }
                    )
                else:
                    typer.secho(f"❌ {msg}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            servings = grams / DEFAULT_SERVING_SIZE_GRAMS

        day_num = day_number_for(when)
        meal_ord = MEAL_TYPES[meal]
        per_serving_cal = (unsaved.nutrients or {}).get(0)
        scaled_cal = (per_serving_cal * servings) if per_serving_cal is not None else None

        # The portion size the official Lose It! UI will display next to the
        # measure-unit name — for grams this is the literal gram count, for
        # everything else it's the # of servings (= 1 each, 1 serving, …).
        unit = measure_name(measure_ord)
        if measure_ord == GRAMS_MEASURE_ORDINAL:
            portion_size = servings * DEFAULT_SERVING_SIZE_GRAMS
            portion_str = f"{portion_size:g} {unit}"
        else:
            portion_size = servings
            portion_str = f"{servings:g} {unit}"

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
                    "portion_size": portion_size,
                    "measure_unit": unit,
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
                f"{prefix} {selected.name} → {MEAL_NAMES[meal_ord]} {portion_str}{cal_str}",
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
    logger.info("cli.diary: date={d}", d=when.isoformat())
    with _open_client(ctx) as client:
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
    logger.info(
        "cli.delete: meal={m} pick={p} date={d!r} yes={y} dry_run={dr}",
        m=meal,
        p=pick,
        d=on_date,
        y=yes,
        dr=dry_run,
    )
    if meal not in MEAL_TYPES:
        typer.secho(
            f"meal must be one of {sorted(MEAL_TYPES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    when = parse_date_arg(on_date)
    meal_ord = MEAL_TYPES[meal]
    with _open_client(ctx) as client:
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
def login(
    ctx: typer.Context,
    browser: Annotated[
        Browser,
        typer.Option(
            "--browser",
            "-b",
            help="Which browser to read the liauth cookie from.",
        ),
    ] = Browser.chrome,
    token_file: Annotated[
        Path,
        typer.Option(
            "--token-file",
            help="Where to write the imported JWT.",
            envvar="LOSEIT_TOKEN_FILE",
        ),
    ] = DEFAULT_TOKEN_FILE,
    config_file: Annotated[
        Path,
        typer.Option(
            "--write-config-to",
            help="Where to write the resolved YAML config.",
            envvar="LOSEIT_CONFIG_FILE",
        ),
    ] = DEFAULT_CONFIG_FILE,
    user_name_override: Annotated[
        str | None,
        typer.Option(
            "--user-name",
            help="Override the loseit.com username instead of prompting / sniffing cookies.",
        ),
    ] = None,
    write_config: Annotated[
        bool,
        typer.Option(
            "--write-config/--no-write-config",
            help=(
                "After importing the JWT, populate the YAML config file with the "
                "resolved user_id (JWT `sub`), user_name (from JWT/cookies/prompt), "
                "and hours_from_gmt (from the OS timezone). Default: on."
            ),
        ),
    ] = True,
    open_signin: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="If the cookie is missing/expired, open the Lose It! signin page.",
        ),
    ] = True,
) -> None:
    """Import the liauth JWT *and* populate the YAML config so the CLI is ready to use.

    Assuming you're already logged into loseit.com in the chosen browser,
    this command:

    1. Extracts the ``liauth`` cookie and writes it to ``token_file``.
    2. Sanity-checks the JWT's ``exp`` claim.
    3. Derives ``user_id`` from the JWT ``sub`` claim, looks for a
       ``user_name`` in either the JWT payload or the browser's other
       loseit.com cookies, and computes ``hours_from_gmt`` from the
       system timezone (DST-aware).
    4. Writes those values to the YAML config file (default
       ``~/.config/loseit/config.yaml``) so subsequent commands don't
       need any ``LOSEIT_*`` env vars.

    Pass ``--user-name`` to skip the username sniff/prompt. Pass
    ``--no-write-config`` to import only the token.
    """
    fmt = _output_format(ctx)
    logger.info(
        "cli.login: browser={b} token_file={tf} config_file={cf} write_config={wc}",
        b=browser.value,
        tf=str(token_file),
        cf=str(config_file),
        wc=write_config,
    )
    name = browser.value
    token = refresh_token_from_browser(name)

    if token is None:
        _login_failure(
            fmt=fmt,
            browser=name,
            reason="missing",
            token_file=token_file,
            open_signin=open_signin,
            message=f"No liauth cookie found in {name.title()} for loseit.com.",
        )
        return

    exp = decode_jwt_exp(token)
    if is_token_expired(token):
        _login_failure(
            fmt=fmt,
            browser=name,
            reason="expired",
            token_file=token_file,
            open_signin=open_signin,
            message=f"liauth cookie in {name.title()} is expired.",
            exp=exp,
        )
        return

    save_token(token, token_file)

    written_config: Path | None = None
    written_values: dict[str, Any] = {}
    if write_config:
        written_values, written_config = _populate_config_from_login(
            token=token,
            browser_name=name,
            user_name_override=user_name_override,
            config_file=config_file,
            interactive=fmt is OutputFormat.text,
        )

    if fmt is OutputFormat.json:
        _emit_json(
            {
                "action": "login",
                "status": "ok",
                "browser": name,
                "token_file": str(token_file),
                "exp": exp,
                "exp_iso": _exp_iso(exp),
                "config_file": str(written_config) if written_config else None,
                "config_values": written_values or None,
            }
        )
    else:
        typer.secho(
            f"✅ Imported liauth from {name.title()} → {token_file}",
            fg=typer.colors.GREEN,
        )
        if exp is not None:
            typer.echo(f"   JWT exp: {_exp_iso(exp)}")
        if written_config:
            typer.secho(f"✅ Wrote config → {written_config}", fg=typer.colors.GREEN)
            for k, v in written_values.items():
                typer.echo(f"   {k:14}: {v}")
        elif write_config:
            # Got here because the values couldn't be resolved and the user
            # is non-interactive (json mode or piped) — explain.
            typer.secho(
                "⚠️  Skipped writing config: could not resolve user_name "
                "non-interactively. Pass --user-name or run in text mode.",
                fg=typer.colors.YELLOW,
                err=True,
            )


def _detect_hours_from_gmt() -> int:
    """Return the current local UTC offset in whole hours (DST-aware)."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    # Floor-division by 3600 would skew negative offsets (-21600 // 3600 = -6,
    # which is correct, but e.g. -19800 // 3600 = -6 too — India's :30 offsets
    # round to the nearest hour). Use int(round(...)) so :30 zones land on
    # whichever hour they're closer to.
    return round(offset.total_seconds() / 3600)


def _populate_config_from_login(
    *,
    token: str,
    browser_name: str,
    user_name_override: str | None,
    config_file: Path,
    interactive: bool,
) -> tuple[dict[str, Any], Path | None]:
    """Resolve user_id / user_name / hours_from_gmt and write the YAML file.

    Returns ``(values_written, path)`` on success, ``({}, None)`` if a
    required value couldn't be resolved (only happens when
    ``user_name_override`` is unset, the JWT and cookies don't expose a
    username, and ``interactive`` is False — i.e. JSON mode or non-TTY).
    """
    info = extract_user_info_from_jwt(token)

    # user_name: explicit flag > JWT claim > browser-cookie sniff > prompt
    user_name = user_name_override or info.get("user_name")
    if not user_name:
        cookies = load_cookies_from_browser(browser_name)
        user_name = extract_user_name_from_cookies(cookies)
    if not user_name and interactive:
        try:
            user_name = typer.prompt("Lose It! username (the email you sign in with)")
        except (typer.Abort, EOFError):
            user_name = None
    if not user_name:
        return {}, None

    values: dict[str, Any] = {
        "user_name": user_name.strip(),
        "hours_from_gmt": _detect_hours_from_gmt(),
    }
    if "user_id" in info:
        values["user_id"] = info["user_id"]

    written = write_yaml_config(config_file, values)
    return values, written


def _login_failure(
    *,
    fmt: OutputFormat,
    browser: str,
    reason: str,
    token_file: Path,
    open_signin: bool,
    message: str,
    exp: int | None = None,
) -> None:
    """Common error path for the ``login`` command: print, open browser, exit 1."""
    opened = False
    if open_signin:
        opened = _open_in_browser(SIGNIN_URL, browser)

    if fmt is OutputFormat.json:
        _emit_json(
            {
                "action": "login",
                "status": reason,
                "browser": browser,
                "token_file": str(token_file),
                "exp": exp,
                "exp_iso": _exp_iso(exp),
                "signin_url": SIGNIN_URL,
                "opened_browser": opened,
                "message": message,
            }
        )
    else:
        typer.secho(f"❌ {message}", fg=typer.colors.RED, err=True)
        if exp is not None:
            typer.echo(f"   JWT exp: {_exp_iso(exp)} (now: {_exp_iso(int(_now()))})", err=True)
        if opened:
            typer.echo(f"   Opened {SIGNIN_URL} in {browser.title()}.", err=True)
        else:
            typer.echo(f"   Sign in here: {SIGNIN_URL}", err=True)
        typer.echo(f"   Then re-run: lose-it login --browser {browser}", err=True)
    raise typer.Exit(code=1)


def _open_in_browser(url: str, browser: str) -> bool:
    """Open ``url`` in the named browser; fall back to the system default."""
    import shutil
    import subprocess
    import sys

    if sys.platform == "darwin":
        app_name = {"chrome": "Google Chrome", "brave": "Brave Browser"}.get(browser)
        if app_name:
            try:
                subprocess.run(["open", "-a", app_name, url], check=True)
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
    elif shutil.which(browser):
        try:
            subprocess.Popen([browser, url])
            return True
        except OSError:
            pass
    return webbrowser.open(url)


def _now() -> float:
    return datetime.now(tz=UTC).timestamp()


def _exp_iso(exp: int | None) -> str | None:
    if exp is None:
        return None
    return datetime.fromtimestamp(exp, tz=UTC).isoformat()


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Print the resolved client configuration."""
    fmt = _output_format(ctx)
    logger.info("cli.whoami: output={o}", o=fmt.value)
    with _open_client(ctx) as client:
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
