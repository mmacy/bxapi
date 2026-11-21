"""Games in progress: starting them, sending them commands, and reading what the players see.

The endpoints that run a game or read a running one go through the
[`SessionManager`][bxapi.session_manager.SessionManager] at `app.state.sessions`, which runs one
request at a time on each session. Session info, the session list, renaming, deleting, and saving
read and write the database directly.
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Request, Response
from osrlib.core.character import Character, party_from_document
from osrlib.core.clock import GameClock
from osrlib.core.events import Event, Visibility
from osrlib.core.ruleset import Ruleset
from osrlib.core.validation import Rejection
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, AnyCommand, Command, SessionMode
from osrlib.crawl.events import AnyEvent
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.crawl.views import PlayerView, RefereeView, build_player_view, build_referee_view
from osrlib.messages import format_message
from pydantic import Field, ValidationError, model_validator

from bxapi.adventures import find_adventure
from bxapi.auth import CurrentPrincipal, Principal, RefereePrincipal
from bxapi.commands import CommandKind, command_kind
from bxapi.db import Database, JsonObject, SaveRow, SessionRow
from bxapi.errors import INTERNAL_ERROR, VALIDATION_ERROR, ApiError, forbidden, invalid, not_found, problem_responses
from bxapi.models import ApiModel, RequestModel
from bxapi.pagination import Page, PageQuery
from bxapi.seeds import Seed, referee_seed
from bxapi.session_manager import MAX_PARTY_SIZE, SessionManager

__all__ = [
    "NAME_MAX_LENGTH",
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

router = APIRouter(prefix="/sessions", tags=["sessions"])

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
    str, Path(description="The session's id, which starts with `ses_`. `POST /v1/sessions` returns it.")
]

_NOT_FOUND = "`not_found`: Your client has no session with this id. `GET /v1/sessions` lists your sessions."
_BUSY = (
    "`session_busy`: The request waited more than 5 seconds for a command running on the session, and did nothing. "
    "`revision` is the session's revision. Send the request again."
)
_UNREADABLE = (
    "`session_unreadable`: The server's osrlib can't load the session's stored state, for example because a server "
    "with another osrlib release wrote it. Report the session's id to the server's operator."
)
_REFEREE_EVENTS = (
    "`referee_required`: A player passed `visibility=referee`. Leave `visibility` out to get the events the players "
    "may see."
)
VisibilityQuery = Annotated[
    Visibility,
    Query(
        description="Which events to return. With `player`, the response has the events the players may see. With "
        "`referee`, it has every event, and only a referee may pass `referee`."
    ),
]


# Models


class Clock(ApiModel):
    """How much game time has passed since the game began. A session restored from a save continues the save's clock."""

    rounds: int
    """The time in rounds of 10 seconds. `turns` and `days` are the same time in whole turns and whole days."""
    turns: int
    """The time in whole turns of 10 minutes."""
    days: int
    """The time in whole days."""

    @classmethod
    def from_rounds(cls, rounds: int) -> Clock:
        """Return the clock for a number of rounds, with the turns and days that osrlib's `GameClock` counts."""
        clock = GameClock(rounds=rounds)
        return cls(rounds=clock.rounds, turns=clock.turns, days=clock.days)


class SessionInfo(ApiModel):
    """A session's name, adventure, mode, revision, clock, versions, and times: what a list or a menu shows."""

    id: str
    """The session's id, which starts with `ses_`."""
    name: str
    """The session's name. It defaults to the adventure's name, and `PATCH /v1/sessions/{session_id}` changes it."""
    adventure_id: str | None
    """The id of the session's adventure, or `null` for a session restored from an imported save.

    A session restored from a save has the save's `adventure_id`, which is `null` for an imported save. The adventure
    may have been deleted since the session started. Neither a deleted adventure nor a `null` id changes play,
    because the session uses the copy of the adventure in its save document.
    """
    adventure_name: str
    """The name of the session's adventure."""
    mode: SessionMode
    """The session's mode after its last accepted command."""
    ended: bool
    """Whether the session is over, which it is in the modes `game_over` and `victory`."""
    revision: int
    """How many of the session's command requests osrlib has accepted. A new session, and a restored one, start at
    0."""
    clock: Clock
    """The game time after the last accepted command."""
    schema_version: int
    """The version of osrlib's save format that the session is stored in."""
    engine_version: str
    """The version of osrlib that last stored the session."""
    created_at: str
    """When the session was created."""
    updated_at: str
    """The time of the session's last accepted command, or `created_at` before the first one. Renaming the session
    doesn't change it."""


