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

The output blocks below format and abbreviate the server's compact JSON with `...`. Because osrlib rolls ability scores, hit points (the damage a character can take), starting gold, and combat dice randomly, your numbers will differ from the examples.

## Roll ability scores and create a party

1. Roll six 3d6 ability scores with [`POST /v1/ability-rolls`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-create_ability_roll) to see which B/X classes the roll qualifies for:

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

2. Replace `rol_yQsuUWgqmzpnDA6Sm51kQA` with the `id` from your response and export `ROLL`:

   ```sh
   export ROLL=rol_yQsuUWgqmzpnDA6Sm51kQA
   ```

3. Create a fighter from that roll with [`POST /v1/characters`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-create_character), buying and equipping starting gear from [`GET /v1/rules/equipment`](https://mmacy.github.io/bxapi/reference/endpoint/rules#operation-list_equipment):

   ```sh
   curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"name\": \"Brenna\", \"class_id\": \"fighter\", \"alignment\": \"lawful\", \"ability_roll_id\": \"$ROLL\", \"purchases\": [{\"item_id\": \"chainmail\"}, {\"item_id\": \"shield\"}, {\"item_id\": \"sword\"}], \"equip_item_ids\": [\"chainmail\", \"shield\", \"sword\"]}"
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

   If the rolled starting gold (3d6 × 10 gp) falls short of the 65 gp total, the server returns 422 `character_invalid` with `items.purchase.insufficient_funds` in `rejections` and leaves `$ROLL` unused: retry with `"leather"` instead of `"chainmail"`. To show rolled gold to a player before shopping, omit `purchases` and `equip_item_ids`, read `character.inventory.purse.gp` from the response, and buy gear after starting the session in `town` mode with the `purchase_equipment` and `equip_item` commands.

4. Create a cleric and a thief without `ability_roll_id` so the server rolls their scores inline:

   ```sh
   curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "purchases": [{"item_id": "mace"}, {"item_id": "leather"}], "equip_item_ids": ["mace", "leather"]}'
   curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Wren", "class_id": "thief", "alignment": "neutral", "purchases": [{"item_id": "dagger"}, {"item_id": "leather"}], "equip_item_ids": ["dagger", "leather"]}'
   ```

5. Replace the three `chr_` IDs below with the `id` values returned for Brenna, Tomas, and Wren:

   ```sh
   export BRENNA=chr_PUtflt1xPCTJIwhqMcAIrA
   export TOMAS=chr_kqRWmM-1d1lMQFy-4CFVnw
   export WREN=chr_8l3D6o0BvEL_V99x7NUK1w
   ```

## Start a session and enter the dungeon

1. Start a session of `builtin-barrow` with [`POST /v1/sessions`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-create_session), passing `character_ids` in marching order (front rank first):

   ```sh
   curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"adventure_id\": \"builtin-barrow\", \"character_ids\": [\"$BRENNA\", \"$TOMAS\", \"$WREN\"]}"
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

   The session copies each character and the adventure at start, so play never modifies your stored characters.

2. Replace `ses_Bq2lhAvBgBLVtCRWuWkVUQ` with the `id` from your response and export `SESSION`:

   ```sh
   export SESSION=ses_Bq2lhAvBgBLVtCRWuWkVUQ
   ```

3. List the commands legal in `town` mode with [`GET /v1/sessions/{session_id}/commands`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-list_session_commands):

   ```sh
   curl -s "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY"
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
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "enter_dungeon", "dungeon_id": "barrow"}'
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
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "north"}'
   ```

   ```json
   {"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
   ```

   Because the HTTP request itself is valid, the server returns `200 OK` with `accepted` set to `false`, osrlib's `rejections`, and `revision` unchanged at `1` (see [Refused commands](https://mmacy.github.io/bxapi/reference/#refused-commands)).

2. Step `east` twice, adding `include=view` on the second move so the response includes the updated player view when the party enters the guard room:

   ```sh
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
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

   You can also fetch the player view separately at any time with [`GET /v1/sessions/{session_id}/view`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-get_player_view). In `encounter` mode, the party has met monsters (`group-0001` at 20 feet) and can `parley`, `evade`, `engage_battle`, or `wait`.

## Fight a battle round

1. Start combat with `engage_battle`:

   ```sh
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle"}'
   ```

   `mode` becomes `battle` at `revision` `4` (unless monster surprise resolved an immediate round or ended the fight).

2. In `battle` mode, `resolve_battle_round` requires one declaration for every character listed in `encounter.declarers`. Only characters in `encounter.front_rank` (`character-0001` and `character-0002` in this corridor of width `2`) can make melee attacks. osrlib refuses a melee attack from `character-0003` (`Wren`) with `battle.declaration.not_in_front_rank`:

   ```sh
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "attack", "target_group_id": "group-0001", "move": "close"}]}'
   ```

   ```json
   {"accepted":false,"rejections":[{"code":"battle.declaration.not_in_front_rank","params":{"character":"character-0003"}}],"events":[],"messages":[],"mode":"battle","ended":false,"revision":4}
   ```

3. Have Brenna and Tomas close the 20-foot gap (`"move": "close"`) to attack `group-0001` while Wren holds (`"action": "hold"`):

   ```sh
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/commands?include=view" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "resolve_battle_round", "declarations": [{"character_id": "character-0001", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0002", "action": "attack", "target_group_id": "group-0001", "move": "close"}, {"character_id": "character-0003", "action": "hold"}]}'
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
   curl -s -X POST "http://127.0.0.1:8640/v1/sessions/$SESSION/saves" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"label": "After round 1"}'
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

2. Replace `sav_E6NKZNiBl4c2jL_rkyY8Bg` with your save's `id` and branch a new session from that checkpoint with [`POST /v1/saves/{save_id}/restore`](https://mmacy.github.io/bxapi/reference/endpoint/saves#operation-restore_save):

   ```sh
   export SAVE=sav_E6NKZNiBl4c2jL_rkyY8Bg
   curl -s -X POST "http://127.0.0.1:8640/v1/saves/$SAVE/restore" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Second try"}'
   ```

   Restoring starts an independent session at `revision` `0` while leaving both the original session and the save untouched.

Read [Build a client](client-guide.md) next for key topologies, safe retries (`Idempotency-Key` and `expected_revision`), `ETag` polling, custom adventures, and referee tools, or inspect [`examples/play_barrow.py`](../examples/README.md) to see this loop automated in Python.
