"""The test adventure, the test party, osrlib oracles, and HTTP helpers for the acceptance tests.

Everything here except the HTTP helpers runs without a server. A test builds its oracle before it
sends its first request, so a mistake in the test's own setup fails before the API is involved.

The `player` and `referee` fixtures in `tests/conftest.py` are two applications that share one
database file, so a session one of them creates is loaded from the database by the other.
"""

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from httpx2 import Response
from osrlib.core.alignment import Alignment
from osrlib.core.character import CHARACTER_CREATION_STREAM, Character, create_character, party_to_document
from osrlib.core.clock import GameClock
from osrlib.core.events import Event, Visibility
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.crawl.adventure import Adventure, TownSpec
from osrlib.crawl.commands import SessionMode, SetFlag, parse_command
from osrlib.crawl.dungeon import (
    AreaSpec,
    DoorSpec,
    DungeonSpec,
    Edge,
    EdgeKind,
    KeyedEncounter,
    KeyedMonster,
    LevelSpec,
    WanderingSpec,
)
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.narrative import NarrativeBlock
from osrlib.crawl.party import Party
from osrlib.crawl.quests import ObjectiveSpec, QuestSpec, TriggerClause
from osrlib.crawl.session import GameSession
from osrlib.crawl.triggers import AreaEnteredPattern, TriggerSpec
from osrlib.messages import format_message
from osrlib.persistence import load_game, save_game
from osrlib.versioning import SCHEMA_VERSION
from starlette.testclient import TestClient

from bxapi.adventures import adventure_document as bxapi_adventure_document

SEED = 42
"""The master seed the referee passes for a session whose outcomes a test compares with an oracle."""

PARTY_SEED = 1
"""The seed that rolls the test party. With it, both characters can afford the gear they buy."""

ADVENTURE_NAME = "Acceptance Vault"
TOWN_NAME = "Testholm"
DUNGEON_ID = "vault"
DUNGEON_NAME = "The Test Vault"
PLAYER_TEXT = "A vault under Testholm that nobody has opened in a long while."
JOURNAL_TEXT = "Footprints cross the dust of the antechamber."

TRIGGER_ID = "trig-antechamber-secret"
FLAG_KEY = "secret-flag-antechamber"
FIRED_TEXT = "SECRET-FIRED: the referee notes the footprints are fresh."
GUARD_ROOM_TEXT = "SECRET-AREA: two goblins guard a chest of copper."
ANTECHAMBER_TEXT = "SECRET-AREA: a draughty hall with footprints."
SHRINE_TEXT = "SECRET-AREA: a shrine with a cracked altar."
QUEST_ID = "quest-reach-the-shrine"
OBJECTIVE_ID = "objective-secret-shrine"
MONSTER_ID = "goblin"

ADVENTURE_SECRETS = (
    TRIGGER_ID,
    FLAG_KEY,
    FIRED_TEXT,
    GUARD_ROOM_TEXT,
    ANTECHAMBER_TEXT,
    SHRINE_TEXT,
    QUEST_ID,
    OBJECTIVE_ID,
    MONSTER_ID,
    "antechamber",
    "guardroom",
    "shrine",
    JOURNAL_TEXT,
)
"""Strings from the adventure that adventure info must never contain."""

PLAYER_SECRETS = (TRIGGER_ID, FLAG_KEY, FIRED_TEXT, GUARD_ROOM_TEXT, ANTECHAMBER_TEXT, SHRINE_TEXT)
"""Strings from the adventure that no response to a player may contain at any point in play."""

ENTER = {"command_type": "enter_dungeon", "dungeon_id": DUNGEON_ID}
TO_ANTECHAMBER = {"command_type": "move_party", "direction": "east"}
OPEN_GUARD_ROOM = {"command_type": "open_door", "direction": "east"}
TO_GUARD_ROOM = {"command_type": "move_party", "direction": "east"}
TO_SHRINE = {"command_type": "move_party", "direction": "south"}
ENGAGE = {"command_type": "engage_battle"}

ROUTE_TO_ENCOUNTER = (ENTER, TO_ANTECHAMBER, OPEN_GUARD_ROOM, TO_GUARD_ROOM)
"""In town, enter the vault, step into the antechamber (its trigger fires), open the door, and step
into the guard room, where two goblins start an encounter."""

