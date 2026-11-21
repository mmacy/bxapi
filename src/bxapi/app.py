"""The application factory.

[`create_app`][bxapi.app.create_app] builds the FastAPI application: the database, the error
handlers, the API key check, the request body limits, the cross-origin headers, compression,
conditional requests, and every router. Serve it with the `bxapi` command, or point an ASGI server at the factory:

```sh
uvicorn bxapi.app:create_app --factory --port 8640
```

The `bxapi` command sends bxapi's log lines, like the one about API keys at startup and the one
for each request, to the same place as uvicorn's. Another ASGI server shows them only if you
configure the `bxapi` logger.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bxapi import __version__
from bxapi.adventures import builtin_adventures
from bxapi.auth import AuthMiddleware, authenticate
from bxapi.caching import ConditionalGetMiddleware, HeadMiddleware
from bxapi.db import Database
from bxapi.errors import (
    INTERNAL_ERROR,
    PROBLEM_MEDIA_TYPE,
    VALIDATION_ERROR,
    content_too_large,
    install_error_handlers,
    problem_response,
    problem_responses,
    request_timeout,
    unsupported_media_type,
)
from bxapi.openapi_examples import add_examples
from bxapi.request_ids import REQUEST_ID_HEADER, RequestIdMiddleware, request_id_of
from bxapi.routers import V1_ROUTERS, meta
from bxapi.session_manager import SessionManager
from bxapi.settings import Settings, SettingsError

__all__ = ["COMPRESSION_MINIMUM_BYTES", "BodyLimitMiddleware", "create_app", "require_json_body"]

COMPRESSION_MINIMUM_BYTES = 1024
"""The smallest response body, in bytes, that the server compresses for a client that accepts gzip."""

_log = logging.getLogger("bxapi")

_DESCRIPTION = """
bxapi runs games of the 1981 B/X (Basic/Expert) fantasy adventure game over HTTP.
[osrlib](https://github.com/mmacy/osrlib-python), a Python library, applies every rule. bxapi
adds API keys, storage, and an error format. A client application draws the screen and reads the
player's input. bxapi rolls the dice, applies the rules, stores the game, and returns what
happened.

## Play a game

1. `GET /v1/meta` checks your API key and returns your client name and role.
2. `POST /v1/ability-rolls` rolls six ability scores and lists the classes they qualify for.
3. `POST /v1/characters` creates a character from the roll and the player's choices. Keep the
   character's `id`.
4. `GET /v1/adventures` lists the adventures you can play. Every server has `builtin-barrow`.
5. `POST /v1/sessions` starts a game of an adventure with a party of stored characters. Keep the
   session's `id`.
6. `GET /v1/sessions/{session_id}/commands` lists the commands that are legal now.
7. `POST /v1/sessions/{session_id}/commands` sends one command and returns what happened.
8. `GET /v1/sessions/{session_id}/view` returns what the players can see. Draw the screen from it.

Repeat steps 6 to 8 until `ended` is `true` in a command's response.

## Authentication

Send an API key as a bearer token in the `Authorization` header: `Authorization: Bearer <key>`.
Whoever runs the server sets its keys in the `BXAPI_API_KEYS` setting, and gives each key a client
name and a role. Every path under `/v1` needs a key. `GET /health`, `/docs`, `/redoc`, and
`/openapi.json` don't. A request with no key, or with a key the server doesn't have, gets 401
`unauthorized`.

A server started with no keys and with `BXAPI_ALLOW_ANONYMOUS=true` asks for no key. It treats
every caller as a referee of the client `local`, and `GET /v1/meta` returns `auth_enabled` set to
`false`.

A key has one of two roles:

- `player`: everything a game needs. In a session, a player sees only what the players may see.
- `referee`: everything a player can do, and also the referee view, referee events, referee
  commands, seeds, imports, and full adventure and save documents.

A player's request for something only a referee may do gets 403 `referee_required`, or 403
`referee_command` for a referee command.

A key identifies a client application, not a person. Every key of one client reads and changes
the same characters, adventures, sessions, and saves, and a client can't read another client's.
A request for another client's resource gets the same 404 `not_found` as an id that doesn't
exist.

## Conventions

- **Ids.** A stored resource's id is a prefix and 22 random characters: `rol_` for an ability
  roll, `chr_` for a character, `adv_` for an adventure, `ses_` for a session, and `sav_` for a
  save. The id of an adventure that comes with the server starts with `builtin-`.
- **Timestamps.** UTC, in RFC 3339 format with whole seconds, like `2026-11-20T22:01:05Z`.
- **Lists.** A list operation takes `limit` (1 to 200, default 50) and `offset` (default 0), and
  returns `items`, `total`, `limit`, and `offset`. To read the next page, add `limit` to `offset`.
  You have read the whole list when `offset` plus `limit` is `total` or more. The server lists
  stored resources newest first.
- **Documents.** A *document* is osrlib's JSON format for an adventure, a character, a party, or a
  save. It's an object with the keys `kind`, `schema_version`, `engine_version`, and `payload`.
  The server reads a document whose `schema_version` is at most the `schema_version` that
  `GET /v1/meta` returns.
- **Seeds.** osrlib draws every random number from one integer, the *seed*. The operations under
  `/v1/dice` and `/v1/generate` take an optional `seed` from any caller and return the seed they
  used. The same request with the same seed returns the same result on the same osrlib version.
  For ability rolls, characters, and sessions, only a referee may choose a seed, and the server
  never returns it to a player.
- **Unknown fields.** A request body with a field the operation doesn't define gets 422
  `validation_error`. A command and a document are osrlib's models, and osrlib ignores a field it
  doesn't define.
- **osrlib's descriptions.** The descriptions of osrlib's models, like the commands and the player
  view, were written for osrlib's Python API. Read `None` as JSON `null`, and a tuple as an array.
  Where a description says that a model raises `ValueError` or `ValidationError`, the server refuses
  the request: a command or another request body gets 422 `validation_error`, and an uploaded or
  imported document, like an adventure, gets 422 `content_invalid`.
- **JSON bodies.** Send a request body as JSON with the header `Content-Type: application/json`.
  A body with another content type, or with none, gets 415 `unsupported_media_type`.
- **Compression.** Send `Accept-Encoding: gzip`, and the server compresses every response of
  1,024 bytes or more. Most HTTP clients send the header and decompress the response for you.
- **Conditional requests.** Every 200 response to a `GET` has an `ETag` header, a short name for
  the response's content. To check whether a response you have is still current, send its `ETag`
  in `If-None-Match`. The server answers 304 with no body while the content is the same, and
  200 with the new content when it has changed. Use it to poll a session's info or view.
- **HEAD.** Every `GET` operation also answers `HEAD`, with the same status and headers and no
  body.
- **Request ids.** Every response has an `X-Request-Id` header, like
  `req_Zk3uY0mWq1x8Jr5cTt2aLw`, and the server logs the id with the request. To choose the id,
  send `X-Request-Id` with 1 to 128 letters, digits, and the characters `.`, `_`, `~`, and `-`.
  The server replaces any other value with an id it draws.
- **Rate limits.** The server has none.

## Refused commands

A command that the rules don't allow isn't an error. `POST /v1/sessions/{session_id}/commands`
returns 200 with `accepted` set to `false` and osrlib's `rejections`, and the game doesn't change.
An HTTP error status means that the request itself failed.

## Errors

Every error response is a problem document (RFC 9457) with the media type
`application/problem+json`. Branch on `code`, which doesn't change between releases. `detail` is a
sentence for a person, at most 1,000 characters. Some codes add members, which the `Problem`
schema lists. Each operation's responses list the codes it returns, with what to do about each.

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No session has the id 'ses_nope'.",
  "code": "not_found",
  "request_id": "req_Zk3uY0mWq1x8Jr5cTt2aLw",
  "resource": "session",
  "id": "ses_nope"
}
```

`request_id` is the id in the response's `X-Request-Id` header.

Three codes can succeed when you send the same request again: 409 `session_busy`, 503
`database_busy` after the one second in its `Retry-After` header, and 408 `request_timeout`. For
any other code, change the request first. For a 500, report the `request_id` to whoever runs
the server.
"""

_TAGS = [
    {
        "name": "meta",
        "description": """
The server's health check, and the versions and identity a client checks when it starts.

- `GET /health` returns `{"status": "ok"}` and needs no key. Point a load balancer's health check
  at it.
- `GET /v1/meta` returns the versions of bxapi and osrlib, whether the server asks for a key, and
  the client and role of your key.
""",
    },
    {
        "name": "rules",
        "description": """
The rules content that osrlib loads: classes, monsters, spells, equipment, magic items, languages,
the printed tables, and the commands a session accepts. Read it to build a class picker, a shop,
a bestiary, or a command menu.

Each list operation returns a page of summaries, and each get operation returns osrlib's full
model for one entry. Pass an entry's `id` from a list to the get operation of the same kind. On a
list, `q` keeps the entries whose id or name contains the text, ignoring case. Every successful
response has the header `Cache-Control: private, max-age=3600`, because the content changes only
when the server's osrlib does. Every role can read every entry, including the ones a referee keeps
from the players, like what a cursed item does.

- `GET /v1/rules/classes` and `GET /v1/rules/classes/{class_id}`: the character classes. Pass an
  `id` as `class_id` to `POST /v1/characters`.
- `GET /v1/rules/equipment` and `GET /v1/rules/equipment/{item_id}`: the weapons, armour, gear,
  and ammunition a character can buy. Pass an `id` as `item_id` in `purchases`.
- `GET /v1/rules/spells` and `GET /v1/rules/spells/{spell_id}`: the spells. Pass an `id` in
  `starting_spell_ids` for a magic-user or an elf.
- `GET /v1/rules/languages`: the languages. Pass the `id` of a language whose `choosable` is
  `true` in `extra_languages`, for a character with a high Intelligence.
- `GET /v1/rules/monsters` and `GET /v1/rules/monsters/{monster_id}`: the monsters, with their
  statistics.
- `GET /v1/rules/magic-items` and `GET /v1/rules/magic-items/{item_id}`: the magic items.
- `GET /v1/rules/tables` and `GET /v1/rules/tables/{name}`: the printed tables for abilities,
  combat, encounters, and treasure.
- `GET /v1/rules/commands` and `GET /v1/rules/commands/{command_type}`: every command a session
  accepts, with the JSON Schema of its body.
- `GET /v1/rules/license`: the Open Game License that covers the content.
""",
    },
    {
        "name": "dice",
        "description": """
Roll a dice expression, like `3d6` or `2d6+1x10`, outside a game. The response has every die and
every total. Use it for a dice roller, or for a roll that a session's rules don't make for you.

- `POST /v1/dice/roll`: an expression, how many times to roll it, and an optional seed. Returns
  the results and the seed.
""",
    },
    {
        "name": "generate",
        "description": """
Roll characters, treasure, NPC parties, stocked rooms, and monsters from the B/X tables, outside
a game. The server stores nothing. Use them for a referee's tools, or to prepare an adventure.

Each operation takes an optional `seed` and returns the seed it used. Send the same body with that
seed to get the same result again, on the same osrlib version.

- `POST /v1/generate/character`: the choices for a first-level character. Returns the character
  and every die rolled for it. To store a character for play, use `POST /v1/characters` instead.
- `POST /v1/generate/treasure`: a treasure type, like `A`. Returns the coins, gems, jewellery,
  and magic items.
- `POST /v1/generate/npc-party`: `basic` or `expert`, and a party size. Returns NPC adventurers
  with gear and treasure.
- `POST /v1/generate/room`: a dungeon level. Returns what a room on that level contains.
- `POST /v1/generate/monsters`: a monster id and a count. Returns the monsters with rolled hit
  points.
""",
    },
    {
        "name": "characters",
        "description": """
A stored character is a first-level character that the server rolled and stored, so that you can
start sessions with it. A session plays a copy, so play never changes the stored character, and
one character can start any number of sessions.

B/X players roll ability scores before they choose a class. An *ability roll* is six scores that
the server rolls and stores before the player chooses. The server rolls them from a *seed*, the
integer that osrlib draws every random number from, and stores the seed with the roll. The
character created from a roll gets its hit points and gold from the same seed, so a retry after
osrlib refuses a choice gets the same rolls.

1. `POST /v1/ability-rolls` rolls the scores. Show the player `scores` and `eligible_class_ids`.
2. `POST /v1/characters` creates the character from the roll's `id` and the player's choices.
3. `POST /v1/sessions` starts a game with the character's `id` in `character_ids`.

`GET /v1/characters` lists the stored characters, `GET /v1/characters/{character_id}` reads one,
and `DELETE /v1/characters/{character_id}` deletes one. A referee can store a character from
another server with `POST /v1/characters/import`. To roll a character without storing it, use
`POST /v1/generate/character`.
""",
    },
    {
        "name": "adventures",
        "description": """
An adventure is the content a session plays: a town, one or more dungeons, and the triggers and
quests that run in them. Every server has the adventure `builtin-barrow`, so you can start a game
without uploading anything.

An *adventure document* is osrlib's JSON format for an adventure. osr-forge, which converts
adventure module PDFs, and osr-editor, a local application for writing adventures, both write
adventure documents. They're separate projects by osrlib's author. *Adventure info* is what a
player may know before play: the title, the description, the town, and the names of the dungeons.

1. `GET /v1/adventures` lists the adventures that come with the server, then your uploads.
2. `POST /v1/adventures/validate` checks a document and stores nothing.
3. `POST /v1/adventures` checks a document and stores it.
4. `POST /v1/sessions` starts a game of an adventure by its `id`.

`GET /v1/adventures/{adventure_id}` returns one adventure's info.
`GET /v1/adventures/{adventure_id}/document` returns the whole document, for a referee only.
`DELETE /v1/adventures/{adventure_id}` deletes an upload. A session has a copy of its adventure,
so deleting the adventure doesn't change a session.
""",
    },
    {
        "name": "sessions",
        "description": """
A session is one game in progress: a party playing an adventure. The party starts in town, and
the game ends in the mode `game_over` or `victory`.

- A *command* is one action, like `{"command_type": "move_party", "direction": "east"}`. osrlib
  accepts it or refuses it.
- A *mode* is where the party is in play: `town`, `exploring`, `encounter`, `battle`,
  `game_over`, or `victory`. The mode decides which commands are legal.
- An *event* is one thing that happened. Each event is visible to the players, or to the referee
  only.
- The *revision* counts the commands that osrlib has accepted in the session. It starts at 0.
- The *player view* is what the players can see now. A client draws the screen from it.

1. `POST /v1/sessions` starts a game and returns the session's info, at revision 0.
2. `GET /v1/sessions/{session_id}/commands` lists the commands that are legal now.
   `GET /v1/rules/commands/{command_type}` has the fields of each one.
3. `POST /v1/sessions/{session_id}/commands` sends one command and returns the outcome: whether
   osrlib accepted it, the events, one line of text for each event, and the new mode and
   revision.
4. `GET /v1/sessions/{session_id}/view` returns the player view. Draw the screen from it.
5. Repeat steps 2 to 4 until `ended` is `true`.

`GET /v1/sessions/{session_id}/events` reads the event log, to catch up after a reconnect.
`POST /v1/sessions/{session_id}/saves` saves the game, and `POST /v1/saves/{save_id}/restore`
starts a new session from a save. `GET /v1/sessions` lists the sessions,
`GET /v1/sessions/{session_id}` reads one session's info, `PATCH /v1/sessions/{session_id}`
renames one, and `DELETE /v1/sessions/{session_id}` deletes one. A referee can read the whole
state with `GET /v1/sessions/{session_id}/referee-view`.

The server runs one command, view, or read of the event log at a time on each session. A request
that waits more than 5 seconds for its turn gets 409 `session_busy`. To send a command safely more
than once, like a retry after a timeout, pass `expected_revision`.
""",
    },
    {
        "name": "saves",
        "description": """
A save is a copy of a session at one moment. Restoring a save starts a new session from that
moment, and leaves the save and the original session as they were. The save's *save document* is
osrlib's JSON record of the whole game. It includes the session's seed and the state only a
referee may see, so only a referee can read or import one.

1. `POST /v1/sessions/{session_id}/saves` saves a session and returns the save's info.
2. `GET /v1/saves` lists the saves, and `GET /v1/saves/{save_id}` reads one save's info.
3. `POST /v1/saves/{save_id}/restore` starts a new session from a save.

`PATCH /v1/saves/{save_id}` changes a save's label, and `DELETE /v1/saves/{save_id}` deletes a
save. To move a game to another server, a referee reads the save document with
`GET /v1/saves/{save_id}/document` and sends it to `POST /v1/saves/import` on the other server.
""",
    },
]

_NO_KEYS = (
    "bxapi has no API keys, so it won't start. To require a key, set BXAPI_API_KEYS to a JSON list like "
    '\'[{"client": "my-game", "key": "<at least 16 characters>", "role": "player"}]\'. To run on your own machine '
    "with no key, where every caller is a referee for the client 'local', set BXAPI_ALLOW_ANONYMOUS=true."
)

_SHARED_ERRORS = problem_responses(
    {
        401: [
            "`unauthorized`: The request has no API key, or a key the server doesn't have. Send "
            "`Authorization: Bearer <key>` with a key from whoever runs the server. The same request gets the same "
            "error."
        ],
        422: [VALIDATION_ERROR],
        500: [INTERNAL_ERROR],
        503: [
            "`database_busy`: Another process held the database's write lock for more than 5 seconds. Wait the one "
            "second that the `Retry-After` header states, and send the request again."
        ],
    }
)
_SHARED_ERRORS[401]["headers"] = {
    "WWW-Authenticate": {"description": "`Bearer`, the scheme the server accepts.", "schema": {"type": "string"}}
}
_SHARED_ERRORS[503]["headers"] = {
    "Retry-After": {
        "description": "The seconds to wait before you send the request again: `1`.",
        "schema": {"type": "integer"},
    }
}

_BODY_ERRORS = {
    400: "- `bad_request`: The server can't decode the body as text, or the body has an integer with more than 4,300 "
    "digits. Send the body as JSON in UTF-8.",
    408: "- `request_timeout`: The whole body didn't arrive within the time the server waits, which its "
    "`BXAPI_BODY_TIMEOUT_SECONDS` setting sets and which is 30 seconds by default. Send the request again.",
    413: "- `content_too_large`: The body is larger than the server accepts, which its `BXAPI_MAX_BODY_BYTES` setting "
    "sets and which is 8,388,608 bytes by default. The same body gets the same error.",
    415: "- `unsupported_media_type`: The request has a body, and its `Content-Type` header doesn't say that the body "
    "is JSON. Send the body as JSON with the header `Content-Type: application/json`.",
}

_JSON_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

_SCHEMAS = "#/components/schemas/"
_PROBLEM_REF = _SCHEMAS + "Problem"
_FASTAPI_VALIDATION_REF = _SCHEMAS + "HTTPValidationError"


class BodyLimitMiddleware:
    """Refuse a request whose body is larger than the server allows or arrives too slowly.

    The middleware answers a request with a `Content-Length` over the size limit before it
    reads the body. For a request with no `Content-Length`, it counts the bytes as they arrive
    and stops reading when the count passes the limit. Either way the caller gets a 413 problem
    document with the code `content_too_large`.

    The whole body must arrive within the time limit, counted from when the application starts
    to read it. A body that takes longer gets a 408 problem document with the code
    `request_timeout`. The limit doesn't cover the request's headers or an idle connection,
    which a reverse proxy in front of the server has to limit.

    Args:
        app: The ASGI application to wrap.
        max_bytes: The largest body to accept, in bytes.
        timeout_seconds: The most time the body may take to arrive, in seconds.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, timeout_seconds: float) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application, and stop reading a body that breaks a limit."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = content_too_large(self.max_bytes)
        too_slow = request_timeout(self.timeout_seconds)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = problem_response(too_large.problem(), request_id=request_id_of(scope))
            await response(scope, receive, send)
            return
        received = 0
        complete = False
        deadline: float | None = None

        async def limited_receive() -> Message:
            nonlocal received, complete, deadline
            if complete:
                # After the body, the application waits here for the client to disconnect, with no time limit.
                return await receive()
            if deadline is None:
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            try:
                async with asyncio.timeout_at(deadline):
                    message = await receive()
            except TimeoutError:
                # FastAPI re-raises an HTTPException raised while it reads the body, and turns
                # any other exception into a 400.
                raise StarletteHTTPException(status_code=too_slow.status, detail=too_slow.detail) from None
            if message["type"] != "http.request" or not message.get("more_body", False):
                complete = True
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise StarletteHTTPException(status_code=too_large.status, detail=too_large.detail)
            return message

        await self.app(scope, limited_receive, send)


def require_json_body(request: Request) -> None:
    """Refuse a request that has a body and doesn't declare the body as JSON.

    FastAPI parses a body as JSON only when the `Content-Type` header says it's JSON. Without this
    check, a request with another content type gets a 422 about the body's type, which doesn't
    say that the header is the cause. [`create_app`][bxapi.app.create_app] makes the function a
    dependency of every route under `/v1`.

    The function accepts the media type `application/json` and any `application` type whose name
    ends in `+json`, with or without parameters like `charset`. It accepts a request with no body,
    whatever its content type, so a `POST` whose body is optional needs no header.

    Args:
        request: The request.

    Raises:
        ApiError: With status 415 and the code `unsupported_media_type`, if a `POST`, `PUT`, or
            `PATCH` request has a body and its `Content-Type` isn't a JSON media type.
    """
    headers = request.headers
    if request.method not in _JSON_BODY_METHODS:
        return
    if headers.get("content-length", "0").strip() == "0" and "transfer-encoding" not in headers:
        return
    media_type = headers.get("content-type", "").partition(";")[0].strip().lower()
    kind, _, subtype = media_type.partition("/")
    if kind != "application" or not (subtype == "json" or subtype.endswith("+json")):
        raise unsupported_media_type()


def _log_startup(settings: Settings) -> None:
    origins = ", ".join(settings.cors_origins) or "none"
    if settings.auth_enabled:
        clients = ", ".join(sorted({entry.client for entry in settings.api_keys}))
        _log.info(
            "bxapi requires an API key. Keys: %d. Clients: %s. CORS origins: %s.",
            len(settings.api_keys),
            clients,
            origins,
        )
    else:
        _log.warning(
            "bxapi asks for no API key, because BXAPI_API_KEYS is empty and BXAPI_ALLOW_ANONYMOUS is set. Every "
            "caller is a referee for the client 'local'. Keys: 0. CORS origins: %s.",
            origins,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _log_startup(app.state.settings)
    yield
    app.state.db.close()


def _operation_id(route: APIRoute) -> str:
    # The endpoint function's name, like "roll_dice", so a generated client gets short method names.
    return route.name


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": _PROBLEM_REF}}}


def _describe_problems(schema: dict[str, Any]) -> None:
    for path_name, path in schema.get("paths", {}).items():
        for operation in path.values():
            responses = operation.setdefault("responses", {})
            if path_name.startswith("/v1/") and "requestBody" in operation:
                for status, description in _BODY_ERRORS.items():
                    responses.setdefault(str(status), {"description": description, "content": _problem_content()})
            for response in responses.values():
                json_schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                if json_schema.get("$ref") in (_PROBLEM_REF, _FASTAPI_VALIDATION_REF):
                    # FastAPI documents every response model as application/json. The server
                    # sends every error as a problem document.
                    response["content"] = _problem_content()
    schemas = schema.get("components", {}).get("schemas", {})
    # HTTPValidationError refers to ValidationError, so check it first.
    for name in ("HTTPValidationError", "ValidationError"):
        rest = {
            "paths": schema.get("paths", {}),
            "schemas": {key: value for key, value in schemas.items() if key != name},
        }
        if name in schemas and _SCHEMAS + name not in set(_refs(rest)):
            del schemas[name]


def _describe_conditional_gets(schema: dict[str, Any]) -> None:
    if_none_match = {
        "name": "If-None-Match",
        "in": "header",
        "required": False,
        "description": "The `ETag` of a response you have for this request. While the content is the same, the "
        "server answers 304 with no body.",
        "schema": {"type": "string"},
        "example": 'W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"',
    }
    # One definition of the response header, which every GET operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})["ETag"] = {
        "description": 'A short name for the response\'s content, like `W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"`. '
        "Send it in `If-None-Match` to ask whether the content has changed.",
        "schema": {"type": "string"},
    }
    etag = {"$ref": "#/components/headers/ETag"}
    for path in schema.get("paths", {}).values():
        operation = path.get("get")
        if operation is None:
            continue
        operation.setdefault("parameters", []).append(dict(if_none_match))
        responses = operation.setdefault("responses", {})
        responses.setdefault("200", {}).setdefault("headers", {})["ETag"] = etag
        responses["304"] = {
            "description": "The response whose `ETag` you sent in `If-None-Match` is still current. The response "
            "has no body.",
            "headers": {"ETag": etag},
        }


