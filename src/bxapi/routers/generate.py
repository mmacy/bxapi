"""Treasure, NPC parties, rooms, and monsters rolled without a game."""

from typing import Literal

from fastapi import APIRouter
from osrlib.core import monsters as osr_monsters
from osrlib.core import npc as osr_npc
from osrlib.core import treasure as osr_treasure
from osrlib.core.items import GeneratedTreasure
from osrlib.core.rng import RngStreams
from osrlib.crawl import stocking as osr_stocking
from osrlib.data import load_monsters, load_treasure_tables
from pydantic import Field

from bxapi.errors import VALIDATION_ERROR, invalid, problem_responses
from bxapi.idempotency import IdempotentRoute
from bxapi.models import ApiModel, RequestModel
from bxapi.seeds import Seed, seed_or_draw

__all__ = [
    "MonstersRequest",
    "MonstersResponse",
    "NpcPartyRequest",
    "NpcPartyResponse",
    "RoomRequest",
    "RoomResponse",
    "TreasureRequest",
    "TreasureResponse",
    "router",
]

router = APIRouter(prefix="/generate", tags=["generate"], route_class=IdempotentRoute)

STOCKING_STREAM = "stocking"
"""The key of the stream the server rolls rooms from. osrlib's `StreamName` has no member for it. The example in the
documentation of `stock_area` draws from a stream with this key. The same seed gives a different stream for a
different key, so changing the key changes every room."""


class TreasureRequest(RequestModel):
    """Treasure type, magic item tier, and optional seed for rolling a treasure hoard."""

    treasure_type: str = Field(min_length=1, max_length=8, examples=["A"])
    """Uppercase B/X treasure type letter from `"A"` to `"V"`, as listed in `GET /v1/rules/tables/treasure`."""
    tier: Literal["basic", "expert"]
    """Magic item table column: `"basic"` for parties of levels 1 to 3, or `"expert"` for parties of level 4 and
    higher."""
    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1). Pass the `seed` from an earlier response with the same body to reproduce the
    hoard, or omit it to let the server draw a random seed."""


class TreasureResponse(ApiModel):
    """Rolled treasure hoard and the seed that produced it."""

    seed: Seed
    treasure: GeneratedTreasure
    """Coins, gems, jewellery, and magic items rolled by osrlib."""


class NpcPartyRequest(RequestModel):
    """Party tier, member count, and optional seed for generating an NPC adventuring party."""

    kind: Literal["basic", "expert"]
    """Party table: `"basic"` for levels 1 to 3, or `"expert"` for higher-level adventurers."""
    count: int = Field(ge=1, le=100)
    """Number of NPC adventurers to generate, from 1 to 100."""
    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1). Pass the `seed` from an earlier response with the same body to reproduce the
    party, or omit it to let the server draw a random seed."""


class NpcPartyResponse(ApiModel):
    """Generated NPC adventuring party and the seed that produced it."""

    seed: Seed
    party: osr_npc.NpcParty
    """Generated adventurers (`members`), their shared `alignment`, and party `treasure`."""


class RoomRequest(RequestModel):
    """Dungeon level and optional seed for stocking one dungeon room."""

    level_number: int = Field(ge=1)
    """Dungeon depth counting from 1 at the top level, used to select the wandering encounter table."""
    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1). Pass the `seed` from an earlier response with the same body to reproduce the
    stocked room, or omit it to let the server draw a random seed."""


class RoomResponse(ApiModel):
    """Stocked dungeon room result and the seed that produced it."""

    seed: Seed
    room: osr_stocking.StockedArea
    """Rolled room `contents` (`empty`, `monster`, `special`, or `trap`), `treasure_present`, and any `encounter`,
    `npc_party`, or unguarded `treasure`."""


class MonstersRequest(RequestModel):
    """Monster template id, count, and optional seed for spawning monsters with rolled hit points."""

    monster_id: str = Field(min_length=1, max_length=128, examples=["goblin"])
    """Monster template `id` from `GET /v1/rules/monsters`, such as `"goblin"`."""
    count: int = Field(default=1, ge=1, le=100)
    """Number of monster instances to spawn, from 1 to 100. Defaults to 1."""
    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1). Pass the `seed` from an earlier response with the same body to reproduce the
    hit points, or omit it to let the server draw a random seed."""


class MonstersResponse(ApiModel):
    """Spawned monster instances and the seed that produced them."""

    seed: Seed
    monsters: list[osr_monsters.MonsterInstance]
    """Monster instances in spawn order (`monster-0001` onward), each with `template`, `max_hp`, and `current_hp`."""


