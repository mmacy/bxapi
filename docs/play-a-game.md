# Play a game

Walk through a dungeon-crawl session (one game in progress) of `builtin-barrow`, the adventure included with bxapi, from rolling ability scores (the six core traits such as strength and dexterity) to saving and restoring a battle. A game client sends these same requests. Complete [Get it running](../README.md#get-it-running) first if you haven't installed bxapi yet.

## Start a server with a player key

1. Stop any running bxapi process and start a server configured with one `player` API key, which restricts responses to player-visible state:

   ```sh
   BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' uv run bxapi
   ```

2. In a second terminal, store the key in `KEY` and verify it against [`GET /v1/meta`](https://mmacy.github.io/bxapi/reference/endpoint/meta#operation-get_meta):

   ```sh
   export KEY=player-key-0123456789
   curl -sS -w '\n' http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $KEY"
   ```

   ```json
   {
     "api_version": "0.1.0",
     "engine_version": "2.7.0",
     "schema_version": 4,
     "auth_enabled": true,
     "client": "my-game",
     "role": "player",
     "storage": {"used_bytes": 0, "max_bytes": 1073741824}
   }
   ```

The output blocks below format and abbreviate the server's compact JSON with `...`. Because osrlib rolls ability scores, hit points (the damage a character can take), starting gold, and combat dice randomly, your numbers will differ from the examples.

## Roll ability scores and create a party

1. Roll six 3d6 ability scores with [`POST /v1/ability-rolls`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-create_ability_roll) to see which B/X classes the roll qualifies for. `rolls.rolls` lists the three d6 behind each score, for a client that animates the dice:

   ```sh
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/ability-rolls -H "Authorization: Bearer $KEY"
   ```

   ```json
   {
     "id": "rol_yQsuUWgqmzpnDA6Sm51kQA",
     "scores": {"str": 6, "int": 12, "wis": 17, "dex": 11, "con": 5, "cha": 11},
     "rolls": {
       "scores": {"str": 6, "int": 12, "wis": 17, "dex": 11, "con": 5, "cha": 11},
       "rolls": {"str": [1, 3, 2], "int": [4, 5, 3], "wis": [6, 5, 6], "dex": [2, 4, 5], "con": [1, 1, 3], "cha": [3, 6, 2]}
     },
     "eligible_class_ids": ["cleric", "elf", "fighter", "magic_user", "thief"],
     "used": false,
     "created_at": "2026-11-21T05:24:33Z"
   }
   ```

2. Replace `rol_yQsuUWgqmzpnDA6Sm51kQA` with the `id` from your response and export `ROLL`:

   ```sh
   export ROLL=rol_yQsuUWgqmzpnDA6Sm51kQA
   ```

3. Preview a fighter from that roll with [`POST /v1/ability-rolls/{roll_id}/preview`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-preview_character) to see the hit points and starting gold before the player shops. The server stores nothing and leaves `$ROLL` unused:

   ```sh
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/ability-rolls/$ROLL/preview -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}'
   ```

   ```json
   {
     "character": {"name": "Brenna", "scores": {...}, "max_hp": 3, "current_hp": 3, "inventory": {"purse": {"gp": 90, ...}, ...}, ...},
     "derived": {"thac0": 19, "armour_class": 9, ...},
     "rolls": {
       "ability_rolls": {...},
       "hit_point_roll": {"rolls": [5], "hit_points": 3},
       "gold_roll": {"rolls": [3, 2, 4], "modifier": 0, "multiplier": 10, "total": 90}
     }
   }
   ```

   The purchases, the equipment, and every other choice except the class and `hp_reroll_at_first_level` leave `hit_point_roll` and `gold_roll` as they are. So you can show the player 90 gp, let them shop, and preview again to check the purchases before you create the character.

4. Create a fighter from that roll with [`POST /v1/characters`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-create_character), buying and equipping leather armour (20 gp) and a sword (10 gp) from [`GET /v1/rules/equipment`](https://mmacy.github.io/bxapi/reference/endpoint/rules#operation-list_equipment):

   ```sh
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"name\": \"Brenna\", \"class_id\": \"fighter\", \"alignment\": \"lawful\", \"ability_roll_id\": \"$ROLL\", \"purchases\": [{\"item_id\": \"leather\"}, {\"item_id\": \"sword\"}], \"equip_item_ids\": [\"leather\", \"sword\"]}"
   ```

   ```json
   {
     "id": "chr_PUtflt1xPCTJIwhqMcAIrA",
     "name": "Brenna",
     "class_id": "fighter",
     "level": 1,
     "created_at": "2026-11-21T05:24:33Z",
     "character": {"name": "Brenna", "scores": {...}, "max_hp": 3, "current_hp": 3, "inventory": {...}, ...},
     "derived": {"thac0": 19, "attack_bonus": 0, "armour_class": 7, "armour_class_ascending": 12, "saves": {...}, ...}
   }
   ```

   Brenna has the 3 hit points that the preview showed, and 60 gp: the 90 gp from the preview less the 30 gp kit. Every starting gold roll (3d6 × 10 gp, from 30 to 180 gp) covers this 30 gp kit. If a player requests gear costing more than the rolled gold (such as `chainmail`, `shield`, and `sword` for 60 gp total), the server returns 422 `character_invalid` with `items.purchase.insufficient_funds` in `rejections` and leaves `$ROLL` unused with the same gold roll so the player can retry with cheaper gear. A preview with the same choices returns the same 422, so you can check the gear before you create the character. A player can also buy gear after the session starts, in `town` mode, with `purchase_equipment` and `equip_item`.

5. Create a cleric and a thief without `ability_roll_id` so the server rolls their scores inline:

   ```sh
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "purchases": [{"item_id": "mace"}, {"item_id": "leather"}], "equip_item_ids": ["mace", "leather"]}'
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Wren", "class_id": "thief", "alignment": "neutral", "purchases": [{"item_id": "dagger"}, {"item_id": "leather"}], "equip_item_ids": ["dagger", "leather"]}'
   ```

6. Replace the three `chr_` IDs below with the `id` values returned for Brenna, Tomas, and Wren:

   ```sh
   export BRENNA=chr_PUtflt1xPCTJIwhqMcAIrA
   export TOMAS=chr_kqRWmM-1d1lMQFy-4CFVnw
   export WREN=chr_8l3D6o0BvEL_V99x7NUK1w
   ```

## Start a session and enter the dungeon

1. Start a session of `builtin-barrow` with [`POST /v1/sessions`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-create_session), passing `character_ids` in marching order (front rank first):

   ```sh
   curl -sS -w '\n' -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"adventure_id\": \"builtin-barrow\", \"character_ids\": [\"$BRENNA\", \"$TOMAS\", \"$WREN\"]}"
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
     "engine_version": "2.7.0",
     "created_at": "2026-11-21T05:24:33Z",
     "updated_at": "2026-11-21T05:24:33Z"
   }
   ```

   The session copies each character and the adventure at start, so play never modifies your stored characters.

2. Replace `ses_Bq2lhAvBgBLVtCRWuWkVUQ` with the `id` from your response and export `SESSION`:

   ```sh
   export SESSION=ses_Bq2lhAvBgBLVtCRWuWkVUQ
   ```

3. List the commands legal in `town` mode with [`GET /v1/sessions/{session_id}/commands`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-list_session_commands):

   ```sh
   curl -sS -w '\n' "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY"
   ```

   ```json
   {
     "mode": "town",
     "revision": 0,
     "commands": [
       {"command_type": "reorder_party", "kind": "play"},
       {"command_type": "give_items", "kind": "play"},
       ...
       {"command_type": "enter_dungeon", "kind": "play"}
     ]
   }
   ```

   To inspect the required fields for any `command_type`, check [`POST /v1/sessions/{session_id}/commands`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-send_command) or fetch its JSON Schema from [`GET /v1/rules/commands/{command_type}`](https://mmacy.github.io/bxapi/reference/endpoint/rules#operation-get_command).

4. Enter the `barrow` dungeon:

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "enter_dungeon", "dungeon_id": "barrow"}'
   ```

   ```json
   {
     "accepted": true,
     "rejections": [],
     "events": [...],
     "messages": [
       "The party enters dungeon barrow (level 1).",
       "A new quest: The Jade Idol. The temple wants the Jade Idol off the barrow king's altar and back on its own."
     ],
     "mode": "exploring",
     "ended": false,
     "revision": 1
   }
   ```

   `mode` switches to `exploring` and `revision` increments to `1`. Inside a session, osrlib assigns party members sequential IDs in marching order (`character-0001` for Brenna, `character-0002` for Tomas, and `character-0003` for Wren).

## Move and trigger an encounter

1. Try moving `north` into a wall:

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "north"}'
   ```

   ```json
   {"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
   ```

   Because the HTTP request itself is valid, the server returns `200 OK` with `accepted` set to `false`, osrlib's `rejections`, and `revision` unchanged at `1` (see [Refused commands](https://mmacy.github.io/bxapi/reference/#refused-commands)).

2. Step `east` into `(1, 0)`, then step `east` again with `include=view` to enter the guard room and receive the updated player view in the same response:

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
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
     "revision": 3,
     "view": {
       "party": [
         {"id": "character-0001", "name": "Brenna", "current_hp": 3, "max_hp": 3, ...},
         {"id": "character-0002", "name": "Tomas", "current_hp": 4, "max_hp": 4, ...},
         {"id": "character-0003", "name": "Wren", "current_hp": 3, "max_hp": 3, ...}
       ],
       "location": {"kind": "dungeon", "dungeon_id": "barrow", "level_number": 1, "position": [2, 0], "facing": "east"},
       "explored": [{"dungeon_id": "barrow", "level_number": 1, "cells": [[0, 0], [1, 0], [2, 0]], "edges": {...}}],
       "encounter": {
         "groups": [{"id": "group-0001", "label": "goblin", "label_plural": "goblins", "family_id": "goblin", "count": 2, "distance_feet": 20, "fled": false, ...}],
         "stance": "hostile",
         "in_battle": false,
         "declarers": ["character-0001", "character-0002", "character-0003"],
         "front_rank": ["character-0001", "character-0002"],
         "formation_width": 2,
         ...
       },
       ...
     }
   }
   ```

   You can also fetch the player view separately at any time with [`GET /v1/sessions/{session_id}/view`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-get_player_view). Check `mode` in your response before continuing: most runs enter `encounter` mode (where the party can `parley`, `evade`, `engage_battle`, or `wait`), whereas an immediate monster attack or surprise roll can transition directly into `battle` mode (or even end a short fight).

## Fight a battle round

1. If your session is in `encounter` mode, start combat with `engage_battle` and pass `include=view` to inspect `view.encounter` (if your session is already in `battle` mode, skip to step 2, or skip to [Save and restore](#save-and-restore) if surprise already ended combat):

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle"}'
   ```

2. In `battle` mode, `resolve_battle_round` requires one declaration for every character in `view.encounter.declarers`, and only characters in `view.encounter.front_rank` can make melee attacks. When all three party members are still in `declarers` and `front_rank` contains only `character-0001` and `character-0002` (corridor width `2`), osrlib refuses a melee attack from `character-0003` (`Wren`) with `battle.declaration.not_in_front_rank`:

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "attack", "target_group_id": "group-0001", "move": "close"}]}'
   ```

   ```json
   {"accepted":false,"rejections":[{"code":"battle.declaration.not_in_front_rank","params":{"character":"character-0003"}}],"events":[],"messages":[],"mode":"battle","ended":false,"revision":4}
   ```

   To learn which declaration osrlib would refuse before you send the round, send the same declarations to [`POST /v1/sessions/{session_id}/battle-round/preview`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-preview_battle_round). The preview changes nothing in the session, and its `rejections` lists each member's refusals under that member's id:

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/battle-round/preview" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "attack", "target_group_id": "group-0001", "move": "close"}]}'
   ```

