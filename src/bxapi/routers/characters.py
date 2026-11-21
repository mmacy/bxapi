"""Stored characters, the ability score rolls that a character starts from, and the stateless character generator."""

from typing import Annotated, Any, Self

from fastapi import APIRouter, Body, Path, Request, Response
from osrlib.core.abilities import AbilityScore
from osrlib.core.character import Character, roll_ability_scores
from pydantic import model_validator

from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.creation import (
    CharacterChoices,
    CreationRolls,
    DerivedValues,
    character_from_roll,
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
    "CharacterRequest",
    "CharacterSummary",
    "GenerateCharacterRequest",
    "GeneratedCharacterResponse",
    "StoredCharacter",
    "router",
]

router = APIRouter(route_class=IdempotentRoute)

_SEED_FORBIDDEN = (
    "`referee_required`: The request used a player key and sent `seed`. Omit `seed` so that the server generates "
    "one, or send the request with a referee key."
)
_CHARACTER_INVALID = (
    "`character_invalid`: A choice breaks a rule, such as a class the scores don't qualify for, a missing first spell "
    "for a magic-user, or purchases that cost more than the starting gold. `rejections` lists osrlib's reasons, each "
    "with a `code` and its `params`. For a class or item ID that osrlib doesn't have, or an `equip_item_ids` entry "
    "that isn't in `purchases`, the problem document instead has `field`, `id`, and, for a list field, `index`. "
    "Change the choice and send the request again."
)
_REFEREE_ONLY = "`referee_required`: Only a referee may call this operation. Use a referee key."
_VERSION_UNSUPPORTED = (
    "`version_unsupported`: The document's `schema_version` is higher than the version the server's osrlib reads, "
    "which `GET /v1/meta` returns as `schema_version`. Import the document on a server with a newer osrlib."
)

RollId = Annotated[str, Path(description="The ability roll's ID, which starts with `rol_`.")]
CharacterId = Annotated[str, Path(description="The character's ID, which starts with `chr_`.")]
"""The character routes. The router sets no tag, because the character generator is in the
`generate` group of the OpenAPI document and every other route is in `characters`."""


class AbilityRollRequest(RequestModel):
    """The optional body of a request for an ability roll."""

    seed: Seed | None = None
    """The seed for the roll. Only a referee may pass one, and the server never returns it. The seed also determines
    the hit point and gold rolls of the character created from the roll."""


class AbilityRoll(ApiModel):
    """Six ability scores that the server rolled and stored, and the classes they qualify for."""

    id: str
    """The roll's ID, which starts with `rol_`. Pass it as `ability_roll_id` to create a character from these
    scores."""
    scores: dict[AbilityScore, int]
    """The six scores, each from 3 to 18, as osrlib rolled them: 3d6 for each ability."""
    eligible_class_ids: list[str]
    """The IDs of the classes whose minimum scores these scores meet, according to osrlib's class check."""
    used: bool
    """Whether a character was created from the roll. You can create at most one character from a roll, and deleting
    the character doesn't free the roll."""
    created_at: str
    """When the server stored the roll."""


class CharacterRequest(CharacterChoices):
    """The choices for a new stored character."""

    ability_roll_id: str | None = None
    """The ID of an unused ability roll to take the scores from. The roll's seed also determines the hit point and
    gold rolls. Without it, the server rolls the scores as part of the request."""
    seed: Seed | None = None
    """The seed for the creation rolls, for a request with no `ability_roll_id`. Only a referee may pass one, and
    the server never returns it."""

    @model_validator(mode="after")
    def _one_source_of_dice(self) -> Self:
        if self.ability_roll_id is not None and self.seed is not None:
            raise ValueError("pass seed or ability_roll_id, not both: an ability roll already has a seed")
        return self


class GenerateCharacterRequest(CharacterChoices):
    """The choices for a character that the server rolls but doesn't store."""

    seed: Seed | None = None
    """The seed for the creation rolls. Omit it to have the server generate one."""


class StoredCharacter(ApiModel):
    """A character that the server stored."""

    id: str
    """The character's ID, which starts with `chr_`."""
    name: str
    """The character's name."""
    class_id: str
    """The ID of the character's class."""
    level: int
    """The character's level."""
    created_at: str
    """When the server stored the character."""
    character: Character
    """osrlib's `Character` model."""
    derived: DerivedValues
    """The values that osrlib computes from the character."""


class CharacterSummary(ApiModel):
    """The fields that a list of stored characters shows."""

    id: str
    """The character's ID, which starts with `chr_`."""
    name: str
    """The character's name."""
    class_id: str
    """The ID of the character's class."""
    level: int
    """The character's level."""
    max_hp: int
    """The character's maximum hit points."""
    created_at: str
    """When the server stored the character."""


class GeneratedCharacterResponse(ApiModel):
    """A character that the server rolled but didn't store, and the seed to roll it again."""

    seed: Seed
    """The seed the server used. Send it with the same choices to get the same character."""
    character: Character
    """osrlib's `Character` model."""
    derived: DerivedValues
    """The values that osrlib computes from the character."""
    rolls: CreationRolls
    """The dice that osrlib rolled to create the character."""


