"""``getInitializationData`` RPC — returns user init data including DayDate keys.

Used as a one-shot bootstrap call: parses out the recent day-key mappings
so other RPCs can reference today's day_key without guessing.
"""
from __future__ import annotations

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
        "1", "2", "3", "4", "1",
        "5", "5", "0", "6", config.user_id, "7", str(config.hours_from_gmt),
    ]
    return build_envelope(strings, data)


def get_daydate_key(http: HttpClient, target_day_num: int) -> str | None:
    """Return the DayDate key associated with ``target_day_num`` (or ``None``).

    The response from ``getInitializationData`` includes a window of recent
    day numbers paired with their short string keys (e.g. ``Z6mB_lo`` for
    today). We scan the token stream for ``(day_num, "<key>")`` adjacency.
    """
    text = http.post_rpc(build_payload(http.config))
    tokens, _ = parse_response(text)
    for i in range(len(tokens) - 2):
        if tokens[i] == target_day_num and isinstance(tokens[i + 1], str):
            return tokens[i + 1]
        if (tokens[i] == http.config.hours_from_gmt
                and tokens[i + 1] == target_day_num
                and isinstance(tokens[i + 2], str)):
            return tokens[i + 2]
    return None
