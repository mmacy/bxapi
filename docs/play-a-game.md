# Play a game

Play one game of the built-in adventure with `curl`, from an ability roll to a saved battle. A game client sends the same requests.

## Start a server with a player key

Start the server with one API key. The key's role is `player`, which is the role a game client uses. A player sees only what the players of a game may see.

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' uv run bxapi
```

In a second terminal, put the key in a shell variable. Every command below sends it as a bearer token:

```sh
export KEY=player-key-0123456789
```

The server returns compact JSON. The responses below are formatted and shortened with `...`. The server creates an id for each character, session, and save, so your ids differ from the ones shown. Replace each id in a command with the one your server returned. The dice are random, so the scores, hit points, and battle results differ for you too. The text says where that matters.

To check the key, ask the server who you are:

```sh
curl -s http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $KEY"
```

```json
{
  "api_version": "0.1.0",
  "engine_version": "2.4.0",
  "schema_version": 4,
  "auth_enabled": true,
  "client": "my-game",
  "role": "player"
}
```

## Roll ability scores

In B/X, a player rolls six ability scores first and then picks a class the scores qualify for. An *ability roll* is that first step. The server rolls and stores the scores:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/ability-rolls -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "rol__zoeisiFGZ4tcVU2ozKMgA",
  "scores": {"str": 7, "int": 9, "wis": 12, "dex": 12, "con": 12, "cha": 9},
  "eligible_class_ids": ["cleric", "dwarf", "elf", "fighter", "halfling", "magic_user", "thief"],
  "used": false,
  "created_at": "2026-11-21T00:26:37Z"
}
```

- `id` is the roll's id. You pass it as `ability_roll_id` when you create a character.
- `scores` has one score from 3 to 18 for each ability: strength, intelligence, wisdom, dexterity, constitution, and charisma.
- `eligible_class_ids` lists the classes the scores qualify for. Show it to the player so they can choose.
- `used` becomes `true` when a character is created from the roll. You can create one character from a roll.

The server doesn't limit how many ability rolls you ask for. If your game wants a limit, your game enforces it.

## Create a character

Create a fighter from the roll. A character's *purchases* are the starting gear it buys with its starting gold, and `equip_item_ids` says which purchased items it wears or wields. First, try to buy ten lots of plate mail, which costs more than the character has:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol__zoeisiFGZ4tcVU2ozKMgA", "purchases": [{"item_id": "plate_mail", "quantity": 10}]}'
```

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The starting gold doesn't cover the purchases.",
  "code": "character_invalid",
  "rejections": [
    {"code": "items.purchase.insufficient_funds", "params": {"item": "plate_mail", "cost_gp": 600}}
  ]
}
```

The server refuses a choice that breaks a rule with status 422 and the code `character_invalid`. The `rejections` list comes from the rules engine: each has a `code` and `params` that your game can turn into a message for the player. The roll stays unused, so you can try again with the same `ability_roll_id`. Buy chainmail, a shield, and a sword, and equip all three:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol__zoeisiFGZ4tcVU2ozKMgA", "purchases": [{"item_id": "chainmail"}, {"item_id": "shield"}, {"item_id": "sword"}], "equip_item_ids": ["chainmail", "shield", "sword"]}'
```

```json
{
  "id": "chr_XSWUBj-UjQauxKNeDtopIg",
  "name": "Brenna",
  "class_id": "fighter",
  "level": 1,
  "created_at": "2026-11-21T00:26:38Z",
  "character": {"name": "Brenna", "scores": {...}, "max_hp": 3, "current_hp": 3, "inventory": {...}, ...},
  "derived": {"thac0": 19, "armour_class": 2, "armour_class_ascending": 17, "saves": {...}, ...}
}
```

- `id` is the character's id. A session uses it to put the character in a party.
- `character` is the character as the rules engine stores it: scores, hit points, inventory, and spells.
- `derived` has the numbers the engine computes from the character, like the attack target number `thac0` and the armour class.

If the starting gold doesn't cover the chainmail, the shield, and the sword, the server refuses with the same `insufficient_funds` rejection. Buy less and try again.

Without an `ability_roll_id`, the server rolls the scores as part of the request. Create two more characters that way:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "purchases": [{"item_id": "mace"}, {"item_id": "leather"}], "equip_item_ids": ["mace", "leather"]}'
```

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Wren", "class_id": "thief", "alignment": "neutral", "purchases": [{"item_id": "dagger"}, {"item_id": "leather"}], "equip_item_ids": ["dagger", "leather"]}'
```

Each response has the same fields as Brenna's. The ids in the rest of this page are `chr_XSWUBj-UjQauxKNeDtopIg` for Brenna, `chr_yqskEJZy5UoVvqCBN3Hbkw` for Tomas, and `chr_5EZYSIgQNlMz7w7p1GwFlA` for Wren.

## Start a session

A *session* is one game in progress. Start one with the party's character ids and the id of an adventure. The order of `character_ids` is the party's *marching order*: the first character walks in front, and the characters at the front are the ones who can fight in melee. A session plays copies of the characters, so the stored characters don't change.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_XSWUBj-UjQauxKNeDtopIg", "chr_yqskEJZy5UoVvqCBN3Hbkw", "chr_5EZYSIgQNlMz7w7p1GwFlA"]}'
```

