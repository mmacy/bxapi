"""The rules reference: read-only views of osrlib's content.

The endpoints cover the classes, monsters, spells, equipment, magic items, languages, printed tables,
and commands that osrlib loads, and the license that covers them.
"""

from collections.abc import Callable
from importlib import resources
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response
from osrlib.core.abilities import AbilityTables
from osrlib.core.classes import ClassDefinition
from osrlib.core.items import MagicItemCategory, MagicItemTemplate
from osrlib.core.monsters import MonsterTemplate
from osrlib.core.spells import SpellTemplate
from osrlib.core.tables import CombatTables, EncounterTables
from osrlib.core.treasure import TreasureTables
from osrlib.crawl.commands import SessionMode
from osrlib.data import (
    Language,
    load_ability_tables,
    load_classes,
    load_combat_tables,
    load_encounter_tables,
    load_equipment,
    load_languages,
    load_magic_items,
    load_monsters,
    load_spells,
    load_treasure_tables,
)

from bxapi.commands import CommandKind
from bxapi.errors import not_found, problem_responses
from bxapi.models import ApiModel
from bxapi.pagination import Page, PageQuery, paginate
from bxapi.rules_catalog import (
    SPELL_LEVEL_MAX,
    SPELL_LEVEL_MIN,
    ClassSummary,
    CommandDetail,
    CommandSummary,
    EquipmentItem,
    EquipmentSummary,
    ItemType,
    MagicItemSummary,
    MonsterSummary,
    SpellSummary,
    TableSet,
    class_summaries,
    command_detail,
    command_summaries,
    equipment_summaries,
    lookup,
    magic_item_summaries,
    monster_summaries,
    spell_summaries,
)

__all__ = ["CACHE_CONTROL", "License", "router"]

CACHE_CONTROL = "private, max-age=3600"
"""The `Cache-Control` value of every successful rules response. The content changes only when the
server's osrlib does."""

_TABLES: dict[str, Callable[[], AbilityTables | CombatTables | EncounterTables | TreasureTables]] = {
    "abilities": load_ability_tables,
    "combat": load_combat_tables,
    "encounters": load_encounter_tables,
    "treasure": load_treasure_tables,
}

_Q = Annotated[
    str | None,
    Query(
        description="Case-insensitive substring filter matched against each entry's `id` and `name`. Omit to return "
        "all entries."
    ),
]


def _not_found(kind: str, listing: str) -> dict[int | str, dict[str, object]]:
    return problem_responses({404: [f"`not_found`: osrlib has no {kind} with this id. `{listing}` lists the ids."]})


def _set_cache_control(response: Response) -> None:
    response.headers["Cache-Control"] = CACHE_CONTROL


router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(_set_cache_control)])


class License(ApiModel):
    """Open Game License covering the rules catalogs."""

    name: str
    """License title, `"Open Game License 1.0a"`."""
    text: str
    """Full license text in Markdown, including its Section 15 copyright notice."""


@router.get("/classes", summary="List the character classes", response_description="A page of class summaries.")
def list_classes(page: PageQuery) -> Page[ClassSummary]:
    """Return a page of B/X character classes in osrlib's catalog order, with requirements, hit dice, and max levels.

    Pass a class's `id` as `class_id` to `POST /v1/characters` or `POST /v1/generate/character`, or to
    `GET /v1/rules/classes/{class_id}` for the full progression table. To list only classes that a rolled set of
    ability scores qualifies for, read `eligible_class_ids` from `POST /v1/ability-rolls`.
    """
    return paginate(class_summaries(), page)


@router.get(
    "/classes/{class_id}",
    summary="Get a character class",
    response_description="osrlib's `ClassDefinition`.",
    responses=_not_found("class", "GET /v1/rules/classes"),
)
def get_class(
    class_id: Annotated[
        str, Path(description="Class `id` from `GET /v1/rules/classes`, such as `fighter` or `magic_user`.")
    ],
) -> ClassDefinition:
    """Return osrlib's `ClassDefinition`: requirements, armour and weapon rules, abilities, and level progression.

    `requirements` maps each constrained ability score to its minimum value, and `progression` contains one row per
    level with required XP, hit dice, `thac0`, `saves`, and spell slots. Pass `class_id` to `GET /v1/rules/spells` to
    list the spells this class can cast, or to `POST /v1/characters` to create a character of this class.
    """
    return lookup(load_classes().get, "class", class_id)


