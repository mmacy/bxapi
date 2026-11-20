"""The server creates characters from a client's choices with osrlib's step-by-step creation functions.

A character starts from an ability roll the server stored earlier, or from scores the server
rolls in the same request. Either way, one seed decides every roll: the ability scores, the hit
points, and the starting gold. [`roll_character`][bxapi.creation.roll_character] rolls a new
character from a seed, and [`character_from_roll`][bxapi.creation.character_from_roll] creates
one from a stored roll and its seed. Both run the step-by-step creation functions from
`osrlib.core.character`, in the order osrlib lists them, so the result is the character that
osrlib's `create_character` returns for the same seed and choices. bxapi doesn't call
`create_character`, because it reports only the first refused choice, as a `ValueError` with no
`Rejection` records.

Each step that refuses a choice stops the creation with 422 `character_invalid`. When osrlib
returns `Rejection` records for the step, the problem document includes them as `rejections`.
When the server refuses one input with no rejections from osrlib, like an unknown class id, the
problem document has `field`, `index` for a list, and `id`, which locate the input.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from osrlib.core.abilities import AbilityAdjustment, AbilityScore, apply_adjustment, validate_adjustment
from osrlib.core.alignment import Alignment
from osrlib.core.character import (
    CHARACTER_CREATION_STREAM,
    AbilityScoreRolls,
    Character,
    HitPointRoll,
    choose_starting_spells,
    roll_ability_scores,
    roll_hit_points,
    roll_starting_gold,
    validate_class_choice,
    validate_extra_languages,
    validate_starting_spells,
)
from osrlib.core.classes import ClassDefinition, SavingThrows, level_title, xp_modifier_pct
from osrlib.core.dice import RollResult
from osrlib.core.items import Inventory, ItemInstance, equip, purchase, validate_equip, validate_purchase
from osrlib.core.rng import RngStream, RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.core.validation import Rejection
from osrlib.data import load_ability_tables, load_classes, load_equipment, load_spells
from pydantic import Field

from bxapi.errors import ApiError, invalid
from bxapi.models import ApiModel, RequestModel

__all__ = [
    "CharacterChoices",
    "CreationRolls",
    "DerivedValues",
    "GeneratedCharacter",
    "Purchase",
    "MAX_NAME_LENGTH",
    "MAX_QUANTITY",
    "character_from_roll",
    "creation_stream",
    "derive",
    "eligible_class_ids",
    "roll_character",
    "scores_from_json",
]


MAX_NAME_LENGTH = 100
"""The longest character name the server accepts, in characters."""

MAX_QUANTITY = 10_000
"""The most lots of one item a single purchase can buy."""


class Purchase(RequestModel):
    """One item to buy with the starting gold."""

    item_id: str
    """The id of an item from `GET /v1/rules/equipment`, like `sword`."""
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)
    """How many lots to buy, from 1 to 10,000. A lot is the unit the item is sold in. osrlib sells
    weapons and armour one at a time, and gear and ammunition in bundles, so one lot of torches is
    six torches."""


class CharacterChoices(RequestModel):
    """The choices a player makes for a new first-level character."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    """The character's name, 1 to 100 characters."""
    class_id: str
    """A class id from `GET /v1/rules/classes`, like `fighter`."""
    alignment: Alignment
    """The character's alignment."""
    adjustment: AbilityAdjustment | None = None
    """A trade of ability points, as osrlib's `AbilityAdjustment`. `lowered` maps each ability to
    take points from to the points taken, and `raised` maps each ability to add points to to the
    points added. The keys are `str` (Strength), `int` (Intelligence), `wis` (Wisdom), `dex`
    (Dexterity), `con` (Constitution), and `cha` (Charisma). osrlib checks the trade, and the server
    returns a trade osrlib refuses as 422 `character_invalid` with osrlib's rejections. Leave the
    field out to keep the scores as rolled."""
    starting_spell_ids: list[str] = []
    """The spells in an arcane caster's first spell book. A first-level magic-user or elf needs
    exactly one first-level spell. Leave it empty for every other class."""
    extra_languages: list[str] = []
    """The extra languages a high INT allows, as language ids from `GET /v1/rules/languages`."""
    purchases: list[Purchase] = []
    """Starting gear to buy, in order, with the starting gold."""
    equip_item_ids: list[str] = []
    """The ids of purchased items to wear or wield, equipped in order."""
    hp_reroll_at_first_level: bool = False
    """Whether to use the optional rule that rerolls a first hit die that shows 1 or 2."""


