"""Typer-based CLI for the Lose It! SDK.

A thin wrapper around :class:`lose_it.LoseIt`. Each subcommand:

1. Parses + CLI-validates flags (mutual exclusion, format choices).
2. Calls a single :class:`LoseIt` method that does all the orchestration
   (search → unsaved → log; diary fetch; describe-food fan-out; login flow).
3. Renders the result — either pretty text via the ``_print_*`` helpers
   or a structured envelope via ``model.to_dict()``.

Subcommands::

    loseit search "tortilla"                              List candidate foods
    loseit log "tortilla" --meal lunch --servings 1.0     Search + log
    loseit log "tortilla" --meal lunch --pick 2           Skip the interactive prompt
    loseit diary                                          Show today's diary
    loseit diary --date 2026-06-08                        Show another day's diary
    loseit describe-food <hex>                            Inspect a food's full profile
    loseit delete --meal lunch --pick 1                   Delete an entry by index
    loseit delete --meal lunch --pick 1 --yes             Skip the confirmation prompt
    loseit login                                          Import liauth from browser cookies
    loseit whoami                                         Print resolved config

Two global options are honored by every subcommand:

- ``--output text|json|toon`` (alias ``-o``) — emit either the default
  human-friendly text, a JSON document suitable for piping into ``jq``, or a
  `Token-Oriented Object Notation <https://toonformat.dev>`_ document, which
  carries the same data shape as JSON but uses ~40-60% fewer tokens — handy
  when piping results into an LLM.
- ``--dry-run`` (applies to ``log`` and ``delete`` only) — perform the read-only
  lookups, then print what *would* be logged / deleted without making the
  mutating RPC call.

All commands honor the ``LOSEIT_*`` env vars for configuration (see
``Config.from_env``) and read the JWT token from ``~/.config/loseit/token``
(or ``$LOSEIT_TOKEN`` if set).
"""

from __future__ import annotations

import asyncio
import enum
import json
import webbrowser
from datetime import date as _date
from datetime import timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated, Any

import toon_format
import typer

from ._logging import configure as _configure_logging
from ._logging import logger
from .client import LoseIt
from .core._config import MEAL_NAMES, MissingConfigError
from .core._dates import parse_date_arg
from .core._portion import PortionError, validate_portion_args
from .core._settings import DEFAULT_CONFIG_FILE
from .core.auth import DEFAULT_TOKEN_FILE
from .enums import MealType, ServingUnit
from .models import FoodLogEntry, FoodSearchResult

# Lose It! signin URL — surfaced when login fails so the user can re-auth.
_SIGNIN_URL = "https://www.loseit.com/"

# Project identity — surfaced by ``loseit version`` / ``loseit --version``.
_PROJECT_REPO = "https://github.com/phitoduck/lose-it"
_PROJECT_LICENSE = "MIT"

# Default backup root (spec §2 / §3.1). Expanded eagerly so ``--help``
# renders the absolute path the user will actually see on disk rather than
# a literal ``~``.
DEFAULT_BACKUP_ROOT = Path("~/.config/loseit/backup").expanduser()


def _resolve_version() -> str:
    """Return the installed package version, or ``"unknown"`` as a last resort.

    Reads from the package's distribution metadata (populated by hatchling
    from ``version.txt`` at build time). Falls back to reading ``version.txt``
    directly only when the package is being run from a source tree that
    somehow isn't installed — this keeps ``python -m lose_it.cli version``
    informative during local hacking.
    """
    try:
        return _pkg_version("lose-it")
    except PackageNotFoundError:
        pass
    version_txt = Path(__file__).resolve().parents[2] / "version.txt"
    if version_txt.is_file():
        return version_txt.read_text().strip()
    return "unknown"


def _format_version_text(ver: str) -> str:
    """Human-readable ``version`` output. Single source of truth for the layout."""
    return (
        f"loseit {ver}\n"
        f"Release: {_PROJECT_REPO}/releases/tag/v{ver}\n"
        f"License: {_PROJECT_LICENSE}\n"
        "\n"
        "This project is unaffiliated with Lose It! / FitNow, Inc.\n"
        "Thank you for using it!"
    )


def _version_payload(ver: str) -> dict[str, str]:
    """Structured (`json`/`toon`) payload for the ``version`` subcommand."""
    return {
        "version": ver,
        "release_url": f"{_PROJECT_REPO}/releases/tag/v{ver}",
        "license": _PROJECT_LICENSE,
        "disclaimer": "This project is unaffiliated with Lose It! / FitNow, Inc.",
        "thanks": "Thank you for using it!",
    }


class OutputFormat(enum.StrEnum):
    """How to render a command's result.

    Genuinely CLI-only: the SDK returns model objects, the CLI decides
    whether to render them as human-friendly text, JSON, or TOON.
    """

    text = "text"
    json = "json"
    toon = "toon"


class Browser(enum.StrEnum):
    """Browsers we can import the ``liauth`` cookie from.

    CLI-only — Lose It! itself doesn't care which browser sourced the
    cookie; this enum exists so ``--browser`` shows a typed choice list
    in ``--help``.
    """

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
    name="loseit",
    help="Unofficial Lose It! food logger / diary CLI.",
    no_args_is_help=True,
    add_completion=False,
)


# ── Top-level callback: global config + output options ──────────────────────


def _version_callback(value: bool) -> None:
    """Eager ``--version`` handler — prints + exits before subcommand dispatch.

    Mirrors the ``version`` subcommand but always emits the human-readable
    block. Use ``loseit version --output json`` for structured output.
    """
    if not value:
        return
    typer.echo(_format_version_text(_resolve_version()))
    raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version, repo URL, license, and disclaimer; then exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            "-o",
            help="Output format. `text` (default) is human-friendly; "
            "`json` emits a script-friendly JSON document on stdout; "
            "`toon` emits the same data as Token-Oriented Object Notation, "
            "a compact JSON-equivalent that uses ~40-60% fewer LLM tokens.",
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
    log_headers: Annotated[
        bool,
        typer.Option(
            "--log-headers/--no-log-headers",
            help=(
                "Include the request/response header + cookie sections in "
                "TRACE-level HTTP dumps. Off by default to save space — the "
                "JWT cookie alone is ~600 bytes per call. Turn on when "
                "investigating a header-/cookie-specific issue."
            ),
            envvar="LOSEIT_LOG_HEADERS",
        ),
    ] = False,
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
        log_headers=log_headers,
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


