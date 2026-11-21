# Errors

Read an error response, decide what your client should do about it, and handle the three kinds of conflict with the state of a game.

## Read a problem document

Every error response has a 4xx or 5xx status and the media type `application/problem+json`. The body is a *problem document*, the standard JSON format for HTTP errors that RFC 9457 defines, with these members (fields):

- `type` is always `about:blank`.
- `title` is the status's standard name.
- `status` is the HTTP status.
- `code` is a short fixed name for the error. Read `code` in your client to decide what to do.
- `detail` is a sentence for a person, at most 1,000 characters. Show it to a developer or put it in a log, and don't match on its text, because the wording can change.
- `request_id` is the id the server gave the request. The `X-Request-Id` response header has the same id.
- Some codes add members, like `revision` or `resource`. The table below lists them. Have your client ignore any member it doesn't recognize.

For example, a request for a character that doesn't exist:

```sh
curl -si http://127.0.0.1:8640/v1/characters/chr_doesnotexist -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 404 Not Found
...
content-type: application/problem+json

{"type":"about:blank","title":"Not Found","status":404,"detail":"No character has the id 'chr_doesnotexist'.","code":"not_found","request_id":"req_C1tg5yjlBBxBDofQPlRXoQ","resource":"character","id":"chr_doesnotexist"}
```

