# Run games as a referee

Use a referee key to see what a game hides from its players, send commands that players can't, and move characters and *saves*, copies of a game at one moment, in and out of the server.

In a tabletop game, the *referee* runs the game and knows the map, the monsters, and the dice. A bxapi key with the `referee` role gives your software the same knowledge. [Authentication](authentication.md) describes how to create keys and what each role may do.

**Keep referee keys on servers you trust.** Give a referee key only to software that runs on a server you trust, such as your game's backend or a tool that runs the game for you. If you put a referee key in a web page or a mobile app, every player who reads the key can see the hidden state of every game that your application has, and can change those games with referee commands.

## Before you begin

Start the server with a player key and a referee key for the same client, as [Authentication](authentication.md#configure-api-keys) shows, and put the referee key in the shell variable `KEY`:

```sh
export KEY=referee-key-0123456789
```

A referee can do everything a player can, so the walkthrough in [Play a game](play-a-game.md) works with this key too. The examples on this page use the characters and the save from that walkthrough, so complete it first, and replace each `chr_`, `ses_`, and `sav_` ID in a command with one of your own.

## Start a repeatable session

A *session* is a single game in progress, and its *seed* is a number that determines every die the session rolls. When a request to start a session has no `seed`, the server generates a random one. No session response includes the seed, so a player can't predict a roll. The seed appears only in a save's document, which only a referee can read.

A referee can choose the seed by passing `seed`, an integer from 0 to 9,007,199,254,740,991, in the request that starts a session. This request starts the session that the rest of this page uses, with seed 7:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_PUtflt1xPCTJIwhqMcAIrA", "chr_kqRWmM-1d1lMQFy-4CFVnw", "chr_8l3D6o0BvEL_V99x7NUK1w"], "seed": 7}'
```

```json
{
  "id": "ses_E_QSB7sk4KaykheFC1oSxw",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "town",
  "ended": false,
  "revision": 0,
  ...
}
```

Two sessions with the same seed, the same adventure, the same party, and the same commands in the same order roll the same dice. Use a seed to test your game, to reproduce a bug that a player reports, or to run the same scenario twice. A session that starts without a `seed` uses a random one, so the same commands produce different rolls.

A referee can also pass a `seed` to `POST /v1/ability-rolls` and `POST /v1/characters`. A request with a player key that passes a `seed` to any of these three endpoints returns `403 referee_required`.

## Send referee commands

A referee can send *referee commands*, which change a game directly. Every command has a `kind`, either `play` or `referee`, and a request with a player key that sends a `referee` command returns `403 referee_command`.

Two endpoints list the referee commands:

- `GET /v1/sessions/{session_id}/commands` lists the commands that are legal in a session's current mode. For a referee, the list includes both play and referee commands.
- `GET /v1/rules/commands?kind=referee` lists every referee command with its `allowed_modes`.

The list of every referee command starts with `grant_item`, `grant_coins`, `award_xp`, `set_flag`, `spawn_monsters`, and `spawn_npc_party`, and it includes `place_party`, `advance_time`, `complete_quest`, and `end_encounter`. Most referee commands are legal in every mode, whereas a few, such as `spawn_monsters`, `place_party`, and `end_encounter`, are legal in fewer modes. In a mode that isn't in a command's `allowed_modes`, the rules reject the command with `session.command.wrong_mode`.

A command refers to a party member by the ID from the player view (`GET /v1/sessions/{session_id}/view`), such as `character-0001` for the first character in *marching order*, the order in which the party walks. This request puts a rope in that character's pack, in the session you started above:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_E_QSB7sk4KaykheFC1oSxw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "grant_item", "character_id": "character-0001", "item_id": "rope"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [{"code": "exploration.item.acquired", "visibility": "player", "event_type": "item_acquired", "character_id": "character-0001", "item_ids": ["rope"], "coins_gp_value": 0, "origin": "grant"}],
  "messages": ["character-0001 acquires rope."],
  "mode": "town",
  "ended": false,
  "revision": 1
}
```

## Read referee events

Every event has a `visibility` of `player` or `referee`. Referee events report what only the referee may know, such as the surprise and reaction rolls and the treasure that the rules engine generates for a room. The server omits `referee` events from a player's responses. A referee receives them by adding the query parameter `visibility=referee` to a command request or an event log request.

Enter the dungeon and move east once, as in [Play a game](play-a-game.md#enter-the-dungeon). Then move east again into the guard room of the built-in adventure, this time with `visibility=referee`:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_E_QSB7sk4KaykheFC1oSxw/commands?visibility=referee" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [
    {"code": "exploration.party.moved", "visibility": "player", ...},
    {"code": "exploration.location.entered", "visibility": "player", ...},
    {"code": "treasure.hoard.generated", "visibility": "referee", "event_type": "hoard_generated", "cache_ref": "cache-0001", "treasure_types": ["C"], "coins_gp_value": 500, "valuable_ids": ["valuable-0001", "valuable-0002", "valuable-0003"], ...},
    {"code": "encounter.surprise.rolled", "visibility": "referee", "event_type": "surprise_rolled", "side": "monsters", "threshold": 2, "roll": 4, "surprised": false},
    {"code": "encounter.surprise.rolled", "visibility": "referee", "event_type": "surprise_rolled", "side": "party", "threshold": 3, "roll": 4, "surprised": false},
    {"code": "encounter.started", "visibility": "player", ...},
    {"code": "encounter.reaction.rolled", "visibility": "referee", "event_type": "reaction_rolled", "roll": 9, "modifier": 0, "total": 9, "result": "indifferent"},
    {"code": "encounter.stance.changed", "visibility": "player", ...}
  ],
  "messages": [
    "The party moves to (2, 0), facing east.",
    "The party enters area guard_room (level 1).",
    "Treasure generated at cache-0001 (C): 500 gp in coin, 3 valuable(s), 0 magic item(s).",
    "Surprise (monsters): rolled 4, surprised on 1-2 — not surprised.",
    "Surprise (party): rolled 4, surprised on 1-3 — not surprised.",
    "Encounter: 2 goblins at 20'.",
    "Reaction roll: 9+0 = 9 — indifferent.",
    "The monsters' bearing: indifferent."
  ],
  "mode": "encounter",
  "ended": false,
  "revision": 4
}
```

`messages` has one line for each event in `events`, so a referee's messages include the lines for the referee events. A request with a player key that adds `visibility=referee` to a command returns `403 referee_required`, and the server doesn't run the command.

## Read the referee view

`GET /v1/sessions/{session_id}/referee-view` returns the whole state of a game, except the session's seed (the *master seed*) and the state of its random number generators. A request with a player key returns `403 referee_required`.

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_E_QSB7sk4KaykheFC1oSxw/referee-view -H "Authorization: Bearer $KEY"
```

