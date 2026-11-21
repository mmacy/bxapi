# Adventures

Upload your own adventure so a session can play it, and check an adventure document before you store it.

An *adventure* is the content that a *session*, one game in progress, plays: a town, dungeons with maps, monsters and treasure, and the triggers and quests that drive them. An *adventure document* is an adventure written as JSON. An adventure can also bundle spells of its own in `spells`, and set what its town's temple charges for each service in `town.healing_prices`.

An adventure can bundle monsters of its own, too. A bundled monster that has no `family_id` gets its template id as its `family_id`, and the player view shows every group's `family_id` to the players. So don't give a monster an id that tells the players what it really is, or set `family_id` to what the players should see.

You don't need to write a document by hand. Two separate projects by osrlib's author write adventure documents: [osr-forge](https://github.com/mmacy/osr-forge) converts a tabletop adventure module PDF into a document, and [osr-editor](https://github.com/mmacy/osr-editor) is a graphical editor for creating and changing one. Both write the `adventure.json` file that bxapi accepts as a request body. A program that uses [osrlib](https://mmacy.github.io/osrlib-python/), the Python rules engine that bxapi runs, can also write one.

The document has four members:

- `kind` is `adventure`.
- `schema_version` is the version of the document format.
- `engine_version` is the release of the rules engine that wrote the document.
- `payload` is the adventure itself, in the format of osrlib's `Adventure` model.

The server comes with one adventure, `builtin-barrow`, so a new *client*, an application that the server identifies by its API keys, can start a game without uploading anything. Its id starts with `builtin-`. Every client can list it and start a session with it, and no client can delete it.

Start the server with a player key and a referee key for the same client, as [Authentication](authentication.md#configure-api-keys) shows. Each example sends a key in the shell variable `KEY`. A player key can upload, validate, list, and delete adventures. To follow along, you need a document. You can read the built-in adventure's document with the referee key, a key with the *referee* role, which is for software that runs games and may see their hidden state, as [Run games as a referee](referee.md) describes. Save the document as `barrow.json`:

```sh
export KEY=referee-key-0123456789
```

```sh
curl -s http://127.0.0.1:8640/v1/adventures/builtin-barrow/document -H "Authorization: Bearer $KEY" -o barrow.json
```

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

Send a document to `POST /v1/adventures/validate` to check it without storing it. The server runs every check that the rules engine makes on an adventure. It checks the document's structure, and every reference the adventure makes to a monster, an item, an area, or a quest. It also checks for content that a session couldn't play, like a treasure type letter outside `A` to `V`, a key in a level's `edges` that doesn't match a side of a cell on the grid, or an area whose cells all belong to areas listed before it. After you upload an adventure that passes, a session can play it on this server's osrlib release. A later release can add checks, and then `POST /v1/sessions` gets 422 `content_invalid` for a stored adventure that fails one. Upload a fixed document to play it again.

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

The response is 200 whether or not the document is valid. When it's valid, `adventure` is the adventure's info, described below, with `id` and `created_at` set to `null` because nothing was stored.

The built-in adventure has goblins in its guard room. To see a document that fails, change the goblins' monster id in a copy of the file to one that doesn't exist:

```sh
sed 's/"goblin"/"gobbin"/' barrow.json > broken.json
```

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

`code` is the error code that an upload of the same document returns, and `detail` describes what's wrong. A document with a newer `schema_version` than the server's rules engine reads gets `version_unsupported` instead. The `detail` is one string that lists at most 20 problems, one on each line.

## Upload an adventure

`POST /v1/adventures` runs the same checks and stores the document. Change the adventure's name in a copy of the file, so you can tell your adventure from the built-in one, and upload it:

```sh
sed 's/The Barrow of the Forgotten King/The Barrow, second edition/' barrow.json > mine.json
```

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

The status is 201. The server stores the document as you sent it, and returns the adventure's info with the new `id`. Your id differs. A document that fails a check gets 422 `content_invalid`, the same `detail` as validation, and the server stores nothing:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/adventures -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @broken.json
```

```text
HTTP/1.1 422 Unprocessable Content
...
{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"adventure validation failed:\nbarrow level 1: area 'guard_room' references unknown monster 'gobbin'","code":"content_invalid"}
```

Each upload stores a new adventure, even when the document is one you uploaded before. The body can be up to 8 MiB by default. See [Configuration](configuration.md) to change the limit.

## Read an adventure's info

`GET /v1/adventures` lists the adventures a client can play: the ones that come with the server first, then your client's, newest first. `GET /v1/adventures/{adventure_id}` returns one. Both return the adventure's *info*:

| Field | Meaning |
| --- | --- |
| `id` | The adventure's id. `builtin-` and a name for an adventure that comes with the server, and `adv_` and 22 random characters for one you uploaded. |
| `name` | The adventure's title. |
| `builtin` | Whether the adventure comes with the server. |
| `created_at` | When your client stored the adventure. `null` for a built-in adventure. |
| `description` | The text the author wrote to describe the adventure. |
| `hooks` | The reasons the author gives for a party to take the adventure on. |
| `party` | The character levels and party size the adventure is written for, or `null` when the author gives none. |
| `town` | The name and description of the town where a session starts. |
| `dungeons` | The `id` and `name` of each dungeon. A session's `enter_dungeon` command takes a dungeon's `id`. |

The info contains only what a player may know before play, whatever the key's role. It leaves out the maps, monsters, treasure, triggers, and quests, and it doesn't say how many levels a dungeon has, because the player view shows only the levels a party has explored. A game that lists adventures for its players can show the info as it is. In the commands below, replace the adventure's id with the one your upload returned.

```sh
curl -s http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw -H "Authorization: Bearer $KEY"
```

The response is the same info that the upload returned. The full document is for referees: `GET /v1/adventures/{adventure_id}/document` returns it as it was uploaded, and a player who asks gets 403 `referee_required`:

```sh
curl -si http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw/document -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required"}
```

## Play an uploaded adventure

Start a session with the adventure's id and the id of a character you created, as [Play a game](play-a-game.md#create-a-character) shows. The rest of play is the same as with the built-in adventure:

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

Delete an adventure you uploaded:

```sh
curl -si -X DELETE http://127.0.0.1:8640/v1/adventures/adv_WGPW5ae_3LkxU1F45n24Aw -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 204 No Content
...
```

A session that already started with the adventure keeps playing, because each session has a copy of its adventure. After the delete, a request for the adventure gets 404 `not_found`, and so does a request to start a session with it. An attempt to delete a built-in adventure gets 403 `adventure_read_only`:

```sh
curl -si -X DELETE http://127.0.0.1:8640/v1/adventures/builtin-barrow -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"The adventure 'builtin-barrow' comes with the server.","code":"adventure_read_only"}
```

## Next steps

- [Play a game](play-a-game.md): start a session with an adventure.
- [Run games as a referee](referee.md): read the full document of an adventure.
- [Errors](errors.md): every error code, including `content_invalid` and `version_unsupported`.
