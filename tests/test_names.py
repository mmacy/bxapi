from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.persistence import save_game

from .test_adventures import a_document
from .test_characters import FIGHTER, osrlib_character
from .test_sessions import Oracle, roll_characters, start

CONTROL_CHARACTERS = ["\x00", "\t", "\n", "\x1b[2J", "\x7f", "\x85", "\x9f"]


def _refused(response: Any) -> None:
    assert (response.status_code, response.json()["code"]) == (422, "validation_error"), response.text
    [entry] = response.json()["errors"]
    assert entry["type"] == "string_control_character"
    assert "control character" in entry["msg"]


@pytest.mark.parametrize("bad", CONTROL_CHARACTERS)
def test_a_character_name_has_no_control_character(player: TestClient, bad: str) -> None:
    for path in ("/v1/characters", "/v1/generate/character"):
        _refused(player.post(path, json=FIGHTER | {"name": f"Hild{bad}"}))
    assert player.get("/v1/characters").json()["total"] == 0


def test_a_name_may_have_any_other_character(player: TestClient) -> None:
    name = "Hild \N{GRINNING FACE} d'Ærøskøbing \N{ZERO WIDTH JOINER}\N{RIGHT-TO-LEFT MARK}"
    stored = player.post("/v1/characters", json=FIGHTER | {"name": name})
    assert stored.status_code == 201, stored.text
    assert stored.json()["name"] == name


@pytest.mark.parametrize("bad", CONTROL_CHARACTERS)
def test_a_session_name_and_a_save_label_have_no_control_character(referee: TestClient, bad: str) -> None:
    text = f"Friday{bad}game"
    session = start(referee)
    _refused(referee.post("/v1/sessions", json={"adventure_id": "builtin-barrow", "character_ids": [], "name": text}))
    _refused(referee.patch(f"/v1/sessions/{session['id']}", json={"name": text}))
    _refused(referee.post(f"/v1/sessions/{session['id']}/saves", json={"label": text}))
    save = referee.post(f"/v1/sessions/{session['id']}/saves").json()
    _refused(referee.patch(f"/v1/saves/{save['id']}", json={"label": text}))
    _refused(referee.post(f"/v1/saves/{save['id']}/restore", json={"name": text}))
    assert referee.get(f"/v1/sessions/{session['id']}").json()["name"] == session["name"]
    assert referee.get("/v1/sessions").json()["total"] == 1
    assert [item["label"] for item in referee.get("/v1/saves").json()["items"]] == [None]


def test_an_adventure_name_has_no_control_character(player: TestClient) -> None:
    document = a_document(name="Second\x1b[2J Barrow")
    upload = player.post("/v1/adventures", json=document)
    assert (upload.status_code, upload.json()["code"]) == (422, "content_invalid")
    assert "control character" in upload.json()["detail"]
    checked = player.post("/v1/adventures/validate", json=document).json()
    assert (checked["valid"], checked["code"]) == (False, "content_invalid")
    assert all(item["builtin"] for item in player.get("/v1/adventures").json()["items"])
    # The description is text of many lines, so it may have a newline.
    assert player.post("/v1/adventures", json=a_document(description="One.\nTwo.")).status_code == 201


def test_an_imported_character_name_has_no_control_character(referee: TestClient) -> None:
    document: dict[str, Any] = osrlib_character(4).character.to_document()
    document["payload"]["name"] = "Hild\nthe Bold"
    response = referee.post("/v1/characters/import", json=document)
    assert (response.status_code, response.json()["code"]) == (422, "content_invalid")
    assert referee.get("/v1/characters").json()["total"] == 0


def test_an_imported_save_has_no_control_character_in_its_adventure_name(referee: TestClient) -> None:
    document = save_game(Oracle(roll_characters()).game)
    document["payload"]["adventure"]["name"] = "The Barrow\x00"
    response = referee.post("/v1/saves/import", json=document)
    assert (response.status_code, response.json()["code"]) == (422, "content_invalid")
    assert referee.get("/v1/saves").json()["total"] == 0
