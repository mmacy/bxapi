"""Stored characters, the ability score rolls that a character starts from, and the stateless character generator."""

from typing import Annotated, Any, Self

from fastapi import APIRouter, Body, Path, Query, Request, Response
from osrlib.core.abilities import AbilityScore, AdjustmentLimits
from osrlib.core.character import AbilityScoreRolls, Character, roll_ability_scores
from osrlib.core.ruleset import Ruleset
from pydantic import model_validator

from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.creation import (
    AbilityScoreMethod,
    CharacterChoices,
    CreationRolls,
    DerivedValues,
    ability_score_method,
    character_from_roll,
    class_adjustment_limits,
    creation_stream,
    derive,
    eligible_class_ids,
    roll_character,
    scores_from_json,
)
from bxapi.db import AbilityRollRow, CharacterDocumentRow, Database, fits_integer_column
from bxapi.errors import VALIDATION_ERROR, ApiError, conflict, invalid, not_found, problem_responses
from bxapi.idempotency import IdempotentRoute
from bxapi.json_body import accepts_document
from bxapi.models import ApiModel, RequestModel, has_control_character
from bxapi.pagination import Page, PageQuery
from bxapi.seeds import Seed, referee_seed, seed_or_draw

__all__ = [
    "AbilityRoll",
    "AbilityRollRequest",
    "CharacterPreview",
    "CharacterPreviewRequest",
    "CharacterRequest",
    "CharacterSummary",
    "GenerateCharacterRequest",
    "GeneratedCharacterResponse",
    "StoredCharacter",
    "router",
]

router = APIRouter(route_class=IdempotentRoute)

_SEED_FORBIDDEN = (
    "`referee_required`: A player sent `seed`. Omit `seed` so the server draws one, or call with a `referee` key."
)
_CHARACTER_INVALID = (
    "`character_invalid`: A choice violates a B/X creation rule, such as choosing a class whose minimum scores the "
    "character misses, omitting a magic-user's starting spell, or spending more than starting gold. `rejections` "
    "lists osrlib's reasons (`code` and `params`). When an unknown `class_id` or `item_id` is sent, or "
    "`equip_item_ids` names an unpurchased item, the problem includes `field`, `id`, and `index` (for list fields) "
    "instead of `rejections`. Adjust the choice and retry."
)
_REFEREE_ONLY = "`referee_required`: Only a referee may call this operation. Send an API key with the `referee` role."
_VERSION_UNSUPPORTED = (
    "`version_unsupported`: The document's `schema_version` exceeds the `schema_version` that `GET /v1/meta` "
    "reports. Export the document with a compatible schema version or upgrade the server's osrlib."
)

RollId = Annotated[
    str,
    Path(description="Ability roll `id` (`rol_` followed by 22 characters), from `POST /v1/ability-rolls`."),
]
CharacterId = Annotated[
    str,
    Path(
        description="Character `id` (`chr_` followed by 22 characters), from `POST /v1/characters`, "
        "`POST /v1/characters/import`, or `GET /v1/characters`."
    ),
]
"""The character routes. The router sets no tag, because the character generator is in the
`generate` group of the OpenAPI document and every other route is in `characters`."""


class AbilityRollRequest(RequestModel):
    """Optional request body for `POST /v1/ability-rolls`."""

    seed: Seed | None = None
    """Referee only. Seed (0 to 2^53 - 1) that determines the six ability scores as well as the hit
    point and gold rolls of the character created from this roll. The server never returns `seed`.
    Omit `seed` (or send no body) to let the server draw one."""
    ruleset: Ruleset | None = None
    """Optional rules for the roll, as osrlib's `Ruleset`. Send the ruleset of the table the
    character is for, the one you pass to `POST /v1/sessions`. The roll reads one field,
    `ability_score_method`: `"3d6"` (the default) rolls three d6 for each score, and
    `"4d6_drop_lowest"` rolls four and drops the lowest. The server stores the method with the roll.
    Send the same ruleset when you preview or create the character from the roll. Omit or set to
    `null` for the rules as written."""


