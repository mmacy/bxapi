# Play a game

Play one game of the adventure that comes with the server, with `curl`, from rolling a character's ability scores to saving the game during a battle. A game client that you build sends the same requests. If you haven't set up bxapi yet, follow [Get it running](../README.md#get-it-running) in the README first.

## Start a server with a player key

Start the server with one API key. The `client` is a name you choose for your application. The key's role is `player`, which is the role a game client uses. The server shows a player only what the players of a game may see.

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' uv run bxapi
```

In a second terminal, put the key in a shell variable. Every command below sends it as a bearer token:

```sh
export KEY=player-key-0123456789
```

The server returns compact JSON. We've formatted the responses below and shortened them with `...`. The server creates an id for each character, session, and save, so your ids differ from the ones shown. Replace each id in a command with the one your server returned. The dice are random, so the characters' scores, the *hit points* (the damage a character can take), and the battle results differ for you too. The text notes where your results can differ.

To check the key, ask the server who you are:

```sh
curl -s http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $KEY"
```

```json
{
  "api_version": "0.1.0",
  "engine_version": "2.5.0",
  "schema_version": 4,
  "auth_enabled": true,
  "client": "my-game",
  "role": "player"
}
```

## Roll ability scores

In B/X, a player first rolls six *ability scores*, the numbers like strength and dexterity that the rules use for a character. Then the player picks a *class*, like fighter or thief, that the scores qualify for. An *ability roll* is that first step. The server rolls the scores and stores them:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/ability-rolls -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "rol_yQsuUWgqmzpnDA6Sm51kQA",
  "scores": {"str": 6, "int": 12, "wis": 17, "dex": 11, "con": 5, "cha": 11},
  "eligible_class_ids": ["cleric", "elf", "fighter", "magic_user", "thief"],
  "used": false,
  "created_at": "2026-11-21T05:24:33Z"
}
```

- `id` is the roll's id. You pass it as `ability_roll_id` when you create a character.
- `scores` has one score from 3 to 18 for each ability: strength, intelligence, wisdom, dexterity, constitution, and charisma.
- `eligible_class_ids` lists the classes the scores qualify for. Show it to the player so they can choose.
- `used` becomes `true` when a character is created from the roll. You can create one character from a roll.

The server doesn't limit how many ability rolls you ask for. If your game wants a limit, your game enforces it.

## Create a character

Create a fighter from the roll. A character's *purchases* are the starting gear it buys with its starting gold, and `equip_item_ids` lists the purchased items it wears or wields. First, try to buy ten lots of plate mail, which costs more than the character has:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol_yQsuUWgqmzpnDA6Sm51kQA", "purchases": [{"item_id": "plate_mail", "quantity": 10}]}'
```

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The starting gold doesn't cover the purchases.",
  "code": "character_invalid",
  "request_id": "req_gyzKc4EYvuWllhUQkYLvWQ",
  "rejections": [
    {"code": "items.purchase.insufficient_funds", "params": {"item": "plate_mail", "cost_gp": 600}}
  ]
}
```

The server refuses a choice that breaks a rule with status 422 and the code `character_invalid`. Character creation is the one place where a broken rule is an error, because the server can't create the character. The `rejections` list comes from the *rules engine*, osrlib, which applies the B/X rules for the server. Each rejection has a `code` and `params` that your game can turn into a message for the player. The roll stays unused, so you can try again with the same `ability_roll_id`. Buy chainmail, a shield, and a sword, and equip all three:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol_yQsuUWgqmzpnDA6Sm51kQA", "purchases": [{"item_id": "chainmail"}, {"item_id": "shield"}, {"item_id": "sword"}], "equip_item_ids": ["chainmail", "shield", "sword"]}'
```

```json
{
  "id": "chr_PUtflt1xPCTJIwhqMcAIrA",
  "name": "Brenna",
  "class_id": "fighter",
  "level": 1,
  "created_at": "2026-11-21T05:24:33Z",
  "character": {"name": "Brenna", "scores": {...}, "max_hp": 3, "current_hp": 3, "inventory": {...}, ...},
  "derived": {"thac0": 19, "attack_bonus": 0, "armour_class": 4, "armour_class_ascending": 15, "saves": {...}, ...}
}
```

- `id` is the character's id. A session uses it to put the character in a party.
- `character` is the character as the rules engine stores it: scores, hit points, inventory, and spells.
- `derived` has the numbers the rules engine computes from the character, like `thac0` and the armour class. `thac0` ("to hit armour class 0") is the number the character must roll on a 20-sided die to hit armour class 0, and the *armour class* is how hard the character is to hit, where lower is better. `saves` has the character's *saving throws*, the 20-sided die rolls that resist dangers like poison and dragon breath (not saved games).

If the starting gold doesn't cover the chainmail, the shield, and the sword, the server refuses with the same `insufficient_funds` rejection. Buy less and try again.

Without an `ability_roll_id`, the server rolls the scores as part of the request. Create two more characters that way:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "purchases": [{"item_id": "mace"}, {"item_id": "leather"}], "equip_item_ids": ["mace", "leather"]}'
```

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Wren", "class_id": "thief", "alignment": "neutral", "purchases": [{"item_id": "dagger"}, {"item_id": "leather"}], "equip_item_ids": ["dagger", "leather"]}'
```

Each response has the same fields as Brenna's. The ids in the rest of this page are `chr_PUtflt1xPCTJIwhqMcAIrA` for Brenna, `chr_kqRWmM-1d1lMQFy-4CFVnw` for Tomas, and `chr_8l3D6o0BvEL_V99x7NUK1w` for Wren.

### Choose gear after you see the gold

The server rolls a character's starting gold in the same request that creates the character. A player who sends `purchases` therefore chooses gear before they know how much gold they have. To show the gold first, create the character with no `purchases`. The response contains the gold in `character.inventory.purse`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Garrick", "class_id": "fighter", "alignment": "lawful"}'
```

```json
{
  "id": "chr_44TkoTKtpBs4IniRSW03Mg",
  "name": "Garrick",
  "class_id": "fighter",
  "level": 1,
  "created_at": "2026-11-21T05:24:33Z",
  "character": {"name": "Garrick", "scores": {...}, "max_hp": 7, "current_hp": 7, "inventory": {"items": [], "purse": {"pp": 0, "gp": 120, "ep": 0, "sp": 0, "cp": 0}, ...}, ...},
  "derived": {...}
}
```

Garrick has 120 gold pieces (`gp`). Show the gold to the player, then buy the gear in a session while the session is in the `town` mode, which is the mode every session starts in. The next sections describe sessions and commands in detail. Start a session with Garrick as the only member of the party:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_44TkoTKtpBs4IniRSW03Mg"]}'
```

The response has `"id": "ses_onfnKBVHb_Bd7jQn5t4aYg"` and `"mode": "town"`. The `purchase_equipment` command buys equipment for one party member, and the session gives the first member of the party the id `character-0001`. Each entry in `item_ids` is one purchase of an item from the shop:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_onfnKBVHb_Bd7jQn5t4aYg/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "purchase_equipment", "character_id": "character-0001", "item_ids": ["chainmail", "shield", "sword"]}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [{"code": "exploration.item.acquired", "visibility": "player", "event_type": "item_acquired", "character_id": "character-0001", "item_ids": ["chainmail", "shield", "sword"], "coins_gp_value": 0, "origin": "purchase"}],
  "messages": ["character-0001 acquires chainmail, shield, sword."],
  "mode": "town",
  "ended": false,
  "revision": 1
}
```

The purchased items are in Garrick's pack. The `equip_item` command makes a member wear or wield one item, so send it once for each item. Here it is for the chainmail:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_onfnKBVHb_Bd7jQn5t4aYg/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "equip_item", "character_id": "character-0001", "item_id": "chainmail"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [],
  "messages": [],
  "mode": "town",
  "ended": false,
  "revision": 2
}
```

Send the same command with `"item_id": "shield"` and then with `"item_id": "sword"`. The player view then shows 60 gold in `purse`, the chainmail in `worn_armour`, the shield in `shield`, and the sword in `wielded`. If the total price is more than the purse, the session refuses the whole purchase with the rejection `items.purchase.insufficient_funds` and buys nothing. A session plays a copy of the character, so the purchases don't change the stored character.

## Start a session

A *session* is one game in progress. Start one with the ids of the characters in the *party*, the group that plays, and the id of an adventure. The order of `character_ids` is the party's *marching order*. The first character walks in front, and only the characters at the front can fight in *melee*, hand to hand. A session plays copies of the characters, so the stored characters don't change.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_PUtflt1xPCTJIwhqMcAIrA", "chr_kqRWmM-1d1lMQFy-4CFVnw", "chr_8l3D6o0BvEL_V99x7NUK1w"]}'
```

