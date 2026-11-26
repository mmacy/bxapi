import copy
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from osrlib.core.alignment import Alignment
from osrlib.core.character import CHARACTER_CREATION_STREAM, Character, create_character, party_to_document
from osrlib.core.classes import ClassDefinition
from osrlib.core.clock import GameClock
from osrlib.core.events import Event, Visibility
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.core.spells import SpellTemplate
from osrlib.crawl.adventure import Adventure, TownSpec
from osrlib.crawl.battle import preview_battle_round
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, BattleDeclaration, SessionMode, SetFlag, parse_command
from osrlib.crawl.dungeon import AreaSpec, DungeonSpec, Edge, EdgeKind, LevelSpec, WanderingSpec
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.crawl.triggers import AreaEnteredPattern, TriggerSpec
from osrlib.data import load_classes, load_spells
from osrlib.messages import format_message
from osrlib.persistence import load_game
from osrlib.versioning import SCHEMA_VERSION, engine_version

from bxapi.adventures import adventure_document, builtin_adventures
from bxapi.app import create_app
from bxapi.commands import command_kind
from bxapi.db import Database
from bxapi.session_manager import MAX_PARTY_SIZE, SessionManager, session_busy, session_unreadable
from bxapi.settings import Settings

from .conftest import REFEREE_KEY, bearer, problem_without_id

BARROW_ID = "builtin-barrow"
BARROW = builtin_adventures()[BARROW_ID].adventure
SEED = 4242
BIG_SEED = 6_118_204_771_930_557
ENTER = {"command_type": "enter_dungeon", "dungeon_id": "barrow"}
EAST = {"command_type": "move_party", "direction": "east"}
WEST = {"command_type": "move_party", "direction": "west"}
NORTH = {"command_type": "move_party", "direction": "north"}
ROUTE = [ENTER, EAST, WEST, EAST, WEST]
NOTE = {"command_type": "record_note", "text": "The referee notes a draught."}
JOURNAL = {"command_type": "add_journal_entry", "text": "A line from the referee."}
ENGAGE = {"command_type": "engage_battle"}
TO_BATTLE = [ENTER, EAST, EAST, ENGAGE]
"""Commands that take the test party into a battle with the barrow's goblins, with the test seed."""
INFO_KEYS = {
    "id",
    "name",
    "adventure_id",
    "adventure_name",
    "mode",
    "ended",
    "revision",
    "clock",
    "schema_version",
    "engine_version",
    "created_at",
    "updated_at",
}


def roll_characters() -> list[Character]:
    stream = RngStreams(master_seed=8).get(CHARACTER_CREATION_STREAM)
    return [
        create_character(
            name=name, class_id=class_id, alignment=Alignment.LAWFUL, ruleset=Ruleset(), stream=stream
        ).character
        for name, class_id in (("Ash", "fighter"), ("Bree", "cleric"))
    ]


def database(client: TestClient) -> Database:
    """Return the database of a test client's application."""
    assert isinstance(client.app, FastAPI)
    return client.app.state.db


def manager(client: TestClient) -> SessionManager:
    """Return the session manager of a test client's application."""
    assert isinstance(client.app, FastAPI)
    return client.app.state.sessions


def store_characters(referee: TestClient) -> list[str]:
    """Import the test characters through the API with a referee key, and return their ids, Ash first."""
    imported = [ok(referee.post("/v1/characters/import", json=made.to_document()), 201) for made in roll_characters()]
    return [character["id"] for character in imported]


def create_characters(client: TestClient) -> list[str]:
    """Create two characters through the API, with dice the server rolls, and return their ids."""
    body = {"class_id": "fighter", "alignment": "lawful"}
    return [ok(client.post("/v1/characters", json=body | {"name": name}), 201)["id"] for name in ("Cato", "Dara")]


class Oracle:
    """An osrlib session that plays the game the server plays, with the outcomes the API returns."""

    def __init__(
        self,
        members: list[Character],
        *,
        seed: int = SEED,
        ruleset: Ruleset | None = None,
        adventure: Adventure = BARROW,
    ) -> None:
        self.game = GameSession.new(Party(members=members), adventure, seed=seed, ruleset=ruleset)
        self.game.register_listener(Interpreter(self.game))
        self.revision = 0

    @classmethod
    def loaded(cls, document: dict[str, Any]) -> Oracle:
        """Return an oracle that continues from a save document with one `Interpreter`, as a restored session does."""
        oracle = cls(roll_characters())
        oracle.game = load_game(copy.deepcopy(document))
        oracle.game.register_listener(Interpreter(oracle.game))
        return oracle

    def execute(self, command: dict[str, Any], visibility: Visibility = Visibility.PLAYER) -> dict[str, Any]:
        parsed = parse_command(command)
        assert parsed is not None
        result = self.game.execute(parsed)
        self.revision += result.accepted
        shown = visible(result.events, visibility)
        return {
            "accepted": result.accepted,
            "rejections": [rejection.model_dump(mode="json") for rejection in result.rejections],
            "events": [event.model_dump(mode="json") for event in shown],
            "messages": [format_message(event) for event in shown],
            "mode": self.game.mode.value,
            "ended": self.game.mode.terminal,
            "revision": self.revision,
        }

    def view(self, visibility: Visibility = Visibility.PLAYER) -> dict[str, Any]:
        return self.game.view(visibility).model_dump(mode="json")

    def log(self, visibility: Visibility = Visibility.PLAYER) -> list[Event]:
        return visible([event for event in self.game.event_log if isinstance(event, Event)], visibility)


def visible(events: Iterable[Event], visibility: Visibility) -> list[Event]:
    return [event for event in events if visibility is Visibility.REFEREE or event.visibility is Visibility.PLAYER]


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return None if status == 204 else response.json()


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == code
    return body


def start(client: TestClient, **fields: Any) -> dict[str, Any]:
    """Start a barrow session from a party document with the test seed."""
    body = {"adventure_id": BARROW_ID, "party_document": party_to_document(roll_characters()), "seed": SEED} | fields
    return ok(client.post("/v1/sessions", json={key: value for key, value in body.items() if value is not None}), 201)


