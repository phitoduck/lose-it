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
            li.delete_entry(target)

    if fmt is not OutputFormat.text:
        _emit_structured(
            fmt,
            {
                "action": "delete",
                "dry_run": dry_run,
                "date": when.isoformat(),
                "meal": meal_type.name,
                "target": target.to_dict(),
            },
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
