# Adventures

Upload your own adventure so that a session can play it, and validate an adventure document before you store it. An *adventure* is the content that a *session*, a single game in progress, plays: a town, dungeons with maps, monsters and treasure, and the triggers and quests that drive them.

The server includes one built-in adventure, `builtin-barrow`, so a new *client*, an application that the server identifies by its API keys, can start a game without uploading anything. The ID of a built-in adventure starts with `builtin-`. Every client can list `builtin-barrow` and start a session with it, but no client can delete it.

## Adventure documents

An *adventure document* is an adventure written as JSON. The document has four fields:

- `kind`: Always `adventure`.
- `schema_version`: The version of the document format.
- `engine_version`: The release of the rules engine that wrote the document.
- `payload`: The adventure itself, in the format of osrlib's `Adventure` model.

An adventure can bundle its own spells in `spells` and its own monsters, and it can set the price that its town's temple charges for each service in `town.healing_prices`.

Choose the IDs of bundled monsters with care, because a monster's ID can reach the players. The player view shows every monster group's `family_id`, and a bundled monster that has no `family_id` uses its template ID as its `family_id`. A template ID that names what a monster really is therefore reveals it to the players. Either give the monster an ID that reveals nothing, or set its `family_id` to the value the players should see.

## Create a document

You don't need to write a document by hand. Two separate projects by osrlib's author write the `adventure.json` file that bxapi accepts as a request body, and your own program can write a document with osrlib:

