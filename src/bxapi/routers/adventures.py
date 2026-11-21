"""Stored adventures and the adventures that come with the server."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Request, Response
from osrlib.crawl.adventure import Adventure, PartySpec
from osrlib.errors import ContentValidationError, SaveVersionError

from bxapi.adventures import builtin_adventures, is_builtin, parse_adventure
from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.db import AdventureRow, Database
from bxapi.errors import VALIDATION_ERROR, forbidden, not_found, problem_responses
from bxapi.idempotency import IdempotentRoute
from bxapi.json_body import accepts_document
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

router = APIRouter(prefix="/adventures", tags=["adventures"], route_class=IdempotentRoute)

AdventureId = Annotated[
    str,
    Path(
        description="The adventure's ID: `adv_` followed by 22 characters for a stored adventure, or an ID such as "
        "`builtin-barrow` for a built-in adventure."
    ),
]

_DOCUMENT_BODY = Body(
    description="An adventure document: osrlib's `Adventure` model in osrlib's versioned envelope, with `kind` set "
    "to `adventure`. osr-forge converts adventure module PDFs into these documents, and osr-editor is a local "
    "graphical application for writing and editing them. Both are separate projects by osrlib's author."
)


_NOT_FOUND = (
    "`not_found`: No built-in adventure has this ID, and your client has no adventure with this ID. "
    "`GET /v1/adventures` lists the valid IDs."
)


class TownInfo(ApiModel):
    """What a player may know about the adventure's town before play."""

    name: str
    """The town's name."""
    description: str
    """The text the adventure's author wrote about the town, or an empty string."""


class DungeonInfo(ApiModel):
    """What a player may know about one of the adventure's dungeons before play.

    The info omits the number of levels in the dungeon, because osrlib's player view shows only the levels the party
    has explored.
    """

    id: str
    """The dungeon's ID. A session's `enter_dungeon` command takes it as `dungeon_id`."""
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
    """The reasons the author gives for a party to take on the adventure."""
    party: PartySpec | None
    """The character levels and party size the adventure is written for, as osrlib's `PartySpec`, or `null` when the
    author specifies none."""
    town: TownInfo
    """The adventure's town, where a session starts."""
    dungeons: list[DungeonInfo]
    """The adventure's dungeons, in the order the document lists them."""


class AdventureInfo(AdventurePreview):
    """What a player may know about an adventure before play, plus its ID, its origin, and when it was stored."""

    id: str | None
    """The adventure's ID. A stored adventure's ID starts with `adv_`, and a built-in adventure's ID starts with
    `builtin-`. It's `null` in the result of `POST /v1/adventures/validate`, which stores nothing."""
    builtin: bool
    """Whether the adventure is built in. No client can delete a built-in adventure."""
    created_at: str | None
    """When the client stored the adventure. It's `null` for a built-in adventure, and in the result of
    `POST /v1/adventures/validate`."""


class ValidAdventure(ApiModel):
    """The result of validating a document that osrlib accepts."""

    valid: Literal[True]
    """Always `true`."""
    adventure: AdventureInfo
    """What a player may know about the adventure before play, with `id` and `created_at` set to `null`."""


class InvalidAdventure(ApiModel):
    """The result of validating a document that the server rejects."""

    valid: Literal[False]
    """Always `false`."""
    code: Literal["content_invalid", "version_unsupported"]
    """The error code that `POST /v1/adventures` returns for the same document: `content_invalid` for a document that
    osrlib rejects, or `version_unsupported` for a document with a newer schema version than the server's osrlib
    reads."""
    detail: str
    """The reason osrlib rejected the document."""


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


@router.post(
    "",
    status_code=201,
    summary="Upload an adventure",
    response_description="The stored adventure's info, with its new ID.",
    responses=problem_responses(
        {
            409: [
                "`version_unsupported`: The document's `schema_version` is higher than the version the server's "
                "osrlib reads, which `GET /v1/meta` returns as `schema_version`."
            ],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib rejects the document. The envelope is wrong, the adventure doesn't match "
                "osrlib's `Adventure` model, the adventure refers to something that doesn't exist (a monster, an "
                "item, an area, or a quest), or it has content that a session can't play, such as a treasure type "
                "letter outside `A` to `V`. `detail` lists the problems. Fix the document.",
            ],
        }
    ),
)
@accepts_document
def upload_adventure(
    request: Request, principal: CurrentPrincipal, document: Annotated[dict[str, Any], _DOCUMENT_BODY]
) -> AdventureInfo:
    """Validate an adventure document with osrlib and store it as uploaded.

    To play the adventure, pass the returned `id` as `adventure_id` to `POST /v1/sessions`. The server runs every
    check that osrlib makes on an adventure, so you can start a session with any adventure you can upload. To check
    a document without storing it, for example in an editor, use `POST /v1/adventures/validate`.

    Each upload stores a new adventure, even when the document is identical to one that's already stored. To replace
    an adventure, upload the new document and delete the old adventure.
    """
    adventure = parse_adventure(document)
    preview = _preview(adventure)
    row = _db(request).insert_adventure(
        principal.client, name=adventure.name, info=preview.model_dump(mode="json"), document=document
    )
    return _stored_info(row)


