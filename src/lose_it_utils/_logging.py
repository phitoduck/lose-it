"""Loguru-based logging for the Lose It! SDK + CLI.

Logging is a cross-cutting concern across the SDK: the HTTP layer
records full request/response payloads at TRACE level (useful for
mapping the GWT-RPC surface), the client modules record one structured
event per RPC at INFO/DEBUG, and the CLI records command entry/exit.

The default verbosity is *muted* — calling ``configure()`` with no
arguments removes every handler so import-time and library usage stay
silent. The CLI's root callback wires ``--log-level`` and ``--log-file``
into this module so end users can opt in.

Levels (loguru's built-in numeric ordering)::

    TRACE    5   full HTTP request/response dumps, including headers + bodies
    DEBUG   10   HTTP one-liners, payload sizes, parser intermediates
    INFO    20   high-level events: searches, logs, logins, diary loads
    SUCCESS 25   mutating RPCs that returned //OK
    WARNING 30   degraded paths (empty diaries, retried lookups, …)
    ERROR   40   HTTP errors, GWT //EX responses, parser failures

The HTTP dump format is intentionally multi-line so a TRACE-level
session can be grepped/scrolled in a terminal and replayed later from
``--log-file`` to reconstruct what the server saw.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from loguru import logger

__all__ = [
    "TRACE_LEVEL",
    "configure",
    "logger",
]

TRACE_LEVEL: Final[str] = "TRACE"

# ── Formats ────────────────────────────────────────────────────────────────

_TERMINAL_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "<level>{message}</level>"
)

_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"

_VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}


def _normalize(level: str | None) -> str | None:
    """Normalize a user-supplied level string; return None to mean muted."""
    if level is None:
        return None
    upper = level.strip().upper()
    if upper not in _VALID_LEVELS:
        raise ValueError(
            f"Invalid --log-level {level!r}. Choose one of: {', '.join(sorted(_VALID_LEVELS))}"
        )
    return upper


def configure(
    *,
    level: str | None = None,
    log_file: Path | str | None = None,
    file_level: str = TRACE_LEVEL,
) -> None:
    """Configure the global loguru logger.

    Parameters
    ----------
    level:
        Console (stderr) level. ``None`` (the default) disables console
        output entirely — useful when the SDK is imported as a library
        or when the CLI is invoked without ``--log-level``.
    log_file:
        Optional path that receives every event at ``file_level``. The
        file format is plain text (no ANSI colors) so it round-trips
        through ``less``/``grep`` cleanly.
    file_level:
        Minimum level recorded to ``log_file``. Defaults to ``TRACE``
        so ``--log-file`` captures the full HTTP wire transcript — the
        whole point of having a file sink is to keep the noisy stuff
        for offline analysis even when the console is set higher.
    """
    level = _normalize(level)
    file_level = _normalize(file_level) or TRACE_LEVEL

    logger.remove()

    if level is not None:
        logger.add(
            sys.stderr,
            level=level,
            format=_TERMINAL_FORMAT,
            backtrace=False,
            diagnose=False,
            enqueue=False,
        )

    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(path),
            level=file_level,
            format=_FILE_FORMAT,
            backtrace=True,
            diagnose=False,
            enqueue=False,
            encoding="utf-8",
        )


# Default: muted. The CLI re-calls ``configure(...)`` from its root
# callback; library users can call it directly to opt into logging.
configure()