def _describe_request_ids(schema: dict[str, Any]) -> None:
    # One header definition, which every response of every operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})[REQUEST_ID_HEADER] = {
        "description": "The id of the request, like `req_Zk3uY0mWq1x8Jr5cTt2aLw`. The server logs it with the "
        "request. It's the `X-Request-Id` the request sent, when that was 1 to 128 letters, digits, and the "
        "characters `.`, `_`, `~`, and `-`. Otherwise the server draws it.",
        "schema": {"type": "string"},
    }
    reference = {"$ref": f"#/components/headers/{REQUEST_ID_HEADER}"}
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            for response in operation.get("responses", {}).values():
                response.setdefault("headers", {})[REQUEST_ID_HEADER] = reference


def _discriminators(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("discriminator"), dict):
            yield value
        for item in value.values():
            yield from _discriminators(item)
    elif isinstance(value, list):
        for item in value:
            yield from _discriminators(item)


def _require_discriminators(schema: dict[str, Any]) -> None:
    # osrlib gives each tag field, like `command_type`, a default, so pydantic leaves it out of
    # `required`. A union can't be narrowed on an optional property, and the server needs the tag
    # to pick the member anyway, so this function adds the tag to `required` in each member that a
    # discriminator refers to.
    schemas = schema.get("components", {}).get("schemas", {})
    for union in list(_discriminators(schema)):
        tag = union["discriminator"]["propertyName"]
        members = set(union["discriminator"].get("mapping", {}).values())
        members.update(option.get("$ref") for option in union.get("oneOf", []) if isinstance(option, dict))
        for ref in members:
            if not isinstance(ref, str) or not ref.startswith(_SCHEMAS):
                continue
            member = schemas.get(ref.removeprefix(_SCHEMAS))
            if member is not None and tag in member.get("properties", {}):
                required = member.setdefault("required", [])
                if tag not in required:
                    required.append(tag)


