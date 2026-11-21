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
        description="Adventure `id`: `builtin-barrow` (or another `builtin-` id) for a built-in adventure, or `adv_` "
        "followed by 22 characters from `POST /v1/adventures` or `GET /v1/adventures`."
    ),
]

_DOCUMENT_BODY = Body(
    description='Adventure document envelope (`kind` set to `"adventure"`, `schema_version`, `engine_version`, and '
    "osrlib's `Adventure` model in `payload`), up to `BXAPI_MAX_BODY_BYTES` (8,388,608 bytes by default). Produced by "
    "[osr-forge](https://github.com/mmacy/osr-forge), [osr-editor](https://github.com/mmacy/osr-editor), or osrlib."
)


_NOT_FOUND = (
    "`not_found`: No built-in adventure and no adventure uploaded by your client has this id. "
    "`GET /v1/adventures` lists available ids."
)


class TownInfo(ApiModel):
    """Player-visible summary of an adventure's base town."""

    name: str
    """Town display name."""
    description: str
    """Player-facing town description written by the adventure's author, or `""` when none is set."""


class DungeonInfo(ApiModel):
    """Player-visible summary of one dungeon in an adventure, omitting level counts and maps."""

    id: str
    """Dungeon identifier within the adventure. Pass as `dungeon_id` in the `enter_dungeon` command
    on `POST /v1/sessions/{session_id}/commands`."""
    name: str
    """Dungeon display name, or `""` when none is set."""


class AdventurePreview(ApiModel):
    """Player-visible summary extracted from an adventure document."""

    name: str
    """Adventure title."""
    description: str
    """Player-facing introduction written by the adventure's author, or `""` when none is set."""
    hooks: list[str]
    """Plot hooks giving the party reasons to undertake the adventure."""
    party: PartySpec | None
    """Recommended character levels and party size as osrlib's `PartySpec`, or `null` when
    unspecified."""
    town: TownInfo
    """Starting town where new sessions of this adventure begin."""
    dungeons: list[DungeonInfo]
    """Dungeons in the adventure, in document order."""


class AdventureInfo(AdventurePreview):
    """Player-visible adventure summary along with storage metadata."""

    id: str | None
    """Adventure identifier (`builtin-` prefix for built-in adventures, or `adv_` followed by 22
    characters for client uploads), or `null` in the response from `POST /v1/adventures/validate`."""
    builtin: bool
    """Whether the adventure ships with the server (`true`) rather than belonging to your client
    (`false`). Built-in adventures cannot be deleted."""
    created_at: str | None
    """UTC timestamp when your client uploaded the adventure (RFC 3339 format, such as
    `"2026-11-20T22:01:05Z"`), or `null` for built-in adventures and validation previews."""


class ValidAdventure(ApiModel):
    """Validation result when `POST /v1/adventures/validate` accepts an adventure document."""

    valid: Literal[True]
    """Always `true`."""
    adventure: AdventureInfo
    """Player-visible info that `POST /v1/adventures` would return, with `id` and `created_at` set to
    `null`."""