```json
{
  "id": "ses_Bq2lhAvBgBLVtCRWuWkVUQ",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "town",
  "ended": false,
  "revision": 0,
  "clock": {"rounds": 0, "turns": 0, "days": 0},
  "schema_version": 4,
  "engine_version": "2.5.0",
  "created_at": "2026-11-21T05:24:33Z",
  "updated_at": "2026-11-21T05:24:33Z"
}
```

- `mode` is what the party is doing. A session starts in `town`. The other modes are `exploring`, `encounter`, `battle`, `game_over`, and `victory`. The mode decides which commands are legal.
- `ended` is `true` in the modes `game_over` and `victory`.
- `revision` counts the commands the session has accepted. It starts at 0.
- `clock` is the game time that has passed, in rounds of 10 seconds, turns of 10 minutes, and days.

A session's party can have at most 20 characters.

## Read the legal commands

You play by sending *commands*, like a move or an attack. The server lists the commands that are legal in the session's current mode:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY"
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

A player sees only commands with `kind` set to `play`. The rules can still refuse a command that's listed, for a reason they check when you send it. To learn which fields a command takes, see [Rules reference and generators](reference-and-generators.md#read-a-commands-json-schema).

## Enter the dungeon

Send a command as a JSON body with a `command_type` and the fields that the command takes. `enter_dungeon` takes the id of a dungeon. The built-in adventure has one dungeon, `barrow`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "enter_dungeon", "dungeon_id": "barrow"}'
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
- `rejections` lists why the rules refused it. It's empty here.
- `events` lists what happened, as data your game can act on. Each event has a `code`, an `event_type`, and fields that depend on the type.
- `messages` has one line of English for each event, in the same order. Show them to the player.
- `mode` and `ended` are the session's mode and whether it's over after the command.
- `revision` is the session's revision after the command. It went from 0 to 1.

