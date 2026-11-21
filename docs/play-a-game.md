# Play a game

Play one game of the built-in adventure with `curl`, from rolling a character's ability scores to saving the game in the middle of a battle. A game client that you build sends the same requests. Before you begin, set up bxapi by following [Start the server](../README.md#start-the-server) in the README.

## Start a server with a player key

Start the server with one API key. The key's `client` is a name you choose for your application, and its `role` is `player`, the role a game client uses. The server shows a player key only what the players of a game may see:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' uv run bxapi
```

In a second terminal, store the key in a shell variable. Every command on this page sends it as a bearer token:

```sh
export KEY=player-key-0123456789
```

The examples on this page differ from what your terminal shows in three ways:

- **Formatting:** The server returns compact JSON. We've formatted the responses on this page and shortened them with `...`.
- **IDs:** The server generates an ID for each character, session, and save, so your IDs differ from the ones shown. Replace each ID in a command with the one your server returned.
- **Dice:** The dice are random, so your characters' scores, their *hit points* (the damage a character can take), and your battle results differ too. The text notes where your results can differ.

To verify the key, ask the server which client and role the key identifies:

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
  "role": "player",
  "storage": {"used_bytes": 0, "max_bytes": 1073741824}
}
```

## Roll ability scores

In B/X, a player first rolls six *ability scores*, the numbers such as strength and dexterity that the rules use for a character, and then picks a *class*, such as fighter or thief, that the scores qualify for. An *ability roll* is that first step. Ask the server to roll the scores and store them:

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

The response describes the stored roll:

- `id`: The roll's ID. Pass it as `ability_roll_id` when you create a character.
- `scores`: One score from 3 to 18 for each ability: strength, intelligence, wisdom, dexterity, constitution, and charisma.
- `eligible_class_ids`: The classes the scores qualify for. Show the list to the player so they can choose.
- `used`: Whether a character has been created from the roll. You can create one character from a roll, and `used` becomes `true` when you do.

The server doesn't limit the number of ability rolls you request. If your game needs a limit, your game must enforce it.

## Create a character

Create a fighter from the roll. A character's *purchases* are the starting gear it buys with its starting gold, and `equip_item_ids` lists the purchased items it wears or wields. To see how the server handles a choice that breaks a rule, first try to buy ten suits of plate mail, which cost more than the character has:

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

The server rejects a choice that breaks a rule with `422 character_invalid`. Character creation is the one place where a broken rule is an error, because the server can't create the character. The `rejections` list comes from the *rules engine*, osrlib, which applies the B/X rules for the server. Each rejection has a `code` and `params` that your game can turn into a message for the player.

The roll stays unused after a rejected request, so you can try again with the same `ability_roll_id`. This time, buy chainmail, a shield, and a sword, and equip all three:

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

The response is the stored character:

- `id`: The character's ID. A *session*, one game in progress, uses it to put the character in a *party*, the group of characters that plays.
- `character`: The character as the rules engine stores it: scores, hit points, inventory, and spells.
- `derived`: The numbers the rules engine computes from the character, including these three:
  - `thac0` ("to hit armour class 0"): The number the character must roll on a 20-sided die to hit armour class 0.
  - `armour_class`: How hard the character is to hit, where lower is better.
  - `saves`: The character's *saving throws*, the 20-sided die rolls that resist dangers such as poison and dragon breath. They aren't saved games.

If the starting gold doesn't cover the chainmail, the shield, and the sword, the server rejects the request with the same `insufficient_funds` rejection. Buy less and try again.

When a request has no `ability_roll_id`, the server rolls the scores as part of the request. Create two more characters that way, a cleric and a thief:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "purchases": [{"item_id": "mace"}, {"item_id": "leather"}], "equip_item_ids": ["mace", "leather"]}'
```

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Wren", "class_id": "thief", "alignment": "neutral", "purchases": [{"item_id": "dagger"}, {"item_id": "leather"}], "equip_item_ids": ["dagger", "leather"]}'
```

