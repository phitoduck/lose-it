"""Single-file pydantic-ai agent that drives the `loseit` CLI via a homelab Ollama model.

Tools (one per non-destructive loseit subcommand): search, log, diary, describe_food, whoami.
Excluded by design: delete (destructive), login (out-of-band auth), version (irrelevant).

LOGGING
-------
Every run produces one log file at `/home/agent/runs/run-YYYYMMDD-HHMMSS.log` containing:
  - The user prompt and system prompt
  - Every tool invocation (args + return value, full strings, untruncated)
  - The complete message history at the end (every model request + response, including
    reasoning text and tool_calls with parsed arguments)
  - Token usage

Mount the runs directory to the host to persist:
  -v /tmp/loseit-agent-runs:/home/agent/runs

Run inside the sandbox Docker image — see Dockerfile and README.md in this directory.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.priv.mlops-club.org/v1")
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
LOSEIT_BIN = os.environ.get("LOSEIT_BIN", "loseit")
RUNS_DIR = Path(os.environ.get("AGENT_RUNS_DIR", "/home/agent/runs"))

# Configure module-level logger — handlers attached in `_setup_logging`.
log = logging.getLogger("agent")


def _setup_logging() -> Path:
    """Create runs dir, attach file + stderr handlers, return the log path."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    log_path = RUNS_DIR / f"run-{ts}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    file_h = logging.FileHandler(log_path, mode="w")
    file_h.setFormatter(fmt)
    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(stderr_h)
    return log_path


