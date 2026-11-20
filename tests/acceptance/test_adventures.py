"""Acceptance tests for stored and built-in adventures ("Adventures" in docs/spec.md)."""

import json
from typing import Any

from osrlib.crawl.adventure import Adventure
from osrlib.versioning import check_document
from starlette.testclient import TestClient

from bxapi.adventures import builtin_adventures

from .conftest import (
    ADVENTURE,
    ADVENTURE_NAME,
    ADVENTURE_SECRETS,
    DUNGEON_ID,
    DUNGEON_NAME,
    ENTER,
    PLAYER_TEXT,
    TOWN_NAME,
    Oracle,
    adventure_document,
    assert_id,
    assert_page,
    assert_timestamp,
    broken_adventure_document,
    create_session,
    expect,
    expect_not_found,
    future_document,
    leaves,
    party_document,
    send,
    upload_adventure,
)


def check_info(info: Any) -> None:
    """Check that adventure info has what the spec lists and nothing a player may not know."""
    assert info["name"] == ADVENTURE_NAME
    assert info["builtin"] is False
    text = json.dumps(info)
    leaked = [secret for secret in ADVENTURE_SECRETS if secret in text]
    assert not leaked, f"adventure info contains {leaked}: {text}"
    values = [value for _, value in leaves(info)]
    assert TOWN_NAME in values, f"adventure info has no town name: {text}"
    dungeons = [value for value in values if isinstance(value, dict) and DUNGEON_ID in value.values()]
    assert dungeons, f"adventure info has no entry for the dungeon {DUNGEON_ID!r}: {text}"
    assert DUNGEON_NAME in dungeons[0].values() and 2 in dungeons[0].values(), (
        f"the dungeon's entry lacks its name or its number of levels, 2: {dungeons[0]}"
    )
    # SPEC QUESTION: "the text the adventure's author wrote for players to read before play" is
    # read here as osrlib's `Adventure.description`, which the player view also shows. The spec
    # doesn't name the field, so the check looks for the text anywhere in the info.
    assert PLAYER_TEXT in values, f"adventure info lacks the adventure's description: {text}"


def stored_info(info: Any) -> None:
    """Check the fields of info for an adventure the client uploaded."""
    check_info(info)
    assert_id(info["id"], "adv")
    assert_timestamp(info["created_at"])


def builtins(client: TestClient) -> list[dict[str, Any]]:
    """Return the built-in adventures, from the start of the client's list."""
    items = assert_page(expect(client.get("/v1/adventures"), 200))
    return [item for item in items if item["builtin"]]


def test_upload_returns_info_with_nothing_secret(player: TestClient) -> None:
    created = upload_adventure(player)
    stored_info(created)
    assert expect(player.get(f"/v1/adventures/{created['id']}"), 200) == created


def test_the_list_puts_built_ins_first_then_the_newest(player: TestClient) -> None:
    first = upload_adventure(player)
    second = upload_adventure(player)
    body = expect(player.get("/v1/adventures"), 200)
    items = assert_page(body)
    flags = [item["builtin"] for item in items]
    assert flags == sorted(flags, reverse=True), f"a built-in adventure comes after a stored one: {flags}"
    assert [item["id"] for item in items if not item["builtin"]] == [second["id"], first["id"]]
    assert body["total"] == len(items) == flags.count(True) + 2


def test_validate_a_good_document(player: TestClient) -> None:
    before = expect(player.get("/v1/adventures"), 200)["total"]
    result = expect(player.post("/v1/adventures/validate", json=adventure_document()), 200)
    assert result["valid"] is True
    check_info(result["adventure"])
    assert expect(player.get("/v1/adventures"), 200)["total"] == before


def test_validate_a_broken_document(player: TestClient) -> None:
    before = expect(player.get("/v1/adventures"), 200)["total"]
    result = expect(player.post("/v1/adventures/validate", json=broken_adventure_document()), 200)
    assert result["valid"] is False
    assert isinstance(result["detail"], str) and result["detail"]
    assert "adventure" not in result
    assert expect(player.get("/v1/adventures"), 200)["total"] == before


def test_upload_refuses_a_document_osrlib_refuses(player: TestClient) -> None:
    before = expect(player.get("/v1/adventures"), 200)["total"]
    expect(player.post("/v1/adventures", json=broken_adventure_document()), 422, "content_invalid")
    document = adventure_document()
    no_town = json.loads(json.dumps(document))
    del no_town["payload"]["town"]
    for malformed in (
        no_town,
        dict(document, kind="party"),
        {"kind": "adventure", "schema_version": document["schema_version"]},
        {"name": "Not a document"},
    ):
        expect(player.post("/v1/adventures", json=malformed), 422)
    expect(player.post("/v1/adventures", json=[document]), 422)
    assert expect(player.get("/v1/adventures"), 200)["total"] == before


def test_upload_refuses_a_newer_schema_version(player: TestClient) -> None:
    before = expect(player.get("/v1/adventures"), 200)["total"]
    expect(player.post("/v1/adventures", json=future_document(adventure_document())), 409, "version_unsupported")
    assert expect(player.get("/v1/adventures"), 200)["total"] == before


def test_the_document_is_for_referees(player: TestClient, referee: TestClient) -> None:
    created = upload_adventure(player)
    expect(player.get(f"/v1/adventures/{created['id']}/document"), 403, "referee_required")
    document = expect(referee.get(f"/v1/adventures/{created['id']}/document"), 200)
    assert document["kind"] == "adventure"
    assert Adventure.model_validate(check_document(document, "adventure")) == ADVENTURE