```json
{
  "id": "ses_eqeMc3BaPn-ii_2kKiCJWw",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "town",
  "ended": false,
  "revision": 0,
  "clock": {"rounds": 0, "turns": 0, "days": 0},
  "schema_version": 4,
  "engine_version": "2.4.0",
  "created_at": "2026-11-21T00:26:38Z",
  "updated_at": "2026-11-21T00:26:38Z"
}
```

- `mode` is what the party is doing. A session starts in `town`. The other modes are `exploring`, `encounter`, `battle`, `game_over`, and `victory`. The mode decides which commands are legal.
- `ended` is `true` in the modes `game_over` and `victory`.
- `revision` counts the commands the session has accepted. It starts at 0.
- `clock` is the game time that has passed, in rounds of 10 seconds, turns of 10 minutes, and days.

A session is limited to 20 characters.

## Read the legal commands

You play by sending *commands*. The server tells you which commands are legal in the session's current mode:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY"
```

```json
{
  "mode": "town",
  "revision": 0,
  "commands": [
    {"command_type": "reorder_party", "kind": "play"},
    {"command_type": "give_items", "kind": "play"},
    ...
    {"command_type": "enter_dungeon", "kind": "play"},
    {"command_type": "purchase_equipment", "kind": "play"},
    {"command_type": "sell_treasure", "kind": "play"},
    {"command_type": "purchase_healing", "kind": "play"}
  ]
}
```

A player sees only commands with `kind` set to `play`. A command that's listed can still be refused, for a reason the rules check when you send it. To learn which fields a command takes, see [Rules reference and generators](reference-and-generators.md#read-a-commands-json-schema).

## Enter the dungeon

Send a command as a JSON body with a `command_type` and the command's own fields. `enter_dungeon` takes the id of a dungeon. The built-in adventure has one, `barrow`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "enter_dungeon", "dungeon_id": "barrow"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [
    {"code": "exploration.location.entered", "visibility": "player", "event_type": "location_entered", "location_kind": "dungeon", "location_id": "barrow", "level_number": 1, ...},
    {"code": "session.quest.activated", "visibility": "player", "event_type": "quest_activated", "quest_id": "the-idol", "name": "The Jade Idol", ...}
  ],
  "messages": [
    "The party enters dungeon barrow (level 1).",
    "A new quest: The Jade Idol. The temple wants the Jade Idol off the barrow king's altar and back on its own."
  ],
  "mode": "exploring",
  "ended": false,
  "revision": 1
}
```

This is the *command outcome*, the response to every command:

- `accepted` is `true` when the rules accepted the command.
- `rejections` says why the rules refused it. It's empty here.
- `events` lists what happened, as data your game can act on. Each event has a `code`, an `event_type`, and fields that depend on the type.
- `messages` has one line of English for each event, in the same order. Show them to the player.
- `mode` and `ended` are the session's mode and whether it's over after the command.
- `revision` is the session's revision after the command. It went from 0 to 1.