class AbilityRoll(ApiModel):
    """Stored set of six rolled ability scores and the classes they qualify for."""

    id: str
    """Ability roll identifier (`rol_` followed by 22 characters). Pass as `ability_roll_id` to
    `POST /v1/characters`, or as `roll_id` to `POST /v1/ability-rolls/{roll_id}/preview`."""
    scores: dict[AbilityScore, int]
    """Six rolled ability scores (`str`, `int`, `wis`, `dex`, `con`, `cha`), each from 3 to 18."""
    ability_score_method: AbilityScoreMethod
    """How osrlib rolled the scores: `"3d6"` or `"4d6_drop_lowest"`, from the `ability_score_method` of the
    `ruleset` that `POST /v1/ability-rolls` received. A preview or a creation from this roll needs a `ruleset`
    with the same method."""
    rolls: AbilityScoreRolls
    """osrlib's `AbilityScoreRolls` behind `scores`, the same as `rolls.ability_rolls` on
    `POST /v1/ability-rolls/{roll_id}/preview`. `rolls.rolls` has the three d6 that count toward each score, in
    the order osrlib rolled them. `rolls.dropped` has the die that a `4d6_drop_lowest` roll dropped for each
    ability, and it's empty for a `3d6` roll. `rolls.sub_par` is `true` when the scores are sub-par under the B/X
    rule that lets a referee allow a new roll: every score is 8 or less, or two or more scores are 5 or less."""
    eligible_class_ids: list[str]
    """Class `id` values from `GET /v1/rules/classes` whose minimum ability requirements `scores`
    satisfy, in catalog order."""
    used: bool
    """Whether `POST /v1/characters` has already consumed this roll. Each roll creates at most one
    character, and deleting that character does not reset `used`."""
    created_at: str
    """UTC timestamp when the server stored the roll, in RFC 3339 format such as
    `"2026-11-20T22:01:05Z"`."""


class CharacterRequest(CharacterChoices):
    """Choices and optional ability roll or referee seed for `POST /v1/characters`."""

    ability_roll_id: str | None = None
    """Unused ability roll `id` from `POST /v1/ability-rolls` to take scores, hit points, and
    starting gold from. Omit to roll fresh ability scores during this request. Mutually exclusive
    with `seed`."""
    seed: Seed | None = None
    """Referee only. Seed (0 to 2^53 - 1) for all creation rolls when you omit `ability_roll_id`.
    Mutually exclusive with `ability_roll_id`. The server never returns `seed`."""

    @model_validator(mode="after")
    def _one_source_of_dice(self) -> Self:
        if self.ability_roll_id is not None and self.seed is not None:
            raise ValueError("pass seed or ability_roll_id, not both: an ability roll already has a seed")
        return self


class CharacterPreviewRequest(CharacterChoices):
    """Choices for previewing the character `POST /v1/characters` would create from an ability roll.

    The body has every member `POST /v1/characters` accepts except `ability_roll_id`, because the
    path names the roll, and `seed`, because the roll's seed decides the dice.
    """


class GenerateCharacterRequest(CharacterChoices):
    """Choices and optional seed for rolling an unstored character via `POST /v1/generate/character`."""

    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1) for all creation rolls. Any caller may pass `seed` to reproduce
    the character, or omit it to let the server draw one."""


class StoredCharacter(ApiModel):
    """Stored character record with osrlib's `Character` model and computed stats."""

    id: str
    """Character identifier (`chr_` followed by 22 characters). Pass in `character_ids` to
    `POST /v1/sessions`."""
    name: str
    """Character name."""
    class_id: str
    """Character class `id`, such as `"fighter"`."""
    level: int
    """Character experience level (1 for newly created characters, or higher when imported)."""
    created_at: str
    """UTC timestamp when the server stored the character, in RFC 3339 format such as
    `"2026-11-20T22:01:05Z"`."""
    character: Character
    """Full osrlib `Character` model: ability scores, hit points, inventory, spells, and conditions."""
    derived: DerivedValues
    """Combat, saving throw, modifier, and language values computed by osrlib from `character`."""


class CharacterSummary(ApiModel):
    """Summary fields returned for each character in `GET /v1/characters`."""

    id: str
    """Character identifier (`chr_` followed by 22 characters). Pass to
    `GET /v1/characters/{character_id}` or in `character_ids` on `POST /v1/sessions`."""
    name: str
    """Character name."""
    class_id: str
    """Character class `id`, such as `"fighter"`."""
    level: int
    """Character experience level."""
    max_hp: int
    """Maximum hit points."""
    created_at: str
    """UTC timestamp when the server stored the character, in RFC 3339 format such as
    `"2026-11-20T22:01:05Z"`."""