def _emit_toon(data: Any) -> None:
    """Print ``data`` as a TOON document on stdout.

    Goes through ``json.dumps`` / ``json.loads`` first so the same dataclass
    coercion (and string-keying of int dict keys) that ``--output json`` uses
    applies here too — TOON sees the same canonicalized payload, just rendered
    in fewer tokens.
    """
    canonical = json.loads(json.dumps(data, default=_jsonable))
    typer.echo(toon_format.encode(canonical))


def _emit_structured(fmt: OutputFormat, data: Any) -> None:
    """Dispatch to the right structured-output emitter for ``fmt``.

    ``fmt`` comes first so the ``data`` argument can be a large multi-line
    dict literal at the call site without the format parameter dangling on
    the end. Callers should already have gated this with
    ``fmt is not OutputFormat.text``.
    """
    if fmt is OutputFormat.toon:
        _emit_toon(data)
    else:
        _emit_json(data)


def _jsonable(obj: Any) -> Any:
    """Coerce dataclass-like objects to plain dicts for ``json.dumps``."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    raise TypeError(f"Unserializable type {type(obj).__name__}")


def _open_loseit(ctx: typer.Context | None = None) -> LoseIt:
    """Build a :class:`LoseIt` client from the layered sources.

    When called with a Typer context, any ``--user-id``/``--user-name``/…
    CLI flags stashed in ``ctx.obj["config_overrides"]`` are forwarded as
    the highest-priority config layer. Maps ``FileNotFoundError`` /
    :class:`MissingConfigError` to a coloured error + ``Exit(2)`` so the
    user gets actionable feedback instead of a Python traceback.
    """
    overrides: dict[str, object] = {}
    if ctx is not None and ctx.obj:
        overrides = ctx.obj.get("config_overrides") or {}
    try:
        return LoseIt.from_env(**overrides)
    except FileNotFoundError as e:
        typer.secho(f"❌ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    except MissingConfigError as e:
        typer.secho(f"❌ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e


def _print_search_results(results: list[FoodSearchResult]) -> None:
    """Render the search-results table for ``--output text``."""
    if not results:
        typer.echo("  No results.")
        return
    typer.echo(f"\n{'#':>3}  {'Food':50} {'Brand':20} {'Food ID'}")
    typer.echo(f"{'─' * 3}  {'─' * 50} {'─' * 20} {'─' * 11}")
    for i, f in enumerate(results[:15]):
        name = (f.name or "")[:50]
        brand = (f.brand or "")[:20]
        food_id_short = f"{f.food_id[:10]}…" if f.food_id else ""
        typer.echo(f"{i + 1:>3}  {name:50} {brand:20} {food_id_short}")


def _print_diary(entries: list[FoodLogEntry], when: Any) -> None:
    """Render the diary block (per-meal grouping) for ``--output text``."""
    if not entries:
        typer.echo(f"  (no entries for {when.isoformat()})")
        return
    by_meal: dict[int, list[FoodLogEntry]] = {0: [], 1: [], 2: [], 3: []}
    for e in entries:
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


def _emit_error(fmt: OutputFormat, code: str, message: str, **context: Any) -> None:
    """Emit a structured error envelope (json/toon) or a coloured red line (text).

    Used at the CLI boundary to turn validation failures into the
    ``{"error": code, ...}`` shape today's tests pin.
    """
    if fmt is not OutputFormat.text:
        payload: dict[str, Any] = {"error": code, "message": message}
        payload.update(context)
        _emit_structured(fmt, payload)
    else:
        typer.secho(f"❌ {message}", fg=typer.colors.RED, err=True)


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


# ── Commands ─────────────────────────────────────────────────────────────────


@app.command()
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Free-text search query")],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help=(
                "Include the raw 16-int ``pk_bytes`` array in JSON/TOON output. "
                "Off by default because ``food_id`` (the lowercase-hex form) is "
                "the only identifier the CLI itself accepts — ``--food-id`` and "
                "``describe-food`` both take hex. ``pk_bytes`` only matters when "
                "writing Python code against the SDK."
            ),
        ),
    ] = False,
) -> None:
    """Search the LoseIt food database."""
    fmt = _output_format(ctx)
    logger.info("cli.search: query={q!r} output={o} verbose={v}", q=query, o=fmt.value, v=verbose)
    with _open_loseit(ctx) as li:
        results = li.search(query)
    if fmt is not OutputFormat.text:
        _emit_structured(
            fmt,
            {
                "query": query,
                "count": len(results),
                "results": [r.to_dict(verbose=verbose) for r in results],
            },
        )
    else:
        _print_search_results(results)


@app.command()
def log(
    ctx: typer.Context,
    query: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Free-text search query. Mutually exclusive with --food-id. "
                "Required unless --food-id is given."
            ),
        ),
    ] = None,
    meal: Annotated[
        str, typer.Option("--meal", "-m", help="Meal (breakfast/lunch/dinner/snacks)")
    ] = "snacks",
    servings: Annotated[
        float,
        typer.Option(
            help=(
                "Number of canonical servings (server-side multiplier on the "
                "food's per-serving nutrients). Use --serving-amount + "
                "--serving-unit for unit-based logging (e.g. 61 g, 490 mL)."
            ),
        ),
    ] = 1.0,
    serving_amount: Annotated[
        float | None,
        typer.Option(
            "--serving-amount",
            help=(
                "Quantity in the unit specified by --serving-unit (e.g. 490 "
                "paired with --serving-unit mL, or 61 with --serving-unit g). "
                "Mutually exclusive with --servings; must be passed together "
                "with --serving-unit."
            ),
        ),
    ] = None,
    serving_unit: Annotated[
        ServingUnit | None,
        typer.Option(
            "--serving-unit",
            case_sensitive=False,
            help=(
                "Unit for --serving-amount. Required whenever --serving-amount "
                "is passed (no default — a default like 'g' would silently "
                "misinterpret '--serving-amount 2' for foods natively measured "
                "in 'each' or 'serving')."
            ),
        ),
    ] = None,
    pick: Annotated[
        int | None, typer.Option(help="Auto-pick the Nth search result (1-indexed)")
    ] = None,
    food_id: Annotated[
        str | None,
        typer.Option(
            "--food-id",
            help=(
                "Stable 32-char hex food ID (from `loseit search`'s Food ID "
                "column or JSON `food_id` field). Bypasses the search step and "
                "goes straight to the unsaved-entry RPC. Mutually exclusive "
                "with the positional query and --pick."
            ),
        ),
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
        "cli.log: query={q!r} food_id={fi!r} meal={m} servings={s} "
        "serving_amount={sa} serving_unit={su!r} pick={p} date={d!r} dry_run={dr}",
        q=query,
        fi=food_id,
        m=meal,
        s=servings,
        sa=serving_amount,
        su=serving_unit,
        p=pick,
        d=on_date,
        dr=dry_run,
    )

    # ── Meal validation ─────────────────────────────────────────────────
    try:
        meal_type = MealType.parse(meal)
    except ValueError as exc:
        _emit_error(fmt, "invalid_meal", str(exc))
        raise typer.Exit(code=2) from exc

    # ── Food-selection validation: --food-id vs query/--pick ───────────
    if food_id is not None and (query is not None or pick is not None):
        _emit_error(
            fmt,
            "mutually_exclusive",
            "--food-id and <query>/--pick are mutually exclusive",
        )
        raise typer.Exit(code=2)
    if food_id is None and query is None:
        _emit_error(fmt, "missing_food", "must pass either --food-id or a search query")
        raise typer.Exit(code=2)

    # ── Portion-arg validation ──────────────────────────────────────────
    # Cheap arg-only check; fails before any HTTP. The full check (which
    # needs the food's native unit) runs inside li.log_food.
    try:
        validate_portion_args(servings, serving_amount, serving_unit)
    except PortionError as exc:
        _emit_error(fmt, exc.code, str(exc), **exc.context)
        raise typer.Exit(code=2) from exc

    when = parse_date_arg(on_date)

    with _open_loseit(ctx) as li:
        # ── Resolve the food ────────────────────────────────────────────
        if food_id is not None:
            try:
                selected = li.get_food(food_id)
            except ValueError as exc:
                _emit_error(fmt, "invalid_food_id", str(exc))
                raise typer.Exit(code=2) from exc
            except Exception as exc:
                _emit_error(fmt, "food_not_found", str(exc))
                raise typer.Exit(code=1) from exc
        else:
            assert query is not None  # mutex check above guarantees this
            results = li.search(query)
            if fmt is OutputFormat.text:
                _print_search_results(results)
            if not results:
                if fmt is not OutputFormat.text:
                    _emit_structured(fmt, {"error": "no_results", "query": query})
                raise typer.Exit(code=1)
            idx = _resolve_pick(pick, "Select food #", len(results))
            selected = results[idx]

        # ── Delegate everything else to LoseIt.log_food ─────────────────
        try:
            logged = li.log_food(
                selected,
                meal=meal_type,
                servings=servings,
                serving_amount=serving_amount,
                serving_unit=serving_unit,
                when=when,
                dry_run=dry_run,
            )
        except PortionError as exc:
            _emit_error(fmt, exc.code, str(exc), **exc.context)
            raise typer.Exit(code=2) from exc

    # ── Render result ──────────────────────────────────────────────────
    if fmt is not OutputFormat.text:
        _emit_structured(fmt, logged.to_dict())
    else:
        prefix = "🟡 DRY RUN — would log" if dry_run else "✅ Logged"
        cal_str = f" ({logged.calories:.0f} cal)" if logged.calories is not None else ""
        id_str = f" (id {logged.food.food_id[:4]}…)" if logged.food.food_id else ""
        portion_str = f"{logged.portion_amount:g} {logged.portion_unit}"
        typer.secho(
            f"{prefix} {logged.food.name}{id_str} → {logged.meal_name} {portion_str}{cal_str}",
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
    with _open_loseit(ctx) as li:
        es = li.diary(when)
    if fmt is not OutputFormat.text:
        _emit_structured(
            fmt,
            {
                "date": when.isoformat(),
                "count": len(es),
                "entries": [e.to_dict() for e in es],
            },
        )
    else:
        _print_diary(es, when)


@app.command(name="describe-food")
def describe_food(
    ctx: typer.Context,
    food_ids: Annotated[
        list[str],
        typer.Argument(
            help=(
                "One or more 32-char hex food IDs (from `lose-it search` "
                "output). Each ID's full nutrient + serving-size data is "
                "fetched concurrently and emitted in JSON."
            ),
        ),
    ],
) -> None:
    """Inspect one or more foods by ID; fetch concurrently.

    Each ID is described via :meth:`LoseIt.describe_food` in a thread, so
    N foods take ~max(per-request-latency) rather than sum. Invalid IDs
    or fetch failures surface as ``{"food_id", "error", "message"}`` rows
    rather than killing the whole batch.
    """
    fmt = _output_format(ctx)
    logger.info("cli.describe_food: n={n}", n=len(food_ids))

    def _safe_describe(li: LoseIt, fid: str) -> dict[str, Any]:
        try:
            desc = li.describe_food(fid)
        except ValueError as exc:
            return {"food_id": fid, "error": "invalid_food_id", "message": str(exc)}
        except Exception as exc:
            return {"food_id": fid, "error": "fetch_failed", "message": str(exc)}
        return desc.to_dict()

    async def _describe_many(li: LoseIt) -> list[dict[str, Any]]:
        return await asyncio.gather(
            *(asyncio.to_thread(_safe_describe, li, fid) for fid in food_ids)
        )

    with _open_loseit(ctx) as li:
        results = asyncio.run(_describe_many(li))

    if fmt is not OutputFormat.text:
        _emit_structured(fmt, {"count": len(results), "foods": results})
    else:
        for r in results:
            if r.get("error"):
                typer.secho(
                    f"❌ {r['food_id']}: {r['error']} — {r.get('message', '')}",
                    fg=typer.colors.RED,
                    err=True,
                )
                continue
            typer.secho(f"\n📋 {r['name']}", fg=typer.colors.CYAN, bold=True)
            typer.echo(f"   brand={r['brand']!r} category={r['category']!r}")
            typer.echo(f"   food_id={r['food_id']}")
            ps = r["primary_serving"]
            typer.echo(
                f"   1 serving = {ps['native_qty_per_serving']} {ps['unit']} (ord={ps['ordinal']})"
            )
            cc = r["cross_class_conversion"]
            if cc["per_serving_g"] is not None:
                typer.echo(f"   per_serving_g = {cc['per_serving_g']}")
            if cc["per_serving_ml"] is not None:
                typer.echo(f"   per_serving_ml = {cc['per_serving_ml']}")
            typer.echo("   nutrients:")
            for label, val in r["nutrients_per_serving"].items():
                typer.echo(f"     {label:<24} {val}")


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
    trash_file: Annotated[
        Path | None,
        typer.Option(
            "--trash-file",
            envvar="LOSEIT_TRASH_FILE",
            help=(
                "Override the local trash file path. "
                "Default: ~/.config/loseit/trash.jsonl "
                "(override via $LOSEIT_TRASH_FILE). "
                "The file is created with mode 0o600 on first write."
            ),
        ),
    ] = None,
    print_deleted: Annotated[
        bool,
        typer.Option(
            "--print-deleted/--no-print-deleted",
            help=(
                "Echo the deleted entry to stdout as TOON. The output mirrors "
                "the trash record so an agent's conversation log captures it "
                "even when the local file lives on ephemeral storage."
            ),
        ),
    ] = True,
    no_trash: Annotated[
        bool,
        typer.Option(
            "--no-trash",
            help=(
                "EXPLICIT opt-out — skip the trash sink entirely. Refuses "
                "unless paired with --i-know-this-is-unrecoverable."
            ),
        ),
    ] = False,
    i_know_this_is_unrecoverable: Annotated[
        bool,
        typer.Option(
            "--i-know-this-is-unrecoverable",
            help=(
                "Required acknowledgement for --no-trash. Confirms you "
                "understand the deleted entry will be unrecoverable."
            ),
        ),
    ] = False,
) -> None:
    """Delete a diary entry by meal + index."""
    fmt = _output_format(ctx)
    logger.info(
        "cli.delete: meal={m} pick={p} date={d!r} yes={y} dry_run={dr} "
        "no_trash={nt} trash_file={tf}",
        m=meal,
        p=pick,
        d=on_date,
        y=yes,
        dr=dry_run,
        nt=no_trash,
        tf=str(trash_file) if trash_file else None,
    )

    # --no-trash gating — pre-validate before any RPC or read.
    if no_trash and not i_know_this_is_unrecoverable:
        # Exact stderr lifted from the BDD scenario (impl-plan §6, T5).
        typer.echo("error: refusing to delete without a trash sink", err=True)
        typer.echo("hint:  pass --i-know-this-is-unrecoverable to override", err=True)
        typer.echo("       (this discards any chance of recovering the entry)", err=True)
        raise typer.Exit(code=2)

    try:
        meal_type = MealType.parse(meal)
    except ValueError as exc:
        _emit_error(fmt, "invalid_meal", str(exc))
        raise typer.Exit(code=2) from exc

    when = parse_date_arg(on_date)
    meal_ord = int(meal_type)
    with _open_loseit(ctx) as li:
        es = li.diary(when)
        if not es:
            if fmt is not OutputFormat.text:
                _emit_structured(fmt, {"error": "empty_diary", "date": when.isoformat()})
            else:
                typer.secho(
                    f"❌ No diary entries for {when.isoformat()}",
                    fg=typer.colors.RED,
                    err=True,
                )
            raise typer.Exit(code=1)
        meal_es = [e for e in es if e.meal_ordinal == meal_ord]
        if not meal_es:
            if fmt is not OutputFormat.text:
                _emit_structured(
                    fmt,
                    {
                        "error": "empty_meal",
                        "date": when.isoformat(),
                        "meal": meal_type.name,
                    },
                )
            else:
                typer.secho(
                    f"❌ No entries in {meal_type.name} on {when.isoformat()}",
                    fg=typer.colors.RED,
                    err=True,
                )
                _print_diary(es, when)
            raise typer.Exit(code=1)
        if pick is None:
            if fmt is not OutputFormat.text:
                _emit_structured(
                    fmt,
                    {
                        "error": "missing_pick",
                        "meal": meal_type.name,
                        "candidates": [e.to_dict() for e in meal_es],
                    },
                )
            else:
                _print_diary(es, when)
                typer.echo(
                    f"\nUse --pick N to choose an entry from {meal_type.name} (1..{len(meal_es)})"
                )
            raise typer.Exit(code=1)
        idx = _resolve_pick(pick, "Pick", len(meal_es))
        target = meal_es[idx]
        if fmt is OutputFormat.text:
            brand_str = f" ({target.food_brand})" if target.food_brand else ""
            prefix = "🟡 DRY RUN — would delete" if dry_run else "🗑️  Deleting"
            typer.echo(
                f"{prefix} from {meal_type.name}: {target.food_name}{brand_str} × {target.servings}"
            )
        if not dry_run:
            if not yes and fmt is OutputFormat.text:
                ans = typer.prompt(
                    "Confirm? type 'delete' to proceed", default="", show_default=False
                )
                if ans.strip().lower() != "delete":
                    typer.echo("Cancelled.")
                    raise typer.Exit(code=0)
            # Build the trash sink + invoke delete_entry through the new
            # safety-routed path.
            from .trash import LocalFileTrashSink

            if no_trash:
                # Opt-out path — gated above.
                try:
                    delete_result = li.delete_entry(
                        target,
                        trash_sink=None,
                        acknowledge_no_trash=True,
                    )
                except Exception as exc:  # pragma: no cover - belt+braces
                    _emit_error(fmt, "delete_failed", str(exc))
                    raise typer.Exit(code=2) from exc
            else:
                sink = LocalFileTrashSink(
                    path=trash_file,
                    user_name=li.config.user_name or "",
                )
                try:
                    delete_result = li.delete_entry(target, trash_sink=sink)
                except OSError as exc:
                    # Trash sink failed before the wire delete fired —
                    # the entry is still on the server.
                    sink_path = trash_file if trash_file is not None else sink.path
                    typer.echo(f"error: trash sink: cannot write {sink_path}", err=True)
                    typer.echo(f"cause: {type(exc).__name__}({exc})", err=True)
                    typer.echo(
                        "hint:  the wire delete was NOT sent — your entry is still on the server",
                        err=True,
                    )
                    raise typer.Exit(code=2) from exc

            # Post-delete chatter (text mode): show the sink pointer
            # before the green "✅ Deleted" line.
            if fmt is OutputFormat.text:
                if delete_result.trash_receipts:
                    typer.echo(f"  trash sink: {delete_result.trash_receipts[0].where}")
                    typer.echo("  (run 'loseit restore-trash' to undo the most recent delete)")
                else:
                    typer.echo("  trash sink: <none — caller acknowledged --no-trash>")

    if fmt is not OutputFormat.text:
        envelope: dict[str, Any] = {
            "action": "delete",
            "dry_run": dry_run,
            "date": when.isoformat(),
            "meal": meal_type.name,
            "target": target.to_dict(),
        }
        if not dry_run:
            envelope["trash_receipts"] = [
                {
                    "where": r.where,
                    "stashed_at": r.stashed_at,
                }
                for r in delete_result.trash_receipts
            ]
            envelope["deleted_at"] = delete_result.deleted_at
        _emit_structured(fmt, envelope)
    elif not dry_run:
        typer.secho("✅ Deleted", fg=typer.colors.GREEN)
        if print_deleted:
            # TOON projection of the deleted entry — same as the trash
            # record's ``entry`` block. Top-level ``deleted_entry`` key
            # matches the BDD scenario's expected stdout layout.
            _emit_toon({"deleted_entry": target.to_dict()})


@app.command(name="restore-trash")
def restore_trash(
    ctx: typer.Context,
    trash_file: Annotated[
        Path | None,
        typer.Option(
            "--trash-file",
            envvar="LOSEIT_TRASH_FILE",
            help=(
                "Source trash file. Default: ~/.config/loseit/trash.jsonl "
                "(override via $LOSEIT_TRASH_FILE)."
            ),
        ),
    ] = None,
    line: Annotated[
        int | None,
        typer.Option(
            "--line",
            help="1-based line number to restore. Default: the last line.",
        ),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option(
            "--keep/--consume",
            help=(
                "Keep the trash line after restoring (``--keep``) or remove "
                "it (``--consume``, the default)."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print which line would be restored without re-logging.",
        ),
    ] = False,
) -> None:
    """Re-log the most recent trash record (or ``--line N``)."""
    fmt = _output_format(ctx)
    logger.info(
        "cli.restore_trash: trash_file={tf} line={ln} keep={k} dry_run={dr}",
        tf=str(trash_file) if trash_file else None,
        ln=line,
        k=keep,
        dr=dry_run,
    )
    try:
        with _open_loseit(ctx) as li:
            result = li.restore_trash(
                trash_file=trash_file,
                line=line,
                keep=keep,
                dry_run=dry_run,
            )
    except FileNotFoundError as exc:
        _emit_error(fmt, "trash_file_not_found", str(exc))
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        _emit_error(fmt, "trash_invalid", str(exc))
        raise typer.Exit(code=2) from exc

    if fmt is not OutputFormat.text:
        envelope: dict[str, Any] = {
            "action": "restore_trash",
            "trash_file": result["trash_file"],
            "line_no": result["line_no"],
            "total_lines": result["total_lines"],
            "is_last": result["is_last"],
            "dry_run": result["dry_run"],
            "keep": result["keep"],
            "consumed": result["consumed"],
            "food_id": result["food_id"],
            "food_name": result["food_name"],
            "meal": result["meal"],
            "date": result["date"],
            "servings": result["servings"],
        }
        if result["logged"] is not None:
            envelope["logged"] = result["logged"].to_dict()
        _emit_structured(fmt, envelope)
        return

    # Text mode — exact stdout layout pinned by the BDD scenarios in
    # ``docs/backup-impl-plan.md`` §6 (`loseit restore-trash`).
    line_no = result["line_no"]
    if result["dry_run"]:
        suffix = "(last line)" if result["is_last"] else f"(line {line_no})"
        typer.echo(f"would restore trash#{line_no} {suffix}")
    elif keep:
        typer.echo(f"restoring trash#{line_no} (--keep, line will remain after restore)")
    else:
        suffix = "(last line)" if result["is_last"] else f"(line {line_no})"
        typer.echo(f"restoring trash#{line_no} {suffix}")
    typer.echo(f"  food: {result['food_name']}")
    typer.echo(f"  meal: {result['meal']}")
    typer.echo(f"  date: {result['date']}")
    typer.echo(f"  servings: {result['servings']}")
    typer.echo("")
    if result["dry_run"]:
        typer.echo("no log RPC sent (dry run).")
        return
    new_food_id = result["food_id"]
    if result["logged"] is not None:
        # The wire log_food doesn't yet surface the new entry's PK at
        # this layer — use the food_id we re-logged as a stable handle.
        new_food_id = result["food_id"]
    typer.echo(f"logged successfully (new entry id: {new_food_id})")
    if keep:
        typer.echo(f"trash#{line_no} retained.")
    else:
        typer.echo(f"trash#{line_no} consumed.")


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

    # Only prompt for a username when running interactively (text mode).
    # In json/toon mode an unresolvable username surfaces as a partial result.
    def _prompt_for_username() -> str | None:
        try:
            return typer.prompt("Lose It! username (the email you sign in with)")
        except (typer.Abort, EOFError):
            return None

    result = LoseIt.login_from_browser(
        browser.value,
        token_file=token_file,
        config_file=config_file,
        user_name=user_name_override,
        write_config=write_config,
        prompt_for_username=_prompt_for_username if fmt is OutputFormat.text else None,
    )

    if fmt is not OutputFormat.text:
        _emit_structured(fmt, result.to_dict())
        if result.status != "ok":
            raise typer.Exit(code=1)
        return

    # ── Text rendering ──────────────────────────────────────────────────
    if result.status != "ok":
        typer.secho(f"❌ {result.message}", fg=typer.colors.RED, err=True)
        if result.exp_iso is not None:
            typer.echo(f"   JWT exp: {result.exp_iso}", err=True)
        opened = _open_in_browser(_SIGNIN_URL, browser.value) if open_signin else False
        if opened:
            typer.echo(f"   Opened {_SIGNIN_URL} in {browser.value.title()}.", err=True)
        else:
            typer.echo(f"   Sign in here: {_SIGNIN_URL}", err=True)
        typer.echo(f"   Then re-run: loseit login --browser {browser.value}", err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"✅ Imported liauth from {browser.value.title()} → {token_file}",
        fg=typer.colors.GREEN,
    )
    if result.exp_iso is not None:
        typer.echo(f"   JWT exp: {result.exp_iso}")
    if result.config_file:
        typer.secho(f"✅ Wrote config → {result.config_file}", fg=typer.colors.GREEN)
        for k, v in (result.config_values or {}).items():
            typer.echo(f"   {k:14}: {v}")
    elif write_config:
        # `--write-config` requested but no values resolved — typically because
        # the username couldn't be sniffed and the user didn't fill the prompt.
        typer.secho(
            "⚠️  Skipped writing config: could not resolve user_name "
            "non-interactively. Pass --user-name or run in text mode.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Print the resolved client configuration."""
    fmt = _output_format(ctx)
    logger.info("cli.whoami: output={o}", o=fmt.value)
    with _open_loseit(ctx) as li:
        cfg = li.whoami()
    if fmt is not OutputFormat.text:
        _emit_structured(
            fmt,
            {
                "user_id": cfg.user_id,
                "user_name": cfg.user_name,
                "hours_from_gmt": cfg.hours_from_gmt,
                "policy_hash": cfg.policy_hash,
                "strong_name": cfg.strong_name,
            },
        )
    else:
        typer.echo(f"user_id        : {cfg.user_id}")
        typer.echo(f"user_name      : {cfg.user_name}")
        typer.echo(f"hours_from_gmt : {cfg.hours_from_gmt}")
        typer.echo(f"policy_hash    : {cfg.policy_hash}")
        typer.echo(f"strong_name    : {cfg.strong_name}")


