"""Stored characters, the ability score rolls that start one, and the stateless character generator."""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request, Response
from osrlib.core.abilities import AbilityScore
from osrlib.core.character import Character, roll_ability_scores

from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.creation import (
    CharacterChoices,
    CreationRolls,
    DerivedValues,
    character_from_scores,
    creation_stream,
    derive,
    eligible_class_ids,
    roll_character,
    scores_from_json,
)
from bxapi.db import AbilityRollRow, CharacterDocumentRow, Database
from bxapi.errors import ApiError, conflict, not_found, problem_responses
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
"""The character routes. The router sets no tag, because the character generator is in the
`generate` group of the OpenAPI document and every other route is in `characters`."""


class AbilityRollRequest(RequestModel):
    """The body of a request for an ability roll."""

    seed: Seed | None = None
    """The seed for the roll. Only a referee may pass one, and the server never returns it."""


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
    """Whether a character was created from the roll. A roll starts one character at most."""
    created_at: str
    """When the server stored the roll."""


class CharacterRequest(CharacterChoices):
    """The choices for a new stored character."""

    ability_roll_id: str | None = None
    """The id of an unused ability roll to take the scores from. Without it, the server rolls the
    scores as part of the request."""
    seed: Seed | None = None
    """The seed for the creation rolls. Only a referee may pass one, and the server never
    returns it."""


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


def _stored(row: CharacterDocumentRow, character: Character) -> StoredCharacter:
    return StoredCharacter(
        id=row.id,
        name=row.name,
        class_id=row.class_id,
        level=row.level,
        created_at=row.created_at,
        character=character,
        derived=derive(character),
    )


def _roll_used(roll_id: str) -> ApiError:
    return conflict("ability_roll_used", f"A character was already created from the ability roll {roll_id!r}.")


def _store(db: Database, client: str, character: Character, roll_id: str | None = None) -> StoredCharacter:
    name, class_id, level, max_hp = character.name, character.class_id, character.level, character.max_hp
    document = character.to_document()
    if roll_id is None:
        row = db.insert_character(client, name=name, class_id=class_id, level=level, max_hp=max_hp, document=document)
        return _stored(row, character)
    created = db.insert_character_from_ability_roll(
        client, roll_id, name=name, class_id=class_id, level=level, max_hp=max_hp, document=document
    )
    if created is None:
        if db.get_ability_roll(client, roll_id) is None:
            raise not_found("ability roll", roll_id)
        raise _roll_used(roll_id)
    return _stored(created, character)


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
    `ability_roll_id` to `POST /v1/characters`. The body is optional.

    A player who passes `seed` gets 403 `referee_required`.
    """
    seed = referee_seed(body.seed if body is not None else None, principal)
    rolled = roll_ability_scores(creation_stream(seed))
    row = _db(request).insert_ability_roll(
        principal.client, scores={ability.value: score for ability, score in rolled.scores.items()}
    )
    return _ability_roll(row)


@router.get(
    "/ability-rolls/{roll_id}", tags=["characters"], summary="Get an ability roll", responses=problem_responses(404)
)
def get_ability_roll(request: Request, principal: CurrentPrincipal, roll_id: str) -> AbilityRoll:
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

    With `ability_roll_id`, the character gets that roll's scores, and the roll becomes used.
    Without it, the server rolls the scores first. Then osrlib checks the class, applies the
    ability adjustment, checks the first spells, rolls hit points, checks the extra languages,
    rolls starting gold, and buys and equips the gear, in that order.

    When a choice breaks a rule, the response is 422 `character_invalid`, with osrlib's
    `rejections` when it reports them, and the ability roll stays unused. A roll that already
    started a character gets 409 `ability_roll_used`. An unknown roll id gets 404. A player who
    passes `seed` gets 403 `referee_required`.
    """
    seed = referee_seed(body.seed, principal)
    db = _db(request)
    stream = creation_stream(seed)
    if body.ability_roll_id is None:
        return _store(db, principal.client, roll_character(body, stream).character)
    roll = db.get_ability_roll(principal.client, body.ability_roll_id)
    if roll is None:
        raise not_found("ability roll", body.ability_roll_id)
    if roll.used:
        raise _roll_used(body.ability_roll_id)
    character = character_from_scores(body, scores_from_json(roll.scores), stream)
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

    The server clears the character's `id`, which a session gives each party member, because a
    session assigns a new one when it starts. A document that isn't a valid character document
    gets 422 `content_invalid`. A document stamped with a newer schema version than the
    server's osrlib reads gets 409 `version_unsupported`. Only a referee may import a character.
    """
    character = Character.from_document(document)
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
def get_character(request: Request, principal: CurrentPrincipal, character_id: str) -> StoredCharacter:
    """Return a stored character, with the values osrlib derives from it."""
    row = _db(request).get_character(principal.client, character_id)
    if row is None:
        raise not_found("character", character_id)
    return _stored(row, Character.from_document(row.document))


@router.delete(
    "/characters/{character_id}",
    status_code=204,
    tags=["characters"],
    summary="Delete a stored character",
    responses=problem_responses(404),
)
def delete_character(request: Request, principal: CurrentPrincipal, character_id: str) -> Response:
    """Delete a stored character. A session that started with the character keeps its copy."""
    if not _db(request).delete_character(principal.client, character_id):
        raise not_found("character", character_id)
    return Response(status_code=204)


@router.post("/generate/character", tags=["generate"], summary="Roll a character without storing it")
def generate_character(body: GenerateCharacterRequest) -> GeneratedCharacterResponse:
    """Roll a first-level character from the choices `POST /v1/characters` accepts, and store nothing.

    The server rolls the ability scores, then applies the choices with the same osrlib steps as
    `POST /v1/characters`. Send the same body with the returned seed to get the same character
    again. A choice that breaks a rule gets 422 `character_invalid`, with osrlib's `rejections`
    when it reports them.
    """
    seed = seed_or_draw(body.seed)
    generated = roll_character(body, creation_stream(seed))
    return GeneratedCharacterResponse(
        seed=seed, character=generated.character, derived=derive(generated.character), rolls=generated.rolls
    )