class GeneratedCharacterResponse(ApiModel):
    """Unstored first-level character, derived stats, creation dice, and seed."""

    seed: Seed
    """Seed used for every creation roll. Send it with the same request body to reproduce this
    character."""
    character: Character
    """Generated osrlib `Character` model (with `id` set to `null`)."""
    derived: DerivedValues
    """Combat, saving throw, modifier, and language values computed by osrlib from `character`."""
    rolls: CreationRolls
    """Individual dice rolled for ability scores, hit points, and starting gold."""


class CharacterPreview(ApiModel):
    """Unstored first-level character that `POST /v1/characters` would create from an ability roll, with its dice."""

    character: Character
    """osrlib `Character` model that `POST /v1/characters` stores for the same roll and choices, with
    `id` set to `null`."""
    derived: DerivedValues
    """Combat, saving throw, modifier, and language values computed by osrlib from `character`."""
    rolls: CreationRolls
    """Individual dice rolled for the roll's ability scores, the hit points, and the starting gold."""


def _db(request: Request) -> Database:
    return request.app.state.db


def _ability_roll(row: AbilityRollRow, rolled: AbilityScoreRolls | None = None) -> AbilityRoll:
    """Return the response for a stored roll.

    The server stores the seed and not the dice. It rolls the scores again from the seed, as
    [`character_from_roll`][bxapi.creation.character_from_roll] does, unless the caller passes the
    roll it just made.
    """
    scores = scores_from_json(row.scores)
    method = ability_score_method(row.ability_score_method)
    return AbilityRoll(
        id=row.id,
        scores=scores,
        ability_score_method=method,
        rolls=roll_ability_scores(creation_stream(row.seed), method=method) if rolled is None else rolled,
        eligible_class_ids=eligible_class_ids(scores),
        used=row.used,
        created_at=row.created_at,
    )


_RULESET_MISMATCH = (
    "`ability_roll_ruleset_mismatch`: The `ability_score_method` of the request's `ruleset` isn't the method the "
    "roll was made with, which the problem's `ability_score_method` names. A request with no `ruleset` asks for "
    "`3d6`. Send the ruleset you sent to `POST /v1/ability-rolls` for this roll, or create a new roll. The roll "
    "stays unused."
)


def _roll_used(roll_id: str) -> ApiError:
    return conflict("ability_roll_used", f"A character was already created from the ability roll {roll_id!r}.")


def _unused_roll(db: Database, client: str, roll_id: str) -> AbilityRollRow:
    roll = db.get_ability_roll(client, roll_id)
    if roll is None:
        raise not_found("ability roll", roll_id)
    if roll.used:
        raise _roll_used(roll_id)
    return roll


def _derived(character: Character) -> DerivedValues:
    try:
        return derive(character)
    except ValueError as error:
        # A document can pass osrlib's model and still refer to an item osrlib doesn't have.
        raise invalid("content_invalid", f"osrlib can't compute the character's values: {error}") from error


def _stored(row: CharacterDocumentRow, character: Character, derived: DerivedValues) -> StoredCharacter:
    return StoredCharacter(
        id=row.id,
        name=row.name,
        class_id=row.class_id,
        level=row.level,
        created_at=row.created_at,
        character=character,
        derived=derived,
    )


def _store(db: Database, client: str, character: Character, roll_id: str | None = None) -> StoredCharacter:
    # The derived values come first, so the server never stores a character that osrlib can't compute them for.
    derived = _derived(character)
    name, class_id, level, max_hp = character.name, character.class_id, character.level, character.max_hp
    document = character.to_document()
    if roll_id is None:
        row = db.insert_character(client, name=name, class_id=class_id, level=level, max_hp=max_hp, document=document)
        return _stored(row, character, derived)
    created = db.insert_character_from_ability_roll(
        client, roll_id, name=name, class_id=class_id, level=level, max_hp=max_hp, document=document
    )
    if created is None:
        if db.get_ability_roll(client, roll_id) is None:
            raise not_found("ability roll", roll_id)
        raise _roll_used(roll_id)
    return _stored(created, character, derived)