# ── Backup / restore-backup helpers ────────────────────────────────────────


def _parse_grain_kind(value: str) -> str:
    """Validate ``--grain`` value (spec §2: ``day | week | month``)."""
    v = value.lower()
    if v not in ("day", "week", "month"):
        raise typer.BadParameter(f"--grain must be day|week|month (got {value!r})")
    return v


def _parse_date_str(value: str | None, *, name: str) -> _date | None:
    """Parse ``YYYY-MM-DD`` or return None; raise ``typer.BadParameter`` on bad input."""
    if value is None:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{name} must be YYYY-MM-DD (got {value!r})") from exc


def _grain_file_rel(grain_path: Path, root: Path) -> str:
    """Relative path under ``root`` for stdout (e.g. ``2016/02.toon``)."""
    try:
        return str(grain_path.relative_to(root))
    except ValueError:
        return str(grain_path)


def _format_grain_label(grain: Any) -> str:
    """Spec §3.1 row label for a :class:`~lose_it.backup.Grain` — e.g.
    ``2016/02.toon`` for month, ``2016/W07.toon`` for week, ``2016/02/15.toon``
    for day. Lives here (not on Grain itself) because it's pure
    presentation.
    """
    if grain.kind == "month":
        return f"{grain.start.year:04d}/{grain.start.month:02d}.toon"
    if grain.kind == "week":
        iso_year, iso_week, _wd = grain.start.isocalendar()
        return f"{iso_year:04d}/W{iso_week:02d}.toon"
    if grain.kind == "day":
        return (
            f"{grain.start.year:04d}/"
            f"{grain.start.month:02d}/"
            f"{grain.start.day:02d}.toon"
        )
    return f"{grain.start.isoformat()} ({grain.kind})"


