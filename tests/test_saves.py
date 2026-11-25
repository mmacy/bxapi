import copy
import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.core.character import party_to_document
from osrlib.core.events import Visibility
from osrlib.crawl.interpreter import Interpreter
from osrlib.persistence import load_game, save_game
from osrlib.versioning import SCHEMA_VERSION

from bxapi.adventures import adventure_document
from bxapi.session_manager import session_unreadable
from bxapi.settings import Settings

from .conftest import REFEREE_KEY, problem_without_id
from .test_sessions import (
    BARROW,
    BARROW_ID,
    EAST,
    ENTER,
    SEED,
    WEST,
    Oracle,
    ok,
    problem,
    roll_characters,
    send,
    start,
)

SAVE_KEYS = {"id", "session_id", "adventure_id", "adventure_name", "label", "mode", "clock", "created_at"}


def save(client: TestClient, session_id: str, **body: Any) -> dict[str, Any]:
    return ok(client.post(f"/v1/sessions/{session_id}/saves", json=body), 201)


def entered(client: TestClient, **fields: Any) -> str:
    """Start a barrow session, enter the dungeon, and return the session's id."""
    session = start(client, **fields)
    assert send(client, session["id"], ENTER)["accepted"]
    return session["id"]


def loaded_oracle(document: dict[str, Any]) -> Oracle:
    """Return an oracle that continues from a save document, as a restored session does."""
    oracle = Oracle(roll_characters())
    oracle.game = load_game(copy.deepcopy(document))
    oracle.game.register_listener(Interpreter(oracle.game))
    return oracle


def test_a_save_copies_the_session_and_changes_nothing(player: TestClient, referee: TestClient) -> None:
    session_id = entered(referee)
    labelled = save(player, session_id, label="Inside the door")
    assert set(labelled) == SAVE_KEYS
    assert labelled["id"].startswith("sav_")
    session = ok(player.get(f"/v1/sessions/{session_id}"))
    assert (labelled["session_id"], labelled["adventure_id"], labelled["adventure_name"], labelled["label"]) == (
        session_id,
        BARROW_ID,
        BARROW.name,
        "Inside the door",
    )
    assert (labelled["mode"], labelled["clock"]) == (session["mode"], session["clock"])
    assert session["revision"] == 1 and session["updated_at"] >= session["created_at"]
    assert save(player, session_id)["label"] is None
    no_body = player.post(f"/v1/sessions/{session_id}/saves")
    assert ok(no_body, 201)["label"] is None
    assert ok(player.post(f"/v1/sessions/{session_id}/saves", json={}), 201)["label"] is None
    assert ok(player.get(f"/v1/saves/{labelled['id']}")) == labelled
    assert ok(player.get(f"/v1/sessions/{session_id}"))["revision"] == 1
    problem(player.post(f"/v1/sessions/{session_id}/saves", json={"lable": "x"}), 422, "validation_error")
    document = ok(referee.get(f"/v1/saves/{labelled['id']}/document"))
    assert load_game(document).view(Visibility.REFEREE).model_dump(mode="json") == ok(
        referee.get(f"/v1/sessions/{session_id}/referee-view")
    )


def test_list_filters_by_session_and_pages(player: TestClient, referee: TestClient) -> None:
    first_session, second_session = entered(referee), entered(referee)
    first = save(player, first_session, label="one")
    second = save(player, second_session, label="two")
    third = save(player, first_session, label="three")
    every = ok(player.get("/v1/saves"))
    assert [item["id"] for item in every["items"]] == [third["id"], second["id"], first["id"]]
    assert every["items"][0] == third and every["total"] == 3
    filtered = ok(player.get("/v1/saves", params={"session_id": first_session}))
    assert ([item["id"] for item in filtered["items"]], filtered["total"]) == ([third["id"], first["id"]], 2)
    page = ok(player.get("/v1/saves", params={"limit": 1, "offset": 2}))
    assert ([item["id"] for item in page["items"]], page["total"]) == ([first["id"]], 3)


def test_relabel_and_delete(player: TestClient, referee: TestClient) -> None:
    created = save(player, entered(referee), label="Old")
    assert ok(player.patch(f"/v1/saves/{created['id']}", json={"label": "New"})) == created | {"label": "New"}
    assert ok(player.patch(f"/v1/saves/{created['id']}", json={"label": None}))["label"] is None
    problem(player.patch(f"/v1/saves/{created['id']}", json={"lable": "Typo"}), 422, "validation_error")
    problem(player.patch(f"/v1/saves/{created['id']}", json={}), 422, "validation_error")
    ok(player.delete(f"/v1/saves/{created['id']}"), 204)
    for response in (
        player.get(f"/v1/saves/{created['id']}"),
        player.delete(f"/v1/saves/{created['id']}"),
        player.post(f"/v1/saves/{created['id']}/restore", json={}),
    ):
        assert problem(response, 404, "not_found")["id"] == created["id"]