def _db(request: Request) -> Database:
    return request.app.state.db


def _ability_roll(row: AbilityRollRow) -> AbilityRoll:
    scores = scores_from_json(row.scores)
    return AbilityRoll(
        id=row.id,
        scores=scores,
        eligible_class_ids=eligible_class_ids(scores),
        used=row.used,
        created_at=row.created_at,
    )


def _roll_used(roll_id: str) -> ApiError:
    return conflict("ability_roll_used", f"A character was already created from the ability roll {roll_id!r}.")


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
    response_description="The stored roll: its ID, the six scores, and the classes the scores qualify for.",
    responses=problem_responses({403: [_SEED_FORBIDDEN]}),
)
def create_ability_roll(
    request: Request, principal: CurrentPrincipal, body: AbilityRollRequest | None = None
) -> AbilityRoll:
    """Roll the six ability scores with osrlib, store them, and return them with the classes they qualify for.

    Show the player `scores` and `eligible_class_ids`, then pass the roll's `id` as `ability_roll_id` to
    `POST /v1/characters` with the player's choices. The request body is optional, and a request with a player key
    sends none.

    The server rolls the scores from a seed, the integer that determines every random number osrlib generates, and
    stores the seed with the roll. The character created from the roll gets its hit point and gold rolls from the
    same seed. Every attempt to create a character from one roll, such as a retry after osrlib rejects a choice,
    therefore gets the same rolls. The server never returns the seed.

    The server doesn't limit how many rolls a client requests, so a game that allows one roll per character must
    enforce that rule itself. To create a character with scores that the server rolls in the same request, omit
    `ability_roll_id` from `POST /v1/characters` and skip this operation.
    """
    seed = referee_seed(body.seed if body is not None else None, principal)
    rolled = roll_ability_scores(creation_stream(seed))
    row = _db(request).insert_ability_roll(
        principal.client, scores={ability.value: score for ability, score in rolled.scores.items()}, seed=seed
    )
    return _ability_roll(row)


@router.get(
    "/ability-rolls/{roll_id}",
    tags=["characters"],
    summary="Get an ability roll",
    response_description="The roll, with `used` set to whether a character was created from it.",
    responses=problem_responses(
        {404: ["`not_found`: Your client has no ability roll with this ID. `POST /v1/ability-rolls` creates one."]}
    ),
)
def get_ability_roll(
    request: Request,
    principal: CurrentPrincipal,
    roll_id: RollId,
) -> AbilityRoll:
    """Return an ability roll, with `used` set to whether a character was created from it.

    Use it to show the scores again, for example after the player reloads a character creation screen. A roll with
    `used` set to `true` can't create another character.
    """
    row = _db(request).get_ability_roll(principal.client, roll_id)
    if row is None:
        raise not_found("ability roll", roll_id)
    return _ability_roll(row)


@router.post(
    "/characters",
    status_code=201,
    tags=["characters"],
    summary="Create a character",
    response_description="The stored character, with its ID and the values osrlib computes from it.",
    responses=problem_responses(
        {
            403: [_SEED_FORBIDDEN],
            404: ["`not_found`: Your client has no ability roll with the ID in `ability_roll_id`."],
            409: [
                "`ability_roll_used`: A character was already created from the ability roll. Deleting that "
                "character doesn't free the roll. Roll again with `POST /v1/ability-rolls`."
            ],
            422: [
                VALIDATION_ERROR + " The server also returns this code for a request with both `seed` and "
                "`ability_roll_id`.",
                _CHARACTER_INVALID + " The ability roll stays unused, so you can send the same `ability_roll_id` "
                "again.",
            ],
        }
    ),
)
def create_character(request: Request, principal: CurrentPrincipal, body: CharacterRequest) -> StoredCharacter:
    """Create a first-level character from the player's choices, and store it.

    To play the character, pass its `id` in `character_ids` to `POST /v1/sessions`. A session plays a copy, so play
    never changes the stored character.

    With an `ability_roll_id` from `POST /v1/ability-rolls`, the character gets that roll's scores, its hit points
    and gold come from the roll's seed, and the roll becomes used. Without one, the server rolls the scores first.
    osrlib then checks the choices in the request and rolls the dice in this order: it checks the class, checks and
    applies the ability adjustment, checks the first spells, rolls hit points, checks the extra languages, rolls
    starting gold, and buys and equips the gear. Creation stops at the first choice that osrlib rejects, and the
    server stores nothing.

    The IDs in the body come from the rules reference: `class_id` from `GET /v1/rules/classes`, `starting_spell_ids`
    from `GET /v1/rules/spells`, `extra_languages` from `GET /v1/rules/languages`, and each `item_id` from
    `GET /v1/rules/equipment`. To show the player the dice before you store anything, use
    `POST /v1/generate/character`, which stores nothing.
    """
    db = _db(request)
    if body.ability_roll_id is None:
        seed = referee_seed(body.seed, principal)
        return _store(db, principal.client, roll_character(body, seed).character)
    roll = db.get_ability_roll(principal.client, body.ability_roll_id)
    if roll is None:
        raise not_found("ability roll", body.ability_roll_id)
    if roll.used:
        raise _roll_used(body.ability_roll_id)
    character = character_from_roll(body, roll.seed, scores_from_json(roll.scores))
    return _store(db, principal.client, character, body.ability_roll_id)


