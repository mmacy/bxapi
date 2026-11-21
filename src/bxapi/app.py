"""The application factory.

[`create_app`][bxapi.app.create_app] builds the FastAPI application: the database, the error
handlers, the API key check, the request body limits, the cross-origin headers, the browser
security headers, compression, conditional requests, and every router. Serve it with the `bxapi`
command, or point an ASGI server at the factory:

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
from bxapi.docs_pages import install_docs_pages
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
from bxapi.idempotency import IDEMPOTENCY_KEY_HEADER, REPLAYED_HEADER
from bxapi.json_body import MAX_JSON_DEPTH, SMALL_BODY_BYTES, body_limit
from bxapi.openapi_examples import add_examples
from bxapi.rate_limit import RateLimiter
from bxapi.request_ids import REQUEST_ID_HEADER, RequestIdMiddleware, request_id_of
from bxapi.routers import V1_ROUTERS, meta
from bxapi.security_headers import SecurityHeadersMiddleware
from bxapi.session_manager import SessionManager
from bxapi.settings import Settings, SettingsError

__all__ = ["COMPRESSION_MINIMUM_BYTES", "BodyLimitMiddleware", "create_app", "require_json_body"]

COMPRESSION_MINIMUM_BYTES = 1024
"""The smallest response body, in bytes, that the server compresses for a client that accepts gzip."""

_log = logging.getLogger("bxapi")

_DESCRIPTION = """
bxapi runs games of B/X (Basic/Expert), the fantasy adventure game published in 1981, over HTTP.
[osrlib](https://github.com/mmacy/osrlib-python), a Python library, applies every rule, and bxapi
adds API keys, storage, and a standard error format. Your client application draws the screen and
reads the player's input, while bxapi rolls the dice, applies the rules, stores the game, and
returns what happened.

## Play a game

A game client calls these operations, in this order:

1. `GET /v1/meta` verifies your API key and returns your client name and role.
2. `POST /v1/ability-rolls` rolls six ability scores and lists the classes they qualify for.
3. `POST /v1/characters` creates a character from the roll and the player's choices. Save the
   character's `id`.
4. `GET /v1/adventures` lists the adventures you can play. Every server includes
   `builtin-barrow`.
5. `POST /v1/sessions` starts a game of an adventure with a party of stored characters. Save the
   session's `id`.
6. `GET /v1/sessions/{session_id}/commands` lists the commands that are currently legal.
7. `POST /v1/sessions/{session_id}/commands` sends one command and returns its outcome.
8. `GET /v1/sessions/{session_id}/view` returns what the players can see, which your client
   renders. To receive the view in step 7's response instead, pass `include=view` on that request.

Repeat steps 6 through 8 until a command's response has `ended` set to `true`.

## Authentication

Send an API key as a bearer token in the `Authorization` header: `Authorization: Bearer <key>`.
The server operator configures the keys in the `BXAPI_API_KEYS` setting and assigns each key a
client name and a role. Every path under `/v1` requires a key. The only paths that need no key are
`GET /health`, `/docs`, `/redoc`, and `/openapi.json`. The server returns `401 unauthorized` for a
request with no key or with a key it doesn't recognize.

A server started with no keys and with `BXAPI_ALLOW_ANONYMOUS=true` runs in anonymous mode and
doesn't require an API key. It treats every caller as a referee for the client `local`, and
`GET /v1/meta` returns `auth_enabled` set to `false`. An anonymous-mode server answers a request
under `/v1` only when the request's `Host` header is `localhost`, `127.0.0.1`, or `[::1]`, with or
without a port. It returns `421 misdirected_request` for any other host.

Each key has one of two roles:

- `player`: Grants everything a game client needs. In a session, a player key sees only what the
  players may see.
- `referee`: Grants everything a player key can do, plus the referee view, referee events,
  referee commands, seeds, imports, and full adventure and save documents.

The server returns `403 referee_required` when a player key requests something only a referee may
do, or `403 referee_command` when a player key sends a referee command.

A key identifies a client application, not a person. Every key of one client reads and changes
the same characters, adventures, sessions, and saves, and no client can read another client's
data. A request for another client's resource receives the same `404 not_found` as a request for
an ID that doesn't exist.

## Conventions

- **IDs.** A stored resource's ID is a prefix followed by 22 random characters: `rol_` for an
  ability roll, `chr_` for a character, `adv_` for an adventure, `ses_` for a session, and `sav_`
  for a save. The ID of a built-in adventure starts with `builtin-`.
- **Timestamps.** Timestamps are UTC in RFC 3339 format with whole seconds, such as
  `2026-11-20T22:01:05Z`.
- **Lists.** A list operation takes `limit` (1 to 200, default 50) and `offset` (default 0), and
  returns `items`, `total`, `limit`, and `offset`. To read the next page, add `limit` to `offset`.
  You've read the whole list when `offset` plus `limit` is `total` or more. The server lists
  stored resources newest first.
- **Documents.** A *document* is osrlib's JSON format for an adventure, a character, a party, or a
  save: an object with the keys `kind`, `schema_version`, `engine_version`, and `payload`. The
  server reads a document whose `schema_version` is no higher than the `schema_version` that
  `GET /v1/meta` returns.
- **Seeds.** osrlib derives every random number from one integer, the *seed*. The operations under
  `/v1/dice` and `/v1/generate` accept an optional `seed` from any caller and return the seed they
  used. The same request with the same seed returns the same result on the same osrlib version.
  For ability rolls, characters, and sessions, only a referee may choose a seed, and the server
  never returns it to a player.
- **Unknown fields.** The server returns `422 validation_error` for a request body with a field
  the operation doesn't define. Commands and documents are the exception: they're osrlib models,
  and osrlib ignores a field it doesn't define.
- **osrlib's descriptions.** The descriptions of osrlib's models, such as the commands and the
  player view, were written for osrlib's Python API. Read `None` as JSON `null`, and a tuple as an
  array. Where a description says that a model raises `ValueError` or `ValidationError`, the
  server rejects the request. It returns `422 validation_error` for a command or another request
  body, and `422 content_invalid` for an uploaded or imported document, such as an adventure.
- **JSON bodies.** Send a request body as JSON with the header `Content-Type: application/json`.
  The server returns `415 unsupported_media_type` for a body with another content type or with
  none. It returns `400 bad_request` for a body that contains `NaN`, `Infinity`, a number too
  large for a 64-bit float, a string with half of a UTF-16 surrogate pair, or arrays and objects
  nested more than 100 levels deep.
- **Body size.** A body is at most 65,536 bytes, except on the five operations that accept an
  osrlib document: `POST /v1/adventures`, `POST /v1/adventures/validate`,
  `POST /v1/characters/import`, `POST /v1/saves/import`, and `POST /v1/sessions`. The limit for
  those five is the server's `BXAPI_MAX_BODY_BYTES` setting, which defaults to 8,388,608 bytes.
  The server returns `413 content_too_large` for a larger body.
- **Compression.** The server compresses every response of 1,024 bytes or more with gzip when the
  request has the header `Accept-Encoding: gzip`. Most HTTP clients send the header and decompress
  the response for you.
- **Conditional requests.** Every `200` response to a `GET` has an `ETag` header, an identifier
  for the response's content. To check whether a response you hold is still current, send its
  `ETag` in `If-None-Match`. The server returns `304` with no body while the content is unchanged,
  and `200` with the new content after it changes. Use conditional requests to poll a session's
  info or view.
- **HEAD.** Every `GET` operation also answers `HEAD`, with the same status and headers and no
  body.
- **Idempotency keys.** To retry a `POST` without running it twice, put a value you choose, such
  as a UUID, in the `Idempotency-Key` header of the first request and of every retry. The server
  runs the first request and stores a `2xx` response for 24 hours. A retry with the same key
  receives the stored response, with the header `Idempotent-Replayed: true`. A request that fails
  with any problem other than `500 internal_error` changed nothing, so the server discards its
  key, and the same key runs the request again. A key is 1 to 255 printable ASCII characters with
  no spaces, and it's scoped to your client.
- **Request IDs.** Every response has an `X-Request-Id` header, such as
  `req_Zk3uY0mWq1x8Jr5cTt2aLw`, and the server logs the ID with the request. To choose the ID
  yourself, send `X-Request-Id` with 1 to 128 letters, digits, and the characters `.`, `_`, `~`,
  and `-`. The server replaces any other value with an ID it generates.
- **Storage.** The server counts the bytes of the documents it stores for your client:
  characters, ability rolls, adventures, sessions, saves, and the responses it stores for
  idempotency keys. `GET /v1/meta` returns the count and the limit. The limit is configured by the
  server's `BXAPI_MAX_CLIENT_BYTES` setting and defaults to 1,073,741,824 bytes. After the count
  reaches the limit, each request that would store more receives `507 storage_full` until you
  delete something.
- **Rate limits.** Each client may send as many requests per minute as the server's
  `BXAPI_RATE_LIMIT_PER_MINUTE` setting allows, which defaults to 600. You can send that many at
  once, and the allowance refills at a steady rate. A request that exceeds the limit receives
  `429 rate_limited`, with the number of seconds to wait in the `Retry-After` header.

## Rejected commands

A command that the rules don't allow isn't an error. `POST /v1/sessions/{session_id}/commands`
returns `200` with `accepted` set to `false` and osrlib's `rejections`, and the game doesn't
change. An HTTP error status means that the request itself failed.

## Errors

Every error response is a problem document (RFC 9457) with the media type
`application/problem+json`. Branch on `code`, which doesn't change between releases. `detail` is a
sentence written for a person, at most 1,000 characters long. Some codes add fields, which the
`Problem` schema lists. Each operation's responses list the codes it returns and what to do about
each one.

The following problem document reports a request for a session that doesn't exist:

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

`request_id` matches the ID in the response's `X-Request-Id` header.

Five codes can succeed when you send the same request again. Three of them state how many seconds
to wait in a `Retry-After` header: `409 idempotency_key_in_progress`, `429 rate_limited`, and
`503 database_busy`. The other two are `409 session_busy` and `408 request_timeout`. Change the
request before you resend it for any other code. If no response arrives at all, send the same
`POST` again with the same `Idempotency-Key`. Report the `request_id` of a `500` to the server
operator.
"""

_TAGS = [
    {
        "name": "meta",
        "description": """
The server's health check, plus the versions and identity that a client verifies when it starts.

- `GET /health` returns `{"status": "ok"}` and requires no key. Point a load balancer's health
  check at it.
- `GET /v1/meta` returns the versions of bxapi and osrlib, whether the server requires a key, and
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
list, `q` keeps the entries whose ID or name contains the text, ignoring case. Every successful
response has the header `Cache-Control: private, max-age=3600`, because the content changes only
when the server's osrlib version changes. Every role can read every entry, including the ones a
referee keeps from the players, such as what a cursed item does.

The rules reference covers these kinds of content:

- `GET /v1/rules/classes` and `GET /v1/rules/classes/{class_id}`: The character classes. Pass an
  `id` as `class_id` to `POST /v1/characters`.
- `GET /v1/rules/equipment` and `GET /v1/rules/equipment/{item_id}`: The weapons, armour, gear,
  and ammunition a character can buy. Pass an `id` as `item_id` in `purchases`.
- `GET /v1/rules/spells` and `GET /v1/rules/spells/{spell_id}`: The spells. Pass an `id` in
  `starting_spell_ids` for a magic-user or an elf.
- `GET /v1/rules/languages`: The languages. For a character with a high Intelligence, pass the
  `id` of a language whose `choosable` is `true` in `extra_languages`.
- `GET /v1/rules/monsters` and `GET /v1/rules/monsters/{monster_id}`: The monsters, with their
  statistics.
- `GET /v1/rules/magic-items` and `GET /v1/rules/magic-items/{item_id}`: The magic items.
- `GET /v1/rules/tables` and `GET /v1/rules/tables/{name}`: The printed tables for abilities,
  combat, encounters, and treasure.
- `GET /v1/rules/commands` and `GET /v1/rules/commands/{command_type}`: Every command a session
  accepts, with the JSON Schema of its body.
- `GET /v1/rules/license`: The Open Game License that covers the content.
""",
    },
    {
        "name": "dice",
        "description": """
Roll a dice expression, such as `3d6` or `2d6+1x10`, outside a game. The response has every die
and every total. Use it for a dice roller, or for a roll that a session's rules don't make for
you.

- `POST /v1/dice/roll` takes an expression, the number of times to roll it, and an optional seed.
  It returns the results and the seed.
""",
    },
    {
        "name": "generate",
        "description": """
Roll characters, treasure, NPC parties, stocked rooms, and monsters from the B/X tables, outside
a game. The server stores nothing. Use the generators to build tools for a referee or to prepare
an adventure.

Each operation takes an optional `seed` and returns the seed it used. Send the same body with that
seed to get the same result again on the same osrlib version.

Each generator takes a few inputs and returns what osrlib rolled:

- `POST /v1/generate/character`: Takes the choices for a first-level character. Returns the
  character and every die rolled for it. To store a character for play, use `POST /v1/characters`
  instead.
- `POST /v1/generate/treasure`: Takes a treasure type, such as `A`. Returns the coins, gems,
  jewellery, and magic items.
- `POST /v1/generate/npc-party`: Takes `basic` or `expert`, and a party size. Returns NPC
  adventurers with gear and treasure.
- `POST /v1/generate/room`: Takes a dungeon level. Returns what a room on that level contains.
- `POST /v1/generate/monsters`: Takes a monster ID and a count. Returns the monsters with rolled
  hit points.
""",
    },
    {
        "name": "characters",
        "description": """
A stored character is a first-level character that the server rolled and stored so that you can
start sessions with it. A session plays a copy, so play never changes the stored character, and
one character can start any number of sessions.

B/X players roll ability scores before they choose a class. An *ability roll* is six scores that
the server rolls and stores before the player chooses. The server rolls them from a *seed*, the
integer from which osrlib derives every random number, and stores the seed with the roll. The
character created from a roll gets its hit points and gold from the same seed, so a retry after
osrlib rejects a choice produces the same rolls.

To create a character and play it, call these operations in order:

1. `POST /v1/ability-rolls` rolls the scores. Show the player `scores` and `eligible_class_ids`.
2. `POST /v1/characters` creates the character from the roll's `id` and the player's choices.
3. `POST /v1/sessions` starts a game with the character's `id` in `character_ids`.

`GET /v1/characters` lists the stored characters, `GET /v1/characters/{character_id}` returns one,
and `DELETE /v1/characters/{character_id}` deletes one. A referee can store a character from
another server with `POST /v1/characters/import`. To roll a character without storing it, use
`POST /v1/generate/character`.
""",
    },
    {
        "name": "adventures",
        "description": """
An adventure is the content a session plays: a town, one or more dungeons, and the triggers and
quests that run in them. Every server includes the adventure `builtin-barrow`, so you can start a
game without uploading anything.

An *adventure document* is osrlib's JSON format for an adventure. Two separate projects by
osrlib's author write adventure documents: osr-forge, which converts adventure module PDFs, and
osr-editor, a local application for writing adventures. *Adventure info* is what a player may know
before play: the title, the description, the town, and the names of the dungeons.

To upload an adventure and play it, call these operations in order:

1. `GET /v1/adventures` lists the built-in adventures, then your uploads.
2. `POST /v1/adventures/validate` checks a document and stores nothing.
3. `POST /v1/adventures` checks a document and stores it.
4. `POST /v1/sessions` starts a game of an adventure by its `id`.

`GET /v1/adventures/{adventure_id}` returns one adventure's info.
`GET /v1/adventures/{adventure_id}/document` returns the whole document, to a referee only.
`DELETE /v1/adventures/{adventure_id}` deletes an upload. Each session has its own copy of its
adventure, so deleting the adventure doesn't change a session.
""",
    },
    {
        "name": "sessions",
        "description": """
A session is one game in progress: a party playing an adventure. The party starts in town, and
the game ends in the mode `game_over` or `victory`.

Five terms describe a session:

- A *command* is one action, such as `{"command_type": "move_party", "direction": "east"}`. osrlib
  accepts it or rejects it.
- A *mode* is where the party is in play: `town`, `exploring`, `encounter`, `battle`,
  `game_over`, or `victory`. The mode determines which commands are legal.
- An *event* is one thing that happened. Each event is visible either to the players or to the
  referee only.
- The *revision* counts the commands that osrlib has accepted in the session. It starts at 0.
- The *player view* is what the players can currently see. A client draws the screen from it.

To play a session, call these operations in order:

1. `POST /v1/sessions` starts a game and returns the session's info, at revision 0.
2. `GET /v1/sessions/{session_id}/commands` lists the commands that are currently legal.
   `GET /v1/rules/commands/{command_type}` describes the fields of each one.
3. `POST /v1/sessions/{session_id}/commands` sends one command and returns the outcome: whether
   osrlib accepted it, the events, one line of text for each event, and the new mode and
   revision.
4. `GET /v1/sessions/{session_id}/view` returns the player view, which your client renders. With
   `include=view`, step 3's response includes the view, and you can skip this step.
5. Repeat steps 2 through 4 until `ended` is `true`.

`GET /v1/sessions/{session_id}/events` returns the event log, so a client can catch up after it
reconnects. `POST /v1/sessions/{session_id}/saves` saves the game, and
`POST /v1/saves/{save_id}/restore` starts a new session from a save. `GET /v1/sessions` lists the
sessions, `GET /v1/sessions/{session_id}` returns one session's info,
`PATCH /v1/sessions/{session_id}` renames one, and `DELETE /v1/sessions/{session_id}` deletes one.
A referee can read the whole state with `GET /v1/sessions/{session_id}/referee-view`.

The server runs one command, view, or read of the event log at a time on each session. A request
that waits more than 5 seconds for its turn receives `409 session_busy`. To send a command safely
more than once, such as a retry after a timeout, pass `expected_revision`.
""",
    },
    {
        "name": "saves",
        "description": """
A save is a copy of a session at one moment. Restoring a save starts a new session from that
moment, and leaves the save and the original session unchanged. A *save document* is osrlib's JSON
record of the whole game. It includes the session's seed and the state only a referee may see, so
only a referee can read or import one.

To save a session and restore it, call these operations in order:

1. `POST /v1/sessions/{session_id}/saves` saves a session and returns the save's info.
2. `GET /v1/saves` lists the saves, and `GET /v1/saves/{save_id}` returns one save's info.
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
            "`unauthorized`: The request has no API key, or it has a key the server doesn't recognize. Send "
            "`Authorization: Bearer <key>` with a key from the server operator. Resending the same request returns "
            "the same error."
        ],
        421: [
            "`misdirected_request`: The server runs in anonymous mode, which requires no API key, and the request's "
            "`Host` header isn't `localhost`, `127.0.0.1`, or `[::1]`. Call the server by one of those names, or ask "
            "the server operator to configure API keys. A server with API keys never returns this code."
        ],
        422: [VALIDATION_ERROR],
        429: [
            "`rate_limited`: Your client sent more requests per minute than the server allows. The limit is "
            "configured by the server's `BXAPI_RATE_LIMIT_PER_MINUTE` setting and defaults to 600. Wait the number "
            "of seconds in the `Retry-After` header, then send the request again. Requests with no valid API key "
            "share one limit, and the server returns this code instead of `401 unauthorized` when they exceed it."
        ],
        500: [INTERNAL_ERROR],
        503: [
            "`database_busy`: Another process held the database's write lock for more than 5 seconds. Wait the one "
            "second that the `Retry-After` header states, then send the request again."
        ],
    }
)
_SHARED_ERRORS[401]["headers"] = {
    "WWW-Authenticate": {
        "description": "`Bearer`, the authentication scheme the server accepts.",
        "schema": {"type": "string"},
    }
}
_SHARED_ERRORS[429]["headers"] = {
    "Retry-After": {
        "description": "The number of seconds to wait before you send the request again, at least `1`.",
        "schema": {"type": "integer"},
    }
}
_SHARED_ERRORS[503]["headers"] = {
    "Retry-After": {
        "description": "The number of seconds to wait before you send the request again: `1`.",
        "schema": {"type": "integer"},
    }
}