@router.get("/monsters", summary="List the monsters", response_description="A page of monster summaries.")
def list_monsters(page: PageQuery, q: _Q = None) -> Page[MonsterSummary]:
    """Return a page of monster summaries in osrlib's catalog order, with hit dice, armour class, XP, and alignment.

    Pass `q` to search by template `id` or `name` (233 templates in total, so pass `limit=200` and page with `offset`
    to read the full catalog). Pass an entry's `id` to `GET /v1/rules/monsters/{monster_id}` for attacks, saves,
    morale, and special abilities, or to `POST /v1/generate/monsters` to roll hit points.
    """
    return paginate(monster_summaries(q), page)


@router.get(
    "/monsters/{monster_id}",
    summary="Get a monster",
    response_description="osrlib's `MonsterTemplate`.",
    responses=_not_found("monster", "GET /v1/rules/monsters"),
)
def get_monster(
    monster_id: Annotated[
        str, Path(description="Monster template `id` from `GET /v1/rules/monsters`, such as `goblin`.")
    ],
) -> MonsterTemplate:
    """Return osrlib's `MonsterTemplate` stat block: attacks, saves, morale, movement, treasure, and abilities.

    Use `display_name` and `display_name_plural` when naming the creature in prose (for example, `"giant centipede"`
    for the index name `"Centipede, Giant"`). `family_id` groups variants of the same creature and matches the
    `family_id` reported on encounter groups in `GET /v1/sessions/{session_id}/view`. This endpoint requires a
    template `id` rather than a `family_id` (for example, `hellhound_3` through `hellhound_7`, whereas `hellhound`
    returns 404 `not_found`).

    Pass `monster_id` to `POST /v1/generate/monsters` to spawn instances with rolled hit points, or pass each letter
    in `treasure` as `treasure_type` to `POST /v1/generate/treasure`.
    """
    return lookup(load_monsters().get, "monster", monster_id)


@router.get(
    "/spells",
    summary="List the spells",
    response_description="A page of spell summaries.",
    responses=_not_found("class", "GET /v1/rules/classes"),
)
def list_spells(
    page: PageQuery,
    q: _Q = None,
    class_id: Annotated[
        str | None,
        Query(
            description="Class `id` from `GET /v1/rules/classes`, such as `cleric` or `magic_user`, to return only "
            "spells on that class's spell list. Returns an empty `items` list for non-casting classes such as "
            "`fighter`, and 404 `not_found` for an unknown `class_id`."
        ),
    ] = None,
    level: Annotated[
        int | None,
        Query(
            ge=SPELL_LEVEL_MIN,
            le=SPELL_LEVEL_MAX,
            description="Spell level from 1 to 6. Omit to return spells of all levels.",
        ),
    ] = None,
) -> Page[SpellSummary]:
    """Return a page of spell summaries in osrlib's catalog order, filterable by search text, class, and spell level.

    Clerics cast from the `cleric` spell list, whereas magic-users and elves cast from `magic_user`. Filter with
    `class_id=magic_user&level=1` to pick the starting spell for a new magic-user or elf, then pass the chosen `id`
    in `starting_spell_ids` to `POST /v1/characters`. Read `GET /v1/rules/spells/{spell_id}` for full targeting,
    saving throw, and effect details.
    """
    return paginate(spell_summaries(q, class_id, level), page)


@router.get(
    "/spells/{spell_id}",
    summary="Get a spell",
    response_description="osrlib's `SpellTemplate`.",
    responses=_not_found("spell", "GET /v1/rules/spells"),
)
def get_spell(
    spell_id: Annotated[str, Path(description="Spell `id` from `GET /v1/rules/spells`, such as `magic_missile`.")],
) -> SpellTemplate:
    """Return osrlib's `SpellTemplate`: range, duration, targeting, saving throw, and mechanical effect.

    Pass `spell_id` in `starting_spell_ids` on `POST /v1/characters` for a first-level arcane caster, or in the
    `learn_spell`, `prepare_spells`, and `cast_spell` commands of `POST /v1/sessions/{session_id}/commands`.
    """
    return lookup(load_spells().get, "spell", spell_id)