@app.command()
def backup(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            envvar="LOSEIT_BACKUP_ROOT",
            help=(
                "Backup root directory. Default: "
                "~/.config/loseit/backup (override via $LOSEIT_BACKUP_ROOT). "
                "One grain file per calendar/ISO unit lives under here."
            ),
        ),
    ] = DEFAULT_BACKUP_ROOT,
    grain: Annotated[
        str,
        typer.Option(
            "--grain",
            help="Granularity of grain files. day|week|month (default month).",
        ),
    ] = "month",
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="First date to fetch (YYYY-MM-DD). Default: discover via probe (§5).",
        ),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(
            "--end",
            help="Last date to fetch (YYYY-MM-DD). Default: today.",
        ),
    ] = None,
    probe_from: Annotated[
        str,
        typer.Option(
            "--probe-from",
            help="Earliest date the start-date probe will consider (default 2015-01-01).",
        ),
    ] = "2015-01-01",
    sleep_seconds: Annotated[
        float,
        typer.Option(
            "--sleep-seconds",
            help="Seconds between per-day fetches (default 1.0).",
        ),
    ] = 1.0,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--no-resume",
            help="Skip grain files already recorded on disk (default --resume).",
        ),
    ] = True,
    refresh_foods: Annotated[
        bool,
        typer.Option(
            "--refresh-foods/--no-refresh-foods",
            help="Re-fetch food descriptions even if cached locally (default off).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the plan and exit. No RPCs sent.",
        ),
    ] = False,
    quiet_skips: Annotated[
        bool,
        typer.Option(
            "--quiet-skips",
            help="Collapse contiguous skip ranges to one line each (spec §3.1).",
        ),
    ] = False,
) -> None:
    """Walk the diary, fetch each grain, write one TOON file per grain (§3.1)."""
    from lose_it.backup import FetchStatus, GrainReport

    fmt = _output_format(ctx)
    grain_kind = _parse_grain_kind(grain)
    start_d = _parse_date_str(start, name="--start")
    end_d = _parse_date_str(end, name="--end")
    probe_from_d = _parse_date_str(probe_from, name="--probe-from") or _date(2015, 1, 1)

    logger.info(
        "cli.backup: root={r} grain={g} start={s} end={e} dry_run={dr} quiet_skips={q}",
        r=str(root),
        g=grain_kind,
        s=start,
        e=end,
        dr=dry_run,
        q=quiet_skips,
    )

    # Collect per-grain reports for the (post-loop) summary block. The
    # text-mode CLI also streams a line as each report arrives so the
    # user gets feedback during the run.
    reports: list[GrainReport] = []
    # State for --quiet-skips contiguous-range collapsing.
    skip_run_first: GrainReport | None = None
    skip_run_last: GrainReport | None = None
    skip_run_days_total = 0
    skip_run_entries_total = 0

    def _flush_skip_run() -> None:
        nonlocal skip_run_first, skip_run_last, skip_run_days_total, skip_run_entries_total
        if skip_run_first is None or skip_run_last is None:
            return
        if skip_run_first is skip_run_last:
            label = _format_grain_label(skip_run_first.grain)
            days = (skip_run_first.grain.end - skip_run_first.grain.start).days + 1
            typer.echo(
                f"skip      {label}   complete ({days} days, {skip_run_first.entries} entries)"
            )
        else:
            first_label = _format_grain_label(skip_run_first.grain)
            last_label = _format_grain_label(skip_run_last.grain)
            # Per the spec §3.1 example, count grains in the contiguous run.
            n_grains = sum(
                1 for r in reports
                if r.status is FetchStatus.skip
                and r.grain.start >= skip_run_first.grain.start
                and r.grain.end <= skip_run_last.grain.end
            )
            typer.echo(
                f"skip      {first_label} .. {last_label}   "
                f"{n_grains} grains complete "
                f"({skip_run_days_total} days, {skip_run_entries_total} entries)"
            )
        skip_run_first = None
        skip_run_last = None
        skip_run_days_total = 0
        skip_run_entries_total = 0

    def _stream(report: GrainReport) -> None:
        """Per-grain progress callback — emits one line in text mode."""
        nonlocal skip_run_first, skip_run_last, skip_run_days_total, skip_run_entries_total
        reports.append(report)
        if fmt is not OutputFormat.text:
            return
        label = _format_grain_label(report.grain)
        days_in_grain = (report.grain.end - report.grain.start).days + 1
        if report.status is FetchStatus.skip:
            if quiet_skips:
                if skip_run_first is None:
                    skip_run_first = report
                skip_run_last = report
                skip_run_days_total += days_in_grain
                skip_run_entries_total += report.entries
                return
            typer.echo(
                f"skip      {label}   complete ({days_in_grain} days, {report.entries} entries)"
            )
            return
        # Non-skip status — flush any pending skip run first.
        if quiet_skips and skip_run_first is not None:
            _flush_skip_run()
        if report.status is FetchStatus.fetch:
            empty = "  (empty month)" if report.entries == 0 and grain_kind == "month" else ""
            typer.echo(
                f"fetch     {label}   {days_in_grain} days  "
                f"[######################]  {report.entries} entries{empty}"
            )
        elif report.status is FetchStatus.fallback:
            typer.echo(
                f"fallback  {label}   succeeded at sub-grain  "
                f"({days_in_grain} days, {report.entries} entries)"
            )

    with _open_loseit(ctx) as li:
        try:
            summary = li.backup(
                root=root,
                grain=grain_kind,
                start=start_d,
                end=end_d,
                probe_from=probe_from_d,
                resume=resume,
                refresh_foods=refresh_foods,
                sleep_seconds=sleep_seconds,
                dry_run=dry_run,
                progress=_stream,
            )
        except ValueError as exc:
            _emit_error(fmt, "backup_invalid", str(exc))
            raise typer.Exit(code=2) from exc

    # Flush any tail skip-run (only matters with --quiet-skips).
    if fmt is OutputFormat.text and quiet_skips:
        _flush_skip_run()

    if fmt is not OutputFormat.text:
        envelope: dict[str, Any] = {
            "action": "backup",
            "root": str(summary.root),
            "grain": grain_kind,
            "dry_run": dry_run,
            "months_total": summary.months_total,
            "months_skipped": summary.months_skipped,
            "months_partial": summary.months_partial,
            "months_fetched": summary.months_fetched,
            "months_fell_back": summary.months_fell_back,
            "days_fetched": summary.days_fetched,
            "days_with_entries": summary.days_with_entries,
            "new_foods_described": summary.new_foods_described,
            "foods_redescribed_today": summary.foods_redescribed_today,
            "archive_size_bytes": summary.archive_size_bytes,
            "grains": [
                {
                    "kind": r.grain.kind,
                    "start": r.grain.start.isoformat(),
                    "end": r.grain.end.isoformat(),
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "days_with_entries": r.days_with_entries,
                    "entries": r.entries,
                }
                for r in reports
            ],
        }
        _emit_structured(fmt, envelope)
        return

    # Text mode summary block (spec §3.1).
    typer.echo("")
    if dry_run:
        typer.echo("plan")
        typer.echo(
            f"  range:              {start or 'discovered'} -> "
            f"{end or 'today'}  ({summary.months_total} grains)"
        )
        typer.echo(f"  grain:              {grain_kind}")
        typer.echo(
            f"  already on disk:    {summary.months_skipped}  (skip)"
        )
        typer.echo(
            f"  partial on disk:    {summary.months_partial}  (partial)"
        )
        typer.echo(
            f"  no file yet:        {summary.months_fetched}  (fetch)"
        )
        typer.echo(f"  root:               {summary.root}")
        typer.echo("no RPCs sent.")
        return

    typer.echo("summary")
    typer.echo(
        f"  months total:        {summary.months_total}"
    )
    typer.echo(
        f"  months skipped:      {summary.months_skipped}"
    )
    typer.echo(
        f"  months partial:      {summary.months_partial}"
    )
    typer.echo(
        f"  months fetched:      {summary.months_fetched}"
    )
    typer.echo(
        f"  months fell back:    {summary.months_fell_back}"
    )
    typer.echo(f"  days fetched:        {summary.days_fetched}")
    typer.echo(f"  days with entries:   {summary.days_with_entries}")
    typer.echo(
        f"  unique foods:        {summary.new_foods_described} new, "
        f"{summary.foods_redescribed_today} re-described today"
    )
    typer.echo(f"  root:                {summary.root}")