Messages and events identify characters and monsters by id, like `character-0001` and `monster-0001`. The server assigns each character in the party an id of the form `character-NNNN`, in marching order. The player view, described below, lists each party member's id and name.

## Move, and see a refused command

The party faces north when it enters. Try to move north:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "north"}'
```

```text
HTTP/1.1 200 OK
...
content-type: application/json

{"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
```

A wall blocks the way, so the rules refuse the command. The status is 200, because the request was valid and the server did what you asked: it asked the rules. A refused command has `accepted` set to `false`, a `rejections` list, and no events, and it changes nothing. The revision is still 1. An error status like 422 means your request itself was wrong. [Errors](errors.md) covers the difference.

Move east instead:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [{"code": "exploration.party.moved", "visibility": "player", "event_type": "party_moved", "x": 1, "y": 0, "facing": "east", "narrative": null}],
  "messages": ["The party moves to (1, 0), facing east."],
  "mode": "exploring",
  "ended": false,
  "revision": 2
}
```

## Walk into the first encounter

Move east again. The next cell is the guard room, and the adventure keeps goblins there:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [...],
  "messages": [
    "The party moves to (2, 0), facing east.",
    "The party enters area guard_room (level 1).",
    "Encounter: 2 × Goblin at 20' — the party is surprised — the monsters are surprised.",
    "The monsters' bearing: uncertain."
  ],
  "mode": "encounter",
  "ended": false,
  "revision": 3
}
```

The session is now in the `encounter` mode. Whether anyone is surprised and what the goblins' bearing is depend on dice, so yours can differ. Ask for the legal commands again:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY"
```

```json
{
  "mode": "encounter",
  "revision": 3,
  "commands": [
    {"command_type": "drop_items", "kind": "play"},
    {"command_type": "use_item", "kind": "play"},
    {"command_type": "parley", "kind": "play"},
    {"command_type": "evade", "kind": "play"},
    {"command_type": "engage_battle", "kind": "play"},
    {"command_type": "wait", "kind": "play"},
    {"command_type": "turn_undead", "kind": "play"}
  ]
}
```

The party can talk (`parley`), run (`evade`), or fight (`engage_battle`). Before you choose, read the player view.

## Read the player view

The *player view* is everything the players may know about the game now. Read it after each command to draw your screen:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/view -H "Authorization: Bearer $KEY"
```

```json
{
  "adventure_name": "The Barrow of the Forgotten King",
  "party": [
    {"id": "character-0001", "name": "Brenna", "class_id": "fighter", "level": 1, "current_hp": 3, "max_hp": 3, "conditions": [], "inventory": {...}, "encounter_rate_feet": 20, ...},
    {"id": "character-0002", "name": "Tomas", "class_id": "cleric", "level": 1, "current_hp": 3, "max_hp": 3, ...},
    {"id": "character-0003", "name": "Wren", "class_id": "thief", "level": 1, "current_hp": 1, "max_hp": 1, ...}
  ],
  "location": {"kind": "dungeon", "dungeon_id": "barrow", "level_number": 1, "position": [2, 0], "facing": "east"},
  "mode": "encounter",
  "explored": [{"dungeon_id": "barrow", "level_number": 1, "cells": [[0, 0], [1, 0], [2, 0]], "edges": {...}}],
  "journal": [...],
  "quests": [{"id": "the-idol", "name": "The Jade Idol", ...}],
  "encounter": {
    "groups": [{"id": "group-0001", "label": "Goblin", "count": 2, "distance_feet": 20, "fled": false, "member_distance_feet": {...}, "front_rank": [...], ...}],
    "stance": "uncertain",
    "in_battle": false,
    "declarers": ["character-0001", "character-0002", "character-0003"],
    "front_rank": ["character-0001", "character-0002"],
    ...
  },
  ...
}
```

The session is in the `encounter` mode and no battle has started, so `in_battle` is `false` and `member_distance_feet` is empty. The fields that matter for the battle:

- `party` lists each member's `id`, `name`, hit points, `conditions` (like `dead`), and inventory. Use it to turn the ids in messages into names. The `character-0001` in a message is Brenna.
- `location` and `explored` give the party's position and the cells it has seen. The view never has the parts of the map the party hasn't explored.
- `encounter.groups` lists the groups of monsters. A group has an `id` to attack, a `label` that says what it is, a `count`, and its `distance_feet` from the party. The view doesn't list the individual monsters. A message about `monster-0001` doesn't say which group it belongs to, so use the group's label and count.
- `encounter.declarers` lists the members who must choose an action this round.
- `encounter.front_rank` lists the members who can fight in melee. The others can't attack monsters in melee.

The view never has monster hit points or the referee's dice.

## Start the battle

Fight:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [{"code": "battle.started", "visibility": "player", "event_type": "battle_started"}],
  "messages": ["Battle is joined!"],
  "mode": "battle",
  "ended": false,
  "revision": 4
}
```

