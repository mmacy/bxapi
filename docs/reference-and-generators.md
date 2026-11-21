# Rules reference and generators

Look up the rules content your game shows its players, and roll dice or generate treasure, monsters, rooms, and characters without starting a game.

The examples send an API key from the shell variable `KEY`, which [Play a game](play-a-game.md#start-a-server-with-a-player-key) sets up. Keys of both roles, `player` and `referee`, can call every endpoint on this page.

## Look up rules content

The rules reference serves the B/X rules content that the server's rules engine loads: classes, monsters, spells, equipment, magic items, languages, and the tables printed in the rules. All of it is read-only. Each path in this table returns a list:

| Path | Lists |
| --- | --- |
| `/v1/rules/classes` | Character classes. |
| `/v1/rules/monsters` | Monsters. |
| `/v1/rules/spells` | Spells. |
| `/v1/rules/equipment` | Weapons, armour, gear, and ammunition. |
| `/v1/rules/magic-items` | Magic items. |
| `/v1/rules/languages` | Languages. |
| `/v1/rules/tables` | The names of the sets of printed tables: `abilities`, `combat`, `encounters`, and `treasure`. |
| `/v1/rules/commands` | The commands a game accepts. |

Append an ID to a path to get a single entry, such as `/v1/rules/monsters/goblin`. `GET /v1/rules/license` returns the license that covers all of this content.

### Search and filter a list

A list endpoint returns one page of summaries. The monster, spell, equipment, and magic item lists accept `q`, a case-insensitive search that matches part of an entry's ID or name. This request searches the monster list for `gob`:

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

In each summary, `ac` is the armour class on the descending scale, where lower is better, and `ac_ascending` is the same armour class on the ascending scale.

Four lists accept additional filters:

| Path | Filters |
| --- | --- |
| `/v1/rules/spells` | `class_id` limits the list to the spells on one class's spell list, and `level` limits it to one spell level. |
| `/v1/rules/equipment` | `item_type` is `weapon`, `armour`, `gear`, or `ammunition`. |
| `/v1/rules/magic-items` | `category` is one of `armour`, `misc`, `potion`, `ring`, `rod`, `staff`, `wand`, `scroll`, `sword`, or `weapon`. |
| `/v1/rules/commands` | `kind` is `play` or `referee`, and `mode` limits the list to the commands that are legal in one session mode. |

Filters combine. This request lists the first-level spells on the cleric spell list:

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

A class that casts no spells, such as `fighter`, returns an empty list, whereas an unknown class ID returns a `404`.

### Page through a list

Every list accepts `limit`, from 1 to 200 with a default of 50, and `offset`, the number of items to skip. The response repeats both values and adds `total`, the length of the whole list. The monster list has 233 entries, and this request reads the third and fourth:

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

To get the next page, add `limit` to `offset`, and stop when the new offset reaches or exceeds `total`. Lists of your own data, such as `GET /v1/characters`, page the same way and return the newest items first.

The rules content changes only when the server's rules engine changes, so every successful response under `/v1/rules` has the header `Cache-Control: private, max-age=3600`, and your client can cache the response for an hour. After the hour, send the response's `ETag` in `If-None-Match`, as [Play a game](play-a-game.md#poll-for-changes-with-etags) shows. While the content is unchanged, the server returns a `304` with no body, and your client keeps its copy for another hour.

### Get a single entry

A detail endpoint returns the rules engine's full model of the entry. This request retrieves the goblin's entry:

```sh
curl -s http://127.0.0.1:8640/v1/rules/monsters/goblin -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "goblin",
  "name": "Goblin",
  "display_name": "goblin",
  "display_name_plural": "goblins",
  "family_id": "goblin",
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

An ID that doesn't exist returns a `404`.

The rules reference contains everything in the rules, including information that a player's character wouldn't know in play, such as the effect of a cursed magic item. To read what a party has learned about an item during a game, use that session's [player view](play-a-game.md#read-the-player-view).

## Read a command's JSON schema

You play a *session*, a single game in progress, by sending commands to `POST /v1/sessions/{session_id}/commands`. `GET /v1/rules/commands` lists every command with these fields:

- `command_type`: The command's name.
- `kind`: `play` for a command that a player may send, or `referee` for a command that only a referee may send.
- `allowed_modes`: The session modes in which the command is legal.
- `summary`: A one-line description of the command.

The server has more commands than the default `limit` of 50, so request `limit=200` to get all of them in one page. This request lists the play commands that are legal in a battle:

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

The rules list shows the modes in which each command is legal in any session. To see which commands a particular session accepts now, call `GET /v1/sessions/{session_id}/commands`, which also omits the commands that your role may not send.

A command's detail endpoint adds `json_schema`, the JSON Schema that describes the command's request body:

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
    "$defs": {"Direction": {"description": "The four grid directions the party faces and moves in. ...", "enum": ["north", "east", "south", "west"], "title": "Direction", "type": "string"}},
    "description": "Move the party one cell, turning to face the way it goes.\n\nThe party must already be in a dungeon. ...",
    "properties": {
      "command_type": {"const": "move_party", "default": "move_party", "description": "The name that identifies the command class in serialized data. ...", "title": "Command Type", "type": "string"},
      "source": {...},
      "direction": {"$ref": "#/$defs/Direction", "description": "Which way to step. The party turns to face that way as it goes, so a move is also a turn. ..."}
    },
    "required": ["direction"],
    "title": "MoveParty",
    "type": "object"
  }
}
```

Each part of the schema tells you something about the request body to build:

- **`command_type`:** Send the value in the property's `const`.
- **`required`:** Send every field that `required` lists. Omit any other field to use its default.
- **`$ref`:** Follow a `$ref` to its definition in `$defs`. In this schema, `direction` is one of the four directions in the `Direction` definition's `enum`.
- **The schema's `description`:** Read it for the rules of the command. It covers the modes the command works in, the `rejections` it can return, and the events it causes.
- **Each property's `description`:** Read it for the field's meaning and the values the field accepts.

The descriptions come from osrlib's Python documentation, and one of them doesn't apply to an HTTP request. The `command_type` description says that you never pass the field yourself, because constructing a command in Python sets it. A request body, however, must include `command_type`, and a body without it returns `422 validation_error`.

A command's `source` field is reserved for referees. A request with a player key that sets `source` returns `403 referee_required`.

A game can generate its forms from the schemas, or it can hard-code the few commands it uses and read a schema when it adds a command. The OpenAPI document at `/openapi.json` also describes every command, in the request body of `POST /v1/sessions/{session_id}/commands`.

## Roll dice

`POST /v1/dice/roll` rolls a dice expression such as `3d6`, `d%`, `1d6+1`, or `2d6+1x10`. An expression has these parts, in order:

- **Dice:** The number of dice, `d`, and the number of sides. The sides are 2, 3, 4, 6, 8, 10, 12, 20, or 100, and `d%` means `d100`. The number of dice defaults to 1.
- **Modifier (optional):** `+` or `-` and a number.
- **Multiplier (optional):** `x` and a number.

The server applies the modifier before the multiplier, so `2d6+1x10` is (2d6 + 1) × 10. The `times` field sets how many times the server rolls the expression, from 1 to 100. This request rolls `2d6+1x10` twice:

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

Each result contains the value of each die in `rolls`, the `modifier` and `multiplier` from the expression, and the `total`. An expression that the server can't parse, or one with an unsupported die size, returns `422 content_invalid`.

## Reproduce a result with a seed

A *seed* is a number that determines every random roll in a request. Every dice and generator request accepts an optional `seed`, an integer from 0 to 9,007,199,254,740,991 (2^53 - 1, the largest integer that a JavaScript client reads exactly). If you omit `seed`, the server generates a random one. Every response includes the seed the server used, and the same request with the same seed returns the same result. This request omits the seed:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"expression": "3d6"}'
```

```json
{"expression":"3d6","seed":2678468627358632,"rolls":[{"rolls":[1,3,3],"modifier":0,"multiplier":1,"total":7}]}
```

Your seed and roll differ from the ones shown. Send the same body with the seed from your response, and the server returns the same dice. With the seed above, the server rolls 1, 3, and 3 again. To show a roll again later, store its seed. A seed also fixes the sequence of repeated rolls: a request with `times` set to 3 returns the first three results of the same request with `times` set to 5.

The seeds on this page apply to requests that store nothing. A session has a separate seed, which the server generates at random unless a referee chooses one. The only response that contains a session's seed is a save's document, which only a referee can read. For more information, see [Run games as a referee](referee.md#start-a-repeatable-session).

## Generate treasure, monsters, rooms, and characters

The generators roll content under the same rules as a game, but they store nothing. Each generator accepts an optional `seed` and returns the seed it used. Use the generators for features such as a loot screen, a random encounter, or a character preview.

### Treasure

`POST /v1/generate/treasure` takes a treasure type from `A` to `V` and a `tier`, which determines the magic item table the server rolls on. Send `basic` for a party whose highest level is 1 to 3, and `expert` for a party whose highest level is 4 or above:

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

### Monsters

`POST /v1/generate/monsters` takes a monster ID from `/v1/rules/monsters` and a `count` from 1 to 100. The server rolls hit points for each monster separately, and each monster's `template` is its full rules entry:

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

### Rooms

`POST /v1/generate/room` takes `level_number`, the number of a dungeon level, counting from 1. The server rolls the room's contents:

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

### NPC parties

`POST /v1/generate/npc-party` generates a party of non-player characters (NPCs), adventurers who aren't player characters. It takes a `kind`, either `basic` for levels 1 to 3 or `expert` for a party of higher levels, and a `count` of members from 1 to 100:

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

### Characters

`POST /v1/generate/character` takes the same choices as `POST /v1/characters`, except `ability_roll_id`. The server rolls the ability scores, so the same body with the same seed returns the same character:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/generate/character -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Garr", "class_id": "dwarf", "alignment": "lawful", "seed": 7}'
```

```json
{
  "seed": 7,
  "character": {"id": null, "name": "Garr", "class_id": "dwarf", "custom_class": null, "race": "dwarf", "level": 1, "scores": {"str": 14, "int": 6, "wis": 9, "dex": 10, "con": 11, "cha": 11}, "max_hp": 3, ...},
  "derived": {"thac0": 19, "armour_class": 9, ...},
  "rolls": {"ability_rolls": {...}, "hit_point_roll": {...}, "gold_roll": {...}}
}
```

`rolls` contains the dice behind the character: the ability scores, the hit points, and the starting gold. The server doesn't store a generated character, so a session can't use it. To create a character that a session can play, use `POST /v1/characters`.

## Next steps

- [Play a game](play-a-game.md): use the commands in a game.
- [Errors](errors.md): what a `422` on these endpoints means.
- [Run games as a referee](referee.md): choose the seed of a session.
