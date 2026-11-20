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

from bxapi.errors import invalid
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

router = APIRouter(prefix="/generate", tags=["generate"])

STOCKING_STREAM = "stocking"
"""The key of the stream that rolls rooms. osrlib's `StreamName` has no member for it. The example in the
documentation of `stock_area` draws from a stream with this key. The key is part of what makes a seed repeatable, so
changing it changes every room."""


class TreasureRequest(RequestModel):
    """The treasure type to roll, the tier of magic items, and an optional seed."""

    treasure_type: str = Field(min_length=1, max_length=8, examples=["A"])
    """The letter of a treasure type, from `A` to `V`. Letters are upper case."""
    tier: Literal["basic", "expert"]
    """The column of the magic item table to roll on.

    Use `basic` for a party whose highest level is 1 to 3, and `expert` from level 4.
    """
    seed: Seed | None = None


class TreasureResponse(ApiModel):
    """Generated treasure, and the seed to send to get the same treasure again."""

    seed: Seed
    treasure: GeneratedTreasure
    """The coins, gems, jewellery, and magic items that osrlib rolled."""


class NpcPartyRequest(RequestModel):
    """The kind of NPC party, its size, and an optional seed."""

    kind: Literal["basic", "expert"]
    """`basic` for a party of levels 1 to 3, or `expert` for a higher-level party."""
    count: int = Field(ge=1, le=100)
    """How many adventurers the party has."""
    seed: Seed | None = None


class NpcPartyResponse(ApiModel):
    """A generated NPC party, and the seed to send to get the same party again."""

    seed: Seed
    party: osr_npc.NpcParty
    """The adventurers, the alignment they share, and the treasure they carry."""


class RoomRequest(RequestModel):
    """The dungeon level to stock a room for, and an optional seed."""

    level_number: int = Field(ge=1)
    """The dungeon level the room is on, counting from 1 at the top."""
    seed: Seed | None = None


class RoomResponse(ApiModel):
    """A stocked room, and the seed to send to get the same room again."""

    seed: Seed
    room: osr_stocking.StockedArea
    """What the room contains: its kind, whether it has treasure, and any encounter or NPC party."""


class MonstersRequest(RequestModel):
    """The monster to spawn, how many, and an optional seed."""

    monster_id: str = Field(min_length=1, max_length=128, examples=["goblin"])
    """The id of a monster, from `GET /v1/rules/monsters`, like `goblin`."""
    count: int = Field(default=1, ge=1, le=100)
    """How many monsters to spawn."""
    seed: Seed | None = None


class MonstersResponse(ApiModel):
    """Spawned monsters, and the seed to send to get the same monsters again."""

    seed: Seed
    monsters: list[osr_monsters.MonsterInstance]
    """The monsters, in spawn order. Each has its template, an id, and separately rolled hit points."""


@router.post("/treasure", summary="Roll treasure for a treasure type")
def generate_treasure(body: TreasureRequest) -> TreasureResponse:
    """Roll the contents of one treasure type: coins, gems, jewellery, and magic items.

    osrlib's `generate_treasure` function does the rolling, in the order the entries appear in the treasure table.
    Send the same body with the returned seed to get the same treasure again, or leave `seed` out to get a new random
    seed.

    A treasure type that osrlib doesn't have gets 422 `content_invalid`.
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


@router.post("/npc-party", summary="Generate a party of NPC adventurers")
def generate_npc_party(body: NpcPartyRequest) -> NpcPartyResponse:
    """Generate a party of NPC adventurers with classes, levels, ability scores, gear, spells, and treasure.

    osrlib's `generate_npc_party` function does the rolling. Send the same body with the returned seed to get the same
    party again, or leave `seed` out to get a new random seed.
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


@router.post("/room", summary="Stock a dungeon room")
def generate_room(body: RoomRequest) -> RoomResponse:
    """Roll what a dungeon room contains, using the B/X stocking tables.

    osrlib's `stock_area` function does the rolling. It rolls whether the room is empty, a trap, special, or has a
    monster. It then rolls whether the room has treasure. For a monster room, it also rolls on the encounter table for
    the level.

    The result is content for an adventure author to place, not a game in progress. Send the same body with the
    returned seed to get the same room again, or leave `seed` out to get a new random seed.
    """
    seed = seed_or_draw(body.seed)
    streams = RngStreams(master_seed=seed)
    room = osr_stocking.stock_area(body.level_number, catalog=load_monsters(), stream=streams.get(STOCKING_STREAM))
    return RoomResponse(seed=seed, room=room)


@router.post("/monsters", summary="Spawn monsters with rolled hit points")
def generate_monsters(body: MonstersRequest) -> MonstersResponse:
    """Spawn one or more monsters from a template, each with separately rolled hit points.

    osrlib's `spawn_monster` function does the rolling, once for each monster, in spawn order. The monsters get the
    ids `monster-0001`, `monster-0002`, and so on. Send the same body with the returned seed to get the same hit
    points again, or leave `seed` out to get a new random seed.

    A monster id that osrlib doesn't have gets 422 `content_invalid`.
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
