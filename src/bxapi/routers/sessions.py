"""Games in progress: starting them, sending them commands, and reading what the players see.

The endpoints that run a game or read a running one go through the
[`SessionManager`][bxapi.session_manager.SessionManager] at `app.state.sessions`, which runs one
request at a time on each session. Session info, the session list, renaming, deleting, and saving
read and write the database directly.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Query, Request, Response
from osrlib.core.character import Character, party_from_document
from osrlib.core.classes import ClassDefinition
from osrlib.core.clock import GameClock
from osrlib.core.events import Event, Visibility
from osrlib.core.ruleset import Ruleset
from osrlib.core.spells import SpellTemplate
from osrlib.core.validation import Rejection
from osrlib.crawl import battle
from osrlib.crawl.battle import BattleRoundPreview
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, AnyCommand, BattleDeclaration, Command, SessionMode
from osrlib.crawl.events import AnyEvent
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.crawl.views import PlayerView, RefereeView, build_player_view, build_referee_view
from osrlib.messages import format_message
from pydantic import Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

from bxapi.adventures import find_adventure
from bxapi.auth import CurrentPrincipal, Principal, RefereePrincipal
from bxapi.commands import CommandKind, command_kind
from bxapi.db import Database, JsonObject, SaveRow, SessionRow
from bxapi.errors import (
    INTERNAL_ERROR,
    VALIDATION_ERROR,
    ApiError,
    conflict,
    forbidden,
    invalid,
    not_found,
    problem_responses,
)
from bxapi.idempotency import IdempotentRoute
from bxapi.json_body import accepts_document
from bxapi.models import ApiModel, NameText, RequestModel, left_out_when_none
from bxapi.pagination import Page, PageQuery
from bxapi.seeds import Seed, referee_seed
from bxapi.session_manager import MAX_PARTY_SIZE, SessionManager

__all__ = [
    "NAME_MAX_LENGTH",
    "BattleRoundPreviewRequest",
    "Clock",
    "CommandChoice",
    "CommandOutcome",
    "CreateSaveRequest",
    "CreateSessionRequest",
    "EventPage",
    "RenameSessionRequest",
    "SaveInfo",
    "SessionCommands",
    "SessionInfo",
    "router",
    "save_info",
    "session_info",
]

router = APIRouter(prefix="/sessions", tags=["sessions"], route_class=IdempotentRoute)

NAME_MAX_LENGTH = 200
"""The longest session name or save label, in characters."""

type _LoggedEvent = Event | dict[str, Any]
"""An entry of a session's event log. A save from a newer osrlib can have an event type this osrlib has no class
for, and osrlib's `load_game` returns that entry as a dict."""

if TYPE_CHECKING:
    # osrlib builds `AnyCommand` and `AnyEvent` from tuples of classes at run time, so a type checker can't read
    # them as types. It checks against the base classes. FastAPI and pydantic read the unions, which validate the
    # body as the command class that matches its `command_type`, serialize every field of each event, and put every
    # command and event class in the OpenAPI document.
    CommandBody = Command
    AnyEventModel = Event
else:
    CommandBody = AnyCommand
    AnyEventModel = AnyEvent

SessionId = Annotated[
    str,
    Path(
        description="Session `id` (`ses_` followed by 22 characters), from `POST /v1/sessions`, "
        "`POST /v1/saves/{save_id}/restore`, or `GET /v1/sessions`."
    ),
]

_NOT_FOUND = "`not_found`: Your client has no session with this id. `GET /v1/sessions` lists your sessions."
_BUSY = (
    "`session_busy`: The request waited more than 5 seconds for another request running on the session and made no "
    "changes. `revision` reports the session's current revision. Retry the request."
)
_UNREADABLE = (
    "`session_unreadable`: The server's osrlib can't load the session's stored state, for example because a server "
    "running another osrlib version wrote it. Report the session's id and `request_id` to the server's operator."
)
_REFEREE_EVENTS = (
    "`referee_required`: A player passed `visibility=referee`. Omit `visibility` (or pass `visibility=player`) to "
    "receive player-visible events."
)
VisibilityQuery = Annotated[
    Visibility,
    Query(
        description="Event visibility filter (`player` or `referee`, defaults to `player`). `player` returns only "
        "player-visible events, whereas `referee` returns both player and referee events (`referee` role required)."
    ),
]


# Models


class Clock(ApiModel):
    """Elapsed in-game time since the adventure began, preserved across saves and restores."""

    rounds: int
    """Total elapsed time in 10-second combat rounds."""
    turns: int
    """Total elapsed time in whole 10-minute exploration turns (`rounds` divided by 60)."""
    days: int
    """Total elapsed time in whole 24-hour days (`rounds` divided by 8,640)."""

    @classmethod
    def from_rounds(cls, rounds: int) -> Clock:
        """Return the clock for a number of rounds, with the turns and days that osrlib's `GameClock` counts."""
        clock = GameClock(rounds=rounds)
        return cls(rounds=clock.rounds, turns=clock.turns, days=clock.days)


