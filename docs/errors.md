# Errors

Read an error response, decide how your client should handle each error code, and recover from the three state conflicts that need more than a retry.

The examples use a player key, a key with the `player` role, in the shell variable `KEY`, which [Play a game](play-a-game.md#start-a-server-with-a-player-key) sets up. [Authentication](authentication.md) covers keys and roles.

## Problem document format

Every error response has a `4xx` or `5xx` status and the media type `application/problem+json`. The body is a *problem document*, the standard JSON format for HTTP errors that RFC 9457 defines. Every problem document has these fields:

- `type`: Always `about:blank`.
- `title`: The standard name of the status.
- `status`: The HTTP status.
- `code`: A short, fixed name for the error. Have your client read `code` to decide what to do.
- `detail`: A sentence written for a person, at most 1,000 characters. Show it to a developer or write it to a log, but don't match on its text, because the wording can change.
- `request_id`: The ID that the server assigned to the request. The `X-Request-Id` response header has the same ID.

Some codes add fields, such as `revision` or `resource`, which the [error code table](#error-codes) lists. Have your client ignore any field it doesn't recognize.

Request a character that doesn't exist to see a problem document. The `-i` option makes `curl` print the status line and headers:

```sh
curl -si http://127.0.0.1:8640/v1/characters/chr_doesnotexist -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 404 Not Found
...
content-type: application/problem+json

{"type":"about:blank","title":"Not Found","status":404,"detail":"No character has the id 'chr_doesnotexist'.","code":"not_found","request_id":"req_C1tg5yjlBBxBDofQPlRXoQ","resource":"character","id":"chr_doesnotexist"}
```

## Request IDs

Every response, successful or not, has an `X-Request-Id` header, and the server logs that ID with the request. Have your client log the `request_id` of each problem document, and include the ID when you report an error to the server operator, so they can find the request in the server's log. For a `500 internal_error`, the server logs the cause with the same ID.

To trace one request through your own systems and the server's log, choose the ID yourself. Send an `X-Request-Id` header of 1 to 128 characters, made up of letters, digits, and the characters `.`, `_`, `~`, and `-`:

```sh
curl -si http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $KEY" -H "X-Request-Id: checkout-7f3a"
```

```text
HTTP/1.1 200 OK
...
x-request-id: checkout-7f3a
```

The server replaces any other value with an ID that it generates, which starts with `req_`.

## Rejected commands versus errors

A game command that the rules reject isn't an error. The server returns status `200` with `accepted` set to `false` and the rules engine's `rejections`, and the *session*, the game in progress, doesn't change. A rejection is a normal part of play, such as walking into a wall:

```text
HTTP/1.1 200 OK
...
{"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
```

An error means that the server didn't do what you asked, for example because the request has no key, is malformed, or refers to something that doesn't exist. A command with an unknown `command_type` is an error:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "fly"}'
```

```text
HTTP/1.1 422 Unprocessable Content
...
content-type: application/problem+json

{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"The request failed validation. See `errors` for each field.","code":"validation_error","request_id":"req_G8f0xBMB3wTR7-DqjdMQjA","errors":[{"loc":["body"],"msg":"'fly' isn't a value that 'command_type' accepts.","type":"union_tag_invalid"}],"error_count":1}
```

Handle a rejection as a result of play: read `rejections` and tell the player. Handle an error as a bug in your client or as a reason to retry, as the [error code table](#error-codes) states for each code.

Character creation is the exception. `POST /v1/characters` and `POST /v1/generate/character` treat a broken rule as an error. A choice that breaks a rule, such as buying more than the character can afford, returns `422 character_invalid`, because the request asks the server to create a character and the server can't create one.

## Error codes

The table lists every error code with its cause, what your client should do about it, and the fields it adds to the problem document. Several codes share a status, so have your client decide on `code`.

| Status | Code | Cause | What to do | Extra fields |
| --- | --- | --- | --- | --- |
| `400` | `bad_request` | The server can't decode the body as text, or the body contains a value the server rejects: an integer of more than 4,300 digits, `NaN`, `Infinity`, a number too large for a 64-bit float, such as `1e400`, a string with half of a UTF-16 surrogate pair, such as `"\ud800"`, or arrays and objects nested more than 100 levels deep. | Read `detail`, which states the cause. Send JSON in UTF-8 without the value. Don't retry. | |
| `401` | `unauthorized` | The key is missing or unknown. | Check the key. See [Authentication](authentication.md#handle-a-401-response). | |
| `403` | `referee_required` | A request with a player key asked for something only a referee may do: a referee endpoint, a field such as `seed`, `source`, or `party_document`, or `visibility=referee`. | Use a referee key, or omit the field. | |
| `403` | `referee_command` | A request with a player key sent a referee command. | Use a referee key, or send a play command. | `command_type` |
| `403` | `adventure_read_only` | The request tried to delete a built-in adventure. | Don't delete a built-in adventure. | |
| `404` | `not_found` | No resource that belongs to your client has that ID, or no route has that path. | Check the ID. A resource that belongs to another client also returns a `404`. | `resource`, `id` |
| `405` | `method_not_allowed` | The path exists, but it doesn't support that HTTP method. | Fix the method. | |
| `408` | `request_timeout` | The request body didn't arrive within `BXAPI_BODY_TIMEOUT_SECONDS`. | Send the request again on a faster connection. | |
| `409` | `revision_conflict` | `expected_revision` doesn't match the session's revision. | Read the session, then decide whether to send the command again. See [`revision_conflict`](#revision_conflict). | `revision` |
| `409` | `session_busy` | The command had no effect, because another request used the session for more than 5 seconds or another server process wrote the session first. | If you sent `expected_revision`, read the session. Otherwise, send the command again. See [`session_busy`](#session_busy). | `revision` |
| `409` | `ability_roll_used` | A character was already created from that ability roll. | Roll again, or use the existing character. See [`ability_roll_used`](#ability_roll_used). | |
| `409` | `version_unsupported` | A document you uploaded or imported has a schema version newer than the server's osrlib reads. | Export the document in an older version, or run a newer server. Check `schema_version` in `GET /v1/meta`. | |
| `409` | `idempotency_key_in_progress` | The first request with the same `Idempotency-Key` is still running. The response has the header `Retry-After: 1`. | Wait the number of seconds in `Retry-After`, then send the request again. | |
| `409` | `idempotency_key_interrupted` | The server failed or stopped while it handled the first request with the same `Idempotency-Key`, and it has no record of whether that request took effect. | Read the resource that the request would have changed. If the request didn't take effect, send it again with a new key. On an overloaded server, the first request might still be running, so read the resource again before you send the new request. | |
| `413` | `content_too_large` | The request body exceeds the server's limit. The limit is `BXAPI_MAX_BODY_BYTES` for a request that takes a document, such as an adventure upload or a save import, and 65,536 bytes for any other request, such as a command. | Send a smaller body. | |
| `415` | `unsupported_media_type` | The request has a body, but its `Content-Type` header doesn't declare the body as JSON. The `-d` option of `curl` sends `application/x-www-form-urlencoded` unless you set the header. | Send the body as JSON with the header `Content-Type: application/json`. | |
| `421` | `misdirected_request` | The server doesn't require an API key, and your request's `Host` header isn't `localhost`, `127.0.0.1`, or `[::1]`. | Call the server as `localhost` or `127.0.0.1`, or ask the server operator to set API keys. See [Authentication](authentication.md#anonymous-mode-for-local-development). | |
| `422` | `validation_error` | The request doesn't match the endpoint's schema, or the body isn't valid JSON. | Read `errors`, then fix the request. | `errors`, `error_count` |
| `422` | `content_invalid` | The rules engine rejected a value or a document as malformed, such as a dice expression it can't parse or an adventure that refers to a monster that doesn't exist. | Read `detail`, then fix the value or the document. | |
| `422` | `character_invalid` | The choices for a new character break a rule, or name a class or an item that doesn't exist. | Show the player the `rejections` and let them choose again. | `rejections`, or `field`, `index`, and `id` |
| `422` | `party_invalid` | The characters named for a new session can't form a party: the rules reject the party, it has more than 20 members, or two members have the same ID. | Change the party. | |
| `422` | `idempotency_key_reused` | Your client already sent the `Idempotency-Key` with a different request. | Send each new request with a new key. | |
| `429` | `rate_limited` | Your client sent more requests in a minute than the server allows. The limit is 600 requests per minute unless the server operator changed `BXAPI_RATE_LIMIT_PER_MINUTE`. The response has the header `Retry-After`, and the request had no effect. | Wait the number of seconds in `Retry-After`, then send the request again. If the request had no valid key, fix the key. | |
| `500` | `internal_error` | The server has a bug. The response says nothing about the cause, which the server logs. | Report the error with the `request_id`. You can safely retry with the same `Idempotency-Key`, or retry a command with `expected_revision`. | |
| `500` | `session_unreadable` | The server can't load a stored session or save, for example because a server with a different osrlib release wrote it. The response has no detail from the stored state. | Don't retry. Report the error to the server operator with the `request_id`. | |
| `503` | `database_busy` | Another process held the database's write lock for longer than the 5 seconds that the server waits. The response has the header `Retry-After: 1`. | Wait the number of seconds in `Retry-After`, then send the request again. | |
| `507` | `storage_full` | The server already stores as many bytes of documents for your client as it allows. The limit is 1,073,741,824 bytes unless the server operator changed `BXAPI_MAX_CLIENT_BYTES`. The request stored nothing. | Delete sessions, saves, adventures, or characters you no longer need, then send the request again. `GET /v1/meta` returns the count and the limit as `storage`. | `used_bytes`, `max_bytes` |

### Validation errors

A `validation_error` lists the first 20 problems in `errors`. Each problem has `loc` (the location in the request), `msg`, and `type`. A dice request that asks for zero rolls returns one problem:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/dice/roll -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"expression": "3d6", "times": 0}'
```

```text
HTTP/1.1 422 Unprocessable Content
...
{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"The request failed validation. See `errors` for each field.","code":"validation_error","request_id":"req_E8rrNKpRWiOqYKkv08gtoA","errors":[{"loc":["body","times"],"msg":"Input should be greater than or equal to 1","type":"greater_than_equal"}],"error_count":1}
```

A request body with a field that the endpoint doesn't define also returns `validation_error`. Commands are the exception: the server ignores a field that the command doesn't define.

### Role errors

The server returns `403 referee_required` when a request with a player key sets a command's `source` field:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle", "source": "my-game"}'
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"Only an API key with the referee role can set a command's source.","code":"referee_required","request_id":"req_MrYTOpV06fNG4ovJevhzrw"}
```

The server returns `403 referee_command` when a request with a player key sends a referee command, and the problem document names the command in `command_type`:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "grant_item", "character_id": "character-0001", "item_id": "sword"}'
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"Only an API key with the referee role can send 'grant_item'.","code":"referee_command","request_id":"req_85m_8BdAXuKAR4A4imjy2Q","command_type":"grant_item"}
```

