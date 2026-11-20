"""Acceptance tests for saves, restores, and save documents ("Saves" in docs/spec.md)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from osrlib.core.events import Visibility
from osrlib.persistence import load_game, save_game
from starlette.testclient import TestClient

from bxapi.settings import Settings

from ..conftest import REFEREE_KEY
from .conftest import (
    ADVENTURE_NAME,
    ENTER,
    PLAYER_SECRETS,
    ROUTE_TO_ENCOUNTER,
    SAVE_INFO_KEYS,
    SEED,
    TO_ANTECHAMBER,
    Oracle,
    adventure_document,
    assert_id,
    assert_page,
    assert_timestamp,
    battle_round,
    create_session,
    expect,
    expect_not_found,
    future_document,
    info,
    json_copy,
    send,
    upload_adventure,
    view,
)


def save(client: TestClient, session_id: str, **body: Any) -> dict[str, Any]:
    """Save a session and return the save info."""
    return expect(client.post(f"/v1/sessions/{session_id}/saves", json=body), 201)


def restore(client: TestClient, save_id: str, **body: Any) -> dict[str, Any]:
    """Restore a save and return the new session's info."""
    return expect(client.post(f"/v1/saves/{save_id}/restore", json=body), 201)


def check_save_info(body: Any, *, session_id: str | None, oracle: Oracle) -> None:
    """Check a save info body against the session the save copies."""
    assert set(body) == SAVE_INFO_KEYS, f"save info has the keys {sorted(body)}"
    assert_id(body["id"], "sav")
    assert body["session_id"] == session_id
    assert body["adventure_name"] == ADVENTURE_NAME
    assert body["mode"] == oracle.mode.value
    assert body["clock"] == oracle.clock()
    assert_timestamp(body["created_at"])


def started(client: TestClient, commands: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (ENTER,)) -> str:
    """Upload the test adventure, start a session with the referee's seed, play `commands`, and return its id."""
    adventure = upload_adventure(client)
    session = create_session(client, adventure["id"])
    for command in commands:
        assert send(client, session["id"], command)["accepted"]
    return session["id"]