BARROW_ID = "builtin-barrow"


def barrow_secrets() -> list[str]:
    """Return strings from the built-in barrow that its info must never contain."""
    barrow = builtin_adventures()[BARROW_ID].adventure
    secrets = [quest.id for quest in barrow.quests] + [trigger.id for trigger in barrow.triggers]
    for dungeon in barrow.dungeons:
        for level in dungeon.levels:
            for area in level.areas:
                if area.description:
                    secrets.append(area.description)
                if area.encounter is not None:
                    secrets.extend(monster.template_id for monster in area.encounter.monsters)
    return secrets


def test_the_built_in_barrow_is_listed_first_and_read_only(player: TestClient, referee: TestClient) -> None:
    barrow = builtin_adventures()[BARROW_ID].adventure
    secrets = barrow_secrets()
    upload_adventure(player)
    items = assert_page(expect(player.get("/v1/adventures"), 200))
    assert items[0]["builtin"] is True, "the list doesn't start with a built-in adventure"
    builtin = next(item for item in items if item["id"] == BARROW_ID)
    assert builtin["builtin"] is True and builtin["name"] == barrow.name
    text = json.dumps(builtin)
    assert barrow.town.name in text
    leaked = [secret for secret in secrets if secret in text]
    assert not leaked, f"the barrow's info contains {leaked}"
    assert expect(player.get(f"/v1/adventures/{BARROW_ID}"), 200) == builtin

    expect(player.delete(f"/v1/adventures/{BARROW_ID}"), 403, "adventure_read_only")
    expect(referee.delete(f"/v1/adventures/{BARROW_ID}"), 403, "adventure_read_only")
    assert expect(player.get(f"/v1/adventures/{BARROW_ID}"), 200) == builtin

    expect(player.get(f"/v1/adventures/{BARROW_ID}/document"), 403, "referee_required")
    document = expect(referee.get(f"/v1/adventures/{BARROW_ID}/document"), 200)
    assert Adventure.model_validate(check_document(document, "adventure")) == barrow


def test_the_built_in_barrow_starts_a_session(player: TestClient, referee: TestClient) -> None:
    barrow = builtin_adventures()[BARROW_ID].adventure
    oracle = Oracle.new(adventure=barrow)
    enter = {"command_type": "enter_dungeon", "dungeon_id": barrow.dungeons[0].id}
    expected = oracle.execute(enter)
    session = create_session(referee, BARROW_ID)
    assert session["adventure_id"] == BARROW_ID
    assert session["adventure_name"] == barrow.name
    assert session["mode"] == "town"
    assert send(player, session["id"], enter) == expected
    assert expect(player.get(f"/v1/sessions/{session['id']}/view"), 200) == oracle.view()


def test_every_client_sees_the_built_in_adventures(player: TestClient, other: TestClient) -> None:
    assert BARROW_ID in [item["id"] for item in builtins(player)]
    assert builtins(player) == builtins(other)
    assert expect(other.get(f"/v1/adventures/{BARROW_ID}"), 200) == expect(
        player.get(f"/v1/adventures/{BARROW_ID}"), 200
    )


def test_delete_leaves_a_started_session_playable(referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = oracle.execute(ENTER)
    created = upload_adventure(referee)
    session = create_session(referee, created["id"])

    expect(referee.delete(f"/v1/adventures/{created['id']}"), 204)
    expect_not_found(referee.get(f"/v1/adventures/{created['id']}"), created["id"])
    expect_not_found(referee.get(f"/v1/adventures/{created['id']}/document"), created["id"])
    expect_not_found(referee.delete(f"/v1/adventures/{created['id']}"), created["id"])
    assert created["id"] not in [item["id"] for item in assert_page(expect(referee.get("/v1/adventures"), 200))]

    assert send(referee, session["id"], ENTER) == expected
    assert expect(referee.get(f"/v1/sessions/{session['id']}"), 200)["adventure_name"] == ADVENTURE_NAME
    late = referee.post("/v1/sessions", json={"adventure_id": created["id"], "party_document": party_document()})
    expect_not_found(late, created["id"])


def test_unknown_adventure_ids_are_404(player: TestClient, referee: TestClient) -> None:
    missing = "adv_" + "x" * 22
    expect_not_found(player.get(f"/v1/adventures/{missing}"), missing)
    expect_not_found(player.delete(f"/v1/adventures/{missing}"), missing)
    expect_not_found(referee.get(f"/v1/adventures/{missing}/document"), missing)
    expect_not_found(referee.get("/v1/adventures/builtin-no-such-adventure"), "builtin-no-such-adventure")


def test_another_client_cannot_read_or_change_an_adventure(player: TestClient, other: TestClient) -> None:
    created = upload_adventure(player)
    adventure_id = created["id"]
    expect_not_found(other.get(f"/v1/adventures/{adventure_id}"), adventure_id)
    expect_not_found(other.get(f"/v1/adventures/{adventure_id}/document"), adventure_id)
    expect_not_found(other.delete(f"/v1/adventures/{adventure_id}"), adventure_id)
    expect_not_found(
        other.post("/v1/sessions", json={"adventure_id": adventure_id, "party_document": party_document()}),
        adventure_id,
    )
    listed = assert_page(expect(other.get("/v1/adventures"), 200))
    assert adventure_id not in [item["id"] for item in listed]
    assert all(item["builtin"] for item in listed)
    assert expect(player.get(f"/v1/adventures/{adventure_id}"), 200) == created