Messages and events identify characters and monsters by id, like `character-0001` and `monster-0001`. When a session starts, the rules engine gives each character in the party an id of the form `character-NNNN`, in marching order. The player view, described below, lists each party member's id and name.

## Move, and see a refused command

The party faces north when it enters. Try to move north:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "north"}'
```

```text
HTTP/1.1 200 OK
...
content-type: application/json

{"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
```

A wall blocks the way, so the rules refuse the command. The status is 200, because the request was valid and the server did what you asked: it passed the command to the rules engine. A refused command has `accepted` set to `false`, a `rejections` list, and no events, and it changes nothing. The revision is still 1. Outside character creation, an error status like 422 means your request itself was wrong. For more about the difference, see [Errors](errors.md).

Move east instead:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
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

Move east again. The next cell is the guard room, which has goblins in it:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [...],
  "messages": [
    "The party moves to (2, 0), facing east.",
    "The party enters area guard_room (level 1).",
    "Encounter: 2 goblins at 20'.",
    "The monsters' bearing: hostile."
  ],
  "mode": "encounter",
  "ended": false,
  "revision": 3
}
```

The session is now in the `encounter` mode: the party has met monsters, and nobody has attacked yet. The dice decide whether anyone is surprised and what the goblins' *bearing* is (how they react to the party, which the player view lists as `stance`), so yours can differ. Ask for the legal commands again:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY"
```

```json
{
  "mode": "encounter",
  "revision": 3,
  "commands": [
    {"command_type": "drop_items", "kind": "play"},
    {"command_type": "give_items", "kind": "play"},
    {"command_type": "use_item", "kind": "play"},
    {"command_type": "parley", "kind": "play"},
    {"command_type": "evade", "kind": "play"},
    {"command_type": "engage_battle", "kind": "play"},
    {"command_type": "wait", "kind": "play"},
    {"command_type": "turn_undead", "kind": "play"}
  ]
}
```

The party can talk (`parley`), run (`evade`), or fight (`engage_battle`). A member can also hand an item to another member with `give_items`, which takes a round of the encounter. Before you choose, read the player view.

## Read the player view

The *player view* is everything the players may know about the game now. Read it after each command to update what your game shows:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/view -H "Authorization: Bearer $KEY"
```

```json
{
  "adventure_name": "The Barrow of the Forgotten King",
  ...
  "town_healing_prices": {"cure_light_wounds": 25, "cure_serious_wounds": 100, "cure_disease": 150, "neutralize_poison": 150, "remove_curse": 200, "raise_dead": 1500},
  "party": [
    {"id": "character-0001", "name": "Brenna", "class_id": "fighter", "level": 1, "current_hp": 3, "max_hp": 3, "conditions": [], "inventory": {...}, "memorized_spells": [], "encounter_rate_feet": 20, "race": "human", "alignment": "lawful", "title": "Veteran", "xp": 0, "xp_next_level": 2000, "scores": {...}, ..., "thac0": 19, "attack_bonus": 0, "armour_class": 4, "armour_class_ascending": 15, "saves": {...}, "movement_rate_feet": 60, ...},
    {"id": "character-0002", "name": "Tomas", "class_id": "cleric", "level": 1, "current_hp": 4, "max_hp": 4, ...},
    {"id": "character-0003", "name": "Wren", "class_id": "thief", "level": 1, "current_hp": 3, "max_hp": 3, ..., "thief_skills": {"climb_sheer_surfaces": 87, "find_remove_treasure_traps": 10, "hear_noise": 2, ...}}
  ],
  "location": {"kind": "dungeon", "dungeon_id": "barrow", "level_number": 1, "position": [2, 0], "facing": "east"},
  "mode": "encounter",
  "explored": [{"dungeon_id": "barrow", "level_number": 1, "cells": [[0, 0], [1, 0], [2, 0]], "edges": {...}}],
  "journal": [...],
  "quests": [{"id": "the-idol", "name": "The Jade Idol", ...}],
  "encounter": {
    "groups": [{"id": "group-0001", "label": "goblin", "label_plural": "goblins", "family_id": "goblin", "categories": ["person"], "count": 2, "distance_feet": 20, "visible_conditions": [], "condition_counts": {}, "fled": false, "member_distance_feet": {}, "front_rank": [], ...}],
    "stance": "hostile",
    "in_battle": false,
    "declarers": ["character-0001", "character-0002", "character-0003"],
    "front_rank": ["character-0001", "character-0002"],
    ...
  },
  ...
}
```

The session is in the `encounter` mode and no battle has started, so `in_battle` is `false` and `member_distance_feet` is empty. These fields are the ones you use in the battle:

- `party` lists each member's `id`, `name`, hit points, `conditions` (like `dead`), and inventory. It also has every other number on a B/X character sheet, like `scores`, `armour_class`, `thac0`, `saves`, and `thief_skills`. Use it to turn the ids in messages into names, and to draw each character's sheet. The `character-0001` in a message is Brenna. The numbers count every item the member wears, identified or not, because those are the numbers the monsters roll against. So a member who equips an unidentified shield +1 shows an `armour_class` one better than a plain shield gives, while the inventory still calls the shield "a shield with a faint aura". A player can infer an item's bonus or curse that way.
- `location` and `explored` contain the party's position and the cells it has seen. The view doesn't include the parts of the map the party hasn't explored.
- `encounter.groups` lists the groups of monsters. A group has an `id` to put in an attack's `target_group_id`, a `count`, and its `distance_feet` from the party. `label` and `label_plural` are what to call one monster of the group and several, like `goblin` and `goblins`. They're text to show, so don't match on their words. To tell what kind of creature a group is, read `family_id`, which is the same for every group of goblins, and `categories`, like `undead` for a group that a cleric can try to turn. The rules reference has no lookup by `family_id`, because a family can have several templates, like `hellhound_3` to `hellhound_7`. To find them, pass the `family_id` as `q` to `GET /v1/rules/monsters`, which also returns other monsters whose id or name contains it, like `hobgoblin` for `goblin`. `visible_conditions` lists the conditions the players can see on the group's living monsters, and `condition_counts` contains the number of monsters with each condition. The player view lists groups, not individual monsters. When a message refers to `monster-0001` and the encounter has one group, the monster is in that group. When the encounter has more than one group, the player view doesn't say which group the monster is in. Only the [referee view](referee.md#read-the-referee-view) lists each group's `monster_ids`.
- `encounter.declarers` lists the members who must choose an action this round.
- `encounter.front_rank` lists the members who can fight in melee. The others can't attack monsters in melee.

The view also has what a town screen needs, like `town_healing_prices`, the price in gold pieces of each service that the `purchase_healing` command buys from the temple. The view doesn't include monster hit points or the dice rolls that only a *referee* sees. A referee is a caller whose key has the role that may see a game's hidden state, as [Run games as a referee](referee.md) describes.

To get the view in the same response as a command's outcome, add `include=view` to the command request. The outcome then has a `view` member, the player view at the outcome's `revision`, and your client doesn't send a second request. With `include=view`, the move that started this encounter is this request:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

Its outcome ends with the view above:

```json
{
  "accepted": true,
  ...
  "mode": "encounter",
  "ended": false,
  "revision": 3,
  "view": {
    "adventure_name": "The Barrow of the Forgotten King",
    ...
  }
}
```

A refused command with `include=view` has the unchanged view.

## Start the battle

Fight:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle"}'
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

A battle runs in rounds. In each round, every member in `declarers` chooses an action, and the rules resolve all of them. Send `resolve_battle_round` with one declaration for each declarer. A declaration has a `character_id` and an `action`: `attack`, `cast`, `turn_undead`, `move`, `use_item`, `give_item`, or `hold`. `give_item` is how a member hands over an item in a battle, because the rules refuse `give_items` there. An attack also has a `target_group_id`. The goblins are 20 feet away, so an attacker adds `"move": "close"` to walk up to them and attack in the same round.

First, send what a client might try: all three characters attack.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "attack", "target_group_id": "group-0001", "move": "close"}]}'
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

