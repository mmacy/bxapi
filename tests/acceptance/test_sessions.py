"""Acceptance tests for sessions, commands, views, and events ("Sessions" in docs/spec.md)."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from osrlib.core.events import Visibility
from osrlib.core.ruleset import EncumbranceMode, Ruleset
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, SessionMode
from osrlib.versioning import SCHEMA_VERSION, engine_version
from starlette.testclient import TestClient

from bxapi.app import create_app
from bxapi.commands import command_kind
from bxapi.seeds import MAX_SEED
from bxapi.settings import Settings

from ..conftest import REFEREE_KEY, bearer
from .conftest import (
    ADVENTURE_NAME,
    ENTER,
    PLAYER_SECRETS,
    ROUTE_TO_ENCOUNTER,
    ROUTE_TO_VICTORY,
    SEED,
    SESSION_INFO_KEYS,
    TO_ANTECHAMBER,
    TO_GUARD_ROOM,
    TO_SHRINE,
    Oracle,
    assert_id,
    assert_page,
    assert_timestamp,
    battle_round,
    clock_of,
    create_character_via_api,
    create_session,
    expect,
    expect_not_found,
    info,
    party_document,
    plan,
    read_all_events,
    referee_view,
    send,
    upload_adventure,
    view,
)

pytestmark = pytest.mark.xfail(reason="chunk: sessions")

JOURNAL = {"command_type": "add_journal_entry", "text": "The referee adds a line."}
BIG_SEED = 7_301_928_455_610_293
"""A seed long enough that its digits can't turn up in a response by chance."""


def available(mode: SessionMode, *, referee: bool) -> list[tuple[str, str]]:
    """Return the `(command_type, kind)` pairs the commands endpoint lists for a mode and a role."""
    pairs = []
    for command_class in ALL_COMMAND_CLASSES:
        command_type = command_class.model_fields["command_type"].default
        kind = command_kind(command_type).value
        if mode in command_class.allowed_modes and (referee or kind == "play"):
            pairs.append((command_type, kind))
    return sorted(pairs)