@router.post(
    "/ability-rolls",
    status_code=201,
    tags=["characters"],
    summary="Roll ability scores",
    response_description="Stored ability roll with its id, six scores, and eligible class ids.",
    responses=problem_responses({403: [_SEED_FORBIDDEN]}),
)
def create_ability_roll(
    request: Request,
    principal: CurrentPrincipal,
    body: Annotated[AbilityRollRequest, Body(default_factory=AbilityRollRequest)],
) -> AbilityRoll:
    """Roll six ability scores with osrlib, store the roll, and return the scores, their dice, and eligible classes.

    Display `scores` and `eligible_class_ids` to the player. A client that animates the dice shows `rolls.rolls`, the
    three d6 that count toward each score. Then pass `id` as `ability_roll_id` to `POST /v1/characters` along with
    the player's class, alignment, and equipment choices. The request body is optional: with no body, osrlib rolls
    3d6 for each score from a seed the server draws. A referee may pass `seed`.

    To roll under a table's house rules, pass the table's `ruleset`. With `ability_score_method` set to
    `4d6_drop_lowest`, osrlib rolls four d6 for each score and drops the lowest, and `rolls.dropped` has the dropped
    die of each ability. The server stores the method with the roll and returns it as `ability_score_method`. Send
    the same `ruleset` when you preview or create the character, because a request whose ruleset has another method
    gets 409 `ability_roll_ruleset_mismatch`.

    `rolls.sub_par` tells you whether the scores are sub-par under the B/X rules, which let a referee allow the
    player to discard such a character and roll again. osrlib only reports it, and the server accepts a sub-par roll
    like any other.

    The server stores the roll's seed privately and derives the character's hit points and starting gold from that
    same seed when `POST /v1/characters` consumes the roll. Retrying `POST /v1/characters` after a 422
    `character_invalid` error therefore keeps the exact same rolls. To show the player those hit points and that gold
    before you create the character, call `POST /v1/ability-rolls/{roll_id}/preview`.

    Ability rolls cannot be deleted (each uses at most 55 bytes toward `storage.used_bytes`), and the server places
    no limit on how many rolls a client creates. To roll scores and create the character in one step, omit
    `ability_roll_id` on `POST /v1/characters`.
    """
    seed = referee_seed(body.seed, principal)
    method = (body.ruleset or Ruleset()).ability_score_method
    rolled = roll_ability_scores(creation_stream(seed), method=method)
    row = _db(request).insert_ability_roll(
        principal.client,
        scores={ability.value: score for ability, score in rolled.scores.items()},
        seed=seed,
        ability_score_method=method,
    )
    return _ability_roll(row, rolled)


@router.get(
    "/ability-rolls/{roll_id}",
    tags=["characters"],
    summary="Get an ability roll",
    response_description="Stored ability roll, with `used` indicating whether a character already consumed it.",
    responses=problem_responses(
        {404: ["`not_found`: Your client has no ability roll with this id. `POST /v1/ability-rolls` creates one."]}
    ),
)
def get_ability_roll(
    request: Request,
    principal: CurrentPrincipal,
    roll_id: RollId,
) -> AbilityRoll:
    """Return a stored ability roll: its six `scores`, the dice in `rolls`, `eligible_class_ids`, and `used`.

    `ability_score_method` is the method the roll was made with, and the dice in `rolls` are the ones the roll's
    first response showed.

    Use this endpoint to resume character creation after a page reload. When `used` is `false`, pass `id` as
    `ability_roll_id` to `POST /v1/characters`. When `used` is `true`, the roll has already created a character and
    cannot be used again.
    """
    row = _db(request).get_ability_roll(principal.client, roll_id)
    if row is None:
        raise not_found("ability roll", roll_id)
    return _ability_roll(row)


_UNKNOWN_CLASS_OR_UNMET = (
    "`character_invalid`: osrlib has no class with this `class_id`, and the problem has `field` (`class_id`) and "
    "`id`. Or the roll's scores don't meet the class's minimum scores, and the problem has osrlib's `rejections`. "
    "Send a class `id` from the roll's `eligible_class_ids`."
)