Each response has the same fields as Brenna's. The rest of this page uses these character IDs:

- Brenna: `chr_PUtflt1xPCTJIwhqMcAIrA`
- Tomas: `chr_kqRWmM-1d1lMQFy-4CFVnw`
- Wren: `chr_8l3D6o0BvEL_V99x7NUK1w`

### Show starting gold before buying gear

The server rolls a character's starting gold in the same request that creates the character, so a player who sends `purchases` chooses gear before they know how much gold they have. To show the gold first, create the character with no `purchases` and read the gold from `character.inventory.purse` in the response:

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

Garrick has 120 gold pieces (`gp`). Show the gold to the player, then buy the gear in a session while the session is in `town` mode, the mode every session starts in. The next sections describe sessions and commands in detail. Start a session with Garrick as the only member of the party:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_44TkoTKtpBs4IniRSW03Mg"]}'
```

The response has `"id": "ses_onfnKBVHb_Bd7jQn5t4aYg"` and `"mode": "town"`. The `purchase_equipment` command buys equipment for one party member, and the session gives the first member of the party the ID `character-0001`. Each entry in `item_ids` is one purchase of an item from the shop:

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

The purchased items are now in Garrick's pack. The `equip_item` command makes a member wear or wield one item, so send it once for each item, starting with the chainmail:

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

Send the same command with `"item_id": "shield"` and then with `"item_id": "sword"`. The player view then shows 60 gold in `purse`, the chainmail in `worn_armour`, the shield in `shield`, and the sword in `wielded`.

If the total price exceeds the purse, the session rejects the whole purchase with `items.purchase.insufficient_funds` and buys nothing. A session plays a copy of the character, so the purchases don't change the stored character.

## Start a session

A session needs the IDs of the characters in its party and the ID of an adventure. The order of `character_ids` is the party's *marching order*: the first character walks in front, and only the characters at the front can fight in *melee*, hand to hand. A session plays copies of the characters, so the stored characters don't change. Start a session with the three characters and the built-in adventure:

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

The response is the session's info. Four of its fields track the state of the game:

- `mode`: What the party is doing. A session starts in `town`, and the other modes are `exploring`, `encounter`, `battle`, `game_over`, and `victory`. The mode determines which commands are legal.
- `ended`: Whether the game is over. It's `true` in the `game_over` and `victory` modes.
- `revision`: The number of commands the session has accepted. It starts at 0.
- `clock`: The game time that has passed, in rounds of 10 seconds, turns of 10 minutes, and days.

A session's party can have at most 20 characters.

## List the legal commands

You play by sending *commands*, such as a move or an attack. Ask the server for the commands that are legal in the session's current mode:

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

A player key sees only the commands whose `kind` is `play`. The rules can still reject a listed command, for a reason they check only when you send it. To learn which fields a command takes, see [Read a command's JSON schema](reference-and-generators.md#read-a-commands-json-schema).

## Enter the dungeon

Send a command as a JSON body with a `command_type` and the fields that the command takes. The `enter_dungeon` command takes the ID of a dungeon, and the built-in adventure has one dungeon, `barrow`:

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

The response is the *command outcome*, which the server returns for every command:

- `accepted`: Whether the rules accepted the command.
- `rejections`: The reasons the rules rejected the command. The list is empty here.
- `events`: What happened, as data your game can act on. Each event has a `code`, an `event_type`, and fields that depend on the type.
- `messages`: One line of English for each event, in the same order. Show the messages to the player.
- `mode` and `ended`: The session's mode after the command, and whether the game is over.
- `revision`: The session's revision after the command. Here it went from 0 to 1.

Messages and events identify characters and monsters by ID, such as `character-0001` and `monster-0001`. When a session starts, the rules engine gives each character in the party an ID of the form `character-NNNN`, in marching order. The player view lists each party member's ID and name, as [Read the player view](#read-the-player-view) shows.

## Move the party

The party faces north when it enters the dungeon. Try to move north, with the `-i` option so that `curl` prints the status line and the headers:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "north"}'
```