Wren isn't in the `front_rank`, so the rules refuse the whole round, and nothing changes. Have Wren `hold` instead:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [
    {"code": "battle.round.started", "visibility": "player", "event_type": "battle_round", "round": 1},
    {"code": "combat.initiative.rolled", ...},
    {"code": "battle.member.moved", "visibility": "player", "event_type": "member_moved", "character_id": "character-0001", "move": "close", "advance_feet": 15, "group_id": "group-0001", "distance_feet": 5},
    ...
  ],
  "messages": [
    "Battle round 1 begins.",
    "Initiative (side): party 3, monsters 1. Order: character-0001, character-0002, character-0003, group-0001.",
    "character-0001 closes to 5' from group-0001.",
    "character-0002 closes to 5' from group-0001.",
    "character-0001 misses monster-0001 with Sword: rolled 7-1 = 6, needing 13.",
    "character-0002 misses monster-0001 with Mace: rolled 4+1 = 5, needing 13.",
    "monster-0001 hits character-0001 with weapon: rolled 15+0 = 15, needing 15.",
    "character-0001 takes 1 damage from monster-0001.",
    "monster-0002 hits character-0002 with weapon: rolled 12+0 = 12, needing 10.",
    "character-0002 takes 2 damage from monster-0002."
  ],
  "mode": "battle",
  "ended": false,
  "revision": 5
}
```

The dice in the messages are random, so your round differs. The revision went from 4 to 5, and the battle goes on. Read the view again to see the party's hit points. In the game on this page, Brenna has 2 of 3 hit points left, and Tomas has 2 of 4. A character at 0 hit points has the condition `dead`, and the next round's `declarers` leaves that character out.

The monsters fight in ranks too. In each group, only the first living monsters make melee attacks on the party, up to the number in the view's `encounter.formation_width`. In the guard room the width is 2, so both goblins can attack.

When a group fails its morale check, the round's messages include a line like `group-0001 flees the battle!`, from a `battle.side.fled` event. The group runs, but its `fled` stays `false` until it stands more than 120 feet from every living member. Until then, a member can still close on it or shoot at it. Once `fled` is `true`, the rules refuse any declaration aimed at the group, with the rejection `battle.declaration.unknown_group`. The battle ends when each group's `fled` is `true` or the group has no living monsters.

## Catch up on events

The server keeps a log of every event the players may see. Read it a page at a time. `after` is the number of events you've already read, and `limit` is the page size. Read the first three:

```sh
curl -s "http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/events?limit=3" -H "Authorization: Bearer $KEY"
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
curl -s "http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/events?after=15" -H "Authorization: Bearer $KEY"
```

```json
{
  "events": [...],
  "messages": [
    "character-0001 takes 1 damage from monster-0001.",
    "monster-0002 hits character-0002 with weapon: rolled 12+0 = 12, needing 10.",
    "character-0002 takes 2 damage from monster-0002."
  ],
  "next_after": 18,
  "total": 18
}
```

The `limit` can be up to 500. A page contains at most `limit` events, so keep asking while `next_after` is less than `total`.

## Check for changes without downloading them again

More than one device can play the same session, and each one needs to notice when another has sent a command. Every 200 response to a `GET` has an `ETag` header, a short name for the response's content. Read the view and note its `ETag`:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/view -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 200 OK
...
etag: W/"730ae2329d30fc29ddae6f1272e917f2"
cache-control: private, no-cache
```