def test_a_save_with_a_label_and_without_one(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    session_id = started(referee)
    labelled = save(player, session_id, label="Inside the door")
    check_save_info(labelled, session_id=session_id, oracle=oracle)
    assert labelled["label"] == "Inside the door"
    unlabelled = save(player, session_id)
    check_save_info(unlabelled, session_id=session_id, oracle=oracle)
    # SPEC QUESTION: the spec says `label` is optional but not what a save without one reports.
    # This accepts `null` or an empty string.
    assert unlabelled["label"] in (None, "")
    assert expect(player.get(f"/v1/saves/{labelled['id']}"), 200) == labelled
    assert info(player, session_id)["revision"] == 1, "saving changed the session's revision"
    expect(player.post(f"/v1/sessions/{session_id}/saves", json={"label": "x", "lable": "y"}), 422, "validation_error")


def test_list_and_the_session_filter(player: TestClient, referee: TestClient) -> None:
    first_session = started(referee)
    second_session = started(referee, [])
    first = save(player, first_session, label="one")
    second = save(player, second_session, label="two")
    third = save(player, first_session, label="three")
    every = assert_page(expect(player.get("/v1/saves"), 200))
    assert [item["id"] for item in every] == [third["id"], second["id"], first["id"]], "newest first"
    assert every[0] == third
    filtered = expect(player.get("/v1/saves", params={"session_id": first_session}), 200)
    assert [item["id"] for item in assert_page(filtered)] == [third["id"], first["id"]]
    assert filtered["total"] == 2
    page = expect(player.get("/v1/saves", params={"limit": 1, "offset": 2}), 200)
    assert [item["id"] for item in assert_page(page, limit=1, offset=2)] == [first["id"]]
    assert page["total"] == 3


def test_relabel_and_delete(player: TestClient, referee: TestClient) -> None:
    session_id = started(referee)
    created = save(player, session_id, label="Old")
    relabelled = expect(player.patch(f"/v1/saves/{created['id']}", json={"label": "New"}), 200)
    assert relabelled == created | {"label": "New"}
    assert expect(player.get(f"/v1/saves/{created['id']}"), 200)["label"] == "New"
    expect(player.patch(f"/v1/saves/{created['id']}", json={"lable": "Typo"}), 422, "validation_error")
    expect(player.delete(f"/v1/saves/{created['id']}"), 204)
    expect_not_found(player.get(f"/v1/saves/{created['id']}"), created["id"])
    expect_not_found(player.delete(f"/v1/saves/{created['id']}"), created["id"])
    expect_not_found(player.post(f"/v1/saves/{created['id']}/restore", json={}), created["id"])
    assert expect(player.get("/v1/saves"), 200)["total"] == 0


def test_a_save_outlives_its_session(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    session_id = started(referee)
    created = save(player, session_id)
    expect(player.delete(f"/v1/sessions/{session_id}"), 204)
    assert expect(player.get(f"/v1/saves/{created['id']}"), 200) == created
    restored = restore(player, created["id"])
    assert view(player, restored["id"]) == oracle.view()


def test_restore_starts_a_new_session_at_the_saved_moment(player: TestClient, referee: TestClient) -> None:
    original = Oracle.new()
    original.run([ENTER])
    at_save = original.view()
    saved = original.copy()
    later = original.run([TO_ANTECHAMBER, *ROUTE_TO_ENCOUNTER[2:]])

    session_id = started(referee)
    created = save(player, session_id, label="At the entrance")
    assert [send(player, session_id, command) for command in [TO_ANTECHAMBER, *ROUTE_TO_ENCOUNTER[2:]]] == later
    moved_on = view(player, session_id)

    restored = restore(player, created["id"], name="Second try")
    assert restored["id"] != session_id
    assert_id(restored["id"], "ses")
    assert (restored["name"], restored["adventure_name"], restored["mode"]) == (
        "Second try",
        ADVENTURE_NAME,
        "exploring",
    )
    assert restored["clock"] == saved.clock()
    assert view(player, restored["id"]) == at_save
    assert view(player, session_id) == moved_on

    # SPEC QUESTION: the spec doesn't say what revision a restored session starts at. The oracle
    # counts on from whatever the restored session's info reports.
    continued = saved.copy(revision=restored["revision"])
    expected = continued.execute(TO_ANTECHAMBER)
    assert "session.journal.entry_added" in [event["code"] for event in expected["events"]], "no trigger ran"
    assert send(player, restored["id"], TO_ANTECHAMBER) == expected
    assert view(player, restored["id"]) == continued.view()
    assert view(player, session_id) == moved_on
    assert expect(player.get(f"/v1/saves/{created['id']}"), 200) == created


def test_the_document_is_for_referees(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    session_id = started(referee)
    created = save(player, session_id)
    expect(player.get(f"/v1/saves/{created['id']}/document"), 403, "referee_required")
    document = expect(referee.get(f"/v1/saves/{created['id']}/document"), 200)
    assert document["kind"] == "save"
    assert document["payload"]["master_seed"] == SEED
    assert load_game(document).view(Visibility.PLAYER).model_dump(mode="json") == oracle.view()


def test_a_save_moves_to_another_server(
    player: TestClient,
    referee: TestClient,
    keyed_settings: Settings,
    make_client: Callable[..., TestClient],
    tmp_path: Path,
) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    session_id = started(referee)
    created = save(player, session_id)
    document = expect(referee.get(f"/v1/saves/{created['id']}/document"), 200)

    elsewhere = make_client(keyed_settings.model_copy(update={"database": str(tmp_path / "elsewhere.db")}), REFEREE_KEY)
    imported = expect(elsewhere.post("/v1/saves/import", json=document), 201)
    check_save_info(imported, session_id=None, oracle=oracle)
    assert [item["id"] for item in assert_page(expect(elsewhere.get("/v1/saves"), 200))] == [imported["id"]]
    restored = restore(elsewhere, imported["id"])
    assert view(elsewhere, restored["id"]) == oracle.view()
    continued = oracle.copy(revision=restored["revision"])
    assert send(elsewhere, restored["id"], TO_ANTECHAMBER) == continued.execute(TO_ANTECHAMBER)


def test_a_save_osrlib_wrote_imports_and_plays_on(referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    document = json_copy(save_game(oracle.session))
    imported = expect(referee.post("/v1/saves/import", json=document), 201)
    check_save_info(imported, session_id=None, oracle=oracle)
    restored = restore(referee, imported["id"])
    assert view(referee, restored["id"]) == oracle.view()
    continued = oracle.copy(revision=restored["revision"])
    for command in ROUTE_TO_ENCOUNTER[1:]:
        assert send(referee, restored["id"], command) == continued.execute(command)


def test_import_is_for_referees(player: TestClient) -> None:
    oracle = Oracle.new()
    document = json_copy(save_game(oracle.session))
    expect(player.post("/v1/saves/import", json=document), 403, "referee_required")
    assert expect(player.get("/v1/saves"), 200)["total"] == 0


def test_import_refuses_a_newer_or_malformed_document(referee: TestClient) -> None:
    document = json_copy(save_game(Oracle.new().session))
    expect(referee.post("/v1/saves/import", json=future_document(document)), 409, "version_unsupported")
    expect(referee.post("/v1/saves/import", json=adventure_document()), 422)
    broken = json_copy(document)
    del broken["payload"]["party"]
    expect(referee.post("/v1/saves/import", json=broken), 422)
    expect(referee.post("/v1/saves/import", json={"label": "Not a document"}), 422)
    assert expect(referee.get("/v1/saves"), 200)["total"] == 0


def test_unknown_save_ids_are_404(referee: TestClient) -> None:
    missing = "sav_" + "x" * 22
    for response in (
        referee.get(f"/v1/saves/{missing}"),
        referee.patch(f"/v1/saves/{missing}", json={"label": "x"}),
        referee.delete(f"/v1/saves/{missing}"),
        referee.post(f"/v1/saves/{missing}/restore", json={}),
        referee.get(f"/v1/saves/{missing}/document"),
    ):
        expect_not_found(response, missing)


def test_another_client_cannot_read_or_change_a_save(
    player: TestClient, referee: TestClient, other: TestClient
) -> None:
    session_id = started(referee)
    created = save(player, session_id, label="Mine")
    save_id = created["id"]
    for response in (
        other.get(f"/v1/saves/{save_id}"),
        other.patch(f"/v1/saves/{save_id}", json={"label": "Taken"}),
        other.delete(f"/v1/saves/{save_id}"),
        other.post(f"/v1/saves/{save_id}/restore", json={}),
        other.get(f"/v1/saves/{save_id}/document"),
    ):
        expect_not_found(response, save_id)
    assert expect(other.get("/v1/saves"), 200)["total"] == 0
    assert expect(other.get("/v1/saves", params={"session_id": session_id}), 200)["total"] == 0
    assert expect(other.get("/v1/sessions"), 200)["total"] == 0
    assert expect(player.get(f"/v1/saves/{save_id}"), 200) == created


def test_a_player_never_sees_the_drawn_seed(player: TestClient, referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], seed=None)
    session_id = session["id"]
    bodies: list[str] = []

    def record(response: Any, status: int = 200) -> Any:
        bodies.append(response.text)
        return expect(response, status)

    def play(target: str, command: dict[str, Any]) -> dict[str, Any]:
        outcome = record(player.post(f"/v1/sessions/{target}/commands", json=command))
        assert all(event["visibility"] == "player" for event in outcome["events"]), "a player got a referee event"
        record(player.get(f"/v1/sessions/{target}/view"))
        return outcome

    for command in ROUTE_TO_ENCOUNTER:
        play(session_id, command)
    for _ in range(4):
        mode = record(player.get(f"/v1/sessions/{session_id}"))["mode"]
        if mode == "encounter":
            play(session_id, {"command_type": "engage_battle"})
        elif mode == "battle":
            play(session_id, battle_round(record(player.get(f"/v1/sessions/{session_id}/view"))))
    created = record(player.post(f"/v1/sessions/{session_id}/saves", json={"label": "Mid-fight"}), 201)
    record(player.get("/v1/saves"))
    record(player.get(f"/v1/saves/{created['id']}"))
    restored = record(player.post(f"/v1/saves/{created['id']}/restore", json={}), 201)
    record(player.get(f"/v1/sessions/{restored['id']}/view"))
    for target in (session_id, restored["id"]):
        log = record(player.get(f"/v1/sessions/{target}/events", params={"limit": 500}))
        assert all(event["visibility"] == "player" for event in log["events"]), "a player read a referee event"
    record(player.get("/v1/sessions"))

    seed = expect(referee.get(f"/v1/saves/{created['id']}/document"), 200)["payload"]["master_seed"]
    assert isinstance(seed, int)
    text = "\n".join(bodies)
    assert str(seed) not in text, "a response to a player contains the master seed"
    leaked = [secret for secret in PLAYER_SECRETS if secret in text]
    assert not leaked, f"a response to a player contains {leaked}"
