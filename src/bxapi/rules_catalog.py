"""The summaries the rules reference lists, and the lookups that build them from osrlib's catalogs.

Each `*_summaries` function reads one catalog that an `osrlib.data` loader returns, applies the
filters of its endpoint, and returns summaries in the catalog's order. osrlib's loaders cache
their catalogs, so none of these functions keeps a copy.
"""

import inspect
import re
import unicodedata
from collections.abc import Callable
from typing import Any, Literal

from osrlib.core.abilities import AbilityScore, AbilityTables
from osrlib.core.items import ItemTemplate, MagicItemCategory, MagicItemTemplate
from osrlib.core.monsters import AlignmentSpec, MonsterHitDice, MonsterTemplate
from osrlib.core.spells import SpellTemplate, caster_profile
from osrlib.core.tables import CombatTables, EncounterTables
from osrlib.core.treasure import TreasureTables
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, Command, SessionMode
from osrlib.data import load_classes, load_equipment, load_magic_items, load_monsters, load_spells
from pydantic import RootModel

from bxapi.commands import CommandKind, command_kind
from bxapi.errors import not_found
from bxapi.models import ApiModel

__all__ = [
    "SPELL_LEVEL_MAX",
    "SPELL_LEVEL_MIN",
    "ClassSummary",
    "CommandDetail",
    "CommandSummary",
    "EquipmentItem",
    "EquipmentSummary",
    "ItemType",
    "MagicItemSummary",
    "MonsterSummary",
    "SpellSummary",
    "TableSet",
    "class_summaries",
    "command_detail",
    "command_summaries",
    "equipment_summaries",
    "lookup",
    "magic_item_summaries",
    "monster_summaries",
    "spell_summaries",
]

ItemType = Literal["weapon", "armour", "gear", "ammunition"]
"""The `item_type` of an equipment template."""


def _spell_level_bounds() -> tuple[int, int]:
    limits = {
        name: getattr(constraint, name)
        for constraint in SpellTemplate.model_fields["level"].metadata
        for name in ("ge", "le")
        if hasattr(constraint, name)
    }
    return limits["ge"], limits["le"]


SPELL_LEVEL_MIN, SPELL_LEVEL_MAX = _spell_level_bounds()
"""The lowest and highest spell level, read from the constraints on osrlib's `SpellTemplate.level`."""


class ClassSummary(ApiModel):
    """The facts a class picker shows, from osrlib's `ClassDefinition`."""

    id: str
    """The class id to pass as `class_id` when you create a character, like `"fighter"`."""
    name: str
    """The display name, like `"Fighter"`."""
    race: str
    """The race of the class's characters, as a lowercase identifier like `"human"` or `"dwarf"`."""
    prime_requisites: tuple[AbilityScore, ...]
    """The ability scores that determine the class's XP bonus."""
    requirements: dict[AbilityScore, int]
    """The lowest score a character needs in each listed ability to take the class."""
    hit_die: int
    """The die osrlib rolls for each level's hit points, as its number of sides."""
    max_level: int
    """The highest level a character of the class can reach."""


class MonsterSummary(ApiModel):
    """The statistics a monster list shows, from osrlib's `MonsterTemplate`."""

    id: str
    """The monster id, like `"goblin"`."""
    name: str
    """The name as the rules print it, like `"Goblin"` or `"Centipede, Giant"`. To name the monster in a sentence, use
    the `display_name` of `GET /v1/rules/monsters/{monster_id}`, like `"giant centipede"`."""
    hit_dice: MonsterHitDice
    """The monster's hit dice, as osrlib's `MonsterHitDice`."""
    ac: int | None
    """The armour class where lower is better. It's `null` for a creature that every attack hits without
    a roll, when the template's `attack_roll_required` is `false`."""
    ac_ascending: int | None
    """The same armour class where higher is better. It's `null` when `ac` is."""
    xp: int
    """The experience for defeating one, as the Old-School Essentials System Reference Document (SRD) prints it."""
    alignment: AlignmentSpec
    """The alignments the monster may have, and the usual one when the monster has one."""