def _run_loseit(args: list[str], *, json_output: bool = True) -> str:
    cmd = [LOSEIT_BIN]
    if json_output:
        cmd += ["-o", "json"]
    cmd += args
    log.info("loseit-cmd: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        err = {"error": "loseit_cli_failed", "exit_code": proc.returncode, "stderr": stderr[:2000]}
        # Catch the common "food doesn't support grams" failure and rewrite into an
        # imperative directive — the raw Click error is verbose and the small model
        # tends to "fix" it by switching unit, which gives wrong calories.
        if "doesn't have a tablespoon" in stderr or "doesn't have a teaspoon" in stderr or "unit_not_supported" in stderr:
            err["guidance"] = (
                "STOP. This food entry does not support the user's requested unit. "
                "DO NOT switch to a unit it does support — that converts the wrong way "
                "and gives bad calories. Instead, call `search` again with DIFFERENT KEYWORDS "
                "(add modifiers like 'fresh', 'homemade', 'plain', or remove brand words) "
                "and pick a different `food_id` from the new search results that DOES support "
                "the user's unit."
            )
        log.warning("loseit-failed: %s", json.dumps(err))
        return json.dumps(err)
    return proc.stdout.strip()


def _log_tool(name: str, args: dict[str, Any], result: str) -> None:
    log.info("[tool-call] %s(%s)", name, json.dumps(args, default=str))
    log.info("[tool-return] %s ->\n%s", name, result)


model = OpenAIChatModel(MODEL_NAME, provider=OpenAIProvider(base_url=OLLAMA_BASE_URL, api_key="ollama"))


def _user_today() -> str:
    """The user's local 'today' as YYYY-MM-DD, using `hours_from_gmt` from the loseit config.

    Lose It! stores log entries by UTC date but the CLI's `diary` defaults to user-local date.
    Forcing both sides to the SAME local date by passing `on_date` everywhere avoids the
    'logged today, missing from diary' mismatch.
    """
    from datetime import datetime, timedelta, timezone

    offset_hours = -6  # default fallback (most-likely US Mountain)
    try:
        cfg = Path("/home/agent/.config/loseit/config.yaml").read_text()
        for line in cfg.splitlines():
            if line.strip().startswith("hours_from_gmt"):
                offset_hours = int(line.split(":", 1)[1].strip())
                break
    except Exception:
        pass
    local_now = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return local_now.date().isoformat()


USER_TODAY = _user_today()

SYSTEM_PROMPT = f"""You log meals to Lose It! by driving the `loseit` CLI via tools.

Today (user's local date) is {USER_TODAY}. Always pass `on_date="{USER_TODAY}"` to `log_food` and `diary`.

For EACH food the user mentions:
  1. `search` with the BARE food noun ONLY (e.g. "guacamole", "asparagus") — DO NOT include
     user modifiers like "homemade", "fresh", "cooked" in the query. The DB is full of brand
     entries that match those words and they're often the WORST entries for gram-based logging.
  2. `describe_food` on at least the TOP 3 food_ids from that search (one batched call).
     Mandatory — the #1 result is often a brand entry that doesn't support grams; #2-#3 are
     usually plain entries that DO.
  3. Pick the candidate that supports the user's stated unit:
     - User said grams → entry needs `primary_serving.unit == "grams"` OR
       `cross_class_conversion.per_serving_g != null` (a number, NOT null).
     - If none of those 3 support grams, RE-SEARCH with a different bare noun ("avocado dip",
       "avocado mash") and describe the top 3 again. Up to 3 rounds.
     - Sanity-check calories: lentils ~115/100g, asparagus ~20/100g, potatoes ~70/100g, guacamole ~150/100g.
  4. `log_food` (no dry_run) with the chosen food_id, the user's AMOUNT and UNIT EXACTLY as the user said it
     (do not convert), meal=snacks unless stated, on_date.
  5. After all foods, `diary(on_date="{USER_TODAY}")` and confirm each appears with sane calories.

NEVER convert "100g" to tablespoons, teaspoons, or fluid ounces. NEVER compute serving counts > 30
of any spoon unit — that's always a math error. If you can't find an entry that supports the user's
unit, log the food in its native unit at `servings=1.0` and note the limitation in your summary.

Meal rules: explicit meal wins; time-of-day cue infers; else default to `snacks` — NEVER ask.

Units: "Xg" → serving_amount=X, serving_unit="g". "N cup" → serving_amount=N, serving_unit="cup". Bare "oz" is rejected.

Splitting: "255g evenly split between A, B, C" → log 255/3 ≈ 85g of each.

Be concise. Don't narrate. When you're done, return one line summarizing what you logged."""


agent: Agent[None, str] = Agent(
    model,
    system_prompt=SYSTEM_PROMPT,
    retries=2,
)


@agent.tool_plain
def search(query: str) -> str:
    """Search the Lose It! food database for candidates matching `query`."""
    out = _run_loseit(["search", query])
    _log_tool("search", {"query": query}, out)
    return out


@agent.tool_plain
def describe_food(food_ids: list[str]) -> str:
    """Inspect one or more foods by their hex `food_id`s (concurrently).

    For each food_id, returns:
      - primary_serving: {unit, native_qty_per_serving}
      - cross_class_conversion: {per_serving_g, per_serving_ml} (nullable — tells you
        whether the entry supports gram/mL logging)
      - nutrients_per_serving: {calories, total_fat_g, sat_fat_g, carb_g, fiber_g, protein_g, ...}
    """
    out = _run_loseit(["describe-food", *food_ids])
    _log_tool("describe_food", {"food_ids": food_ids}, out)
    return out


@agent.tool_plain
def log_food(
    food_id: str,
    meal: Literal["breakfast", "lunch", "dinner", "snacks"],
    serving_amount: float | None = None,
    serving_unit: str | None = None,
    servings: float = 1.0,
    on_date: str | None = None,
    dry_run: bool = False,
) -> str:
    """Log a food to the diary. Default behaviour ACTUALLY WRITES the entry — that's the goal.

    Args:
        food_id: The 32-char hex food_id (from `search`).
        meal: breakfast | lunch | dinner | snacks.
        serving_amount: Quantity in `serving_unit` (e.g. 120 grams → 120). Pair with serving_unit.
        serving_unit: One of: g, tsp, tbsp, cup, piece, each, fl_oz, mL, bottle, can, slice,
            serving, scoop, container, pie. Bare "oz" is REJECTED — pick g or fl_oz.
        servings: Use ONLY when not specifying serving_amount/unit (logs `servings` native servings).
        on_date: YYYY-MM-DD for past-dated entries. Default: today.
        dry_run: If True, preview without writing. DEFAULT FALSE — leave it false to actually log.
    """
    # Guardrail: a serving_amount > 30 in tsp/tbsp/fl_oz is almost certainly the model
    # doing unit-conversion math after it failed to find a gram-supporting entry. Block
    # it and force a re-search instead of letting absurd logs land in the diary.
    if serving_unit in {"tsp", "tbsp", "fl_oz"} and serving_amount and serving_amount > 30:
        guidance = (
            f"REFUSING: {serving_amount} {serving_unit} is absurdly high for one food entry. "
            "You almost certainly converted grams or milliliters into spoons after a previous "
            "log_food failed. Don't do that — it gives huge wrong calorie counts. Instead, call "
            "`search` again with a simpler query (just the food name, no user modifiers like "
            "'homemade' or 'fresh') and `describe_food` the FIRST 3 results to find one with "
            "`cross_class_conversion.per_serving_g != null`. Then log in the user's original unit."
        )
        err = {"error": "agent_guardrail_unit_conversion", "guidance": guidance}
        log.warning("guardrail-tripped: %s", json.dumps(err))
        return json.dumps(err)

    args = ["log", "--food-id", food_id, "--meal", meal]
    if serving_amount is not None:
        args += ["--serving-amount", str(serving_amount)]
    if serving_unit:
        args += ["--serving-unit", serving_unit]
    if serving_amount is None and serving_unit is None:
        args += ["--servings", str(servings)]
    if on_date:
        args += ["--date", on_date]
    if dry_run:
        args += ["--dry-run"]
    out = _run_loseit(args, json_output=False)
    _log_tool(
        "log_food",
        {
            "food_id": food_id,
            "meal": meal,
            "serving_amount": serving_amount,
            "serving_unit": serving_unit,
            "servings": servings,
            "on_date": on_date,
            "dry_run": dry_run,
        },
        out,
    )
    return out


@agent.tool_plain
def diary(on_date: str | None = None) -> str:
    """Read the user's diary for a given date (default: today).

    Returns JSON with `date`, `count`, and `entries[]`. Each entry has `food_name`,
    `food_brand`, `food_measure_unit`, `servings`, `meal_ordinal` (0=breakfast 1=lunch
    2=dinner 3=snacks), and `nutrients_by_label: {calories, protein_g, ...}`.
    """
    args = ["diary"]
    if on_date:
        args += ["--date", on_date]
    out = _run_loseit(args)
    _log_tool("diary", {"on_date": on_date}, out)
    return out


@agent.tool_plain
def whoami() -> str:
    """Print resolved Lose It! client configuration."""
    out = _run_loseit(["whoami"])
    _log_tool("whoami", {}, out)
    return out


def _dump_message_history(result: Any) -> None:
    """Pretty-dump the full pydantic-ai conversation to the log."""
    log.info("=" * 72)
    log.info("FINAL MESSAGE HISTORY (%d messages)", len(result.all_messages()))
    log.info("=" * 72)
    for i, msg in enumerate(result.all_messages()):
        kind = type(msg).__name__
        log.info("--- msg[%d] %s ---", i, kind)
        for part in getattr(msg, "parts", []):
            ptype = type(part).__name__
            payload: dict[str, Any] = {"part_type": ptype}
            for attr in ("content", "tool_name", "args", "tool_call_id", "timestamp"):
                if hasattr(part, attr):
                    val = getattr(part, attr)
                    if attr == "args" and isinstance(val, str):
                        try:
                            val = json.loads(val)
                        except Exception:
                            pass
                    payload[attr] = val
            log.info("%s", json.dumps(payload, default=str, indent=2))
    log.info("=" * 72)
    try:
        usage = result.usage()
        log.info("USAGE: %s", usage)
    except Exception as exc:
        log.info("usage-unavailable: %s", exc)


def main() -> None:
    log_path = _setup_logging()
    prompt = " ".join(sys.argv[1:]).strip() or os.environ.get("AGENT_PROMPT", "").strip()
    if not prompt:
        log.error("usage: agent.py <prompt>   (or set AGENT_PROMPT)")
        sys.exit(2)

    log.info("model=%s  endpoint=%s", MODEL_NAME, OLLAMA_BASE_URL)
    log.info("log-file=%s", log_path)
    log.info("system-prompt:\n%s", SYSTEM_PROMPT)
    log.info("user-prompt: %s", prompt)

    # Ollama's OpenAI-compat layer intermittently returns HTTP 400 on assistant messages
    # with empty content + tool_calls. Retry the whole run a couple of times.
    last_exc: Exception | None = None
    result = None
    for attempt in range(3):
        try:
            result = agent.run_sync(prompt)
            break
        except Exception as exc:
            log.warning("agent-attempt-%d-failed: %s", attempt + 1, exc)
            last_exc = exc
    if result is None:
        log.error("agent-run-failed after retries")
        raise last_exc  # type: ignore[misc]

    _dump_message_history(result)
    log.info("FINAL OUTPUT:\n%s", result.output)
    print(result.output)


if __name__ == "__main__":
    main()
