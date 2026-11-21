import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bxapi.adventures import builtin_adventures
from bxapi.errors import PROBLEM_MEDIA_TYPE, content_too_large
from bxapi.idempotency import IdempotentRoute
from bxapi.json_body import MAX_JSON_DEPTH, SMALL_BODY_BYTES, body_limit, parse_json
from bxapi.openapi import build_openapi
from bxapi.routers import V1_ROUTERS
from bxapi.settings import Settings

from .conftest import REFEREE_KEY, problem_without_id

JSON = {"Content-Type": "application/json"}
ROLL = b'{"expression": "1d6"}'
FIGHTER = b'{"name": "%s", "class_id": "fighter", "alignment": "lawful"}'


def _barrow() -> bytes:
    """Return the built-in adventure's document as JSON, without the closing brace."""
    return json.dumps(dict(builtin_adventures()["builtin-barrow"].document)).encode()[:-1]


def _nested(levels: int) -> bytes:
    """Return a JSON object nested `levels` levels deep."""
    return b'{"a":' * (levels - 1) + b"{}" + b"}" * (levels - 1)


def _too_large_for_a_small_body() -> dict[str, Any]:
    return content_too_large(SMALL_BODY_BYTES, for_document=False).problem().model_dump(mode="json")


# Values the server refuses


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b'{"expression": "1d6", "times": NaN}', "NaN"),
        (b'{"expression": "1d6", "times": Infinity}', "Infinity"),
        (b'{"expression": "1d6", "times": -Infinity}', "-Infinity"),
        (b'{"expression": "1d6", "times": 1e400}', "64-bit float"),
        (b'{"expression": "1d6", "times": -1e400}', "64-bit float"),
        (b'{"expression": "\\ud800"}', "surrogate"),
        (b'{"expression": "1d6", "\\udfff": 1}', "surrogate"),
        (b'{"expression": "1d6", "x": ' + b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1) + b"}", "levels"),
    ],
)
def test_a_value_the_server_cannot_store_or_send_back_is_400(player: TestClient, content: bytes, said: str) -> None:
    response = player.post("/v1/dice/roll", content=content, headers=JSON)
    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    problem = problem_without_id(response)
    assert problem["code"] == "bad_request"
    assert said in problem["detail"]


@pytest.mark.parametrize("extra", [b"NaN", b"Infinity", b"1e400", b'"\\ud800"', b'{"\\udc00": 1}'])
def test_a_document_with_a_refused_value_is_not_stored(referee: TestClient, extra: bytes) -> None:
    before = referee.get("/v1/adventures").json()["total"]
    document = _barrow() + b', "x_extra": ' + extra + b"}"
    for path in ("/v1/adventures", "/v1/adventures/validate"):
        response = referee.post(path, content=document, headers=JSON)
        assert (response.status_code, response.json()["code"]) == (400, "bad_request"), path
    assert referee.get("/v1/adventures").json()["total"] == before


def test_a_field_osrlib_ignores_comes_back_as_valid_json(referee: TestClient) -> None:
    document = _barrow() + b', "x_extra": {"emoji": "\\ud83d\\ude00", "nul": "a\\u0000b", "big": 1e308}}'
    stored = referee.post("/v1/adventures", content=document, headers=JSON)
    assert stored.status_code == 201, stored.text
    read = referee.get(f"/v1/adventures/{stored.json()['id']}/document")
    assert read.status_code == 200
    assert parse_json(read.content)["x_extra"] == {"emoji": "\N{GRINNING FACE}", "nul": "a\x00b", "big": 1e308}


def test_a_surrogate_pair_is_one_character(player: TestClient) -> None:
    response = player.post("/v1/characters", content=FIGHTER % b"Hild \\ud83d\\ude00", headers=JSON)
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Hild \N{GRINNING FACE}"


def test_a_body_may_nest_the_most_levels_and_no_more(player: TestClient) -> None:
    deepest = player.post("/v1/adventures/validate", content=_nested(MAX_JSON_DEPTH), headers=JSON)
    assert deepest.status_code == 200
    assert deepest.json()["code"] == "content_invalid"
    for levels in (MAX_JSON_DEPTH + 1, 900, 50_000):
        deeper = player.post("/v1/adventures/validate", content=_nested(levels), headers=JSON)
        assert (deeper.status_code, deeper.json()["code"]) == (400, "bad_request"), levels


def test_the_depth_counts_arrays_and_objects_inside_a_document(player: TestClient) -> None:
    # The envelope and the payload are two levels, so the payload's value may nest 98 more.
    def document(levels: int) -> bytes:
        return b'{"kind": "adventure", "payload": {"x": ' + b"[" * levels + b"]" * levels + b"}}"

    allowed = player.post("/v1/adventures/validate", content=document(MAX_JSON_DEPTH - 2), headers=JSON)
    assert allowed.status_code == 200
    refused = player.post("/v1/adventures/validate", content=document(MAX_JSON_DEPTH - 1), headers=JSON)
    assert (refused.status_code, refused.json()["code"]) == (400, "bad_request")


