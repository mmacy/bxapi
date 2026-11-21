from typing import Any

from fastapi.testclient import TestClient

FIGHTER = {"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}
ENTER = {"command_type": "enter_dungeon", "dungeon_id": "barrow"}
NORTH = {"command_type": "move_party", "direction": "north"}
WITH_VIEW = {"include": "view"}


def start(client: TestClient) -> str:
    character = client.post("/v1/characters", json=FIGHTER).json()
    session = client.post("/v1/sessions", json={"adventure_id": "builtin-barrow", "character_ids": [character["id"]]})
    return session.json()["id"]


def send(client: TestClient, session_id: str, command: dict[str, Any], **params: Any) -> dict[str, Any]:
    response = client.post(f"/v1/sessions/{session_id}/commands", json=command, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_an_accepted_command_returns_the_view_at_its_revision(client: TestClient) -> None:
    session_id = start(client)
    before = client.get(f"/v1/sessions/{session_id}/view").json()
    outcome = send(client, session_id, ENTER, **WITH_VIEW)
    assert outcome["accepted"] and outcome["revision"] == 1
    assert outcome["view"] == client.get(f"/v1/sessions/{session_id}/view").json()
    assert outcome["view"] != before
    assert outcome["view"]["mode"] == outcome["mode"] == "exploring"


def test_a_refused_command_returns_the_unchanged_view(client: TestClient) -> None:
    session_id = start(client)
    send(client, session_id, ENTER)
    before = client.get(f"/v1/sessions/{session_id}/view").json()
    outcome = send(client, session_id, NORTH, **WITH_VIEW)
    assert not outcome["accepted"] and outcome["revision"] == 1
    assert outcome["view"] == before


def test_a_response_has_no_view_unless_the_request_asks_for_it(client: TestClient) -> None:
    session_id = start(client)
    assert "view" not in send(client, session_id, ENTER)
    assert "view" not in send(client, session_id, NORTH, visibility="referee")


def test_a_referee_gets_the_player_view_too(player: TestClient, referee: TestClient) -> None:
    session_id = start(referee)
    outcome = send(referee, session_id, ENTER, visibility="referee", **WITH_VIEW)
    assert outcome["view"] == player.get(f"/v1/sessions/{session_id}/view").json()
    assert outcome["view"] != referee.get(f"/v1/sessions/{session_id}/referee-view").json()


def test_another_include_value_is_a_validation_error(client: TestClient) -> None:
    session_id = start(client)
    for value in ("referee-view", "view,events", ""):
        response = client.post(f"/v1/sessions/{session_id}/commands", json=ENTER, params={"include": value})
        assert response.status_code == 422, value
        assert response.json()["errors"][0]["loc"] == ["query", "include"]
    assert client.get(f"/v1/sessions/{session_id}").json()["revision"] == 0


def test_a_replayed_response_has_the_first_view(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    first = client.post(path, json=ENTER, params=WITH_VIEW, headers={"Idempotency-Key": "enter"})
    send(client, session_id, {"command_type": "move_party", "direction": "east"})
    again = client.post(path, json=ENTER, params=WITH_VIEW, headers={"Idempotency-Key": "enter"})
    assert again.headers["idempotent-replayed"] == "true"
    assert again.json() == first.json()
    assert again.json()["view"] != client.get(f"/v1/sessions/{session_id}/view").json()


def test_the_openapi_document_describes_the_view_as_an_optional_player_view(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    outcome = document["components"]["schemas"]["CommandOutcome"]
    assert outcome["properties"]["view"]["$ref"] == "#/components/schemas/PlayerView"
    assert "view" not in outcome["required"]
    operation = document["paths"]["/v1/sessions/{session_id}/commands"]["post"]
    include = next(parameter for parameter in operation["parameters"] if parameter["name"] == "include")
    assert include["in"] == "query" and not include["required"]
    assert "view" in str(include["schema"])
