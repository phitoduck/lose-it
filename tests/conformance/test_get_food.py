"""Conformance tests for ``foods.get_food`` and ``foods._build_get_food_payload``.

The payload-shape test byte-compares the SDK's generated request against
the wire-evidence snippet captured from the official UI on 2026-06-11
(see ``docs/food-id-flow-spec.md``).
"""

from __future__ import annotations

import pytest

from lose_it import Client
from lose_it.client import foods
from lose_it.client._config import Config
from lose_it.client._http import LoseItError

SERVICE_URL = "https://www.loseit.com/web/service"


# Pulled from the spec's wire-evidence snippet. The 16 bytes after
# "10|11|16|" are the on-wire (reversed) PK; response-form pk_bytes is
# the un-reversed view that the SDK speaks internally.
_WIRE_PK_REVERSED = [16, 17, 13, 95, 48, -65, 66, 49, -93, -38, -116, 60, -106, 8, -16, 99]
_PK_RESPONSE_FORM = list(reversed(_WIRE_PK_REVERSED))


@pytest.fixture
def spec_config() -> Config:
    """A Config matching the user-identity values in the wire-evidence snippet."""
    return Config(
        user_id="53539329",
        user_name="eric.riddoch",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )


def test_build_get_food_payload_matches_wire_evidence(spec_config: Config) -> None:
    """Byte-compare the generated envelope against the spec's captured wire.

    The expected envelope below is the spec's "Wire-level evidence" snippet
    verbatim, with ``config.user_id`` (53539329) and the reversed PK bytes
    interpolated. The base URL comes from Config's default
    (``https://d3hsih69yn4d89.cloudfront.net/web/``), matching the actual
    capture.
    """
    payload = foods._build_get_food_payload(spec_config, _PK_RESPONSE_FORM)
    expected = (
        "7|0|11|"
        "https://d3hsih69yn4d89.cloudfront.net/web/|"
        "8F87EC8969F17AE77B6283D3A83F6D4C|"
        "com.loseit.core.client.service.LoseItRemoteService|"
        "getFood|"
        "com.loseit.core.client.service.ServiceRequestToken/1076571655|"
        "com.loseit.core.client.model.interfaces.IPrimaryKey|"
        "java.lang.String/2004016611|"
        "com.loseit.core.client.model.UserId/4281239478|"
        "eric.riddoch|"
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060|"
        "[B/3308590456|"
        "1|2|3|4|3|5|6|7|"
        "5|0|8|53539329|9|-6|"
        "10|11|16|"
        "16|17|13|95|48|-65|66|49|-93|-38|-116|60|-106|8|-16|99|"
        "0|"
    )
    assert payload == expected


def test_build_get_food_payload_rejects_bad_pk(spec_config: Config) -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        foods._build_get_food_payload(spec_config, [1, 2, 3])


def test_get_food_request_envelope(test_client, httpx_mock, fixture_text) -> None:
    """``get_food`` sends a ``getFood`` envelope with the reversed PK bytes.

    Uses an unrelated fixture (``get_unsaved_tortilla.txt``) to stand in for
    the response body since ``getFood`` returns a ``FoodIdentifier`` nested
    in the same way ``getUnsavedFoodLogEntry`` does. The wire-shape assertion
    is on the OUTGOING request, not the parsed response.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_unsaved_tortilla.txt"),
    )
    pk = list(range(16))  # response-form
    foods.get_food(test_client.http, pk)

    sent = httpx_mock.get_request()
    body = sent.content.decode()
    assert "getFood" in body
    # No name/locale strings in the getFood payload (vs getUnsavedFoodLogEntry).
    assert "en-US" not in body
    # PK bytes appear REVERSED on the wire.
    assert "|15|14|13|12|11|10|9|8|7|6|5|4|3|2|1|0|" in body


def test_get_food_parses_food_identifier(test_client, httpx_mock, fixture_text) -> None:
    """A response carrying a FoodIdentifier yields a populated FoodSearchResult.

    The ``get_unsaved_tortilla.txt`` fixture contains the same FoodIdentifier
    shape ``getFood`` returns at the top level, so we can reuse it to verify
    that the decoder + ``_walk`` pull out name/brand/category correctly.
    """
    httpx_mock.add_response(
        url=SERVICE_URL,
        text=fixture_text("get_unsaved_tortilla.txt"),
    )
    pk = list(range(16))
    result = foods.get_food(test_client.http, pk)
    assert "Tortilla" in result.name
    assert result.brand == "Mission Tortillas Carb Balance"
    assert result.category == "Tortilla"
    # pk_bytes is the caller-supplied PK, untouched.
    assert result.pk_bytes == pk


def test_get_food_raises_when_identifier_missing(test_client, httpx_mock) -> None:
    """A response with no FoodIdentifier raises ``LoseItError``."""
    httpx_mock.add_response(
        url=SERVICE_URL,
        text="//OK[0,[],0,7]",
    )
    pk = list(range(16))
    with pytest.raises(LoseItError, match="not found"):
        foods.get_food(test_client.http, pk)


def test_get_food_filters_email_local_part(httpx_mock, fixture_text) -> None:
    """``get_food`` drops the ``test.user`` brand placeholder on personal-DB foods.

    Stage the captured ``get_unsaved_tortilla.txt`` fixture with the brand
    field swapped for the email-local-part placeholder so we can verify the
    sanitizer fires.
    """
    cfg = Config(
        user_id="12345678",
        user_name="test.user@example.com",
        hours_from_gmt=-6,
        policy_hash="8F87EC8969F17AE77B6283D3A83F6D4C",
        strong_name="351AE5DC0CA36AD3BA9C7CBA7B0E07B8",
    )
    client = Client(cfg, token="fake-jwt-token")
    response_text = fixture_text("get_unsaved_tortilla.txt").replace(
        "Mission Tortillas Carb Balance",
        "test.user",
    )
    httpx_mock.add_response(url=SERVICE_URL, text=response_text)
    result = foods.get_food(client.http, list(range(16)))
    assert result.brand != "test.user"
    assert result.brand == ""
