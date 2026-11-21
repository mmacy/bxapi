# Run games as a referee

Use a referee key to see what's hidden from the players, send commands the players can't, and move characters and saves in and out of the server.

In a tabletop game, the *referee* runs the game: they know the map, the monsters, and the dice. A bxapi key with the `referee` role gives your software the same view. Give it only to software that runs on a server you trust, like your game's backend or a tool that runs the game for you. A referee key in a web page or a mobile app gives every player the hidden state of every game. [Authentication](authentication.md) describes how to create keys and what each role may do.

Start the server with a referee key, as [Authentication](authentication.md#configure-api-keys) shows, and put the key in the shell variable `KEY`:

```sh
export KEY=referee-key-0123456789
```

A referee can do everything a player can, so the walkthrough in [Play a game](play-a-game.md) works with this key too. The examples here use the characters from that page.

## Start a repeatable session

A session's *seed* decides every die the session rolls. The server picks one for each session, and it never returns it, so a player can't predict a roll. A referee can pass a `seed`, an integer from 0 to 9,007,199,254,740,991, when it starts a session. Start the session that the rest of this page uses with seed 7:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_XSWUBj-UjQauxKNeDtopIg", "chr_yqskEJZy5UoVvqCBN3Hbkw", "chr_5EZYSIgQNlMz7w7p1GwFlA"], "seed": 7}'
```

```json
{
  "id": "ses_0VOivRW_xUk35SOGrfDwDA",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "town",
  "ended": false,
  "revision": 0,
  ...
}
```

Two sessions with the same seed, the same adventure, the same party, and the same commands in the same order roll the same dice. Use a seed to test your game, to reproduce a bug a player reports, or to run the same scenario twice. A session without a seed gets a random one, so the same commands give different rolls.

A referee can pass a `seed` to `POST /v1/ability-rolls` and `POST /v1/characters` too. A player who passes a `seed` anywhere gets 403 `referee_required`.

## Send referee commands

A referee can send *referee commands*, which change a game directly. `GET /v1/sessions/{session_id}/commands` shows a referee both kinds. A command's `kind` says which it is: a `referee` command is legal in most modes, and a player who sends one gets 403 `referee_command`. The list starts with `grant_item`, `grant_coins`, `award_xp`, `set_flag`, `spawn_monsters`, and `spawn_npc_party`, and includes `place_party`, `advance_time`, `complete_quest`, and `end_encounter`.

Put a rope in a character's pack in the session you just started. A command names a party member by the id from the player view:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_0VOivRW_xUk35SOGrfDwDA/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "grant_item", "character_id": "character-0001", "item_id": "rope"}'
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

Some events are for the referee only: the surprise and reaction rolls, and the treasure the adventure generates in a room. Every event has a `visibility`, `player` or `referee`. A player's responses leave out the `referee` events. A referee sees them by adding `visibility=referee` to the request, either to a command or to the event log.

Enter the dungeon and move east once, as in [Play a game](play-a-game.md#enter-the-dungeon). Then move east again into the guard room of the built-in adventure, this time with `visibility=referee`:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_0VOivRW_xUk35SOGrfDwDA/commands?visibility=referee" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
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
    "Encounter: 2 × Goblin at 20'.",
    "Reaction roll: 9+0 = 9 — indifferent.",
    "The monsters' bearing: indifferent."
  ],
  "mode": "encounter",
  "ended": false,
  "revision": 4
}
```

`messages` has one line for each event in `events`, referee events included. A player who adds `visibility=referee` to a command gets 403 `referee_required`, and the server doesn't run the command.

## Read the referee view

`GET /v1/sessions/{session_id}/referee-view` returns the whole state of a game, except the master seed and the dice streams. A player who asks gets 403 `referee_required`.

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_0VOivRW_xUk35SOGrfDwDA/referee-view -H "Authorization: Bearer $KEY"
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
    "groups": [{"id": "group-0001", "label": "Goblin", "monster_ids": ["monster-0001", "monster-0002"], "distance_feet": 20, ...}],
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

Compared with the player view, it has the monsters' hit points and ids, the whole adventure and its map, the treasure in each cache, the state of the dice-driven parts of the game, and every event, including the ones only a referee may see. The `monster_ids` of a group tell you which monsters in the messages belong to it.

## End an encounter

End the encounter after the party talks its way past the goblins. `outcome` is `parley` or `departed`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_0VOivRW_xUk35SOGrfDwDA/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "end_encounter", "outcome": "parley"}'
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