@router.get(
    "/ability-rolls/{roll_id}/adjustment-limits",
    tags=["characters"],
    summary="Get the limits on an ability score trade",
    response_description="osrlib's `AdjustmentLimits` for the roll's scores as rolled and the class.",
    responses=problem_responses(
        {
            404: [
                "`not_found`: Your client has no ability roll with this id, including a roll that another client "
                "created. `POST /v1/ability-rolls` creates one."
            ],
            422: [VALIDATION_ERROR + " A request without `class_id` returns this code.", _UNKNOWN_CLASS_OR_UNMET],
        }
    ),
)
def get_adjustment_limits(
    request: Request,
    principal: CurrentPrincipal,
    roll_id: RollId,
    class_id: Annotated[
        str, Query(description="Character class `id` from the roll's `eligible_class_ids`, such as `fighter`.")
    ],
) -> AdjustmentLimits:
    """Return how far the `adjustment` trade can move each of this roll's ability scores for a class.

    The response is osrlib's `AdjustmentLimits`, unchanged. osrlib computes it from the roll's scores as rolled and
    from the class's prime requisites and `may_not_lower`, so it holds for every trade on this roll and class. Read it
    once, when the player picks a class, and use it to drive the adjustment step's controls. A trade that
    `POST /v1/characters` or `POST /v1/ability-rolls/{roll_id}/preview` refuses doesn't change it.

    Count the points the current trade buys: `raise_step` for each step of lowering. Then drive each ability's
    controls from its entry in `abilities`:

    - A lower control takes `lower_step` off the ability. Enable it while the current score is above `lowest` and the
      points bought are below `max_points`. When the score is at `lowest`, show `lowest_reason`. Otherwise show
      `max_points_reason`.
    - A raise control adds `raise_step` to the ability. Enable it while the current score is below `highest` and the
      trade has a point that it bought and hasn't spent. When the score is at `highest`, show `highest_reason`.
      Otherwise the player has no point to spend, which your client tracks itself and which has no code.
    - When `can_trade` is `false`, no trade changes a score. Disable the whole step and show `max_points_reason`.

    For a class with two prime requisites, each one's `highest` is its own limit, and `max_points` caps the points
    raised across both. The rules above keep the total within `max_points`, because a raise spends a point the trade
    bought and the lower controls stop buying at `max_points`.

    Each reason is a `Rejection`, with the `code` and `params` that the rejections of a 422 `character_invalid` have:

    - `lowest_reason`: `creation.adjustment.not_lowerable`, `creation.adjustment.prime_requisite_lowered`,
      `creation.adjustment.class_restriction`, `creation.adjustment.below_floor`, or the code of `max_points_reason`.
    - `highest_reason`: `creation.adjustment.raise_not_prime_requisite`, `creation.adjustment.above_cap`, or the code
      of `max_points_reason`.
    - `max_points_reason`: `creation.adjustment.no_room_to_raise` when the prime requisites have room for only
      `max_points` points, or `creation.adjustment.no_room_to_lower` when the abilities the class can lower pay for
      only that many. Both have the params `{"max_points": integer}`. These two codes appear only here, never in the
      `rejections` of a 422.

    `below_floor` and `above_cap` have the `ability`, and the `score` and `amount` of the first trade osrlib refuses:
    the rolled score, and the amount that takes it one step past the limit.

    The operation takes no `ruleset` and no other choice, and it works on a used roll, as
    `GET /v1/ability-rolls/{roll_id}` does. The server stores nothing. A `player` key and a `referee` key can both
    call this operation.
    """
    roll = _db(request).get_ability_roll(principal.client, roll_id)
    if roll is None:
        raise not_found("ability roll", roll_id)
    return class_adjustment_limits(scores_from_json(roll.scores), class_id)