def listed_commands(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return the commands endpoint's body, with `commands` as sorted pairs."""
    body = expect(client.get(f"/v1/sessions/{session_id}/commands"), 200)
    assert set(body) == {"mode", "revision", "commands"}
    for entry in body["commands"]:
        assert set(entry) == {"command_type", "kind"}, f"a listed command has the keys {sorted(entry)}"
    body["commands"] = sorted((entry["command_type"], entry["kind"]) for entry in body["commands"])
    return body


def event_total(client: TestClient, session_id: str, **params: Any) -> int:
    """Return how many events of a visibility a session's log has."""
    return expect(client.get(f"/v1/sessions/{session_id}/events", params=params), 200)["total"]


def snapshot(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return what a refused or forbidden request must leave unchanged: the info, view, and log size."""
    current = info(client, session_id)
    return {
        "revision": current["revision"],
        "mode": current["mode"],
        "clock": current["clock"],
        "view": view(client, session_id),
        "events": event_total(client, session_id),
    }


def check_info(body: Any, *, name: str = ADVENTURE_NAME, adventure_id: str) -> None:
    """Check a session info body's fields and the values that don't depend on play."""
    assert set(body) == SESSION_INFO_KEYS, f"session info has the keys {sorted(body)}"
    assert_id(body["id"], "ses")
    assert body["name"] == name
    assert body["adventure_id"] == adventure_id
    assert body["adventure_name"] == ADVENTURE_NAME
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["engine_version"] == engine_version()
    assert_timestamp(body["created_at"])
    assert_timestamp(body["updated_at"])
    assert body["updated_at"] >= body["created_at"]
    assert body["ended"] is (body["mode"] in ("game_over", "victory"))


# Creating sessions


def test_a_session_from_stored_characters_marches_in_list_order(player: TestClient) -> None:
    adventure = upload_adventure(player)
    alpha = create_character_via_api(player, name="Alpha")
    beta = create_character_via_api(player, name="Beta", class_id="cleric")
    session = create_session(player, adventure["id"], character_ids=[beta["id"], alpha["id"]], seed=None)
    check_info(session, adventure_id=adventure["id"])
    assert (session["mode"], session["revision"], session["clock"]) == ("town", 0, clock_of(0))
    party = view(player, session["id"])["party"]
    assert [member["name"] for member in party] == ["Beta", "Alpha"]
    assert [member["class_id"] for member in party] == ["cleric", "fighter"]


def test_stored_characters_with_a_referee_seed_play_as_osrlib_does(player: TestClient, referee: TestClient) -> None:
    adventure = upload_adventure(player)
    made = [create_character_via_api(player, name="Alpha"), create_character_via_api(player, name="Beta")]
    stored = [expect(player.get(f"/v1/characters/{entry['id']}"), 200) for entry in made]
    planner = Oracle.from_stored(stored)
    planner.run(ROUTE_TO_ENCOUNTER)
    commands = [*ROUTE_TO_ENCOUNTER, *planner.fight()]
    oracle = Oracle.from_stored(stored)

    session = create_session(referee, adventure["id"], character_ids=[entry["id"] for entry in made])
    assert view(player, session["id"]) == oracle.view()
    for command in commands:
        assert send(player, session["id"], command) == oracle.execute(command)
    assert view(player, session["id"]) == oracle.view()
    assert referee_view(referee, session["id"]) == oracle.view(Visibility.REFEREE)


def test_a_referee_starts_a_session_from_a_party_document(referee: TestClient) -> None:
    oracle = Oracle.new()
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], name="From a document")
    check_info(session, name="From a document", adventure_id=adventure["id"])
    assert view(referee, session["id"]) == oracle.view()
    assert info(referee, session["id"]) == session


def test_a_session_needs_exactly_one_party(player: TestClient, referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    character = create_character_via_api(referee)
    both = {"adventure_id": adventure["id"], "character_ids": [character["id"]], "party_document": party_document()}
    # SPEC QUESTION: the spec says "one of the two" but not which code the 422 has. This reads
    # the rule as part of the request schema, so the code is `validation_error`.
    expect(referee.post("/v1/sessions", json=both), 422, "validation_error")
    expect(referee.post("/v1/sessions", json={"adventure_id": adventure["id"]}), 422, "validation_error")
    expect(referee.post("/v1/sessions", json={"adventure_id": adventure["id"], "character_ids": []}), 422)
    assert expect(player.get("/v1/sessions"), 200)["total"] == 0


def test_unknown_characters_and_adventures_are_404(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    character = create_character_via_api(referee)
    missing_character = "chr_" + "x" * 22
    missing_adventure = "adv_" + "x" * 22
    body = {"adventure_id": adventure["id"], "character_ids": [character["id"], missing_character]}
    expect_not_found(referee.post("/v1/sessions", json=body), missing_character)
    body = {"adventure_id": missing_adventure, "character_ids": [character["id"]]}
    expect_not_found(referee.post("/v1/sessions", json=body), missing_adventure)
    assert expect(referee.get("/v1/sessions"), 200)["total"] == 0


def test_a_player_cannot_choose_the_seed_or_the_party_document(player: TestClient) -> None:
    adventure = upload_adventure(player)
    character = create_character_via_api(player)
    seeded = {"adventure_id": adventure["id"], "character_ids": [character["id"]], "seed": 5}
    expect(player.post("/v1/sessions", json=seeded), 403, "referee_required")
    documented = {"adventure_id": adventure["id"], "party_document": party_document()}
    expect(player.post("/v1/sessions", json=documented), 403, "referee_required")
    assert expect(player.get("/v1/sessions"), 200)["total"] == 0


def test_a_referee_seed_goes_up_to_the_largest_safe_integer(referee: TestClient) -> None:
    oracle = Oracle.new(seed=MAX_SEED)
    expected = oracle.run(ROUTE_TO_ENCOUNTER)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], seed=MAX_SEED)
    assert [send(referee, session["id"], command) for command in ROUTE_TO_ENCOUNTER] == expected
    for seed in (MAX_SEED + 1, -1):
        body = {"adventure_id": adventure["id"], "party_document": party_document(), "seed": seed}
        expect(referee.post("/v1/sessions", json=body), 422, "validation_error")
    assert expect(referee.get("/v1/sessions"), 200)["total"] == 1


def test_an_unknown_field_in_a_session_request_is_422(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    body = {"adventure_id": adventure["id"], "party_document": party_document(), "seed": SEED, "sede": 1}
    expect(referee.post("/v1/sessions", json=body), 422, "validation_error")
    assert expect(referee.get("/v1/sessions"), 200)["total"] == 0
    session = create_session(referee, adventure["id"])
    expect(referee.patch(f"/v1/sessions/{session['id']}", json={"name": "x", "nmae": "y"}), 422, "validation_error")
    assert info(referee, session["id"])["name"] == ADVENTURE_NAME


def test_an_unknown_field_in_a_command_is_ignored(referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = oracle.execute(ENTER)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    assert send(referee, session["id"], ENTER | {"from_a_newer_client": True}) == expected


def test_the_openapi_document_describes_every_command(referee: TestClient) -> None:
    document = expect(referee.get("/openapi.json"), 200)
    path = "/v1/sessions/{session_id}/commands"
    assert "post" in document["paths"].get(path, {}), f"the OpenAPI document has no POST {path}"
    operation = document["paths"][path]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    components = document.get("components", {}).get("schemas", {})
    found: set[str] = set()
    seen: set[str] = set()
    pending: list[Any] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference not in seen:
                seen.add(reference)
                pending.append(components.get(reference.rsplit("/", 1)[-1]))
            properties = node.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get("command_type"), dict):
                command_type = properties["command_type"]
                found.update(value for value in (command_type.get("const"), command_type.get("default")) if value)
                found.update(command_type.get("enum", ()))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    expected = {command_class.model_fields["command_type"].default for command_class in ALL_COMMAND_CLASSES}
    assert "move_party" in found
    assert expected <= found, f"the command body's schema lacks {sorted(expected - found)}"


def test_a_ruleset_changes_a_rule_osrlib_applies(referee: TestClient) -> None:
    # Without encumbrance, the fighter in plate mail moves 40 feet a round in an encounter, not 20.
    unencumbered = Ruleset(encumbrance=EncumbranceMode.NONE)
    oracle = Oracle.new(ruleset=unencumbered)
    expected = oracle.run(ROUTE_TO_ENCOUNTER)
    as_written = Oracle.new()
    as_written.run(ROUTE_TO_ENCOUNTER)
    assert oracle.view() != as_written.view()

    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], ruleset={"encumbrance": "none"})
    assert [send(referee, session["id"], command) for command in ROUTE_TO_ENCOUNTER] == expected
    assert view(referee, session["id"]) == oracle.view()
    assert referee_view(referee, session["id"])["ruleset"] == unencumbered.model_dump(mode="json")

    plain = create_session(referee, adventure["id"])
    for command in ROUTE_TO_ENCOUNTER:
        send(referee, plain["id"], command)
    assert view(referee, plain["id"]) == as_written.view()
    expect(referee.post("/v1/sessions", json={"adventure_id": adventure["id"], "ruleset": {"encumbrance": "x"}}), 422)


def test_session_info_follows_play(referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER, TO_ANTECHAMBER])
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    check_info(session, adventure_id=adventure["id"])
    assert (session["mode"], session["ended"], session["revision"]) == ("town", False, 0)
    for command in (ENTER, TO_ANTECHAMBER):
        send(referee, session["id"], command)
    current = info(referee, session["id"])
    check_info(current, adventure_id=adventure["id"])
    assert current["id"] == session["id"]
    assert current["created_at"] == session["created_at"]
    assert (current["mode"], current["ended"], current["revision"]) == ("exploring", False, 2)
    assert current["clock"] == oracle.clock()


def test_rename(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    renamed = expect(referee.patch(f"/v1/sessions/{session['id']}", json={"name": "Renamed"}), 200)
    assert renamed == session | {"name": "Renamed", "updated_at": renamed["updated_at"]}
    assert info(referee, session["id"])["name"] == "Renamed"
    assert assert_page(expect(referee.get("/v1/sessions"), 200))[0]["name"] == "Renamed"


def test_list_and_the_mode_filter(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    first = create_session(referee, adventure["id"], name="Stays in town")
    second = create_session(referee, adventure["id"], name="Goes in")
    send(referee, second["id"], ENTER)
    every = assert_page(expect(referee.get("/v1/sessions"), 200))
    assert [item["id"] for item in every] == [second["id"], first["id"]], "newest first"
    assert every[0] == info(referee, second["id"])
    exploring = expect(referee.get("/v1/sessions", params={"mode": "exploring"}), 200)
    assert [item["id"] for item in assert_page(exploring)] == [second["id"]]
    assert exploring["total"] == 1
    town = assert_page(expect(referee.get("/v1/sessions", params={"mode": "town"}), 200))
    assert [item["id"] for item in town] == [first["id"]]
    assert assert_page(expect(referee.get("/v1/sessions", params={"mode": "victory"}), 200)) == []
    page = expect(referee.get("/v1/sessions", params={"limit": 1, "offset": 1}), 200)
    assert [item["id"] for item in assert_page(page, limit=1, offset=1)] == [first["id"]]
    assert page["total"] == 2


def test_delete(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    session_id = session["id"]
    expect(referee.delete(f"/v1/sessions/{session_id}"), 204)
    expect_not_found(referee.get(f"/v1/sessions/{session_id}"), session_id)
    expect_not_found(referee.get(f"/v1/sessions/{session_id}/view"), session_id)
    expect_not_found(referee.post(f"/v1/sessions/{session_id}/commands", json=ENTER), session_id)
    expect_not_found(referee.delete(f"/v1/sessions/{session_id}"), session_id)
    assert expect(referee.get("/v1/sessions"), 200)["total"] == 0


def test_unknown_session_ids_are_404(referee: TestClient) -> None:
    missing = "ses_" + "x" * 22
    for response in (
        referee.get(f"/v1/sessions/{missing}"),
        referee.patch(f"/v1/sessions/{missing}", json={"name": "x"}),
        referee.delete(f"/v1/sessions/{missing}"),
        referee.get(f"/v1/sessions/{missing}/commands"),
        referee.post(f"/v1/sessions/{missing}/commands", json=ENTER),
        referee.get(f"/v1/sessions/{missing}/view"),
        referee.get(f"/v1/sessions/{missing}/referee-view"),
        referee.get(f"/v1/sessions/{missing}/events"),
        referee.post(f"/v1/sessions/{missing}/saves", json={}),
    ):
        expect_not_found(response, missing)


# Commands


def test_accepted_commands_return_osrlib_events_and_messages(player: TestClient, referee: TestClient) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    oracle = Oracle.new()
    expected = oracle.run(commands)
    assert any(outcome["mode"] == "battle" for outcome in expected)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    for command, outcome in zip(commands, expected, strict=True):
        actual = send(player, session["id"], command)
        assert actual == outcome, f"the outcome of {command['command_type']} differs from osrlib's"


def test_a_refused_command_is_a_200_that_changes_nothing(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = oracle.execute(TO_SHRINE)
    assert expected["accepted"] is False
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    before = snapshot(player, session["id"])
    outcome = send(player, session["id"], TO_SHRINE)
    assert outcome == expected
    assert outcome["rejections"] and outcome["revision"] == 0
    assert snapshot(player, session["id"]) == before


@pytest.mark.parametrize(
    "body",
    [
        {"command_type": "no_such_command"},
        {"command_type": "move_party"},
        {"command_type": "move_party", "direction": "up"},
        {"command_type": "enter_dungeon", "dungeon_id": 7},
        {"direction": "north"},
        ["enter_dungeon"],
    ],
)
def test_an_unknown_or_malformed_command_is_422(player: TestClient, referee: TestClient, body: Any) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    before = snapshot(player, session["id"])
    expect(player.post(f"/v1/sessions/{session['id']}/commands", json=body), 422)
    assert snapshot(player, session["id"]) == before


@pytest.mark.parametrize(
    "command",
    [
        JOURNAL,
        {"command_type": "grant_coins", "character_id": "character-0001", "coins": {"gp": 1000}},
        {"command_type": "set_flag", "key": "cheat", "value": True},
    ],
)
def test_a_player_cannot_send_a_referee_command(player: TestClient, referee: TestClient, command: Any) -> None:
    assert Oracle.new().execute(command, Visibility.REFEREE)["accepted"], "osrlib refuses the referee command"
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    before = snapshot(player, session["id"])
    problem = expect(player.post(f"/v1/sessions/{session['id']}/commands", json=command), 403, "referee_command")
    assert problem["command_type"] == command["command_type"]
    assert snapshot(player, session["id"]) == before
    assert referee_view(referee, session["id"])["flags"] == {}


def test_a_referee_can_send_a_referee_command(referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = [oracle.execute(ENTER), oracle.execute(JOURNAL)]
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    assert [send(referee, session["id"], command) for command in (ENTER, JOURNAL)] == expected
    assert view(referee, session["id"]) == oracle.view()


def test_referee_visibility_shows_every_event_to_a_referee_only(player: TestClient, referee: TestClient) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    as_player = Oracle.new()
    as_referee = Oracle.new()
    player_outcomes = as_player.run(commands)
    referee_outcomes = [as_referee.execute(command, Visibility.REFEREE) for command in commands]
    assert any(event["visibility"] == "referee" for outcome in referee_outcomes for event in outcome["events"]), (
        "the route has no referee events to hide"
    )

    adventure = upload_adventure(referee)
    shown = create_session(referee, adventure["id"])
    hidden = create_session(referee, adventure["id"])
    for index, command in enumerate(commands):
        refused = player.post(f"/v1/sessions/{hidden['id']}/commands", json=command, params={"visibility": "referee"})
        expect(refused, 403, "referee_required")
        # SPEC QUESTION: the spec gives a player's `visibility=referee` a 403 but doesn't say the
        # command doesn't run. This reads the 403 as refusing the whole request.
        assert info(player, hidden["id"])["revision"] == index, "the refused request executed the command"
        assert send(player, hidden["id"], command) == player_outcomes[index]
        assert send(referee, shown["id"], command, visibility="referee") == referee_outcomes[index]
    for outcome in player_outcomes:
        assert all(event["visibility"] == "player" for event in outcome["events"])


def test_expected_revision(referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = oracle.run([ENTER, TO_ANTECHAMBER])
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    assert send(referee, session["id"], ENTER, expected_revision=0) == expected[0]
    before = snapshot(referee, session["id"])
    stale = referee.post(f"/v1/sessions/{session['id']}/commands", json=TO_ANTECHAMBER, params={"expected_revision": 0})
    problem = expect(stale, 409, "revision_conflict")
    assert problem["revision"] == 1
    assert snapshot(referee, session["id"]) == before
    assert send(referee, session["id"], TO_ANTECHAMBER, expected_revision=1) == expected[1]


def test_the_commands_endpoint_lists_what_the_mode_and_role_allow(player: TestClient, referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    for revision, mode, command in ((0, SessionMode.TOWN, ENTER), (1, SessionMode.EXPLORING, None)):
        as_player = listed_commands(player, session["id"])
        as_referee = listed_commands(referee, session["id"])
        assert (as_player["mode"], as_player["revision"]) == (mode.value, revision)
        assert as_player["commands"] == available(mode, referee=False)
        assert as_referee["commands"] == available(mode, referee=True)
        assert all(kind == "play" for _, kind in as_player["commands"])
        if command is not None:
            send(referee, session["id"], command)


# Views


def test_the_player_view_equals_osrlib_after_each_command(player: TestClient, referee: TestClient) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    oracle = Oracle.new()
    views = [oracle.view()]
    for command in commands:
        oracle.execute(command)
        views.append(oracle.view())
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    assert view(player, session["id"]) == views[0]
    for command, expected in zip(commands, views[1:], strict=True):
        send(player, session["id"], command)
        assert view(player, session["id"]) == expected, f"the view after {command['command_type']} differs"


def test_the_referee_view_is_for_referees_and_equals_osrlib(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run(ROUTE_TO_ENCOUNTER)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    for command in ROUTE_TO_ENCOUNTER:
        send(player, session["id"], command)
    expect(player.get(f"/v1/sessions/{session['id']}/referee-view"), 403, "referee_required")
    assert referee_view(referee, session["id"]) == oracle.view(Visibility.REFEREE)


# Events


def test_the_event_log_pages_for_each_visibility(player: TestClient, referee: TestClient) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    oracle = Oracle.new()
    oracle.run(commands)
    player_log, referee_log = oracle.events(), oracle.events(Visibility.REFEREE)
    assert len(referee_log) > len(player_log) > 8

    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    path = f"/v1/sessions/{session['id']}/events"
    empty = expect(player.get(path), 200)
    assert empty == {"events": [], "messages": [], "next_after": 0, "total": 0}
    for command in commands:
        send(player, session["id"], command)

    read = read_all_events(player, session["id"], limit=4)
    assert read["events"] == player_log
    assert read["messages"] == oracle.messages()
    assert read["total"] == read["next_after"] == len(player_log)
    read = read_all_events(referee, session["id"], limit=5, visibility="referee")
    assert read["events"] == referee_log
    assert read["messages"] == oracle.messages(Visibility.REFEREE)
    assert read["total"] == len(referee_log)
    assert read_all_events(referee, session["id"], limit=500)["events"] == player_log

    first = expect(player.get(path), 200)
    assert first["events"] == player_log[:100] and first["next_after"] == min(100, len(player_log))
    middle = expect(player.get(path, params={"after": 3, "limit": 2}), 200)
    assert middle == {
        "events": player_log[3:5],
        "messages": oracle.messages()[3:5],
        "next_after": 5,
        "total": len(player_log),
    }
    end = len(player_log)
    caught_up = expect(player.get(path, params={"after": end}), 200)
    assert caught_up == {"events": [], "messages": [], "next_after": end, "total": end}

    expect(player.get(path, params={"visibility": "referee"}), 403, "referee_required")
    expect(player.get(path, params={"limit": 0}), 422)
    expect(player.get(path, params={"limit": 501}), 422)


def test_reading_new_events_after_catching_up(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    oracle.run([ENTER])
    after_enter = oracle.events()
    oracle.execute(TO_ANTECHAMBER)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    path = f"/v1/sessions/{session['id']}/events"
    send(player, session["id"], ENTER)
    first = expect(player.get(path), 200)
    assert first["events"] == after_enter
    send(player, session["id"], TO_ANTECHAMBER)
    more = expect(player.get(path, params={"after": first["next_after"]}), 200)
    assert more["events"] == oracle.events()[len(after_enter) :]
    assert more["next_after"] == more["total"] == len(oracle.events())


# Determinism and durability


def test_the_same_seed_party_and_commands_give_the_same_game(referee: TestClient) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    adventure = upload_adventure(referee)
    first = create_session(referee, adventure["id"])
    second = create_session(referee, adventure["id"])
    for command in commands:
        assert send(referee, first["id"], command) == send(referee, second["id"], command)
    assert view(referee, first["id"]) == view(referee, second["id"])
    assert referee_view(referee, first["id"]) == referee_view(referee, second["id"])


def test_a_session_survives_a_new_application(keyed_settings: Settings) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    oracle = Oracle.new()
    expected = oracle.run(commands)
    with TestClient(create_app(keyed_settings), headers=bearer(REFEREE_KEY)) as before:
        adventure = upload_adventure(before)
        session = create_session(before, adventure["id"])
        for command, outcome in zip(commands[:4], expected[:4], strict=True):
            assert send(before, session["id"], command) == outcome
    with TestClient(create_app(keyed_settings), headers=bearer(REFEREE_KEY)) as after:
        current = info(after, session["id"])
        assert current["revision"] == 4
        assert current["mode"] == expected[3]["mode"]
        partial = Oracle.new()
        partial.run(commands[:4])
        assert view(after, session["id"]) == partial.view()
        for command, outcome in zip(commands[4:], expected[4:], strict=True):
            assert send(after, session["id"], command) == outcome
        assert referee_view(after, session["id"]) == oracle.view(Visibility.REFEREE)


def test_a_cache_of_one_session_alternates_without_mixing_sessions(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(keyed_settings.model_copy(update={"session_cache_size": 1}), REFEREE_KEY)
    first_commands = plan(ROUTE_TO_ENCOUNTER, seed=SEED)
    second_commands = plan(ROUTE_TO_ENCOUNTER, seed=SEED + 1)
    first_oracle, second_oracle = Oracle.new(seed=SEED), Oracle.new(seed=SEED + 1)
    adventure = upload_adventure(client)
    first = create_session(client, adventure["id"])
    second = create_session(client, adventure["id"], seed=SEED + 1)
    for index in range(max(len(first_commands), len(second_commands))):
        if index < len(first_commands):
            assert send(client, first["id"], first_commands[index]) == first_oracle.execute(first_commands[index])
        if index < len(second_commands):
            assert send(client, second["id"], second_commands[index]) == second_oracle.execute(second_commands[index])
    assert view(client, first["id"]) == first_oracle.view()
    assert view(client, second["id"]) == second_oracle.view()


def test_two_applications_take_turns_on_one_session(
    referee: TestClient, keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    commands = plan(ROUTE_TO_ENCOUNTER)
    oracle = Oracle.new()
    expected = oracle.run(commands)
    second = make_client(keyed_settings, REFEREE_KEY)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    for index, (command, outcome) in enumerate(zip(commands, expected, strict=True)):
        client = referee if index % 2 == 0 else second
        assert send(client, session["id"], command) == outcome, f"command {index} on application {index % 2 + 1}"
        assert view(second if client is referee else referee, session["id"]) == oracle_view_after(commands, index)
    assert info(referee, session["id"])["revision"] == info(second, session["id"])["revision"] == len(commands)
    assert read_all_events(second, session["id"], limit=500, visibility="referee")["events"] == oracle.events(
        Visibility.REFEREE
    )


def oracle_view_after(commands: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """Return the oracle's player view after the commands up to and including `index`."""
    oracle = Oracle.new()
    oracle.run(commands[: index + 1])
    return oracle.view()


def test_concurrent_commands_execute_one_at_a_time(referee: TestClient) -> None:
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    texts = [f"Line {number}" for number in range(24)]

    def journal(text: str) -> dict[str, Any]:
        return send(referee, session["id"], {"command_type": "add_journal_entry", "text": text})

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(journal, texts))
    assert all(outcome["accepted"] for outcome in outcomes)
    assert sorted(outcome["revision"] for outcome in outcomes) == list(range(1, 25))
    assert info(referee, session["id"])["revision"] == 24
    journal_texts = [entry["text"] for entry in view(referee, session["id"])["journal"]]
    assert sorted(journal_texts) == sorted(texts)
    logged = read_all_events(referee, session["id"], limit=500)["events"]
    assert len(logged) == 24
    by_revision = sorted(outcomes, key=lambda outcome: outcome["revision"])
    assert logged == [event for outcome in by_revision for event in outcome["events"]]


def test_two_applications_sending_at_once_lose_and_repeat_nothing(
    referee: TestClient, keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    second = make_client(keyed_settings, REFEREE_KEY)
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    texts = [f"Line {number}" for number in range(16)]

    def journal(pair: tuple[int, str]) -> dict[str, Any]:
        index, text = pair
        client = referee if index % 2 == 0 else second
        command = {"command_type": "add_journal_entry", "text": text}
        for _ in range(50):
            response = client.post(f"/v1/sessions/{session['id']}/commands", json=command)
            if response.status_code == 409 and response.json()["code"] == "session_busy":
                continue
            return expect(response, 200)
        raise AssertionError(f"{text!r} got session_busy 50 times")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(journal, enumerate(texts)))
    assert sorted(outcome["revision"] for outcome in outcomes) == list(range(1, 17))
    assert info(second, session["id"])["revision"] == 16
    assert sorted(entry["text"] for entry in view(referee, session["id"])["journal"]) == sorted(texts)
    assert read_all_events(second, session["id"], limit=500)["total"] == 16


# Playing to the end


def test_play_to_victory(player: TestClient, referee: TestClient) -> None:
    oracle = Oracle.new()
    expected = oracle.run(ROUTE_TO_VICTORY)
    refused = oracle.execute({"command_type": "move_party", "direction": "north"})
    afterwards = oracle.execute(JOURNAL)
    assert expected[-1]["mode"] == "victory" and refused["accepted"] is False

    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"])
    assert [send(player, session["id"], command) for command in ROUTE_TO_VICTORY] == expected
    current = info(player, session["id"])
    assert (current["mode"], current["ended"], current["revision"]) == ("victory", True, 2)
    assert "session.adventure.completed" in [event["code"] for event in expected[-1]["events"]]

    before = snapshot(player, session["id"])
    assert send(player, session["id"], {"command_type": "move_party", "direction": "north"}) == refused
    assert snapshot(player, session["id"]) == before
    listed = listed_commands(player, session["id"])
    assert listed["commands"] == available(SessionMode.VICTORY, referee=False)
    assert send(referee, session["id"], JOURNAL) == afterwards
    victorious = expect(player.get("/v1/sessions", params={"mode": "victory"}), 200)
    assert [item["id"] for item in assert_page(victorious)] == [session["id"]]


def test_a_player_never_sees_the_seed_or_a_referee_event(player: TestClient, referee: TestClient) -> None:
    planner = Oracle.new(seed=BIG_SEED)
    planner.run(ROUTE_TO_ENCOUNTER)
    commands = [*ROUTE_TO_ENCOUNTER, *planner.fight(rounds=6)]
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], seed=BIG_SEED)
    session_id = session["id"]

    bodies: list[str] = []

    def record(response: Any) -> Any:
        bodies.append(response.text)
        return expect(response, 200)

    for command in commands:
        outcome = record(player.post(f"/v1/sessions/{session_id}/commands", json=command))
        assert all(event["visibility"] == "player" for event in outcome["events"])
        record(player.get(f"/v1/sessions/{session_id}/view"))
        record(player.get(f"/v1/sessions/{session_id}"))
        record(player.get(f"/v1/sessions/{session_id}/commands"))
    log = record(player.get(f"/v1/sessions/{session_id}/events", params={"limit": 500}))
    assert all(event["visibility"] == "player" for event in log["events"])
    record(player.get("/v1/sessions"))

    text = "\n".join(bodies)
    assert str(BIG_SEED) not in text, "a response to a player contains the master seed"
    leaked = [secret for secret in PLAYER_SECRETS if secret in text]
    assert not leaked, f"a response to a player contains {leaked}"
    assert len(read_all_events(referee, session_id, limit=500, visibility="referee")["events"]) > len(log["events"])


