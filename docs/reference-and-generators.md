# Rules reference and generators

Look up the rules content your game shows its players, and roll dice or generate treasure and monsters without starting a game.

The examples use a key in the shell variable `KEY`, as [Play a game](play-a-game.md#start-a-server-with-a-player-key) sets up. Every role can use every endpoint on this page.

## Look up rules content

The rules reference serves the content of the B/X rules that the server's rules engine loads: classes, monsters, spells, equipment, magic items, languages, and the printed tables. All of it is read-only.

| Path | Lists |
| --- | --- |
| `/v1/rules/classes` | Character classes. |
| `/v1/rules/monsters` | Monsters. |
| `/v1/rules/spells` | Spells. |
| `/v1/rules/equipment` | Weapons, armour, gear, and ammunition. |
| `/v1/rules/magic-items` | Magic items. |
| `/v1/rules/languages` | Languages. |
| `/v1/rules/tables` | The names of the printed tables: `abilities`, `combat`, `encounters`, and `treasure`. |
| `/v1/rules/commands` | The commands a game accepts. |

Add an id to a path to get one entry, like `/v1/rules/monsters/goblin`. `GET /v1/rules/license` returns the license of all this content.

### Search a list

A list endpoint returns one page of summaries. Search by name with `q`, which matches part of an entry's id or name and ignores case. The monster, spell, equipment, and magic item lists take `q`:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/monsters?q=gob&limit=2" -H "Authorization: Bearer $KEY"
```

```json
{
  "items": [
    {
      "id": "goblin",
      "name": "Goblin",
      "hit_dice": {"count": 1, "die": 8, "modifier": -1, "asterisks": 0, "average_hp": 3, "fixed_hp": null},
      "ac": 6,
      "ac_ascending": 13,
      "xp": 5,
      "alignment": {"options": ["chaotic"], "usual": null}
    },
    {
      "id": "hobgoblin",
      "name": "Hobgoblin",
      "hit_dice": {"count": 1, "die": 8, "modifier": 1, "asterisks": 0, "average_hp": 5, "fixed_hp": null},
      "ac": 6,
      "ac_ascending": 13,
      "xp": 15,
      "alignment": {"options": ["chaotic"], "usual": null}
    }
  ],
  "total": 2,
  "limit": 2,
  "offset": 0
}
```

`ac` is the armour class on the descending scale, where lower is better, and `ac_ascending` is the same armour class on the ascending scale.

Some lists have more filters:

| Path | Filters |
| --- | --- |
| `/v1/rules/spells` | `class_id` keeps the spells on one class's spell list, and `level` keeps one spell level. |
| `/v1/rules/equipment` | `item_type` is `weapon`, `armour`, `gear`, or `ammunition`. |
| `/v1/rules/magic-items` | `category` is one of `armour`, `misc`, `potion`, `ring`, `rod`, `staff`, `wand`, `scroll`, `sword`, or `weapon`. |
| `/v1/rules/commands` | `kind` is `play` or `referee`, and `mode` keeps the commands that are legal in one session mode. |

Filters combine. The first-level spells of the cleric class:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/spells?class_id=cleric&level=1" -H "Authorization: Bearer $KEY"
```

```json
{
  "items": [
    {"id": "cure_light_wounds", "name": "Cure Light Wounds", "level": 1, "spell_list": "cleric", "range": "The caster or a creature touched", "duration": "Instant"},
    {"id": "detect_evil_c", "name": "Detect Evil", "level": 1, "spell_list": "cleric", "range": "120’", "duration": "6 turns"},
    ...
  ],
  "total": 8,
  "limit": 50,
  "offset": 0
}
```

A class that casts no spells, like `fighter`, gets an empty list. An unknown class id gets a 404.

### Page through a list

Every list takes `limit`, from 1 to 200 with a default of 50, and `offset`, the number of items to skip. The response repeats both and adds `total`, the length of the whole list. There are 233 monsters. To read the third and fourth:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/monsters?limit=2&offset=2" -H "Authorization: Bearer $KEY"
```

```json
{
  "items": [
    {"id": "air_elemental_intermediate", "name": "Air Elemental (Intermediate)", ...},
    {"id": "air_elemental_lesser", "name": "Air Elemental (Lesser)", ...}
  ],
  "total": 233,
  "limit": 2,
  "offset": 2
}
```

Add `limit` to `offset` for the next page, and stop when the result is at or past `total`. Lists of your own data, like `GET /v1/characters`, page the same way, and the newest items come first.

The rules content changes only when the server's rules engine does, so every successful response under `/v1/rules` has the header `Cache-Control: private, max-age=3600`, and your client can cache it for an hour.

### Look up one entry

A detail endpoint returns the rules engine's full model of the entry:

```sh
curl -s http://127.0.0.1:8640/v1/rules/monsters/goblin -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "goblin",
  "name": "Goblin",
  "page": "Goblin.md",
  "intro": "Small, grotesque humanoids with pallid, earth-coloured skin and glowing, red eyes. Dwell underground.",
  "ac": 6,
  "ac_ascending": 13,
  ...
  "attacks": [...],
  "thac0": 19,
  "attack_bonus": 0,
  "movement": [{"rate_feet": 60, "encounter_rate_feet": 20, "descriptor": null}],
  "saves": {"values": {"death": 14, "wands": 15, "paralysis": 16, "breath": 17, "spells": 18}, "save_as": "NH"},
  "morale": 7,
  ...
  "xp": 5,
  ...
}
```

An id that doesn't exist gets a 404.

The rules reference is the whole rulebook, so it includes what a player's character wouldn't know in play, like what a cursed magic item does. What a party has learned about an item in a game is in that session's [player view](play-a-game.md#read-the-player-view).

## Read a command's JSON schema

You play a session by sending it commands. `GET /v1/rules/commands` lists them. A command has a `command_type`, a `kind` (`play` for a command a player may send, `referee` for one only a referee may send), the `allowed_modes` in which it's legal, and a one-line `summary`. The server has more commands than the default `limit`, so ask for all of them with `limit=200`. The commands that are legal in a battle:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/commands?kind=play&mode=battle" -H "Authorization: Bearer $KEY"
```