@router.get("/equipment", summary="List the equipment", response_description="A page of equipment summaries.")
def list_equipment(
    page: PageQuery,
    q: _Q = None,
    item_type: Annotated[
        ItemType | None,
        Query(
            description="Equipment category filter: `weapon`, `armour` (including shields), `gear` (adventuring "
            "gear), or `ammunition`. Omit to return all four categories."
        ),
    ] = None,
) -> Page[EquipmentSummary]:
    """Return a page of mundane equipment items ordered by category: weapons, armour, gear, then ammunition.

    Filter by `q` or `item_type`, and read `GET /v1/rules/equipment/{item_id}` for weight, damage, armour class, or
    bundle size. Pass `id` as `item_id` in `purchases` on `POST /v1/characters` or in `item_ids` on a session's
    `purchase_equipment` command. `cost_gp` is the price in gold pieces for one purchase lot (one weapon or suit of
    armour, or a bundle of gear or ammunition such as six torches).
    """
    return paginate(equipment_summaries(q, item_type), page)


@router.get(
    "/equipment/{item_id}",
    summary="Get an equipment item",
    response_description="osrlib's template for the item.",
    responses=_not_found("equipment item", "GET /v1/rules/equipment"),
)
def get_equipment_item(
    item_id: Annotated[str, Path(description="Equipment `id` from `GET /v1/rules/equipment`, such as `sword`.")],
) -> EquipmentItem:
    """Return osrlib's template for one weapon, armour, adventuring gear, or ammunition item.

    Narrow the union on `item_type` (`weapon`, `armour`, `gear`, or `ammunition`). Pass `item_id` in `purchases` and
    `equip_item_ids` on `POST /v1/characters`, or in a session's `purchase_equipment`, `equip_item`, and `grant_item`
    commands.
    """
    return EquipmentItem(lookup(load_equipment().get, "equipment item", item_id))


@router.get("/magic-items", summary="List the magic items", response_description="A page of magic item summaries.")
def list_magic_items(
    page: PageQuery,
    q: _Q = None,
    category: Annotated[
        MagicItemCategory | None,
        Query(
            description="Magic item category filter: `armour`, `misc`, `potion`, `ring`, `rod`, `staff`, `wand`, "
            "`scroll`, `sword`, or `weapon`. Omit to return all categories."
        ),
    ] = None,
) -> Page[MagicItemSummary]:
    """Return a page of magic item summaries in osrlib's catalog order, filterable by search text and category.

    Pass an entry's `id` to `GET /v1/rules/magic-items/{item_id}` for full rules and modifiers, or to a session's
    referee `grant_item` command on `POST /v1/sessions/{session_id}/commands`.
    """
    return paginate(magic_item_summaries(q, category), page)


@router.get(
    "/magic-items/{item_id}",
    summary="Get a magic item",
    response_description="osrlib's `MagicItemTemplate`.",
    responses=_not_found("magic item", "GET /v1/rules/magic-items"),
)
def get_magic_item(
    item_id: Annotated[
        str, Path(description="Magic item `id` from `GET /v1/rules/magic-items`, such as `potion_of_healing`.")
    ],
) -> MagicItemTemplate:
    """Return osrlib's `MagicItemTemplate` for one magic item, including bonuses, charges, usable classes, and curses.

    Both `player` and `referee` keys receive the full template, including `cursed` and unidentified details. During a
    session, show players only what `GET /v1/sessions/{session_id}/view` reveals about items in their inventory.
    """
    return lookup(load_magic_items().get, "magic item", item_id)


@router.get("/languages", summary="List the languages", response_description="A page of languages.")
def list_languages(page: PageQuery) -> Page[Language]:
    """Return the spoken languages in osrlib's catalog, sorted alphabetically by `id`.

    osrlib grants alignment tongues (`alignment_lawful`, `alignment_neutral`, `alignment_chaotic`) automatically from
    alignment, so they do not appear in this list. When creating a character with high Intelligence on
    `POST /v1/characters` or `POST /v1/generate/character`, pass `id` values whose `choosable` field is `true` in
    `extra_languages`.
    """
    return paginate(load_languages().languages, page)


