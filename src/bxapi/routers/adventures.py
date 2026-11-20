"""Stored adventures and the adventures that come with the server."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Request, Response
from osrlib.crawl.adventure import Adventure, PartySpec
from osrlib.errors import ContentValidationError, SaveVersionError

from bxapi.adventures import builtin_adventures, is_builtin, parse_adventure
from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.db import AdventureRow, Database
from bxapi.errors import forbidden, not_found, problem_responses
from bxapi.models import ApiModel
from bxapi.pagination import Page, PageQuery

__all__ = [
    "AdventureInfo",
    "AdventurePreview",
    "DungeonInfo",
    "InvalidAdventure",
    "TownInfo",
    "ValidAdventure",
    "router",
]

router = APIRouter(prefix="/adventures", tags=["adventures"])

_DOCUMENT_BODY = Body(
    description="An adventure document: osrlib's `Adventure` model in osrlib's versioned envelope, with `kind` set "
    "to `adventure`. Tools like osr-forge and osr-editor write these documents."
)


class TownInfo(ApiModel):
    """What a player may know about the adventure's town before play."""

    name: str
    """The town's name."""
    description: str
    """The text the adventure's author wrote about the town, or an empty string."""


class DungeonInfo(ApiModel):
    """What a player may know about one of the adventure's dungeons before play.

    It leaves out how many levels the dungeon has, because osrlib's player view shows only the
    levels the party has explored.
    """

    id: str
    """The dungeon's id. A session's `enter_dungeon` command takes it as `dungeon_id`."""
    name: str
    """The dungeon's name, or an empty string."""


class AdventurePreview(ApiModel):
    """What a player may know about an adventure before play, read from its document.

    The server stores it with an uploaded adventure, in the `info` column.
    """

    name: str
    """The adventure's title."""
    description: str
    """The text the adventure's author wrote to describe the adventure, or an empty string."""
    hooks: list[str]
    """The reasons the author gives for a party to take the adventure on."""
    party: PartySpec | None
    """The character levels and party size the adventure is written for, as osrlib's `PartySpec`,
    or `null` when the author gives none."""
    town: TownInfo
    """The adventure's town, where a session starts."""
    dungeons: list[DungeonInfo]
    """The adventure's dungeons, in the order the document lists them."""


class AdventureInfo(AdventurePreview):
    """What a player may know about an adventure before play, and where the adventure comes from."""

    id: str | None
    """The adventure's id. A stored adventure's id starts with `adv_`, and an adventure that
    comes with the server has an id that starts with `builtin-`. It's `null` in the result of
    `POST /v1/adventures/validate`, which stores nothing."""
    builtin: bool
    """Whether the adventure comes with the server. No client can delete one that does."""
    created_at: str | None
    """When the client stored the adventure. It's `null` for an adventure that comes with the
    server, and in the result of `POST /v1/adventures/validate`."""


class ValidAdventure(ApiModel):
    """The result of validating a document that osrlib accepts."""

    valid: Literal[True]
    """Always `true`."""
    adventure: AdventureInfo
    """What a player may know about the adventure before play, with `id` and `created_at` set to
    `null`."""


class InvalidAdventure(ApiModel):
    """The result of validating a document that the server refuses."""

    valid: Literal[False]
    """Always `false`."""
    code: Literal["content_invalid", "version_unsupported"]
    """The error code `POST /v1/adventures` returns for the same document: `content_invalid` for a
    document osrlib refuses, and `version_unsupported` for a document stamped with a newer schema
    version than the server's osrlib reads."""
    detail: str
    """Why the document was refused."""


def _db(request: Request) -> Database:
    return request.app.state.db


def _preview(adventure: Adventure) -> AdventurePreview:
    return AdventurePreview(
        name=adventure.name,
        description=adventure.description,
        hooks=list(adventure.hooks),
        party=adventure.party,
        town=TownInfo(name=adventure.town.name, description=adventure.town.description),
        dungeons=[DungeonInfo(id=dungeon.id, name=dungeon.name) for dungeon in adventure.dungeons],
    )


def _builtin_info(adventure_id: str, adventure: Adventure) -> AdventureInfo:
    return AdventureInfo(**dict(_preview(adventure)), id=adventure_id, builtin=True, created_at=None)


def _stored_info(row: AdventureRow) -> AdventureInfo:
    preview = AdventurePreview.model_validate(row.info)
    return AdventureInfo(**dict(preview), id=row.id, builtin=False, created_at=row.created_at)