```json
{
  "ruleset": {...},
  "party": {"members": [...]},
  "adventure": {...},
  "mode": "encounter",
  "clock_rounds": 120,
  "monsters": [
    {"id": "monster-0001", "template": {...}, "max_hp": 1, "current_hp": 1, "conditions": [], ...},
    {"id": "monster-0002", "template": {...}, "max_hp": 3, "current_hp": 3, "conditions": [], ...}
  ],
  "npcs": [...],
  "flags": {},
  "encounter": {
    "kind": "keyed",
    "area_ref": "barrow:1:guard_room",
    "groups": [{"id": "group-0001", "label": "goblin", "label_plural": "goblins", "monster_ids": ["monster-0001", "monster-0002"], "distance_feet": 20, "fleeing": false, "fled": false, ...}],
    "stance": "indifferent",
    ...
  },
  "dungeon_state": {...},
  "quests": [...],
  "event_log": [...],
  "command_log": [...],
  ...
}
```

The referee view contains the following state that the player view omits:

- The monsters' hit points and IDs.
- The whole adventure and its map.
- The treasure in each cache.
- The session's flags and the triggers that have fired.
- The running effects and the round at which each one expires.
- The state of the battle.
- The log of every command.
- Every event, including the events that only a referee may see.

A group's `monster_ids` tell you which group each monster in the battle messages, such as `monster-0001`, belongs to.

## End an encounter

The `end_encounter` referee command ends the current encounter with an `outcome` of `parley` or `departed`. This request ends the encounter as if the party had talked its way past the goblins:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_E_QSB7sk4KaykheFC1oSxw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "end_encounter", "outcome": "parley"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [{"code": "encounter.ended", "visibility": "player", "event_type": "encounter_ended", "outcome": "parley"}],
  "messages": ["The encounter ends (parley)."],
  "mode": "exploring",
  "ended": false,
  "revision": 5
}
```

A referee command raises the session's revision as any other accepted command does, and the players' events include any `player` events it causes. The rules can still reject a referee command, in which case the response is a `200` with `accepted` set to `false`, the same as for a play command. To learn a command's fields, see [Read a command's JSON schema](reference-and-generators.md#read-a-commands-json-schema). The schema's `description` explains what the command does.

Only a referee can set a command's `source` field, which records who or what issued the command. The session's command log includes `source`, but the rules engine doesn't read it, so a command behaves the same with or without it.

## Read the referee event log

The event log also accepts `visibility=referee`, and `after` then counts the events of the visibility you request. In this request, a referee who has read 8 events asks for the remaining ones:

```sh
curl -s "http://127.0.0.1:8640/v1/sessions/ses_E_QSB7sk4KaykheFC1oSxw/events?visibility=referee&after=8" -H "Authorization: Bearer $KEY"
```

```json
{
  "events": [...],
  "messages": [
    "Surprise (party): rolled 4, surprised on 1-3 — not surprised.",
    "Encounter: 2 goblins at 20'.",
    "Reaction roll: 9+0 = 9 — indifferent.",
    "The monsters' bearing: indifferent.",
    "The encounter ends (parley)."
  ],
  "next_after": 13,
  "total": 13
}
```

## Start a session from a party document

A referee can start a session with a `party_document` in place of `character_ids`. A *party document* is the format that the rules engine writes for a whole party, and it can contain at most 20 characters. A program that uses osrlib writes one with `party_to_document`. A request with both `party_document` and `character_ids`, or with neither, returns `422 validation_error`.

## Import a character

`POST /v1/characters/import` stores a character that you created elsewhere, such as a level 5 fighter, so that a session can use it. The request body is a *character document*, which has the same four fields as an [adventure document](adventures.md#adventure-documents): `kind` set to `character`, the `schema_version` of the document format, the `engine_version` of the rules engine that wrote it, and the character as `payload`. A program that uses osrlib writes one with `Character.to_document`.

To try the endpoint, build a document from a character you created. The two versions in the document are the ones that `GET /v1/meta` returns. This command uses `jq` to copy Brenna's character from `GET /v1/characters/{character_id}`, raise it to level 5 with 30 hit points, and save the result as `hero.json`:

```sh
curl -s http://127.0.0.1:8640/v1/characters/chr_PUtflt1xPCTJIwhqMcAIrA -H "Authorization: Bearer $KEY" | jq '{kind: "character", schema_version: 4, engine_version: "2.5.0", payload: (.character | .name = "Brenna the Bold" | .level = 5 | .max_hp = 30 | .current_hp = 30)}' > hero.json
```

Import the document:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters/import -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @hero.json
```

