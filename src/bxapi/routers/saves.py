"""Saved games: listing and labelling them, starting new sessions from them, and moving save documents.

A save is osrlib's save document for one moment of a session, stored beside the save's info. The document
contains the master seed, the state only the referee may see, like the unexplored map and the monsters' hit
points, and the logs of every command and event. Only a referee may read or import a save document.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Request, Response
from osrlib.persistence import load_game, save_game
from pydantic import Field

from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.db import Database, JsonObject, SaveRow, fits_integer_column
from bxapi.errors import INTERNAL_ERROR, VALIDATION_ERROR, invalid, not_found, problem_responses
from bxapi.idempotency import IdempotentRoute
from bxapi.json_body import accepts_document
from bxapi.models import RequestModel
from bxapi.pagination import Page, PageQuery
from bxapi.routers.sessions import NAME_MAX_LENGTH, SaveInfo, SessionInfo, save_info, session_info
from bxapi.session_manager import SessionManager, load_stored_save, validate_loaded_adventure

__all__ = ["RelabelSaveRequest", "RestoreSaveRequest", "router"]

router = APIRouter(prefix="/saves", tags=["saves"], route_class=IdempotentRoute)

SaveId = Annotated[
    str,
    Path(description="The save's id, which starts with `sav_`. `POST /v1/sessions/{session_id}/saves` returns it."),
]

_NOT_FOUND = "`not_found`: Your client has no save with this id. `GET /v1/saves` lists your saves."


class RelabelSaveRequest(RequestModel):
    """The save's new label."""

    label: str | None = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    """The new label, or `null` to remove the label."""


class RestoreSaveRequest(RequestModel):
    """An optional name for the session a restore starts."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """A name for the new session. Defaults to the name of the adventure in the save."""


def _db(request: Request) -> Database:
    return request.app.state.db


def _found(db: Database, client: str, save_id: str) -> SaveRow:
    row = db.get_save(client, save_id)
    if row is None:
        raise not_found("save", save_id)
    return row


@router.get("", summary="List the saves", response_description="A page of save info, newest first.")
def list_saves(
    request: Request,
    principal: CurrentPrincipal,
    page: PageQuery,
    session_id: Annotated[str | None, Query(description="Return only the saves made from this session.")] = None,
) -> Page[SaveInfo]:
    """List your client's saves, newest first.

    Use it for a menu of saved games. Pass `session_id` to list only the saves of one session, which works after the
    session is deleted too. Restore a save with `POST /v1/saves/{save_id}/restore`.
    """
    rows = _db(request).list_saves(principal.client, limit=page.limit, offset=page.offset, session_id=session_id)
    return Page(items=[save_info(row) for row in rows.items], total=rows.total, limit=page.limit, offset=page.offset)


@router.post(
    "/import",
    status_code=201,
    summary="Import a save document",
    response_description="The new save's info. It has no session, no adventure id, and no label.",
    responses=problem_responses(
        {
            403: ["`referee_required`: Only a referee may import a save. Use a key with the referee role."],
            409: [
                "`version_unsupported`: The document's `schema_version` is higher than the server's osrlib reads, "
                "which is the `schema_version` of `GET /v1/meta`. Import it on a server with a newer osrlib."
            ],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib can't load the document, the adventure in it fails osrlib's checks, or "
                "its game clock is larger than the server can store. The checks are the ones `POST /v1/adventures` "
                "runs: the adventure refers to something that doesn't exist (a monster, an item, an area, or a "
                "quest), or it has content that a session can't play, like a treasure type letter outside `A` to "
                "`V`. `detail` says which.",
            ],
        }
    ),
)
@accepts_document
def import_save(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[JsonObject, Body(description="A save document from osrlib's `save_game`.")],
) -> SaveInfo:
    """Store the save document in the request body as a new save, like one from `GET /v1/saves/{save_id}/document`.

    Only a referee may import a save. Use it to move a game from one server to another: read the document on the
    first server, send it here on the second, then restore the new save with `POST /v1/saves/{save_id}/restore`.

    The server loads the document with osrlib's `load_game`, runs osrlib's `validate_adventure` on the adventure in
    it, and stores the document osrlib writes back out of the loaded game, in the installed osrlib's format. The new
    save has no session, no adventure id, and no label. Set a label with `PATCH /v1/saves/{save_id}`.
    """
    game = load_game(document)
    validate_loaded_adventure(game)
    if not fits_integer_column(game.clock.rounds):
        raise invalid("content_invalid", "The save's game clock is larger than the server can store.")
    row = _db(request).insert_save(
        principal.client,
        session_id=None,
        adventure_id=None,
        adventure_name=game.adventure.name,
        label=None,
        mode=game.mode.value,
        clock_rounds=game.clock.rounds,
        document=save_game(game),
    )
    return save_info(row)


@router.get(
    "/{save_id}",
    summary="Get a save",
    response_description="The save's info.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_save(request: Request, principal: CurrentPrincipal, save_id: SaveId) -> SaveInfo:
    """Return a save's info: its session, adventure, label, mode, and clock.

    The info has no game state. A referee reads the whole game in `GET /v1/saves/{save_id}/document`.
    """
    return save_info(_found(_db(request), principal.client, save_id))


@router.patch(
    "/{save_id}",
    summary="Relabel a save",
    response_description="The save's info, with the new label.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def relabel_save(request: Request, principal: CurrentPrincipal, save_id: SaveId, body: RelabelSaveRequest) -> SaveInfo:
    """Change a save's label, or remove the label with `null`. The saved game doesn't change."""
    db = _db(request)
    if not db.update_save_label(principal.client, save_id, body.label):
        raise not_found("save", save_id)
    return save_info(_found(db, principal.client, save_id))


@router.delete(
    "/{save_id}",
    status_code=204,
    summary="Delete a save",
    response_description="The server deleted the save. The response has no body.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def delete_save(request: Request, principal: CurrentPrincipal, save_id: SaveId) -> Response:
    """Delete a save. A session restored from the save doesn't change."""
    if not _db(request).delete_save(principal.client, save_id):
        raise not_found("save", save_id)
    return Response(status_code=204)


