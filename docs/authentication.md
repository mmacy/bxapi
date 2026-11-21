# Authentication

bxapi authenticates every request with an API key. When you run a server, configure a key for each application that calls it, and give each key a *role*, either `player` (the default) or `referee`, which determines what the key may do. If you haven't set up bxapi yet, follow [Start the server](../README.md#start-the-server) in the README first.

## Configure API keys

The server reads its keys from the `BXAPI_API_KEYS` environment variable, a JSON list. Each entry has a `client`, a `key`, and an optional `role`:

- `client`: The name of your application, such as `my-game`. A client name has up to 64 letters, digits, dots, underscores, and hyphens, and it starts with a letter or a digit.
- `key`: A secret that you choose and your application sends. A key has at least 16 printable ASCII characters and no spaces, and every key in the list must be different.
- `role`: Either `player` or `referee`. Defaults to `player`.

Generate each key with a random-number generator instead of writing one by hand:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```text
I2xREiV099R-y8OEwT_Xb7FFZuP4vWrK
```

Your key differs. The examples on this page use short, readable keys, which you shouldn't use on a server that other people can reach.

Start the server with three keys: a player key and a referee key for the application `my-game`, and a player key for a second application, `other-game`.

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}, {"client": "my-game", "key": "referee-key-0123456789", "role": "referee"}, {"client": "other-game", "key": "other-key-0123456789ab", "role": "player"}]' uv run bxapi
```

At startup, the server logs the number of keys and the client names, but never a key:

```text
INFO:     bxapi requires an API key. Keys: 3. Clients: my-game, other-game. CORS origins: none.
```

A server with no keys doesn't start unless `BXAPI_ALLOW_ANONYMOUS` is `true`. [Configuration](configuration.md) lists every setting and shows how to put the keys in a `.env` file.

## Send an API key

Send the key as a bearer token in the `Authorization` header of every request under `/v1`. The scheme name `Bearer` is case-insensitive. In a second terminal, store the player key of `my-game` in a shell variable, then ask the server which client and role the key identifies:

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
  "role": "player",
  "storage": {"used_bytes": 0, "max_bytes": 1073741824}
}
```

`client` and `role` are the application and the role that the key identifies. Call this endpoint when your application starts to confirm that the key works.

The only paths that don't require a key are `GET /health`, `/docs`, `/redoc`, and `/openapi.json`.

## Handle a `401` response

The server returns `401 unauthorized` for a request with no key, a key the server doesn't recognize, or a malformed `Authorization` header. The server checks the key before it reads the rest of the request, so a request without a valid key receives the same response at every path under `/v1`, whether or not the path exists. This request sends no key:

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

Retrying the request can't fix a `401`. Check the key that your application sends.

## Roles

A key's role determines what its holder may do in a *session*, a game in progress on the server. The `referee` role includes everything the `player` role allows:

| Role | Permissions |
| --- | --- |
| `player` | Everything a game client needs: read the rules, roll dice, create characters, upload adventures, start and play sessions, and save and restore. The server shows a player only what the players of a game may see. |
| `referee` | Everything a player may do, plus: read the referee view and referee events, send referee commands, choose seeds, import characters and saves, and read full adventure and save documents. |