class SessionInfo(ApiModel):
    """Metadata for a game session, returned by session list, create, get, rename, and restore operations."""

    id: str
    """Session identifier (`ses_` followed by 22 characters)."""
    name: str
    """Display name of the session. Defaults to `adventure_name` and can be updated via
    `PATCH /v1/sessions/{session_id}`."""
    adventure_id: str | None
    """Adventure identifier that started the session, or `null` when restored from an imported save.
    Play continues unaffected if `adventure_id` is `null` or you deleted the original adventure,
    because each session embeds a copy of the adventure."""
    adventure_name: str
    """Title of the session's embedded adventure."""
    mode: SessionMode
    """Current session mode (`town`, `exploring`, `encounter`, `battle`, `game_over`, or `victory`)
    after the last accepted command."""
    ended: bool
    """Whether the session has reached a terminal mode (`game_over` or `victory`)."""
    revision: int
    """Count of accepted command requests in this session. Starts at 0 on `POST /v1/sessions` and on
    `POST /v1/saves/{save_id}/restore`."""
    clock: Clock
    """Elapsed in-game time after the last accepted command."""
    schema_version: int
    """osrlib save document schema version of the stored session."""
    engine_version: str
    """osrlib version string that last wrote the session."""
    created_at: str
    """UTC timestamp when the server created the session, in RFC 3339 format such as
    `"2026-11-20T22:01:05Z"`."""
    updated_at: str
    """UTC timestamp of the last accepted command (RFC 3339 format), or `created_at` when `revision`
    is 0. Renaming the session leaves `updated_at` unchanged."""


class SaveInfo(ApiModel):
    """Metadata for a saved game snapshot, without its referee-only save document."""

    id: str
    """Save identifier (`sav_` followed by 22 characters). Pass to `POST /v1/saves/{save_id}/restore`
    or `GET /v1/saves/{save_id}`."""
    session_id: str | None
    """Source session `id` that produced the save (even if you later deleted that session), or
    `null` when imported via `POST /v1/saves/import`."""
    adventure_id: str | None
    """Source session's `adventure_id` when you saved the session (even if later deleted), or `null`
    for an imported save or a save made from a session restored from an imported save."""
    adventure_name: str
    """Title of the adventure embedded in the save."""
    label: str | None
    """Optional human-readable label (1 to 200 characters), or `null` when unlabeled."""
    mode: SessionMode
    """Session mode when you saved the session."""
    clock: Clock
    """Elapsed in-game time when you saved the session."""
    created_at: str
    """UTC timestamp when the server stored the save, in RFC 3339 format such as
    `"2026-11-20T22:01:05Z"`."""


class CreateSessionRequest(RequestModel):
    """Adventure, party, and optional ruleset and seed for starting a session.

    Provide either `character_ids` or `party_document`, never both.
    """

    adventure_id: str = Field(min_length=1, max_length=128)
    """Adventure `id` from `GET /v1/adventures` or `POST /v1/adventures`, such as `"builtin-barrow"`."""
    character_ids: list[str] | None = Field(default=None, max_length=MAX_PARTY_SIZE)
    """Stored character `id` values from `POST /v1/characters` or `GET /v1/characters` (1 to 20
    entries), in marching order with the front rank first. Required unless `party_document` is set.
    Sessions copy each character, so listing the same `id` twice creates two independent copies with
    distinct in-session ids."""
    party_document: JsonObject | None = None
    """Referee only. Party document envelope (`kind` set to `"party"`, `schema_version`,
    `engine_version`, and `payload.characters` with 1 to 20 characters) as written by osrlib's
    `party_to_document`. Mutually exclusive with `character_ids`."""
    name: NameText | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """Optional session display name, 1 to 200 characters without control characters. Defaults to the
    adventure's `name` when omitted or `null`."""
    ruleset: Ruleset | None = None
    """Optional B/X house-rule overrides as osrlib's `Ruleset`. Any omitted field uses the standard
    1981 B/X rule as written."""
    seed: Seed | None = None
    """Referee only. Master random seed (0 to 2^53 - 1) for every roll in the session. Omit to let
    the server draw a random seed. Never returned outside a referee save document."""

    @model_validator(mode="after")
    def _one_party(self) -> CreateSessionRequest:
        if (self.character_ids is None) == (self.party_document is None):
            raise ValueError("send either character_ids or party_document, and not both")
        return self


class RenameSessionRequest(RequestModel):
    """New display name for `PATCH /v1/sessions/{session_id}`."""

    name: NameText = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    """New session name, 1 to 200 characters without control characters such as tabs or newlines."""


class CreateSaveRequest(RequestModel):
    """Optional request body for `POST /v1/sessions/{session_id}/saves`."""

    label: NameText | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """Optional label for save menus, 1 to 200 characters without control characters, or `null` (the
    default) for an unlabeled save."""


class BattleRoundPreviewRequest(RequestModel):
    """The declarations planned so far for a battle round, for `POST /v1/sessions/{session_id}/battle-round/preview`."""

    declarations: list[BattleDeclaration] = Field(max_length=MAX_PARTY_SIZE)
    """osrlib `BattleDeclaration` objects, at most one for each member ID in `encounter.declarers` of the player view,
    in any order. Pass any subset of the declarers, including none (`[]`). At most 20 entries."""


class CommandChoice(ApiModel):
    """One command legal in the session's current mode for the caller's role."""

    command_type: str
    """Command discriminator value, such as `"move_party"`. Pass to
    `GET /v1/rules/commands/{command_type}` for its body schema or send in
    `POST /v1/sessions/{session_id}/commands`."""
    kind: CommandKind
    """Who may send the command: `"play"` (any caller) or `"referee"` (`referee` role only)."""


class SessionCommands(ApiModel):
    """Mode, revision, and legal commands for a session."""

    mode: SessionMode
    """Current session mode."""
    revision: int
    """Current session revision. Pass as `expected_revision` on
    `POST /v1/sessions/{session_id}/commands` to guard against concurrent moves."""
    commands: list[CommandChoice]
    """Commands legal in `mode` for your API key's role, in osrlib's catalog order (`player` keys
    receive only `play` commands). Execution can still return `accepted: false` when an in-world
    condition blocks the move."""