## Handle state conflicts

Three of the `409` codes report that a valid request conflicts with the current state of a session or an ability roll: `revision_conflict`, `session_busy`, and `ability_roll_used`. Each needs more handling than a table row can state, so each has its own section here.

### `revision_conflict`

A session has a *revision*, a count of the commands that the session has accepted. A rejected command doesn't change the revision. When you send a command with the `expected_revision` query parameter, the server runs the command only if the session's revision equals that number. Otherwise, the server runs nothing and returns `409 revision_conflict` with the session's current `revision`:

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"The session is at revision 1, not 0. Read the view and decide whether to send the command again.","code":"revision_conflict","request_id":"req_OTRODr6Qm2o2f5m56PotAQ","revision":1}
```

A revision higher than the one you sent means that the session accepted a command after you read the revision. That happens in one of two ways:

- **Your own retry:** The server applied your earlier request, and you sent it again because the response was lost. The `revision` is the one you expected plus 1.
- **Another sender:** Someone else sent a command to the session, such as another player of your application or another device.

In both cases, don't send the command again right away. Read the session's view with `GET /v1/sessions/{session_id}/view`, or read the events you haven't seen by passing `after` to the event log, and decide from the current state of the game whether the command still makes sense. If it does, send it with the new revision.

### `session_busy`

The server runs one command at a time on each session. A `409 session_busy` means that the command had no effect, for one of two reasons:

- **Lock timeout:** The request waited more than 5 seconds for a session that another request was using.
- **Concurrent write:** Two server processes share one database file, and the other process wrote the session after this request read it and before this request wrote it.

The problem document includes the session's current `revision`:

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"Another server process wrote the session after this request read it and before this request wrote it, so the command had no effect. The session is at revision 11. If you sent expected_revision, read the session before you decide what to do: when the revision has changed, the same request gets revision_conflict. If you didn't, send the request again.","code":"session_busy","request_id":"req_NmLg5cAIh3LnK2XI_Dps1w","revision":11}
```