## Fight one battle round

A battle runs in rounds. In each round, every member in `declarers` chooses an action, and the rules resolve all of them. Send `resolve_battle_round` with one declaration for each declarer. A declaration names a `character_id` and an `action`: `attack`, `cast`, `turn_undead`, `move`, `use_item`, or `hold`. An attack also names the `target_group_id`. The goblins are 20 feet away, so an attacker adds `"move": "close"` to walk up to them and attack in the same round.

First, send what a client might try: all three characters attack.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "attack", "target_group_id": "group-0001", "move": "close"}]}'
```

```json
{
  "accepted": false,
  "rejections": [
    {"code": "battle.declaration.not_in_front_rank", "params": {"character": "character-0003"}}
  ],
  "events": [],
  "messages": [],
  "mode": "battle",
  "ended": false,
  "revision": 4
}
```

Wren isn't in the `front_rank`, so the rules refuse the whole round, and nothing changed. Have Wren `hold` instead:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [
    {"code": "battle.round.started", "visibility": "player", "event_type": "battle_round", "round": 1},
    {"code": "combat.initiative.rolled", ...},
    {"code": "battle.group.moved", "visibility": "player", "event_type": "group_moved", "group_id": "group-0001", "distance_feet": 5},
    ...
  ],
  "messages": [
    "Battle round 1 begins.",
    "Initiative (side): party 1, monsters 4. Order: group-0001, character-0001, character-0002, character-0003.",
    "group-0001 is now 5' away.",
    "monster-0001 hits character-0001 with weapon: rolled 16+0 = 16, needing 15.",
    "character-0001 takes 2 damage from monster-0001.",
    "monster-0002 hits character-0002 with weapon: rolled 19+0 = 19, needing 12.",
    "character-0002 takes 2 damage from monster-0002.",
    "character-0001 misses monster-0001 with Sword: rolled 5-1 = 4, needing 13.",
    "character-0002 hits monster-0001 with Mace: rolled 18+0 = 18, needing 13.",
    "monster-0001 takes 3 damage from character-0002."
  ],
  "mode": "battle",
  "ended": false,
  "revision": 5
}
```

The dice decide every roll in the messages, so your round differs. The revision went from 4 to 5, and the battle goes on. Read the view again to see the party's hit points. In this game Brenna and Tomas each have 1 of 3 hit points left. A character at 0 hit points has the condition `dead`, and the next round's `declarers` leaves that character out.

## Catch up on events

The server keeps a log of every event the players may see. Read it a page at a time. `after` is the number of events you've already read, and `limit` is the page size. Read the first three:

```sh
curl -s "http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/events?limit=3" -H "Authorization: Bearer $KEY"
```

```json
{
  "events": [...],
  "messages": [
    "The party enters dungeon barrow (level 1).",
    "A new quest: The Jade Idol. The temple wants the Jade Idol off the barrow king's altar and back on its own.",
    "The party moves to (1, 0), facing east."
  ],
  "next_after": 3,
  "total": 18
}
```