def test_parse_json_counts_depth_in_every_encoding() -> None:
    for encoding in ("utf-8", "utf-16", "utf-32"):
        assert parse_json(("[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH).encode(encoding)) is not None
        assert parse_json('{"name": "\N{GRINNING FACE}"}'.encode(encoding)) == {"name": "\N{GRINNING FACE}"}
    assert parse_json(b"3") == 3
    assert parse_json(b'"[[[["') == "[[[["


# Body size


def test_a_body_that_is_no_document_has_the_small_limit(player: TestClient) -> None:
    fits = player.post("/v1/dice/roll", content=ROLL + b" " * (SMALL_BODY_BYTES - len(ROLL)), headers=JSON)
    assert fits.status_code == 200
    larger = player.post("/v1/dice/roll", content=ROLL + b" " * (SMALL_BODY_BYTES - len(ROLL) + 1), headers=JSON)
    assert larger.status_code == 413
    assert larger.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert problem_without_id(larger) == _too_large_for_a_small_body()


def test_the_small_limit_counts_a_body_with_no_content_length(player: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield ROLL
        for _ in range(SMALL_BODY_BYTES // 1024):
            yield b" " * 1024

    response = player.post("/v1/dice/roll", content=chunks(), headers=JSON)
    assert response.status_code == 413
    assert problem_without_id(response) == _too_large_for_a_small_body()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/characters"),
        ("POST", "/v1/generate/character"),
        ("POST", "/v1/sessions/ses_none/commands"),
        ("POST", "/v1/sessions/ses_none/saves"),
        ("PATCH", "/v1/sessions/ses_none"),
        ("PATCH", "/v1/saves/sav_none"),
        ("POST", "/v1/saves/sav_none/restore"),
    ],
)
def test_the_small_limit_applies_before_the_server_reads_the_body(player: TestClient, method: str, path: str) -> None:
    response = player.request(method, path, content=FIGHTER % (b"x" * SMALL_BODY_BYTES), headers=JSON)
    assert response.status_code == 413
    assert problem_without_id(response) == _too_large_for_a_small_body()


def test_a_body_over_the_small_limit_claims_no_idempotency_key(player: TestClient) -> None:
    headers = JSON | {"Idempotency-Key": "roll-1"}
    assert player.post("/v1/dice/roll", content=ROLL + b" " * SMALL_BODY_BYTES, headers=headers).status_code == 413
    assert player.post("/v1/dice/roll", content=ROLL, headers=headers).status_code == 200


def test_a_document_may_be_larger_than_the_small_limit(referee: TestClient) -> None:
    document = _barrow() + b"}" + b" " * (2 * SMALL_BODY_BYTES)
    checked = referee.post("/v1/adventures/validate", content=document, headers=JSON)
    assert (checked.status_code, checked.json()["valid"]) == (200, True)
    assert referee.post("/v1/adventures", content=document, headers=JSON).status_code == 201


def test_the_setting_limits_a_document(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    small = keyed_settings.model_copy(update={"max_body_bytes": 1024})
    client = make_client(small, REFEREE_KEY)
    response = client.post("/v1/adventures/validate", content=_barrow() + b"}", headers=JSON)
    assert response.status_code == 413
    assert problem_without_id(response) == content_too_large(1024).problem().model_dump(mode="json")


# Routes


def _v1_routes() -> list[APIRoute]:
    return [route for router in V1_ROUTERS for route in router.routes if isinstance(route, APIRoute)]


def test_every_route_with_a_body_reads_it_strictly() -> None:
    with_body = [route for route in _v1_routes() if route.body_field is not None]
    assert with_body
    assert [route.name for route in with_body if not isinstance(route, IdempotentRoute)] == []


def test_only_the_document_operations_read_a_large_body() -> None:
    unlimited = {route.name for route in _v1_routes() if body_limit(route.endpoint) is None}
    assert unlimited == {"upload_adventure", "validate_adventure", "import_character", "import_save", "create_session"}


def test_the_openapi_document_states_each_operations_limit() -> None:
    operations = {
        operation["operationId"]: operation for item in build_openapi()["paths"].values() for operation in item.values()
    }
    assert f"{SMALL_BODY_BYTES:,} bytes" in operations["roll_dice"]["responses"]["413"]["description"]
    assert f"{SMALL_BODY_BYTES:,} bytes" in operations["send_command"]["responses"]["413"]["description"]
    for name in ("upload_adventure", "import_save", "create_session"):
        description = operations[name]["responses"]["413"]["description"]
        assert "BXAPI_MAX_BODY_BYTES" in description and f"{SMALL_BODY_BYTES:,}" not in description
    assert f"{MAX_JSON_DEPTH} levels" in operations["roll_dice"]["responses"]["400"]["description"]
    description = build_openapi()["info"]["description"]
    assert f"more than {MAX_JSON_DEPTH} levels deep" in description
    assert f"at most {SMALL_BODY_BYTES:,} bytes" in description