@router.post(
    "/ability-rolls/{roll_id}/preview",
    tags=["characters"],
    summary="Preview a character from an ability roll",
    response_description="The character `POST /v1/characters` would store, its derived stats, and its dice.",
    responses=problem_responses(
        {
            404: [
                "`not_found`: Your client has no ability roll with this id, including a roll that another client "
                "created. `POST /v1/ability-rolls` creates one."
            ],
            409: [
                "`ability_roll_used`: A character was already created from this roll, so `POST /v1/characters` "
                "would refuse it too. Create a new roll with `POST /v1/ability-rolls`.",
                _RULESET_MISMATCH,
            ],
            422: [
                VALIDATION_ERROR + " A body with `seed` or `ability_roll_id` returns this code too, as does any member "
                "`POST /v1/characters` doesn't accept.",
                _CHARACTER_INVALID + " The roll stays unused, and `POST /v1/characters` refuses the same choices with "
                "the same problem.",
            ],
        }
    ),
)
def preview_character(
    request: Request, principal: CurrentPrincipal, roll_id: RollId, body: CharacterPreviewRequest
) -> CharacterPreview:
    """Return the character that `POST /v1/characters` would create from this ability roll and these choices.

    The response has the `character`, its `derived` stats, and every die osrlib rolled for it in `rolls`: the dice
    behind the roll's scores in `ability_rolls`, the hit point roll in `hit_point_roll`, and the starting gold roll in
    `gold_roll`. Use it to show a player their hit points and starting gold before they shop, and before the character
    exists. The roll stays unused. Without an `Idempotency-Key`, the server stores nothing and `storage.used_bytes`
    doesn't change. With one, the server stores the response for the key, as it does for every `POST`.

    Send the choices `POST /v1/characters` accepts, without `ability_roll_id`, because the path names the roll, and
    without `seed`. The preview and the creation run the same steps on the roll's private seed, so:

    - Every preview of one roll with the same choices returns the same response.
    - `POST /v1/characters` with this roll and the same choices stores a character equal to `character`, with the
      same hit points, gold, and inventory.
    - A choice that `POST /v1/characters` refuses gets the same 422 `character_invalid` here, with the same members,
      and the roll stays unused.

    For one roll, `gold_roll` changes only when `class_id` or the ruleset's `hp_reroll_at_first_level` changes, and
    `hit_point_roll` changes only with those two and the ruleset's `max_hp_at_first_level`. The `adjustment`,
    `starting_spell_ids`, `extra_languages`, `name`, `alignment`, `purchases`, and `equip_item_ids` change neither
    roll. So once the player has chosen a class under the table's ruleset, you can show the hit points and gold and
    let the player make every other choice without the dice changing. A different class or reroll option can change
    the gold as well as the hit points.

    For the limits on the `adjustment`, call `GET /v1/ability-rolls/{roll_id}/adjustment-limits` with the class. It
    takes no other choice, so it answers before the player has chosen the starting spells that a preview of a
    `magic_user` or an `elf` requires.

    Send the `ruleset` you sent to `POST /v1/ability-rolls` for this roll. A ruleset with another
    `ability_score_method` than the roll's gets 409 `ability_roll_ruleset_mismatch`, and so does a request with no
    `ruleset` for a roll made with `4d6_drop_lowest`.

    The response has no `seed`, because the roll's seed stays private, and no `id`, because the server stores no
    character. `character.id` is `null`. A `player` key and a `referee` key can both call this operation.
    """
    roll = _unused_roll(_db(request), principal.client, roll_id)
    method = ability_score_method(roll.ability_score_method)
    previewed = character_from_roll(body, roll.seed, scores_from_json(roll.scores), method)
    return CharacterPreview(character=previewed.character, derived=_derived(previewed.character), rolls=previewed.rolls)