To ask whether the view has changed, send the `ETag` in `If-None-Match`:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/view -H "Authorization: Bearer $KEY" -H 'If-None-Match: W/"730ae2329d30fc29ddae6f1272e917f2"'
```

```text
HTTP/1.1 304 Not Modified
...
etag: W/"730ae2329d30fc29ddae6f1272e917f2"
```

The status 304 means that the view you have is current, and the response has no body. After a command changes what the players see, the same request gets 200 with the new view and a new `ETag`. The same works for every `GET`, like the session's info and the event log. A browser's `fetch` sends `If-None-Match` for you, because the response has `Cache-Control: private, no-cache`.

The server also compresses a response of 1,024 bytes or more for a client that sends `Accept-Encoding: gzip`. Most HTTP libraries send that header and decompress the response for you. With curl, add `--compressed`.

## Save and restore

A *save* is a copy of a session at one moment. Save the session before the second round, with a label:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/saves -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"label": "Before round 2"}'
```

```json
{
  "id": "sav_E6NKZNiBl4c2jL_rkyY8Bg",
  "session_id": "ses_Bq2lhAvBgBLVtCRWuWkVUQ",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "label": "Before round 2",
  "mode": "battle",
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  "created_at": "2026-11-21T05:24:33Z"
}
```

Saving doesn't change the session or its revision. The body is optional, and a save without a label has `"label": null`. A player can't read the save's contents. The save is for restoring.

