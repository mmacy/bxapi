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
import re
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
from bxapi.caching import REVALIDATE, ConditionalGetMiddleware, HeadMiddleware
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
from bxapi.routers.rules import CACHE_CONTROL as RULES_CACHE_CONTROL
from bxapi.security_headers import SecurityHeadersMiddleware
from bxapi.session_manager import SessionManager
from bxapi.settings import Settings, SettingsError

__all__ = ["COMPRESSION_MINIMUM_BYTES", "BodyLimitMiddleware", "create_app", "require_json_body"]

COMPRESSION_MINIMUM_BYTES = 1024
"""The smallest response body, in bytes, that the server compresses for a client that accepts gzip."""

_log = logging.getLogger("bxapi")

# The members that some error codes add to a problem document, as a Markdown table. Its rows are longer than a line
# of code, so they can't be in the triple-quoted description.
_PROBLEM_MEMBERS = "\n".join(
    (
        "| Code | Added members |",
        "| --- | --- |",
        "| `validation_error` | `errors`: the first 20 parts of the request that failed validation, each "
        "with `loc`, the path to the value, `msg`, and `type`. `error_count`: how many parts failed, "
        "including any that `errors` leaves out. |",
        "| `not_found` | `resource`: the kind of resource the request asked for, such as `session`. `id`: the "
        "id in the request, or its first 128 characters followed by `...`. |",
        "| `revision_conflict` and `session_busy` | `revision`: the session's current revision. |",
        "| `referee_command` | `command_type`: the `command_type` of the referee command a player sent. |",
        "| `character_invalid` | `rejections`: osrlib's reasons for refusing the character, each with `code` "
        "and `params`. When osrlib reports no rejections, `field` and `id` appear instead: the request field that "
        "failed and the value of the entry, plus `index` (from 0) when `field` is a list. |",
        "| `storage_full` | `used_bytes`: the bytes of documents the server stores for your client. "
        "`max_bytes`: the most bytes the server stores for one client. |",
    )
)