- [osr-forge](https://github.com/mmacy/osr-forge): Converts a tabletop adventure module PDF into a document.
- [osr-editor](https://github.com/mmacy/osr-editor): A graphical editor for creating and changing a document.
- [osrlib](https://mmacy.github.io/osrlib-python/): The Python rules engine that bxapi runs. A program that uses osrlib can write a document.

## Before you begin

Start the server with a player key and a referee key for the same client, as [Authentication](authentication.md#configure-api-keys) shows. Each example sends the key in the shell variable `KEY`. A player key can upload, validate, list, and delete adventures.

To follow along, you need a document. The built-in adventure's document works, but reading it requires a referee key, a key with the *referee* role. That role is for software that runs games and may see their hidden state, as [Run games as a referee](referee.md) describes. Set the referee key, then save the document as `barrow.json`:

```sh
export KEY=referee-key-0123456789
```

```sh
curl -s http://127.0.0.1:8640/v1/adventures/builtin-barrow/document -H "Authorization: Bearer $KEY" -o barrow.json
```

Print the first 200 bytes of the file to confirm the download:

```sh
head -c 200 barrow.json
```

```text
{"kind":"adventure","schema_version":4,"engine_version":"2.4.0","payload":{"name":"The Barrow of the Forgotten King","description":"A plundered barrow outside town, and the idol the temple wants back.
```

The rest of this page uses a player key:

```sh
export KEY=player-key-0123456789
```

## Validate a document

Send a document to `POST /v1/adventures/validate` to check it without storing it. The server runs every check that the rules engine makes on an adventure:

- **Structure:** The document has the structure of an adventure document.
- **References:** Every monster, item, area, and quest that the adventure refers to exists.
- **Playability:** The adventure has no content that a session couldn't play, such as a treasure type letter outside `A` to `V`, a key in a level's `edges` that doesn't match a side of a cell on the grid, or an area whose cells all belong to areas listed before it.

After you upload an adventure that passes, a session can play it on this server's osrlib release. A later osrlib release might add checks. If a stored adventure fails a new check, `POST /v1/sessions` returns `422 content_invalid` for it, and you upload a corrected document to play the adventure again.

This request validates `barrow.json`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/adventures/validate -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @barrow.json
```

```json
{
  "valid": true,
  "adventure": {
    "name": "The Barrow of the Forgotten King",
    "description": "A plundered barrow outside town, and the idol the temple wants back.",
    "hooks": ["The temple pays 200 gp for the Jade Idol's return."],
    "party": null,
    "town": {"name": "Threshold", "description": "A walled market town at the edge of the moors."},
    "dungeons": [{"id": "barrow", "name": "The Barrow"}],
    "id": null,
    "builtin": false,
    "created_at": null
  }
}
```

The response status is `200` whether or not the document is valid. For a valid document, `adventure` contains the adventure's info, which [Read an adventure's info](#read-an-adventures-info) describes, with `id` and `created_at` set to `null` because the server stored nothing.

To see a document fail validation, break a reference. The built-in adventure has goblins in its guard room, so copy the file and change the goblins' monster ID to one that doesn't exist:

```sh
sed 's/"goblin"/"gobbin"/' barrow.json > broken.json
```

Then validate the broken copy:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/adventures/validate -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @broken.json
```

```json
{
  "valid": false,
  "code": "content_invalid",
  "detail": "adventure validation failed:\nbarrow level 1: area 'guard_room' references unknown monster 'gobbin'"
}
```

`code` is the error code that an upload of the same document returns, and `detail` is a single string that lists at most 20 problems, one per line. A document whose `schema_version` is newer than the server's rules engine can read returns `version_unsupported` instead.

## Upload an adventure

`POST /v1/adventures` runs the same checks and stores the document. To tell your adventure apart from the built-in one, change the adventure's name in a copy of the file:

```sh
sed 's/The Barrow of the Forgotten King/The Barrow, second edition/' barrow.json > mine.json
```

Then upload the copy:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/adventures -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @mine.json
```

```json
{
  "name": "The Barrow, second edition",
  "description": "A plundered barrow outside town, and the idol the temple wants back.",
  "hooks": ["The temple pays 200 gp for the Jade Idol's return."],
  "party": null,
  "town": {"name": "Threshold", "description": "A walled market town at the edge of the moors."},
  "dungeons": [{"id": "barrow", "name": "The Barrow"}],
  "id": "adv_WGPW5ae_3LkxU1F45n24Aw",
  "builtin": false,
  "created_at": "2026-11-21T05:24:33Z"
}
```

The response status is `201`. The server stores the document exactly as you sent it and returns the adventure's info with the new `id`, which differs from the one shown. A document that fails a check returns `422 content_invalid` with the same `detail` as validation, and the server stores nothing:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/adventures -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @broken.json
```

```text
HTTP/1.1 422 Unprocessable Content
...
{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"adventure validation failed:\nbarrow level 1: area 'guard_room' references unknown monster 'gobbin'","code":"content_invalid","request_id":"req_afuWebB7a8-GKDxXczaG9Q"}
```

Each upload stores a new adventure, even if you uploaded the same document before. The request body can be up to 8 MiB by default. To change the limit, see [Configuration](configuration.md).

## Read an adventure's info

`GET /v1/adventures` lists the adventures that a client can play: the built-in adventures first, then your client's adventures, newest first. `GET /v1/adventures/{adventure_id}` returns a single adventure. Both endpoints return the adventure's *info*, which has these fields:

| Field | Meaning |
| --- | --- |
| `id` | The adventure's ID: `builtin-` and a name for a built-in adventure, or `adv_` and 22 random characters for an adventure you uploaded. |
| `name` | The adventure's title. |
| `builtin` | Whether the adventure is built in. |
| `created_at` | When your client stored the adventure, or `null` for a built-in adventure. |
| `description` | The author's description of the adventure. |
| `hooks` | The author's reasons for a party to take on the adventure. |
| `party` | The character levels and party size the adventure is written for, or `null` if the author gives none. |
| `town` | The name and description of the town where a session starts. |
| `dungeons` | The `id` and `name` of each dungeon. A session's `enter_dungeon` command takes a dungeon's `id`. |

The info contains only what a player may know before play, whatever the key's role. It omits the maps, monsters, treasure, triggers, and quests. It also omits the number of levels in a dungeon, because the player view shows only the levels that a party has explored. A game that lists adventures for its players can therefore show the info unchanged.

This request reads the info of the adventure you uploaded. In this command and the ones that follow, replace the adventure's ID with the one your upload returned:

```sh
curl -s http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw -H "Authorization: Bearer $KEY"
```

The response is the same info that the upload returned. The full document is reserved for referees. `GET /v1/adventures/{adventure_id}/document` returns the document as it was uploaded, and a request with a player key returns `403 referee_required`:

```sh
curl -si http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw/document -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required","request_id":"req_xHBXV9VUPmxOEm1Pa1egfw"}
```

## Play an uploaded adventure

Start a session with the adventure's ID and the ID of a character you created, as [Play a game](play-a-game.md#create-a-character) shows. Play then works the same way as with the built-in adventure:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"adventure_id": "adv_WGPW5ae_3LkxU1F45n24Aw", "character_ids": ["chr_PUtflt1xPCTJIwhqMcAIrA"]}'
```

```json
{
  "id": "ses_XsUOF5owj4gNTroX5QZnwA",
  "name": "The Barrow, second edition",
  "adventure_id": "adv_WGPW5ae_3LkxU1F45n24Aw",
  "adventure_name": "The Barrow, second edition",
  "mode": "town",
  ...
}
```

## Delete an adventure

Send a `DELETE` request to remove an adventure you uploaded:

```sh
curl -si -X DELETE http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 204 No Content
...
```

A session that already started with the adventure keeps playing, because each session has its own copy of the adventure. After the deletion, a request for the adventure returns `404 not_found`, and so does a request to start a session with it. An attempt to delete a built-in adventure returns `403 adventure_read_only`:

```sh
curl -si -X DELETE http://127.0.0.1:8640/v1/adventures/builtin-barrow -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"The adventure 'builtin-barrow' comes with the server.","code":"adventure_read_only","request_id":"req_mbGvc16Ft6ylytZ6JL3GVw"}
```

## Next steps

- [Play a game](play-a-game.md): start a session with an adventure.
- [Run games as a referee](referee.md): read the full document of an adventure.
- [Errors](errors.md): every error code, including `content_invalid` and `version_unsupported`.