Restore the save to start a new session from that moment. The original session and the save stay as they are, so you can restore the same save as many times as you like. The body is optional:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/saves/sav_E6NKZNiBl4c2jL_rkyY8Bg/restore -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Second try"}'
```

```json
{
  "id": "ses_69rujo4njhlTcX0L0EgfEw",
  "name": "Second try",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "battle",
  "ended": false,
  "revision": 0,
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  ...
}
```

The new session is in the same mode, with the same party, and its revision starts again at 0. Its `adventure_id` is the save's. The `adventure_id` is `null` for a save that a referee imported, and it can be the id of an adventure that you deleted after you saved. Neither changes play, because each session has a copy of its adventure.

## Retry a request safely

A request can fail after the server ran it, for example when the connection drops before the response arrives. If your client sends a `POST` again, the server runs it a second time: a second character, a second session, or a second battle round. To prevent that, choose a new value for each request, like a UUID, and send it in the `Idempotency-Key` header of the request and of every retry:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/saves -H "Authorization: Bearer $KEY" -H "Idempotency-Key: 0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11"
```

```text
HTTP/1.1 201 Created
...
{"id":"sav_YkM2nXS6Nn0JvCqk1vXh4w","session_id":"ses_69rujo4njhlTcX0L0EgfEw",...}
```