def send(client: TestClient, session_id: str, command: dict[str, Any], **params: Any) -> dict[str, Any]:
    return ok(client.post(f"/v1/sessions/{session_id}/commands", json=command, params=params))


def split_handles(referee: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Return a referee view without `feature_handles`, and the feature references that have a handle.

    The server gives each session a random handle key, so its handles differ from the handles of an
    oracle that starts with the same seed. Everything else in the two views is equal.
    """
    rest = {key: value for key, value in referee.items() if key != "feature_handles"}
    handles = referee["feature_handles"]
    assert all(handle.startswith("feature:") for handle in handles.values())
    assert len(set(handles.values())) == len(handles)
    return rest, set(handles)


def state(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return what a refused request must leave as it was."""
    return {
        "info": ok(client.get(f"/v1/sessions/{session_id}")),
        "view": ok(client.get(f"/v1/sessions/{session_id}/view")),
        "events": ok(client.get(f"/v1/sessions/{session_id}/events", params={"limit": 500}))["total"],
    }


# Starting sessions


def test_a_session_from_stored_characters_plays_as_osrlib_does(player: TestClient, referee: TestClient) -> None:
    ash, bree = store_characters(referee)
    body = {"adventure_id": BARROW_ID, "character_ids": [bree, ash], "seed": SEED}
    session = ok(referee.post("/v1/sessions", json=body), 201)
    assert set(session) == INFO_KEYS
    assert session["id"].startswith("ses_") and len(session["id"]) == 26
    assert (session["name"], session["adventure_id"], session["adventure_name"]) == (
        BARROW.name,
        BARROW_ID,
        BARROW.name,
    )
    assert (session["mode"], session["ended"], session["revision"]) == ("town", False, 0)
    assert session["clock"] == {"rounds": 0, "turns": 0, "days": 0}
    assert (session["schema_version"], session["engine_version"]) == (SCHEMA_VERSION, engine_version())
    assert session["created_at"] == session["updated_at"]

    ash_character, bree_character = roll_characters()
    oracle = Oracle([bree_character, ash_character])
    assert ok(player.get(f"/v1/sessions/{session['id']}/view")) == oracle.view()
    assert [member["name"] for member in oracle.view()["party"]] == ["Bree", "Ash"]
    for command in [*ROUTE, NORTH]:
        assert send(player, session["id"], command) == oracle.execute(command)
    assert split_handles(ok(referee.get(f"/v1/sessions/{session['id']}/referee-view"))) == split_handles(
        oracle.view(Visibility.REFEREE)
    )


def test_a_player_session_draws_its_seed(player: TestClient) -> None:
    ids = create_characters(player)
    body = {"adventure_id": BARROW_ID, "character_ids": ids, "name": "Mine"}
    session = ok(player.post("/v1/sessions", json=body), 201)
    assert session["name"] == "Mine"
    assert send(player, session["id"], ENTER)["accepted"]


def test_a_character_can_join_twice(referee: TestClient) -> None:
    ash, _ = store_characters(referee)
    session = ok(referee.post("/v1/sessions", json={"adventure_id": BARROW_ID, "character_ids": [ash, ash]}), 201)
    party = ok(referee.get(f"/v1/sessions/{session['id']}/view"))["party"]
    assert [member["id"] for member in party] == ["character-0001", "character-0002"]


def test_a_player_cannot_choose_the_seed_or_send_a_party_document(player: TestClient) -> None:
    ids = create_characters(player)
    seeded = {"adventure_id": BARROW_ID, "character_ids": ids, "seed": 1}
    problem(player.post("/v1/sessions", json=seeded), 403, "referee_required")
    documented = {"adventure_id": BARROW_ID, "party_document": party_to_document(roll_characters())}
    problem(player.post("/v1/sessions", json=documented), 403, "referee_required")
    assert ok(player.get("/v1/sessions"))["total"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"character_ids": ["chr_x"], "party_document": {"kind": "party"}},
        {"character_ids": ["chr_x"], "sede": 1},
        {"character_ids": ["chr_x"], "seed": -1},
        {"character_ids": ["chr_x"], "seed": 2**53},
        {"character_ids": ["chr_x"], "ruleset": {"encumbrance": "heavy"}},
        {"character_ids": ["chr_x"], "ruleset": {"no_such_rule": True}},
        {"character_ids": ["chr_x"], "name": ""},
    ],
)
def test_a_malformed_session_request_is_a_validation_error(referee: TestClient, fields: dict[str, Any]) -> None:
    problem(referee.post("/v1/sessions", json={"adventure_id": BARROW_ID} | fields), 422, "validation_error")


def test_a_party_osrlib_refuses_is_party_invalid(referee: TestClient) -> None:
    problem(referee.post("/v1/sessions", json={"adventure_id": BARROW_ID, "character_ids": []}), 422, "party_invalid")
    empty = {"kind": "party", "schema_version": SCHEMA_VERSION, "engine_version": engine_version()}
    empty_party = {"adventure_id": BARROW_ID, "party_document": empty | {"payload": {"characters": []}}}
    problem(referee.post("/v1/sessions", json=empty_party), 422, "party_invalid")
    twin = roll_characters()[0].model_copy(update={"id": "character-0002"})
    twins = {"adventure_id": BARROW_ID, "party_document": party_to_document([twin, twin])}
    problem(referee.post("/v1/sessions", json=twins), 422, "party_invalid")
    broken = {"adventure_id": BARROW_ID, "party_document": {"kind": "save", "payload": {}}}
    problem(referee.post("/v1/sessions", json=broken), 422, "content_invalid")
    assert ok(referee.get("/v1/sessions"))["total"] == 0