Recover in one of two ways, according to whether you sent `expected_revision`:

- **With `expected_revision`:** Read the session. If its revision is still the one you sent, send the same request again. If the revision has changed, the same request returns `revision_conflict`, which you handle as [`revision_conflict`](#revision_conflict) describes.
- **Without `expected_revision`:** Send the command again. If the server applied an earlier attempt whose response was lost, the new request runs the command a second time. To prevent that, send `expected_revision` with every command that you might send twice.

### `ability_roll_used`

An *ability roll* is a set of ability scores that `POST /v1/ability-rolls` rolls and stores for a new character, and you can create one character from each roll. A second request to create a character from the same `ability_roll_id` returns `409 ability_roll_used`, whether or not the first character still exists, because deleting a character doesn't free its roll:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna the second", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol_yQsuUWgqmzpnDA6Sm51kQA"}'
```

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"A character was already created from the ability roll 'rol_yQsuUWgqmzpnDA6Sm51kQA'.","code":"ability_roll_used","request_id":"req_OF7V4g893HeodzfygO6keA"}
```

Have your client either request a new roll with `POST /v1/ability-rolls` or find the character that the roll created with `GET /v1/characters`. A request that failed with `character_invalid` doesn't use up the roll, so the player can try again with the same roll.

## Next steps

- [Play a game](play-a-game.md): see a rejected command and a `revision_conflict` in a game.
- [Authentication](authentication.md): the keys behind `401` and `403` responses.
- [Configuration](configuration.md): the settings behind the `408`, `413`, `429`, and `507` limits, and the multi-process setup behind `503`.
