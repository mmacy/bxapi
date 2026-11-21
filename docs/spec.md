# bxapi specification

bxapi is an HTTP API for the 1981 B/X (Basic/Expert) rules. Other applications call it as their backend: games that run a dungeon crawl, and utilities that look up a monster or roll a treasure. [osrlib](https://github.com/mmacy/osrlib-python), a Python library, implements the rules. bxapi adds what a network service needs around it: authentication, storage, one-at-a-time access to each game, and an error format.

The contract below covers version 1 of the API. For how to work in the repository, see `AGENTS.md`.

## Design rules

1. **osrlib applies every rule.** bxapi never computes a rule's result, adjusts one, or re-implements one. An endpoint validates its input, calls osrlib, stores what needs storing, and returns osrlib's models unchanged wherever a model exists.
2. **A player sees only what the players may see.** For a caller with the player role, the server builds every response about a session from osrlib's player view and its player-visible events. A session's master seed, referee events, monster hit points, and unexplored map go only to a caller with the referee role. So do save documents and full adventure documents. Each member's character sheet in the player view counts every item the member wears, identified or not, because those are the numbers the monsters roll against. So a player can infer an unidentified item's bonus or curse from numbers like `armour_class` and `melee_modifier`, while the inventory keeps the item's name masked.
3. **A refused command is a result.** When osrlib refuses a command, the response has status 200, `accepted` set to `false`, and osrlib's rejections. A refused command changes nothing in the game. An HTTP error status means the request itself failed, for example because it broke the API's contract.
4. **The server decides the dice.** The server draws each session's seed and returns it only inside a save document, which only a referee can read. Only a referee can supply one. The stateless generators under `/v1/dice` and `/v1/generate` are different: any caller can pass a seed, and each response includes the seed it used, so the caller can repeat the roll.
5. **A client reads and changes only the data it created.** Every stored resource belongs to the client application that created it. A request for another client's resource gets the same 404 as a request for an id that doesn't exist.
6. **Endpoints are synchronous.** osrlib is synchronous and CPU-bound, so endpoints are plain `def` functions that FastAPI runs in its thread pool. The session manager (`bxapi.session_manager`) makes sure that the server runs one command at a time on each session.

## Conventions

- **Base path.** Every endpoint except `GET /health` and the documentation paths in **OpenAPI** below is under `/v1`, and every request under `/v1` needs an API key, unless the server allows anonymous callers.
- **Bodies.** Requests and responses are JSON. Field names are snake_case. A `POST`, `PUT`, or `PATCH` request with a body states that the body is JSON in its `Content-Type` header: `application/json`, or an `application` type whose name ends in `+json`, with or without parameters like `charset`. A body with another content type, or with none, gets 415 `unsupported_media_type`. A request with an empty body needs no `Content-Type`. A request body with a field the endpoint doesn't define gets 422 `validation_error`. A body or a field that is an osrlib model, like a command or a character's `adjustment`, is the exception: osrlib ignores unknown fields.
- **Body size.** A request body is at most `BXAPI_MAX_BODY_BYTES`, on every path. On an operation that takes no document, a body is also at most 65,536 bytes. The operations that take a document are `POST /v1/adventures`, `POST /v1/adventures/validate`, `POST /v1/characters/import`, `POST /v1/saves/import`, and `POST /v1/sessions`, whose `party_document` is a document. A larger body gets 413 `content_too_large`. The server sets the 65,536-byte limit, and it isn't a rule of the game. It limits the time the server spends on one command or one set of character choices, and how much one accepted command adds to the session's save document.
- **JSON values.** The server refuses a body with a value it can't store or can't send back as JSON, with 400 `bad_request`. The values are `NaN`, `Infinity`, and `-Infinity`, a number too large for a 64-bit float, like `1e400`, a string or a key with half of a UTF-16 surrogate pair, like `"\ud800"`, and arrays and objects nested more than 100 levels deep. A save document from osrlib nests 12 levels.
- **Names.** A name or a label that the server stores in a column of its own has no control character: a character's `name`, a session's `name`, a save's `label`, and an adventure's `name`. A control character is one from U+0000 to U+001F or from U+007F to U+009F, like a tab, a newline, or an escape. In a request body that bxapi defines, a name with one gets 422 `validation_error`, with the error type `string_control_character`. An uploaded adventure, an imported character, and an imported save's adventure get 422 `content_invalid`. The rule keeps an escape sequence out of the lists a client prints. Other text can contain any character, like an adventure's description or the name of a character in a party document.
- **Identifiers.** A stored resource has an id made of a prefix and 22 random URL-safe characters: `chr_` for a character, `rol_` for an ability roll, `adv_` for an adventure, `ses_` for a session, and `sav_` for a save. An adventure that comes with the server has an id that starts with `builtin-`.
- **Timestamps.** UTC, in RFC 3339 format with whole seconds, like `2026-11-20T22:01:05Z`.
- **Lists.** A list endpoint takes `limit` (1 to 200, default 50) and `offset` (0 to 2^53 - 1, default 0), and returns `{"items": [...], "total": n, "limit": n, "offset": n}`. Stored resources are listed newest first.
- **Documents.** osrlib wraps each serialized adventure, character, party, and save in an envelope with the keys `kind`, `schema_version`, `engine_version`, and `payload`. This specification calls the envelope a *document*. An endpoint that imports a document accepts the document as the whole request body.
- **OpenAPI.** `GET /openapi.json` describes every endpoint. Each `GET` operation lists the `If-None-Match` request header, the `ETag` response header, and the 304 response. Each operation's `operationId` is a short snake_case name, like `roll_dice`, that doesn't change between releases. Each operation has request and response examples, including one for each error code the operation lists. `tests/test_openapi_examples.py` checks the recorded examples against the server. The interactive documentation is at `/docs`, which shows the document with Swagger UI, and `/redoc`, which shows it with ReDoc. Each page loads one exact version of its script from `cdn.jsdelivr.net`, with a Subresource Integrity hash, so the browser refuses a file that differs from the one bxapi was tested with. Each page's `Content-Security-Policy` lets the page load only those files and call only the server, so a script on the page can't send an API key that a person typed into it to another host. The three paths need no API key, so anyone who can reach the server can read the API's description. `BXAPI_DOCS_ENABLED=false` turns all three off.
- **Creation.** A request that creates a stored resource returns 201 and the resource. A delete returns 204 with no body.
- **Compression.** For a request with `Accept-Encoding: gzip`, the server compresses every response body of 1,024 bytes or more with gzip, whatever its status, and the response has `Content-Encoding: gzip` and `Vary: Accept-Encoding`.
- **Conditional requests.** Every 200 response to a `GET`, on any path, has an `ETag` header: `W/"`, 32 hexadecimal digits of a hash of the uncompressed body, and `"`. The tag is weak because the compressed and uncompressed responses have the same one. When a `GET` request's `If-None-Match` header is `*` or lists the tag of the response the server would send, compared without the `W/` prefixes, the server sends 304 with the response's headers except `Content-Type` and `Content-Length`, and no body. A 304 has `Vary: Accept-Encoding`. The server builds the response either way, so a 304 saves the transfer and not the work. A 200 or a 304 under `/v1` that sets no `Cache-Control` of its own has `Cache-Control: private, no-cache`, so a browser asks the server before it uses a stored copy. A response with another status, or to another method, has no `ETag`.
- **HEAD.** A `HEAD` request gets the status and headers of the `GET` response for the same path, and no body. The OpenAPI document doesn't list the `HEAD` operations.
- **Idempotency keys.** A `POST` under `/v1` can have an `Idempotency-Key` header: a value the client chooses, of 1 to 255 printable ASCII characters with no spaces. Another value gets 422 `validation_error`. A key belongs to the client that sent it. The server claims the key in the database before it runs the request, and then:
  - It stores the status and body of a 2xx response with the key. A later request from the same client with the same key, method, path, query string, body, and role gets the stored status and body, with the header `Idempotent-Replayed: true`, and the server runs nothing. A refused command is a 200, so it's stored too.
  - A request that fails with a problem document other than 500 `internal_error` changed nothing, so the server forgets the key, and the same request with the same key runs again.
  - A request that fails with 500 `internal_error` leaves the key with no stored response. A later request with the key gets 409 `idempotency_key_interrupted`. So does a request whose key was claimed 60 seconds ago or longer and has no stored response. That happens when the server stops in the middle of the first request, and when an overloaded server takes 60 seconds or longer to run it. In the second case, the first request can still take effect after the retry got this code.
  - While the first request runs, another request with the key gets 409 `idempotency_key_in_progress`, with the header `Retry-After: 1`.
  - The same key with a different method, path, query string, body, or role gets 422 `idempotency_key_reused`.
  - The server forgets a key 24 hours after the first request with it.

  The server writes the request's change and the stored response in two transactions. If the server stops between them, or the database refuses the second one, the change is in the database and the key has no stored response. A retry then gets `idempotency_key_in_progress` for 60 seconds and `idempotency_key_interrupted` afterward, and never runs the request a second time. Other methods ignore the header: `GET`, `PATCH`, and `DELETE` requests have the same effect however many times a client sends them.
- **Rate limits.** Each client has a budget of `BXAPI_RATE_LIMIT_PER_MINUTE` requests, which is 600 by default. The budget starts full. Every request under `/v1` with one of the client's keys takes one request from it, whatever the response, and the budget regains requests at a steady rate, up to full: `BXAPI_RATE_LIMIT_PER_MINUTE` of them in a minute. So a client can send that many requests at once, and that many a minute after that. A request that finds the budget empty gets 429 `rate_limited`, takes nothing from the budget, and has no effect. Its `Retry-After` header states the whole seconds until the budget has one request, at least 1. Requests under `/v1` with no valid key share one budget of the same size, and one that finds it empty gets 429 in place of 401, which limits how fast a caller can guess keys. A CORS preflight request, and a request that gets 421 `misdirected_request`, take nothing. The paths outside `/v1` have no limit. The server counts in the memory of one process, so each process that shares a database file gives a client the whole budget. `BXAPI_RATE_LIMIT_PER_MINUTE=0` turns the limit off.
- **Browser security headers.** Every response has `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`. Every response except the pages at `/docs` and `/redoc` also has `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. A browser that loads a response as a page runs nothing from it, and no page can show a response in a frame. The two pages have the policies that **OpenAPI** above describes. The `bxapi` command sends no `Server` header.
- **Request ids.** The server gives every request an id and returns it in the `X-Request-Id` response header, on every response, including an error and the answer to a CORS preflight request. A request can choose its id by sending `X-Request-Id` with 1 to 128 letters, digits, and the characters `.`, `_`, `~`, and `-`. For a request with no such header, or with any other value, the server draws an id made of `req_` and 22 random URL-safe characters. A problem document repeats the id as `request_id`. The server logs one line for each request to the `bxapi.access` logger when the response is sent: the id, the method, the path without the query string, the status, the time taken in milliseconds, and the client name of the request's API key, or `-` for a request with no valid key. The traceback the server logs for 500 `internal_error` has the id too.

### Authentication and roles

A client sends an API key as a bearer token: `Authorization: Bearer <key>`. The scheme name `Bearer` is case-insensitive. The server's `BXAPI_API_KEYS` setting lists each key with a client name and a role. A key is at least 16 printable ASCII characters, with no spaces.

bxapi has no user accounts. A key identifies a client application, not a person. Everyone who has one of an application's keys shares that application's data: they can read and delete all of its characters, adventures, sessions, and saves, send commands to its sessions, rename its sessions, and relabel its saves. An application with many users either gives each user only the ids of that user's resources, which are random and can't be guessed, or sends its users' requests to bxapi through a server that the application runs. The role limits what the holder of a key can see and do in a game, not whose data they can reach.

| Role | May do |
| --- | --- |
| `player` | Everything a game client needs: read the rules, roll dice, create characters, start and play sessions, save and restore. |
| `referee` | Everything a player may do, and also: read the referee view and referee events, send referee commands, choose seeds, import characters and saves, and read full adventure and save documents. |

A server with no keys doesn't start, unless `BXAPI_ALLOW_ANONYMOUS` is `true`. With that setting, the server asks for no key and treats every caller as a referee for the client `local`. Use it only on your own machine. With keys, `BXAPI_ALLOW_ANONYMOUS` has no effect.

A server that asks for no key answers a request under `/v1` only when the request's `Host` header is `localhost`, `127.0.0.1`, or `[::1]`, in any letter case, with or without a port. A request with another `Host`, or with none, gets 421 `misdirected_request`. Without the check, a web page on another site could read and change the server's data from the browser of the person who runs the server. The page points its own host name at `127.0.0.1`, which is called DNS rebinding, and the browser then lets the page call the server and read the responses. The browser sends the page's host name in `Host`, so the server refuses the request. The check doesn't stop a program that sends `Host: localhost` itself, so keep such a server on the default `BXAPI_HOST`. A server with keys answers any `Host`, because a page that has no key gets 401.

At startup, the server logs the number of keys, the client names, and the CORS origins, as a warning when it asks for no key.

The server checks the key, the `Host` of a request to a server with no keys, and the rate limit before it reads the request's body, and it checks the body's size and arrival time before the body's `Content-Type`. A request under `/v1` with no key or an unknown key gets 401 `unauthorized`, whatever its path, method, or body. A CORS preflight request needs no key. A player's request for something only a referee may do gets 403 `referee_required`.

### Errors

Every error response is an RFC 9457 problem document with the media type `application/problem+json`. `type` is always `about:blank`. A client reads `code`. `detail` is a short description for a person, at most 1,000 characters. `request_id` is the request's id, the same as the `X-Request-Id` header. A document has only the extra members that the table below lists for its code. A client ignores any member it doesn't know.

| Status | `code` | When | Extra members |
| --- | --- | --- | --- |
| 400 | `bad_request` | The server can't decode the body's bytes as text, or the body has a value the server refuses: an integer with more than 4,300 digits, or one of the values that **JSON values** lists. `detail` states which. The server decodes a body as UTF-8, or as UTF-16 or UTF-32 when the body starts with that encoding's byte order mark or zero bytes. | |
| 401 | `unauthorized` | The key is missing or unknown. | |
| 403 | `referee_required` | A player asked for something only a referee may do, including a field like `seed`. | |
| 403 | `referee_command` | A player sent a referee command. | `command_type` |
| 403 | `adventure_read_only` | The caller tried to delete an adventure that comes with the server. | |
| 404 | `not_found` | No resource the caller can read has that id, or no route has that path. | `resource`, `id` |
| 405 | `method_not_allowed` | The path exists, but not with that method. | |
| 409 | `revision_conflict` | `expected_revision` doesn't match the session's revision. | `revision` |
| 409 | `session_busy` | The request waited more than 5 seconds for the session's lock, or another server process wrote the session after this request read it and before this request wrote it. The request had no effect. | `revision` |
| 409 | `ability_roll_used` | A character was already created from that ability roll. | |
| 409 | `idempotency_key_in_progress` | The first request with the `Idempotency-Key` is still running. The response has the header `Retry-After: 1`. | |
| 409 | `idempotency_key_interrupted` | The server failed or stopped while it handled the first request with the `Idempotency-Key`, and has no record of whether that request took effect. | |
| 409 | `version_unsupported` | A document is stamped with a schema version newer than the server's osrlib. | |
| 408 | `request_timeout` | The request body didn't arrive within `BXAPI_BODY_TIMEOUT_SECONDS`. | |
| 413 | `content_too_large` | The request body is larger than `BXAPI_MAX_BODY_BYTES`, or larger than 65,536 bytes on an operation that takes no document. | |
| 415 | `unsupported_media_type` | The request has a body, and its `Content-Type` isn't a JSON media type. | |
| 421 | `misdirected_request` | The server asks for no API key, and the request's `Host` header isn't `localhost`, `127.0.0.1`, or `[::1]`. | |
| 422 | `validation_error` | The request doesn't match the endpoint's schema, or its body decodes as text but isn't valid JSON. A body that isn't valid JSON gets one error with the type `json_invalid`. | `errors`, the first 20 of pydantic's errors. Each has `loc`, `msg`, and `type`, and no copy of the value sent. The server cuts each name in `loc` and each `msg` to 128 characters. `error_count` is how many errors there are. An object with more than 20 fields the endpoint doesn't define gets one error, whose `msg` states how many there are. |
| 422 | `idempotency_key_reused` | The client already sent the `Idempotency-Key` with a different method, path, query string, body, or role. | |
| 422 | `content_invalid` | osrlib refused a document or a value as malformed, or the server refused an imported character. | |
| 422 | `character_invalid` | The choices for a new character break a rule, or name a class or an item osrlib doesn't have. | `rejections`, when osrlib reports them. Otherwise `field` (the request field that's wrong, like `equip_item_ids`), `index` (the entry's position, when `field` is a list), and `id` (the value of that entry). |
| 422 | `party_invalid` | The characters named for a new session can't form a party: osrlib refused the party, a `party_document` has more than 20 members, or two members end up with the same id. | |
| 429 | `rate_limited` | The client sent more requests in a minute than `BXAPI_RATE_LIMIT_PER_MINUTE`, or the server got that many with no valid API key. The response has the header `Retry-After`, with the seconds to wait. | |
| 500 | `internal_error` | A bug. The response says nothing about the cause, and the server logs the traceback. | |
| 500 | `session_unreadable` | The server's osrlib can't load a stored session, or a stored save to restore. The `detail` says whether it's the session's or the save's stored state. The response contains nothing else from the stored state, and the server logs the cause with the id. | |
| 503 | `database_busy` | Another process held the database's write lock for longer than the 5 seconds the server waits. The response has the header `Retry-After: 1`. Send the request again. | |

## Endpoints

### Meta

| Method and path | Role | Returns |
| --- | --- | --- |
| `GET /health` | none | `{"status": "ok"}`. A load balancer can call it with no key. |
| `GET /v1/meta` | player | `api_version`, `engine_version`, `schema_version`, `auth_enabled`, `client`, `role` |

### Rules reference

Read-only views of the rules content osrlib loads with the functions in `osrlib.data`. A list returns a page of summaries. A detail endpoint returns osrlib's model for that entry, unchanged. `q` matches a substring of an entry's id or name, ignoring case. An unknown id gets 404. Every successful response under `/v1/rules` has the header `Cache-Control: private, max-age=3600`, because the content changes only when the server's osrlib does. An error response doesn't have it.

The rules reference is the rulebook, so every role can read all of it, including the entries a referee would normally keep to themselves, like what a cursed item does. The player view of a session shows what that session's players have learned about a particular item. Its inventory masks the name of an unidentified item, but its character sheets include the effect of every item a member wears, as design rule 2 describes.

| Method and path | Returns |
| --- | --- |
| `GET /v1/rules/classes` | A page of class summaries: id, name, and the facts a class picker shows (race, prime requisites, requirements, hit die, maximum level). |
| `GET /v1/rules/classes/{class_id}` | The class definition. |
| `GET /v1/rules/monsters?q=` | A page of monster summaries: id, name, hit dice, armour class (descending and ascending), XP, alignment. |
| `GET /v1/rules/monsters/{monster_id}` | The monster template. |
| `GET /v1/rules/spells?q=&class_id=&level=` | A page of spell summaries: id, name, level, `spell_list`, range, and duration. `class_id` selects the spells on that class's spell list. A class that casts no spells gets an empty page, and an unknown class gets 404. |
| `GET /v1/rules/spells/{spell_id}` | The spell template. |
| `GET /v1/rules/equipment?q=&item_type=` | A page of equipment summaries: id, name, `item_type` (`weapon`, `armour`, `gear`, or `ammunition`), and `cost_gp`, listed weapons first, then armour, gear, and ammunition. |
| `GET /v1/rules/equipment/{item_id}` | The item template. |
| `GET /v1/rules/magic-items?q=&category=` | A page of magic item summaries: id, name, category. `category` is one of osrlib's magic item categories. |
| `GET /v1/rules/magic-items/{item_id}` | The magic item template. |
| `GET /v1/rules/languages` | A page of languages, in alphabetical order by id. The alignment languages aren't in osrlib's catalog, so they aren't listed. |
| `GET /v1/rules/tables` | A page of the names of the printed tables: `abilities`, `combat`, `encounters`, `treasure`. |
| `GET /v1/rules/tables/{name}` | One set of tables, as osrlib's model. |
| `GET /v1/rules/commands?kind=&mode=` | A page of commands: `command_type`, `kind` (`play` or `referee`), `allowed_modes`, and the summary line of its documentation. osrlib has more commands than the default `limit`, so a client that wants them all passes `limit=200`. |
| `GET /v1/rules/commands/{command_type}` | The same, with `json_schema`, the JSON Schema of the command. |
| `GET /v1/rules/license` | `name` and `text`: the Open Game License and its Section 15 notice, from osrlib's data package. |

Each summary field is a field of osrlib's model, with the same name and value. A command's `kind` and `summary` are the exceptions.

### Dice and generators

Stateless. Any role. Every request body accepts an optional `seed` (an integer from 0 to 2^53 - 1, the largest integer a JavaScript client reads exactly). When the request has none, the server draws one. Every response includes the `seed` it used. The same request with the same seed returns the same result on the same osrlib version.

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /v1/dice/roll` | `expression`, `times` (1 to 100, default 1) | `expression`, `seed`, and `rolls`: one osrlib `RollResult` (`rolls`, `modifier`, `multiplier`, `total`) for each time. |
| `POST /v1/generate/character` | The choices `POST /v1/characters` accepts, except `ability_roll_id`. | `seed`, `character`, `derived`, and the creation `rolls`. The server stores nothing. |
| `POST /v1/generate/treasure` | `treasure_type` (a letter), `tier` (`basic` or `expert`) | `seed` and `treasure`, osrlib's generated treasure. |
| `POST /v1/generate/npc-party` | `kind` (`basic` or `expert`), `count` (1 to 100) | `seed` and `party`, osrlib's NPC party. |
| `POST /v1/generate/room` | `level_number` (1 or more) | `seed` and `room`, the stocked area osrlib rolled. |
| `POST /v1/generate/monsters` | `monster_id`, `count` (1 to 100, default 1) | `seed` and `monsters`, each with rolled hit points. |

A dice expression osrlib can't parse gets 422 `content_invalid`. An unknown monster id or treasure type gets 422 `content_invalid`.

### Characters

The server rolls a character and stores it. When a session starts, the server copies its characters into it, so a stored character stays as it was created, and you can start any number of sessions with it.

B/X players roll ability scores before they choose a class. To support that, a client first asks for an *ability roll*, shows the player the scores and the classes they qualify for, and then creates the character from that roll. The server rolls the scores from a seed and stores the seed with the roll, and it never returns that seed. The character created from the roll gets its hit point and gold rolls from the same seed, so every attempt to create a character from one roll, like a retry after osrlib refuses a choice, gets the same rolls. bxapi doesn't limit how many ability rolls a client asks for, so a game that wants a limit enforces it.

| Method and path | Role | Body | Returns |
| --- | --- | --- | --- |
| `POST /v1/ability-rolls` | player | `seed` (referee only), which determines the scores and the hit point and gold rolls. The body is optional. | 201: the roll: `id`, `scores`, `eligible_class_ids`, `used` (`false`), `created_at` |
| `GET /v1/ability-rolls/{roll_id}` | player | | The roll, with `used` set to whether a character was created from it. |
| `POST /v1/characters` | player | See below. | 201: the stored character. |
| `POST /v1/characters/import` | referee | A character document. | 201: the stored character. |
| `GET /v1/characters` | player | | A page of character summaries: `id`, `name`, `class_id`, `level`, `max_hp`, `created_at`. |
| `GET /v1/characters/{character_id}` | player | | The stored character. |
| `DELETE /v1/characters/{character_id}` | player | | 204 |

The body of `POST /v1/characters`:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | The character's name, 1 to 100 characters, with no control character. osrlib accepts a name of spaces. |
| `class_id` | yes | A class id from `/v1/rules/classes`. |
| `alignment` | yes | `lawful`, `neutral`, or `chaotic`. |
| `ability_roll_id` | no | Create the character from the roll's scores, with the hit point and gold rolls of the roll's seed. Without it, the server rolls the scores as part of the request. |
| `adjustment` | no | A trade of ability points, as osrlib's `AbilityAdjustment`: `lowered` and `raised`, each a map from an ability to a number of points. osrlib checks the trade. |
| `starting_spell_ids` | no | An arcane caster's first spells. |
| `extra_languages` | no | The extra languages a high Intelligence allows. |
| `purchases` | no | Starting gear to buy, in order: a list of `{item_id, quantity}`. `quantity` is a number of osrlib's purchase lots, from 1 to 10,000, and defaults to 1. osrlib sells weapons and armour one at a time, and gear and ammunition in bundles, so one lot of torches is six torches. |
| `equip_item_ids` | no | Which purchased items to equip. |
| `hp_reroll_at_first_level` | no | The optional rule that rerolls a first hit die of 1 or 2. Default `false`. |
| `seed` | no | Referee only. A request with both `seed` and `ability_roll_id` gets 422 `validation_error`, because the roll's seed determines the rolls. |

`scores` is osrlib's score table, keyed by `str`, `int`, `wis`, `dex`, `con`, and `cha`. `eligible_class_ids` lists, in osrlib's class order, each class for which osrlib's `validate_class_choice` returns no rejections.

A stored character is `{id, name, class_id, level, created_at, character, derived}`. `character` is osrlib's `Character` model. `derived` contains the values osrlib computes from it and doesn't serialize. Each key is the name of the `Character` property or the osrlib function that computes it:

- `thac0` and `attack_bonus`.
- `armour_class` (descending) and `armour_class_ascending`.
- `saves`, osrlib's `SavingThrows`.
- The ability modifiers: `melee_modifier`, `open_doors_chance`, `missile_modifier`, `initiative_modifier`, `hit_point_modifier`, `magic_save_modifier`, and `npc_reaction_modifier`.
- `xp_modifier_pct` and `level_title`, from the functions of those names in `osrlib.core.classes`. `level_title` is `null` past the class's last title.
- `alignment_tongue` and `languages`.

`derived` leaves out the movement rate, because the movement rate depends on the ruleset of the session the character plays in. In a session, each member's character sheet in the player view includes `movement_rate_feet`, which osrlib computes under the session's ruleset.

The server creates a character with osrlib's step-by-step creation functions, in the order osrlib lists them: roll the scores (unless the request names an ability roll), check the class, check and apply the adjustment, check the first spells, roll hit points, check the extra languages, roll starting gold, buy each purchase, and equip each item. If a step refuses a choice, creation stops there with 422 `character_invalid`. The response includes osrlib's `rejections` when that step returns them. A class id or an item id osrlib doesn't have gets 422 `character_invalid` with `field` and `id`, and `index` for a list field, instead of `rejections`. So does an id in `equip_item_ids` that isn't in the purchases, or that appears there more times than the purchases buy it. Equipping a purchase equips all of it, whatever its `quantity`. A malformed body, like an unknown `alignment`, a `quantity` of 0, or a name longer than 100 characters, gets 422 `validation_error`.

When a choice breaks a rule, an ability roll named in the request stays unused. A successful creation marks the roll used and stores the character in one database transaction, so two creations from one roll never both succeed, and a failed creation never marks the roll used. Deleting the character doesn't free the roll. A second creation from the same roll gets 409 `ability_roll_used`. An `ability_roll_id` that matches no roll of the caller's client gets 404.

The `document` column of a `characters` row is what osrlib's `Character.to_document` returns for the stored character. `POST /v1/characters/import` accepts the same document, with any character osrlib's `Character` model accepts, like a fighter at level 14. The endpoint stores the character with a new `chr_` id. It clears the `id` field of the `character` itself, because osrlib gives each party member without an id a new one when a session starts. The server builds `derived` before it stores anything. A document that isn't a valid character document gets 422 `content_invalid`, and so does a character osrlib can't compute `derived` for, like one that wears a magic item osrlib doesn't have, and a character whose `max_hp` is larger than a SQLite integer. A document stamped with a newer schema version gets 409 `version_unsupported`.

An import also gets 422 `content_invalid` when the character has a stat modifier, or a condition with an `effect_id`. Each of these refers to an effect in the game the character came from. In a new session such a condition would never end, and osrlib would remove such a modifier when an unrelated effect with the same `effect_id` ended. A condition with no `effect_id`, like `dead`, is accepted.

`POST /v1/generate/character` runs the same steps and stores nothing. Its `rolls` is `{ability_rolls, hit_point_roll, gold_roll}`: osrlib's `AbilityScoreRolls`, `HitPointRoll`, and `RollResult`. With the same seed and choices, it returns the character osrlib's `create_character` returns.

### Adventures

An adventure is the content a session plays: a town, dungeons, and the triggers and quests that drive them. Two separate projects by osrlib's author write adventure documents: osr-forge converts adventure module PDFs into them, and osr-editor is a local graphical application for writing and editing them. The server also includes at least one small adventure, so a new client can start a game without uploading anything.

| Method and path | Role | Returns |
| --- | --- | --- |
| `POST /v1/adventures` | player | 201: the adventure's info. The body is an adventure document, which the server validates with osrlib before it stores it. |
| `POST /v1/adventures/validate` | player | 200: `{"valid": true, "adventure": info}` or `{"valid": false, "code": "...", "detail": "..."}`. Stores nothing. |
| `GET /v1/adventures` | player | A page of adventure info: the adventures that come with the server first, then the client's. |
| `GET /v1/adventures/{adventure_id}` | player | The adventure's info. |
| `GET /v1/adventures/{adventure_id}/document` | referee | The adventure document as uploaded, including any field osrlib ignores. |
| `DELETE /v1/adventures/{adventure_id}` | player | 204. An adventure that comes with the server gets 403 `adventure_read_only`. |

Adventure info contains only what a player may know before play:

| Field | Meaning |
| --- | --- |
| `id` | The adventure's id. `null` in the result of `POST /v1/adventures/validate`. |
| `name` | The adventure's title. |
| `builtin` | Whether the adventure comes with the server. |
| `created_at` | When the client stored the adventure. `null` for an adventure that comes with the server, and in the result of `POST /v1/adventures/validate`. |
| `description` | The adventure's `description`, the text its author wrote for players. |
| `hooks` | The adventure's `hooks`: the reasons a party might take it on. |
| `party` | osrlib's `PartySpec`: the character levels and party size the adventure is written for, or `null`. |
| `town` | `{name, description}` of the town. |
| `dungeons` | A list of `{id, name}`, one for each dungeon. Info leaves out how many levels a dungeon has, because osrlib's player view shows only the levels the party has explored. |

Adventure info never contains map geometry, keyed areas, monsters, treasure, triggers, or quest internals.

The server stores the adventure document as uploaded, and parses it again when a session starts. Two uploads of the same document store two adventures, each with a different id. The list puts the adventures that come with the server first, in id order, and then the client's adventures, newest first. `limit`, `offset`, and `total` count the two parts as one list. An upload that osrlib refuses gets 422 `content_invalid`, and a document stamped with a newer schema version gets 409 `version_unsupported`. `POST /v1/adventures/validate` returns `valid: false` in both cases, with `code` set to the code the upload would return. osrlib checks a stored adventure again each time the server parses it, so `POST /v1/sessions` gets 422 `content_invalid` for a stored adventure that an earlier osrlib release accepted and the server's release refuses.

A session embeds a copy of its adventure, so deleting an adventure doesn't affect a session that already started.

### Sessions

A session is one game in progress. It starts in town and ends in `game_over` or `victory`.

| Method and path | Role | Returns |
| --- | --- | --- |
| `POST /v1/sessions` | player | 201: session info. |
| `GET /v1/sessions?mode=` | player | A page of session info. |
| `GET /v1/sessions/{session_id}` | player | Session info. |
| `PATCH /v1/sessions/{session_id}` | player | Session info, after changing `name`. |
| `DELETE /v1/sessions/{session_id}` | player | 204. The session's saves stay. |
| `GET /v1/sessions/{session_id}/commands` | player | `mode`, `revision`, and `commands`: the `command_type` and `kind` of each command that's legal in the session's mode and that the caller's role may send. |
| `POST /v1/sessions/{session_id}/commands` | player | The command outcome, with the player view when the request has `include=view`. |
| `GET /v1/sessions/{session_id}/view` | player | osrlib's `PlayerView`: each member's character sheet, the explored map, the encounter as the players see it, the journal, the quests, and the town's healing prices. |
| `GET /v1/sessions/{session_id}/referee-view` | referee | osrlib's `RefereeView`. |
| `GET /v1/sessions/{session_id}/events` | player | A page of the event log. |
| `POST /v1/sessions/{session_id}/saves` | player | 201: save info. The body is `{"label": "..."}`, and both the body and `label` are optional. A save without a label has `label: null`. Saving doesn't change the session or its revision. |

The body of `POST /v1/sessions`:

| Field | Required | Meaning |
| --- | --- | --- |
| `adventure_id` | yes | The adventure to play. |
| `character_ids` | one of the two | The stored characters that form the party, in marching order, at most 20. The session uses copies, so the list can contain a character more than once. |
| `party_document` | one of the two | Referee only. A party document, in place of stored characters, with at most 20 characters. |
| `name` | no | A label for the session, 1 to 200 characters, with no control character. Defaults to the adventure's name. |
| `ruleset` | no | osrlib's `Ruleset` fields, to change optional rules. Defaults to the rules as written. |
| `seed` | no | Referee only. |

A request with both `character_ids` and `party_document`, or with neither, gets 422 `validation_error`. A player who sends `party_document` or `seed` gets 403 `referee_required`. An unknown adventure or character id gets 404 `not_found`, with `resource` set to `adventure` or `character`. osrlib requires a party of at least one member, and a party it refuses gets 422 `party_invalid`. A `party_document` in which two members end up with the same id gets 422 `party_invalid` too, because a command identifies a party member by id. A malformed `party_document` gets 422 `content_invalid`.

The server allows at most 20 members in a new session's party. The server sets this limit, and it isn't a rule of the game. After every accepted command, the server writes the whole party into the session's save document, so a larger party slows every command for the server's other clients. More than 20 `character_ids` gets 422 `validation_error`, and a `party_document` with more than 20 characters gets 422 `party_invalid`.

Session info is `{id, name, adventure_id, adventure_name, mode, ended, revision, clock, schema_version, engine_version, created_at, updated_at}`. `clock` is `{rounds, turns, days}`, with the turns and days that osrlib's `GameClock` counts. `ended` is `true` in the modes `game_over` and `victory`. `updated_at` is the time of the last accepted command, or `created_at` before the first one. Renaming the session doesn't change `updated_at`.

`adventure_id` is the id of the adventure the session started with, and it can be the id of an adventure that has since been deleted. A session restored from a save has the save's `adventure_id`, which is `null` for an imported save. Neither a deleted adventure nor a `null` id changes play, because a session uses the copy of the adventure in its save document.

#### Commands

The body of `POST /v1/sessions/{session_id}/commands` is one osrlib command, like `{"command_type": "move_party", "direction": "north"}`. The request body's schema is osrlib's `AnyCommand` union, so the OpenAPI document describes every command. An unknown `command_type` or a payload that doesn't fit its command gets 422.

The command outcome:

| Field | Meaning |
| --- | --- |
| `accepted` | Whether osrlib accepted the command. |
| `rejections` | osrlib's rejections when it didn't: each a `code` and `params`. |
| `events` | The events the command caused, as osrlib serializes them, in order. |
| `messages` | One line of English for each event, in the same order, from osrlib's `format_message`. |
| `mode` | The session's mode after the command. |
| `ended` | Whether the session is over. |
| `revision` | The session's revision after the command. |
| `view` | osrlib's `PlayerView` after the command, only when the request has `include=view`. |

A request with the query parameter `include=view` gets the player view in the outcome's `view`, for an accepted command and for a refused one. The server builds the view while it still has the session's lock, so it's the view at the outcome's `revision`, and it's the player view for a referee too. Without `include`, the outcome has no `view` member. Another value of `include` gets 422 `validation_error`.

A caller sees the events with player visibility. A referee who passes `visibility=referee` sees every event. A player who passes it gets 403 `referee_required`, and the server doesn't execute the command. A player who sends a referee command gets 403 `referee_command`, and the server doesn't execute it. The server checks the body against the command's schema before it checks the role, so a referee command that doesn't fit its schema, like a `roll_dice` with the expression `2x7`, gets 422 `validation_error` from a player too. osrlib ignores a field that the command doesn't define.

Every command has an optional `source`: a note of who or what issued the command, which osrlib logs with it and never reads. osrlib's `Interpreter` sets it to `trigger:` or `quest:` and an id on the commands it issues for the adventure's triggers and quests, so the command log shows why each one ran. Only a referee may set `source`. A player who sends a command with a `source` gets 403 `referee_required`, and the server doesn't execute the command.

**Revisions.** A session's revision starts at 0 and goes up by 1 for each command request that osrlib accepts, whatever commands the adventure's triggers run in response. A refused command leaves it unchanged. The revision and `updated_at` count every accepted command, a referee's too, so a player can tell that something happened without seeing what. A client that might send a request twice, like a retry after a timeout, sends an `Idempotency-Key`, as **Conventions** describes, or passes `expected_revision` as a query parameter. If the session's revision is different, the server executes nothing and returns 409 `revision_conflict` with the current `revision`. The client then reads the view and decides what to do.

**Busy sessions.** 409 `session_busy` means the command had no effect, and the problem has the session's current `revision`. If you sent `expected_revision`, read the session before you decide what to do: when the revision has changed, sending the same request again gets `revision_conflict`. If you didn't send `expected_revision`, send the command again.

#### Events

`GET /v1/sessions/{session_id}/events` takes `after` (default 0), `limit` (1 to 500, default 100), and `visibility` (`player` by default, and a referee can pass `referee`). It returns `{events, messages, next_after, total}`. `after` is the number of events of the requested visibility that the client has already read, and `next_after` is the value to pass next time. A client that reconnects reads from its last `next_after` to catch up.

#### How the server runs a session

- The server registers osrlib's `Interpreter` on every session it creates, restores, or loads, so the adventure's triggers and quests run.
- After every accepted command, the server writes the session's save document, its mode, and its revision to the database before it responds. A server that restarts loses no accepted command. If a command's response didn't reach the client, because the connection or the server failed, the server may have applied the command. Send an `Idempotency-Key` with every command you might send twice, so that a retry of an applied command gets the first response. Without a key, send `expected_revision`, so that the retry gets `revision_conflict` instead of running again.
- After each accepted command, the server writes the whole save document, event log included, so a command takes longer as the game goes on. On the built-in adventure `builtin-barrow`, a command request takes about 2 ms at revision 5, 5 ms at revision 600, and 9 ms at revision 2,000, when the document is about 510 kB. A game of tens of thousands of commands can have a save document larger than `BXAPI_MAX_BODY_BYTES`, and a server with the same setting can't import that document.
- The server keeps up to `BXAPI_SESSION_CACHE_SIZE` sessions loaded in memory, each with a lock. A request takes the session's lock to execute a command, read a view, or read the event log, so a view or a page of events always shows the session between two commands. A request waits at most 5 seconds for the lock, and then gets 409 `session_busy` without having executed or read anything. The limit keeps requests for one slow session from taking every worker thread the server has. When the cache is full, the server drops the session used longest ago and loads it from the database when a request next needs it. A request that's using a dropped session finishes with its copy, and a request that arrives meanwhile waits for the same lock.
- Before each request uses a loaded session, the server compares the revision in the database with the revision of the loaded copy. If they differ, another process changed the session, and the server loads it again.
- The write after a command succeeds only if the database still has the revision the command started from. If it doesn't, another process wrote first, so the server drops its loaded copy and returns 409 `session_busy`. Two processes that share one database file can therefore serve the same session without losing or repeating a command.
- If osrlib raises an exception while it executes a command, the server drops the loaded copy, writes nothing, and returns 500 `internal_error`. The next request loads the session from the database.
- If osrlib can't load a session's stored document, for example because a server with another osrlib release wrote it to a shared file, the request gets 500 `session_unreadable`. The response contains nothing from osrlib's message, which can quote the stored state, and the server logs the message with the session's id.
- The endpoints for session info, the session list, the commands list, renaming, and saving read the `sessions` table and don't take the lock. A save copies the save document of the last accepted command.

### Saves

A save is a copy of a session at one moment. Restoring a save creates a new session and leaves the save and the original session as they were.

| Method and path | Role | Returns |
| --- | --- | --- |
| `GET /v1/saves?session_id=` | player | A page of save info. |
| `GET /v1/saves/{save_id}` | player | Save info. |
| `PATCH /v1/saves/{save_id}` | player | Save info, after changing `label`. The body is `{"label": "..."}`, and `null` removes the label. |
| `DELETE /v1/saves/{save_id}` | player | 204 |
| `POST /v1/saves/{save_id}/restore` | player | 201: info for the new session, at revision 0. The body is `{"name": "..."}`, and both the body and `name` are optional. |
| `GET /v1/saves/{save_id}/document` | referee | The save document. |
| `POST /v1/saves/import` | referee | 201: save info. The body is a save document. |

Save info is `{id, session_id, adventure_id, adventure_name, label, mode, clock, created_at}`. `session_id` is the session the save was made from, which may have been deleted since. `adventure_id` is the `adventure_id` of that session when the save was made, and it can be the id of an adventure that has since been deleted. Both are `null` for an imported save, and `adventure_id` is `null` for a save of a session restored from an imported save.

A restored session plays on from the save as the original session would have: the server loads the document with osrlib's `load_game`, checks its adventure with osrlib's `validate_adventure`, and registers the `Interpreter` again. A stored save that osrlib can't load, or whose adventure it refuses, gets 500 `session_unreadable`. That includes a save stored before the server's osrlib was upgraded, when its adventure fails a check that the newer osrlib added, like the 2.5.0 check of the keys in a level's `edges`.

An import loads the document and checks its adventure the same way before it stores anything, and stores the document osrlib writes back out of the loaded game, in the format of the server's osrlib. A document osrlib can't load, an adventure that fails osrlib's checks, and a game clock larger than a SQLite integer get 422 `content_invalid`. The checks are the ones an upload runs: the adventure can't refer to a monster, an item, an area, or a quest that doesn't exist, and it can't have content that a session can't play, like a treasure type letter outside `A` to `V`. A document with a newer `schema_version` gets 409 `version_unsupported`.

## Storage

One SQLite database file, opened with Python's `sqlite3` module. No ORM. The `Database` class in `bxapi.db` is the only code that runs SQL. It uses one connection, write-ahead logging for a file database, and works with `":memory:"` for tests.

The server creates a new database file readable and writable only by the user that runs it (mode 0600), and SQLite gives the `-wal` and `-shm` files it creates the same mode. In an empty file, the server creates the tables and sets `PRAGMA user_version` to the version of the table layout, which is 2. The server refuses to start with a file of another version, or with a file that has tables and no version, and its message states the path, the version found, and the version expected. It also refuses a path it can't open, and states the path. The server has no migrations. A change to the tables changes the version, so the server refuses a file that a bxapi with other tables created.

| Table | Columns |
| --- | --- |
| `ability_rolls` | `seq`, `id`, `client`, `scores` (JSON), `seed`, `used`, `created_at` |
| `characters` | `seq`, `id`, `client`, `name`, `class_id`, `level`, `max_hp`, `document` (JSON), `created_at` |
| `adventures` | `seq`, `id`, `client`, `name`, `info` (JSON), `document` (JSON), `created_at` |
| `sessions` | `seq`, `id`, `client`, `name`, `adventure_id` (can be null), `adventure_name`, `mode`, `revision`, `clock_rounds`, `schema_version`, `engine_version`, `document` (JSON), `created_at`, `updated_at` |
| `saves` | `seq`, `id`, `client`, `session_id` (can be null), `adventure_id` (can be null), `adventure_name`, `label` (can be null), `mode`, `clock_rounds`, `document` (JSON), `created_at` |
| `idempotency_keys` | `client`, `key`, `fingerprint`, `status` (can be null), `body` (can be null), `claimed_at` |

Every query that reads or changes a row filters on `client`.

`seq` is each table's integer primary key, which SQLite assigns in the order the rows are inserted. `id` is unique. A list sorts by `created_at` and then by `seq`, both descending, so two rows created in the same second are always listed in the same order.

The primary key of `idempotency_keys` is `client` and `key`. `fingerprint` is a hash of the request's method, path, query string, role, and body. `status` and `body` are the stored response. `status` is null while the first request runs, and 0 after a request that failed with 500 `internal_error`. `claimed_at` is the time of the first request, in seconds since the Unix epoch. Each request with a key first deletes every row claimed 24 hours ago or earlier.

`schema_version` and `engine_version` in `sessions` are copies of the keys of the same names in the session's document. The `Database` class writes them each time it writes the document, so the server can return session info without reading the document.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `BXAPI_DATABASE` | `bxapi.db` | The path of the SQLite file, or `:memory:`. |
| `BXAPI_API_KEYS` | `[]` | A JSON list of `{"client", "key", "role"}`. `role` defaults to `player`, and a member other than `client`, `key`, and `role` is an error. |
| `BXAPI_ALLOW_ANONYMOUS` | `false` | Whether a server with no API keys starts. Such a server asks for no key and treats every caller as a referee for the client `local`. It answers a request under `/v1` only when the `Host` header is `localhost`, `127.0.0.1`, or `[::1]`. |
| `BXAPI_CORS_ORIGINS` | `[]` | A JSON list of the web origins whose pages may call the API. Each is a scheme, a host, and an optional port, like `https://game.example`, with no path and no trailing slash. `*` isn't allowed. Cross-origin responses expose the `WWW-Authenticate`, `Retry-After`, `ETag`, `Idempotent-Replayed`, and `X-Request-Id` headers. |
| `BXAPI_RATE_LIMIT_PER_MINUTE` | `600` | How many requests under `/v1` each client may send in a minute, in each server process. 0 turns the limit off. |
| `BXAPI_SESSION_CACHE_SIZE` | `256` | How many sessions stay loaded in memory. |
| `BXAPI_MAX_BODY_BYTES` | `8388608` | The largest request body. On an operation that takes no document, the limit is 65,536 bytes when this setting is larger. |
| `BXAPI_BODY_TIMEOUT_SECONDS` | `30` | How long the server waits for a request's whole body to arrive. |
| `BXAPI_DOCS_ENABLED` | `true` | Whether the server serves `/docs`, `/redoc`, and `/openapi.json`. |
| `BXAPI_HOST` | `127.0.0.1` | The address the `bxapi` command listens on. |
| `BXAPI_PORT` | `8640` | The port the `bxapi` command listens on. |

A setting the server can't accept stops it from starting, and the error message doesn't repeat the value, so a key doesn't end up in a log.

The server limits how long a request's body may take to arrive, but it can't limit how long a client takes to send the request's headers, or how long a connection stays open between requests. Run a deployed server behind a reverse proxy that enforces header and idle timeouts.

## Not in version 1

- Replaying a command log.
- Moving a session's characters back into the stored characters, to play them in another adventure.
- Adding a party member to a session, or replacing one, with a player key. A referee can with the `add_member` command.
- A step-by-step character builder beyond the ability roll.
- Rate limits and usage quotas.
- WebSockets or server-sent events.
- A database other than SQLite.
- Narration, or any call to a language model.
- Looking monsters up by `family_id`. `GET /v1/rules/monsters/{monster_id}` takes a template id, and the monster list has no `family_id` filter.