@router.post("", status_code=201, summary="Upload an adventure", responses=problem_responses(409))
def upload_adventure(
    request: Request, principal: CurrentPrincipal, document: Annotated[dict[str, Any], _DOCUMENT_BODY]
) -> AdventureInfo:
    """Check an adventure document with osrlib and store it as uploaded.

    The server runs every check osrlib makes on an adventure, so an adventure you can upload can
    start a session. Each upload stores a new adventure, even when the document is the same as
    one already stored. A document osrlib refuses gets 422 `content_invalid`. A document stamped
    with a newer schema version than the server's osrlib reads gets 409 `version_unsupported`.
    """
    adventure = parse_adventure(document)
    preview = _preview(adventure)
    row = _db(request).insert_adventure(
        principal.client, name=adventure.name, info=preview.model_dump(mode="json"), document=document
    )
    return _stored_info(row)


@router.post("/validate", summary="Check an adventure document without storing it")
def validate_adventure(document: Annotated[dict[str, Any], _DOCUMENT_BODY]) -> ValidAdventure | InvalidAdventure:
    """Run the checks of `POST /v1/adventures` on a document, and store nothing.

    The response is 200 either way. When the upload would fail, `valid` is `false`, `code` is
    the error code the upload would return, and `detail` says why.
    """
    try:
        adventure = parse_adventure(document)
    except ContentValidationError as error:
        return InvalidAdventure(valid=False, code="content_invalid", detail=str(error))
    except SaveVersionError as error:
        return InvalidAdventure(valid=False, code="version_unsupported", detail=str(error))
    info = AdventureInfo(**dict(_preview(adventure)), id=None, builtin=False, created_at=None)
    return ValidAdventure(valid=True, adventure=info)


@router.get("", summary="List the adventures")
def list_adventures(request: Request, principal: CurrentPrincipal, page: PageQuery) -> Page[AdventureInfo]:
    """Return a page of adventures.

    The list starts with the adventures that come with the server, in id order, and continues
    with the client's adventures, newest first. `total`, `limit`, and `offset` count both parts
    as one list.
    """
    builtins = [
        _builtin_info(adventure_id, builtin.adventure) for adventure_id, builtin in builtin_adventures().items()
    ]
    items = builtins[page.offset : page.offset + page.limit]
    stored = _db(request).list_adventures(
        principal.client, limit=page.limit - len(items), offset=max(0, page.offset - len(builtins))
    )
    items += [_stored_info(row) for row in stored.items]
    return Page(items=items, total=len(builtins) + stored.total, limit=page.limit, offset=page.offset)


@router.get("/{adventure_id}", summary="Get an adventure's info", responses=problem_responses(404))
def get_adventure(request: Request, principal: CurrentPrincipal, adventure_id: str) -> AdventureInfo:
    """Return what a player may know about an adventure before play."""
    builtin = builtin_adventures().get(adventure_id)
    if builtin is not None:
        return _builtin_info(adventure_id, builtin.adventure)
    row = None if is_builtin(adventure_id) else _db(request).get_adventure(principal.client, adventure_id)
    if row is None:
        raise not_found("adventure", adventure_id)
    return _stored_info(row)


@router.get(
    "/{adventure_id}/document",
    summary="Get an adventure document",
    responses=problem_responses(403, 404),
)
def get_adventure_document(request: Request, principal: RefereePrincipal, adventure_id: str) -> dict[str, Any]:
    """Return the whole adventure document, as uploaded, with the map, monsters, treasure, triggers, and quests.

    The document is the one the client uploaded, including any field osrlib ignores. Only a
    referee may read it. It works for the adventures that come with the server too.
    """
    builtin = builtin_adventures().get(adventure_id)
    if builtin is not None:
        return dict(builtin.document)
    document = None if is_builtin(adventure_id) else _db(request).get_adventure_document(principal.client, adventure_id)
    if document is None:
        raise not_found("adventure", adventure_id)
    return document


@router.delete(
    "/{adventure_id}",
    status_code=204,
    summary="Delete a stored adventure",
    responses=problem_responses(403, 404),
)
def delete_adventure(request: Request, principal: CurrentPrincipal, adventure_id: str) -> Response:
    """Delete an adventure the client stored. A session that already started keeps its copy.

    An adventure that comes with the server gets 403 `adventure_read_only`.
    """
    if adventure_id in builtin_adventures():
        raise forbidden("adventure_read_only", f"The adventure {adventure_id!r} comes with the server.")
    if is_builtin(adventure_id) or not _db(request).delete_adventure(principal.client, adventure_id):
        raise not_found("adventure", adventure_id)
    return Response(status_code=204)
