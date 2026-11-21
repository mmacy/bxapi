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
from bxapi.models import NameText, RequestModel, has_control_character
from bxapi.pagination import Page, PageQuery
from bxapi.routers.sessions import NAME_MAX_LENGTH, SaveInfo, SessionInfo, save_info, session_info
from bxapi.session_manager import SessionManager, load_stored_save, validate_loaded_adventure

__all__ = ["RelabelSaveRequest", "RestoreSaveRequest", "router"]

router = APIRouter(prefix="/saves", tags=["saves"], route_class=IdempotentRoute)

SaveId = Annotated[
    str,
    Path(
        description="Save `id` (`sav_` followed by 22 characters), from `POST /v1/sessions/{session_id}/saves`, "
        "`POST /v1/saves/import`, or `GET /v1/saves`."
    ),
]

_NOT_FOUND = "`not_found`: Your client has no save with this id. `GET /v1/saves` lists your saves."


class RelabelSaveRequest(RequestModel):
    """New label (or `null` to clear it) for `PATCH /v1/saves/{save_id}`."""

    label: NameText | None = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    """New label, 1 to 200 characters without control characters such as tabs or newlines, or `null` to
    remove the existing label."""


class RestoreSaveRequest(RequestModel):
    """Optional request body for `POST /v1/saves/{save_id}/restore`."""

    name: NameText | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """Optional display name for the restored session, 1 to 200 characters without control characters.
    Defaults to `adventure_name` from the save when omitted or `null`."""


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
    session_id: Annotated[
        str | None,
        Query(
            description="Filter to saves created from this `session_id` (`ses_` followed by 22 characters), even if "
            "the session has since been deleted. Omit to return all saves for your client."
        ),
    ] = None,
) -> Page[SaveInfo]:
    """Return a page of your client's saved games, ordered newest first.

    Use this list for a load-game menu, optionally passing `session_id` to show checkpoints from one session (which
    continues to work after `DELETE /v1/sessions/{session_id}`). Pass a save's `id` to
    `POST /v1/saves/{save_id}/restore` to start a new session from that checkpoint.
    """
    rows = _db(request).list_saves(principal.client, limit=page.limit, offset=page.offset, session_id=session_id)
    return Page(items=[save_info(row) for row in rows.items], total=rows.total, limit=page.limit, offset=page.offset)


@router.post(
    "/import",
    status_code=201,
    summary="Import a save document",
    response_description="Stored save metadata (`session_id`, `adventure_id`, and `label` are `null`).",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: Only a referee may import a save document. Send an API key with the `referee` "
                "role."
            ],
            409: [
                "`version_unsupported`: The document's `schema_version` exceeds the `schema_version` that "
                "`GET /v1/meta` reports. Import it on a server running a newer osrlib."
            ],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib can't load the save document, the embedded adventure fails osrlib's "
                "checks (such as unknown monster, item, area, or quest references or a treasure type letter outside "
                "`A` to `V`), the adventure's `name` contains a control character, or `clock.rounds` exceeds "
                "2^63 - 1. Check `detail` and fix the document.",
            ],
        }
    ),
)
@accepts_document
def import_save(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[
        JsonObject,
        Body(
            description='Save document envelope (`kind` set to `"save"`, `schema_version`, `engine_version`, and '
            "`payload`), up to `BXAPI_MAX_BODY_BYTES` (8,388,608 bytes by default), from "
            "`GET /v1/saves/{save_id}/document` or osrlib's `save_game`."
        ),
    ],
) -> SaveInfo:
    """Requires a `referee` API key. Validate and store an exported save document as a new save.

    Use this operation to migrate a game from another server or import a save created with osrlib: export via
    `GET /v1/saves/{save_id}/document` on the source server, upload here, then call
    `POST /v1/saves/{save_id}/restore` to resume play.

    Loads the document with osrlib's `load_game`, validates the embedded adventure, checks that `clock.rounds` fits in
    a 64-bit integer and `adventure.name` contains no control characters, and re-serializes the document in the
    installed osrlib's format. The imported save has `session_id`, `adventure_id`, and `label` set to `null` (assign a
    label with `PATCH /v1/saves/{save_id}`).
    """
    game = load_game(document)
    validate_loaded_adventure(game)
    if not fits_integer_column(game.clock.rounds):
        raise invalid("content_invalid", "The save's game clock is larger than the server can store.")
    if has_control_character(game.adventure.name):
        raise invalid("content_invalid", "The name of the save's adventure has a control character.")
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
    response_description="Save metadata.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_save(request: Request, principal: CurrentPrincipal, save_id: SaveId) -> SaveInfo:
    """Return metadata for one save: `session_id`, `adventure_id`, `label`, `mode`, `clock`, and `created_at`.

    Contains only player-safe metadata. To start a session from this save, call `POST /v1/saves/{save_id}/restore`. To
    export the full game state and master seed, call `GET /v1/saves/{save_id}/document` with a `referee` key.
    """
    return save_info(_found(_db(request), principal.client, save_id))


@router.patch(
    "/{save_id}",
    summary="Relabel a save",
    response_description="Updated save metadata.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def relabel_save(request: Request, principal: CurrentPrincipal, save_id: SaveId, body: RelabelSaveRequest) -> SaveInfo:
    """Set a save's `label`, or clear it by passing `null`, without modifying the saved game."""
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
    """Delete a save and reclaim its bytes in your client's storage quota (`GET /v1/meta`).

    Sessions already restored from this save continue unaffected. Repeating a completed delete returns 404
    `not_found`.
    """
    if not _db(request).delete_save(principal.client, save_id):
        raise not_found("save", save_id)
    return Response(status_code=204)


@router.post(
    "/{save_id}/restore",
    status_code=201,
    summary="Restore a save",
    response_description="New session metadata at revision 0.",
    responses=problem_responses(
        {
            404: [_NOT_FOUND],
            500: [
                INTERNAL_ERROR,
                "`session_unreadable`: The server's osrlib can't load the save's stored document or refuses its "
                "embedded adventure. Report `save_id` and `request_id` to the server's operator.",
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
    """Start a new session from a saved checkpoint and return the new session's metadata at revision 0.

    Save the returned session `id`, then continue play with `POST /v1/sessions/{session_id}/commands` and
    `GET /v1/sessions/{session_id}/view`. The new session inherits the save's `adventure_id`, `mode`, `clock`, party,
    and RNG state, while leaving both the save and the original session untouched so one save can be restored any
    number of times. The request body is optional: pass `name` to title the new session, or send an empty body.
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
    response_description="Full save document envelope.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: Only a referee may read a save document. Send an API key with the `referee` role."
            ],
            404: [_NOT_FOUND],
        }
    ),
)
def get_save_document(request: Request, principal: RefereePrincipal, save_id: SaveId) -> JsonObject:
    """Requires a `referee` API key. Return the complete save document envelope (`kind`, `schema_version`, `payload`).

    Contains the session's master seed, RNG streams, unexplored map, monster hit points, `command_log`, and
    `event_log`, so players receive 403 `referee_required` (use `GET /v1/saves/{save_id}` for player-safe metadata).
    Pass the returned JSON object to `POST /v1/saves/import` on another server to migrate the game.
    """
    document = _db(request).get_save_document(principal.client, save_id)
    if document is None:
        raise not_found("save", save_id)
    return document
