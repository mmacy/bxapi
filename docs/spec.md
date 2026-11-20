# bxapi specification

bxapi is an HTTP API for the 1981 B/X (Basic/Expert) rules. Other applications call it as their backend: games that run a dungeon crawl, and utilities that look up a monster or roll a treasure. [osrlib](https://github.com/mmacy/osrlib-python) applies every rule. bxapi adds what a network service needs around it: authentication, storage, one-at-a-time access to each game, and an error format.

This file is the contract for version 1 of the API. `AGENTS.md` is the guide for working in the repository.

## Design rules

1. **osrlib applies every rule.** bxapi never computes a rule's result, adjusts one, or re-implements one. An endpoint validates its input, calls osrlib, stores what needs storing, and returns osrlib's models unchanged wherever a model exists.
2. **A player sees only what the players may see.** For a caller with the player role, the server builds every response from osrlib's player view and its player-visible events. The master seed, referee events, monster hit points, unexplored map, save documents, and full adventure documents go only to a caller with the referee role.
3. **A refused command is a result.** When the rules refuse a command, the response has status 200, `accepted` set to `false`, and osrlib's rejections. A refused command changes nothing in the game. An HTTP error status means the caller broke the API's contract.
4. **The server decides the dice.** The server draws each session's seed and never returns it. Only a referee can supply one. The stateless generators under `/v1/dice` and `/v1/generate` are different: any caller can pass a seed, and each response includes the seed it used, so the caller can repeat the roll.
5. **A client reads and changes only the data it created.** Every stored resource belongs to the client application that created it. A request for another client's resource gets the same 404 as a request for an id that doesn't exist.
6. **Endpoints are synchronous.** osrlib is synchronous and CPU-bound, so endpoints are plain `def` functions that FastAPI runs in its thread pool. The session manager makes sure one session executes one command at a time.

## Conventions

- **Base path.** Every endpoint except `GET /health` is under `/v1`.
- **Bodies.** Requests and responses are JSON. Field names are snake_case. A request body with a field the endpoint doesn't define gets 422 `validation_error`. A body that is an osrlib model, like a command, is the exception: osrlib ignores unknown fields.
- **Identifiers.** A stored resource has an id made of a prefix and 22 random URL-safe characters: `chr_` for a character, `rol_` for an ability roll, `adv_` for an adventure, `ses_` for a session, and `sav_` for a save. An adventure that comes with the server has an id that starts with `builtin-`.
- **Timestamps.** UTC, in RFC 3339 format with whole seconds, like `2026-11-20T22:01:05Z`.
- **Lists.** A list endpoint takes `limit` (1 to 200, default 50) and `offset` (default 0), and returns `{"items": [...], "total": n, "limit": n, "offset": n}`. Stored resources are listed newest first.
- **Documents.** osrlib wraps each serialized adventure, character, party, and save in an envelope with the keys `kind`, `schema_version`, `engine_version`, and `payload`. This specification calls the envelope a *document*. An endpoint that imports a document accepts the document as the whole request body.
- **OpenAPI.** `GET /openapi.json` describes every endpoint. Each operation's `operationId` is a short snake_case name, like `roll_dice`, that doesn't change between releases.
- **Creation.** A request that creates a stored resource returns 201 and the resource. A delete returns 204 with no body.

### Authentication and roles

A client sends an API key as a bearer token: `Authorization: Bearer <key>`. The server's `BXAPI_API_KEYS` setting lists each key with a client name and a role.

| Role | May do |
| --- | --- |
| `player` | Everything a game client needs: read the rules, roll dice, create characters, start and play sessions, save and restore. |
| `referee` | Everything a player may do, and also: read the referee view and referee events, send referee commands, choose seeds, import characters and saves, and read full adventure and save documents. |

When `BXAPI_API_KEYS` is empty, the server asks for no key and treats every caller as a referee for the client `local`.

A request with no key or an unknown key gets 401 `unauthorized`. A player's request for something only a referee may do gets 403 `referee_required`.

### Errors

Every error response is an RFC 9457 problem document with the media type `application/problem+json`. `type` is always `about:blank`. A client reads `code`.

| Status | `code` | When | Extra members |
| --- | --- | --- | --- |
| 401 | `unauthorized` | The key is missing or unknown. | |
| 403 | `referee_required` | A player asked for something only a referee may do, including a field like `seed`. | |
| 403 | `referee_command` | A player sent a referee command. | `command_type` |
| 403 | `adventure_read_only` | The caller tried to delete an adventure that comes with the server. | |
| 404 | `not_found` | No resource the caller can read has that id, or no route has that path. | `resource`, `id` |
| 405 | `method_not_allowed` | The path exists, but not with that method. | |
| 409 | `revision_conflict` | `expected_revision` doesn't match the session's revision. | `revision` |
| 409 | `session_busy` | Another server process changed the session while this one executed the command. Send the command again. | |
| 409 | `ability_roll_used` | A character was already created from that ability roll. | |
| 409 | `version_unsupported` | A document is stamped with a schema version newer than the server's osrlib. | |
| 413 | `content_too_large` | The request body is larger than `BXAPI_MAX_BODY_BYTES`. | |
| 422 | `validation_error` | The request doesn't match the endpoint's schema. | `errors`, pydantic's list |
| 422 | `content_invalid` | osrlib refused a document or a value as malformed. | |
| 422 | `character_invalid` | The choices for a new character break a rule. | `rejections`, when osrlib reports them |
| 422 | `party_invalid` | The characters named for a new session can't form a party. | |
| 500 | `internal_error` | A bug. The response says nothing about the cause, and the server logs the traceback. | |

## Endpoints

### Meta

| Method and path | Role | Returns |
| --- | --- | --- |
| `GET /health` | none | `{"status": "ok"}` |
| `GET /v1/meta` | player | `api_version`, `engine_version`, `schema_version`, `auth_enabled`, `client`, `role` |

### Rules reference

Read-only views of the rules content osrlib loads with the functions in `osrlib.data`. A list returns a page of summaries. A detail endpoint returns osrlib's model for that entry, unchanged. `q` matches a substring of an entry's id or name, ignoring case. An unknown id gets 404. Every successful response under `/v1/rules` has the header `Cache-Control: private, max-age=3600`, because the content changes only when the server's osrlib does. An error response doesn't have it.

The rules reference is the rulebook, so every role can read all of it, including the entries a referee would normally keep to themselves, like what a cursed item does. What a session's players have learned about a particular item is a different matter, and the player view covers it.

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
| `POST /v1/generate/character` | The choices `POST /v1/characters` accepts, except `ability_roll_id`. | `seed`, `character`, `derived`, and the creation `rolls`. Nothing is stored. |
| `POST /v1/generate/treasure` | `treasure_type` (a letter), `tier` (`basic` or `expert`) | `seed` and `treasure`, osrlib's generated treasure. |
| `POST /v1/generate/npc-party` | `kind` (`basic` or `expert`), `count` (1 to 100) | `seed` and `party`, osrlib's NPC party. |
| `POST /v1/generate/room` | `level_number` (1 or more) | `seed` and `room`, the stocked area osrlib rolled. |
| `POST /v1/generate/monsters` | `monster_id`, `count` (1 to 100, default 1) | `seed` and `monsters`, each with rolled hit points. |

A dice expression osrlib can't parse gets 422 `content_invalid`. An unknown monster id or treasure type gets 422 `content_invalid`.

### Characters

The server rolls a character and stores it. A session copies the characters it starts with, so a stored character stays as it was created and can start any number of sessions.

B/X players roll ability scores before they choose a class. To support that, a client first asks for an *ability roll*, shows the player the scores and the classes they qualify for, and then creates the character from that roll.

| Method and path | Role | Body | Returns |
| --- | --- | --- | --- |
| `POST /v1/ability-rolls` | player | `seed` (referee only) | 201: `id`, `scores`, `eligible_class_ids`, `created_at` |
| `GET /v1/ability-rolls/{roll_id}` | player | | The roll, with `used` set to whether a character was created from it. |
| `POST /v1/characters` | player | See below. | 201: the stored character. |
| `POST /v1/characters/import` | referee | A character document. | 201: the stored character. |
| `GET /v1/characters` | player | | A page of character summaries: `id`, `name`, `class_id`, `level`, `max_hp`, `created_at`. |
| `GET /v1/characters/{character_id}` | player | | The stored character. |
| `DELETE /v1/characters/{character_id}` | player | | 204 |

The body of `POST /v1/characters`:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | The character's name. |
| `class_id` | yes | A class id from `/v1/rules/classes`. |
| `alignment` | yes | `lawful`, `neutral`, or `chaotic`. |
| `ability_roll_id` | no | Create the character from these scores. Without it, the server rolls the scores as part of the request. |
| `adjustment` | no | A trade of ability points, as osrlib's `AbilityAdjustment`. |
| `starting_spell_ids` | no | An arcane caster's first spells. |
| `extra_languages` | no | The extra languages a high Intelligence allows. |
| `purchases` | no | Starting gear to buy: a list of `{item_id, quantity}`. |
| `equip_item_ids` | no | Which purchased items to equip. |
| `hp_reroll_at_first_level` | no | The optional rule that rerolls a first hit die of 1 or 2. Default `false`. |
| `seed` | no | Referee only. |

A stored character is `{id, name, class_id, level, created_at, character, derived}`. `character` is osrlib's `Character` model. `derived` contains the values osrlib computes from it and doesn't serialize: THAC0, both armour classes, the saving throws, the ability modifiers, and the languages.

When a choice breaks a rule, the response is 422 `character_invalid`, and an ability roll named in the request stays unused. A successful creation marks the roll used. A second creation from the same roll gets 409 `ability_roll_used`.

### Adventures

An adventure is the content a session plays: a town, dungeons, and the triggers and quests that drive them. Tools like osr-forge and osr-editor write adventure documents. The server also includes at least one small adventure, so a new client can start a game without uploading anything.

| Method and path | Role | Returns |
| --- | --- | --- |
| `POST /v1/adventures` | player | 201: the adventure's info. The body is an adventure document, which the server validates with osrlib before it stores it. |
| `POST /v1/adventures/validate` | player | 200: `{"valid": true, "adventure": info}` or `{"valid": false, "detail": "..."}`. Stores nothing. |
| `GET /v1/adventures` | player | A page of adventure info: the adventures that come with the server first, then the client's. |
| `GET /v1/adventures/{adventure_id}` | player | The adventure's info. |
| `GET /v1/adventures/{adventure_id}/document` | referee | The adventure document. |
| `DELETE /v1/adventures/{adventure_id}` | player | 204. An adventure that comes with the server gets 403 `adventure_read_only`. |

Adventure info contains only what a player may know before play: `id`, `name`, `builtin`, `created_at`, the town's name, each dungeon's id, name, and number of levels, and the text the adventure's author wrote for players to read before play, if the model has any. It never contains map geometry, keyed areas, monsters, treasure, triggers, or quest internals.

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
| `POST /v1/sessions/{session_id}/commands` | player | The command outcome. |
| `GET /v1/sessions/{session_id}/view` | player | osrlib's `PlayerView`. |
| `GET /v1/sessions/{session_id}/referee-view` | referee | osrlib's `RefereeView`. |
| `GET /v1/sessions/{session_id}/events` | player | A page of the event log. |
| `POST /v1/sessions/{session_id}/saves` | player | 201: save info. The body is `{"label": "..."}`, and `label` is optional. |

The body of `POST /v1/sessions`:

| Field | Required | Meaning |
| --- | --- | --- |
| `adventure_id` | yes | The adventure to play. |
| `character_ids` | one of the two | The stored characters that form the party, in marching order. |
| `party_document` | one of the two | Referee only. A party document, in place of stored characters. |
| `name` | no | A label for the session. Defaults to the adventure's name. |
| `ruleset` | no | osrlib's `Ruleset` fields, to change optional rules. Defaults to the rules as written. |
| `seed` | no | Referee only. |

Session info is `{id, name, adventure_id, adventure_name, mode, ended, revision, clock, schema_version, engine_version, created_at, updated_at}`. `clock` is `{rounds, turns, days}`. `ended` is `true` in the modes `game_over` and `victory`.

#### Commands

The body of `POST /v1/sessions/{session_id}/commands` is one osrlib command, like `{"command_type": "move_party", "direction": "north"}`. The request body's schema is osrlib's `AnyCommand` union, so the OpenAPI document describes every command. An unknown `command_type` or a payload that doesn't fit its command gets 422.

The command outcome:

| Field | Meaning |
| --- | --- |
| `accepted` | Whether the rules accepted the command. |
| `rejections` | osrlib's rejections when they didn't: each a `code` and `params`. |
| `events` | The events the command caused, as osrlib serializes them, in order. |
| `messages` | One line of English for each event, in the same order, from osrlib's `format_message`. |
| `mode` | The session's mode after the command. |
| `ended` | Whether the session is over. |
| `revision` | The session's revision after the command. |

A caller sees the events with player visibility. A referee who passes `visibility=referee` sees every event. A player who passes it gets 403 `referee_required`. A player who sends a referee command gets 403 `referee_command`, and the session never executes it.

**Revisions.** A session's revision starts at 0 and goes up by 1 each time the session accepts a command request. A refused command leaves it unchanged. A client that might send a request twice, like a retry after a timeout, passes `expected_revision` as a query parameter. If the session's revision is different, the server executes nothing and returns 409 `revision_conflict` with the current `revision`. The client then reads the view and decides what to do.

#### Events

`GET /v1/sessions/{session_id}/events` takes `after` (default 0), `limit` (1 to 500, default 100), and `visibility` (`player` by default, and `referee` for a referee). It returns `{events, messages, next_after, total}`. `after` is the number of events of the requested visibility that the client has already read, and `next_after` is the value to pass next time. A client that reconnects reads from its last `next_after` to catch up.

#### How the server runs a session

- The server registers osrlib's `Interpreter` on every session it creates, restores, or loads, so the adventure's triggers and quests run.
- After every accepted command, the server writes the session's save document, its mode, and its revision to the database. A server that restarts loses no accepted command.
- The server keeps up to `BXAPI_SESSION_CACHE_SIZE` sessions loaded in memory, each with a lock. A request takes the session's lock to execute a command or read a view. When the cache is full, the server drops the session used longest ago and loads it from the database when a request next needs it.
- Before each request uses a loaded session, the server compares the revision in the database with the revision of the loaded copy. If they differ, another process changed the session, and the server loads it again.
- The write after a command succeeds only if the database still has the revision the command started from. If it doesn't, the server discards the loaded copy and returns 409 `session_busy`. Two processes that share one database file can therefore serve the same session without losing or repeating a command.

### Saves

A save is a copy of a session at one moment. Restoring a save creates a new session and leaves the save and the original session as they were.

| Method and path | Role | Returns |
| --- | --- | --- |
| `GET /v1/saves?session_id=` | player | A page of save info. |
| `GET /v1/saves/{save_id}` | player | Save info. |
| `PATCH /v1/saves/{save_id}` | player | Save info, after changing `label`. |
| `DELETE /v1/saves/{save_id}` | player | 204 |
| `POST /v1/saves/{save_id}/restore` | player | 201: info for the new session. The body is `{"name": "..."}`, and `name` is optional. |
| `GET /v1/saves/{save_id}/document` | referee | The save document. |
| `POST /v1/saves/import` | referee | 201: save info. The body is a save document. |

Save info is `{id, session_id, adventure_name, label, mode, clock, created_at}`. `session_id` is the session the save was made from, which may have been deleted since. It's `null` for an imported save.

## Storage

One SQLite database file, opened with Python's `sqlite3` module. No ORM. The `Database` class in `bxapi.db` is the only code that runs SQL. It creates the tables when it opens the file, uses write-ahead logging for a file database, and works with `":memory:"` for tests.

| Table | Columns |
| --- | --- |
| `ability_rolls` | `seq`, `id`, `client`, `scores` (JSON), `used`, `created_at` |
| `characters` | `seq`, `id`, `client`, `name`, `class_id`, `level`, `max_hp`, `document` (JSON), `created_at` |
| `adventures` | `seq`, `id`, `client`, `name`, `info` (JSON), `document` (JSON), `created_at` |
| `sessions` | `seq`, `id`, `client`, `name`, `adventure_id`, `adventure_name`, `mode`, `revision`, `clock_rounds`, `schema_version`, `engine_version`, `document` (JSON), `created_at`, `updated_at` |
| `saves` | `seq`, `id`, `client`, `session_id`, `adventure_name`, `label`, `mode`, `clock_rounds`, `document` (JSON), `created_at` |

Every query that reads or changes a row filters on `client`.

`seq` is each table's integer primary key, which SQLite assigns in the order the rows are inserted. `id` is unique. A list sorts by `created_at` and then by `seq`, both descending, so two rows created in the same second are always listed in the same order.

`schema_version` and `engine_version` in `sessions` are copies of the keys of the same names in the session's document. The `Database` class writes them each time it writes the document, so the server can return session info without reading the document.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `BXAPI_DATABASE` | `bxapi.db` | The path of the SQLite file, or `:memory:`. |
| `BXAPI_API_KEYS` | `[]` | A JSON list of `{"client", "key", "role"}`. |
| `BXAPI_CORS_ORIGINS` | `[]` | A JSON list of the web origins whose pages may call the API. |
| `BXAPI_SESSION_CACHE_SIZE` | `256` | How many sessions stay loaded in memory. |
| `BXAPI_MAX_BODY_BYTES` | `8388608` | The largest request body. |
| `BXAPI_HOST` | `127.0.0.1` | The address the `bxapi` command listens on. |
| `BXAPI_PORT` | `8640` | The port the `bxapi` command listens on. |

## Not in version 1

- Replaying a command log.
- Moving a session's characters back into the stored characters, to play them in another adventure.
- A step-by-step character builder beyond the ability roll.
- Rate limits and usage quotas.
- WebSockets or server-sent events.
- A database other than SQLite.
- Narration, or any call to a language model.