def test_unknown_adventures_and_characters_are_404(referee: TestClient, other: TestClient) -> None:
    ash, _ = store_characters(referee)
    for body, missing, resource in (
        ({"adventure_id": "adv_" + "x" * 22, "character_ids": [ash]}, "adv_" + "x" * 22, "adventure"),
        ({"adventure_id": "builtin-nope", "character_ids": [ash]}, "builtin-nope", "adventure"),
        ({"adventure_id": BARROW_ID, "character_ids": [ash, "chr_missing"]}, "chr_missing", "character"),
    ):
        found = problem(referee.post("/v1/sessions", json=body), 404, "not_found")
        assert (found["resource"], found["id"]) == (resource, missing)
    borrowed = problem(
        other.post("/v1/sessions", json={"adventure_id": BARROW_ID, "character_ids": [ash]}), 404, "not_found"
    )
    assert borrowed["id"] == ash


def test_a_ruleset_reaches_osrlib(referee: TestClient) -> None:
    rules = Ruleset(individual_initiative=True)
    session = start(referee, ruleset={"individual_initiative": True})
    assert ok(referee.get(f"/v1/sessions/{session['id']}/referee-view"))["ruleset"] == rules.model_dump(mode="json")
    plain = start(referee)
    assert ok(referee.get(f"/v1/sessions/{plain['id']}/referee-view"))["ruleset"] == Ruleset().model_dump(mode="json")
    # A client sends a table's whole ruleset, with the creation house rules in it, and the session keeps it.
    house = Ruleset(ability_score_method="4d6_drop_lowest", max_hp_at_first_level=True)
    table = start(referee, ruleset=house.model_dump(mode="json"))
    assert ok(referee.get(f"/v1/sessions/{table['id']}/referee-view"))["ruleset"] == house.model_dump(mode="json")


# Session info