class CommandOutcome(ApiModel):
    """Result of executing a command on `POST /v1/sessions/{session_id}/commands`."""

    accepted: bool
    """Whether osrlib accepted and applied the command. When `false`, the session state and
    `revision` are unchanged."""
    rejections: list[Rejection]
    """Rule rejections (`code` and `params`) explaining why `accepted` is `false`, or `[]` when
    `accepted` is `true`."""
    events: list[AnyEventModel]
    """Ordered events emitted by the command and any triggered adventure logic, filtered to the
    requested `visibility` (`player` by default)."""
    messages: list[str]
    """Human-readable English summary line for each entry in `events`, in matching order, formatted
    by osrlib's `format_message`."""
    mode: SessionMode
    """Session mode after the command."""
    ended: bool
    """Whether `mode` is terminal (`game_over` or `victory`)."""
    revision: int
    """Session revision after the command (incremented by 1 when `accepted` is `true`)."""
    view: PlayerView | SkipJsonSchema[None] = left_out_when_none()
    """Updated `PlayerView` at `revision` (matching `GET /v1/sessions/{session_id}/view`), with
    known map features and encounter member IDs, included only when the request passes
    `include=view`."""


class EventPage(ApiModel):
    """Slice of a session's chronological event log for one visibility level."""

    events: list[AnyEventModel | JsonObject]
    """Events after the first `after` events of the requested `visibility`, oldest first. Events
    written by a newer osrlib version without a local model class appear as raw JSON objects."""
    messages: list[str]
    """Human-readable English line for each entry in `events` (or the event's `code` for unrecognized
    event classes)."""
    next_after: int
    """Cursor value to pass as `after` on the next request to read subsequent events (`after` plus
    `len(events)`)."""
    total: int
    """Total number of events of the requested `visibility` currently in the session log."""


def session_info(row: SessionRow) -> SessionInfo:
    """Return the session info for a row of the `sessions` table.

    Args:
        row: The session's row.

    Returns:
        The info.
    """
    mode = SessionMode(row.mode)
    return SessionInfo(
        id=row.id,
        name=row.name,
        adventure_id=row.adventure_id,
        adventure_name=row.adventure_name,
        mode=mode,
        ended=mode.terminal,
        revision=row.revision,
        clock=Clock.from_rounds(row.clock_rounds),
        schema_version=row.schema_version,
        engine_version=row.engine_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def save_info(row: SaveRow) -> SaveInfo:
    """Return the save info for a row of the `saves` table.

    Args:
        row: The save's row.

    Returns:
        The info.
    """
    return SaveInfo(
        id=row.id,
        session_id=row.session_id,
        adventure_id=row.adventure_id,
        adventure_name=row.adventure_name,
        label=row.label,
        mode=SessionMode(row.mode),
        clock=Clock.from_rounds(row.clock_rounds),
        created_at=row.created_at,
    )


def _db(request: Request) -> Database:
    return request.app.state.db


def _sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def _require_visibility(visibility: Visibility, principal: Principal) -> None:
    if visibility is Visibility.REFEREE and not principal.is_referee:
        raise forbidden("referee_required", "Only an API key with the referee role can read referee events.")


def _shown(events: Iterable[_LoggedEvent], visibility: Visibility) -> list[_LoggedEvent]:
    """Return the events that a caller with `visibility` may see, in order."""
    if visibility is Visibility.REFEREE:
        return list(events)
    return [
        event
        for event in events
        if (event.visibility if isinstance(event, Event) else event.get("visibility")) == Visibility.PLAYER
    ]


def _message(event: _LoggedEvent) -> str:
    return format_message(event) if isinstance(event, Event) else str(event.get("code", ""))


def _too_large() -> ApiError:
    return invalid("party_invalid", f"A party on this server has at most {MAX_PARTY_SIZE} members.")


def _party(db: Database, principal: Principal, body: CreateSessionRequest) -> Party:
    if body.party_document is not None:
        # Count before osrlib parses the characters, so a huge document costs no more than the count.
        listed = body.party_document.get("payload")
        characters = listed.get("characters") if isinstance(listed, dict) else None
        if isinstance(characters, list) and len(characters) > MAX_PARTY_SIZE:
            raise _too_large()
        members = party_from_document(body.party_document)
    else:
        members = []
        for character_id in body.character_ids or ():
            row = db.get_character(principal.client, character_id)
            if row is None:
                raise not_found("character", character_id)
            members.append(Character.from_document(row.document))
    if len(members) > MAX_PARTY_SIZE:
        raise _too_large()
    try:
        return Party(members=members)
    except ValidationError as error:
        problems = "; ".join(entry["msg"] for entry in error.errors())
        raise invalid("party_invalid", f"The characters can't form a party. {problems}") from error


# Endpoints


@router.post(
    "",
    status_code=201,
    summary="Start a session",
    response_description="New session info in `town` mode at revision 0.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: A player sent `seed` or `party_document`. Omit them and send `character_ids`, "
                "or call with a `referee` key."
            ],
            404: [
                "`not_found`: Your client has no adventure or stored character matching an id in the request. "
                "`resource` is `adventure` or `character`, and `id` identifies the missing entry. Check "
                "`GET /v1/adventures` and `GET /v1/characters`."
            ],
            422: [
                VALIDATION_ERROR
                + " Sending both `character_ids` and `party_document`, sending neither, or passing more than 20 "
                "`character_ids` returns this code too.",
                "`party_invalid`: osrlib refuses the party (such as an empty `character_ids` list), `party_document` "
                "has more than 20 characters, or two party members share the same id.",
                "`content_invalid`: `party_document` isn't a valid party document, or the server's osrlib refuses a "
                "stored adventure uploaded under an older osrlib version. Fix the party document, or upload a fixed "
                "adventure via `POST /v1/adventures` and start the session with the new `adventure_id`.",
            ],
        }
    ),
)
@accepts_document
def create_session(request: Request, principal: CurrentPrincipal, body: CreateSessionRequest) -> SessionInfo:
    """Start a new game session of an adventure with a party of 1 to 20 characters, in `town` mode at revision 0.

    Take `adventure_id` from `GET /v1/adventures` (every server includes `builtin-barrow`) and `character_ids` from
    `POST /v1/characters` or `GET /v1/characters`. Save the returned `id`, then call
    `GET /v1/sessions/{session_id}/commands` for legal actions, `POST /v1/sessions/{session_id}/commands` to act, and
    `GET /v1/sessions/{session_id}/view` to render the screen.

    Sessions copy their party members and adventure at start, so play never mutates stored characters or adventures.
    A referee may pass `party_document` (up to `BXAPI_MAX_BODY_BYTES`, 8,388,608 bytes by default) instead of
    `character_ids`, and may choose `seed`. The server also draws a separate secret key for opaque player feature
    handles. It never exposes `seed` or that key outside `GET /v1/saves/{save_id}/document`.
    """
    seed = referee_seed(body.seed, principal)
    if body.party_document is not None and not principal.is_referee:
        raise forbidden(
            "referee_required", "Only an API key with the referee role can start a session from a party document."
        )
    db = _db(request)
    adventure = find_adventure(db, principal.client, body.adventure_id)
    party = _party(db, principal, body)
    row = _sessions(request).start(
        principal.client,
        name=body.name,
        adventure_id=body.adventure_id,
        adventure=adventure,
        party=party,
        seed=seed,
        ruleset=body.ruleset,
    )
    return session_info(row)