@router.get("/tables", summary="List the printed tables", response_description="A page of table names.")
def list_tables(page: PageQuery) -> Page[str]:
    """Return the four printed B/X table set names: `abilities`, `combat`, `encounters`, and `treasure`.

    Pass any returned name as `name` to `GET /v1/rules/tables/{name}` to read that table set.
    """
    return paginate(list(_TABLES), page)


@router.get(
    "/tables/{name}",
    summary="Get a set of printed tables",
    response_description="osrlib's model for the set of tables.",
    responses=problem_responses(
        {404: ["`not_found`: No set of tables has this name. Send `abilities`, `combat`, `encounters`, or `treasure`."]}
    ),
)
def get_table(
    name: Annotated[
        str,
        Path(
            description="Table set name from `GET /v1/rules/tables`: `abilities`, `combat`, `encounters`, or "
            "`treasure`."
        ),
    ],
) -> TableSet:
    """Return osrlib's model for one printed B/X table set.

    - `abilities`: `AbilityTables` (modifiers, bonus languages, and literacy by ability score).
    - `combat`: `CombatTables` (attack matrices, saving throws, XP awards, undead turning, morale, and reactions).
    - `encounters`: `EncounterTables` (wandering monster tables by dungeon level and NPC party tables).
    - `treasure`: `TreasureTables` (treasure types `A` through `V`, gems, jewellery, magic item probabilities, and
      room stocking).

    In a session, osrlib consults these tables automatically. To roll treasure or room stocking outside a session,
    call `POST /v1/generate/treasure` or `POST /v1/generate/room`.
    """
    loader = _TABLES.get(name)
    if loader is None:
        raise not_found("table", name)
    return TableSet(loader())


@router.get("/commands", summary="List the commands", response_description="A page of command summaries.")
def list_commands(
    page: PageQuery,
    kind: Annotated[
        CommandKind | None,
        Query(
            description="Filter by who may send the command: `play` (any caller) or `referee` (`referee` role only). "
            "Omit to return both."
        ),
    ] = None,
    mode: Annotated[
        SessionMode | None,
        Query(
            description="Filter to commands legal in this session mode (`town`, `exploring`, `encounter`, `battle`, "
            "`game_over`, or `victory`). Omit to return commands for all modes."
        ),
    ] = None,
) -> Page[CommandSummary]:
    """Return session commands in osrlib's catalog order, with `kind`, `allowed_modes`, and a one-line summary.

    osrlib defines more than 50 commands, so pass `limit=200` to fetch the entire catalog in one request. Pass any
    `command_type` to `GET /v1/rules/commands/{command_type}` for its request body JSON Schema.

    To list only the commands legal right now in a specific running session for your API key's role, call
    `GET /v1/sessions/{session_id}/commands` instead.
    """
    return paginate(command_summaries(kind, mode), page)


@router.get(
    "/commands/{command_type}",
    summary="Get a command",
    response_description="The command's summary and the JSON Schema of its body.",
    responses=problem_responses(
        {404: ["`not_found`: osrlib has no command of this type. `GET /v1/rules/commands` lists the types."]}
    ),
)
def get_command(
    command_type: Annotated[
        str, Path(description="`command_type` identifier from `GET /v1/rules/commands`, such as `move_party`.")
    ],
) -> CommandDetail:
    """Return one command's summary and the JSON Schema for `POST /v1/sessions/{session_id}/commands`.

    Use `json_schema` to build dynamic command forms or validate payloads client-side. Always send `command_type` in
    the request body even though osrlib's raw Pydantic schema lists `command_type` with a default rather than inside
    `required`. Only a caller with the `referee` role may send commands whose `kind` is `referee` or set a non-`null`
    `source` field.
    """
    return command_detail(command_type)


@router.get(
    "/license", summary="Get the license of the rules content", response_description="The license's name and text."
)
def get_license() -> License:
    """Return the Open Game License 1.0a text and Section 15 copyright notice that govern osrlib's rules content."""
    text = resources.files("osrlib.data").joinpath("LICENSE-OGL.md").read_text(encoding="utf-8")
    return License(name="Open Game License 1.0a", text=text)