3. Submit one valid declaration per character in your current `view.encounter.declarers` (if a surprise round already closed the gap to 5 feet or knocked out a character, omit `"move": "close"` or drop that character's entry to match `view.encounter`):

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "hold"}]}'
   ```

   ```json
   {
     "accepted": true,
     "rejections": [],
     "events": [...],
     "messages": [
       "Battle round 1 begins.",
       "Initiative (side): party 3, monsters 1. Order: character-0001, character-0002, character-0003, group-0001.",
       "character-0001 closes to 5' from group-0001.",
       "character-0002 closes to 5' from group-0001.",
       ...
     ],
     "mode": "battle",
     "ended": false,
     "revision": 5,
     "view": {...}
   }
   ```

   If any party member drops to `0` hit points, they gain the `dead` condition and drop out of the next round's `declarers`, so always read `view.encounter.declarers` before building the next round's declarations. If a monster group fails a morale check (`battle.side.fled`), it retreats each round, but `fled` stays `false` until it is more than 120 feet from every living party member. Once `fled` becomes `true`, osrlib refuses declarations targeting that group with `battle.declaration.unknown_group`.

## Save and restore

1. Snapshot the session with [`POST /v1/sessions/{session_id}/saves`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-save_session):

   ```sh
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/saves" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"label": "After round 1"}'
   ```

   ```json
   {
     "id": "sav_E6NKZNiBl4c2jL_rkyY8Bg",
     "session_id": "ses_Bq2lhAvBgBLVtCRWuWkVUQ",
     "adventure_id": "builtin-barrow",
     "adventure_name": "The Barrow of the Forgotten King",
     "label": "After round 1",
     "mode": "battle",
     "clock": {"rounds": 121, "turns": 2, "days": 0},
     "created_at": "2026-11-21T05:24:33Z"
   }
   ```

   `clock` tracks total in-game time since the adventure began (two 10-minute dungeon exploration turns equal 120 10-second rounds, plus 1 combat round), which is separate from the session's HTTP `revision`.

2. Replace `sav_E6NKZNiBl4c2jL_rkyY8Bg` with your save's `id` and branch a new session from that checkpoint with [`POST /v1/saves/{save_id}/restore`](https://mmacy.github.io/bxapi/reference/endpoint/saves#operation-restore_save):

   ```sh
   export SAVE=sav_E6NKZNiBl4c2jL_rkyY8Bg
   curl -sS -w '\n' -X POST "http://127.0.0.1:8640/v1/saves/$SAVE/restore" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Second try"}'
   ```

   Restoring returns a new `ses_` ID at `revision` `0` while leaving both the original `$SESSION` and `$SAVE` untouched. Export the returned `id` to `SESSION` before sending commands to the restored game.

Read [Build a client](client-guide.md) next for key topologies, safe retries (`Idempotency-Key` and `expected_revision`), `ETag` polling, custom adventures, and referee tools, or inspect [`examples/play_barrow.py`](../examples/README.md) to see this loop automated in Python.