@router.post(
    "/characters",
    status_code=201,
    tags=["characters"],
    summary="Create a character",
    response_description="Stored character with its new `chr_` id and derived stats.",
    responses=problem_responses(
        {
            403: [_SEED_FORBIDDEN],
            404: ["`not_found`: Your client has no ability roll with the id in `ability_roll_id`."],
            409: [
                "`ability_roll_used`: A character was already created from `ability_roll_id`. Deleting that "
                "character doesn't reset the roll. Create a new roll with `POST /v1/ability-rolls`.",
                _RULESET_MISMATCH,
            ],
            422: [
                VALIDATION_ERROR + " Sending both `seed` and `ability_roll_id` returns this code too.",
                _CHARACTER_INVALID + " Any `ability_roll_id` in the request remains unused for a retry.",
            ],
        }
    ),
)
def create_character(request: Request, principal: CurrentPrincipal, body: CharacterRequest) -> StoredCharacter:
    """Create and store a first-level character from the player's choices.

    Pass the returned `id` in `character_ids` to `POST /v1/sessions` to start a game. Sessions copy their party
    members at start, so play never mutates the stored character and one character can enter any number of sessions.

    When `ability_roll_id` (from `POST /v1/ability-rolls`) is provided, the character uses those scores, draws hit
    points and starting gold from that roll's seed, and marks the roll `used` in the same transaction. Without
    `ability_roll_id`, the server rolls ability scores first, with the `ability_score_method` of `ruleset`. The
    ruleset's `hp_reroll_at_first_level` and `max_hp_at_first_level` decide the hit point roll either way. With an
    `ability_roll_id`, send the `ruleset` you sent for the roll, because another `ability_score_method` than the
    roll's gets 409 `ability_roll_ruleset_mismatch`. osrlib then validates and applies choices in order:
    class requirements, ability `adjustment`, `starting_spell_ids`, hit point roll, `extra_languages`, starting gold
    roll, `purchases`, and `equip_item_ids`. Creation stops at the first invalid step with 422 `character_invalid`
    and stores nothing.

    Look up catalog ids via `GET /v1/rules/classes`, `GET /v1/rules/spells`, `GET /v1/rules/languages`, and
    `GET /v1/rules/equipment`. To show the player the hit points and starting gold of an ability roll before you
    create the character, call `POST /v1/ability-rolls/{roll_id}/preview` with the same choices. To roll a character
    from new scores without storing anything, call `POST /v1/generate/character`.
    """
    db = _db(request)
    if body.ability_roll_id is None:
        seed = referee_seed(body.seed, principal)
        return _store(db, principal.client, roll_character(body, seed).character)
    roll = _unused_roll(db, principal.client, body.ability_roll_id)
    method = ability_score_method(roll.ability_score_method)
    created = character_from_roll(body, roll.seed, scores_from_json(roll.scores), method)
    return _store(db, principal.client, created.character, body.ability_roll_id)


@router.post(
    "/characters/import",
    status_code=201,
    tags=["characters"],
    summary="Import a character document",
    response_description="Stored character with a newly assigned `chr_` id.",
    responses=problem_responses(
        {
            403: [_REFEREE_ONLY],
            409: [_VERSION_UNSUPPORTED],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: The body isn't a valid character document, osrlib refuses the character or "
                "cannot compute derived stats (such as an unknown item id), `stat_modifiers` is non-empty, a "
                "condition has an `effect_id`, `max_hp` exceeds 2^63 - 1, or `name` contains a control character. "
                "Check `detail` and fix the document.",
            ],
        }
    ),
)
@accepts_document
def import_character(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[
        dict[str, Any],
        Body(
            description='Character document envelope (`kind` set to `"character"`, `schema_version`, '
            "`engine_version`, and `payload`), as written by osrlib's `Character.to_document`."
        ),
    ],
) -> StoredCharacter:
    """Requires a `referee` API key. Validate and store a character from an osrlib character document.

    Pass the returned `id` in `character_ids` to `POST /v1/sessions`. Use this operation to migrate characters
    between servers, import characters built with osrlib, or store characters above level 1 that
    `POST /v1/characters` doesn't create.

    Send an envelope object with `kind` set to `"character"`, `schema_version` (at most the `schema_version` from
    `GET /v1/meta`), `engine_version`, and an osrlib `Character` in `payload` (matching `character` on
    `GET /v1/characters/{character_id}`). The request body may be up to `BXAPI_MAX_BODY_BYTES` (8,388,608 bytes by
    default). The server clears `payload.id` so each session assigns a fresh party-member id, and assigns a new
    `chr_` id to the stored record.

    Refuses characters with `stat_modifiers` or conditions with an `effect_id` (422 `content_invalid`), because
    those reference active effects from another session's ledger. The server accepts permanent conditions with
    `effect_id` set to `null` (such as `dead`). Also refuses `max_hp` above 2^63 - 1 or a `name` containing control
    characters.
    """
    character = Character.from_document(document)
    if character.stat_modifiers or any(condition.effect_id is not None for condition in character.conditions):
        raise invalid(
            "content_invalid",
            "The character has conditions or stat modifiers from effects in another game. Remove `stat_modifiers` and "
            "each condition with an `effect_id` before you import it.",
        )
    if not fits_integer_column(character.max_hp):
        raise invalid("content_invalid", "The character's max_hp is larger than the server can store.")
    if has_control_character(character.name):
        raise invalid("content_invalid", "The character's name has a control character.")
    character.id = None
    return _store(_db(request), principal.client, character)