_BODY_ERRORS = {
    400: "- `bad_request`: The server can't decode the body as text, or the body contains a value the server rejects: "
    "an integer with more than 4,300 digits, `NaN`, `Infinity`, a number too large for a 64-bit float, a string with "
    f"half of a UTF-16 surrogate pair, or arrays and objects nested more than {MAX_JSON_DEPTH} levels deep. `detail` "
    "states which. Send the body as UTF-8 JSON without those values.",
    408: "- `request_timeout`: The whole body didn't arrive within the server's body timeout, which is configured by "
    "its `BXAPI_BODY_TIMEOUT_SECONDS` setting and defaults to 30 seconds. Send the request again.",
    413: "- `content_too_large`: The body exceeds the limit for an operation that accepts a document. The limit is "
    "configured by the server's `BXAPI_MAX_BODY_BYTES` setting and defaults to 8,388,608 bytes. Resending the same "
    "body returns the same error.",
    415: "- `unsupported_media_type`: The request has a body, but its `Content-Type` header doesn't declare the body "
    "as JSON. Send the body as JSON with the header `Content-Type: application/json`.",
}

_SMALL_BODY_TOO_LARGE = (
    f"- `content_too_large`: The body exceeds {SMALL_BODY_BYTES:,} bytes, the limit for an operation that accepts no "
    "document, or it exceeds the server's `BXAPI_MAX_BODY_BYTES` setting when that setting is smaller. Resending the "
    "same body returns the same error."
)

