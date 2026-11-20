from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.core.dice import roll
from osrlib.core.rng import RngStreams, StreamName

from bxapi.seeds import MAX_SEED

URL = "/v1/dice/roll"


def has_description(prop: dict[str, Any]) -> bool:
    """Whether a schema property, or one branch of its `anyOf`, has a description."""
    return bool(prop.get("description") or any(branch.get("description") for branch in prop.get("anyOf", [])))


def test_roll_matches_osrlib(client: TestClient) -> None:
    response = client.post(URL, json={"expression": "2d6+1x10", "seed": 42})
    assert response.status_code == 200
    expected = roll("2d6+1x10", RngStreams(master_seed=42).get(StreamName.ADJUDICATION))
    assert response.json() == {"expression": "2d6+1x10", "seed": 42, "rolls": [expected.model_dump(mode="json")]}


def test_times_rolls_in_order_on_one_stream(client: TestClient) -> None:
    response = client.post(URL, json={"expression": "3d6", "times": 5, "seed": 7})
    assert response.status_code == 200
    stream = RngStreams(master_seed=7).get(StreamName.ADJUDICATION)
    expected = [roll("3d6", stream).model_dump(mode="json") for _ in range(5)]
    assert response.json()["rolls"] == expected


def test_fewer_times_return_the_first_results_of_more(client: TestClient) -> None:
    three = client.post(URL, json={"expression": "2d8", "times": 3, "seed": 31}).json()["rolls"]
    five = client.post(URL, json={"expression": "2d8", "times": 5, "seed": 31}).json()["rolls"]
    assert three == five[:3]


def test_case_and_surrounding_spaces_do_not_matter(client: TestClient) -> None:
    plain = client.post(URL, json={"expression": "2d6+1x10", "seed": 8}).json()["rolls"]
    for expression in (" 2D6+1X10 ", "2d6+1*10", "2d6+1\u00d710"):
        assert client.post(URL, json={"expression": expression, "seed": 8}).json()["rolls"] == plain


def test_same_seed_gives_the_same_body(client: TestClient) -> None:
    body = {"expression": "1d20", "times": 10, "seed": 99}
    assert client.post(URL, json=body).json() == client.post(URL, json=body).json()


def test_no_seed_draws_a_different_seed_each_time(client: TestClient) -> None:
    first = client.post(URL, json={"expression": "1d20"}).json()
    second = client.post(URL, json={"expression": "1d20"}).json()
    assert 0 <= first["seed"] <= MAX_SEED
    assert first["seed"] != second["seed"]
    repeat = client.post(URL, json={"expression": "1d20", "seed": first["seed"]}).json()
    assert repeat == first


def test_times_defaults_to_one(client: TestClient) -> None:
    assert len(client.post(URL, json={"expression": "1d6", "seed": 1}).json()["rolls"]) == 1


def test_a_player_can_roll_and_choose_a_seed(player: TestClient) -> None:
    response = player.post(URL, json={"expression": "1d6", "seed": 5})
    assert response.status_code == 200
    assert response.json()["seed"] == 5


def test_no_key_is_unauthorized(player: TestClient) -> None:
    response = player.post(URL, json={"expression": "1d6"}, headers={"Authorization": ""})
    assert response.status_code == 401


@pytest.mark.parametrize("expression", ["abc", "3d7", "0d6", "3 d6", "1d6+", "1000d6", "1d6+1000000", "1d6x01"])
def test_an_expression_osrlib_rejects_is_content_invalid(client: TestClient, expression: str) -> None:
    response = client.post(URL, json={"expression": expression})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "content_invalid"
    assert response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expression": 6},
        {"expression": ""},
        {"expression": "1d6" + " " * 62},
        {"expression": "1d6", "Times": 5},
        {"expression": "1d6", "times": 0},
        {"expression": "1d6", "times": 101},
        {"expression": "1d6", "seed": -1},
        {"expression": "1d6", "seed": MAX_SEED + 1},
    ],
)
def test_a_body_that_breaks_the_schema_is_a_validation_error(client: TestClient, body: dict[str, object]) -> None:
    response = client.post(URL, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_the_largest_seed_and_times_are_accepted(client: TestClient) -> None:
    response = client.post(URL, json={"expression": "1d6", "times": 100, "seed": MAX_SEED})
    assert response.status_code == 200
    assert len(response.json()["rolls"]) == 100


def test_openapi_describes_the_endpoint(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]
    assert operation["summary"]
    assert "roll" in operation["description"]
    roll_result = schema["components"]["schemas"]["RollResult"]
    assert set(roll_result["properties"]) == {"rolls", "modifier", "multiplier", "total"}
    assert operation["operationId"] == "roll_dice"
    for model in ("DiceRollRequest", "DiceRollResponse"):
        properties = schema["components"]["schemas"][model]["properties"]
        assert all(has_description(prop) for prop in properties.values())
