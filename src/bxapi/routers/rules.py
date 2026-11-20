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

_Q = Annotated[str | None, Query(description="Keep entries whose id or name contains this text, ignoring case.")]


def _set_cache_control(response: Response) -> None:
    response.headers["Cache-Control"] = CACHE_CONTROL


router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(_set_cache_control)])


class License(ApiModel):
    """The license that covers the rules content."""

    name: str
    """The name of the license."""
    text: str
    """The license text in Markdown, with its Section 15 notice."""


@router.get("/classes", summary="List the character classes")
def list_classes(page: PageQuery) -> Page[ClassSummary]:
    """List every character class in osrlib's order, with the facts a class picker shows.

    Read `/v1/rules/classes/{class_id}` for the full definition. Pass a class's `id` as `class_id` when you
    create a character.

    Returns:
        A page of class summaries.
    """
    return paginate(class_summaries(), page)


@router.get("/classes/{class_id}", summary="Get a character class", responses=problem_responses(404))
def get_class(
    class_id: Annotated[str, Path(description="A class `id` from the class list, like `fighter`.")],
) -> ClassDefinition:
    """Return osrlib's definition of one class: its requirements, XP tiers, abilities, and level progression."""
    return lookup(load_classes().get, "class", class_id)


@router.get("/monsters", summary="List the monsters")
def list_monsters(page: PageQuery, q: _Q = None) -> Page[MonsterSummary]:
    """List the monsters in osrlib's order, with hit dice, armour class, experience, and alignment.

    Pass `q` to search by id or name. Read `/v1/rules/monsters/{monster_id}` for the full template.

    Returns:
        A page of monster summaries.
    """
    return paginate(monster_summaries(q), page)


@router.get("/monsters/{monster_id}", summary="Get a monster", responses=problem_responses(404))
def get_monster(
    monster_id: Annotated[str, Path(description="A monster `id` from the monster list, like `goblin`.")],
) -> MonsterTemplate:
    """Return osrlib's template for one monster: attacks, saves, morale, treasure, and special abilities."""
    return lookup(load_monsters().get, "monster", monster_id)


@router.get("/spells", summary="List the spells", responses=problem_responses(404))
def list_spells(
    page: PageQuery,
    q: _Q = None,
    class_id: Annotated[
        str | None,
        Query(
            description="Keep the spells on this class's spell list. For a class that casts no spells, the page is "
            "empty. An unknown class id is a 404."
        ),
    ] = None,
    level: Annotated[
        int | None,
        Query(ge=SPELL_LEVEL_MIN, le=SPELL_LEVEL_MAX, description="Keep the spells of this spell level."),
    ] = None,
) -> Page[SpellSummary]:
    """List the spells in osrlib's order, with each spell's level, spell list, range, and duration.

    A class casts the spells on one spell list. The cleric uses `cleric`, and the magic-user and the elf use
    `magic_user`. Pass `class_id` to get the spells a class can cast, and `level` to keep one spell level.

    Returns:
        A page of spell summaries.
    """
    return paginate(spell_summaries(q, class_id, level), page)


@router.get("/spells/{spell_id}", summary="Get a spell", responses=problem_responses(404))
def get_spell(
    spell_id: Annotated[str, Path(description="A spell `id` from the spell list, like `magic_missile`.")],
) -> SpellTemplate:
    """Return osrlib's template for one spell: its range, duration, and effects."""
    return lookup(load_spells().get, "spell", spell_id)


@router.get("/equipment", summary="List the equipment")
def list_equipment(
    page: PageQuery,
    q: _Q = None,
    item_type: Annotated[ItemType | None, Query(description="Keep the items of one type.")] = None,
) -> Page[EquipmentSummary]:
    """List the mundane equipment a character can buy: weapons first, then armour, gear, and ammunition.

    Pass `item_type` to keep one type of item.

    Returns:
        A page of equipment summaries.
    """
    return paginate(equipment_summaries(q, item_type), page)