def test_a_session_started_from_a_player_view_can_fight(player: TestClient, referee: TestClient) -> None:
    # The battle declarations here come from the API's player view, as a client builds them.
    adventure = upload_adventure(referee)
    session = create_session(referee, adventure["id"], seed=None)
    for command in ROUTE_TO_ENCOUNTER:
        assert send(player, session["id"], command)["accepted"]
    if info(player, session["id"])["mode"] == "encounter":
        assert send(player, session["id"], {"command_type": "engage_battle"})["mode"] in ("battle", "exploring")
    if info(player, session["id"])["mode"] == "battle":
        outcome = send(player, session["id"], battle_round(view(player, session["id"])))
        assert outcome["accepted"], outcome["rejections"]


# Tenancy


def test_another_client_cannot_read_or_change_a_session(
    player: TestClient, referee: TestClient, other: TestClient
) -> None:
    adventure = upload_adventure(referee)
    character = create_character_via_api(player)
    session = create_session(referee, adventure["id"])
    session_id = session["id"]
    send(player, session_id, ENTER)
    before = referee_view(referee, session_id)
    for response in (
        other.get(f"/v1/sessions/{session_id}"),
        other.patch(f"/v1/sessions/{session_id}", json={"name": "Taken"}),
        other.delete(f"/v1/sessions/{session_id}"),
        other.get(f"/v1/sessions/{session_id}/commands"),
        other.post(f"/v1/sessions/{session_id}/commands", json=TO_GUARD_ROOM),
        other.post(f"/v1/sessions/{session_id}/commands", json=JOURNAL),
        other.get(f"/v1/sessions/{session_id}/view"),
        other.get(f"/v1/sessions/{session_id}/referee-view"),
        other.get(f"/v1/sessions/{session_id}/events"),
        other.post(f"/v1/sessions/{session_id}/saves", json={}),
    ):
        expect_not_found(response, session_id)
    assert expect(other.get("/v1/sessions"), 200)["total"] == 0
    builtin = assert_page(expect(other.get("/v1/adventures"), 200))[0]
    borrowed = other.post("/v1/sessions", json={"adventure_id": builtin["id"], "character_ids": [character["id"]]})
    expect_not_found(borrowed, character["id"])
    assert referee_view(referee, session_id) == before
    assert info(player, session_id)["name"] == ADVENTURE_NAME
    assert json.dumps(before)