These examples use a player key, a key with the `player` role, in the shell variable `KEY`, as [Play a game](play-a-game.md#start-a-server-with-a-player-key) sets up. For more about keys and roles, see [Authentication](authentication.md). The `-i` option makes curl print the status line and headers.

## Report a failed request

Every response, successful or not, has an `X-Request-Id` header, and the server logs that id with the request. Have your client log the `request_id` of each problem document. When you report an error to whoever runs the server, include the id, so they can find the request in the server's log. For a 500 `internal_error`, the server logs the cause with the same id.

To follow one request through your own systems and the server's log, choose the id yourself. Send an `X-Request-Id` header with 1 to 128 letters, digits, and the characters `.`, `_`, `~`, and `-`:

```sh
curl -si http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $KEY" -H "X-Request-Id: checkout-7f3a"
```

```text
HTTP/1.1 200 OK
...
x-request-id: checkout-7f3a
```

The server replaces any other value with an id it draws, which starts with `req_`.

## Tell a refused command from an error

A game command that the rules refuse isn't an error. The server answers with status 200, `accepted` set to `false`, and the rules engine's `rejections`. The session, the game in progress, doesn't change. A refusal is a normal part of play, like walking into a wall:

```text
HTTP/1.1 200 OK
...
{"accepted":false,"rejections":[{"code":"exploration.move.blocked","params":{"direction":"north"}}],"events":[],"messages":[],"mode":"exploring","ended":false,"revision":1}
```

An error means the server didn't do what you asked, for example because the request has no key, it's malformed, or it refers to something that doesn't exist. A command with an unknown `command_type` is an error:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "fly"}'
```

```text
HTTP/1.1 422 Unprocessable Content
...
content-type: application/problem+json

{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"The request failed validation. See `errors` for each field.","code":"validation_error","request_id":"req_G8f0xBMB3wTR7-DqjdMQjA","errors":[{"loc":["body"],"msg":"'fly' isn't a value that 'command_type' accepts.","type":"union_tag_invalid"}],"error_count":1}
```

Handle a refusal as a result of play: read `rejections` and tell the player. Handle an error as a bug in your client or as a reason to retry. The table below lists what to do for each code.

Unlike the commands endpoint, the endpoints that create a character, `POST /v1/characters` and `POST /v1/generate/character`, treat a broken rule as an error. They answer a choice that breaks a rule, like buying more than the character can afford, with status 422 and the code `character_invalid`, because the request asks the server to create a character and the server can't.

## Error codes

| Status | `code` | When it happens | What your client does | Extra members |
| --- | --- | --- | --- | --- |
| 400 | `bad_request` | The server can't decode the body as text, or the body has an integer of more than 4,300 digits. Send JSON in UTF-8. | Fix the request. Don't retry. | |
| 401 | `unauthorized` | The key is missing or unknown. | Check the key. See [Authentication](authentication.md#handle-a-401). | |
| 403 | `referee_required` | A caller with a player key asked for something only a referee may do: a referee endpoint, or a field like `seed`, `source`, or `party_document`, or `visibility=referee`. | Use a referee key, or leave the field out. | |
| 403 | `referee_command` | A caller with a player key sent a referee command. | Use a referee key, or send a play command. | `command_type` |
| 403 | `adventure_read_only` | The request tried to delete an adventure that comes with the server. | Don't delete it. | |
| 404 | `not_found` | No resource of your client has that id, or no route has that path. | Check the id. A resource of another client is a 404 too. | `resource`, `id` |
| 405 | `method_not_allowed` | The path exists, but not with that HTTP method. | Fix the method. | |
| 408 | `request_timeout` | The request body didn't arrive within `BXAPI_BODY_TIMEOUT_SECONDS`. | Send the request again on a faster connection. | |
| 409 | `revision_conflict` | `expected_revision` doesn't match the session's revision. | Read the session, then decide. See [below](#revision_conflict). | `revision` |
| 409 | `session_busy` | The command had no effect, because another request used the session for more than 5 seconds or another server process wrote the session first. | Read the session if you sent `expected_revision`, and send the command again if you didn't. See [below](#session_busy). | `revision` |
| 409 | `ability_roll_used` | A character was already created from that ability roll, the stored set of ability scores that `POST /v1/ability-rolls` returned. | Roll again, or use the character that exists. See [below](#ability_roll_used). | |
| 409 | `version_unsupported` | A document you uploaded or imported has a schema version newer than the server's osrlib reads. | Export the document in an older version, or run a newer server. Check `schema_version` in `GET /v1/meta`. | |
| 409 | `idempotency_key_in_progress` | The first request with the same `Idempotency-Key` is still running. The response has the header `Retry-After: 1`. | Wait the number of seconds in `Retry-After`, then send the request again. | |
| 409 | `idempotency_key_interrupted` | The server failed or stopped while it handled the first request with the same `Idempotency-Key`, and it has no record of whether that request took effect. | Read the resource the request would have changed. If the request didn't take effect, send it again with a new key. On an overloaded server the first request can still be running, so read the resource again before you do. | |
| 413 | `content_too_large` | The request body is larger than `BXAPI_MAX_BODY_BYTES`. | Send a smaller body. | |
| 415 | `unsupported_media_type` | The request has a body, and its `Content-Type` header doesn't say that the body is JSON. curl's `-d` option sends `application/x-www-form-urlencoded` unless you set the header. | Send the body as JSON with the header `Content-Type: application/json`. | |
| 422 | `validation_error` | The request doesn't match the endpoint's schema, or the body isn't valid JSON. | Fix the request. Read `errors`. | `errors`, `error_count` |
| 422 | `content_invalid` | The rules engine refused a value or a document as malformed, like a dice expression it can't parse or an adventure that refers to a monster that doesn't exist. | Fix the value or the document. Read `detail`. | |
| 422 | `character_invalid` | The choices for a new character break a rule, or name a class or an item that doesn't exist. | Show the player the `rejections` and let them choose again. | `rejections`, or `field`, `index`, and `id` |
| 422 | `party_invalid` | The characters named for a new session can't form a party: the rules refuse the party, it has more than 20 members, or two members have the same id. | Change the party. | |
| 422 | `idempotency_key_reused` | Your client already sent the `Idempotency-Key` with a different request. | Send each new request with a new key. | |
| 500 | `internal_error` | A bug in the server. The response says nothing about the cause, and the server logs it. | Report it, with the `request_id`. Retrying with the same `Idempotency-Key`, or retrying a command with `expected_revision`, is safe. | |
| 500 | `session_unreadable` | The server can't load a stored session or save, for example because a server with a different osrlib release wrote it. The response has no detail from the stored state. | Don't retry. Report it to whoever runs the server, with the `request_id`. | |
| 503 | `database_busy` | Another process held the database's write lock longer than the server waits, which is 5 seconds. The response has the header `Retry-After: 1`. | Wait the number of seconds in `Retry-After`, then send the request again. | |

For `validation_error`, `errors` lists the first 20 problems. Each has `loc`, the place in the request, `msg`, and `type`. For example, a dice request that asks for zero rolls:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/dice/roll -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"expression": "3d6", "times": 0}'
```

```text
HTTP/1.1 422 Unprocessable Content
...
{"type":"about:blank","title":"Unprocessable Content","status":422,"detail":"The request failed validation. See `errors` for each field.","code":"validation_error","request_id":"req_E8rrNKpRWiOqYKkv08gtoA","errors":[{"loc":["body","times"],"msg":"Input should be greater than or equal to 1","type":"greater_than_equal"}],"error_count":1}
```

A request body with a field that the endpoint doesn't define is a `validation_error` too, except in a command, where the server ignores a field the command doesn't define.

A caller with a player key who sets a command's `source` field gets 403 `referee_required`:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "engage_battle", "source": "my-game"}'
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"Only an API key with the referee role can set a command's source.","code":"referee_required","request_id":"req_MrYTOpV06fNG4ovJevhzrw"}
```

A caller with a player key who sends a referee command gets `referee_command`, with the command's name:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/sessions/ses_69rujo4njhlTcX0L0EgfEw/commands -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"command_type": "grant_item", "character_id": "character-0001", "item_id": "sword"}'
```