@router.post(
    "/validate",
    summary="Check an adventure document without storing it",
    response_description="`valid` with the adventure's info, or the reason the upload would fail.",
)
@accepts_document
def validate_adventure(document: Annotated[dict[str, Any], _DOCUMENT_BODY]) -> ValidAdventure | InvalidAdventure:
    """Run the checks of `POST /v1/adventures` on a document, and store nothing.

    Use it in a tool that writes adventures, to show the author what's wrong before an upload. The response is a
    `200` whether or not the document passes. When the document passes, `valid` is `true` and `adventure` is the
    info that the upload would store, with `id` and `created_at` set to `null`. When the upload would fail, `valid`
    is `false`, `code` is the error code that the upload would return, and `detail` states the reason.
    """
    try:
        adventure = parse_adventure(document)
    except ContentValidationError as error:
        return InvalidAdventure(valid=False, code="content_invalid", detail=str(error))
    except SaveVersionError as error:
        return InvalidAdventure(valid=False, code="version_unsupported", detail=str(error))
    info = AdventureInfo(**dict(_preview(adventure)), id=None, builtin=False, created_at=None)
    return ValidAdventure(valid=True, adventure=info)


@router.get("", summary="List the adventures", response_description="A page of adventure info.")
def list_adventures(request: Request, principal: CurrentPrincipal, page: PageQuery) -> Page[AdventureInfo]:
    """Return a page of the adventures you can play.

    Show the list to the player to choose an adventure, and pass the chosen `id` as `adventure_id` to
    `POST /v1/sessions`. The list starts with the built-in adventures, in ID order, and continues with your client's
    adventures, newest first. `total`, `limit`, and `offset` count both parts as one list.
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


@router.get(
    "/{adventure_id}",
    summary="Get an adventure's info",
    response_description="The adventure's info.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_adventure(request: Request, principal: CurrentPrincipal, adventure_id: AdventureId) -> AdventureInfo:
    """Return what a player may know about an adventure before play.

    The info includes the title, the author's description and hooks, the town, and the names of the dungeons. It
    never includes the map, the monsters, the treasure, the triggers, or the quests. A referee reads those with
    `GET /v1/adventures/{adventure_id}/document`.
    """
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
    response_description="The adventure document.",
    responses=problem_responses(
        {
            403: ["`referee_required`: Only a referee may read an adventure document. Use a referee key."],
            404: [_NOT_FOUND],
        }
    ),
)
def get_adventure_document(request: Request, principal: RefereePrincipal, adventure_id: AdventureId) -> dict[str, Any]:
    """Return the whole adventure document, as uploaded, with the map, monsters, treasure, triggers, and quests.

    Only a referee may read it, because it reveals everything the players are meant to discover in play. The
    document is the one the client uploaded, including any field that osrlib ignores. You can also read a built-in
    adventure's document, for example to start a new adventure from it in an editor.
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
    response_description="The server deleted the adventure. The response has no body.",
    responses=problem_responses(
        {
            403: ["`adventure_read_only`: The adventure is built in, and no client can delete it."],
            404: ["`not_found`: Your client has no adventure with this ID."],
        }
    ),
)
def delete_adventure(request: Request, principal: CurrentPrincipal, adventure_id: AdventureId) -> Response:
    """Delete an adventure that the client stored. A session that already started has its own copy and isn't affected.

    A request to delete a built-in adventure returns `403 adventure_read_only`.
    """
    if adventure_id in builtin_adventures():
        raise forbidden("adventure_read_only", f"The adventure {adventure_id!r} comes with the server.")
    if is_builtin(adventure_id) or not _db(request).delete_adventure(principal.client, adventure_id):
        raise not_found("adventure", adventure_id)
    return Response(status_code=204)