class CreationRolls(ApiModel):
    """The dice osrlib rolled to create a character."""

    ability_rolls: AbilityScoreRolls
    """The six ability scores as rolled, before any adjustment, with the three dice behind each."""
    hit_point_roll: HitPointRoll
    """The first-level hit point roll, with every die thrown."""
    gold_roll: RollResult
    """The 3d6 × 10 roll for starting gold. Its `total` is the gold before the purchases."""


class DerivedValues(ApiModel):
    """The values osrlib computes from a character and doesn't store in the character document."""

    thac0: int
    """The d20 roll the character needs to hit armour class 0."""
    attack_bonus: int
    """The bonus to an attack roll under ascending armour class. It describes the same attack as
    `thac0`."""
    armour_class: int
    """The descending armour class, where lower is better."""
    armour_class_ascending: int
    """The ascending armour class, where higher is better."""
    saves: SavingThrows
    """The five saving throw targets for the character's level."""
    melee_modifier: int
    """The STR modifier to melee attack and damage rolls."""
    open_doors_chance: int
    """The chance in 6 of forcing a stuck door open, from STR."""
    missile_modifier: int
    """The DEX modifier to missile attack rolls."""
    initiative_modifier: int
    """The DEX modifier to individual initiative, used only under the individual initiative rule."""
    hit_point_modifier: int
    """The CON modifier to each hit die."""
    magic_save_modifier: int
    """The WIS modifier to saving throws against magic."""
    npc_reaction_modifier: int
    """The CHA modifier to monster reaction rolls when the character speaks."""
    xp_modifier_pct: int
    """The percentage the class adds to or takes from each experience award, from the prime
    requisites."""
    level_title: str | None
    """The title of the character's class at its level, like `Veteran`, or `null` past the last
    title."""
    alignment_tongue: str
    """The id of the secret language of the character's alignment, like `alignment_lawful`."""
    languages: list[str]
    """Every language the character speaks: the alignment tongue, the class's languages, and the
    extra languages."""


@dataclass(frozen=True)
class GeneratedCharacter:
    """A character created from scores rolled in the same request, and the dice rolled for it."""

    character: Character
    """The first-level character."""
    rolls: CreationRolls
    """The dice osrlib rolled."""


def creation_stream(seed: int) -> RngStream:
    """Return osrlib's character creation stream for a seed.

    Args:
        seed: The seed for the request.

    Returns:
        The stream osrlib's creation functions draw from.
    """
    return RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM)


def scores_from_json(scores: Mapping[str, int]) -> dict[AbilityScore, int]:
    """Return ability scores read back from JSON, keyed by osrlib's `AbilityScore`.

    Args:
        scores: Scores keyed by the abilities' values, like `{"str": 12}`.

    Returns:
        The same scores, keyed by `AbilityScore`.
    """
    return {AbilityScore(ability): score for ability, score in scores.items()}


def eligible_class_ids(scores: dict[AbilityScore, int]) -> list[str]:
    """Return the ids of the classes whose requirements a set of scores meets.

    The function asks osrlib's `validate_class_choice` about each class in `load_classes`, in
    the catalog's order.

    Args:
        scores: All six ability scores.

    Returns:
        The class ids with no rejections.

    Examples:
        ```python
        from osrlib.core.abilities import AbilityScore

        from bxapi.creation import eligible_class_ids

        scores = dict.fromkeys(AbilityScore, 12)
        scores[AbilityScore.CON] = 7
        assert "dwarf" not in eligible_class_ids(scores)
        assert "fighter" in eligible_class_ids(scores)
        ```
    """
    return [definition.id for definition in load_classes().classes if not validate_class_choice(scores, definition)]