class SpellSummary(ApiModel):
    """The facts a spell list shows, from osrlib's `SpellTemplate`."""

    id: str
    """The spell id, like `"magic_missile"`."""
    name: str
    """The display name, like `"Magic Missile"`."""
    level: int
    """The spell level, from 1 to 6."""
    spell_list: str
    """The id of the spell list the spell is on, like `"cleric"` or `"magic_user"`. A class casts the spells
    on the list in the `spell_list` parameter of its `divine_magic` or `arcane_magic` ability."""
    range: str
    """The range line as printed."""
    duration: str
    """The duration line as printed."""


class EquipmentSummary(ApiModel):
    """The facts an equipment list shows, from osrlib's four equipment templates."""

    id: str
    """The item id to pass in a purchase, like `"sword"`."""
    name: str
    """The display name, like `"Sword"`."""
    item_type: ItemType
    """Which of the four equipment templates the item is."""
    cost_gp: int
    """The price in gold pieces."""


class MagicItemSummary(ApiModel):
    """The facts a magic item list shows, from osrlib's `MagicItemTemplate`."""

    id: str
    """The magic item id, like `"potion_of_healing"`."""
    name: str
    """The display name."""
    category: MagicItemCategory
    """The kind of item, like `potion`, `scroll`, `ring`, or `sword`."""


class CommandSummary(ApiModel):
    """One command you can send to a session, as osrlib defines it."""

    command_type: str
    """The `command_type` to send in a command request body, like `"move_party"`."""
    kind: CommandKind
    """Whether a player may send the command (`play`), or only a referee (`referee`)."""
    allowed_modes: list[SessionMode]
    """The session modes the command is legal in, in alphabetical order."""
    summary: str
    """The first paragraph of osrlib's documentation for the command, on one line, with each link
    reduced to its text."""


class CommandDetail(CommandSummary):
    """A command's summary, with the JSON Schema of its request body."""

    json_schema: dict[str, Any]
    """The JSON Schema of osrlib's command class, which pydantic generates. `command_type` is a property
    of the schema but isn't in its `required` list. Include it in every request body anyway."""


class EquipmentItem(RootModel[ItemTemplate]):
    """One equipment template: `WeaponTemplate`, `ArmourTemplate`, `GearTemplate`, or `AmmunitionTemplate`.

    The `item_type` field identifies the template. The JSON is osrlib's model, unchanged.
    """


class TableSet(RootModel[AbilityTables | CombatTables | EncounterTables | TreasureTables]):
    """One set of printed tables, as osrlib's model for the table name.

    `abilities` is osrlib's `AbilityTables`, `combat` is `CombatTables`, `encounters` is
    `EncounterTables`, and `treasure` is `TreasureTables`. The JSON is osrlib's model, unchanged.
    """


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _matches(q: str | None, *texts: str) -> bool:
    if q is None:
        return True
    needle = _normalize(q)
    return any(needle in _normalize(text) for text in texts)


def lookup[Entry](get: Callable[[str], Entry], resource: str, entry_id: str) -> Entry:
    """Return the entry a catalog's `get` method finds, or raise the 404 for an unknown id.

    Args:
        get: The `get` method of an osrlib catalog, which raises `ValueError` for an unknown id.
        resource: The name of the resource in the 404, like `"monster"`.
        entry_id: The id the caller sent.

    Returns:
        The entry.

    Raises:
        ApiError: With status 404 and the code `not_found`, if the catalog has no such id.
    """
    try:
        return get(entry_id)
    except ValueError:
        raise not_found(resource, entry_id) from None


def class_summaries() -> list[ClassSummary]:
    """Return a summary of every class, in the catalog's order."""
    return [
        ClassSummary(
            id=definition.id,
            name=definition.name,
            race=definition.race,
            prime_requisites=definition.prime_requisites,
            requirements=dict(definition.requirements),
            hit_die=definition.hit_die,
            max_level=definition.max_level,
        )
        for definition in load_classes().classes
    ]


def monster_summary(monster: MonsterTemplate) -> MonsterSummary:
    """Return the summary of one monster template."""
    return MonsterSummary(
        id=monster.id,
        name=monster.name,
        hit_dice=monster.hit_dice,
        ac=monster.ac,
        ac_ascending=monster.ac_ascending,
        xp=monster.xp,
        alignment=monster.alignment,
    )


def monster_summaries(q: str | None) -> list[MonsterSummary]:
    """Return a summary of each monster whose id or name contains `q`, in the catalog's order."""
    return [monster_summary(monster) for monster in load_monsters().monsters if _matches(q, monster.id, monster.name)]