class SaveInfo(ApiModel):
    """A save's info, without its save document.

    The save document is osrlib's record of the whole game at that moment.
    """

    id: str
    """The save's id, which starts with `sav_`."""
    session_id: str | None
    """The session the save was made from, which may have been deleted since. `null` for an imported save."""
    adventure_id: str | None
    """The id of the adventure the save's session played, which may have been deleted since. `null` for an
    imported save, and for a save of a session restored from an imported save."""
    adventure_name: str
    """The name of the adventure in the save."""
    label: str | None
    """The label the player gave the save, or `null` if it has none."""
    mode: SessionMode
    """The session's mode when the save was made."""
    clock: Clock
    """The game time when the save was made."""
    created_at: str
    """When the save was made."""


class CreateSessionRequest(RequestModel):
    """The adventure to play, the party to play it with, and the optional choices for a new session.

    Send `character_ids` or `party_document`, and not both.
    """

    adventure_id: str = Field(min_length=1, max_length=128)
    """The id of the adventure to play: a built-in adventure, like `builtin-barrow`, or one you uploaded."""
    character_ids: list[str] | None = Field(default=None, max_length=MAX_PARTY_SIZE)
    """The ids of stored characters, in marching order: the first one walks in front. The session uses copies,
    so play doesn't change the stored characters. osrlib requires at least one member, and this server allows at
    most 20. You can name a character twice to play two copies of it, and the session gives each copy a different
    id."""
    party_document: JsonObject | None = None
    """Referee only. A party document from osrlib's `party_to_document`, in place of stored characters. It has at
    most 20 characters."""
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """A name for the session. Defaults to the adventure's name."""
    ruleset: Ruleset | None = None
    """osrlib's optional rules, as `Ruleset` fields. For each field you leave out, the session uses the rule as
    written. With no `ruleset`, the session uses every rule as written."""
    seed: Seed | None = None
    """Referee only. The session's master seed. The server draws one when you leave it out. It returns the seed only
    inside a save document, which only a referee can read."""

    @model_validator(mode="after")
    def _one_party(self) -> CreateSessionRequest:
        if (self.character_ids is None) == (self.party_document is None):
            raise ValueError("send either character_ids or party_document, and not both")
        return self


class RenameSessionRequest(RequestModel):
    """The session's new name."""

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    """The new name."""


class CreateSaveRequest(RequestModel):
    """An optional label for a new save."""

    label: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    """A label to show in a list of saves. A save without one has the label `null`."""


class CommandChoice(ApiModel):
    """A command that's legal in the session's current mode."""

    command_type: str
    """The `command_type` to send, like `"move_party"`."""
    kind: CommandKind
    """`play` for a command a player may send, and `referee` for one only a referee may send."""


class SessionCommands(ApiModel):
    """The commands that are legal in a session now, for the caller's role."""

    mode: SessionMode
    """The session's mode."""
    revision: int
    """The session's revision. Pass it as `expected_revision` to send a command only if the session has accepted no
    command since you read this list."""
    commands: list[CommandChoice]
    """Each command that's legal in the mode and that the caller may send, in osrlib's order. A player gets only
    `play` commands. osrlib can still refuse a legal command, for a reason it checks when you send the command."""


class CommandOutcome(ApiModel):
    """What happened when you sent the command. A refused command is an outcome, not an error."""

    accepted: bool
    """Whether osrlib accepted the command. When osrlib refused it, nothing in the game changed."""
    rejections: list[Rejection]
    """Why osrlib refused the command: each a `code` and its `params`. Empty when osrlib accepted the command."""
    events: list[AnyEventModel]
    """The events the command caused, in order, as osrlib serializes them: the command's events, then the events
    of what the adventure's triggers and quests did in response. With player visibility, the list leaves out the
    events only the referee may see."""
    messages: list[str]
    """One line of English for each event in `events`, in the same order, from osrlib's `format_message`."""
    mode: SessionMode
    """The session's mode after the command."""
    ended: bool
    """Whether the session is over."""
    revision: int
    """The session's revision after the command."""