def _refused(detail: str, rejections: Sequence[Rejection]) -> ApiError:
    return invalid(
        "character_invalid", detail, rejections=[rejection.model_dump(mode="json") for rejection in rejections]
    )


def _bad_input(detail: str, field: str, input_id: str, index: int | None = None) -> ApiError:
    if index is None:
        return invalid("character_invalid", detail, field=field, id=input_id)
    return invalid("character_invalid", detail, field=field, index=index, id=input_id)


def _class(class_id: str) -> ClassDefinition:
    try:
        return load_classes().get(class_id)
    except ValueError as error:
        raise _bad_input(f"No class has the id {class_id!r}.", "class_id", class_id) from error


def _buy_and_equip(inventory: Inventory, definition: ClassDefinition, choices: CharacterChoices) -> None:
    equipment = load_equipment()
    for index, wanted in enumerate(choices.purchases):
        try:
            template = equipment.get(wanted.item_id)
        except ValueError as error:
            raise _bad_input(f"No item has the id {wanted.item_id!r}.", "purchases", wanted.item_id, index) from error
        rejections = validate_purchase(inventory.purse, template, wanted.quantity)
        if rejections:
            raise _refused("The starting gold doesn't cover the purchases.", rejections)
        purchase(inventory, template, wanted.quantity)
    bought = {wanted.item_id for wanted in choices.purchases}
    for index, item_id in enumerate(choices.equip_item_ids):
        instance = next(
            (item for item in inventory.items if isinstance(item, ItemInstance) and item.template.id == item_id), None
        )
        if instance is None and item_id in bought:
            detail = (
                f"equip_item_ids equips {item_id!r} more times than the purchases buy it. Equipping a purchase equips "
                "all of it, whatever its quantity."
            )
            raise _bad_input(detail, "equip_item_ids", item_id, index)
        if instance is None:
            raise _bad_input(f"The purchases don't include {item_id!r}.", "equip_item_ids", item_id, index)
        rejections = validate_equip(definition, instance, inventory)
        if rejections:
            raise _refused(f"The class can't equip the item {item_id!r}.", rejections)
        equip(inventory, definition, instance)


def roll_character(choices: CharacterChoices, seed: int) -> GeneratedCharacter:
    """Create a first-level character from a seed, rolling the ability scores first.

    The function draws from osrlib's character creation stream for `seed`, in the order that
    osrlib's `create_character` draws: scores, hit points, then gold. So the same seed and
    choices give the character `create_character` returns. The steps after the ability roll are
    the ones [`character_from_roll`][bxapi.creation.character_from_roll] lists.

    Args:
        choices: The player's choices.
        seed: The seed for every creation roll.

    Returns:
        The character, and the dice rolled for it.

    Raises:
        ApiError: With status 422 and the code `character_invalid`, at the first step that
            refuses a choice.
    """
    definition = _class(choices.class_id)
    stream = creation_stream(seed)
    ability_rolls = roll_ability_scores(stream)
    character, hit_point_roll, gold_roll = _create(choices, definition, dict(ability_rolls.scores), stream)
    rolls = CreationRolls(ability_rolls=ability_rolls, hit_point_roll=hit_point_roll, gold_roll=gold_roll)
    return GeneratedCharacter(character=character, rolls=rolls)


def character_from_roll(choices: CharacterChoices, seed: int, scores: dict[AbilityScore, int]) -> Character:
    """Create a first-level character from a stored ability roll and the seed it was rolled from.

    The function re-creates the creation stream for `seed` and rolls the ability scores on it
    again, so the hit points and the gold come from the same draws on every attempt. The
    character gets `scores`, the scores the player saw. With the osrlib that rolled them, the
    scores rolled again are the same, and the character is the one osrlib's `create_character`
    returns for the same seed and choices.

    After the ability roll, the function runs osrlib's step-by-step creation functions in the
    order the `osrlib.core.character` module lists them:

    1. `validate_class_choice`.
    2. `validate_adjustment` and `apply_adjustment`, when the choices include an adjustment.
    3. `validate_starting_spells`.
    4. `roll_hit_points`.
    5. `validate_extra_languages`.
    6. `roll_starting_gold`.
    7. `validate_purchase` and `purchase` for each purchase, then `validate_equip` and `equip`
       for each item to equip.
    8. The `Character` constructor, then `choose_starting_spells` to write the spell book.

    Args:
        choices: The player's choices.
        seed: The seed the stored roll was rolled from.
        scores: The stored roll's six ability scores.

    Returns:
        The character.

    Raises:
        ApiError: With status 422 and the code `character_invalid`, at the first step that
            refuses a choice. The error includes osrlib's rejections when the step returns
            them.
    """
    definition = _class(choices.class_id)
    stream = creation_stream(seed)
    roll_ability_scores(stream)
    character, _, _ = _create(choices, definition, scores, stream)
    return character