@router.post(
    "/{save_id}/restore",
    status_code=201,
    summary="Restore a save",
    response_description="The new session's info, at revision 0.",
    responses=problem_responses(
        {
            404: [_NOT_FOUND],
            500: [
                INTERNAL_ERROR,
                "`session_unreadable`: The server's osrlib can't load the save's stored document, or refuses the "
                "adventure in it. Report the save's id to the server's operator.",
            ],
        }
    ),
)
def restore_save(
    request: Request,
    principal: CurrentPrincipal,
    save_id: SaveId,
    body: Annotated[RestoreSaveRequest, Body(default_factory=RestoreSaveRequest)],
) -> SessionInfo:
    """Start a new session at the moment of the save, and return the new session's info.

    Keep the new session's `id`, and play it with `POST /v1/sessions/{session_id}/commands`. The new session starts
    at revision 0, has the save's `adventure_id`, and plays on from the save as the original session would have. Its
    `clock` continues the save's clock. The save, and the session it was made from, don't change, so you can restore
    one save any number of times. The request body is optional.
    """
    db = _db(request)
    row = _found(db, principal.client, save_id)
    document = db.get_save_document(principal.client, save_id)
    if document is None:
        raise not_found("save", save_id)
    sessions: SessionManager = request.app.state.sessions
    restored = sessions.restore(
        principal.client, name=body.name, adventure_id=row.adventure_id, game=load_stored_save(save_id, document)
    )
    return session_info(restored)


@router.get(
    "/{save_id}/document",
    summary="Get a save document",
    response_description="The save document.",
    responses=problem_responses(
        {
            403: ["`referee_required`: Only a referee may read a save document. Use a key with the referee role."],
            404: [_NOT_FOUND],
        }
    ),
)
def get_save_document(request: Request, principal: RefereePrincipal, save_id: SaveId) -> JsonObject:
    """Return the save document, osrlib's `save_game` output, to keep or to import on another server.

    Only a referee may read it, because it contains the master seed, the state only the referee may see, and the
    logs of every command and event. Send it to `POST /v1/saves/import` on another server to move the game there.
    """
    document = _db(request).get_save_document(principal.client, save_id)
    if document is None:
        raise not_found("save", save_id)
    return document