```text
HTTP/1.1 200 OK
...
content-type: application/json

{"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
```

A wall blocks the way, so the rules reject the command. The status is still `200`, because the request was valid and the server did what you asked: it passed the command to the rules engine. A rejected command has `accepted` set to `false`, a `rejections` list, and no events. It changes nothing, so the revision is still 1.

Outside character creation, an error status such as `422` means that your request itself was wrong. For more about the difference, see [Rejected commands versus errors](errors.md#rejected-commands-versus-errors).

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

The session is now in `encounter` mode: the party has met monsters, but nobody has attacked yet. The dice decide whether anyone is surprised and what the goblins' *bearing* is, so your encounter can differ. The bearing is how the monsters react to the party, and the player view lists it as `stance`. List the legal commands again:

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

The *player view* is everything the players may know about the game at the current moment. Read it after each command to update what your game shows:

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

The session is in `encounter` mode and no battle has started, so `in_battle` is `false` and `member_distance_feet` is empty. The battle later on this page uses these fields:

- `party`: Each member's `id`, `name`, hit points, `conditions` (such as `dead`), and inventory. It also has every other number on a B/X character sheet, such as `scores`, `armour_class`, `thac0`, `saves`, and `thief_skills`. Use it to turn the IDs in messages into names and to draw each character's sheet. The `character-0001` in a message is Brenna.
- `location` and `explored`: The party's position and the cells it has seen. The view doesn't include the parts of the map that the party hasn't explored.
- `encounter.groups`: The groups of monsters, which [Monster groups](#monster-groups) describes.
- `encounter.declarers`: The members who must choose an action this round.
- `encounter.front_rank`: The members who can fight in melee. The other members can't attack monsters in melee.

The view also has what a town screen needs, such as `town_healing_prices`, the price in gold pieces of each service that the `purchase_healing` command buys from the temple.

The view leaves out monster hit points and the dice rolls that only a *referee* key can read. The referee role may see a game's hidden state, as [Run games as a referee](referee.md) describes.

### Unidentified items on a character sheet

The numbers on a member's sheet count every item the member wears, identified or not, because those are the numbers the monsters roll against. A member who equips an unidentified shield +1 therefore shows an `armour_class` one better than a plain shield gives, while the inventory still calls the shield `a shield with a faint aura`. A player can infer an item's bonus or curse from that difference.

### Monster groups

The player view lists groups of monsters, not individual monsters. Each entry in `encounter.groups` describes one group:

- `id`: The ID to put in an attack's `target_group_id`.
- `count` and `distance_feet`: The number of monsters in the group, and the group's distance from the party.
- `label` and `label_plural`: What to call one monster of the group and several, such as `goblin` and `goblins`. They're text to show, so don't match on their words.
- `family_id` and `categories`: The kind of creature. `family_id` is the same for every group of goblins, and `categories` has values such as `undead` for a group that a cleric can try to turn.
- `visible_conditions` and `condition_counts`: The conditions the players can see on the group's living monsters, and the number of monsters with each condition.

The rules reference has no lookup by `family_id`, because a family can have several templates, such as `hellhound_3` to `hellhound_7`. To find a family's templates, pass the `family_id` as `q` to `GET /v1/rules/monsters`. That search also returns other monsters whose ID or name contains the value, such as `hobgoblin` for `goblin`.

Messages name individual monsters, such as `monster-0001`, but the player view doesn't always say which group a monster belongs to. When the encounter has one group, the monster is in that group. When the encounter has more than one group, only the [referee view](referee.md#read-the-referee-view) identifies the monster's group, because it lists each group's `monster_ids`.

### Return the view with a command outcome

To get the view in the same response as a command's outcome, add `include=view` to the command request. The outcome then has a `view` field, the player view at the outcome's `revision`, so your client doesn't send a second request. With `include=view`, the move that started this encounter is this request:

```sh
curl -s -X POST "http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

The outcome of that request ends with the view shown above:

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

A rejected command sent with `include=view` returns the unchanged view.

## Start the battle

Send `engage_battle` to fight the goblins:

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

## Resolve a battle round

A battle runs in rounds. In each round, every member in `declarers` chooses an action, and the rules resolve all the actions together. Send `resolve_battle_round` with one declaration for each declarer.

A declaration has a `character_id` and an `action`, which is `attack`, `cast`, `turn_undead`, `move`, `use_item`, `give_item`, or `hold`. An attack also has a `target_group_id`. The goblins are 20 feet away, so an attacker adds `"move": "close"` to walk up to them and attack in the same round. A member hands over an item in a battle with the `give_item` action, because the rules reject the `give_items` command there.

First, send what a client might try, an attack from all three characters:

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

Wren isn't in `front_rank`, so the rules reject the whole round, and nothing changes. Have Wren `hold` instead:

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

The dice in the messages are random, so your round differs. The revision went from 4 to 5, and the battle continues. Read the view again to see the party's hit points: in the game on this page, Brenna has 2 of 3 hit points left, and Tomas has 2 of 4. A character at 0 hit points has the `dead` condition, and the next round's `declarers` omits that character.

The monsters fight in ranks too. Only the first living monsters of each group make melee attacks on the party, up to the number in the view's `encounter.formation_width`. The guard room's width is 2, so both goblins can attack.

A group that fails its morale check leaves the battle in three stages:

1. The round's messages include a line such as `group-0001 flees the battle!`, from a `battle.side.fled` event, and the group runs.
2. The group's `fled` field stays `false` until the group stands more than 120 feet from every living member. Until then, a member can still close on the group or shoot at it.
3. Once `fled` is `true`, the rules reject any declaration aimed at the group with `battle.declaration.unknown_group`.

The battle ends when every group either has `fled` set to `true` or has no living monsters.

## Read the event log

The server keeps a log of every event the players may see. Read the log one page at a time with two query parameters: `after`, the number of events you've already read, and `limit`, the page size. Read the first three events:

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

`next_after` is the value to send as `after` in your next request, and `total` is the number of events in the log. A client that loses its connection stores the last `next_after` it received. When the client reconnects, it sends that number as `after` and receives only the events it missed. Here, a client that had read 15 events asks for the remaining three:

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

The `limit` can be up to 500. A page contains at most `limit` events, so keep requesting pages while `next_after` is less than `total`.

## Poll for changes with ETags

More than one device can play the same session, and each device needs to notice when another has sent a command. Every `200` response to a `GET` has an `ETag` header, a short identifier for the response's content. Read the view and note its `ETag`:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/view -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 200 OK
...
etag: W/"730ae2329d30fc29ddae6f1272e917f2"
cache-control: private, no-cache
```

To ask whether the view has changed, send the `ETag` value in an `If-None-Match` header:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/view -H "Authorization: Bearer $KEY" -H 'If-None-Match: W/"730ae2329d30fc29ddae6f1272e917f2"'
```

```text
HTTP/1.1 304 Not Modified
...
etag: W/"730ae2329d30fc29ddae6f1272e917f2"
```

The `304` status means that the view you have is current, and the response has no body. After a command changes what the players see, the same request returns `200` with the new view and a new `ETag`. Every `GET` supports the same check, including the session's info and the event log. A browser's `fetch` sends `If-None-Match` for you, because the response has `Cache-Control: private, no-cache`.

The server also compresses a response of 1,024 bytes or more for a client that sends `Accept-Encoding: gzip`. Most HTTP libraries send that header and decompress the response for you. With `curl`, add `--compressed`.

## Save and restore

A *save* is a copy of a session at one moment. Save the session before the second round, and give the save a label:

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

Saving doesn't change the session or its revision. The request body is optional, and a save without a label has `"label": null`. A player key can restore a save but can't read its contents.

Restore the save to start a new session from that moment. The original session and the save stay as they are, so you can restore the same save as many times as you want. The request body is optional, and here it names the new session:

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

The new session is in the same mode with the same party, and its revision starts again at 0. Its `adventure_id` is the save's `adventure_id`, which isn't always the ID of a stored adventure: it's `null` for a save that a referee imported, and it can be the ID of an adventure that you deleted after you saved. Neither case changes play, because each session has its own copy of its adventure.

## Retry requests safely

A request can fail after the server has run it, for example when the connection drops before the response arrives. If your client then sends the `POST` again, the server runs it a second time and creates a second character, starts a second session, or resolves a second battle round. To prevent the duplicate, choose a new value for each request, such as a UUID, and send it in the `Idempotency-Key` header of the request and of every retry:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/saves -H "Authorization: Bearer $KEY" -H "Idempotency-Key: 0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11"
```

```text
HTTP/1.1 201 Created
...
{"id":"sav_YkM2nXS6Nn0JvCqk1vXh4w","session_id":"ses_69rujo4njhlTcX0L0EgfEw",...}
```

When you send the same request again with the same key, the server returns the first response without saving a second time. The `Idempotent-Replayed: true` header marks the stored response:

```text
HTTP/1.1 201 Created
...
idempotent-replayed: true

{"id":"sav_YkM2nXS6Nn0JvCqk1vXh4w","session_id":"ses_69rujo4njhlTcX0L0EgfEw",...}
```

The key works on every `POST`, including a command. The server handles keys by these rules:

- **Lifetime and scope:** The server remembers a key for 24 hours, and a key belongs to your client.
- **Failed requests:** A request that fails with an error other than `500 internal_error` changed nothing, so the server forgets its key. Fix the request if the error says to, and send it with the same key or a new one.
- **Reused keys:** Use a new key for each new request. The same key with a different body returns `422 idempotency_key_reused`.
- **Requests in progress:** A retry that arrives while the first request is still running returns `409 idempotency_key_in_progress`. Wait a second and send the retry again.
- **Interrupted requests:** If the server failed or stopped in the middle of the first request, a retry returns `409 idempotency_key_interrupted`, because the server has no record of whether the request took effect. Read the session or the list to find out.

### Retry a command with the revision

A session's revision gives you a second way to retry a command safely, without a key. Send the revision you last read as the `expected_revision` query parameter, and the server runs the command only if the session is still at that revision.

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

Now suppose that the response was lost and your client sends the same request again:

```sh
curl -si -X POST "http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands?expected_revision=0" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001"}, {"character_id": "character-0003", "action": "hold"}]}'
```

```text
HTTP/1.1 409 Conflict
...
content-type: application/problem+json

{"type":"about:blank","title":"Conflict","status":409,"detail":"The session is at revision 1, not 0. Read the view and decide whether to send the command again.","code":"revision_conflict","request_id":"req_p7XWyfcbC8hFgnPx37l7rA","revision":1}
```

The session is at revision 1, so the server ran nothing. When you receive a `revision_conflict` after a retry and nobody else sends commands to the session, the server applied the first request. Read the view to see the result. The retry doesn't return the lost response, so use an `Idempotency-Key` instead when you need the first response back. For what to do in other cases, see [Errors](errors.md#revision_conflict).

Every command outcome has the new `revision`, and `GET /v1/sessions/{session_id}/commands` returns the current one, so your client always has the revision to send with its next command.

## Next steps

- [Errors](errors.md): every error code and what to do about it.
- [Rules reference and generators](reference-and-generators.md): look up the classes, spells, and equipment your players choose from, and read the schema of each command.
- [Authentication](authentication.md): run a server with API keys for your game.
- [Example client](../examples/README.md): a Python script that plays this battle and picks each command from the legal commands and the player view.
- [Run games as a referee](referee.md): see hidden state, send referee commands, and choose seeds.