def test_info_follows_play_and_rename_keeps_the_game(referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    for command in ROUTE:
        send(referee, session["id"], command)
        oracle.execute(command)
    current = ok(referee.get(f"/v1/sessions/{session['id']}"))
    clock = GameClock(rounds=oracle.game.clock.rounds)
    assert current["clock"] == {"rounds": clock.rounds, "turns": clock.turns, "days": clock.days}
    assert (current["mode"], current["revision"], current["created_at"]) == ("exploring", 5, session["created_at"])
    renamed = ok(referee.patch(f"/v1/sessions/{session['id']}", json={"name": "Second delve"}))
    assert renamed == current | {"name": "Second delve"}
    assert ok(referee.get(f"/v1/sessions/{session['id']}")) == renamed
    problem(referee.patch(f"/v1/sessions/{session['id']}", json={"name": "x", "nmae": "y"}), 422, "validation_error")
    problem(referee.patch(f"/v1/sessions/{session['id']}", json={}), 422, "validation_error")


def test_a_rename_in_another_application_shows_at_once(referee: TestClient, player: TestClient) -> None:
    session = start(referee)
    send(player, session["id"], ENTER)
    ok(referee.patch(f"/v1/sessions/{session['id']}", json={"name": "Renamed"}))
    assert ok(player.get(f"/v1/sessions/{session['id']}"))["name"] == "Renamed"
    assert send(player, session["id"], EAST)["revision"] == 2


def test_list_pages_and_filters_by_mode(referee: TestClient) -> None:
    first = start(referee, name="Stays in town")
    second = start(referee, name="Goes in")
    send(referee, second["id"], ENTER)
    every = ok(referee.get("/v1/sessions"))
    assert [item["id"] for item in every["items"]] == [second["id"], first["id"]]
    assert (every["total"], every["limit"], every["offset"]) == (2, 50, 0)
    assert every["items"][0] == ok(referee.get(f"/v1/sessions/{second['id']}"))
    exploring = ok(referee.get("/v1/sessions", params={"mode": "exploring"}))
    assert ([item["id"] for item in exploring["items"]], exploring["total"]) == ([second["id"]], 1)
    assert ok(referee.get("/v1/sessions", params={"mode": "victory"}))["items"] == []
    page = ok(referee.get("/v1/sessions", params={"limit": 1, "offset": 1}))
    assert [item["id"] for item in page["items"]] == [first["id"]]
    problem(referee.get("/v1/sessions", params={"mode": "flying"}), 422, "validation_error")


def test_delete_removes_the_session_in_every_application(referee: TestClient, player: TestClient) -> None:
    session = start(referee)
    session_id = session["id"]
    send(player, session_id, ENTER)
    send(referee, session_id, EAST)
    ok(referee.delete(f"/v1/sessions/{session_id}"), 204)
    for client in (referee, player):
        for response in (
            client.get(f"/v1/sessions/{session_id}"),
            client.get(f"/v1/sessions/{session_id}/view"),
            client.get(f"/v1/sessions/{session_id}/events"),
            client.post(f"/v1/sessions/{session_id}/commands", json=WEST),
            client.delete(f"/v1/sessions/{session_id}"),
        ):
            assert problem(response, 404, "not_found")["id"] == session_id
    assert ok(player.get("/v1/sessions"))["total"] == 0


# Commands


def test_the_commands_endpoint_lists_by_mode_and_role(player: TestClient, referee: TestClient) -> None:
    session = start(referee)

    def expected(mode: SessionMode, *, play_only: bool) -> list[dict[str, str]]:
        listed = []
        for command_class in ALL_COMMAND_CLASSES:
            command_type = command_class.model_fields["command_type"].default
            kind = command_kind(command_type).value
            if mode in command_class.allowed_modes and (kind == "play" or not play_only):
                listed.append({"command_type": command_type, "kind": kind})
        return listed

    for revision, mode in ((0, SessionMode.TOWN), (1, SessionMode.EXPLORING)):
        as_player = ok(player.get(f"/v1/sessions/{session['id']}/commands"))
        assert as_player == {"mode": mode.value, "revision": revision, "commands": expected(mode, play_only=True)}
        as_referee = ok(referee.get(f"/v1/sessions/{session['id']}/commands"))
        assert as_referee["commands"] == expected(mode, play_only=False)
        send(referee, session["id"], ENTER)


def test_a_refused_command_is_a_200_that_changes_nothing(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    send(player, session["id"], ENTER)
    before = state(player, session["id"])
    document = database(referee).get_session_with_document("game", session["id"])
    outcome = send(player, session["id"], NORTH)
    assert outcome["accepted"] is False and outcome["rejections"] and outcome["events"] == outcome["messages"] == []
    assert outcome["revision"] == 1
    assert state(player, session["id"]) == before
    assert database(referee).get_session_with_document("game", session["id"]) == document


@pytest.mark.parametrize(
    "body",
    [
        {"command_type": "no_such_command"},
        {"command_type": "move_party"},
        {"command_type": "move_party", "direction": "up"},
        {"direction": "north"},
        ["move_party"],
    ],
)
def test_an_unknown_or_malformed_command_is_422(player: TestClient, referee: TestClient, body: Any) -> None:
    session = start(referee)
    before = state(player, session["id"])
    problem(player.post(f"/v1/sessions/{session['id']}/commands", json=body), 422, "validation_error")
    assert state(player, session["id"]) == before


def test_an_unknown_command_type_gets_a_short_message(referee: TestClient) -> None:
    session = start(referee)
    refused = problem(
        referee.post(f"/v1/sessions/{session['id']}/commands", json={"command_type": "fly"}), 422, "validation_error"
    )
    [error] = refused["errors"]
    assert error["type"] == "union_tag_invalid"
    assert error["msg"] == "'fly' isn't a value that 'command_type' accepts."


def test_a_long_unknown_command_type_is_cut_in_the_message(referee: TestClient) -> None:
    session = start(referee)
    refused = problem(
        referee.post(f"/v1/sessions/{session['id']}/commands", json={"command_type": "x" * 500}),
        422,
        "validation_error",
    )
    [error] = refused["errors"]
    assert error["msg"] == f"'{'x' * 61}...' isn't a value that 'command_type' accepts."


def test_a_command_with_no_command_type_gets_a_short_message(referee: TestClient) -> None:
    session = start(referee)
    refused = problem(
        referee.post(f"/v1/sessions/{session['id']}/commands", json={"direction": "north"}), 422, "validation_error"
    )
    [error] = refused["errors"]
    assert error["type"] == "union_tag_not_found"
    assert error["msg"] == "The body needs 'command_type'."


def test_an_unknown_field_in_a_command_is_ignored(referee: TestClient) -> None:
    session = start(referee)
    assert send(referee, session["id"], ENTER | {"from_a_newer_client": 1}) == Oracle(roll_characters()).execute(ENTER)


@pytest.mark.parametrize("command", [JOURNAL, NOTE, {"command_type": "set_flag", "key": "cheat", "value": True}])
def test_a_player_cannot_send_a_referee_command(player: TestClient, referee: TestClient, command: Any) -> None:
    session = start(referee)
    before = state(player, session["id"])
    refused = problem(player.post(f"/v1/sessions/{session['id']}/commands", json=command), 403, "referee_command")
    assert refused["command_type"] == command["command_type"]
    assert state(player, session["id"]) == before
    assert send(referee, session["id"], command)["accepted"]


def test_referee_events_go_only_to_a_referee_who_asks(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    send(referee, session["id"], ENTER)
    oracle.execute(ENTER)
    as_player = send(referee, session["id"], NOTE)
    assert as_player["accepted"] and as_player["events"] == [] and as_player["messages"] == []
    oracle.execute(NOTE)
    as_referee = send(referee, session["id"], NOTE, visibility="referee")
    assert as_referee == oracle.execute(NOTE, Visibility.REFEREE)
    assert {event["visibility"] for event in as_referee["events"]} == {"referee"}

    before = state(player, session["id"])
    problem(
        player.post(f"/v1/sessions/{session['id']}/commands", json=EAST, params={"visibility": "referee"}),
        403,
        "referee_required",
    )
    assert state(player, session["id"]) == before
    problem(
        referee.post(f"/v1/sessions/{session['id']}/commands", json=EAST, params={"visibility": "x"}),
        422,
        "validation_error",
    )


def test_expected_revision(referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    assert send(referee, session["id"], ENTER, expected_revision=0) == oracle.execute(ENTER)
    stale = referee.post(f"/v1/sessions/{session['id']}/commands", json=EAST, params={"expected_revision": 0})
    assert problem(stale, 409, "revision_conflict")["revision"] == 1
    assert ok(referee.get(f"/v1/sessions/{session['id']}"))["revision"] == 1
    assert send(referee, session["id"], EAST, expected_revision=1) == oracle.execute(EAST)
    problem(
        referee.post(f"/v1/sessions/{session['id']}/commands", json=EAST, params={"expected_revision": -1}),
        422,
        "validation_error",
    )


def test_the_openapi_document_describes_every_command_and_event(client: TestClient) -> None:
    document = ok(client.get("/openapi.json"))
    operation = document["paths"]["/v1/sessions/{session_id}/commands"]["post"]
    union = operation["requestBody"]["content"]["application/json"]["schema"]
    schemas = document["components"]["schemas"]
    referenced = {entry["$ref"].rsplit("/", 1)[-1] for entry in union["oneOf"]}
    command_types = {schemas[name]["properties"]["command_type"].get("const") for name in referenced}
    assert command_types == {command.model_fields["command_type"].default for command in ALL_COMMAND_CLASSES}
    events = schemas["CommandOutcome"]["properties"]["events"]["items"]
    assert len(events["oneOf"]) > 50 and events["discriminator"]["propertyName"] == "event_type"
    assert operation["operationId"] == "send_command"


# Battle round previews


def preview(client: TestClient, session_id: str, declarations: list[dict[str, Any]]) -> Any:
    return client.post(f"/v1/sessions/{session_id}/battle-round/preview", json={"declarations": declarations})


def test_a_preview_is_osrlibs_and_changes_nothing(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    for command in TO_BATTLE:
        send(player, session["id"], command)
        oracle.execute(command)
    declarers = ok(player.get(f"/v1/sessions/{session['id']}/view"))["encounter"]["declarers"]
    before = state(player, session["id"])
    plans = [
        [],
        [{"character_id": declarers[0], "action": "attack", "target_group_id": "group-0001"}],
        [
            {"character_id": member, "action": "attack", "move": "close", "target_group_id": "group-0001"}
            for member in declarers
        ],
    ]
    for plan in plans:
        expected = preview_battle_round(oracle.game, [BattleDeclaration.model_validate(entry) for entry in plan])
        assert ok(preview(player, session["id"], plan)) == expected.model_dump(mode="json")
    assert state(player, session["id"]) == before
    # The preview takes no random number, so the round plays as it does in a session that never previewed.
    attack = {"command_type": "resolve_battle_round", "declarations": plans[2]}
    assert send(player, session["id"], attack) == oracle.execute(attack)


def test_a_preview_attributes_each_rejection_to_its_member(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    for command in TO_BATTLE:
        send(player, session["id"], command)
    first, second = ok(player.get(f"/v1/sessions/{session['id']}/view"))["encounter"]["declarers"]
    plan = [
        {"character_id": first, "action": "hold"},
        {"character_id": second, "action": "attack", "target_group_id": "group-0001"},
    ]
    rejections = ok(preview(player, session["id"], plan))["rejections"]
    assert rejections[first] == []
    assert [rejection["code"] for rejection in rejections[second]] == ["combat.attack.out_of_reach"]
    assert rejections[second][0]["params"]["character"] == second
    # The refused round names the member too, so a client can attribute it without a preview.
    refused = send(player, session["id"], {"command_type": "resolve_battle_round", "declarations": plan})
    assert refused["accepted"] is False
    assert [rejection["params"]["character"] for rejection in refused["rejections"]] == [second]


def test_a_preview_outside_a_battle_is_409(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    found = problem(preview(player, session["id"], []), 409, "not_in_battle")
    assert found["mode"] == "town"


@pytest.mark.parametrize(
    "plan",
    [
        [{"character_id": "character-0099", "action": "hold"}],
        [{"character_id": "character-0001", "action": "hold"}, {"character_id": "character-0001", "action": "hold"}],
    ],
)
def test_a_preview_of_a_member_who_cannot_declare_is_422(
    player: TestClient, referee: TestClient, plan: list[dict[str, Any]]
) -> None:
    session = start(referee)
    for command in TO_BATTLE:
        send(player, session["id"], command)
    problem(preview(player, session["id"], plan), 422, "declaration_invalid")


@pytest.mark.parametrize(
    "body",
    [
        {"declarations": [{"character_id": "character-0001", "action": "hold"}] * (MAX_PARTY_SIZE + 1)},
        {"declarations": [], "expected_revision": 3},
        {"declarations": [{"character_id": "character-0001", "action": "dance"}]},
        {},
    ],
)
def test_a_malformed_preview_is_a_validation_error(referee: TestClient, body: dict[str, Any]) -> None:
    session = start(referee)
    response = referee.post(f"/v1/sessions/{session['id']}/battle-round/preview", json=body)
    problem(response, 422, "validation_error")


# Session rules


def bundled_spell(source_id: str, spell_id: str, name: str) -> SpellTemplate:
    """Return a copy of a built-in spell under a new id and name, as an adventure bundles one."""
    return load_spells().get(source_id).model_copy(update={"id": spell_id, "name": name})


GLIMMER = bundled_spell("magic_missile", "glimmer", "Glimmer")
BALM = bundled_spell("cure_light_wounds", "balm", "Balm")
WITCH = load_classes().get("magic_user").model_copy(update={"id": "witch", "name": "Witch"})


def start_with_custom_content(referee: TestClient) -> tuple[str, ClassDefinition]:
    """Start a session whose adventure bundles two spells, with a witch who has one of them in her book.

    Returns the session's id and the definition of the witch's class.
    """
    adventure = BARROW.model_copy(update={"spells": (GLIMMER, BALM)})
    uploaded = ok(referee.post("/v1/adventures", json=adventure_document(adventure)), 201)
    hex_the_witch = create_character(
        name="Hex",
        class_id=WITCH.id,
        custom_class=WITCH,
        alignment=Alignment.NEUTRAL,
        ruleset=Ruleset(),
        stream=RngStreams(master_seed=8).get(CHARACTER_CREATION_STREAM),
        spell_catalog=load_spells().model_copy(update={"spells": (*load_spells().spells, GLIMMER)}),
        starting_spell_ids=[GLIMMER.id],
    ).character
    party = party_to_document([*roll_characters(), hex_the_witch])
    session = start(referee, adventure_id=uploaded["id"], party_document=party)
    return session["id"], hex_the_witch.definition


def test_a_session_serves_the_spells_its_player_view_names(player: TestClient, referee: TestClient) -> None:
    session_id, _ = start_with_custom_content(referee)
    names = ok(player.get(f"/v1/sessions/{session_id}/view"))["spell_names"]
    assert names == {GLIMMER.id: GLIMMER.name}
    for caller in (player, referee):
        served = ok(caller.get(f"/v1/sessions/{session_id}/rules/spells/{GLIMMER.id}"))
        assert served == GLIMMER.model_dump(mode="json")
    problem(player.get(f"/v1/rules/spells/{GLIMMER.id}"), 404, "not_found")
    # A spell the view doesn't name gets the same answer whether the adventure bundles it, osrlib has it, or neither.
    answers = []
    for spell_id in (BALM.id, "fire_ball", "no_such_spell"):
        for caller in (player, referee):
            found = problem(caller.get(f"/v1/sessions/{session_id}/rules/spells/{spell_id}"), 404, "not_found")
            assert (found["resource"], found["id"]) == ("spell", spell_id)
            answers.append(found["detail"].replace(spell_id, "<id>"))
    assert len(set(answers)) == 1


def test_a_session_serves_a_bundled_spell_once_a_member_could_prepare_it(
    player: TestClient, referee: TestClient
) -> None:
    session_id, _ = start_with_custom_content(referee)
    path = f"/v1/sessions/{session_id}/rules/spells/{BALM.id}"
    before = ok(player.get(f"/v1/sessions/{session_id}/view"))
    cleric = next(member for member in before["party"] if member["class_id"] == "cleric")
    assert cleric["preparable_spells"] == [] and BALM.id not in before["spell_names"]
    problem(player.get(path), 404, "not_found")
    # A cleric gets a first spell slot at 2nd level, and osrlib then lists the spells the cleric could prepare.
    award = {"command_type": "award_xp", "character_id": cleric["id"], "amount": 2500}
    assert send(referee, session_id, award)["accepted"]
    after = ok(player.get(f"/v1/sessions/{session_id}/view"))
    cleric = next(member for member in after["party"] if member["class_id"] == "cleric")
    assert cleric["level"] == 2 and BALM.id in cleric["preparable_spells"]
    assert after["spell_names"][BALM.id] == BALM.name and after["spell_levels"][BALM.id] == BALM.level
    assert set(cleric["preparable_spells"]) <= set(after["spell_names"])
    for caller in (player, referee):
        assert ok(caller.get(path)) == BALM.model_dump(mode="json")
    for spell_id in cleric["preparable_spells"]:
        ok(player.get(f"/v1/sessions/{session_id}/rules/spells/{spell_id}"))


def test_a_session_serves_the_classes_of_its_party(player: TestClient, referee: TestClient) -> None:
    session_id, witch = start_with_custom_content(referee)
    party = ok(player.get(f"/v1/sessions/{session_id}/view"))["party"]
    assert [member["class_id"] for member in party] == ["fighter", "cleric", WITCH.id]
    path = f"/v1/sessions/{session_id}/rules/classes"
    assert ok(player.get(f"{path}/{WITCH.id}")) == witch.model_dump(mode="json")
    assert ok(player.get(f"{path}/{WITCH.id}"))["name"] == party[2]["class_name"]
    assert ok(player.get(f"{path}/cleric")) == ok(player.get("/v1/rules/classes/cleric"))
    problem(player.get(f"/v1/rules/classes/{WITCH.id}"), 404, "not_found")
    for class_id in ("thief", "no_such_class"):
        found = problem(referee.get(f"{path}/{class_id}"), 404, "not_found")
        assert (found["resource"], found["id"]) == ("class", class_id)


def test_session_rules_change_nothing(player: TestClient, referee: TestClient) -> None:
    session_id, _ = start_with_custom_content(referee)
    before = state(player, session_id)
    ok(player.get(f"/v1/sessions/{session_id}/rules/spells/{GLIMMER.id}"))
    ok(player.get(f"/v1/sessions/{session_id}/rules/classes/{WITCH.id}"))
    assert state(player, session_id) == before


# Views and events


def test_the_views_equal_osrlib_and_the_referee_view_is_for_referees(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    for command in ROUTE:
        send(player, session["id"], command)
        oracle.execute(command)
        assert ok(player.get(f"/v1/sessions/{session['id']}/view")) == oracle.view()
    problem(player.get(f"/v1/sessions/{session['id']}/referee-view"), 403, "referee_required")
    assert split_handles(ok(referee.get(f"/v1/sessions/{session['id']}/referee-view"))) == split_handles(
        oracle.view(Visibility.REFEREE)
    )


def test_the_event_log_pages_by_visibility(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    path = f"/v1/sessions/{session['id']}/events"
    assert ok(player.get(path)) == {"events": [], "messages": [], "next_after": 0, "total": 0}
    oracle = Oracle(roll_characters())
    for command in [*ROUTE, NOTE, EAST, NOTE]:
        send(referee, session["id"], command)
        oracle.execute(command)
    shown, everything = oracle.log(), oracle.log(Visibility.REFEREE)
    assert len(everything) > len(shown) > 4

    def page(client: TestClient, **params: Any) -> dict[str, Any]:
        return ok(client.get(path, params=params))

    middle = page(player, after=2, limit=3)
    assert middle == {
        "events": [event.model_dump(mode="json") for event in shown[2:5]],
        "messages": [format_message(event) for event in shown[2:5]],
        "next_after": 5,
        "total": len(shown),
    }
    every = page(referee, visibility="referee", limit=500)
    assert every["events"] == [event.model_dump(mode="json") for event in everything]
    assert every["messages"] == [format_message(event) for event in everything]
    assert page(referee, limit=500)["events"] == [event.model_dump(mode="json") for event in shown]
    end = len(shown)
    assert page(player, after=end + 10) == {"events": [], "messages": [], "next_after": end + 10, "total": end}
    problem(player.get(path, params={"visibility": "referee"}), 403, "referee_required")
    for params in ({"limit": 0}, {"limit": 501}, {"after": -1}):
        problem(player.get(path, params=params), 422, "validation_error")


def test_a_player_never_receives_the_seed_or_a_referee_event(player: TestClient, referee: TestClient) -> None:
    session = start(referee, seed=BIG_SEED)
    session_id = session["id"]
    bodies = []
    for command in ROUTE:
        bodies.append(player.post(f"/v1/sessions/{session_id}/commands", json=command).text)
        send(referee, session_id, NOTE)
        for path in ("", "/view", "/commands", "/events?limit=500"):
            bodies.append(player.get(f"/v1/sessions/{session_id}{path}").text)
    bodies.append(player.get("/v1/sessions").text)
    assert str(BIG_SEED) not in "\n".join(bodies)
    log = ok(player.get(f"/v1/sessions/{session_id}/events", params={"limit": 500}))["events"]
    assert log and all(event["visibility"] == "player" for event in log)
    assert "master_seed" not in ok(referee.get(f"/v1/sessions/{session_id}/referee-view"))


# Durability and concurrency


def test_a_rebuilt_application_continues_every_session(keyed_settings: Settings) -> None:
    oracle = Oracle(roll_characters())
    with TestClient(create_app(keyed_settings), headers=bearer(REFEREE_KEY)) as before:
        session = start(before)
        for command in ROUTE[:3]:
            assert send(before, session["id"], command) == oracle.execute(command)
    with TestClient(create_app(keyed_settings), headers=bearer(REFEREE_KEY)) as after:
        assert ok(after.get(f"/v1/sessions/{session['id']}"))["revision"] == 3
        assert ok(after.get(f"/v1/sessions/{session['id']}/view")) == oracle.view()
        for command in ROUTE[3:]:
            assert send(after, session["id"], command) == oracle.execute(command)
        assert split_handles(ok(after.get(f"/v1/sessions/{session['id']}/referee-view"))) == split_handles(
            oracle.view(Visibility.REFEREE)
        )


def test_a_cache_of_one_alternates_sessions(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(keyed_settings.model_copy(update={"session_cache_size": 1}), REFEREE_KEY)
    first, second = start(client), start(client, seed=SEED + 1)
    oracles = {first["id"]: Oracle(roll_characters()), second["id"]: Oracle(roll_characters(), seed=SEED + 1)}
    for command in ROUTE:
        for session_id, oracle in oracles.items():
            assert send(client, session_id, command) == oracle.execute(command)
    for session_id, oracle in oracles.items():
        assert split_handles(ok(client.get(f"/v1/sessions/{session_id}/referee-view"))) == split_handles(
            oracle.view(Visibility.REFEREE)
        )


def test_two_applications_take_turns(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    oracle = Oracle(roll_characters())
    for index, command in enumerate(ROUTE):
        sender, reader = (player, referee) if index % 2 == 0 else (referee, player)
        assert send(sender, session["id"], command) == oracle.execute(command)
        assert ok(reader.get(f"/v1/sessions/{session['id']}/view")) == oracle.view()
    assert ok(player.get(f"/v1/sessions/{session['id']}"))["revision"] == len(ROUTE)


# Tenancy


def test_another_client_cannot_read_or_change_a_session(referee: TestClient, other: TestClient) -> None:
    session = start(referee)
    session_id = session["id"]
    send(referee, session_id, ENTER)
    for response in (
        other.get(f"/v1/sessions/{session_id}"),
        other.patch(f"/v1/sessions/{session_id}", json={"name": "Taken"}),
        other.delete(f"/v1/sessions/{session_id}"),
        other.get(f"/v1/sessions/{session_id}/commands"),
        other.post(f"/v1/sessions/{session_id}/commands", json=EAST),
        other.post(f"/v1/sessions/{session_id}/battle-round/preview", json={"declarations": []}),
        other.get(f"/v1/sessions/{session_id}/view"),
        other.get(f"/v1/sessions/{session_id}/referee-view"),
        other.get(f"/v1/sessions/{session_id}/rules/spells/sleep"),
        other.get(f"/v1/sessions/{session_id}/rules/classes/fighter"),
        other.get(f"/v1/sessions/{session_id}/events"),
        other.post(f"/v1/sessions/{session_id}/saves", json={}),
    ):
        found = problem(response, 404, "not_found")
        assert (found["resource"], found["id"]) == ("session", session_id)
    assert ok(other.get("/v1/sessions"))["total"] == 0
    current = ok(referee.get(f"/v1/sessions/{session_id}"))
    assert (current["name"], current["revision"]) == (BARROW.name, 1)


def test_a_request_without_a_key_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    anonymous = make_client(keyed_settings)
    problem(anonymous.get("/v1/sessions"), 401, "unauthorized")
    problem(anonymous.post("/v1/sessions/ses_x/commands", json=ENTER), 401, "unauthorized")


# Review fixes


def test_a_party_has_at_most_twenty_members(referee: TestClient) -> None:
    ash, _ = store_characters(referee)
    too_many = {"adventure_id": BARROW_ID, "character_ids": [ash] * (MAX_PARTY_SIZE + 1)}
    problem(referee.post("/v1/sessions", json=too_many), 422, "validation_error")
    character = roll_characters()[0]
    document = {"adventure_id": BARROW_ID, "party_document": party_to_document([character] * (MAX_PARTY_SIZE + 1))}
    problem(referee.post("/v1/sessions", json=document), 422, "party_invalid")
    assert ok(referee.get("/v1/sessions"))["total"] == 0
    largest = ok(referee.post("/v1/sessions", json={"adventure_id": BARROW_ID, "character_ids": [ash] * 20}), 201)
    assert len(ok(referee.get(f"/v1/sessions/{largest['id']}/view"))["party"]) == MAX_PARTY_SIZE


def test_only_a_referee_sets_a_command_source(player: TestClient, referee: TestClient) -> None:
    session = start(referee)
    before = state(player, session["id"])
    forged = player.post(f"/v1/sessions/{session['id']}/commands", json=ENTER | {"source": "quest:the-idol"})
    problem(forged, 403, "referee_required")
    assert state(player, session["id"]) == before
    assert send(referee, session["id"], ENTER | {"source": "dm-screen"})["accepted"]
    logged = ok(referee.get(f"/v1/sessions/{session['id']}/referee-view"))["command_log"]
    assert [entry["source"] for entry in logged if entry["command_type"] == "enter_dungeon"] == ["dm-screen"]


def test_a_stored_session_osrlib_cannot_load_is_unreadable_without_details(
    player: TestClient, referee: TestClient, keyed_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    session = start(referee)
    send(referee, session["id"], ENTER)
    document = ok(referee.post(f"/v1/sessions/{session['id']}/saves"), 201)
    stored = ok(referee.get(f"/v1/saves/{document['id']}/document"))
    stored["payload"]["mode"] = "SECRET-TRAP-AT-3,4"
    with sqlite3.connect(keyed_settings.database) as connection:
        connection.execute(
            "UPDATE sessions SET document = ?, revision = revision + 1 WHERE id = ?",
            (json.dumps(stored), session["id"]),
        )
    session_id = session["id"]
    with caplog.at_level(logging.ERROR, logger="bxapi"):
        responses = [
            player.get(f"/v1/sessions/{session_id}/view"),
            player.get(f"/v1/sessions/{session_id}/events"),
            player.post(f"/v1/sessions/{session_id}/commands", json=EAST),
            referee.get(f"/v1/sessions/{session_id}/referee-view"),
        ]
    for response in responses:
        problem(response, 500, "session_unreadable")
        assert problem_without_id(response) == session_unreadable("session").problem().model_dump(mode="json")
        assert "SECRET" not in response.text
    assert session_id in caplog.text and "SECRET-TRAP-AT-3,4" in caplog.text
    assert ok(player.get(f"/v1/sessions/{session_id}"))["revision"] == 2


def test_session_busy_has_the_revision_when_a_request_waits_too_long(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(keyed_settings, REFEREE_KEY)
    assert isinstance(client.app, FastAPI)
    client.app.state.sessions = SessionManager(client.app.state.db, cache_size=4, lock_timeout=0.05)
    session = start(client)
    send(client, session["id"], ENTER)
    entered, release = threading.Event(), threading.Event()

    def hold(_: GameSession) -> None:
        entered.set()
        release.wait(5)

    holder = threading.Thread(target=manager(client).read, args=("game", session["id"], hold))
    holder.start()
    try:
        assert entered.wait(5)
        busy = problem(client.post(f"/v1/sessions/{session['id']}/commands", json=EAST), 409, "session_busy")
        assert busy.pop("request_id").startswith("req_")
        assert busy == session_busy(1, waited_seconds=0.05).problem().model_dump(mode="json")
        problem(client.get(f"/v1/sessions/{session['id']}/view"), 409, "session_busy")
    finally:
        release.set()
        holder.join(5)
    assert ok(client.get(f"/v1/sessions/{session['id']}"))["revision"] == 1
    assert send(client, session["id"], EAST)["revision"] == 2


def test_the_openapi_document_names_the_bodies_and_the_errors(client: TestClient) -> None:
    paths = ok(client.get("/openapi.json"))["paths"]
    save_body = paths["/v1/sessions/{session_id}/saves"]["post"]["requestBody"]
    restore_body = paths["/v1/saves/{save_id}/restore"]["post"]["requestBody"]
    assert save_body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/CreateSaveRequest"}
    assert restore_body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/RestoreSaveRequest"}
    assert not save_body.get("required") and not restore_body.get("required")
    for path, method in (
        ("/v1/sessions/{session_id}/commands", "post"),
        ("/v1/sessions/{session_id}/view", "get"),
        ("/v1/sessions/{session_id}/referee-view", "get"),
        ("/v1/sessions/{session_id}/events", "get"),
        ("/v1/saves/{save_id}/restore", "post"),
    ):
        assert "500" in paths[path][method]["responses"], path
    assert "409" in paths["/v1/sessions/{session_id}/view"]["get"]["responses"]


LOOP_DUNGEON = "loop"
HALL_TRIGGER = "trig-hall"


def repeating_adventure() -> Adventure:
    """Return an adventure whose one trigger fires each time the party enters the hall, east of the entrance."""
    level = LevelSpec(
        number=1,
        width=2,
        height=1,
        entrance=(0, 0),
        wandering=WanderingSpec(chance_in_six=0),
        edges={"1,0:west": Edge(kind=EdgeKind.OPEN)},
        areas=(AreaSpec(id="hall", name="Hall", description="An empty hall.", cells=((1, 0),)),),
    )
    trigger = TriggerSpec(
        id=HALL_TRIGGER,
        when=AreaEnteredPattern(dungeon_id=LOOP_DUNGEON, level_number=1, area_id="hall"),
        repeatable=True,
        consequences=(SetFlag(key="hall-entered", value=True),),
    )
    return Adventure(
        name="The Loop",
        town=TownSpec(name="Loopton", travel_turns={LOOP_DUNGEON: 1}),
        dungeons=(DungeonSpec(id=LOOP_DUNGEON, name="The Loop", levels=(level,)),),
        triggers=(trigger,),
    )


def firings(outcome: dict[str, Any]) -> int:
    return sum(1 for event in outcome["events"] if event["event_type"] == "trigger_fired")


def check_loop(client: TestClient, session_id: str, oracle: Oracle, *, evict: Callable[[], object]) -> None:
    """Walk in and out of the hall, and check that the trigger fires once each time, as with one `Interpreter`."""
    for _ in range(3):
        for command, fired in ((EAST, 1), (WEST, 0)):
            evict()
            outcome = send(client, session_id, command, visibility="referee")
            assert outcome == oracle.execute(command, Visibility.REFEREE)
            assert firings(outcome) == fired
            assert manager(client).read("game", session_id, lambda game: len(game.listeners)) == 1


def test_the_interpreter_runs_once_after_a_reload_a_restore_and_an_import(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(keyed_settings.model_copy(update={"session_cache_size": 1}), REFEREE_KEY)
    loop = repeating_adventure()
    uploaded = ok(client.post("/v1/adventures", json=adventure_document(loop)), 201)
    session = start(client, adventure_id=uploaded["id"])
    other = start(client, adventure_id=uploaded["id"], seed=SEED + 1)
    oracle = Oracle(roll_characters(), adventure=loop)
    enter = {"command_type": "enter_dungeon", "dungeon_id": LOOP_DUNGEON}
    assert send(client, session["id"], enter) == oracle.execute(enter)

    def evict() -> None:
        ok(client.get(f"/v1/sessions/{other['id']}/view"))
        assert manager(client).loaded() == [("game", other["id"])]

    check_loop(client, session["id"], oracle, evict=evict)

    created = ok(client.post(f"/v1/sessions/{session['id']}/saves"), 201)
    document = ok(client.get(f"/v1/saves/{created['id']}/document"))
    restored = ok(client.post(f"/v1/saves/{created['id']}/restore"), 201)
    check_loop(client, restored["id"], Oracle.loaded(document), evict=evict)

    imported = ok(client.post("/v1/saves/import", json=document), 201)
    from_import = ok(client.post(f"/v1/saves/{imported['id']}/restore"), 201)
    check_loop(client, from_import["id"], Oracle.loaded(document), evict=evict)