_DESCRIPTION = (
    """
bxapi runs games of the 1981 B/X (Basic/Expert) fantasy adventure game over HTTP.
[osrlib](https://github.com/mmacy/osrlib-python), a Python rules engine, applies every rule, while
bxapi adds API keys, SQLite storage, one-at-a-time access to each game, and RFC 9457 error
responses. Your client application draws the screen and reads the player's input. bxapi rolls the
dice, applies the rules, stores the game, and returns what happened.

## Base URL

Each bxapi server runs at its own address. The `bxapi` command listens on
`http://127.0.0.1:8640` unless the server's `BXAPI_HOST` and `BXAPI_PORT` settings specify another
address, and the examples in this reference use `http://127.0.0.1:8640`. Every path in this
reference is relative to the server's address. Every operation except `GET /health` is under `/v1`.

## Play a game

1. `GET /v1/meta` checks your API key and returns your client name, role, and storage usage.
2. `POST /v1/ability-rolls` rolls six ability scores and lists the classes they qualify for.
3. `POST /v1/characters` creates a character from the roll and the player's choices. Save the
   character's `id`.
4. `GET /v1/adventures` lists the adventures you can play. Every server includes `builtin-barrow`.
5. `POST /v1/sessions` starts a game of an adventure with a party of stored characters. Save the
   session's `id`.
6. `GET /v1/sessions/{session_id}/commands` lists the commands that are legal now.
7. `POST /v1/sessions/{session_id}/commands` sends one command and returns what happened.
8. `GET /v1/sessions/{session_id}/view` returns what the players can see, so your client can draw
   the screen. Pass `include=view` in step 7 to get the view in the command's response instead.

Repeat steps 6 to 8 until `ended` is `true` in a command's response.

## Authentication

Send an API key as a bearer token in the `Authorization` header: `Authorization: Bearer <key>`.
Whoever runs the server configures its keys in the `BXAPI_API_KEYS` setting and assigns each key a
client name and a role. Every path under `/v1` requires an API key, whereas `GET /health`, `/docs`,
`/redoc`, and `/openapi.json` require none. A request with no key, or with an unknown key, gets 401
`unauthorized`.

A server started with no keys and `BXAPI_ALLOW_ANONYMOUS=true` asks for no key. It treats every
caller as a referee for the client `local`, and `GET /v1/meta` returns `auth_enabled` set to
`false`. Such a server answers a request under `/v1` only when the request's `Host` header is
`localhost`, `127.0.0.1`, or `[::1]`, with or without a port. A request with another host gets 421
`misdirected_request`.

Each key grants one of two roles:

- `player`: everything a game client needs. In a session, a player sees only what the players may
  see.
- `referee`: everything a player may do, plus the referee view, referee events, referee commands,
  chosen seeds, character and save imports, and full adventure and save documents.

A player request for a referee feature gets 403 `referee_required`, or 403 `referee_command` when a
player sends a referee command.

A key identifies a client application, not a person. Every key for one client reads and changes
that client's characters, adventures, sessions, and saves, whereas another client's resources are
hidden. A request for another client's resource gets the same 404 `not_found` as an id that
doesn't exist.

## Conventions

- **Ids.** A stored resource's id is a prefix followed by 22 random URL-safe characters: `rol_` for
  an ability roll, `chr_` for a character, `adv_` for an adventure, `ses_` for a session, and
  `sav_` for a save. Built-in adventure ids start with `builtin-`.
- **Timestamps.** UTC timestamps in RFC 3339 format with whole seconds, such as
  `2026-11-20T22:01:05Z`.
- **Lists.** Every list operation accepts `limit` (1 to 200, default 50) and `offset` (0 to
  2^53 - 1, default 0), and returns `items`, `total`, `limit`, and `offset`. Add `limit` to
  `offset` for the next page, and stop when `offset` plus `limit` is `total` or greater. The
  server lists stored resources newest first.
- **Documents.** A *document* is osrlib's JSON envelope for an adventure, a character, a party, or
  a save. It is an object with `kind`, `schema_version`, `engine_version`, and `payload`. The
  server accepts documents whose `schema_version` is at most the `schema_version` that
  `GET /v1/meta` returns, and refuses newer ones with 409 `version_unsupported`.
- **Seeds.** osrlib draws every random number from one integer, the *seed* (0 to 2^53 - 1). The
  stateless operations under `/v1/dice` and `/v1/generate` accept an optional `seed` from any
  caller and return the seed they used. Sending the same request with the same seed returns the
  same result on the same osrlib version. For ability rolls, characters, and sessions, only a
  referee may choose a seed, and the server never returns it to a player.
- **Unknown fields.** A request body with a field the operation doesn't define gets 422
  `validation_error`, whereas commands and documents are osrlib models and ignore unknown fields.
- **osrlib's descriptions.** The schema descriptions for commands, events, views, and rules models
  come from osrlib. Where an osrlib description mentions a validation check, the server returns 422
  `validation_error` for a malformed command or request body, and 422 `content_invalid` for an
  uploaded or imported document.
- **JSON bodies.** Send a request body as UTF-8 JSON with `Content-Type: application/json` (or an
  `application/*+json` media type). A non-empty body without a JSON `Content-Type` gets 415
  `unsupported_media_type`. The server refuses undecodable bytes, integers with more than 4,300
  digits, `NaN`, `Infinity`, numbers too large for a 64-bit float, strings with half of a UTF-16
  surrogate pair, or arrays and objects nested more than 100 levels deep, with 400 `bad_request`.
- **Body size.** A body is at most 65,536 bytes. On the five operations that accept an osrlib
  document, the limit is the server's `BXAPI_MAX_BODY_BYTES` setting instead (8,388,608 bytes by
  default): `POST /v1/adventures`, `POST /v1/adventures/validate`, `POST /v1/characters/import`,
  `POST /v1/saves/import`, and `POST /v1/sessions`. A larger body gets 413 `content_too_large`.
- **Compression.** Send `Accept-Encoding: gzip`, and the server compresses every response of 1,024
  bytes or more. Most HTTP clients send the header and decompress the response automatically.
- **Conditional requests and caching.** Every 200 response to a `GET` includes a weak `ETag`
  header. Send that `ETag` in `If-None-Match` to check whether your copy is still current: the
  server answers 304 with no body while the content is unchanged, or 200 with the new content when
  it has changed. Successful `GET` responses under `/v1/rules` include
  `Cache-Control: private, max-age=3600`, while other `/v1` `GET` responses include
  `Cache-Control: private, no-cache`.
- **HEAD.** Every `GET` operation also answers `HEAD` with the same status and headers and no body.
- **Idempotency keys.** To retry a `POST` under `/v1` without running it twice, send an
  `Idempotency-Key` header (1 to 255 printable ASCII characters with no spaces, such as a UUID) on
  the first request and on every retry. The server stores a 2xx response for 24 hours and returns
  it with `Idempotent-Replayed: true` on a retry. When a request fails with a problem other than
  500 `internal_error`, nothing changed and the server releases the key so a retry runs the
  request again. Keys are scoped to your client.
- **Request ids.** Every response includes an `X-Request-Id` header, such as
  `req_Zk3uY0mWq1x8Jr5cTt2aLw`, which the server logs with the request. Send `X-Request-Id` with 1
  to 128 letters, digits, and `.`, `_`, `~`, or `-` to supply your own id. The server replaces any
  other value with a generated `req_` id.
- **Storage.** The server counts the bytes of the documents it stores for your client: ability
  rolls, characters, adventures, sessions, saves, and stored idempotency responses. `GET /v1/meta`
  returns the current count and the limit (`BXAPI_MAX_CLIENT_BYTES`, 1,073,741,824 bytes by
  default, or `null` when disabled). At or above the limit, any request that would store a
  document gets 507 `storage_full` until you delete something.
- **Rate limits.** Each client has a token-bucket budget of `BXAPI_RATE_LIMIT_PER_MINUTE` requests
  per server process (600 per minute by default). You can spend the full budget in a burst and
  regain tokens steadily. A request over the limit gets 429 `rate_limited` with the seconds to wait
  in `Retry-After`.

## Refused commands

A command that the B/X rules forbid isn't an HTTP error. `POST /v1/sessions/{session_id}/commands`
returns 200 with `accepted` set to `false` and osrlib's `rejections`, and the session doesn't
change. An HTTP 4xx or 5xx status means the request itself failed.

## Errors

Every error response is an RFC 9457 problem document with the media type
`application/problem+json`. Branch on `code`, which stays stable across releases. `detail` is a
human-readable sentence of at most 1,000 characters. Each operation lists the codes it can return
and how to handle each one.

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

Every problem document includes `type`, `title`, `status`, `detail`, `code`, and `request_id`
(matching the response's `X-Request-Id` header). Specific codes add extra members:

"""
    + _PROBLEM_MEMBERS
    + """

Later releases may add members, so ignore any member your client doesn't recognize.

Five error codes can succeed when you send the same request again: 409
`idempotency_key_in_progress`, 429 `rate_limited`, and 503 `database_busy` after waiting the
seconds in their `Retry-After` headers, 408 `request_timeout` right away, and 409 `session_busy`
right away unless you passed `expected_revision` (if you did, read the session first). For any
other code, fix the request before retrying. When no response arrives at all, retry a `POST` with
the same `Idempotency-Key`. For a 500 error, report `request_id` to the server's operator.
"""
)