@router.get(
    "/characters",
    tags=["characters"],
    summary="List the stored characters",
    response_description="A page of character summaries, newest first.",
)
def list_characters(request: Request, principal: CurrentPrincipal, page: PageQuery) -> Page[CharacterSummary]:
    """Return a page of your client's stored characters, ordered newest first.

    Use this list to populate a party-selection screen before calling `POST /v1/sessions`. Each item contains summary
    fields (`id`, `name`, `class_id`, `level`, `max_hp`, `created_at`). Call `GET /v1/characters/{character_id}` for
    full ability scores, inventory, spells, and derived stats.
    """
    rows = _db(request).list_characters(principal.client, limit=page.limit, offset=page.offset)
    items = [
        CharacterSummary(
            id=row.id,
            name=row.name,
            class_id=row.class_id,
            level=row.level,
            max_hp=row.max_hp,
            created_at=row.created_at,
        )
        for row in rows.items
    ]
    return Page(items=items, total=rows.total, limit=page.limit, offset=page.offset)


@router.get(
    "/characters/{character_id}",
    tags=["characters"],
    summary="Get a stored character",
    response_description="Stored character with computed `derived` values.",
    responses=problem_responses(
        {404: ["`not_found`: Your client has no character with this id. `GET /v1/characters` lists your characters."]}
    ),
)
def get_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> StoredCharacter:
    """Return one stored character's full osrlib `Character` model and computed `derived` values.

    `character` contains scores, hit points, inventory, spells, and conditions, while `derived` contains computed
    values such as `thac0`, `armour_class`, `saves`, and ability modifiers (`derived` omits `movement_rate_feet`
    because it depends on a session's `ruleset`). Because sessions copy characters when starting, this record never
    reflects in-session changes. Read `GET /v1/sessions/{session_id}/view` for a party member's state during play.
    """
    row = _db(request).get_character(principal.client, character_id)
    if row is None:
        raise not_found("character", character_id)
    character = Character.from_document(row.document)
    return _stored(row, character, _derived(character))


@router.delete(
    "/characters/{character_id}",
    status_code=204,
    tags=["characters"],
    summary="Delete a stored character",
    response_description="The server deleted the character. The response has no body.",
    responses=problem_responses({404: ["`not_found`: Your client has no character with this id."]}),
)
def delete_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> Response:
    """Delete a stored character and reclaim its bytes in your client's storage quota (`GET /v1/meta`).

    Existing sessions that started with this character use their copies and continue unaffected. Deleting a
    character does not reset the `used` flag on the ability roll it came from, and repeating a completed delete
    returns 404 `not_found`.
    """
    if not _db(request).delete_character(principal.client, character_id):
        raise not_found("character", character_id)
    return Response(status_code=204)


@router.post(
    "/generate/character",
    tags=["generate"],
    summary="Roll a character without storing it",
    response_description="Generated character, derived stats, creation rolls, and the seed used.",
    responses=problem_responses({422: [VALIDATION_ERROR, _CHARACTER_INVALID]}),
)
def generate_character(body: GenerateCharacterRequest) -> GeneratedCharacterResponse:
    """Roll an unstored first-level character with osrlib from the same choices `POST /v1/characters` accepts.

    osrlib rolls the six ability scores and applies your class, adjustment, spell, language, and equipment choices in
    order, returning the `character`, `derived` stats, and every individual die in `rolls` (`ability_rolls`,
    `hit_point_roll`, and `gold_roll`). The `ruleset` decides how osrlib rolls the scores and the hit points. Both
    `player` and `referee` keys may pass `seed` to reproduce a character, or omit it to draw a new seed.

    Because the character isn't stored, it has `id` set to `null` and cannot be passed in `character_ids` to
    `POST /v1/sessions`. To store a character for play, call `POST /v1/characters` instead. This operation rolls new
    scores and takes no `ability_roll_id`. To see the character a stored ability roll would create, call
    `POST /v1/ability-rolls/{roll_id}/preview`.
    """
    seed = seed_or_draw(body.seed)
    generated = roll_character(body, seed)
    return GeneratedCharacterResponse(
        seed=seed, character=generated.character, derived=_derived(generated.character), rolls=generated.rolls
    )
