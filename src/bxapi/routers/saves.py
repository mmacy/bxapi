"""Saved games: listing and labelling them, starting new sessions from them, and moving save documents.

A save is osrlib's save document for one moment of a session, stored beside the save's info. Only a
referee reads or imports the document itself, because it contains the master seed and everything
the referee knows.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Request, Response
from osrlib.crawl.adventure import Adventure
from osrlib.persistence import load_game, save_game
from pydantic import Field

from bxapi.adventures import builtin_adventures
from bxapi.auth import CurrentPrincipal, RefereePrincipal
from bxapi.db import Database, JsonObject, SaveRow
from bxapi.errors import not_found, problem_responses
from bxapi.models import RequestModel
from bxapi.pagination import Page, PageQuery
from bxapi.routers.sessions import NAME_MAX_LENGTH, SaveInfo, SessionInfo, save_info, session_info
from bxapi.session_manager import SessionManager

__all__ = ["RelabelSaveRequest", "RestoreSaveRequest", "router"]

router = APIRouter(prefix="/saves", tags=["saves"])

SaveId = Annotated[str, Path(description="The save's id, which starts with `sav_`.")]


class RelabelSaveRequest(RequestModel):
    """The save's new label."""

    label: str | None = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    """The new label, or `null` to remove the label."""


class RestoreSaveRequest(RequestModel):
    """An optional name for the session a restore starts."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """A name for the new session. Defaults to the adventure's name."""


def _db(request: Request) -> Database:
    return request.app.state.db


def _found(db: Database, client: str, save_id: str) -> SaveRow:
    row = db.get_save(client, save_id)
    if row is None:
        raise not_found("save", save_id)
    return row


def _adventure_id(db: Database, client: str, row: SaveRow, adventure: Adventure) -> str | None:
    """Return the id of the adventure a save plays, or `None` when the server can't tell.

    The save's session records the id. A save whose session was deleted, or an imported save, gets the id of a
    built-in adventure with the same content as the save's copy of the adventure, if there's one.
    """
    if row.session_id is not None:
        origin = db.get_session(client, row.session_id)
        if origin is not None:
            return origin.adventure_id
    for builtin in builtin_adventures().values():
        if builtin.adventure == adventure:
            return builtin.id
    return None


@router.get("", summary="List the saves")
def list_saves(
    request: Request,
    principal: CurrentPrincipal,
    page: PageQuery,
    session_id: Annotated[str | None, Query(description="Keep the saves made from this session.")] = None,
) -> Page[SaveInfo]:
    """List your client's saves, newest first. Pass `session_id` to keep the saves of one session."""
    rows = _db(request).list_saves(principal.client, limit=page.limit, offset=page.offset, session_id=session_id)
    return Page(items=[save_info(row) for row in rows.items], total=rows.total, limit=page.limit, offset=page.offset)


@router.post("/import", status_code=201, summary="Import a save document", responses=problem_responses(403, 409))
def import_save(
    request: Request,
    principal: RefereePrincipal,
    document: Annotated[JsonObject, Body(description="A save document from osrlib's `save_game`.")],
) -> SaveInfo:
    """Store a save document, like one from `GET /v1/saves/{save_id}/document` on another server, as a new save.

    Only a referee may import a save. The server loads the document with osrlib's `load_game` first, and stores the
    document osrlib writes back out of the loaded game, in the installed osrlib's format. A document osrlib can't
    load gets 422 `content_invalid`, and one stamped with a newer `schema_version` gets 409 `version_unsupported`.
    The new save has no session and no label.
    """
    game = load_game(document)
    row = _db(request).insert_save(
        principal.client,
        session_id=None,
        adventure_name=game.adventure.name,
        label=None,
        mode=game.mode.value,
        clock_rounds=game.clock.rounds,
        document=save_game(game),
    )
    return save_info(row)


@router.get("/{save_id}", summary="Get a save", responses=problem_responses(404))
def get_save(request: Request, principal: CurrentPrincipal, save_id: SaveId) -> SaveInfo:
    """Return a save's info: its session, label, mode, and clock."""
    return save_info(_found(_db(request), principal.client, save_id))


@router.patch("/{save_id}", summary="Relabel a save", responses=problem_responses(404))
def relabel_save(request: Request, principal: CurrentPrincipal, save_id: SaveId, body: RelabelSaveRequest) -> SaveInfo:
    """Change a save's label, or remove it with `null`. The saved game doesn't change."""
    db = _db(request)
    if not db.update_save_label(principal.client, save_id, body.label):
        raise not_found("save", save_id)
    return save_info(_found(db, principal.client, save_id))


@router.delete("/{save_id}", status_code=204, summary="Delete a save", responses=problem_responses(404))
def delete_save(request: Request, principal: CurrentPrincipal, save_id: SaveId) -> Response:
    """Delete a save. A session restored from it doesn't change."""
    if not _db(request).delete_save(principal.client, save_id):
        raise not_found("save", save_id)
    return Response(status_code=204)


@router.post("/{save_id}/restore", status_code=201, summary="Restore a save", responses=problem_responses(404))
def restore_save(
    request: Request, principal: CurrentPrincipal, save_id: SaveId, body: RestoreSaveRequest | None = None
) -> SessionInfo:
    """Start a new session at the moment of the save, and return its info.

    The new session starts at revision 0 and plays on from the save as the original session would have. The save,
    and the session it was made from, don't change. The body is optional.
    """
    db = _db(request)
    row = _found(db, principal.client, save_id)
    document = db.get_save_document(principal.client, save_id)
    if document is None:
        raise not_found("save", save_id)
    sessions: SessionManager = request.app.state.sessions
    game = load_game(document)
    restored = sessions.restore(
        principal.client,
        name=body.name if body is not None else None,
        adventure_id=_adventure_id(db, principal.client, row, game.adventure),
        game=game,
    )
    return session_info(restored)


@router.get("/{save_id}/document", summary="Get a save document", responses=problem_responses(403, 404))
def get_save_document(request: Request, principal: RefereePrincipal, save_id: SaveId) -> JsonObject:
    """Return the save document, osrlib's `save_game` output, to keep or to import on another server.

    Only a referee may read it, because it contains the master seed and the whole state of the game. A player gets
    403 `referee_required`.
    """
    document = _db(request).get_save_document(principal.client, save_id)
    if document is None:
        raise not_found("save", save_id)
    return document