```json
{
  "items": [
    {
      "command_type": "resolve_battle_round",
      "kind": "play",
      "allowed_modes": ["battle"],
      "summary": "Resolve one battle round from one declaration per party member able to act."
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

This list is about modes in general. To see what a particular session accepts now, read `GET /v1/sessions/{session_id}/commands`, which also leaves out the commands your role may not send.

A command's detail adds `json_schema`, the JSON Schema that describes the command's body:

```sh
curl -s http://127.0.0.1:8640/v1/rules/commands/move_party -H "Authorization: Bearer $KEY"
```

```json
{
  "command_type": "move_party",
  "kind": "play",
  "allowed_modes": ["exploring"],
  "summary": "Move the party one cell, turning to face the way it goes.",
  "json_schema": {
    "$defs": {"Direction": {"enum": ["north", "east", "south", "west"], "title": "Direction", "type": "string", ...}},
    "description": "Move the party one cell, turning to face the way it goes.\n\nThe party must already be in a dungeon. ...",
    "properties": {
      "command_type": {"const": "move_party", "default": "move_party", "title": "Command Type", "type": "string"},
      "source": {...},
      "direction": {"$ref": "#/$defs/Direction"}
    },
    "required": ["direction"],
    "title": "MoveParty",
    "type": "object"
  }
}
```

To build a request from a schema:

- Send `command_type` with the value in the schema's `const`.
- Send each field in `required`. Leave out the others to use their defaults.
- A `$ref` points at a definition in `$defs`. Here, `direction` is one of the four directions in the `enum`.
- Read `description` for the rules of the command. It documents the modes the command works in, the `rejections` it can return, and the events it causes.

A command's `source` field is for referees. A player who sets it gets 403 `referee_required`.

A game can generate its forms from the schemas, or it can hard-code the few commands it uses and read the schema when it adds one. The OpenAPI document at `/openapi.json` describes every command in the body of the commands endpoint too.

## Roll dice

`POST /v1/dice/roll` rolls a dice expression. The expression is the number of dice, `d`, and the number of sides, with an optional `+` or `-` modifier and an optional multiplier after `x`, like `3d6`, `d%`, `1d6+1`, or `2d6+1x10`. The sides are 2, 3, 4, 6, 8, 10, 12, 20, or 100, and `d%` means `d100`. The modifier applies before the multiplier, so `2d6+1x10` is (2d6 + 1) × 10. `times` is how many times to roll the expression, from 1 to 100:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"expression": "2d6+1x10", "times": 2, "seed": 7}'
```

```json
{
  "expression": "2d6+1x10",
  "seed": 7,
  "rolls": [
    {"rolls": [3, 2], "modifier": 1, "multiplier": 10, "total": 60},
    {"rolls": [4, 2], "modifier": 1, "multiplier": 10, "total": 70}
  ]
}
```

Each result has the value of each die in `rolls`, the `modifier` and `multiplier` from the expression, and the `total`. An expression the server can't parse, or with a die size it doesn't use, gets 422 `content_invalid`.

## Choose a seed to get the same result again