@router.post(
    "/treasure",
    summary="Roll treasure for a treasure type",
    response_description="Rolled treasure hoard and the seed used.",
    responses=problem_responses(
        {
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib has no treasure type with that letter. Send an uppercase letter from "
                "`A` to `V`.",
            ]
        }
    ),
)
def generate_treasure(body: TreasureRequest) -> TreasureResponse:
    """Roll coins, gems, jewellery, and magic items for one B/X treasure type without storing anything.

    osrlib's `generate_treasure` rolls each entry in `GET /v1/rules/tables/treasure` in table order. `coins` reports
    each denomination (`pp`, `gp`, `ep`, `sp`, `cp`), `valuables` lists rolled gems and jewellery with `value_gp`,
    and each entry in `magic_items` includes `template_id` for lookup via `GET /v1/rules/magic-items/{item_id}`. A
    monster's treasure letters appear on `GET /v1/rules/monsters/{monster_id}` under `treasure`.

    Any caller may pass `seed` to repeat a roll, or omit it to get a new random seed. In an active session, osrlib
    rolls area and lair treasure automatically during play.
    """
    seed = seed_or_draw(body.seed)
    try:
        load_treasure_tables().treasure_type(body.treasure_type)
    except ValueError as error:
        raise invalid("content_invalid", str(error)) from error
    streams = RngStreams(master_seed=seed)
    treasure = osr_treasure.generate_treasure(
        body.treasure_type,
        tier=body.tier,
        stream=streams.get(osr_treasure.TREASURE_STREAM),
        allocator=osr_monsters.IdAllocator(),
    )
    return TreasureResponse(seed=seed, treasure=treasure)


@router.post(
    "/npc-party",
    summary="Generate a party of NPC adventurers",
    response_description="Generated NPC party and the seed used.",
)
def generate_npc_party(body: NpcPartyRequest) -> NpcPartyResponse:
    """Roll a party of NPC adventurers with classes, levels, scores, equipment, spells, and treasure.

    osrlib's `generate_npc_party` rolls each adventurer as an osrlib `Character` in `party.members`, matching the
    `character` structure on `GET /v1/characters/{character_id}`, and rolls shared alignment and treasure. Any
    caller may pass `seed` to reproduce the party, or omit it to draw a new seed.

    The server stores nothing. Use the result for rival parties or hirelings in external tools. To place an NPC
    party directly into a running session, a referee sends `spawn_npc_party` to
    `POST /v1/sessions/{session_id}/commands`.
    """
    seed = seed_or_draw(body.seed)
    streams = RngStreams(master_seed=seed)
    party = osr_npc.generate_npc_party(
        body.kind,
        count=body.count,
        npc_stream=streams.get(osr_npc.NPC_PARTY_STREAM),
        treasure_stream=streams.get(osr_treasure.TREASURE_STREAM),
        allocator=osr_monsters.IdAllocator(),
    )
    return NpcPartyResponse(seed=seed, party=party)


@router.post(
    "/room",
    summary="Stock a dungeon room",
    response_description="Stocked room contents and the seed used.",
)
def generate_room(body: RoomRequest) -> RoomResponse:
    """Roll the contents and treasure of one dungeon room using osrlib's `stock_area` and the B/X stocking tables.

    Rolls `room.contents` (`empty`, `monster`, `special`, or `trap`) and `room.treasure_present`. For a `monster`
    room, rolls on the level's wandering encounter table (`GET /v1/rules/tables/encounters`) and populates
    `room.encounter` with monster `template_id` values (for `GET /v1/rules/monsters/{monster_id}`) or
    `room.npc_party`. For an unguarded treasure room, populates `room.treasure`.

    The server stores nothing. Use the result when authoring an adventure document (`POST /v1/adventures`), calling
    once per room with a new seed (or passing `seed` to reproduce a room).
    """
    seed = seed_or_draw(body.seed)
    streams = RngStreams(master_seed=seed)
    room = osr_stocking.stock_area(body.level_number, catalog=load_monsters(), stream=streams.get(STOCKING_STREAM))
    return RoomResponse(seed=seed, room=room)


@router.post(
    "/monsters",
    summary="Spawn monsters with rolled hit points",
    response_description="Spawned monster instances with rolled hit points and the seed used.",
    responses=problem_responses(
        {
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib has no monster template with `monster_id`. Look up valid ids with "
                "`GET /v1/rules/monsters`.",
            ]
        }
    ),
)
def generate_monsters(body: MonstersRequest) -> MonstersResponse:
    """Spawn one or more monster instances from a catalog template, each with independently rolled hit points.

    osrlib's `spawn_monster` rolls hit points for each instance in order, assigning ids `monster-0001`,
    `monster-0002`, and so on. Each entry includes the full `MonsterTemplate` (matching
    `GET /v1/rules/monsters/{monster_id}`) in `template` alongside `max_hp` and `current_hp`. Any caller may pass
    `seed` to reproduce the rolls, or omit it to draw a new seed.

    The server stores nothing. To spawn monsters into a running session's encounter, a referee sends the
    `spawn_monsters` command to `POST /v1/sessions/{session_id}/commands` instead.
    """
    seed = seed_or_draw(body.seed)
    try:
        template = load_monsters().get(body.monster_id)
    except ValueError as error:
        raise invalid("content_invalid", str(error)) from error
    stream = RngStreams(master_seed=seed).get(osr_monsters.MONSTER_SPAWN_STREAM)
    allocator = osr_monsters.IdAllocator()
    monsters = [
        osr_monsters.spawn_monster(template, id=allocator.allocate("monster"), stream=stream) for _ in range(body.count)
    ]
    return MonstersResponse(seed=seed, monsters=monsters)