def spell_summaries(q: str | None, class_id: str | None, level: int | None) -> list[SpellSummary]:
    """Return a summary of each spell that matches every filter, in the catalog's order.

    Args:
        q: A substring of the spell's id or name, ignoring case.
        class_id: A class id. The result contains the spells on the class's spell list, and it's
            empty for a class that casts no spells.
        level: A spell level.

    Raises:
        ApiError: With status 404 and the code `not_found`, if `class_id` isn't a class id.
    """
    catalog = load_spells()
    spells: tuple[SpellTemplate, ...] = catalog.spells
    if class_id is not None:
        profile = caster_profile(lookup(load_classes().get, "class", class_id))
        spells = catalog.by_list(profile.spell_list, level) if profile is not None else ()
    elif level is not None:
        spells = tuple(spell for spell in spells if spell.level == level)
    return [
        SpellSummary(
            id=spell.id,
            name=spell.name,
            level=spell.level,
            spell_list=spell.spell_list,
            range=spell.range,
            duration=spell.duration,
        )
        for spell in spells
        if _matches(q, spell.id, spell.name)
    ]


def equipment_summaries(q: str | None, item_type: ItemType | None) -> list[EquipmentSummary]:
    """Return a summary of each item that matches the filters: weapons, armour, gear, then ammunition.

    Args:
        q: A substring of the item's id or name, ignoring case.
        item_type: Keep only items of this type.
    """
    catalog = load_equipment()
    templates: list[ItemTemplate] = [*catalog.weapons, *catalog.armour, *catalog.gear, *catalog.ammunition]
    return [
        EquipmentSummary(id=item.id, name=item.name, item_type=item.item_type, cost_gp=item.cost_gp)
        for item in templates
        if _matches(q, item.id, item.name) and (item_type is None or item.item_type == item_type)
    ]


def magic_item_summaries(q: str | None, category: MagicItemCategory | None) -> list[MagicItemSummary]:
    """Return a summary of each magic item that matches the filters, in the catalog's order.

    Args:
        q: A substring of the item's id or name, ignoring case.
        category: Keep only items of this category.
    """
    items: list[MagicItemTemplate] = list(load_magic_items().items)
    return [
        MagicItemSummary(id=item.id, name=item.name, category=item.category)
        for item in items
        if _matches(q, item.id, item.name) and (category is None or item.category == category)
    ]


_DOC_LINK = re.compile(r"\[(`[^`]+`)\]\[[^\]]*\]")


def _command_type(command_class: type[Command]) -> str:
    return command_class.model_fields["command_type"].default


def _summary_line(command_class: type[Command]) -> str:
    docstring = inspect.getdoc(command_class) or ""
    return _DOC_LINK.sub(r"\1", " ".join(docstring.split("\n\n")[0].split()))


def _command_summary(command_class: type[Command]) -> CommandSummary:
    command_type = _command_type(command_class)
    return CommandSummary(
        command_type=command_type,
        kind=command_kind(command_type),
        allowed_modes=sorted(command_class.allowed_modes),
        summary=_summary_line(command_class),
    )


def command_summaries(kind: CommandKind | None, mode: SessionMode | None) -> list[CommandSummary]:
    """Return a summary of each command that matches the filters, in osrlib's order.

    Args:
        kind: Keep only play commands or only referee commands.
        mode: Keep only the commands that are legal in this session mode.
    """
    summaries = [_command_summary(command_class) for command_class in ALL_COMMAND_CLASSES]
    return [
        summary
        for summary in summaries
        if (kind is None or summary.kind is kind) and (mode is None or mode in summary.allowed_modes)
    ]


def command_detail(command_type: str) -> CommandDetail:
    """Return a command's summary and JSON Schema.

    Args:
        command_type: The `command_type` of an osrlib command.

    Returns:
        The command's summary fields and the JSON Schema of its class.

    Raises:
        ApiError: With status 404 and the code `not_found`, if osrlib has no such command.
    """
    for command_class in ALL_COMMAND_CLASSES:
        if _command_type(command_class) == command_type:
            summary = _command_summary(command_class)
            return CommandDetail(**summary.model_dump(), json_schema=command_class.model_json_schema())
    raise not_found("command", command_type)