def _install_openapi(app: FastAPI) -> None:
    """Make the application's OpenAPI document describe error responses as problem documents."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags,
                license_info=app.license_info,
            )
            _describe_problems(schema)
            _describe_conditional_gets(schema)
            _describe_request_ids(schema)
            _require_discriminators(schema)
            add_examples(schema)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the bxapi application.

    Call it once per process. A test builds a separate application for each test, with settings
    that point at a temporary database.

    Every route under `/v1` requires a valid API key. `GET /health`, and the documentation at
    `/docs`, `/redoc`, and `/openapi.json`, need none. A server with no API keys starts only
    when [`allow_anonymous`][bxapi.settings.Settings.allow_anonymous] is set, and then asks
    for no key.

    The function loads the adventures that come with the server, so if an adventure file is
    broken, the server stops before it serves a request. It opens the database in the settings and puts
    it at `app.state.db`, and puts the
    [`SessionManager`][bxapi.session_manager.SessionManager] that runs the sessions at
    `app.state.sessions`. It opens the database here rather than at startup, so a test client that
    never runs the application's startup can still use it. When the server starts the
    application, the application logs one line about its API keys and CORS origins, as a warning
    when it asks for no key. When the server shuts the application down, the application closes
    the database.

    Args:
        settings: The settings to run with. When it's `None`, the function reads them from the
            `BXAPI_` environment variables.

    Returns:
        The application, ready to serve.

    Raises:
        SettingsError: If the settings have no API keys and don't set `allow_anonymous`.
        DatabaseOpenError: If the database file can't be opened or has another table layout.
        osrlib.errors.ContentValidationError: If an adventure that comes with the server isn't
            valid for the installed osrlib.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:", allow_anonymous=True)))
        assert client.get("/health").json() == {"status": "ok"}
        ```
    """
    settings = settings if settings is not None else Settings()
    if not settings.auth_enabled and not settings.allow_anonymous:
        raise SettingsError(_NO_KEYS)
    builtin_adventures()
    app = FastAPI(
        title="bxapi",
        version=__version__,
        description=_DESCRIPTION,
        openapi_tags=_TAGS,
        license_info={"name": "MIT", "identifier": "MIT"},
        lifespan=_lifespan,
        generate_unique_id_function=_operation_id,
        # A path with a trailing slash is a 404, not a redirect to a path that may not exist either.
        redirect_slashes=False,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    _install_openapi(app)
    app.state.settings = settings
    app.state.db = Database(settings.database)
    app.state.sessions = SessionManager(app.state.db, cache_size=settings.session_cache_size)
    install_error_handlers(app)
    # Each middleware added wraps the ones before it. A request gets its id first. A HEAD request
    # becomes a GET, and the response loses its body last, after compression. Then the request
    # passes the CORS headers, the conditional check, the API key check, and the body limits, so
    # the server reads no body from a caller without a key.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.max_body_bytes, timeout_seconds=settings.body_timeout_seconds
    )
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(ConditionalGetMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "Retry-After", "ETag", REQUEST_ID_HEADER],
        )
    app.add_middleware(GZipMiddleware, minimum_size=COMPRESSION_MINIMUM_BYTES, compresslevel=6)
    app.add_middleware(HeadMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(meta.health_router)
    v1 = APIRouter(
        prefix="/v1", dependencies=[Depends(authenticate), Depends(require_json_body)], responses=_SHARED_ERRORS
    )
    for router in V1_ROUTERS:
        v1.include_router(router)
    app.include_router(v1)
    return app
