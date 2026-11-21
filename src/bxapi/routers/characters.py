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
from bxapi.db import AbilityRollRow, CharacterDocumentRow, Database
from bxapi.errors import ApiError, conflict, invalid, not_found, problem_responses
from bxapi.models import ApiModel, RequestModel
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

router = APIRouter()

RollId = Annotated[str, Path(description="The ability roll's id, which starts with `rol_`.")]
CharacterId = Annotated[str, Path(description="The character's id, which starts with `chr_`.")]
"""The character routes. The router sets no tag, because the character generator is in the
`generate` group of the OpenAPI document and every other route is in `characters`."""


class AbilityRollRequest(RequestModel):
    """The body of a request for an ability roll."""

    seed: Seed | None = None
    """The seed for the roll. Only a referee may pass one, and the server never returns the seed. It
    also determines the hit point and gold rolls of the character created from the roll."""


class AbilityRoll(ApiModel):
    """Six ability scores the server rolled and stored, and the classes they qualify for."""

    id: str
    """The roll's id, which starts with `rol_`. Pass it as `ability_roll_id` to create a character
    from these scores."""
    scores: dict[AbilityScore, int]
    """The six scores, each from 3 to 18, as osrlib rolled them: 3d6 for each ability."""
    eligible_class_ids: list[str]
    """The ids of the classes whose minimum scores these scores meet, from osrlib's class check."""
    used: bool
    """Whether a character was created from the roll. You can create at most one character from a
    roll, and deleting the character doesn't free the roll."""
    created_at: str
    """When the server stored the roll."""


class CharacterRequest(CharacterChoices):
    """The choices for a new stored character."""

    ability_roll_id: str | None = None
    """The id of an unused ability roll to take the scores from. The roll's seed also determines the
    hit point and gold rolls. Without it, the server rolls the scores as part of the request."""
    seed: Seed | None = None
    """The seed for the creation rolls, when the request has no `ability_roll_id`. Only a referee
    may pass one, and the server never returns it."""

    @model_validator(mode="after")
    def _one_source_of_dice(self) -> Self:
        if self.ability_roll_id is not None and self.seed is not None:
            raise ValueError("pass seed or ability_roll_id, not both: an ability roll already has a seed")
        return self


class GenerateCharacterRequest(CharacterChoices):
    """The choices for a character the server rolls and doesn't store."""

    seed: Seed | None = None
    """The seed for the creation rolls. Leave it out to have the server draw one."""


class StoredCharacter(ApiModel):
    """A character the server stored."""

    id: str
    """The character's id, which starts with `chr_`."""
    name: str
    """The character's name."""
    class_id: str
    """The character's class."""
    level: int
    """The character's level."""
    created_at: str
    """When the server stored the character."""
    character: Character
    """osrlib's `Character` model."""
    derived: DerivedValues
    """The values osrlib computes from the character."""


class CharacterSummary(ApiModel):
    """The fields a list of stored characters shows."""

    id: str
    """The character's id, which starts with `chr_`."""
    name: str
    """The character's name."""
    class_id: str
    """The character's class."""
    level: int
    """The character's level."""
    max_hp: int
    """The character's maximum hit points."""
    created_at: str
    """When the server stored the character."""


class GeneratedCharacterResponse(ApiModel):
    """A character the server rolled and didn't store, and the seed to roll it again."""

    seed: Seed
    """The seed the server used. Send it with the same choices to get the same character."""
    character: Character
    """osrlib's `Character` model."""
    derived: DerivedValues
    """The values osrlib computes from the character."""
    rolls: CreationRolls
    """The dice osrlib rolled to create the character."""


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
    responses=problem_responses(403),
)
def create_ability_roll(
    request: Request, principal: CurrentPrincipal, body: AbilityRollRequest | None = None
) -> AbilityRoll:
    """Roll the six ability scores with osrlib, store them, and return them with the classes they qualify for.

    Show the player the scores and `eligible_class_ids`, then pass the roll's `id` as
    `ability_roll_id` to `POST /v1/characters`. The request body of this endpoint is optional.

    The server stores the roll's seed with it, and the character created from the roll gets
    its hit point and gold rolls from the same seed. So every attempt to create a character
    from one roll, like a retry after osrlib refuses a choice, gets the same rolls. The server
    doesn't limit how many rolls a client asks for.

    A player who passes `seed` gets 403 `referee_required`.
    """
    seed = referee_seed(body.seed if body is not None else None, principal)
    rolled = roll_ability_scores(creation_stream(seed))
    row = _db(request).insert_ability_roll(
        principal.client, scores={ability.value: score for ability, score in rolled.scores.items()}, seed=seed
    )
    return _ability_roll(row)