A referee command raises the session's revision like any other accepted command, and the players' events include any `player` events it causes. The rules can still refuse a referee command, and then the response is a 200 with `accepted` set to `false`, as for a player command. To read a command's fields, see [Read a command's JSON schema](reference-and-generators.md#read-a-commands-json-schema). The schema's `description` says what the command does.

A referee, and no player, can also set a command's `source` field. It's a note of who issued the command, which the game logs and never reads.

## Read the event log

The event log takes the same parameter, and `after` counts the events of the visibility you ask for. A referee who has read 8 events asks for the rest:

```sh
curl -s "http://127.0.0.1:8640/v1/sessions/ses_0VOivRW_xUk35SOGrfDwDA/events?visibility=referee&after=8" -H "Authorization: Bearer $KEY"
```

```json
{
  "events": [...],
  "messages": [
    "Surprise (party): rolled 4, surprised on 1-3 — not surprised.",
    "Encounter: 2 × Goblin at 20'.",
    "Reaction roll: 9+0 = 9 — indifferent.",
    "The monsters' bearing: indifferent.",
    "The encounter ends (parley)."
  ],
  "next_after": 13,
  "total": 13
}
```

## Start a session from a party document

A referee can start a session with a `party_document` in place of `character_ids`. A party document is the format the rules engine writes for a whole party, and it can hold at most 20 characters. A request with both, or with neither, gets 422 `validation_error`.

## Import a character

`POST /v1/characters/import` stores a character that you made somewhere else, like a level 5 fighter, as a stored character that a session can use. The body is a *character document*: a character in the same envelope that adventures use, with `kind` set to `character`, the `schema_version` and `engine_version` of the rules engine that wrote it, and the character as `payload`. A program that uses osrlib writes one with `Character.to_document`.

To try the endpoint, build a document from a character you created. This command uses `jq` to take Brenna's character from `GET /v1/characters/{character_id}`, raise it to level 5 with 30 hit points, and save the result as `hero.json`:

```sh
curl -s http://127.0.0.1:8640/v1/characters/chr_XSWUBj-UjQauxKNeDtopIg -H "Authorization: Bearer $KEY" | jq '{kind: "character", schema_version: 4, engine_version: "2.4.0", payload: (.character | .name = "Brenna the Bold" | .level = 5 | .max_hp = 30 | .current_hp = 30)}' > hero.json
```

The two versions are the ones that `GET /v1/meta` returns. Import the document:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters/import -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @hero.json
```

```json
{
  "id": "chr_7sB21cwDr97zAJ5dFpiQ4w",
  "name": "Brenna the Bold",
  "class_id": "fighter",
  "level": 5,
  "created_at": "2026-11-21T00:26:59Z",
  "character": {...},
  "derived": {...}
}
```

The response is a stored character, like the one `POST /v1/characters` returns. The server accepts any character the rules engine's model accepts, but not one that carries a stat modifier or a condition with an `effect_id`, because those refer to effects in the game the character came from. A document that isn't valid gets 422 `content_invalid`, and a document with a newer schema version gets 409 `version_unsupported`. A player who imports a character gets 403 `referee_required`.

## Export and import a save

A save's *document* contains the whole game, including the master seed. Only a referee can read one. Export the save from [Play a game](play-a-game.md#save-and-restore), to keep a copy or to move it to another server:

```sh
curl -s http://127.0.0.1:8640/v1/saves/sav_aJfDsAPQJJMycBUJySEXRA/document -H "Authorization: Bearer $KEY" -o save.json
```

```sh
head -c 300 save.json
```

```text
{"kind":"save","schema_version":4,"engine_version":"2.4.0","payload":{"master_seed":8490118134873150,"ruleset":{"hp_reroll_at_first_level":false,"encumbrance":"basic","variable_weapon_damage":true,"individual_initiative":false,"thac0_arithmetic":false,"weapon_reload":false,"hd5_counts_as_magical":fa
```

Import it, on this server or another one:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/import -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @save.json
```

```json
{
  "id": "sav_6A6GS6DlA8U4T31M7YAQoA",
  "session_id": null,
  "adventure_id": null,
  "adventure_name": "The Barrow of the Forgotten King",
  "label": null,
  "mode": "battle",
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  "created_at": "2026-11-21T00:26:59Z"
}
```

An imported save has no `session_id`, `adventure_id`, or label. The server loads the document before it stores anything, so a document that the rules engine can't load gets 422 `content_invalid`, and one with a newer schema version gets 409 `version_unsupported`. Restore the imported save the way you restore any save:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/sav_6A6GS6DlA8U4T31M7YAQoA/restore -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "ses_hrDLkmHRyi3IRaTNymkylg",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": null,
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "battle",
  "ended": false,
  "revision": 0,
  ...
}
```

The restored session's `adventure_id` is `null`, and the session plays the copy of the adventure in the save. A player who asks for a save's document gets 403 `referee_required`. Switch to the player key to see it:

```sh
export KEY=player-key-0123456789
```

```sh
curl -si http://127.0.0.1:8640/v1/saves/sav_aJfDsAPQJJMycBUJySEXRA/document -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required"}
```

## Read the full adventure document

A referee can read an adventure's whole document, including the map, monsters, treasure, triggers, and quests. See [Adventures](adventures.md).

## Next steps

- [Authentication](authentication.md): roles, keys, and whose data a key reaches.
- [Adventures](adventures.md): the full adventure document.
- [Errors](errors.md): the codes a referee request can get, like `referee_command`.
