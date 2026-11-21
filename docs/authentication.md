# Authentication

When you run a bxapi server, set up API keys so only your application can call it. Give each key a *role*, `player` (the default) or `referee`, which determines what the key may do.

## Configure API keys

The server reads its keys from the `BXAPI_API_KEYS` environment variable, a JSON list. Each entry has a `client` and a `key`, and it can have a `role`:

- `client` is the name of your application, like `my-game`. It can have letters, digits, dots, underscores, and hyphens, up to 64 characters, and it starts with a letter or a digit.
- `key` is a secret that you choose and your application sends. It has at least 16 printable ASCII characters and no spaces. Every key in the list must be different.
- `role` is `player` or `referee`. It defaults to `player`.

Generate a key with a random-number generator, not by hand:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```text
I2xREiV099R-y8OEwT_Xb7FFZuP4vWrK
```

Your key differs. The examples on this page use short keys that are easy to read, which you shouldn't use on a server that other people can reach. If you haven't set up bxapi yet, follow [Get it running](../README.md#get-it-running) in the README first. Start the server with a player key and a referee key for the application `my-game`, and a player key for a second application, `other-game`:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}, {"client": "my-game", "key": "referee-key-0123456789", "role": "referee"}, {"client": "other-game", "key": "other-key-0123456789ab", "role": "player"}]' uv run bxapi
```

At startup the server logs how many keys it has and the client names, but never a key:

```text
INFO:     bxapi requires an API key. Keys: 3. Clients: my-game, other-game. CORS origins: none.
```

A server with no keys doesn't start, unless `BXAPI_ALLOW_ANONYMOUS` is `true`. [Configuration](configuration.md) lists every setting, including how to put the keys in a `.env` file.

## Send a key

Send the key as a bearer token in the `Authorization` header of every request under `/v1`. The word `Bearer` can be in any case. In a second terminal, put the player key of `my-game` in a shell variable and ask the server who you are:

```sh
export KEY=player-key-0123456789
```

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

`client` and `role` are the application and the role that the key identifies. Call this endpoint when your application starts, to check that the key works.

Only `GET /health`, `/docs`, `/redoc`, and `/openapi.json` need no key.

## Handle a 401

A request with no key, a key the server doesn't know, or a malformed `Authorization` header gets status 401 and the code `unauthorized`. The server checks the key before it reads the request, so a request without a valid key gets the same answer at every path under `/v1`, whether or not the path exists.

```sh
curl -si http://127.0.0.1:8640/v1/meta
```

```text
HTTP/1.1 401 Unauthorized
...
www-authenticate: Bearer
content-type: application/problem+json

{"type":"about:blank","title":"Unauthorized","status":401,"detail":"Send a valid API key as a bearer token in the Authorization header.","code":"unauthorized","request_id":"req_pewzocxFBnn9U0Tb3_JQiQ"}
```

Your application can't fix a 401 by trying again. Check the key your application sends.

## Roles

A key's role determines what the holder may do in a *session*, a game in progress on the server.

| Role | May do |
| --- | --- |
| `player` | Everything a game client needs: read the rules, roll dice, create characters, upload adventures, start and play sessions, and save and restore. The server shows a player only what the players of a game may see. |
| `referee` | Everything a player may do, and also: read the referee view and referee events, send referee commands, choose seeds, import characters and saves, and read full adventure and save documents. |

A player who asks for something only a referee may do gets status 403 and the code `referee_required`. This request and the ones below use the id of a session that the player key of `my-game` started, as [Play a game](play-a-game.md#start-a-session) shows. Replace the id with one of yours:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/referee-view -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
content-type: application/problem+json

{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required","request_id":"req_HM0yFc_Skx2W4peAo6cCFg"}
```

The same request with the referee key works:

```sh
export KEY=referee-key-0123456789
```

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/referee-view -H "Authorization: Bearer $KEY" | head -c 300
```

```text
{"ruleset":{"hp_reroll_at_first_level":false,"encumbrance":"basic","variable_weapon_damage":true,"individual_initiative":false,"thac0_arithmetic":false,"weapon_reload":false,"hd5_counts_as_magical":false,"magic_item_death_save":true,"xp_award_timing":"on_return","deprivation_penalties":false,"aoe_fr
```

Trying again doesn't fix a 403 either. Give your application a referee key only if it needs one. [Run games as a referee](referee.md) describes what the role adds. Give a referee key only to software that runs on a server you trust, like your game's backend. Don't put a referee key in a web page or an app that your players can read.

## Know whose data a key reaches

bxapi has no user accounts. A key identifies a client application, not a person. The server stores every character, adventure, session, and save with the client name of the key that created it, and it applies these four rules:

- A request can read and change only the data of the client that its key belongs to. A request for another client's resource gets the same 404 as a request for an id that doesn't exist.
- Everyone who has one of a client's keys shares that client's data, whatever the key's role. Both keys of `my-game` can read, command, rename, and delete all of the application's characters, adventures, sessions, and saves.
- Two keys with the same client name share data too, so you can add a new key, switch your application to it, and remove the old one without losing anything.
- The role limits what a key's holder can see and do inside a game, not whose data the holder can reach.

The referee key of `my-game`, still in `KEY`, can read the session that the player key of `my-game` created:

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "ses_Bq2lhAvBgBLVtCRWuWkVUQ",
  "name": "The Barrow of the Forgotten King",
  "adventure_id": "builtin-barrow",
  "adventure_name": "The Barrow of the Forgotten King",
  "mode": "battle",
  "ended": false,
  "revision": 5,
  "clock": {"rounds": 121, "turns": 2, "days": 0},
  "schema_version": 4,
  "engine_version": "2.5.0",
  "created_at": "2026-11-21T05:24:33Z",
  "updated_at": "2026-11-21T05:24:33Z"
}
```

The key of `other-game` can't:

```sh
export KEY=other-key-0123456789ab
```

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 404 Not Found
...
content-type: application/problem+json

{"type":"about:blank","title":"Not Found","status":404,"detail":"No session has the id 'ses_Bq2lhAvBgBLVtCRWuWkVUQ'.","code":"not_found","request_id":"req_biSalP_FWxiKOYf0axyAlA","resource":"session","id":"ses_Bq2lhAvBgBLVtCRWuWkVUQ"}
```

```sh
curl -s http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY"
```

```json
{"items": [], "total": 0, "limit": 50, "offset": 0}
```

### An application with many users

If your application has many users, bxapi can't tell them apart. Every request from your application includes one of your keys, so the server treats every request as your application's, and every list endpoint returns the data of all your users. There are two ways to keep one user from reaching another user's data:

- Your server calls bxapi on behalf of your users. Your server keeps the key, records which ids belong to which user, and refuses a request for an id that isn't the user's. The users never see the key.
- Your clients call bxapi directly. Each client stores only the ids of its user's characters, sessions, and saves. Each id ends in 22 random characters, so a user can't guess another user's ids. But a client has the key, so it can also list everything with `GET /v1/sessions`. Use this only when you don't mind a user who inspects the client's traffic reading the other users' data.

We recommend the first way for a game that has user accounts.

## Run without keys on your own machine

With `BXAPI_ALLOW_ANONYMOUS=true` and no keys, the server asks for no key and treats every caller as a referee for the client `local`. Stop the server above and start this one:

```sh
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

`GET /v1/meta` shows who you are:

```sh
curl -s http://127.0.0.1:8640/v1/meta
```

```json
{"api_version":"0.1.0","engine_version":"2.5.0","schema_version":4,"auth_enabled":false,"client":"local","role":"referee"}
```

Use this mode only on your own machine. When the server has any key, `BXAPI_ALLOW_ANONYMOUS` has no effect.

## Next steps

- [Errors](errors.md): every error code and what to do about it.
- [Run games as a referee](referee.md): what a referee key adds.
- [Configuration](configuration.md): every setting, and how to run a server for other people.