@router.get("/equipment/{item_id}", summary="Get an equipment item", responses=problem_responses(404))
def get_equipment_item(
    item_id: Annotated[str, Path(description="An equipment `id` from the equipment list, like `sword`.")],
) -> EquipmentItem:
    """Return osrlib's template for one weapon, armour, gear, or ammunition item.

    `item_type` is `weapon`, `armour`, `gear`, or `ammunition`.
    """
    return EquipmentItem(lookup(load_equipment().get, "equipment item", item_id))


@router.get("/magic-items", summary="List the magic items")
def list_magic_items(
    page: PageQuery,
    q: _Q = None,
    category: Annotated[MagicItemCategory | None, Query(description="Keep the items of one category.")] = None,
) -> Page[MagicItemSummary]:
    """List the magic items in osrlib's order.

    Returns:
        A page of magic item summaries.
    """
    return paginate(magic_item_summaries(q, category), page)


@router.get("/magic-items/{item_id}", summary="Get a magic item", responses=problem_responses(404))
def get_magic_item(
    item_id: Annotated[str, Path(description="A magic item `id` from the magic item list, like `potion_of_healing`.")],
) -> MagicItemTemplate:
    """Return osrlib's template for one magic item."""
    return lookup(load_magic_items().get, "magic item", item_id)


@router.get("/languages", summary="List the languages")
def list_languages(page: PageQuery) -> Page[Language]:
    """List the languages in osrlib's catalog, in alphabetical order by id.

    A character speaks the language of its alignment without choosing it, so the alignment languages aren't
    listed. When you create a character with a high Intelligence, pass the `id` of a language whose
    `choosable` is `true` in `extra_languages`.

    Returns:
        A page of languages.
    """
    return paginate(load_languages().languages, page)


@router.get("/tables", summary="List the printed tables")
def list_tables(page: PageQuery) -> Page[str]:
    """List the names of the printed tables: `abilities`, `combat`, `encounters`, and `treasure`.

    Pass a name to `/v1/rules/tables/{name}` to read that set of tables.

    Returns:
        A page of table names.
    """
    return paginate(list(_TABLES), page)


@router.get("/tables/{name}", summary="Get a set of printed tables", responses=problem_responses(404))
def get_table(
    name: Annotated[str, Path(description="A table name: `abilities`, `combat`, `encounters`, or `treasure`.")],
) -> TableSet:
    """Return osrlib's model for one set of tables.

    `abilities` returns `AbilityTables`: what each ability score is worth. `combat` returns `CombatTables`:
    the attack matrix, saving throws, XP awards, turning, and reactions. `encounters` returns
    `EncounterTables`: the wandering monsters by dungeon level. `treasure` returns `TreasureTables`: the
    treasure types. An unknown name is a 404.
    """
    loader = _TABLES.get(name)
    if loader is None:
        raise not_found("table", name)
    return TableSet(loader())


@router.get("/commands", summary="List the commands")
def list_commands(
    page: PageQuery,
    kind: Annotated[CommandKind | None, Query(description="Keep the play commands or the referee commands.")] = None,
    mode: Annotated[SessionMode | None, Query(description="Keep the commands that are legal in this mode.")] = None,
) -> Page[CommandSummary]:
    """List the commands a session accepts, in osrlib's order.

    A `play` command is one a player may send, and a `referee` command is one only a referee may send.
    `allowed_modes` lists the session modes the command is legal in. osrlib has more commands than the default
    `limit`, so pass `limit=200` to read them all in one request.

    Returns:
        A page of command summaries.
    """
    return paginate(command_summaries(kind, mode), page)


@router.get("/commands/{command_type}", summary="Get a command", responses=problem_responses(404))
def get_command(
    command_type: Annotated[str, Path(description="A `command_type` from the command list, like `move_party`.")],
) -> CommandDetail:
    """Return one command's summary and the JSON Schema of its request body.

    Post a body that matches `json_schema`, with `command_type` set.

    Args:
        command_type: The `command_type` of an osrlib command.

    Returns:
        The command's summary fields and `json_schema`.
    """
    return command_detail(command_type)


@router.get("/license", summary="Get the license of the rules content")
def get_license() -> License:
    """Return the Open Game License 1.0a text that covers the rules content, with its Section 15 notice."""
    text = resources.files("osrlib.data").joinpath("LICENSE-OGL.md").read_text(encoding="utf-8")
    return License(name="Open Game License 1.0a", text=text)
