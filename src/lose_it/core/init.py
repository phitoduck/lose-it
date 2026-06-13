"""``getInitializationData`` RPC — returns user init data including DayDate keys.

Used as a one-shot bootstrap call: parses out the recent day-key mappings
so other RPCs can reference today's day_key without guessing.
"""

from __future__ import annotations

from .._logging import logger
from ._config import Config
from ._gwt import build_envelope, parse_response
from ._http import HttpClient


def build_payload(config: Config) -> str:
    """Build the ``getInitializationData`` GWT-RPC envelope."""
    strings = [
        config.base_url,
        config.policy_hash,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getInitializationData",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.UserId/4281239478",
        config.user_name,
    ]
    data = [
        "1",
        "2",
        "3",
        "4",
        "1",
        "5",
        "5",
        "0",
        "6",
        config.user_id,
        "7",
        str(config.hours_from_gmt),
    ]
    return build_envelope(strings, data)


# Placeholder used when getInitializationData's day-key window doesn't
# include the target day. Empirically the server ignores the day_key
# string entirely — it routes the request using day_num alone — but
# rejects an *empty* day_key with HTTP 500. Any non-empty alphanumeric
# string of the right shape is accepted. Verified 2026-06-12 by sending
# arbitrary "XXXXXX" / "ABCDEFG" alongside historical day_nums and
# receiving byte-identical responses to the real keys for those days.
_FALLBACK_DAY_KEY = "ZZZZZZZ"


def get_init_day_keys(http: HttpClient) -> dict[int, str]:
    """Return every ``{day_num: day_key}`` pair encoded in the init response.

    Issues a single ``getInitializationData`` RPC and walks the token
    stream for adjacent ``(day_num, key_string)`` or
    ``(hours_from_gmt, day_num, key_string)`` triples. That covers both
    the "recent window" pairs the diary uses and any historical pairs
    the server happens to include.

    Used by :class:`lose_it.LoseIt`'s ``diary_range`` to bootstrap the
    day-key cache with a single RPC instead of one per endpoint.
    """
    logger.info("get_init_day_keys: fetching full window")
    text = http.post_rpc(build_payload(http.config))
    tokens, _ = parse_response(text)
    keys: dict[int, str] = {}
    hours_from_gmt = http.config.hours_from_gmt
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if isinstance(a, int) and a >= 5000 and isinstance(b, str) and 4 <= len(b) <= 16:
            keys.setdefault(a, b)
        # Also match (hours_from_gmt, day_num, key) triples.
        if (
            i + 2 < len(tokens)
            and a == hours_from_gmt
            and isinstance(b, int)
            and b >= 5000
            and isinstance(tokens[i + 2], str)
            and 4 <= len(tokens[i + 2]) <= 16
        ):
            keys.setdefault(b, tokens[i + 2])
    logger.debug("get_init_day_keys: parsed {n} day_key pairs", n=len(keys))
    return keys


def get_daydate_key(http: HttpClient, target_day_num: int) -> str:
    """Return the DayDate key associated with ``target_day_num``.

    The response from ``getInitializationData`` includes a window of
    recent day numbers paired with their short string keys (e.g.
    ``Z6mB_lo`` for today). We scan the token stream for adjacency.

    When ``target_day_num`` isn't in the init response's window
    (the user is fetching a date the server didn't pre-cache), we
    return ``_FALLBACK_DAY_KEY``: the server accepts any non-empty
    key and uses ``day_num`` alone to resolve the diary day. This
    is what unlocks historical diary mining.
    """
    logger.info("get_daydate_key: target_day_num={n}", n=target_day_num)
    text = http.post_rpc(build_payload(http.config))
    tokens, _ = parse_response(text)
    for i in range(len(tokens) - 2):
        if tokens[i] == target_day_num and isinstance(tokens[i + 1], str):
            key = tokens[i + 1]
            logger.debug("get_daydate_key: matched day_num→key {n}→{k!r}", n=target_day_num, k=key)
            return key
        if (
            tokens[i] == http.config.hours_from_gmt
            and tokens[i + 1] == target_day_num
            and isinstance(tokens[i + 2], str)
        ):
            key = tokens[i + 2]
            logger.debug(
                "get_daydate_key: matched (hrs, day_num)→key {n}→{k!r}", n=target_day_num, k=key
            )
            return key
    logger.debug(
        "get_daydate_key: no exact key for day_num={n}; using placeholder (server ignores key)",
        n=target_day_num,
    )
    return _FALLBACK_DAY_KEY