```json
{
  "id": "chr_9YBr9e9yLj4j_wuaQ6w0Iw",
  "name": "Brenna the Bold",
  "class_id": "fighter",
  "level": 5,
  "created_at": "2026-11-21T05:24:33Z",
  "character": {...},
  "derived": {...}
}
```

The response is a stored character, the same as the one `POST /v1/characters` returns. The server accepts any character that the rules engine's model accepts, with one exception: it rejects a character that has a stat modifier or a condition with an `effect_id`, because both refer to effects in the game the character came from.

An import can fail with one of these errors:

- `422 content_invalid`: The document isn't valid.
- `409 version_unsupported`: The document has a newer schema version than the server reads.
- `403 referee_required`: The request has a player key.

## Export and import a save

A save's *document* contains the whole game, including the master seed, so only a referee can read one. Export the save from [Play a game](play-a-game.md#save-and-restore) to keep a copy or to move the game to another server:

```sh
curl -s http://127.0.0.1:8640/v1/saves/sav_E6NKZNiBl4c2jL_rkyY8Bg/document -H "Authorization: Bearer $KEY" -o save.json
```

Print the first 300 bytes of the file to see the start of the document:

```sh
head -c 300 save.json
```

```text
{"kind":"save","schema_version":4,"engine_version":"2.5.0","payload":{"master_seed":3860435029322934,"ruleset":{"hp_reroll_at_first_level":false,"encumbrance":"basic","variable_weapon_damage":true,"individual_initiative":false,"thac0_arithmetic":false,"weapon_reload":false,"hd5_counts_as_magical":fa
```

Import the document on this server or another one:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/import -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @save.json
```

```json
{
  "id": "sav_mAJU7Pqpm2ScDYKejDK-Rw",
  "session_id": null,
  "adventure_id": null,
  "adventure_name": "The Barrow of the Forgotten King",
  "label": null,
  "mode": "battle",
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  "created_at": "2026-11-21T05:24:33Z"
}
```

An imported save has no `session_id`, `adventure_id`, or label. Before the server stores anything, it loads the document and checks the document's adventure. A document that the rules engine can't load returns `422 content_invalid`, as does a document whose adventure fails the checks that an [upload](adventures.md#validate-a-document) runs. A document with a newer schema version returns `409 version_unsupported`.

Restore the imported save the same way as any other save:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/sav_mAJU7Pqpm2ScDYKejDK-Rw/restore -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "ses_XcI1kemu07lLvUu_-aBbHw",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": null,
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "battle",
  "ended": false,
  "revision": 0,
  ...
}
```

The restored session's `adventure_id` is `null`, and the session plays the copy of the adventure in the save.

A request with a player key for a save's document returns `403 referee_required`. To see the error, switch to the player key:

```sh
export KEY=player-key-0123456789
```

```sh
curl -si http://127.0.0.1:8640/v1/saves/sav_E6NKZNiBl4c2jL_rkyY8Bg/document -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required","request_id":"req_jKx8npCJb0HmgXFm7im4_Q"}
```

## Read the full adventure document

A referee can read an adventure's whole document, including the map, monsters, treasure, triggers, and quests, with `GET /v1/adventures/{adventure_id}/document`. For details, see [Adventures](adventures.md#read-an-adventures-info).

## Next steps

- [Authentication](authentication.md): roles, keys, and whose data a key reaches.
- [Adventures](adventures.md): the full adventure document.
- [Errors](errors.md): the codes a referee request can receive, such as `referee_command`.