@router.get(
    "/ability-rolls/{roll_id}", tags=["characters"], summary="Get an ability roll", responses=problem_responses(404)
)
def get_ability_roll(
    request: Request,
    principal: CurrentPrincipal,
    roll_id: RollId,
) -> AbilityRoll:
    """Return an ability roll, with `used` set to whether a character was created from it."""
    row = _db(request).get_ability_roll(principal.client, roll_id)
    if row is None:
        raise not_found("ability roll", roll_id)
    return _ability_roll(row)


@router.post(
    "/characters",
    status_code=201,
    tags=["characters"],
    summary="Create a character",
    responses=problem_responses(403, 404, 409),
)
def create_character(request: Request, principal: CurrentPrincipal, body: CharacterRequest) -> StoredCharacter:
    """Create a first-level character from the player's choices, and store it.

    With `ability_roll_id`, the character gets that roll's scores, its hit points and gold come
    from the roll's seed, and the roll becomes used. Without it, the server rolls the scores
    first. Then osrlib checks the choices in the request and rolls the dice, in this order: it
    checks the class, checks and applies the ability adjustment, checks the first spells, rolls
    hit points, checks the extra languages, rolls starting gold, and buys and equips the gear.

    When a choice breaks a rule, the response is 422 `character_invalid`, with osrlib's
    `rejections` when osrlib reports them, and the ability roll stays unused. If a character was
    already created from the roll, the response is 409 `ability_roll_used`. An unknown roll id gets 404
    `not_found`. A player who
    passes `seed` gets 403 `referee_required`, and a request with both `seed` and
    `ability_roll_id` gets 422 `validation_error`.
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
    responses=problem_responses(403, 409),
)
def import_character(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[dict[str, Any], Body(description="A character document, as osrlib's `to_document` writes it.")],
) -> StoredCharacter:
    """Store a character from a character document, the format osrlib's `Character.to_document` writes.

    The server accepts any character osrlib's `Character` model accepts, like a fighter at level
    14. It clears the `id` field of the character in the document, because osrlib gives each party
    member without an id a new one when a session starts. The stored character gets a new `chr_` id.

    When an effect grants a character a condition or a stat modifier, the condition or modifier
    refers to the effect by its `effect_id`, and the effect belongs to the game the character
    came from. In a new session the condition would never end, and the modifier would end with
    an unrelated effect that has the same id. So a character with a stat modifier, or with a
    condition that has an `effect_id`, gets 422 `content_invalid`. A condition with no
    `effect_id`, like `dead`, is accepted.

    A document that isn't a valid character document, or that refers to an item osrlib doesn't
    have, gets 422 `content_invalid`. A document stamped with a newer schema version than the
    server's osrlib reads gets 409 `version_unsupported`. Only a referee may import a character.
    """
    character = Character.from_document(document)
    if character.stat_modifiers or any(condition.effect_id is not None for condition in character.conditions):
        raise invalid(
            "content_invalid",
            "The character has conditions or stat modifiers from effects in another game. Remove `stat_modifiers` and "
            "each condition with an `effect_id` before you import it.",
        )
    character.id = None
    return _store(_db(request), principal.client, character)


@router.get("/characters", tags=["characters"], summary="List the stored characters")
def list_characters(request: Request, principal: CurrentPrincipal, page: PageQuery) -> Page[CharacterSummary]:
    """Return a page of the client's characters, newest first."""
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
    responses=problem_responses(404),
)
def get_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> StoredCharacter:
    """Return a stored character, with the values osrlib derives from it."""
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
    responses=problem_responses(404),
)
def delete_character(request: Request, principal: CurrentPrincipal, character_id: CharacterId) -> Response:
    """Delete a stored character. A session that started with the character has a copy and isn't affected."""
    if not _db(request).delete_character(principal.client, character_id):
        raise not_found("character", character_id)
    return Response(status_code=204)


@router.post("/generate/character", tags=["generate"], summary="Roll a character without storing it")
def generate_character(body: GenerateCharacterRequest) -> GeneratedCharacterResponse:
    """Roll a first-level character from the choices `POST /v1/characters` accepts, and store nothing.

    The server rolls the ability scores, then applies the choices with the same osrlib steps as
    `POST /v1/characters`. Send the same body with the returned seed to get the same character
    again. A choice that breaks a rule gets 422 `character_invalid`, with osrlib's `rejections`
    when osrlib reports them.
    """
    seed = seed_or_draw(body.seed)
    generated = roll_character(body, seed)
    return GeneratedCharacterResponse(
        seed=seed, character=generated.character, derived=_derived(generated.character), rolls=generated.rolls
    )