```text
HTTP/1.1 403 Forbidden
...
{"type":"about:blank","title":"Forbidden","status":403,"detail":"Only an API key with the referee role can send 'grant_item'.","code":"referee_command","request_id":"req_85m_8BdAXuKAR4A4imjy2Q","command_type":"grant_item"}
```

## Handle the three conflicts

Three of the 409 codes mean that the request is valid, but it conflicts with the state of a session or an ability roll. The fourth, `version_unsupported`, is about the version of a document you sent.

### revision_conflict

A session has a *revision*, a count of the commands that the session has accepted. A refused command doesn't change the revision. When you send a command with the query parameter `expected_revision`, the server runs the command only if the session's revision is that number. Otherwise it runs nothing and returns 409 `revision_conflict` with the session's current `revision`:

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"The session is at revision 1, not 0. Read the view and decide whether to send the command again.","code":"revision_conflict","request_id":"req_OTRODr6Qm2o2f5m56PotAQ","revision":1}
```

When the revision is higher than the one you sent, the session accepted a command after you read the revision. There are two ways that can happen:

- Your own earlier request was applied, and you sent it again because the response was lost. The `revision` is the one you expected plus 1.
- Someone else sent a command to the session, like another player of your application or another device.

Either way, don't send the command again right away. Read the session's view with `GET /v1/sessions/{session_id}/view`, or the events you haven't read with `after`, and decide from what the game looks like now whether the command still makes sense. If it does, send it with the new revision.

### session_busy

The server runs one command at a time on each session. A 409 `session_busy` means the command had no effect. It has two causes:

- The request waited more than 5 seconds for a session that another request was using.
- Two server processes share one database file, and the other process wrote the session after this request read it and before this request wrote it.

The problem document includes the session's current `revision`:

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"Another server process wrote the session after this request read it and before this request wrote it, so the command had no effect. The session is at revision 11. If you sent expected_revision, read the session before you decide what to do: when the revision has changed, the same request gets revision_conflict. If you didn't, send the request again.","code":"session_busy","request_id":"req_NmLg5cAIh3LnK2XI_Dps1w","revision":11}
```

Handle it this way:

- If you sent `expected_revision`, read the session. When its revision is still the one you sent, send the same request again. When it has changed, sending the same request gets `revision_conflict`, and you handle that as above.
- If you didn't send `expected_revision`, send the command again. If the response to an earlier attempt was lost after the server applied it, this runs the command twice. To prevent that, send `expected_revision` with every command that you might send twice.

### ability_roll_used

An *ability roll* is a set of ability scores that the server rolled and stored for a new character, and you can create one character from it. A second request to create a character from the same `ability_roll_id` gets 409 `ability_roll_used`, whether or not the first character still exists, because deleting a character doesn't free its roll:

```sh
curl -si -X POST http://127.0.0.1:8640/v1/characters -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"name": "Brenna the second", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": "rol_yQsuUWgqmzpnDA6Sm51kQA"}'
```

```text
HTTP/1.1 409 Conflict
...
{"type":"about:blank","title":"Conflict","status":409,"detail":"A character was already created from the ability roll 'rol_yQsuUWgqmzpnDA6Sm51kQA'.","code":"ability_roll_used","request_id":"req_OF7V4g893HeodzfygO6keA"}
```

Your client either asks for a new roll with `POST /v1/ability-rolls`, or it finds the character the roll created with `GET /v1/characters`. A request that failed with `character_invalid` doesn't use up the roll, so the player can try again with the same one.

## Next steps

- [Play a game](play-a-game.md): see a refused command and a `revision_conflict` in a game.
- [Authentication](authentication.md): the keys behind 401 and 403.
- [Configuration](configuration.md): the limits behind 408, 413, and 503.