class EventPage(ApiModel):
    """A page of a session's event log, for one visibility."""

    events: list[AnyEventModel | JsonObject]
    """The events that follow the first `after` events of this visibility, in the order they happened, as osrlib
    serializes them. An event that this server's osrlib has no class for, from a save a newer osrlib wrote, comes
    back as it was stored."""
    messages: list[str]
    """One line of English for each event in `events`, in the same order, from osrlib's `format_message`. For an
    event that osrlib has no class for, the line is the event's `code`."""
    next_after: int
    """The `after` to pass to read the events that come after this page."""
    total: int
    """How many events of this visibility the log has."""


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
    response_description="The new session's info, in the mode `town` at revision 0.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: A player sent `seed` or `party_document`. Leave them out and send "
                "`character_ids`, or use a key with the referee role."
            ],
            404: [
                "`not_found`: Your client has no adventure or no character with an id in the request. `resource` is "
                "`adventure` or `character`, and `id` is the id. `GET /v1/adventures` and `GET /v1/characters` list "
                "the ids."
            ],
            422: [
                VALIDATION_ERROR
                + " A request with both `character_ids` and `party_document`, or with neither, gets this code, and "
                "so does a request with more than 20 `character_ids`.",
                "`party_invalid`: osrlib refuses the party, like an empty `character_ids`, or a `party_document` has "
                "more than 20 characters or two members with the same id.",
                "`content_invalid`: The `party_document` isn't a valid party document, or the server's osrlib "
                "refuses a stored adventure that an earlier osrlib release accepted. If the party document is the "
                "cause, send a valid one. If the adventure is, upload a fixed adventure document, and start the "
                "session with the new adventure's id.",
            ],
        }
    ),
)
def create_session(request: Request, principal: CurrentPrincipal, body: CreateSessionRequest) -> SessionInfo:
    """Start a game of an adventure with a party. The session starts in town, at revision 0.

    Take `adventure_id` from `GET /v1/adventures`, and the party's ids from `POST /v1/characters`. Keep the session's
    `id`, then call `GET /v1/sessions/{session_id}/commands` for the commands that are legal in town and
    `GET /v1/sessions/{session_id}/view` to draw the screen.

    Choose the party with `character_ids`, the stored characters in marching order. The session plays copies, so
    play never changes the stored characters. A referee can send a `party_document` instead, and can choose the
    `seed`, the integer osrlib draws every random number from. The server draws a seed when the request has none.
    Only a save document contains the seed, and only a referee can read one.

    A party document is an object with `kind` set to `party`, `schema_version` and `engine_version`, and a `payload`
    whose `characters` is a list of osrlib `Character` models, like the `character` of a stored character.
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
    mode: Annotated[SessionMode | None, Query(description="Return only the sessions in this mode.")] = None,
) -> Page[SessionInfo]:
    """List your client's sessions, newest first.

    Use it for a menu of games to continue. Pass `mode` to list only the sessions in one mode, like `victory` for the
    games that ended in a win. The list reads the stored info, so it doesn't wait for a command running on a session.
    """
    rows = _db(request).list_sessions(
        principal.client, limit=page.limit, offset=page.offset, mode=None if mode is None else mode.value
    )
    return Page(items=[session_info(row) for row in rows.items], total=rows.total, limit=page.limit, offset=page.offset)


@router.get(
    "/{session_id}",
    summary="Get a session",
    response_description="The session's info.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def get_session(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> SessionInfo:
    """Return a session's info, like its mode, revision, clock, and versions.

    The info is what a menu shows, and it doesn't wait for a command running on the session. Compare `revision` with
    the revision you last saw to find out whether another request changed the game. For what the players can see,
    use `GET /v1/sessions/{session_id}/view`.
    """
    row = _db(request).get_session(principal.client, session_id)
    if row is None:
        raise not_found("session", session_id)
    return session_info(row)


@router.patch(
    "/{session_id}",
    summary="Rename a session",
    response_description="The session's info, with the new name.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def rename_session(
    request: Request, principal: CurrentPrincipal, session_id: SessionId, body: RenameSessionRequest
) -> SessionInfo:
    """Change a session's name. The game doesn't change, so `revision` and `updated_at` stay as they were."""
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
    """Delete a session. The saves made from it stay, and you can still restore them.

    The saves keep the deleted session's id in `session_id`, so `GET /v1/saves?session_id=` still finds them.
    """
    if not _db(request).delete_session(principal.client, session_id):
        raise not_found("session", session_id)
    _sessions(request).forget(principal.client, session_id)
    return Response(status_code=204)


@router.get(
    "/{session_id}/commands",
    summary="List the commands that are legal in a session now",
    response_description="The session's mode and revision, and the commands your role may send in that mode.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def list_session_commands(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> SessionCommands:
    """Return the session's mode and revision, and the commands that are legal in that mode for your role.

    Use it to build the menu of actions after each command. Read `GET /v1/rules/commands/{command_type}` for a
    command's fields, then send the command to `POST /v1/sessions/{session_id}/commands`. A player gets only `play`
    commands, and a referee gets the `referee` commands too.

    A command on the list can still be refused, for a reason osrlib checks only when you send it, like a wall in the
    way of `move_party`. Pass `revision` as `expected_revision` to send a command only if no other command was
    accepted since you read the list.
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
    response_description="The command's outcome. A command osrlib refused has `accepted` set to `false`.",
    responses=problem_responses(
        {
            403: [
                "`referee_command`: A player sent a referee command. `command_type` is the command's type. "
                "`GET /v1/sessions/{session_id}/commands` lists the commands your role may send.",
                "`referee_required`: A player passed `visibility=referee` or sent a command with a `source`. Leave "
                "them out.",
            ],
            404: [_NOT_FOUND],
            409: [
                "`revision_conflict`: You passed `expected_revision`, and the session is at another revision, which "
                "`revision` states. The server ran nothing. Read the view, then decide whether to send the command.",
                "`session_busy`: The command waited more than 5 seconds for the request ahead of it, or another "
                "server process wrote the session after this request read it. The command had no effect, and "
                "`revision` is the session's revision. If you passed `expected_revision`, read the session first, "
                "and send the command again only if the revision is the one you expected. Otherwise send it again.",
            ],
            422: [
                VALIDATION_ERROR + " An unknown or missing `command_type` gets this code too.",
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
            description="Execute the command only if the session has this revision. Pass it when you might send "
            "the same request twice, like a retry after a timeout.",
        ),
    ] = None,
    visibility: VisibilityQuery = Visibility.PLAYER,
) -> CommandOutcome:
    """Execute one osrlib command, like `{"command_type": "move_party", "direction": "north"}`, and return the outcome.

    The body is one of osrlib's commands, chosen by `command_type`. `GET /v1/sessions/{session_id}/commands` lists
    the ones that are legal now, and `GET /v1/rules/commands/{command_type}` has each one's fields. osrlib ignores a
    field that the command doesn't define. After the response, show the player `messages`, and read
    `GET /v1/sessions/{session_id}/view` to draw the screen.

    When osrlib refuses the command, the response is a 200 with `accepted` set to `false` and osrlib's
    `rejections`, and the session doesn't change. Show the player why, from each rejection's `code` and `params`.
    When osrlib accepts it, the server stores the new state before it responds, and the revision goes up by 1.
    `events` has what the command caused, then what the adventure's triggers and quests did in response.

    A player gets the events the players may see. A referee who passes `visibility=referee` gets every event.

    If the response doesn't arrive, because the connection or the server failed, the server may have applied the
    command. To retry safely, pass the revision you last saw as `expected_revision`. A retry of a command the server
    already applied then gets 409 `revision_conflict` instead of running again.
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
    executed = _sessions(request).execute(principal.client, session_id, command, expected_revision=expected_revision)
    shown = [event for event in _shown(executed.result.events, visibility) if isinstance(event, Event)]
    return CommandOutcome(
        accepted=executed.result.accepted,
        rejections=list(executed.result.rejections),
        events=shown,
        messages=[format_message(event) for event in shown],
        mode=executed.mode,
        ended=executed.mode.terminal,
        revision=executed.revision,
    )


@router.get(
    "/{session_id}/view",
    summary="Get the player view",
    response_description="osrlib's `PlayerView` of the session.",
    responses=problem_responses({404: [_NOT_FOUND], 409: [_BUSY], 500: [INTERNAL_ERROR, _UNREADABLE]}),
)
def get_player_view(request: Request, principal: CurrentPrincipal, session_id: SessionId) -> PlayerView:
    """Return osrlib's `PlayerView` of the session: what the players may see now.

    Draw the screen from it after each command. It has the party, the mapped part of the dungeon, the encounter or
    battle in progress, the journal, the active quests, and the town's prices for healing. It never has unexplored
    map, monster hit points, or the referee's rolls. The server builds the view while no command runs on the
    session, so it shows the session between two commands.

    These fields are the ones a screen usually needs:

    - `mode`: the session's mode, which decides the commands that are legal.
    - `location`: in town, `kind` is `town`. In a dungeon, it has `dungeon_id`, `level_number`, the party's
      `position` as `[x, y]`, and `facing`.
    - `party`: each member's character sheet, in marching order: `id`, conditions, inventory, hit points, and every
      other number on a B/X character sheet, like `scores`, `armour_class`, `thac0`, `saves`, and `thief_skills`. A
      command that acts on one member takes this `id`, like `character_id` in `equip_item`.
    - `explored`: for each dungeon level the party has visited, the `cells` it has seen and the `edges` between
      them. An edge's key is `x,y:side`, like `0,0:north`.
    - `encounter`: the encounter in progress, or `null`. Each of its `groups` has an `id` to put in a battle
      declaration's `target_group_id`. Show a group's `label` or `label_plural`, and read its `family_id` and
      `categories` to tell what kind of creature it is. The view doesn't say which group a monster id in a message
      belongs to. Only the referee view lists each group's `monster_ids`.
    - `journal` and `quests`: what the players have learned, and the quests they know about.
    - `town_healing_prices`: what the town's temple charges, in gold pieces, for each service that
      `purchase_healing` buys.
    """
    return _sessions(request).read(principal.client, session_id, build_player_view)


@router.get(
    "/{session_id}/referee-view",
    summary="Get the referee view",
    response_description="osrlib's `RefereeView` of the session.",
    responses=problem_responses(
        {
            403: [
                "`referee_required`: Only a referee may read the referee view. A player reads "
                "`GET /v1/sessions/{session_id}/view`."
            ],
            404: [_NOT_FOUND],
            409: [_BUSY],
            500: [INTERNAL_ERROR, _UNREADABLE],
        }
    ),
)
def get_referee_view(request: Request, principal: RefereePrincipal, session_id: SessionId) -> RefereeView:
    """Return osrlib's `RefereeView` of the session: the whole state except the master seed and the dice streams.

    Only a referee may read it. Use it for a referee's screen, or to decide which referee command to send. It has the
    whole adventure, every monster with its hit points, the flags the triggers set, and the logs of every command and
    event. To save the whole state, use `POST /v1/sessions/{session_id}/saves` instead.
    """
    return _sessions(request).read(principal.client, session_id, build_referee_view)


@router.get(
    "/{session_id}/events",
    summary="Read the event log",
    response_description="A page of events, oldest first, with the `after` to pass next.",
    responses=problem_responses(
        {403: [_REFEREE_EVENTS], 404: [_NOT_FOUND], 409: [_BUSY], 500: [INTERNAL_ERROR, _UNREADABLE]}
    ),
)
def list_events(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    after: Annotated[
        int, Query(ge=0, description="How many events of this visibility you've already read. Start at 0.")
    ] = 0,
    limit: Annotated[int, Query(ge=1, le=500, description="The largest number of events to return.")] = 100,
    visibility: VisibilityQuery = Visibility.PLAYER,
) -> EventPage:
    """Return a page of the events the session has logged since it started, oldest first.

    `after` counts the events of the requested visibility that you've read, so pass the `next_after` of the last
    page to read what's new. A client that reconnects reads from its last `next_after` to catch up. You've read every
    event when `next_after` equals `total`.

    Each command's response already has the events it caused, so a client that reads every response doesn't need the
    log. The log has the events from the session's start, including the ones from before a restore.
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
    response_description="The new save's info.",
    responses=problem_responses({404: [_NOT_FOUND]}),
)
def save_session(
    request: Request,
    principal: CurrentPrincipal,
    session_id: SessionId,
    body: Annotated[CreateSaveRequest, Body(default_factory=CreateSaveRequest)],
) -> SaveInfo:
    """Store a copy of the session as it is after its last accepted command, and return the save's info.

    The body is optional. Send a `label` to show in a list of saves. Saving doesn't change the session or its
    revision, and it doesn't wait for a command running on the session. Restore the save with
    `POST /v1/saves/{save_id}/restore` to start a new session from this moment. A session can have any number of
    saves.
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