@router.get("", summary="List the sessions", response_description="A page of session info, newest first.")
def list_sessions(
    request: Request,
    principal: CurrentPrincipal,
    page: PageQuery,
    mode: Annotated[
        SessionMode | None,
        Query(
            description="Filter to sessions in this mode (`town`, `exploring`, `encounter`, `battle`, `game_over`, "
            "or `victory`). Omit to return sessions in all modes."
        ),
    ] = None,
) -> Page[SessionInfo]:
    """Return a page of your client's sessions, ordered newest first.

    Use this list for a game-selection menu, passing `mode` to filter (for example, `victory` for completed wins).
    Reads stored metadata directly without acquiring any session lock, so an active command on a session never blocks
    this call.
    """
    rows = _db(request).list_sessions(
        principal.client, limit=page.limit, offset=page.offset, mode=None if mode is None else mode.value
    )
    return Page(items=[session_info(row) for row in rows.items], total=rows.total, limit=page.limit, offset=page.offset)


@router.get(
    "/{session_id}",
    summary="Get a session",
    response_description="Session metadata.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_session(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> SessionInfo:
    """Return metadata for one session: `name`, `adventure_id`, `mode`, `ended`, `revision`, `clock`, and timestamps.

    Reads the `sessions` table directly without waiting on the session lock. Poll `revision` (with `If-None-Match`)
    to check whether another request advanced the game. To render what the players see in play, call
    `GET /v1/sessions/{session_id}/view`.
    """
    row = _db(request).get_session(principal.client, session_id)
    if row is None:
        raise not_found("session", session_id)
    return session_info(row)


@router.patch(
    "/{session_id}",
    summary="Rename a session",
    response_description="Updated session metadata.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def rename_session(
    request: Request, principal: CurrentPrincipal, session_id: SessionId, body: RenameSessionRequest
) -> SessionInfo:
    """Update a session's display `name` without modifying game state, `revision`, or `updated_at`."""
    db = _db(request)
    row = (
        db.get_session(principal.client, session_id)
        if db.rename_session(principal.client, session_id, body.name)
        else None
    )
    if row is None:
        raise not_found("session", session_id)
    return session_info(row)


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="Delete a session",
    response_description="The server deleted the session. The response has no body.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def delete_session(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> Response:
    """Delete a session and reclaim its bytes in your client's storage quota (`GET /v1/meta`).

    Saves created from this session remain intact: they keep `session_id` for filtering via
    `GET /v1/saves?session_id=` and can still start new sessions via `POST /v1/saves/{save_id}/restore`. Repeating a
    completed delete returns 404 `not_found`.
    """
    if not _db(request).delete_session(principal.client, session_id):
        raise not_found("session", session_id)
    _sessions(request).forget(principal.client, session_id)
    return Response(status_code=204)


@router.get(
    "/{session_id}/commands",
    summary="List the commands that are legal in a session now",
    response_description="Current `mode`, `revision`, and legal commands for your role.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def list_session_commands(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> SessionCommands:
    """Return the session's current `mode`, `revision`, and the commands legal in that mode for your API key's role.

    Call this operation to populate an action menu. Inspect `GET /v1/rules/commands/{command_type}` for a command's
    fields, then submit the command to `POST /v1/sessions/{session_id}/commands`. `player` keys receive only `play`
    commands, whereas `referee` keys receive both `play` and `referee` commands.

    Reads the `sessions` table without taking the session lock. A listed command can still return `accepted: false`
    when executed if an in-world check fails (such as walking into a wall with `move_party`). Pass the returned
    `revision` as `expected_revision` when sending the command to prevent acting on stale state.
    """
    row = _db(request).get_session(principal.client, session_id)
    if row is None:
        raise not_found("session", session_id)
    mode = SessionMode(row.mode)
    choices = []
    for command_class in ALL_COMMAND_CLASSES:
        command_type = command_class.model_fields["command_type"].default
        kind = command_kind(command_type)
        if mode in command_class.allowed_modes and (principal.is_referee or kind is CommandKind.PLAY):
            choices.append(CommandChoice(command_type=command_type, kind=kind))
    return SessionCommands(mode=mode, revision=row.revision, commands=choices)


@router.post(
    "/{session_id}/commands",
    summary="Send a command to a session",
    response_description="Command outcome, with the updated player-visible session view when `include=view` "
    "(`accepted: false` with `rejections` when refused by the B/X rules).",
    responses=problem_responses(
        {
            403: [
                "`referee_command`: A player sent a referee command (`command_type` identifies it). Check "
                "`GET /v1/sessions/{session_id}/commands` for commands your role may send. Schema validation runs "
                "first, so a malformed referee command returns 422 `validation_error` instead.",
                "`referee_required`: A player passed `visibility=referee` or set a non-`null` `source` on the "
                "command. Omit both.",
            ],
            404: [_NOT_FOUND],
            409: [
                "`revision_conflict`: `expected_revision` doesn't match the session's current `revision` (reported "
                "in `revision`). Nothing ran. Refresh `GET /v1/sessions/{session_id}/view` before deciding whether "
                "to send a new command.",
                "`session_busy`: The request waited more than 5 seconds for the session lock, or another server "
                "process updated the session concurrently. Nothing took effect, and `revision` reports the current "
                "revision. If you passed `expected_revision`, check `GET /v1/sessions/{session_id}` first and retry "
                "only when `revision` still matches `expected_revision`. Otherwise retry directly.",
            ],
            422: [
                VALIDATION_ERROR + " Missing or unknown `command_type` values return this code too.",
            ],
            500: [
                INTERNAL_ERROR,
                _UNREADABLE,
            ],
        }
    ),
)
def send_command(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    command: CommandBody,
    expected_revision: Annotated[
        int | None,
        Query(
            ge=0,
            description="Require the session to be at this exact `revision` (0 or greater) before running the "
            "command. Returns 409 `revision_conflict` if another command was already accepted. Omit to run at the "
            "current revision.",
        ),
    ] = None,
    visibility: VisibilityQuery = Visibility.PLAYER,
    include: Annotated[
        Literal["view"] | None,
        Query(
            description="Pass `view` to include the updated `PlayerView` in the response's `view` field, built while "
            "still holding the session lock at the returned `revision` (replacing a separate "
            "`GET /v1/sessions/{session_id}/view` call). Omit to leave `view` out of the response.",
        ),
    ] = None,
) -> CommandOutcome:
    """Execute one osrlib command, such as `{"command_type": "move_party", "direction": "north"}`.

    Select the command variant with `command_type` (up to 65,536 bytes). `GET /v1/sessions/{session_id}/commands`
    lists commands legal in the current mode for your role, and `GET /v1/rules/commands/{command_type}` returns each
    command's JSON Schema. Command models ignore unknown fields. Display `messages` to the player and update your UI
    from `view` (pass `include=view` to receive `PlayerView` alongside `revision` in one atomic response, or call
    `GET /v1/sessions/{session_id}/view`).

    Mapping `PlayerView` fields into common multi-field commands:

    - **Town purchases (`purchase_equipment`):** Pass a party member's `party[i].id` (such as `"character-0001"`) as
      `character_id` and repeat equipment `id` values in `item_ids` once per purchase lot (unlike `purchases` on
      `POST /v1/characters`, which takes `{item_id, quantity}` objects).
    - **Out-of-battle spells (`cast_spell`):** Pass `character_id`, `spell_id`, and `mode` set to a `SpellMode.key`
      from `GET /v1/rules/spells/{spell_id}` (such as `"heal"` on `cure_light_wounds`, not the session's
      `SessionMode`), plus target IDs in `targets` (such as `["character-0001"]` or `"cell:barrow:1:0,0"`).
    - **Battle rounds (`resolve_battle_round`):** Include exactly one `BattleDeclaration` in `declarations` for each
      member ID in `encounter.declarers`. Pass `target_group_id` from `encounter.groups[i].id` (such as
      `"group-0001"`) for `attack`, `close`, thrown splash items, and `area` or `hd_budget` spells (such as
      `fire_ball` or `sleep` with `spell_mode: "hd_budget"`). In battle, spell casts set `spell_mode` rather than
      `mode`. Spells and scrolls with `single` or `up_to_n` targeting (such as `magic_missile` with
      `spell_mode: "missiles"`) take individual `"monster-NNNN"` or `"npc-NNNN"` IDs from
      `encounter.groups[i].members` in `targets`. Use an entry whose `status` is `"alive"`.
    - **Treasure caches (`inspect_treasure`, `remove_treasure_trap`, `take_treasure`):** Pass a handle from the
      player view's `features_here` as `feature_id`. The list has the `explored[i].features[j].id` of each treasure
      cache the party can reach from where it stands. The handle starts with `feature:`. Don't parse it. A referee
      tool reads the same handle from `feature_handles` in the referee view. osrlib also accepts the feature's
      authored ID from the adventure document, a legacy form, and resolves either form only among the features at
      the party's location.

    When osrlib refuses a command under the B/X rules, this operation still returns HTTP 200 with `accepted` set to
    `false` and rule `rejections`, leaving the session and `revision` unchanged. When accepted, the server persists
    the updated state before responding, increments `revision` by 1, and returns the command's events followed by any
    triggered quest or area events (filtered by `visibility`, which defaults to `player`).

    To retry safely after a lost connection or timeout, send an `Idempotency-Key` header (which replays the original
    `CommandOutcome`) or pass `expected_revision` (which returns 409 `revision_conflict` if the first attempt
    already succeeded).
    """
    _require_visibility(visibility, principal)
    command_type = command.command_type
    if command_kind(command_type) is CommandKind.REFEREE and not principal.is_referee:
        raise forbidden(
            "referee_command",
            f"Only an API key with the referee role can send {command_type!r}.",
            command_type=command_type,
        )
    if command.source is not None and not principal.is_referee:
        raise forbidden("referee_required", "Only an API key with the referee role can set a command's source.")
    executed = _sessions(request).execute(
        principal.client, session_id, command, expected_revision=expected_revision, with_view=include == "view"
    )
    shown = [event for event in _shown(executed.result.events, visibility) if isinstance(event, Event)]
    return CommandOutcome(
        accepted=executed.result.accepted,
        rejections=list(executed.result.rejections),
        events=shown,
        messages=[format_message(event) for event in shown],
        mode=executed.mode,
        ended=executed.mode.terminal,
        revision=executed.revision,
        view=executed.view,
    )


@router.post(
    "/{session_id}/battle-round/preview",
    summary="Preview a battle round",
    response_description="osrlib's `BattleRoundPreview`: each declaration's rejections, keyed by member ID, and "
    "where the members would stand.",
    responses=problem_responses(
        {
            404: [_NOT_FOUND],
            409: [
                "`not_in_battle`: The session isn't in `battle` mode (`mode` reports its mode). Read "
                "`GET /v1/sessions/{session_id}/view` and preview a round only while `mode` is `battle`.",
                _BUSY,
            ],
            422: [
                VALIDATION_ERROR + " More than 20 `declarations` returns this code too.",
                "`declaration_invalid`: A declaration names a member who isn't in `encounter.declarers`, or two "
                "declarations name the same member. Send at most one declaration for each member in "
                "`encounter.declarers`.",
            ],
            500: [INTERNAL_ERROR, _UNREADABLE],
        }
    ),
)
def preview_battle_round(
    request: Request, principal: CurrentPrincipal, session_id: SessionId, body: BattleRoundPreviewRequest
) -> BattleRoundPreview:
    """Judge the declarations planned so far for a battle round, and show where they would leave the party.

    Call this after each choice on a battle screen, with every declaration made so far, to learn which declarations
    the session would refuse and whose they are. `rejections` has one entry for each declaration you sent, keyed by
    the declaring member's ID, with the same `code` and `params` that `resolve_battle_round` returns on
    `POST /v1/sessions/{session_id}/commands`. An empty list means the session would accept that declaration. When
    you send one declaration for each member in `encounter.declarers` and every list in `rejections` is empty,
    `resolve_battle_round` accepts the same declarations.

    `advance_feet` gives each living member's projected place on the range track once the planned moves are made,
    and `front_rank` gives each group's projected front rank, keyed by group ID. The projection leaves out what the
    monsters do, because the session decides that after it rolls initiative.

    `can_hide` lists the declarers who could declare `hide` beside the other planned declarations, and
    `can_back_stab` lists the declarers who could declare an `attack` with `back_stab`. Use them to offer those two
    choices only to a member the session would accept them from.

    The preview takes the session lock, rolls no dice, passes no game time, and changes nothing: `revision` stays the
    same, so the request takes no `expected_revision` and needs no `Idempotency-Key`.
    """

    def preview(game: GameSession) -> BattleRoundPreview:
        if game.battle is None:
            raise conflict(
                "not_in_battle",
                f"The session is in {game.mode.value!r} mode, not 'battle'.",
                mode=game.mode.value,
            )
        try:
            return battle.preview_battle_round(game, body.declarations)
        except ValueError as error:
            raise invalid("declaration_invalid", f"osrlib refused the declarations: {error}") from error

    return _sessions(request).read(principal.client, session_id, preview)


@router.get(
    "/{session_id}/view",
    summary="Get the player view",
    response_description="osrlib's player-visible session state, including known map and encounter IDs.",
    responses=problem_responses({404: [_NOT_FOUND], 409: [_BUSY], 500: [INTERNAL_ERROR, _UNREADABLE]}),
)
def get_player_view(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> PlayerView:
    """Return osrlib's `PlayerView`: everything the players may see at the session's current revision.

    Render your game screen from this object (or pass `include=view` to `POST /v1/sessions/{session_id}/commands` to
    receive `PlayerView` alongside `revision` in one response, because standalone `PlayerView` omits `revision`).
    Acquires the session lock so the snapshot always reflects state between commands. The view never includes
    unexplored cells, monster hit points, referee rolls, or the secret feature-handle key.

    Key fields for rendering a client UI:

    - `mode`: current session mode, which governs legal commands (`GET /v1/sessions/{session_id}/commands`).
    - `location`: `kind` is `"town"` in town, or `"dungeon"` with `dungeon_id`, `level_number`, `[x, y]` `position`,
      and `facing`.
    - `party`: party members in marching order, with in-session `id` (such as `"character-0001"`, passed as
      `character_id` to commands), hit points, conditions, inventory, `scores`, `armour_class`, `thac0`, `saves`,
      `movement_rate_feet`, and `thief_skills`. Combat stats include bonuses and curses from worn unidentified magic
      items while `inventory` masks unidentified names (such as `"a shield with a faint aura"`).
    - `dungeons`: IDs and names of dungeons the party has entered. `can_travel_to_town` states whether
      `travel_to_town` is currently usable from the party's position.
    - `explored`: the party's known map for each level. Each entry includes mapped `cells` and `edges`, entered
      `areas` with their names, descriptions, and walked cells, visible `features` with opaque `feature:` IDs,
      known `transitions` without destinations or gates, and `entrance` when that cell is known. A feature's
      `emptied` is `true` once the party has emptied that authored cache, and `trap_state` on a feature or an area
      is the trap's state as the party knows it. Feature cache contents, hidden discoveries, and destinations that
      the party hasn't learned stay omitted.
    - `features_here`: the feature IDs that `inspect_treasure`, `remove_treasure_trap`, and `take_treasure` accept as
      `feature_id` from where the party stands. It's empty outside `exploring` mode.
    - `spell_names`, `reversed_spell_names`, and `spell_levels`: the name and level of every spell ID the view
      shows. Each member's `spell_slots`, `preparable_spells`, and `learnable_spells` give what a `prepare_spells` or
      `learn_spell` dialog offers that member.
    - `item_names` and `item_names_plural`: what to call each item ID the view shows, in a member's `inventory` or
      in the `contents` of a pile in `piles`.
    - `encounter`: active encounter state, or `null`. Pass `declarers` to build `resolve_battle_round`, and pass a
      group's `id` as `target_group_id` for attacks, `close`, thrown splash items, and `area` or `hd_budget` spells
      (such as `fire_ball` and `sleep`). Each group also has `members` with individual `"monster-NNNN"` or
      `"npc-NNNN"` IDs and coarse `status` values. Use an `"alive"` member's `id` in `targets` for a `single` or
      `up_to_n` spell or scroll. To find catalog templates matching a group's `family_id`, search
      `GET /v1/rules/monsters` with `q` and check `family_id` on `GET /v1/rules/monsters/{monster_id}`.
    - `journal` and `quests`: discovered journal entries and active or completed quests.
    - `town_healing_prices`: gold-piece prices for temple services accepted by `purchase_healing`.
    """
    return _sessions(request).read(principal.client, session_id, build_player_view)


@router.get(
    "/{session_id}/rules/spells/{spell_id}",
    summary="Get a spell the player view names",
    response_description="osrlib's `SpellTemplate`, from the session's spell catalog.",
    responses=problem_responses(
        {
            404: [
                _NOT_FOUND,
                "`not_found`: The player view's `spell_names` has no entry for this spell id (`resource` is `spell`). "
                "Read the ids from `GET /v1/sessions/{session_id}/view`. For a built-in spell the view doesn't name, "
                "call `GET /v1/rules/spells/{spell_id}`.",
            ],
            409: [_BUSY],
            500: [INTERNAL_ERROR, _UNREADABLE],
        }
    ),
)
def get_session_spell(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    spell_id: Annotated[
        str,
        Path(description="Spell id from a key of `spell_names` in the player view, such as `magic_missile`."),
    ],
) -> SpellTemplate:
    """Return osrlib's `SpellTemplate` for a spell that the session's player view names in `spell_names`.

    A session's adventure can add spells to the built-in catalog, and `GET /v1/rules/spells/{spell_id}` serves only
    the built-in ones. This operation reads the session's own catalog, osrlib's `GameSession.effective_spells`, so
    it also answers for a spell the adventure adds. Use `level` and `spell_list` to place a spell from a member's
    `spell_book` in a `prepare_spells` dialog, and the `key` of each entry in `modes` as the `mode` of a `cast_spell`
    command.

    The operation serves the spells in `spell_names` and no others: the ones in a member's `spell_book`,
    `memorized_spells`, `preparable_spells`, or `learnable_spells`, and the ones behind an effect on a member. So it
    answers for a spell the adventure adds as soon as a member could prepare or learn it. A spell the view doesn't
    name gets 404 `not_found`, whether or not the session's catalog has it, so the response doesn't reveal a spell
    on an unidentified scroll or a spell the adventure keeps for its monsters. A player and a referee get the same
    answer.
    """

    def read(game: GameSession) -> SpellTemplate:
        if spell_id not in build_player_view(game).spell_names:
            raise not_found("spell", spell_id, among="in the session's player view")
        return game.effective_spells.get(spell_id)

    return _sessions(request).read(principal.client, session_id, read)


@router.get(
    "/{session_id}/rules/classes/{class_id}",
    summary="Get a party member's class",
    response_description="osrlib's `ClassDefinition` of the party member's class.",
    responses=problem_responses(
        {
            404: [
                _NOT_FOUND,
                "`not_found`: No party member in the player view has this `class_id` (`resource` is `class`). Read "
                "the ids from `party[i].class_id` on `GET /v1/sessions/{session_id}/view`.",
            ],
            409: [_BUSY],
            500: [INTERNAL_ERROR, _UNREADABLE],
        }
    ),
)
def get_session_class(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    class_id: Annotated[
        str,
        Path(description="Class id from `party[i].class_id` in the player view, such as `magic_user`."),
    ],
) -> ClassDefinition:
    """Return osrlib's `ClassDefinition` for the class of a party member, including a class the party brought.

    A character can have a custom class that osrlib's catalog doesn't include, and `GET /v1/rules/classes/{class_id}`
    serves only the built-in classes. This operation returns the definition osrlib uses for the member, read from
    `Character.definition`. `progression` has one row for each level, with the `spell_slots` a spell dialog needs,
    `thac0`, `saves`, and the XP the level takes.

    The operation serves the classes of the party's members and no others. When two members have the same
    `class_id`, it returns the definition of the first in marching order. A class no member has gets 404
    `not_found`. A player and a referee get the same answer.
    """

    def read(game: GameSession) -> ClassDefinition:
        shown = {member.id for member in build_player_view(game).party if member.class_id == class_id}
        for member in game.party.members:
            if member.id in shown:
                return member.definition
        raise not_found("class", class_id, among="of a member of the session's party")

    return _sessions(request).read(principal.client, session_id, read)


@router.get(
    "/{session_id}/referee-view",
    summary="Get the referee view",
    response_description="osrlib's `RefereeView` of the session.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: Only a referee may read the referee view. Players call "
                "`GET /v1/sessions/{session_id}/view`."
            ],
            404: [_NOT_FOUND],
            409: [_BUSY],
            500: [INTERNAL_ERROR, _UNREADABLE],
        }
    ),
)
def get_referee_view(request: Request, principal: RefereePrincipal, session_id: SessionId) -> RefereeView:
    """Requires a `referee` API key. Return osrlib's `RefereeView` of the session.

    Acquires the session lock to return a consistent snapshot containing the full embedded adventure, all spawned
    monsters with hit points and group `monster_ids`, active effects, trigger flags, `command_log`, and `event_log`.
    The view omits the master seed, RNG state, and secret feature-handle key. `feature_handles` maps each feature
    to the `feature:` ID the player view shows for it, keyed `"{dungeon}:{level}:{feature id}"` for an authored
    feature and by cache ID for a generated cache. Use the view to power a referee dashboard or
    choose referee commands for `POST /v1/sessions/{session_id}/commands`. To create a restorable snapshot or export
    the master seed and feature-handle key, call `POST /v1/sessions/{session_id}/saves` and
    `GET /v1/saves/{save_id}/document` instead.
    """
    return _sessions(request).read(principal.client, session_id, build_referee_view)


