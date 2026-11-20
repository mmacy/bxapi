from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from osrlib.core.monsters import MONSTER_SPAWN_STREAM, IdAllocator, spawn_monster
from osrlib.core.npc import NPC_PARTY_STREAM, generate_npc_party
from osrlib.core.rng import RngStreams
from osrlib.core.treasure import TREASURE_STREAM, generate_treasure
from osrlib.crawl.stocking import stock_area
from osrlib.data import load_monsters, load_treasure_tables

from bxapi.seeds import MAX_SEED

TREASURE = "/v1/generate/treasure"
NPC_PARTY = "/v1/generate/npc-party"
ROOM = "/v1/generate/room"
MONSTERS = "/v1/generate/monsters"

TREASURE_BODY = {"treasure_type": "A", "tier": "expert"}
NPC_PARTY_BODY = {"kind": "expert", "count": 4}
ROOM_BODY = {"level_number": 3}
MONSTERS_BODY = {"monster_id": "goblin", "count": 3}

OPERATION_IDS = {
    TREASURE: "generate_treasure",
    NPC_PARTY: "generate_npc_party",
    ROOM: "generate_room",
    MONSTERS: "generate_monsters",
}


def has_description(prop: dict[str, Any]) -> bool:
    """Whether a schema property, or one branch of its `anyOf`, has a description."""
    return bool(prop.get("description") or any(branch.get("description") for branch in prop.get("anyOf", [])))


ENDPOINTS = [
    (TREASURE, TREASURE_BODY),
    (NPC_PARTY, NPC_PARTY_BODY),
    (ROOM, ROOM_BODY),
    (MONSTERS, MONSTERS_BODY),
]


def roll_treasure(tier: Literal["basic", "expert"], seed: int) -> dict[str, Any]:
    """Call osrlib the way the endpoint should, for treasure type A."""
    treasure = generate_treasure(
        "A", tier=tier, stream=RngStreams(master_seed=seed).get(TREASURE_STREAM), allocator=IdAllocator()
    )
    return treasure.model_dump(mode="json")


@pytest.mark.parametrize("tier", ["basic", "expert"])
def test_treasure_matches_osrlib(client: TestClient, tier: Literal["basic", "expert"]) -> None:
    for seed in range(40):
        response = client.post(TREASURE, json={"treasure_type": "A", "tier": tier, "seed": seed})
        assert response.status_code == 200
        assert response.json() == {"seed": seed, "treasure": roll_treasure(tier, seed)}


def test_the_seeds_that_treasure_is_checked_with_tell_the_tiers_apart() -> None:
    assert any(roll_treasure("basic", seed) != roll_treasure("expert", seed) for seed in range(40))


@pytest.mark.parametrize("kind", ["basic", "expert"])
def test_npc_party_matches_osrlib(client: TestClient, kind: Literal["basic", "expert"]) -> None:
    response = client.post(NPC_PARTY, json={"kind": kind, "count": 4, "seed": 12})
    assert response.status_code == 200
    streams = RngStreams(master_seed=12)
    expected = generate_npc_party(
        kind,
        count=4,
        npc_stream=streams.get(NPC_PARTY_STREAM),
        treasure_stream=streams.get(TREASURE_STREAM),
        allocator=IdAllocator(),
    )
    assert response.json() == {"seed": 12, "party": expected.model_dump(mode="json")}
    assert len(response.json()["party"]["members"]) == 4


def test_room_matches_osrlib(client: TestClient) -> None:
    for seed in range(20):
        response = client.post(ROOM, json=ROOM_BODY | {"seed": seed})
        assert response.status_code == 200
        expected = stock_area(3, catalog=load_monsters(), stream=RngStreams(master_seed=seed).get("stocking"))
        assert response.json() == {"seed": seed, "room": expected.model_dump(mode="json")}


def test_rooms_cover_every_kind_osrlib_rolls(client: TestClient) -> None:
    seeds = range(60)
    expected = {
        stock_area(3, catalog=load_monsters(), stream=RngStreams(master_seed=seed).get("stocking")).contents
        for seed in seeds
    }
    assert expected == {"empty", "monster", "special", "trap"}
    assert {client.post(ROOM, json=ROOM_BODY | {"seed": seed}).json()["room"]["contents"] for seed in seeds} == expected


def test_monsters_match_osrlib(client: TestClient) -> None:
    response = client.post(MONSTERS, json=MONSTERS_BODY | {"seed": 13})
    assert response.status_code == 200
    stream = RngStreams(master_seed=13).get(MONSTER_SPAWN_STREAM)
    template = load_monsters().get("goblin")
    allocator = IdAllocator()
    expected = [spawn_monster(template, id=allocator.allocate("monster"), stream=stream) for _ in range(3)]
    assert response.json() == {"seed": 13, "monsters": [monster.model_dump(mode="json") for monster in expected]}


def test_monster_count_defaults_to_one(client: TestClient) -> None:
    response = client.post(MONSTERS, json={"monster_id": "troll", "seed": 1})
    assert [monster["id"] for monster in response.json()["monsters"]] == ["monster-0001"]


@pytest.mark.parametrize(("url", "body"), ENDPOINTS)
def test_same_seed_gives_the_same_body(client: TestClient, url: str, body: dict[str, Any]) -> None:
    first = client.post(url, json=body | {"seed": 2026})
    second = client.post(url, json=body | {"seed": 2026})
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["seed"] == 2026