_TAGS = [
    {
        "name": "meta",
        "description": """
Check that the server is reachable, verify an API key, and inspect the server's versions and your
client's storage usage.

- `GET /health`: returns `{"status": "ok"}` without an API key, for load balancer and container
  health checks.
- `GET /v1/meta`: checks your API key and returns `api_version`, `engine_version`,
  `schema_version` (the document format version), `auth_enabled`, your `client` name and `role`,
  and your `storage` usage and limit.
""",
    },
    {
        "name": "rules",
        "description": """
Read the B/X rules catalogs that osrlib loads: character classes, monsters, spells, mundane
equipment, magic items, languages, printed reference tables, session commands, and the Open Game
License. Use these endpoints to populate a class picker, a shop, a bestiary, or a command menu.

Each list operation returns a page of summaries, and each detail operation returns osrlib's full
model for one entry. Pass an entry's `id` from a list to the matching detail operation. Where a
list accepts `q`, it filters to entries whose `id` or `name` contains that text, ignoring case.
Every 200 response includes `Cache-Control: private, max-age=3600`, because the catalogs change
only when the server's osrlib version changes. Both `player` and `referee` keys can read every
entry, including details that remain hidden in play until identified, such as whether a magic item
is cursed.

- `GET /v1/rules/classes` and `GET /v1/rules/classes/{class_id}`: character classes and level
  progressions. Pass `id` as `class_id` to `POST /v1/characters`.
- `GET /v1/rules/equipment` and `GET /v1/rules/equipment/{item_id}`: weapons, armour, adventuring
  gear, and ammunition, filterable by `q` and `item_type`. Pass `id` in `purchases` on
  `POST /v1/characters` or in a session's `purchase_equipment` command.
- `GET /v1/rules/spells` and `GET /v1/rules/spells/{spell_id}`: cleric and arcane spells,
  filterable by `q`, `class_id`, and `level`. Pass `id` in `starting_spell_ids` on
  `POST /v1/characters` for a magic-user or elf.
- `GET /v1/rules/languages`: spoken languages. Pass an `id` whose `choosable` is `true` in
  `extra_languages` on `POST /v1/characters`.
- `GET /v1/rules/monsters` and `GET /v1/rules/monsters/{monster_id}`: monster stat blocks,
  filterable by `q`. Pass `id` as `monster_id` to `POST /v1/generate/monsters`.
- `GET /v1/rules/magic-items` and `GET /v1/rules/magic-items/{item_id}`: magic item templates,
  filterable by `q` and `category`.
- `GET /v1/rules/tables` and `GET /v1/rules/tables/{name}`: the printed `abilities`, `combat`,
  `encounters`, and `treasure` tables.
- `GET /v1/rules/commands` and `GET /v1/rules/commands/{command_type}`: all session commands,
  filterable by `kind` (`play` or `referee`) and `mode`, with the JSON Schema for each command's
  request body.
- `GET /v1/rules/license`: the Open Game License 1.0a text and Section 15 notice for the rules
  content.
""",
    },
    {
        "name": "dice",
        "description": """
Roll B/X dice expressions such as `3d6`, `d%`, or `2d6+1x10` outside a session and get every
individual die alongside the final total. The server stores nothing.

A *seed* is an integer from 0 to 2^53 - 1 that determines every random number osrlib draws. Any
caller may pass `seed` to repeat a roll, or omit it to let the server draw and return a new one.

- `POST /v1/dice/roll`: takes `expression`, `times` (1 to 100), and an optional `seed`, and
  returns `rolls` and the `seed` used.
""",
    },
    {
        "name": "generate",
        "description": """
Roll first-level characters, treasure hoards, NPC parties, stocked dungeon rooms, and monster
groups from the B/X tables without storing anything. Use these stateless endpoints for referee
tools, adventure preparation, or previews.

Every operation accepts an optional *seed* (an integer from 0 to 2^53 - 1 that determines every
random roll) from any caller and returns the `seed` it used. Sending the same request body with
that `seed` reproduces the exact result on the same osrlib version.

- `POST /v1/generate/character`: takes character creation choices and returns the character,
  derived stats, every creation roll, and `seed`. To store a character for play, call
  `POST /v1/characters` instead.
- `POST /v1/generate/treasure`: takes a `treasure_type` (`A` to `V`) and magic item `tier`
  (`basic` or `expert`), and returns rolled coins, valuables, and magic items.
- `POST /v1/generate/npc-party`: takes `kind` (`basic` or `expert`) and `count` (1 to 100), and
  returns NPC adventurers with gear and treasure.
- `POST /v1/generate/room`: takes a dungeon `level_number` (from 1), and returns rolled room
  contents, treasure presence, and any encounter.
- `POST /v1/generate/monsters`: takes a `monster_id` from `GET /v1/rules/monsters` and `count` (1
  to 100), and returns monster instances with rolled hit points.
""",
    },
    {
        "name": "characters",
        "description": """
Create, import, inspect, and delete stored player characters for starting game sessions. Each
session copies its party members when it starts, so play never modifies a stored character and one
stored character can start any number of sessions.

In B/X, players roll six ability scores before choosing a class. An *ability roll* stores those
six scores along with a *seed* (the integer that determines osrlib's random rolls), which the
server never reveals to players. Creating a character from that roll derives hit points and
starting gold from the same seed, so retrying after an invalid choice keeps the same rolls.

1. `POST /v1/ability-rolls` rolls and stores six scores, returning `id`, `scores`, and
   `eligible_class_ids`. `GET /v1/ability-rolls/{roll_id}` reads a stored roll again.
2. `POST /v1/characters` creates and stores a first-level character from `ability_roll_id` (or
   freshly rolled scores) and the player's choices.
3. `POST /v1/sessions` starts a game session with the character's `id` in `character_ids`.

`GET /v1/characters` lists your client's stored characters, `GET /v1/characters/{character_id}`
returns one character with its derived stats, and `DELETE /v1/characters/{character_id}` deletes
one without affecting active sessions. A referee can import an osrlib *character document* (`kind`,
`schema_version`, `engine_version`, and `payload`) with `POST /v1/characters/import`. To roll a
character without storing it, call `POST /v1/generate/character`.
""",
    },
    {
        "name": "adventures",
        "description": """
List, validate, upload, inspect, and delete adventures that sessions can play. An *adventure*
defines a starting town, one or more dungeons, and their encounters, treasure, triggers, and
quests. Every server includes the built-in adventure `builtin-barrow`, so you can start a session
without uploading anything.

An *adventure document* is osrlib's versioned JSON envelope (`kind` set to `"adventure"`,
`schema_version`, `engine_version`, and `payload`), as produced by
[osr-forge](https://github.com/mmacy/osr-forge), [osr-editor](https://github.com/mmacy/osr-editor),
or osrlib. *Adventure info* is the player-safe summary shown before play: title, description,
hooks, recommended party, town, and dungeon names, with maps, monsters, and quests omitted.

1. `GET /v1/adventures` lists built-in adventures first, followed by your client's uploads.
2. `POST /v1/adventures/validate` checks an adventure document and returns `valid` without storing
   anything.
3. `POST /v1/adventures` validates and stores an adventure document, returning its `adv_` id.
4. `POST /v1/sessions` starts a game session with the adventure's `id` as `adventure_id`.

`GET /v1/adventures/{adventure_id}` returns one adventure's player-safe info,
`GET /v1/adventures/{adventure_id}/document` returns the full uploaded document to a referee, and
`DELETE /v1/adventures/{adventure_id}` deletes a client upload without affecting sessions already
started from it.
""",
    },
    {
        "name": "sessions",
        "description": """
Start, play, inspect, save, and delete dungeon-crawl game sessions. A *session* is one game in
progress: a party of characters playing a copy of an adventure from `town` until play reaches
`game_over` or `victory`.

- A *command* is one action sent to the session, such as
  `{"command_type": "move_party", "direction": "east"}`, which osrlib either accepts or refuses.
- A *mode* is the current phase of play (`town`, `exploring`, `encounter`, `battle`, `game_over`,
  or `victory`), which determines which commands are legal.
- An *event* is one recorded occurrence from a command, trigger, or quest, marked visible to all
  players (`player`) or to the referee alone (`referee`).
- The *revision* counts accepted command requests in the session, starting at 0.
- The *player view* is everything the players may see at the current revision, whereas the
  *referee view* exposes the hidden map, monster hit points, flags, and logs to a referee.

1. `POST /v1/sessions` starts a session in `town` at revision 0 from an `adventure_id` and
   `character_ids` (or a referee's `party_document`).
2. `GET /v1/sessions/{session_id}/commands` lists the commands legal in the current mode for your
   role. Read `GET /v1/rules/commands/{command_type}` for a command's body schema.
3. `POST /v1/sessions/{session_id}/commands` executes one command and returns `accepted`,
   `rejections`, `events`, `messages`, `mode`, `ended`, and `revision` (plus `view` when you pass
   `include=view`).
4. `GET /v1/sessions/{session_id}/view` returns the player view when you didn't request
   `include=view` in step 3.
5. Repeat steps 2 to 4 until `ended` is `true`.

`GET /v1/sessions/{session_id}/events` pages through the event log after a reconnect, and
`GET /v1/sessions/{session_id}/referee-view` returns the full game state to a referee.
`POST /v1/sessions/{session_id}/saves` snapshots the session into a save that
`POST /v1/saves/{save_id}/restore` can branch from. `GET /v1/sessions` lists your sessions,
`GET /v1/sessions/{session_id}` reads session metadata, `PATCH /v1/sessions/{session_id}` renames a
session, and `DELETE /v1/sessions/{session_id}` deletes a session while keeping its saves.

The server runs one command, view read, or event-log read at a time per session. A request that
waits longer than 5 seconds for the session lock gets 409 `session_busy`. Pass `Idempotency-Key`
or `expected_revision` when retrying a command so a network retry never executes the move twice.
""",
    },
    {
        "name": "saves",
        "description": """
List, inspect, relabel, restore, export, import, and delete saved games. A *save* snapshots a
session after its last accepted command. Restoring a save starts a new session at revision 0 from
that snapshot while leaving both the save and the original session unchanged.

A *save document* is osrlib's versioned JSON envelope (`kind` set to `"save"`, `schema_version`,
`engine_version`, and `payload`) containing the full game state, including the master seed,
unexplored map, monster hit points, and logs. Because it exposes hidden state, only a referee may
export or import a save document.

1. `POST /v1/sessions/{session_id}/saves` creates a save from a session and returns its `sav_` id.
2. `GET /v1/saves` lists your saves (optionally filtered by `session_id`), and
   `GET /v1/saves/{save_id}` returns one save's metadata.
3. `POST /v1/saves/{save_id}/restore` starts a new session from the save.

`PATCH /v1/saves/{save_id}` updates or clears a save's `label`, and `DELETE /v1/saves/{save_id}`
deletes a save without affecting sessions restored from it. A referee exports a save document with
`GET /v1/saves/{save_id}/document` and imports one on any compatible server with
`POST /v1/saves/import`.
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
            "`unauthorized`: The request has no API key or has an unknown key. Send "
            "`Authorization: Bearer <key>` with a key configured on the server. Retrying the same request fails."
        ],
        421: [
            "`misdirected_request`: The server runs without API keys, and the request's `Host` header isn't "
            "`localhost`, `127.0.0.1`, or `[::1]`. Call the server by one of those names, or configure API keys on "
            "the server. A server with API keys never returns this code."
        ],
        422: [VALIDATION_ERROR],
        429: [
            "`rate_limited`: Your client exceeded the per-minute request budget (`BXAPI_RATE_LIMIT_PER_MINUTE`, 600 "
            "by default). Wait the seconds in the `Retry-After` header and retry. Requests without a valid API key "
            "share a separate budget and receive 429 instead of 401 `unauthorized` when that budget is exhausted."
        ],
        500: [INTERNAL_ERROR],
        503: [
            "`database_busy`: Another process held the SQLite write lock for more than 5 seconds. Wait the 1 second "
            "in the `Retry-After` header and retry."
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
        "description": "Whole seconds to wait before retrying, at least `1`.",
        "schema": {"type": "integer"},
    }
}
_SHARED_ERRORS[503]["headers"] = {
    "Retry-After": {
        "description": "Whole seconds to wait before retrying: `1`.",
        "schema": {"type": "integer"},
    }
}

_BODY_ERRORS = {
    400: "- `bad_request`: The body doesn't decode as text, or contains a disallowed value: an integer with more "
    "than 4,300 digits, `NaN`, `Infinity`, a number too large for a 64-bit float, a string with half of a UTF-16 "
    f"surrogate pair, or arrays and objects nested more than {MAX_JSON_DEPTH} levels deep. Check `detail` and send "
    "UTF-8 JSON without those values. Retrying the same body fails.",
    408: "- `request_timeout`: The full request body didn't arrive within `BXAPI_BODY_TIMEOUT_SECONDS` (30 seconds "
    "by default). Retry the request.",
    413: "- `content_too_large`: The request body exceeds the limit for document operations (`BXAPI_MAX_BODY_BYTES`, "
    "8,388,608 bytes by default). Send a smaller body. Retrying the same body fails.",
    415: "- `unsupported_media_type`: The request includes a non-empty body without a JSON `Content-Type` header. "
    "Send `Content-Type: application/json`.",
}

_SMALL_BODY_TOO_LARGE = (
    f"- `content_too_large`: The request body exceeds {SMALL_BODY_BYTES:,} bytes (the limit for operations that take "
    "no document), or exceeds `BXAPI_MAX_BODY_BYTES` when set lower. Send a smaller body. Retrying the same body "
    "fails."
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
        "`idempotency_key_in_progress`: The first request with this `Idempotency-Key` is still running. Wait the 1 "
        "second in the `Retry-After` header and retry.",
        "`idempotency_key_interrupted`: The server failed or stopped while handling the first request with this "
        "`Idempotency-Key` and has no record of whether it took effect. Inspect the target resource first. If the "
        "change didn't take effect, send the request again with a new key.",
    ],
    "422": [
        "`idempotency_key_reused`: Your client already sent this `Idempotency-Key` with a different method, path, "
        "query, body, or role. Generate a new key for each distinct request.",
    ],
}


def _describe_idempotency(schema: dict[str, Any]) -> None:
    key = {
        "name": IDEMPOTENCY_KEY_HEADER,
        "in": "header",
        "required": False,
        "description": "Client-chosen key (1 to 255 printable ASCII characters with no spaces, such as a UUID) that "
        "deduplicates retries of this `POST` for 24 hours. Resending the identical request with the same key returns "
        "the stored 2xx response with `Idempotent-Replayed: true` instead of running the operation again.",
        "schema": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[!-~]+$"},
        "example": "0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11",
    }
    # One definition of the response header, which every POST operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})[REPLAYED_HEADER] = {
        "description": "`true` when the response is replayed from an earlier `POST` with the same `Idempotency-Key`. "
        "Omitted on the initial response.",
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
            "description": "With `idempotency_key_in_progress`, whole seconds to wait before retrying: `1`.",
            "schema": {"type": "integer"},
        }


_STORAGE_FULL = (
    "- `storage_full`: Stored documents for your client have reached `BXAPI_MAX_CLIENT_BYTES` (1,073,741,824 bytes by "
    "default). `used_bytes` and `max_bytes` report your current usage and limit, and the request stored nothing. "
    "Delete unused sessions, saves, adventures, or characters, then retry. Stateless operations such as dice rolls "
    "return this code only when `Idempotency-Key` is set, because storing the replay response counts toward the "
    "limit."
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
        "description": "The `ETag` of a cached response for this request, such as "
        '`W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"`, or `*`. While the content is unchanged, the server answers 304 '
        "with no body.",
        "schema": {"type": "string"},
        "example": 'W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"',
    }
    headers = schema.setdefault("components", {}).setdefault("headers", {})
    headers["ETag"] = {
        "description": "Weak entity tag for the uncompressed response body, such as "
        '`W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"`. Send it in `If-None-Match` to check whether the content has '
        "changed.",
        "schema": {"type": "string"},
    }
    etag = {"$ref": "#/components/headers/ETag"}
    rules_cache = {
        "description": f"`{RULES_CACHE_CONTROL}`. Rules catalogs change only when the server's osrlib version changes.",
        "schema": {"type": "string"},
    }
    revalidate_cache = {
        "description": f"`{REVALIDATE}`. Revalidate with `If-None-Match` before reusing a cached copy.",
        "schema": {"type": "string"},
    }
    for path_name, path in schema.get("paths", {}).items():
        operation = path.get("get")
        if operation is None:
            continue
        operation.setdefault("parameters", []).append(dict(if_none_match))
        responses = operation.setdefault("responses", {})
        ok_headers = responses.setdefault("200", {}).setdefault("headers", {})
        ok_headers["ETag"] = etag
        not_modified_headers: dict[str, Any] = {"ETag": etag}
        if path_name.startswith("/v1/rules/"):
            ok_headers["Cache-Control"] = dict(rules_cache)
            not_modified_headers["Cache-Control"] = dict(rules_cache)
        elif path_name.startswith("/v1/"):
            ok_headers["Cache-Control"] = dict(revalidate_cache)
            not_modified_headers["Cache-Control"] = dict(revalidate_cache)
        responses["304"] = {
            "description": "The response whose `ETag` you sent in `If-None-Match` is still current. The response "
            "has no body.",
            "headers": not_modified_headers,
        }


def _describe_request_ids(schema: dict[str, Any]) -> None:
    # One header definition, which every response of every operation refers to.
    schema.setdefault("components", {}).setdefault("headers", {})[REQUEST_ID_HEADER] = {
        "description": "The request identifier, such as `req_Zk3uY0mWq1x8Jr5cTt2aLw`, which the server logs with "
        "the request. Echoes the request's `X-Request-Id` header when it contains 1 to 128 letters, digits, and "
        "`.`, `_`, `~`, or `-`. Otherwise the server generates a `req_` id.",
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
    # to pick the member anyway, so this function removes `default` and adds the tag to `required`
    # in each member that a discriminator refers to.
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
                member["properties"][tag].pop("default", None)
                required = member.setdefault("required", [])
                if tag not in required:
                    required.append(tag)


_QUERY_VALIDATION_ERROR = (
    "- `validation_error`: A query parameter has the wrong type or is out of range. `errors` lists the first 20 "
    "problems, and the `loc` of each is the path to the value. Fix the request before you send it again."
)


def _can_fail_validation(parameter: dict[str, Any]) -> bool:
    if parameter["in"] == "query":
        return True
    if parameter["in"] == "path":
        # A path parameter that takes any string matches every request that reaches the route.
        constraints = set(parameter["schema"]) - {"type", "title", "description"}
        return parameter["schema"].get("type") != "string" or bool(constraints)
    return False


def _describe_validation_errors(schema: dict[str, Any]) -> None:
    # The shared 422 entry describes a request body. An operation without a body fails validation only on a query
    # parameter or a constrained path parameter, and an operation with neither never returns `validation_error`.
    shared = f"- {VALIDATION_ERROR}"
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            responses = operation.get("responses", {})
            response = responses.get("422")
            if response is None or "requestBody" in operation:
                continue
            entries = response["description"].split("\n")
            if shared not in entries:
                continue
            if any(_can_fail_validation(parameter) for parameter in operation.get("parameters", [])):
                entries[entries.index(shared)] = _QUERY_VALIDATION_ERROR
            else:
                entries.remove(shared)
            if entries:
                response["description"] = "\n".join(entries)
            else:
                del responses["422"]


# Google-style `Examples:` and `Attributes:` sections: the heading, then lines that are blank or indented.
# `Attributes:` repeats the schema's properties, and `Examples:` contains Python snippets for osrlib callers.
_STRIPPED_SECTIONS = re.compile(r"^(?:Examples|Attributes):\n(?:(?: {4}.*)?\n)*(?: {4}.*)?", re.MULTILINE)
_MODES_SECTION = re.compile(r"^Modes:\n {4}(.+)$", re.MULTILINE)
_DOC_SECTION = re.compile(r"^(Rejections|Events|Raises):\n((?:(?: {4}.*)?\n)*(?: {4}.*)?)", re.MULTILINE)
_EXCEPTION_PREFIX = re.compile(r"^(?:ValueError|ContentValidationError): (?:If )?", re.MULTILINE)
_PYTHON_BLOCK = re.compile(r"^`{3}python\n.*?^`{3}$", re.MULTILINE | re.DOTALL)
_BLANK_LINES = re.compile(r"\n{3,}")
_PYTHON_LITERALS = {"`None`": "`null`", "`True`": "`true`", "`False`": "`false`"}
# Keys whose values are data, not schemas, so a `description` key inside them isn't a description.
_DATA_KEYS = frozenset({"default", "enum", "example", "examples", "const"})

_COMMAND_SOURCE_DESCRIPTION = (
    "Referee only. Who or what issued the command, such as a trigger id, a quest id, or the name of a subsystem in "
    "your application (at least 1 character), or `null` when unset. The session records `source` in `command_log` "
    "without changing how the command executes. A player who sets `source` gets 403 `referee_required`."
)

_SCHEMA_OVERRIDES = {
    "AbilityScore": (
        "One of the six B/X ability scores: `str` (Strength), `int` (Intelligence), `wis` (Wisdom), `dex` "
        "(Dexterity), `con` (Constitution), or `cha` (Charisma). Score objects use these six lowercase strings as "
        "keys and require all six."
    ),
    "CoinDenomination": (
        "One of the five B/X coin denominations: `pp` (platinum), `gp` (gold), `ep` (electrum), `sp` (silver), or "
        "`cp` (copper). Each value matches the corresponding field name on `Coins`."
    ),
    "JsonObject": "A JSON object with string keys.",
}


def _format_doc_section(match: re.Match[str]) -> str:
    heading, body = match.group(1), match.group(2)
    dedented = "\n".join(line.removeprefix("    ") for line in body.splitlines()).strip()
    if heading != "Raises":
        return f"**{heading}:**\n\n{dedented}\n\n"
    # Join wrapped lines of each `Exception: If ...` entry, then turn each into a validation bullet.
    joined = re.sub(r"\n {4}", " ", dedented)
    bullets = [f"- Invalid when {_EXCEPTION_PREFIX.sub('', line)}" for line in joined.splitlines() if line.strip()]
    return "**Validation:**\n\n" + "\n".join(bullets) + "\n\n"


def _json_description(text: str) -> str:
    text = _PYTHON_BLOCK.sub("", _STRIPPED_SECTIONS.sub("", text))
    text = _MODES_SECTION.sub(r"**Modes:** \1", text)
    text = _DOC_SECTION.sub(_format_doc_section, text)
    text = _BLANK_LINES.sub("\n\n", text).strip()
    for python, json_value in _PYTHON_LITERALS.items():
        text = text.replace(python, json_value)
    return text


def _describe_in_json_terms(value: object) -> None:
    # osrlib's docstrings become the descriptions of its models. Their examples are Python code that a client of the
    # HTTP API can't run, and they write `None`, `True`, and `False` where a client reads JSON.
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                value[key] = _json_description(item)
            elif key not in _DATA_KEYS:
                _describe_in_json_terms(item)
    elif isinstance(value, list):
        for item in value:
            _describe_in_json_terms(item)


def _describe_commands_and_schemas(schema: dict[str, Any]) -> None:
    schemas = schema.get("components", {}).get("schemas", {})
    for name, description in _SCHEMA_OVERRIDES.items():
        if name in schemas:
            schemas[name]["description"] = description
    command_body = (
        schema.get("paths", {})
        .get("/v1/sessions/{session_id}/commands", {})
        .get("post", {})
        .get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    for option in command_body.get("oneOf", []):
        ref = option.get("$ref") if isinstance(option, dict) else None
        if not isinstance(ref, str) or not ref.startswith(_SCHEMAS):
            continue
        member = schemas.get(ref.removeprefix(_SCHEMAS), {})
        properties = member.get("properties", {})
        tag = properties.get("command_type")
        if isinstance(tag, dict) and isinstance(tag.get("const"), str):
            tag["description"] = f'Always `"{tag["const"]}"`. Pass it in every request body to select this command.'
        if "source" in properties and isinstance(properties["source"], dict):
            properties["source"]["description"] = _COMMAND_SOURCE_DESCRIPTION
    paths = schema.get("paths", {})
    if "/v1/generate/character" in paths and "/v1/generate/treasure" in paths:
        reordered: dict[str, Any] = {}
        character_path = paths["/v1/generate/character"]
        for path_name, item in paths.items():
            if path_name == "/v1/generate/character":
                continue
            if path_name == "/v1/generate/treasure":
                reordered["/v1/generate/character"] = character_path
            reordered[path_name] = item
        schema["paths"] = reordered


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
            _describe_validation_errors(schema)
            _describe_idempotency(schema)
            _describe_storage(schema)
            _describe_conditional_gets(schema)
            _describe_request_ids(schema)
            _require_discriminators(schema)
            _describe_in_json_terms(schema.get("components", {}).get("schemas", {}))
            _describe_commands_and_schemas(schema)
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