def test_restore_starts_a_new_session_at_the_saved_moment(player: TestClient, referee: TestClient) -> None:
    session_id = entered(referee)
    at_save = ok(player.get(f"/v1/sessions/{session_id}/view"))
    created = save(player, session_id)
    document = ok(referee.get(f"/v1/saves/{created['id']}/document"))
    send(player, session_id, EAST)
    moved_on = ok(player.get(f"/v1/sessions/{session_id}/view"))

    restored = ok(player.post(f"/v1/saves/{created['id']}/restore", json={"name": "Second try"}), 201)
    assert restored["id"] != session_id
    assert (restored["name"], restored["adventure_id"], restored["revision"]) == ("Second try", BARROW_ID, 0)
    assert (restored["mode"], restored["clock"]) == (created["mode"], created["clock"])
    assert restored["created_at"] == restored["updated_at"]
    assert ok(player.get(f"/v1/sessions/{restored['id']}/view")) == at_save

    oracle = loaded_oracle(document)
    for command in (EAST, WEST, EAST):
        assert send(referee, restored["id"], command) == oracle.execute(command)
    assert ok(referee.get(f"/v1/sessions/{restored['id']}/referee-view")) == oracle.view(Visibility.REFEREE)
    assert ok(player.get(f"/v1/sessions/{session_id}/view")) == moved_on
    assert ok(player.get(f"/v1/saves/{created['id']}")) == created
    assert ok(player.post(f"/v1/saves/{created['id']}/restore"), 201)["name"] == BARROW.name


def test_a_save_outlives_its_session(player: TestClient, referee: TestClient) -> None:
    created = save(player, entered(referee))
    ok(player.delete(f"/v1/sessions/{created['session_id']}"), 204)
    assert ok(player.get(f"/v1/saves/{created['id']}")) == created
    restored = ok(player.post(f"/v1/saves/{created['id']}/restore"), 201)
    assert restored["adventure_id"] == BARROW_ID


def test_a_restore_keeps_the_adventure_id_after_the_session_and_the_adventure_are_deleted(referee: TestClient) -> None:
    # The copy has the built-in barrow's content, so only the recorded id tells them apart.
    stored = ok(referee.post("/v1/adventures", json=adventure_document(BARROW)), 201)
    session = start(referee, adventure_id=stored["id"])
    created = save(referee, session["id"])
    assert created["adventure_id"] == stored["id"]
    ok(referee.delete(f"/v1/sessions/{session['id']}"), 204)
    ok(referee.delete(f"/v1/adventures/{stored['id']}"), 204)
    restored = ok(referee.post(f"/v1/saves/{created['id']}/restore"), 201)
    assert (restored["adventure_id"], restored["adventure_name"]) == (stored["id"], BARROW.name)
    assert ok(referee.get(f"/v1/sessions/{restored['id']}"))["adventure_id"] == stored["id"]
    assert send(referee, restored["id"], ENTER)["accepted"]
    assert save(referee, restored["id"])["adventure_id"] == stored["id"]


def test_the_document_is_for_referees(player: TestClient, referee: TestClient) -> None:
    created = save(player, entered(referee))
    problem(player.get(f"/v1/saves/{created['id']}/document"), 403, "referee_required")
    document = ok(referee.get(f"/v1/saves/{created['id']}/document"))
    assert document["kind"] == "save" and document["payload"]["master_seed"] == SEED
    assert len(bytes.fromhex(document["payload"]["player_handle_key"])) == 32
    assert load_game(document).view(Visibility.PLAYER).model_dump(mode="json") == ok(
        player.get(f"/v1/sessions/{created['session_id']}/view")
    )