@app.command(name="restore-backup")
def restore_backup(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            envvar="LOSEIT_BACKUP_ROOT",
            help=(
                "Backup root directory. Default: same as `backup` "
                "(~/.config/loseit/backup, override via $LOSEIT_BACKUP_ROOT)."
            ),
        ),
    ] = DEFAULT_BACKUP_ROOT,
    grain: Annotated[
        str,
        typer.Option(
            "--grain",
            help="Grain layout to walk. day|week|month (default month).",
        ),
    ] = "month",
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="Earliest grain to restore (YYYY-MM-DD). Default: archive's earliest.",
        ),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(
            "--end",
            help="Latest grain to restore (YYYY-MM-DD). Default: archive's latest.",
        ),
    ] = None,
    skip_restore_on_nonempty_grain_time_ranges: Annotated[
        bool,
        typer.Option(
            "--skip-restore-on-nonempty-grain-time-ranges",
            help=(
                "Cheap mode (spec §7.2): skip any grain whose server "
                "diary has any entries. Default: off (safe-mode upsert)."
            ),
        ),
    ] = False,
    strict_account: Annotated[
        bool,
        typer.Option(
            "--strict-account/--no-strict-account",
            help=(
                "Refuse to restore from a grain file pinned to a different "
                "account. Default: on (spec §8)."
            ),
        ),
    ] = True,
    sleep_seconds: Annotated[
        float,
        typer.Option(
            "--sleep-seconds",
            help="Seconds between log calls (default 1.0).",
        ),
    ] = 1.0,
    upsert_window_minutes: Annotated[
        float,
        typer.Option(
            "--upsert-window-minutes",
            help="Safe-mode match-key fuzz window in minutes (default 10).",
        ),
    ] = 10.0,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the plan and exit. No log_food RPCs sent.",
        ),
    ] = False,
    quiet_skips: Annotated[
        bool,
        typer.Option(
            "--quiet-skips",
            help="Collapse contiguous skip ranges to one line each (cheap mode).",
        ),
    ] = False,
) -> None:
    """Walk grain files in ``root`` and replay missing entries to the server (§3.2)."""
    from lose_it.backup import CheapRestoreGrainReport, SafeRestoreGrainReport

    fmt = _output_format(ctx)
    grain_kind = _parse_grain_kind(grain)
    start_d = _parse_date_str(start, name="--start")
    end_d = _parse_date_str(end, name="--end")
    cheap_mode = skip_restore_on_nonempty_grain_time_ranges

    logger.info(
        "cli.restore_backup: root={r} grain={g} mode={m} dry_run={dr}",
        r=str(root),
        g=grain_kind,
        m="cheap" if cheap_mode else "safe",
        dr=dry_run,
    )

    cheap_reports: list[CheapRestoreGrainReport] = []
    safe_reports: list[SafeRestoreGrainReport] = []

    # --quiet-skips cheap-mode state.
    skip_run_first: CheapRestoreGrainReport | None = None
    skip_run_last: CheapRestoreGrainReport | None = None
    skip_run_days = 0
    skip_run_entries = 0

    def _flush_cheap_skip_run() -> None:
        nonlocal skip_run_first, skip_run_last, skip_run_days, skip_run_entries
        if skip_run_first is None or skip_run_last is None:
            return
        first_label = _grain_file_rel(skip_run_first.grain_path, root)
        last_label = _grain_file_rel(skip_run_last.grain_path, root)
        n_grains = sum(
            1
            for r in cheap_reports
            if r.status == "skip"
            and r.grain_path >= skip_run_first.grain_path
            and r.grain_path <= skip_run_last.grain_path
        )
        typer.echo(
            f"skip      {first_label} .. {last_label}   "
            f"{n_grains} grains complete ({skip_run_days} days scanned, "
            f"{skip_run_entries} entries on disk)"
        )
        skip_run_first = None
        skip_run_last = None
        skip_run_days = 0
        skip_run_entries = 0

    def _stream_cheap(report: CheapRestoreGrainReport) -> None:
        nonlocal skip_run_first, skip_run_last, skip_run_days, skip_run_entries
        cheap_reports.append(report)
        if fmt is not OutputFormat.text:
            return
        label = _grain_file_rel(report.grain_path, root)
        if report.status == "skip":
            if quiet_skips:
                if skip_run_first is None:
                    skip_run_first = report
                skip_run_last = report
                skip_run_days += report.days_scanned
                skip_run_entries += report.entries_in_grain
                return
            hit = report.hit_day.isoformat() if report.hit_day else "?"
            typer.echo(
                f"skip      {label}   {report.days_scanned} days scanned, "
                f"non-empty on {hit} -> skip"
            )
            return
        if quiet_skips and skip_run_first is not None:
            _flush_cheap_skip_run()
        verb = "would log" if dry_run else "to log"
        typer.echo(
            f"restore   {label}   {report.days_scanned} days scanned, all empty  "
            f"({report.entries_in_grain} entries {verb})"
        )

    def _stream_safe(report: SafeRestoreGrainReport) -> None:
        safe_reports.append(report)
        if fmt is not OutputFormat.text:
            return
        label = _grain_file_rel(report.grain_path, root)
        typer.echo(
            f"{label}   {report.days_with_entries} days with entries  "
            f"[######################]"
        )
        logged_note = (
            f"   (logged {report.entries_logged} new entries)"
            if report.entries_logged > 0
            else ""
        )
        typer.echo(
            f"                 present  {report.entries_present:>2}   "
            f"upsert  {report.days_upserted:>2}   empty  {report.days_empty:>2}"
            f"{logged_note}"
        )

    with _open_loseit(ctx) as li:
        user_id = li.config.user_id or ""
        if fmt is OutputFormat.text:
            typer.echo(f"account:              loseit user_id {user_id}")
            typer.echo(f"backup root:          {root}")
            typer.echo(f"grain:                {grain_kind}")
            if cheap_mode:
                typer.echo("mode:                 simple (skip grain on first non-empty day in range)")
                typer.echo("")
                typer.echo("scanning server for existing data...")
            else:
                typer.echo("mode:                 safe (upsert by food_id + modified_at ± window)")
                typer.echo("")

        try:
            summary = li.restore_backup(
                root=root,
                grain=grain_kind,
                start=start_d,
                end=end_d,
                strict_account=strict_account,
                skip_restore_on_nonempty_grain_time_ranges=cheap_mode,
                upsert_window=timedelta(minutes=upsert_window_minutes),
                sleep_seconds=sleep_seconds,
                dry_run=dry_run,
                progress=_stream_cheap if cheap_mode else _stream_safe,
            )
        except ValueError as exc:
            _emit_error(fmt, "restore_invalid", str(exc))
            raise typer.Exit(code=2) from exc

    if fmt is OutputFormat.text and cheap_mode and quiet_skips:
        _flush_cheap_skip_run()

    if fmt is not OutputFormat.text:
        envelope: dict[str, Any] = {
            "action": "restore_backup",
            "mode": "cheap" if cheap_mode else "safe",
            "root": str(summary.root),
            "grain": grain_kind,
            "dry_run": dry_run,
            "grains_scanned": summary.grains_scanned,
            "grains_skipped": summary.grains_skipped,
            "grains_restored": summary.grains_restored,
            "entries_logged": summary.entries_logged,
            "days_scanned": summary.days_scanned,
            "days_fully_present": summary.days_fully_present,
            "days_upserted": summary.days_upserted,
            "entries_already_present": summary.entries_already_present,
        }
        if cheap_mode:
            envelope["grains"] = [
                {
                    "path": str(r.grain_path),
                    "status": r.status,
                    "days_scanned": r.days_scanned,
                    "hit_day": r.hit_day.isoformat() if r.hit_day else None,
                    "entries_in_grain": r.entries_in_grain,
                    "entries_logged": r.entries_logged,
                }
                for r in cheap_reports
            ]
        else:
            envelope["grains"] = [
                {
                    "path": str(r.grain_path),
                    "days_with_entries": r.days_with_entries,
                    "days_present": r.days_present,
                    "days_upserted": r.days_upserted,
                    "days_empty": r.days_empty,
                    "entries_present": r.entries_present,
                    "entries_logged": r.entries_logged,
                }
                for r in safe_reports
            ]
        _emit_structured(fmt, envelope)
        return

    # Text-mode summary block.
    typer.echo("")
    typer.echo("summary")
    typer.echo(f"  grains scanned:       {summary.grains_scanned}")
    if cheap_mode:
        typer.echo(f"  grains skipped:       {summary.grains_skipped}")
        typer.echo(f"  grains restored:      {summary.grains_restored}")
        if dry_run:
            typer.echo(f"  entries to log:       {sum(r.entries_in_grain for r in cheap_reports if r.status == 'restore')}")
        else:
            typer.echo(f"  entries logged:       {summary.entries_logged}")
    else:
        typer.echo(f"  days scanned:         {summary.days_scanned}")
        typer.echo(f"  days fully present:   {summary.days_fully_present}")
        typer.echo(f"  days upserted:        {summary.days_upserted}")
        typer.echo(f"  entries already present: {summary.entries_already_present}")
        typer.echo(f"  entries logged:       {summary.entries_logged}")
    typer.echo(f"  root:                 {summary.root}")


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the CLI version, release URL, license, and disclaimer."""
    fmt = _output_format(ctx)
    ver = _resolve_version()
    if fmt is OutputFormat.text:
        typer.echo(_format_version_text(ver))
    else:
        _emit_structured(fmt, _version_payload(ver))


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main() -> None:  # used by the `loseit` script entry point
    app()


if __name__ == "__main__":
    main()