class InvalidAdventure(ApiModel):
    """Validation result when `POST /v1/adventures/validate` refuses an adventure document."""

    valid: Literal[False]
    """Always `false`."""
    code: Literal["content_invalid", "version_unsupported"]
    """Error code that `POST /v1/adventures` would return for the same document: `content_invalid`
    when the envelope, schema, references, or adventure name fail validation, or
    `version_unsupported` when `schema_version` exceeds the server's `schema_version`."""
    detail: str
    """Human-readable validation error details, at most 1,000 characters."""


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
    response_description="Stored adventure info with its newly assigned `adv_` id.",
    responses=problem_responses(
        {
            409: [
                "`version_unsupported`: The document's `schema_version` exceeds the `schema_version` that "
                "`GET /v1/meta` reports."
            ],
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib refuses the document: the envelope is malformed, `payload` fails osrlib's "
                "`Adventure` schema, a monster, item, area, or quest reference is unknown, a gameplay constraint "
                "fails (such as a treasure type letter outside `A` to `V`), or the adventure's `name` contains a "
                "control character. Check `detail` and fix the document.",
            ],
        }
    ),
)
@accepts_document
def upload_adventure(
    request: Request, principal: CurrentPrincipal, document: Annotated[dict[str, Any], _DOCUMENT_BODY]
) -> AdventureInfo:
    """Validate an adventure document with osrlib, store it as uploaded, and return its player-safe info.

    Pass the returned `id` as `adventure_id` to `POST /v1/sessions` to start a game. The request body may be up to
    `BXAPI_MAX_BODY_BYTES` (8,388,608 bytes by default). Because the server runs every osrlib adventure check before
    storing, any uploaded adventure is ready to play on the current osrlib version. To check a document without
    storing it, call `POST /v1/adventures/validate` instead.

    Uploading the same document twice creates two independent records with distinct `adv_` ids. To replace an
    adventure, upload the new version and delete the old one with `DELETE /v1/adventures/{adventure_id}`.
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
    response_description="`valid` with the adventure's info when accepted, or the failure `code` and `detail`.",
)
@accepts_document
def validate_adventure(document: Annotated[dict[str, Any], _DOCUMENT_BODY]) -> ValidAdventure | InvalidAdventure:
    """Run every check of `POST /v1/adventures` on an adventure document without storing anything.

    Use this endpoint in an editor or CI pipeline before calling `POST /v1/adventures`. Always returns HTTP 200 for a
    syntactically valid JSON object (up to `BXAPI_MAX_BODY_BYTES`): when accepted, `valid` is `true` and `adventure`
    contains the player-safe preview with `id` and `created_at` set to `null`. When refused, `valid` is `false`,
    `code` matches the error code `POST /v1/adventures` would return (`content_invalid` or `version_unsupported`),
    and `detail` explains what failed.
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
    """Return playable adventures: built-in adventures in id order first, then your client's uploads newest first.

    Display the returned `AdventureInfo` entries in an adventure picker and pass the chosen `id` as `adventure_id` to
    `POST /v1/sessions`. `total`, `limit`, and `offset` paginate across built-in and uploaded adventures as one
    combined list. Every server includes `builtin-barrow`.
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
    response_description="Player-visible adventure info.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_adventure(request: Request, principal: CurrentPrincipal, adventure_id: AdventureId) -> AdventureInfo:
    """Return player-safe info for one built-in or uploaded adventure before play.

    Includes `name`, `description`, `hooks`, recommended `party`, `town`, and dungeon `id` and `name` entries, while
    omitting maps, keyed rooms, monsters, treasure, triggers, and quests. Pass `id` as `adventure_id` to
    `POST /v1/sessions` to start a game, or call `GET /v1/adventures/{adventure_id}/document` with a `referee` key to
    read the full document.
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
    response_description="Full adventure document envelope.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: Only a referee may read an adventure document. Send an API key with the "
                "`referee` role."
            ],
            404: [_NOT_FOUND],
        }
    ),
)
def get_adventure_document(request: Request, principal: RefereePrincipal, adventure_id: AdventureId) -> dict[str, Any]:
    """Requires a `referee` API key. Return the complete adventure document as uploaded.

    Exposes dungeon maps, keyed monsters, treasure, triggers, and quests, so players receive 403 `referee_required`
    (use `GET /v1/adventures/{adventure_id}` for player-safe info). Returns uploaded documents verbatim, including
    any unknown fields osrlib ignores, and also exports built-in adventures such as `builtin-barrow` so you can
    inspect or adapt them in an editor and re-upload via `POST /v1/adventures`.
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
            403: ["`adventure_read_only`: Built-in adventures ship with the server and cannot be deleted."],
            404: ["`not_found`: Your client has no uploaded adventure with this id."],
        }
    ),
)
def delete_adventure(request: Request, principal: CurrentPrincipal, adventure_id: AdventureId) -> Response:
    """Delete an adventure uploaded by your client and reclaim its bytes in your storage quota (`GET /v1/meta`).

    Sessions already started from this adventure embed a copy and continue unaffected. Attempting to delete a
    built-in adventure such as `builtin-barrow` fails with 403 `adventure_read_only`, and repeating a completed
    delete returns 404 `not_found`.
    """
    if adventure_id in builtin_adventures():
        raise forbidden("adventure_read_only", f"The adventure {adventure_id!r} comes with the server.")
    if is_builtin(adventure_id) or not _db(request).delete_adventure(principal.client, adventure_id):
        raise not_found("adventure", adventure_id)
    return Response(status_code=204)