_JSON_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

_OPENAPI_URL = "/openapi.json"

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


async def require_json_body(request: Request) -> None:
    """Refuse a request that has a body and doesn't declare the body as JSON.

    FastAPI parses a body as JSON only when the `Content-Type` header says it's JSON. Without this
    check, a request with another content type gets a 422 about the body's type, which doesn't
    say that the header is the cause. [`create_app`][bxapi.app.create_app] makes the function a
    dependency of every route under `/v1`.

    The function accepts the media type `application/json` and any `application` type whose name
    ends in `+json`, with or without parameters like `charset`. It accepts a request with an
    empty body, whatever its content type and however the client framed it, so a `POST` whose
    body is optional needs no header. The function is `async` only to read the body, which
    Starlette keeps for the endpoint. It does no other work.

    Args:
        request: The request.

    Raises:
        ApiError: With status 415 and the code `unsupported_media_type`, if a `POST`, `PUT`, or
            `PATCH` request has a body and its `Content-Type` isn't a JSON media type.
    """
    if request.method not in _JSON_BODY_METHODS or not await request.body():
        return
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
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


def _describe_problems(schema: dict[str, Any], small_body_operations: frozenset[str]) -> None:
    for path_name, path in schema.get("paths", {}).items():
        for operation in path.values():
            responses = operation.setdefault("responses", {})
            if path_name.startswith("/v1/") and "requestBody" in operation:
                for status, description in _BODY_ERRORS.items():
                    if status == 413 and operation.get("operationId") in small_body_operations:
                        description = _SMALL_BODY_TOO_LARGE
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