The server returns `403 referee_required` when a request with a player key asks for something only a referee may do. This request and the later ones on this page use the ID of a session that the player key of `my-game` started, as [Play a game](play-a-game.md#start-a-session) shows. Replace the ID with one of yours:

```sh
curl -si http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/referee-view -H "Authorization: Bearer $KEY"
```

```text
HTTP/1.1 403 Forbidden
...
content-type: application/problem+json

{"type":"about:blank","title":"Forbidden","status":403,"detail":"This request needs an API key with the referee role.","code":"referee_required","request_id":"req_HM0yFc_Skx2W4peAo6cCFg"}
```

The same request succeeds with the referee key:

```sh
export KEY=referee-key-0123456789
```

```sh
curl -s http://127.0.0.1:8640/v1/sessions/ses_Bq2lhAvBgBLVtCRWuWkVUQ/referee-view -H "Authorization: Bearer $KEY" | head -c 300
```

```text
{"ruleset":{"hp_reroll_at_first_level":false,"encumbrance":"basic","variable_weapon_damage":true,"individual_initiative":false,"thac0_arithmetic":false,"weapon_reload":false,"hd5_counts_as_magical":false,"magic_item_death_save":true,"xp_award_timing":"on_return","deprivation_penalties":false,"aoe_fr
```

Retrying the request can't fix a `403` either. Give your application a referee key only if it needs one, and only if it runs on a server you trust, such as your game's backend. Never put a referee key in a web page or an app that your players can read. [Run games as a referee](referee.md) describes what the role adds.

## Data isolation between clients

bxapi has no user accounts, so a key identifies a client application, not a person. The server stores every character, adventure, session, and save with the client name of the key that created it, and it applies these four rules:

- **Each client reaches only its own data.** A request can read and change only the data of the client that its key belongs to. A request for another client's resource receives the same `404` as a request for an ID that doesn't exist.
- **All of a client's keys share its data.** Everyone who has one of a client's keys shares that client's data, whatever the key's role. Both keys of `my-game` can read, command, rename, and delete all of the application's characters, adventures, sessions, and saves.
- **You can rotate keys without losing data.** Two keys with the same client name share data, so you can add a new key, switch your application to it, and remove the old key.
- **A role doesn't change whose data a key reaches.** The role limits what a key's holder may see and do inside a game, but not which client's data the holder can reach.

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

The key of `other-game` can't read that session, so the same request returns `404 not_found`:

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

The session list for `other-game` is empty:

```sh
curl -s http://127.0.0.1:8640/v1/sessions -H "Authorization: Bearer $KEY"
```

```json
{"items": [], "total": 0, "limit": 50, "offset": 0}
```

### Multi-user applications

bxapi can't tell your application's users apart. Every request from your application includes one of your keys, so the server treats every request as your application's, and every list endpoint returns the data of all your users. You can keep one user from reaching another user's data in two ways:

- **Your server calls bxapi on behalf of your users.** Your server keeps the key, records which IDs belong to which user, and rejects a request for an ID that isn't the user's. The users never see the key.
- **Your clients call bxapi directly.** Each client stores only the IDs of its user's characters, sessions, and saves. Each ID ends in 22 random characters, so a user can't guess another user's IDs. However, each client has the key, so a user who inspects the client's traffic can list everything with `GET /v1/sessions`. Use this approach only when you accept that one user can read the other users' data.

We recommend the first approach for a game that has user accounts.

## Anonymous mode for local development

A server with `BXAPI_ALLOW_ANONYMOUS=true` and no keys runs in *anonymous mode*: it doesn't require an API key, and it treats every caller as a referee for the client `local`. Stop the server from the previous sections and start one in anonymous mode:

```sh
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

`GET /v1/meta` reports the client and the role that every caller has:

```sh
curl -s http://127.0.0.1:8640/v1/meta
```

```json
{"api_version":"0.1.0","engine_version":"2.5.0","schema_version":4,"auth_enabled":false,"client":"local","role":"referee","storage":{"used_bytes":0,"max_bytes":1073741824}}
```

Use anonymous mode only on your own machine. When the server has any key, `BXAPI_ALLOW_ANONYMOUS` has no effect.

A server in anonymous mode answers a request under `/v1` only when you call it as `localhost`, `127.0.0.1`, or `[::1]`, with or without a port. The server reads the name from the request's `Host` header, which your HTTP client fills in from the URL. A request with any other name receives `421 misdirected_request`:

```sh
curl -s http://127.0.0.1:8640/v1/meta -H "Host: game.example"
```

```json
{"type":"about:blank","title":"Misdirected Request","status":421,"detail":"This server asks for no API key, so it answers only a request whose Host header is localhost, 127.0.0.1, or [::1]. To call the server by another name, set BXAPI_API_KEYS and send a key.","code":"misdirected_request","request_id":"req_Zk3uY0mWq1x8Jr5cTt2aLw"}
```

The `Host` check protects your data from the web pages you visit while the server runs. A page on another site can make your browser send requests to `127.0.0.1`. If the page first points its own host name at `127.0.0.1`, a technique called DNS rebinding, your browser also lets the page read the responses. The browser then sends the page's host name in `Host`, so the server rejects the request.

To reach the server from another machine, or under another name, set `BXAPI_API_KEYS`. A server with keys answers any `Host`.

## Next steps

- [Errors](errors.md): every error code and how to handle it.
- [Run games as a referee](referee.md): what a referee key adds.
- [Configuration](configuration.md): every setting, and how to deploy a shared server.