`next_after` is the `after` to send next, and `total` is how many events the log has. A client that loses its connection stores the last `next_after` it received. When it reconnects, it sends that number as `after` and gets only the events it missed. Here, a client that had read 15 events asks for the rest:

```sh
curl -s "http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/events?after=15" -H "Authorization: Bearer $KEY"
```

```json
{
  "events": [...],
  "messages": [
    "character-0001 misses monster-0001 with Sword: rolled 5-1 = 4, needing 13.",
    "character-0002 hits monster-0001 with Mace: rolled 18+0 = 18, needing 13.",
    "monster-0001 takes 3 damage from character-0002."
  ],
  "next_after": 18,
  "total": 18
}
```

The `limit` can be up to 500. A page holds at most `limit` events, so keep asking while `next_after` is less than `total`.

## Save and restore

A *save* is a copy of a session at one moment. Save the session before the second round, with a label:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_eqeMc3BaPn-ii_2kKiCJWw/saves -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"label": "Before round 2"}'
```

```json
{
  "id": "sav_aJfDsAPQJJMycBUJySEXRA",
  "session_id": "ses_eqeMc3BaPn-ii_2kKiCJWw",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "label": "Before round 2",
  "mode": "battle",
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  "created_at": "2026-11-21T00:26:38Z"
}
```

Saving doesn't change the session or its revision. The body is optional, and a save without a label has `"label": null`. A player can't read the save's contents. The save is for restoring.

Restore the save to start a new session from that moment. The original session and the save stay as they are, so you can restore the same save as many times as you like. The body is optional:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/sav_aJfDsAPQJJMycBUJySEXRA/restore -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Second try"}'
```

```json
{
  "id": "ses_dJ_n8qHMPAHJ5zMNuRIhlQ",
  "name": "Second try",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "battle",
  "ended": false,
  "revision": 0,
  "clock": {"rounds": 122, "turns": 2, "days": 0},
  ...
}
```

The new session is in the same mode, with the same party, and its revision starts again at 0. Its `adventure_id` is the save's. The id is `null` for a save that a referee imported, and it can be the id of an adventure that you deleted since. Neither changes play, because a session plays its own copy of the adventure.

## Retry a command safely

A request can fail after the server applied the command, for example when the connection drops before the response arrives. If your client sends the command again, the rules run it a second time. To prevent that, send the revision you last read as the `expected_revision` query parameter. The server runs the command only if the session is still at that revision.

Send the second round to the restored session, which is at revision 0:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_dJ_n8qHMPAHJ5zMNuRIhlQ/commands?expected_revision=0" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [...],
  "messages": [
    "Battle round 2 begins.",
    ...
    "monster-0001 is dead.",
    "monster-0001 is killed."
  ],
  "mode": "battle",
  "ended": false,
  "revision": 1
}
```

Now suppose the response was lost and your client sends the same request again:

```sh
curl -si -X POST "http://127.0.0.1:8640/v1/sessions/ses_dJ_n8qHMPAHJ5zMNuRIhlQ/commands?expected_revision=0" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```text
HTTP/1.1 409 Conflict
...
content-type: application/problem+json

{"type":"about:blank","title":"Conflict","status":409,"detail":"The session is at revision 1, not 0. Read the view and decide whether to send the command again.","code":"revision_conflict","revision":1}
```

The session is at revision 1, so the server ran nothing. A `revision_conflict` after a retry tells your client that the first request was applied. Read the view to see the result. [Errors](errors.md#revision_conflict) covers what to do in other cases.

Every response to a command has the new `revision`, and `GET /v1/sessions/{session_id}/commands` has the current one, so your client always knows what to send.

## Next steps

- [Errors](errors.md): every error code and what to do about it.
- [Rules reference and generators](reference-and-generators.md): look up the classes, spells, and equipment your players choose from, and read the schema of each command.
- [Authentication](authentication.md): run a server with API keys for your game.
- [Example client](../examples/README.md): a Python script that plays this battle without a hard-coded script.
- [Run games as a referee](referee.md): see hidden state, send referee commands, and choose seeds.