_IDEMPOTENCY_ERRORS = {
    "409": [
        "`idempotency_key_in_progress`: The first request with this `Idempotency-Key` is still running. Wait the one "
        "second that the `Retry-After` header states, then send the request again.",
        "`idempotency_key_interrupted`: The server failed or stopped while it handled the first request with this "
        "`Idempotency-Key`, and it has no record of whether that request took effect. Read the resource the request "
        "would have changed. If the request didn't take effect, send it again with a new key.",
    ],
    "422": [
        "`idempotency_key_reused`: Your client already sent this `Idempotency-Key` with a different request: one "
        "with a different method, path, query, body, or role. Send each new request with a new key.",
    ],
}


def _describe_idempotency(schema: dict[str, Any]) -> None:
    key = {
        "name": IDEMPOTENCY_KEY_HEADER,
        "in": "header",
        "required": False,
        "description": "A value you choose, such as a UUID, that identifies this request: 1 to 255 printable ASCII "
        "characters with no spaces. Send the same key when you retry the request, and the server returns the first "
        "response without running the request twice. The server remembers a key for 24 hours.",
        "schema": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[!-~]+$"},
        "example": "0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11",
    }
    # One definition of the response header, which every POST operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})[REPLAYED_HEADER] = {
        "description": "`true` when the response is the stored response to an earlier request with the same "
        "`Idempotency-Key`. The server omits the header from the response to the first request.",
        "schema": {"type": "string", "enum": ["true"]},
    }
    replayed = {"$ref": f"#/components/headers/{REPLAYED_HEADER}"}
    for path_name, path in schema.get("paths", {}).items():
        operation = path.get("post")
        if operation is None or not path_name.startswith("/v1/"):
            continue
        operation.setdefault("parameters", []).append(dict(key))
        responses = operation.setdefault("responses", {})
        for status, response in responses.items():
            if status.startswith("2"):
                response.setdefault("headers", {})[REPLAYED_HEADER] = replayed
        for status, entries in _IDEMPOTENCY_ERRORS.items():
            listed = "\n".join(f"- {entry}" for entry in entries)
            response = responses.setdefault(status, {"description": "", "content": _problem_content()})
            response["description"] = "\n".join(part for part in (response["description"], listed) if part)
        retry_after = responses["409"].setdefault("headers", {})
        retry_after["Retry-After"] = {
            "description": "With `idempotency_key_in_progress`, the number of seconds to wait before you send the "
            "request again: `1`.",
            "schema": {"type": "integer"},
        }