@router.post(
    "/characters/import",
    status_code=201,
    tags=["characters"],
    summary="Import a character document",
    response_description="The stored character, with a new ID.",
    responses=problem_responses(
        {
            403: [_REFEREE_ONLY],
            409: [_VERSION_UNSUPPORTED],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: The body isn't a character document, osrlib rejects the character, the character "
                "refers to an item that osrlib doesn't have, or the character has a stat modifier or a condition "
                "with an `effect_id`. `detail` identifies the cause. Fix the document.",
            ],
        }
    ),
)
@accepts_document
def import_character(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[dict[str, Any], Body(description="A character document, as osrlib's `to_document` writes it.")],
) -> StoredCharacter:
    """Store a character from a character document, the format that osrlib's `Character.to_document` writes.

    Only a referee may import a character. Use it to bring in a character from another server or from a program that
    uses osrlib, or to store a character that `POST /v1/characters` can't create, such as a fighter at level 14. The
    server accepts any character that osrlib's `Character` model accepts, with the three exceptions listed below.

    A character document is an object with `kind` set to `character`, a `schema_version`, an `engine_version`, and
    the character in `payload`. The `character` of a stored character, from `GET /v1/characters/{character_id}`, is
    a valid `payload`. Take `schema_version` from `GET /v1/meta`.

    The server clears the `id` field of the character in the document, because osrlib gives each party member
    without an ID a new one when a session starts. The stored character gets a new `chr_` ID.

    The server returns `422 content_invalid` for three kinds of character that osrlib's model accepts:

    - A character with a stat modifier, or with a condition that has an `effect_id`. When an effect grants a
      character a condition or a stat modifier, the condition or modifier refers to the effect by its `effect_id`,
      and the effect belongs to the game the character came from. In a new session, the condition would never end,
      and the modifier would end with an unrelated effect that has the same ID. The server accepts a condition with
      no `effect_id`, such as `dead`.
    - A character whose `max_hp` exceeds 2^63 - 1, the largest integer the server's database stores.
    - A character whose `name` contains a control character, such as a tab or a newline.
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
    """Return a page of your client's characters, newest first.

    Use it to build a character picker before you start a session. Each summary has the fields that a list shows.
    For the whole character, with its scores, inventory, and the values osrlib computes, use
    `GET /v1/characters/{character_id}`.
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
    response_description="The stored character, with the values osrlib computes from it.",
    responses=problem_responses({404: ["`not_found`: Your client has no character with this ID."]}),
)
def get_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> StoredCharacter:
    """Return a stored character, with the values osrlib derives from it.

    `character` is osrlib's `Character` model: the scores, hit points, inventory, spells, and conditions. `derived`
    has the values that osrlib computes from the character but doesn't store, such as `thac0`, `armour_class`, and
    `saves`. The stored character stays as it was created. To see a character as it is in a game, read the session's
    `GET /v1/sessions/{session_id}/view`.
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
    responses=problem_responses({404: ["`not_found`: Your client has no character with this ID."]}),
)
def delete_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> Response:
    """Delete a stored character. A session that started with the character has its own copy and isn't affected.

    Deleting the character doesn't free the ability roll it was created from.
    """
    if not _db(request).delete_character(principal.client, character_id):
        raise not_found("character", character_id)
    return Response(status_code=204)


@router.post(
    "/generate/character",
    tags=["generate"],
    summary="Roll a character without storing it",
    response_description="The character, the values osrlib computes from it, every die rolled, and the seed.",
    responses=problem_responses({422: [VALIDATION_ERROR, _CHARACTER_INVALID]}),
)
def generate_character(body: GenerateCharacterRequest) -> GeneratedCharacterResponse:
    """Roll a first-level character from the choices that `POST /v1/characters` accepts, and store nothing.

    The server rolls the ability scores, then applies the choices with the same osrlib steps as
    `POST /v1/characters`. `rolls` has every die: the three dice of each ability score, the hit point roll, and the
    gold roll. Send the same body with the returned seed to get the same character again. A key of either role may
    pass a seed.

    Use it for a character generator, or for an NPC that a referee needs immediately. The result has no ID, so you
    can't pass it in `character_ids` to start a session. To store a character for play, use `POST /v1/characters`.
    """
    seed = seed_or_draw(body.seed)
    generated = roll_character(body, seed)
    return GeneratedCharacterResponse(
        seed=seed, character=generated.character, derived=_derived(generated.character), rolls=generated.rolls
    )