@router.get(
    "/{session_id}/events",
    summary="Read the event log",
    response_description="Chronological page of events and messages, with `next_after` cursor.",
    responses=problem_responses(
        {403: [_REFEREE_EVENTS], 404: [_NOT_FOUND], 409: [_BUSY], 500: [INTERNAL_ERROR, _UNREADABLE]}
    ),
)
def list_events(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    after: Annotated[
        int,
        Query(
            ge=0,
            description="Number of events of the requested `visibility` to skip (0 or greater, defaults to 0). Pass "
            "`next_after` from the previous response to fetch new events.",
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Maximum number of events to return, from 1 to 500. Defaults to 100.",
        ),
    ] = 100,
    visibility: VisibilityQuery = Visibility.PLAYER,
) -> EventPage:
    """Return a chronological page of events logged since the game began, oldest first.

    `after` counts events within the requested `visibility` (`player` by default, or `referee` with a referee key).
    Pass `next_after` from the previous page as `after` to catch up after a reconnect. You have reached the end of
    the log when `next_after` equals `total`.

    Because `POST /v1/sessions/{session_id}/commands` already returns the events caused by each command, you only
    need this endpoint when reconnecting, syncing across multiple clients, or reading events from before a save was
    restored. `CommandOutcome` omits `next_after`, and one command can emit zero or several events, so persist
    `next_after` from this endpoint rather than adding `len(CommandOutcome.events)` when other callers may also act
    on the session.
    """
    _require_visibility(visibility, principal)

    def read(game: GameSession) -> list[_LoggedEvent]:
        return _shown(game.event_log, visibility)

    shown = _sessions(request).read(principal.client, session_id, read)
    page = shown[after : after + limit]
    return EventPage(
        events=page,
        messages=[_message(event) for event in page],
        next_after=after + len(page),
        total=len(shown),
    )


@router.post(
    "/{session_id}/saves",
    status_code=201,
    summary="Save a session",
    response_description="New save metadata.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def save_session(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    body: Annotated[CreateSaveRequest, Body(default_factory=CreateSaveRequest)],
) -> SaveInfo:
    """Snapshot the session after its last accepted command into a new save and return the save's metadata.

    The request body is optional: pass `label` to name the checkpoint in save menus, or send an empty body. Reads
    the persisted session row without waiting for any command currently holding the session lock, and leaves the
    session and its `revision` unchanged. Pass the returned `id` to `POST /v1/saves/{save_id}/restore` to start a
    new session from this point, or to `GET /v1/saves/{save_id}/document` (referee only) to export the save.
    """
    db = _db(request)
    row = db.get_session_with_document(principal.client, session_id)
    if row is None:
        raise not_found("session", session_id)
    saved = db.insert_save(
        principal.client,
        session_id=row.id,
        adventure_id=row.adventure_id,
        adventure_name=row.adventure_name,
        label=body.label,
        mode=row.mode,
        clock_rounds=row.clock_rounds,
        document=row.document,
    )
    return save_info(saved)