ROUTE_TO_VICTORY = (ENTER, TO_SHRINE)
"""Enter the vault and step south into the shrine, which completes the quest that ends the
adventure."""

ID_PATTERN = r"[A-Za-z0-9_-]{22}"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

SESSION_INFO_KEYS = {
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
SAVE_INFO_KEYS = {"id", "session_id", "adventure_id", "adventure_name", "label", "mode", "clock", "created_at"}
CHARACTER_KEYS = {"id", "name", "class_id", "level", "created_at", "character", "derived"}
CHARACTER_SUMMARY_KEYS = {"id", "name", "class_id", "level", "max_hp", "created_at"}
OUTCOME_KEYS = {"accepted", "rejections", "events", "messages", "mode", "ended", "revision"}


def build_adventure(*, monster_id: str = MONSTER_ID) -> Adventure:
    """Return the test adventure.

    Level 1 of the vault is three cells wide and two high, entered at (0, 0) with no wandering
    monsters. East of the entrance is the antechamber, whose trigger sets a flag and adds a
    journal entry. A closed door leads east from there into the guard room, where two goblins
    wait. South of the entrance is the shrine, and entering it completes the one objective of a
    quest that concludes the adventure. Level 2 is a single cell that nothing leads to, so the
    dungeon has two levels.

    Args:
        monster_id: The template id of the guard room's monsters. Pass an id osrlib doesn't have
            to get an adventure that fails `validate_adventure`.
    """
    quiet = WanderingSpec(chance_in_six=0)
    first = LevelSpec(
        number=1,
        width=3,
        height=2,
        entrance=(0, 0),
        wandering=quiet,
        edges={
            "1,0:west": Edge(kind=EdgeKind.OPEN),
            "2,0:west": Edge(kind=EdgeKind.DOOR, door=DoorSpec()),
            "0,1:north": Edge(kind=EdgeKind.OPEN),
        },
        areas=(
            AreaSpec(id="antechamber", name="Antechamber", description=ANTECHAMBER_TEXT, cells=((1, 0),)),
            AreaSpec(
                id="guardroom",
                name="Guard room",
                description=GUARD_ROOM_TEXT,
                cells=((2, 0),),
                encounter=KeyedEncounter(monsters=(KeyedMonster(template_id=monster_id, count_fixed=2),)),
            ),
            AreaSpec(id="shrine", name="Shrine", description=SHRINE_TEXT, cells=((0, 1),)),
        ),
    )
    second = LevelSpec(number=2, width=1, height=1, entrance=(0, 0), wandering=quiet)
    trigger = TriggerSpec(
        id=TRIGGER_ID,
        when=AreaEnteredPattern(dungeon_id=DUNGEON_ID, level_number=1, area_id="antechamber"),
        consequences=(SetFlag(key=FLAG_KEY, value=True),),
        narrative=NarrativeBlock(fired=FIRED_TEXT, journal=JOURNAL_TEXT),
    )
    quest = QuestSpec(
        id=QUEST_ID,
        name="Reach the shrine",
        objectives=(
            ObjectiveSpec(
                id=OBJECTIVE_ID,
                when=TriggerClause(pattern=AreaEnteredPattern(dungeon_id=DUNGEON_ID, level_number=1, area_id="shrine")),
            ),
        ),
        concludes_adventure=True,
        narrative=NarrativeBlock(offer="Find the shrine under Testholm.", completion="The shrine is found."),
    )
    return Adventure(
        name=ADVENTURE_NAME,
        description=PLAYER_TEXT,
        town=TownSpec(name=TOWN_NAME, travel_turns={DUNGEON_ID: 1}),
        dungeons=(DungeonSpec(id=DUNGEON_ID, name=DUNGEON_NAME, levels=(first, second)),),
        triggers=(trigger,),
        quests=(quest,),
    )


ADVENTURE = build_adventure()


def json_copy[T](value: T) -> T:
    """Return a copy of a JSON value, the way it looks after a trip through the network."""
    return json.loads(json.dumps(value))


def adventure_document(adventure: Adventure = ADVENTURE) -> dict[str, Any]:
    """Return an adventure as the stamped document the API accepts, after a trip through JSON."""
    return json_copy(bxapi_adventure_document(adventure))


def broken_adventure_document() -> dict[str, Any]:
    """Return an adventure document that parses but fails `validate_adventure`: an unknown monster."""
    return adventure_document(build_adventure(monster_id="no_such_monster"))


def future_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a document stamped with a schema version newer than the installed osrlib."""
    newer = copy.deepcopy(dict(document))
    newer["schema_version"] = SCHEMA_VERSION + 1
    return newer


def build_party(seed: int = PARTY_SEED) -> list[Character]:
    """Return the test party: a fighter in plate mail with a sword, and a cleric with a mace."""
    rules = Ruleset()
    stream = RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM)
    fighter = create_character(
        name="Ash",
        class_id="fighter",
        alignment=Alignment.LAWFUL,
        ruleset=rules,
        stream=stream,
        purchases=[("plate_mail", 1), ("sword", 1)],
        equip_ids=["plate_mail", "sword"],
    ).character
    cleric = create_character(
        name="Bree",
        class_id="cleric",
        alignment=Alignment.LAWFUL,
        ruleset=rules,
        stream=stream,
        purchases=[("mace", 1)],
        equip_ids=["mace"],
    ).character
    return [fighter, cleric]


def party_document(seed: int = PARTY_SEED) -> dict[str, Any]:
    """Return the test party as the party document a referee passes to create a session."""
    return json_copy(party_to_document(build_party(seed)))


def clock_of(rounds: int) -> dict[str, int]:
    """Return the `clock` object of session and save info for a clock at `rounds`."""
    clock = GameClock(rounds=rounds)
    return {"rounds": clock.rounds, "turns": clock.turns, "days": clock.days}


def event_json(events: Iterable[Event], visibility: Visibility) -> list[dict[str, Any]]:
    """Return the events a caller with `visibility` sees, serialized the way osrlib serializes them."""
    return [event.model_dump(mode="json") for event in visible(events, visibility)]


def visible(events: Iterable[Event], visibility: Visibility) -> list[Event]:
    """Return the events a caller with `visibility` sees: every event for a referee."""
    return [event for event in events if visibility is Visibility.REFEREE or event.visibility is Visibility.PLAYER]


def battle_round(player_view: Mapping[str, Any]) -> dict[str, Any]:
    """Return a `resolve_battle_round` that every declarer of the current round can make.

    A member in the first group's front rank attacks it, and every other member closes on it.
    The command is built from a serialized player view, as a client builds one.
    """
    encounter = player_view["encounter"]
    assert encounter is not None, "a battle round needs an open battle"
    group = encounter["groups"][0]
    declarations = []
    for member_id in encounter["declarers"]:
        if member_id in group["front_rank"]:
            declarations.append({"character_id": member_id, "action": "attack", "target_group_id": group["id"]})
        else:
            declarations.append(
                {"character_id": member_id, "action": "move", "move": "close", "target_group_id": group["id"]}
            )
    return {"command_type": "resolve_battle_round", "declarations": declarations}


class Oracle:
    """An osrlib session that plays the same game as a session on the server.

    The server registers the `Interpreter` on every session, so the oracle does too. The oracle
    counts revisions the way the spec does: one more for each accepted command.

    Args:
        session: The osrlib session to drive.
        revision: The revision the server's session has at this point.
    """

    def __init__(self, session: GameSession, revision: int = 0) -> None:
        self.session = session
        self.session.register_listener(Interpreter(self.session))
        self.revision = revision
        self.seed = session.master_seed

    @classmethod
    def new(
        cls,
        party: Mapping[str, Any] | Sequence[Character] | None = None,
        *,
        adventure: Adventure = ADVENTURE,
        seed: int = SEED,
        ruleset: Ruleset | None = None,
    ) -> Oracle:
        """Start the oracle the way the server starts a session.

        Args:
            party: A party document, or the characters in marching order. Defaults to the test party.
            adventure: The adventure to play.
            seed: The master seed.
            ruleset: The optional rules. Defaults to the rules as written.
        """
        if party is None:
            members = build_party()
        elif isinstance(party, Mapping):
            members = [Character.model_validate(entry) for entry in party["payload"]["characters"]]
        else:
            members = [character.model_copy(deep=True) for character in party]
        return cls(GameSession.new(Party(members=members), adventure, seed=seed, ruleset=ruleset))

    @classmethod
    def from_stored(cls, stored: Sequence[Mapping[str, Any]], *, seed: int = SEED) -> Oracle:
        """Start the oracle with stored characters, as the API returns them, in marching order."""
        return cls.new([Character.model_validate(entry["character"]) for entry in stored], seed=seed)

    def copy(self, revision: int | None = None) -> Oracle:
        """Return an oracle for a session loaded from this one's save, as a restore makes it.

        Args:
            revision: The revision of the new session. Defaults to this oracle's revision.
        """
        loaded = load_game(json_copy(save_game(self.session)))
        return Oracle(loaded, self.revision if revision is None else revision)

    def execute(self, command: Mapping[str, Any], visibility: Visibility = Visibility.PLAYER) -> dict[str, Any]:
        """Execute a command and return the command outcome the API must return for it."""
        parsed = parse_command(dict(command))
        assert parsed is not None, f"the test sent an unknown command: {command}"
        result = self.session.execute(parsed)
        if result.accepted:
            self.revision += 1
        shown = visible(result.events, visibility)
        return {
            "accepted": result.accepted,
            "rejections": [rejection.model_dump(mode="json") for rejection in result.rejections],
            "events": [event.model_dump(mode="json") for event in shown],
            "messages": [format_message(event) for event in shown],
            "mode": self.session.mode.value,
            "ended": self.session.mode.terminal,
            "revision": self.revision,
        }

    def run(self, commands: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Execute each command, and return the outcomes a player sees."""
        return [self.execute(command) for command in commands]

    def view(self, visibility: Visibility = Visibility.PLAYER) -> dict[str, Any]:
        """Return the view the API must return, serialized."""
        return self.session.view(visibility).model_dump(mode="json")

    def events(self, visibility: Visibility = Visibility.PLAYER) -> list[dict[str, Any]]:
        """Return the event log a caller with `visibility` sees."""
        logged = [event for event in self.session.event_log if isinstance(event, Event)]
        return event_json(logged, visibility)

    def messages(self, visibility: Visibility = Visibility.PLAYER) -> list[str]:
        """Return one message for each event in the log a caller with `visibility` sees."""
        logged = [event for event in self.session.event_log if isinstance(event, Event)]
        return [format_message(event) for event in visible(logged, visibility)]

    @property
    def mode(self) -> SessionMode:
        """The session's mode."""
        return self.session.mode

    def clock(self) -> dict[str, int]:
        """Return the `clock` of session info."""
        return clock_of(self.session.clock.rounds)

    def battle_round(self) -> dict[str, Any]:
        """Return a `resolve_battle_round` for the current round, built from the player view."""
        return battle_round(self.view())

    def fight(self, rounds: int = 3) -> list[dict[str, Any]]:
        """From an encounter or a battle, return commands that open the battle and play up to `rounds` rounds.

        The oracle executes each command as it builds it, so call it on an oracle that the test
        uses only to plan the commands.
        """
        commands: list[dict[str, Any]] = []
        if self.mode is SessionMode.ENCOUNTER:
            commands.append(dict(ENGAGE))
            assert self.execute(ENGAGE)["accepted"]
        for _ in range(rounds):
            if self.mode is not SessionMode.BATTLE:
                break
            command = self.battle_round()
            commands.append(command)
            assert self.execute(command)["accepted"]
        return commands


def plan(commands: Iterable[Mapping[str, Any]], *, seed: int = SEED, rounds: int = 3) -> list[dict[str, Any]]:
    """Return the route to the guard room followed by a fight, for the test party at `seed`.

    The battle commands depend on the dice, so a planning oracle plays the route first to build
    them. The commands in `commands` come first, then the fight.
    """
    planner = Oracle.new(seed=seed)
    route = [dict(command) for command in commands]
    planner.run(route)
    return route + planner.fight(rounds)


def expect(response: Response, status: int, code: str | None = None) -> Any:
    """Check a response's status, and its problem `code` when given, and return its JSON body.

    The failure message names the request and includes the body, so a failure caused by a
    missing endpoint reads as a 404 or a 405 from the API.
    """
    request = response.request
    assert response.status_code == status, (
        f"{request.method} {request.url.path} returned {response.status_code}, expected {status}: {response.text}"
    )
    if status == 204:
        assert response.content == b"", f"{request.method} {request.url.path} returned a body with 204"
        return None
    body = response.json()
    if code is not None:
        assert body.get("code") == code, f"{request.method} {request.url.path} returned code {body.get('code')!r}"
        assert response.headers["content-type"] == "application/problem+json"
    return body


def expect_not_found(response: Response, resource_id: str) -> None:
    """Check that a response is the 404 the spec gives for an id the caller can't read."""
    body = expect(response, 404, "not_found")
    request = response.request
    assert body.get("id") == resource_id, (
        f"{request.method} {request.url.path} returned a 404 that names id {body.get('id')!r}, expected "
        f"{resource_id!r}. A 404 with no id comes from a path with no route: {response.text}"
    )
    assert isinstance(body.get("resource"), str), "the 404 has no `resource` member"


def assert_id(value: Any, prefix: str) -> None:
    """Check that a value is a stored resource's id with the given prefix."""
    assert isinstance(value, str) and re.fullmatch(f"{prefix}_{ID_PATTERN}", value), f"{value!r} isn't a {prefix} id"


def assert_timestamp(value: Any) -> None:
    """Check that a value is a UTC timestamp in RFC 3339 format with whole seconds."""
    assert isinstance(value, str) and TIMESTAMP.match(value), f"{value!r} isn't a timestamp like 2026-11-20T22:01:05Z"


def assert_page(body: Any, *, limit: int = 50, offset: int = 0) -> list[Any]:
    """Check a list response's envelope, and return its items."""
    assert set(body) == {"items", "total", "limit", "offset"}, f"a page has the keys {sorted(body)}"
    assert body["limit"] == limit and body["offset"] == offset
    assert isinstance(body["items"], list) and isinstance(body["total"], int)
    return body["items"]


def upload_adventure(client: TestClient, document: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Upload the test adventure, or another document, and return its info."""
    return expect(client.post("/v1/adventures", json=document or adventure_document()), 201)


def create_session(client: TestClient, adventure_id: str, **fields: Any) -> dict[str, Any]:
    """Create a session from the test party with the referee's seed, unless `fields` says otherwise.

    Pass `character_ids` to use stored characters instead of the test party. Pass `seed=None`
    to let the server draw the seed.
    """
    body: dict[str, Any] = {"adventure_id": adventure_id}
    if "character_ids" not in fields:
        body["party_document"] = party_document()
    body["seed"] = SEED
    body.update(fields)
    body = {key: value for key, value in body.items() if value is not None}
    return expect(client.post("/v1/sessions", json=body), 201)


def send(client: TestClient, session_id: str, command: Mapping[str, Any], **params: Any) -> dict[str, Any]:
    """Send one command and return the outcome, which must be a 200."""
    return expect(client.post(f"/v1/sessions/{session_id}/commands", json=dict(command), params=params), 200)


def view(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return a session's player view."""
    return expect(client.get(f"/v1/sessions/{session_id}/view"), 200)


def referee_view(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return a session's referee view."""
    return expect(client.get(f"/v1/sessions/{session_id}/referee-view"), 200)


def info(client: TestClient, session_id: str) -> dict[str, Any]:
    """Return a session's info."""
    return expect(client.get(f"/v1/sessions/{session_id}"), 200)


def create_character_via_api(client: TestClient, **fields: Any) -> dict[str, Any]:
    """Create a stored character, a lawful fighter unless `fields` says otherwise, and return it."""
    body: dict[str, Any] = {"name": "Cato", "class_id": "fighter", "alignment": "lawful"}
    body.update(fields)
    return expect(client.post("/v1/characters", json=body), 201)


def read_all_events(client: TestClient, session_id: str, *, limit: int, **params: Any) -> dict[str, Any]:
    """Read a session's event log in pages of `limit`, and return every event and message read.

    The function checks each page's `next_after` and `total` as it goes.
    """
    events: list[Any] = []
    messages: list[Any] = []
    after = 0
    total = None
    while True:
        page = expect(
            client.get(f"/v1/sessions/{session_id}/events", params={"after": after, "limit": limit, **params}), 200
        )
        assert set(page) == {"events", "messages", "next_after", "total"}, f"an events page has {sorted(page)}"
        assert len(page["events"]) == len(page["messages"]) <= limit
        assert page["next_after"] == after + len(page["events"])
        total = page["total"] if total is None else total
        assert page["total"] == total
        events.extend(page["events"])
        messages.extend(page["messages"])
        if not page["events"]:
            break
        after = page["next_after"]
    return {"events": events, "messages": messages, "next_after": after, "total": total}


def leaves(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    """Yield every value in a JSON tree with its path, the inner values included."""
    yield path, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from leaves(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, (*path, str(index)))