A *seed* is a number that decides every random roll in a request. Every dice and generator request accepts an optional `seed`, an integer from 0 to 9,007,199,254,740,991 (2^53 - 1, the largest integer that a JavaScript client reads exactly). When you leave it out, the server picks a random seed, and every response includes the seed it used. The same request with the same seed returns the same result:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"expression": "3d6"}'
```

```json
{"expression":"3d6","seed":4905287810713684,"rolls":[{"rolls":[3,3,6],"modifier":0,"multiplier":1,"total":12}]}
```

The seed and the roll differ for you. Send the same body with the seed from the response, and the server rolls 3, 3, and 6 again. Store the seed to be able to show a roll again later. A request with `times` set to 3 gives the first three results of the same request with `times` set to 5.

The seeds on this page are for stateless requests. A session's own seed is different: the server picks it, never returns it, and only a referee can choose one. See [Run games as a referee](referee.md#start-a-repeatable-session).

## Generate treasure, monsters, rooms, and characters

The generators roll content with the same rules as a game and store nothing. Each takes a seed and returns it. Use them for a loot screen, a random encounter, or a character preview.

Treasure of a type from `A` to `V` takes a `tier`: `basic` for a party of levels 1 to 3 and `expert` from level 4. The tier decides which magic item table to roll on.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/treasure -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"treasure_type": "C", "tier": "basic", "seed": 7}'
```

```json
{
  "seed": 7,
  "treasure": {
    "coins": {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 6000},
    "valuables": [],
    "magic_items": [
      {"instance_type": "magic_item", "instance_id": "magic-item-0001", "template_id": "potion_of_poison", "identified": false, ...},
      {"instance_type": "magic_item", "instance_id": "magic-item-0002", "template_id": "potion_of_healing", "identified": false, ...}
    ]
  }
}
```

Monsters, by id from `/v1/rules/monsters`, with `count` from 1 to 100. Each monster has its own rolled hit points, and `template` is the monster's full entry:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/monsters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"monster_id": "goblin", "count": 2, "seed": 7}'
```

```json
{
  "seed": 7,
  "monsters": [
    {"id": "monster-0001", "template": {...}, "max_hp": 1, "current_hp": 1, "conditions": [], ...},
    {"id": "monster-0002", "template": {...}, "max_hp": 3, "current_hp": 3, "conditions": [], ...}
  ]
}
```

A room on a dungeon level, counting from 1. The server rolls what the room holds:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/room -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"level_number": 1, "seed": 7}'
```

```json
{
  "seed": 7,
  "room": {
    "contents": "monster",
    "treasure_present": false,
    "encounter": {"monsters": [{"template_id": "gecko", "count_dice": null, "count_fixed": 3}], "alignment": null, "aware": false, "stance": null, "hoard": false},
    "npc_party": null,
    "treasure": null
  }
}
```

A party of non-player adventurers, `basic` for levels 1 to 3 or `expert` for a higher-level party, with `count` members from 1 to 100:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/npc-party -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"kind": "basic", "count": 3, "seed": 7}'
```

```json
{
  "seed": 7,
  "party": {
    "kind": "basic",
    "alignment": "chaotic",
    "members": [
      {"id": "npc-0001", "name": "Fighter adventurer 1", "class_id": "fighter", "level": 2, ...},
      {"id": "npc-0002", "name": "Fighter adventurer 2", "class_id": "fighter", "level": 1, ...},
      {"id": "npc-0003", "name": "Magic-User adventurer 3", "class_id": "magic_user", "level": 2, ...}
    ],
    "treasure": {...}
  }
}
```

A character, from the choices that `POST /v1/characters` takes except `ability_roll_id`. The server rolls the ability scores, so the same body with the same seed returns the same character:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/character -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Garr", "class_id": "dwarf", "alignment": "lawful", "seed": 7}'
```

```json
{
  "seed": 7,
  "character": {"id": null, "name": "Garr", "class_id": "dwarf", "race": "dwarf", "level": 1, "scores": {"str": 14, "int": 6, "wis": 9, "dex": 10, "con": 11, "cha": 11}, "max_hp": 3, ...},
  "derived": {"thac0": 19, "armour_class": 9, ...},
  "rolls": {"ability_rolls": {...}, "hit_point_roll": {...}, "gold_roll": {...}}
}
```

`rolls` has the dice behind the character: the ability scores, the hit points, and the starting gold. The server doesn't store the character, so a session can't use it. To play a character, create it with `POST /v1/characters`.

## Next steps

- [Play a game](play-a-game.md): use the commands in a game.
- [Errors](errors.md): what a 422 on these endpoints means.
- [Run games as a referee](referee.md): choose the seed of a session.