def test_import_validates_and_restores_on_another_server(
    referee: TestClient, keyed_settings: Settings, make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    created = save(referee, entered(referee))
    document = ok(referee.get(f"/v1/saves/{created['id']}/document"))
    elsewhere = make_client(keyed_settings.model_copy(update={"database": str(tmp_path / "elsewhere.db")}), REFEREE_KEY)
    imported = ok(elsewhere.post("/v1/saves/import", json=document), 201)
    assert imported == created | {
        "id": imported["id"],
        "session_id": None,
        "adventure_id": None,
        "label": None,
        "created_at": imported["created_at"],
    }
    restored = ok(elsewhere.post(f"/v1/saves/{imported['id']}/restore"), 201)
    assert (restored["adventure_id"], restored["revision"]) == (None, 0)
    assert save(elsewhere, restored["id"])["adventure_id"] is None
    oracle = loaded_oracle(document)
    for command in (EAST, WEST):
        assert send(elsewhere, restored["id"], command) == oracle.execute(command)


def test_import_is_for_referees_and_refuses_what_osrlib_refuses(player: TestClient, referee: TestClient) -> None:
    document = save_game(Oracle(roll_characters()).game)
    problem(player.post("/v1/saves/import", json=document), 403, "referee_required")
    newer = copy.deepcopy(document) | {"schema_version": SCHEMA_VERSION + 1}
    problem(referee.post("/v1/saves/import", json=newer), 409, "version_unsupported")
    broken = copy.deepcopy(document)
    del broken["payload"]["party"]
    problem(referee.post("/v1/saves/import", json=broken), 422, "content_invalid")
    problem(referee.post("/v1/saves/import", json=party_to_document(roll_characters())), 422, "content_invalid")
    problem(referee.post("/v1/saves/import", json=["save"]), 422, "validation_error")
    assert ok(referee.get("/v1/saves"))["total"] == 0


def test_unknown_save_ids_are_404(referee: TestClient) -> None:
    missing = "sav_" + "x" * 22
    for response in (
        referee.get(f"/v1/saves/{missing}"),
        referee.patch(f"/v1/saves/{missing}", json={"label": "x"}),
        referee.delete(f"/v1/saves/{missing}"),
        referee.post(f"/v1/saves/{missing}/restore", json={}),
        referee.get(f"/v1/saves/{missing}/document"),
    ):
        found = problem(response, 404, "not_found")
        assert (found["resource"], found["id"]) == ("save", missing)


def test_another_client_cannot_read_or_change_a_save(referee: TestClient, other: TestClient) -> None:
    created = save(referee, entered(referee), label="Mine")
    save_id = created["id"]
    for response in (
        other.get(f"/v1/saves/{save_id}"),
        other.patch(f"/v1/saves/{save_id}", json={"label": "Taken"}),
        other.delete(f"/v1/saves/{save_id}"),
        other.post(f"/v1/saves/{save_id}/restore", json={}),
        other.get(f"/v1/saves/{save_id}/document"),
    ):
        assert problem(response, 404, "not_found")["id"] == save_id
    assert ok(other.get("/v1/saves"))["total"] == 0
    assert ok(other.get("/v1/saves", params={"session_id": created["session_id"]}))["total"] == 0
    assert ok(referee.get(f"/v1/saves/{save_id}")) == created


def test_a_player_never_receives_the_seed_or_handle_key_from_a_save(player: TestClient, referee: TestClient) -> None:
    seed = 5_550_123_987_654_321
    session_id = entered(referee, seed=seed)
    bodies = [player.post(f"/v1/sessions/{session_id}/saves", json={"label": "x"}).text]
    save_id = player.get("/v1/saves").json()["items"][0]["id"]
    bodies += [player.get("/v1/saves").text, player.get(f"/v1/saves/{save_id}").text]
    restored = player.post(f"/v1/saves/{save_id}/restore", json={})
    bodies += [restored.text, player.get(f"/v1/sessions/{restored.json()['id']}/view").text]
    document = ok(referee.get(f"/v1/saves/{save_id}/document"))
    handle_key = document["payload"]["player_handle_key"]
    text = "\n".join(bodies)
    assert str(seed) not in text
    assert handle_key not in text and "player_handle_key" not in text


def test_import_stores_the_document_osrlib_writes_back(referee: TestClient) -> None:
    received = save_game(Oracle(roll_characters()).game)
    received["engine_version"] = "2.3.0"
    received["payload"]["written_by_an_older_server"] = True
    imported = ok(referee.post("/v1/saves/import", json=received), 201)
    stored = ok(referee.get(f"/v1/saves/{imported['id']}/document"))
    assert stored == save_game(load_game(copy.deepcopy(received)))
    assert stored != received


def test_import_refuses_an_adventure_osrlib_refuses(referee: TestClient) -> None:
    document = save_game(Oracle(roll_characters()).game)
    broken = json.loads(json.dumps(document).replace('"template_id": "goblin"', '"template_id": "no_such_monster"'))
    assert broken != document
    load_game(copy.deepcopy(broken))
    found = problem(referee.post("/v1/saves/import", json=broken), 422, "content_invalid")
    assert "no_such_monster" in found["detail"]
    assert ok(referee.get("/v1/saves"))["total"] == 0


def test_import_refuses_a_clock_the_database_cannot_store(referee: TestClient) -> None:
    document = save_game(Oracle(roll_characters()).game)
    document["payload"]["clock_rounds"] = 10**30
    problem(referee.post("/v1/saves/import", json=document), 422, "content_invalid")
    assert ok(referee.get("/v1/saves"))["total"] == 0


def test_a_stored_save_osrlib_cannot_load_is_unreadable_without_details(
    referee: TestClient, keyed_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    created = save(referee, entered(referee))
    document = ok(referee.get(f"/v1/saves/{created['id']}/document"))
    document["payload"]["mode"] = "SECRET-TRAP-AT-3,4"
    with sqlite3.connect(keyed_settings.database) as connection:
        connection.execute("UPDATE saves SET document = ? WHERE id = ?", (json.dumps(document), created["id"]))
    with caplog.at_level(logging.ERROR, logger="bxapi"):
        response = referee.post(f"/v1/saves/{created['id']}/restore")
    problem(response, 500, "session_unreadable")
    assert problem_without_id(response) == session_unreadable("save").problem().model_dump(mode="json")
    assert "SECRET" not in response.text
    assert created["id"] in caplog.text and "SECRET-TRAP-AT-3,4" in caplog.text
    assert ok(referee.get("/v1/sessions"))["total"] == 1