_STORAGE_FULL = (
    "- `storage_full`: The documents the server stores for your client have reached its storage limit, which is "
    "configured by the server's `BXAPI_MAX_CLIENT_BYTES` setting and defaults to 1,073,741,824 bytes. `used_bytes` "
    "and `max_bytes` state the count and the limit. The request stored nothing. Delete sessions, saves, adventures, "
    "or characters you no longer need, then send the request again. An operation that stores nothing, such as a dice "
    "roll, returns this code only for a request with an `Idempotency-Key`, because the server stores the response."
)


def _describe_storage(schema: dict[str, Any]) -> None:
    # Every operation that stores a document is a POST, and any POST with an idempotency key stores its response.
    for path_name, path in schema.get("paths", {}).items():
        operation = path.get("post")
        if operation is not None and path_name.startswith("/v1/"):
            operation.setdefault("responses", {})["507"] = {"description": _STORAGE_FULL, "content": _problem_content()}


def _describe_conditional_gets(schema: dict[str, Any]) -> None:
    if_none_match = {
        "name": "If-None-Match",
        "in": "header",
        "required": False,
        "description": "The `ETag` of a response you hold for this request. While the content is unchanged, the "
        "server returns `304` with no body.",
        "schema": {"type": "string"},
        "example": 'W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"',
    }
    # One definition of the response header, which every GET operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})["ETag"] = {
        "description": 'An identifier for the response\'s content, such as `W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"`. '
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
        "description": "The ID of the request, such as `req_Zk3uY0mWq1x8Jr5cTt2aLw`. The server logs it with the "
        "request. The server uses the `X-Request-Id` that the request sent when that value is 1 to 128 letters, "
        "digits, and the characters `.`, `_`, `~`, and `-`. Otherwise, the server generates the ID.",
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
            small_body_operations = frozenset(
                route.name
                for router in V1_ROUTERS
                for route in router.routes
                if isinstance(route, APIRoute) and body_limit(route.endpoint) is not None
            )
            _describe_problems(schema, small_body_operations)
            _describe_idempotency(schema)
            _describe_storage(schema)
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
    when [`allow_anonymous`][bxapi.settings.Settings.allow_anonymous] is set. It then asks for no
    key, and answers a request under `/v1` only when the request's `Host` header is `localhost`,
    `127.0.0.1`, or `[::1]`.

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
        # install_docs_pages adds the two documentation pages in place of FastAPI's.
        docs_url=None,
        redoc_url=None,
        openapi_url=_OPENAPI_URL if settings.docs_enabled else None,
    )
    if settings.docs_enabled:
        install_docs_pages(app, openapi_url=_OPENAPI_URL)
    _install_openapi(app)
    app.state.settings = settings
    app.state.db = Database(settings.database, max_client_bytes=settings.max_client_bytes)
    app.state.sessions = SessionManager(app.state.db, cache_size=settings.session_cache_size)
    install_error_handlers(app)
    # Each middleware added wraps the ones before it. A request gets its id first, and every
    # response gets the browser security headers. A HEAD request becomes a GET, and the response
    # loses its body last, after compression. Then the request passes the CORS headers, the
    # conditional check, the API key check, and the body limits, so the server reads no body from
    # a caller without a key.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.max_body_bytes, timeout_seconds=settings.body_timeout_seconds
    )
    app.add_middleware(AuthMiddleware, settings=settings, limiter=RateLimiter(settings.rate_limit_per_minute))
    app.add_middleware(ConditionalGetMiddleware, vary="Accept-Encoding")
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "Retry-After", "ETag", REPLAYED_HEADER, REQUEST_ID_HEADER],
        )
    app.add_middleware(GZipMiddleware, minimum_size=COMPRESSION_MINIMUM_BYTES, compresslevel=6)
    app.add_middleware(HeadMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(meta.health_router)
    v1 = APIRouter(
        prefix="/v1", dependencies=[Depends(authenticate), Depends(require_json_body)], responses=_SHARED_ERRORS
    )
    for router in V1_ROUTERS:
        v1.include_router(router)
    app.include_router(v1)
    return app