Send the same request with the same key again, and the server returns the first response without saving a second time. The header `Idempotent-Replayed: true` marks the stored response:

```text
HTTP/1.1 201 Created
...
idempotent-replayed: true

{"id":"sav_YkM2nXS6Nn0JvCqk1vXh4w","session_id":"ses_69rujo4njhlTcX0L0EgfEw",...}
```

The key works on every `POST`, including a command. These rules apply:

- The server remembers a key for 24 hours, and a key belongs to your client.
- A request that fails with an error other than 500 `internal_error` changed nothing, so the server forgets its key. Fix the request if the error says to, and send it with the same key or a new one.
- Use a new key for each new request. The same key with a different body gets 422 `idempotency_key_reused`.
- While the first request still runs, a retry gets 409 `idempotency_key_in_progress`. Wait a second and send it again.
- If the server failed or stopped in the middle of the first request, a retry gets 409 `idempotency_key_interrupted`, because the server has no record of whether the request took effect. Read the session or the list to find out.

### Retry a command with the revision

For a command, the session's revision gives you a second way to retry safely, with no key. Send the revision you last read as the `expected_revision` query parameter. The server runs the command only if the session is still at that revision.

Send the second round to the restored session, which is at revision 0:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands?expected_revision=0" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```json
{
  "accepted": true,
  "rejections": [],
  "events": [...],
  "messages": [
    "Battle round 2 begins.",
    ...
    "character-0002 is dead.",
    "character-0002 is killed."
  ],
  "mode": "battle",
  "ended": false,
  "revision": 1
}
```

Now suppose the response was lost and your client sends the same request again:

```sh
curl -si -X POST "http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands?expected_revision=0" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```text
HTTP/1.1 409 Conflict
...
content-type: application/problem+json

{"type":"about:blank","title":"Conflict","status":409,"detail":"The session is at revision 1, not 0. Read the view and decide whether to send the command again.","code":"revision_conflict","request_id":"req_p7XWyfcbC8hFgnPx37l7rA","revision":1}
```

The session is at revision 1, so the server ran nothing. When you get a `revision_conflict` after a retry and nobody else sends commands to the session, the server applied the first request. Read the view to see the result. The retry doesn't return the lost response, so to get the first response back, use an `Idempotency-Key` instead. For what to do in other cases, see [Errors](errors.md#revision_conflict).

Every response to a command has the new `revision`, and `GET /v1/sessions/{session_id}/commands` has the current one, so your client has the revision to send with the next command.

## Next steps

- [Errors](errors.md): every error code and what to do about it.
- [Rules reference and generators](reference-and-generators.md): look up the classes, spells, and equipment your players choose from, and read the schema of each command.
- [Authentication](authentication.md): run a server with API keys for your game.
- [Example client](../examples/README.md): a Python script that plays this battle and picks each command from the legal commands and the player view.
- [Run games as a referee](referee.md): see hidden state, send referee commands, and choose seeds.