@pytest.mark.parametrize(("url", "body"), ENDPOINTS)
def test_no_seed_draws_a_different_seed_each_time(client: TestClient, url: str, body: dict[str, Any]) -> None:
    first = client.post(url, json=body).json()
    second = client.post(url, json=body).json()
    assert 0 <= first["seed"] <= MAX_SEED
    assert first["seed"] != second["seed"]
    assert client.post(url, json=body | {"seed": first["seed"]}).json() == first


@pytest.mark.parametrize(("url", "body"), ENDPOINTS)
def test_a_player_can_generate_and_choose_a_seed(player: TestClient, url: str, body: dict[str, Any]) -> None:
    response = player.post(url, json=body | {"seed": 5})
    assert response.status_code == 200
    assert response.json()["seed"] == 5


@pytest.mark.parametrize(("url", "body"), ENDPOINTS)
def test_no_key_is_unauthorized(player: TestClient, url: str, body: dict[str, Any]) -> None:
    assert player.post(url, json=body, headers={"Authorization": ""}).status_code == 401


def test_an_unknown_treasure_type_is_content_invalid(client: TestClient) -> None:
    for treasure_type in ("Z", "a", "AA", "1"):
        response = client.post(TREASURE, json={"treasure_type": treasure_type, "tier": "basic"})
        assert response.status_code == 422, treasure_type
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "content_invalid"
        assert treasure_type in response.json()["detail"]


def test_an_unknown_monster_id_is_content_invalid(client: TestClient) -> None:
    response = client.post(MONSTERS, json={"monster_id": "no-such-monster"})
    assert response.status_code == 422
    assert response.json()["code"] == "content_invalid"
    assert "no-such-monster" in response.json()["detail"]


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (TREASURE, {"treasure_type": "", "tier": "basic"}),
        (TREASURE, {"treasure_type": "A", "tier": "heroic"}),
        (TREASURE, {"treasure_type": "A"}),
        (NPC_PARTY, {"kind": "epic", "count": 2}),
        (NPC_PARTY, {"kind": "basic", "count": 0}),
        (NPC_PARTY, {"kind": "basic", "count": 101}),
        (NPC_PARTY, {"kind": "basic"}),
        (ROOM, {"level_number": 0}),
        (ROOM, {"level_number": -1}),
        (ROOM, {}),
        (TREASURE, {"treasure_type": "ABCDEFGHI", "tier": "basic"}),
        (MONSTERS, {"monster_id": ""}),
        (MONSTERS, {"monster_id": "x" * 129}),
        (MONSTERS, {"monster_id": "goblin", "count": 0}),
        (MONSTERS, {"monster_id": "goblin", "count": 101}),
        (MONSTERS, {}),
        (TREASURE, TREASURE_BODY | {"seed": -1}),
        (ROOM, ROOM_BODY | {"seed": MAX_SEED + 1}),
    ],
)
def test_a_body_that_breaks_the_schema_is_a_validation_error(
    client: TestClient, url: str, body: dict[str, Any]
) -> None:
    response = client.post(url, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(("url", "body"), ENDPOINTS)
def test_an_unknown_field_is_a_validation_error(client: TestClient, url: str, body: dict[str, Any]) -> None:
    response = client.post(url, json=body | {"Count": 5})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["errors"][0]["loc"] == ["body", "Count"]


def test_the_limits_of_count_are_accepted(client: TestClient) -> None:
    assert len(client.post(NPC_PARTY, json={"kind": "basic", "count": 100}).json()["party"]["members"]) == 100
    assert len(client.post(MONSTERS, json={"monster_id": "goblin", "count": 100}).json()["monsters"]) == 100
    assert client.post(ROOM, json={"level_number": 1000}).status_code == 200


def test_every_treasure_type_osrlib_has_rolls(client: TestClient) -> None:
    letters = [table.letter for table in load_treasure_tables().treasure_types]
    assert letters
    for letter in letters:
        response = client.post(TREASURE, json={"treasure_type": letter, "tier": "basic", "seed": 3})
        assert response.status_code == 200, letter


def test_openapi_describes_each_endpoint_and_osrlib_type(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    payloads = {
        TREASURE: ("TreasureResponse", "treasure", "GeneratedTreasure"),
        NPC_PARTY: ("NpcPartyResponse", "party", "NpcParty"),
        ROOM: ("RoomResponse", "room", "StockedArea"),
        MONSTERS: ("MonstersResponse", "monsters", "MonsterInstance"),
    }
    for url, (response_model, field, osrlib_type) in payloads.items():
        operation = schema["paths"][url]["post"]
        assert operation["summary"]
        assert operation["operationId"] == OPERATION_IDS[url]
        assert "osrlib" in operation["description"]
        assert osrlib_type in schemas
        assert osrlib_type in str(schemas[response_model]["properties"][field])
    for model in (
        "TreasureRequest",
        "TreasureResponse",
        "NpcPartyRequest",
        "NpcPartyResponse",
        "RoomRequest",
        "RoomResponse",
        "MonstersRequest",
        "MonstersResponse",
    ):
        properties = schemas[model]["properties"]
        assert all(has_description(prop) for prop in properties.values()), model