def _create(
    choices: CharacterChoices, definition: ClassDefinition, scores: dict[AbilityScore, int], stream: RngStream
) -> tuple[Character, HitPointRoll, RollResult]:
    rejections = validate_class_choice(scores, definition)
    if rejections:
        raise _refused(f"The ability scores don't meet the requirements of the class {definition.id!r}.", rejections)
    if choices.adjustment is not None:
        prime, fixed = definition.prime_requisites, definition.may_not_lower
        rejections = validate_adjustment(scores, choices.adjustment, prime, fixed)
        if rejections:
            raise _refused("The ability adjustment breaks the rules for trading ability points.", rejections)
        scores = apply_adjustment(scores, choices.adjustment, prime, fixed)
    spells = load_spells()
    rejections = validate_starting_spells(definition, spells, choices.starting_spell_ids)
    if rejections:
        raise _refused("The starting spells don't fit the class's first spell book.", rejections)
    con_modifier = load_ability_tables().hit_point_modifier(scores[AbilityScore.CON])
    ruleset = Ruleset(hp_reroll_at_first_level=choices.hp_reroll_at_first_level)
    hit_point_roll = roll_hit_points(definition, con_modifier, ruleset, stream)
    rejections = validate_extra_languages(definition, scores[AbilityScore.INT], choices.extra_languages)
    if rejections:
        raise _refused("The extra languages aren't allowed for this character.", rejections)
    gold_roll = roll_starting_gold(stream)
    inventory = Inventory()
    inventory.purse.gp = gold_roll.total
    _buy_and_equip(inventory, definition, choices)
    character = Character(
        name=choices.name,
        class_id=definition.id,
        race=definition.race,
        level=1,
        xp=0,
        scores=scores,
        alignment=choices.alignment,
        extra_languages=tuple(choices.extra_languages),
        max_hp=hit_point_roll.hit_points,
        current_hp=hit_point_roll.hit_points,
        inventory=inventory,
    )
    rejections = choose_starting_spells(character, definition, spells, choices.starting_spell_ids)
    if rejections:
        raise _refused("The starting spells don't fit the class's first spell book.", rejections)
    return character, hit_point_roll, gold_roll


def derive(character: Character) -> DerivedValues:
    """Return the values osrlib computes from a character.

    Every value but two is a property of osrlib's `Character`. `xp_modifier_pct` and
    `level_title` come from the functions of the same names in `osrlib.core.classes`.

    Args:
        character: The character.

    Returns:
        The derived values.
    """
    definition = character.definition
    return DerivedValues(
        thac0=character.thac0,
        attack_bonus=character.attack_bonus,
        armour_class=character.armour_class,
        armour_class_ascending=character.armour_class_ascending,
        saves=character.saves,
        melee_modifier=character.melee_modifier,
        open_doors_chance=character.open_doors_chance,
        missile_modifier=character.missile_modifier,
        initiative_modifier=character.initiative_modifier,
        hit_point_modifier=character.hit_point_modifier,
        magic_save_modifier=character.magic_save_modifier,
        npc_reaction_modifier=character.npc_reaction_modifier,
        xp_modifier_pct=xp_modifier_pct(definition, character.scores),
        level_title=level_title(definition, character.level),
        alignment_tongue=character.alignment_tongue,
        languages=list(character.languages),
    )
