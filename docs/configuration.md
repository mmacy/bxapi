# Configuration

You run bxapi yourself, as a server. Set the server's settings with environment variables, and prepare a server that other people, like the players of your game, can reach over a network.

## Set a setting

Every setting is an environment variable that starts with `BXAPI_`. Set it in the same command that starts the server:

```sh
BXAPI_PORT=8700 BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server reads a `.env` file in its working directory too, with one `NAME=value` per line, and an environment variable overrides the same setting in the file:

```text
BXAPI_PORT=8700
BXAPI_DATABASE=/var/lib/bxapi/game.db
BXAPI_API_KEYS=[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]
```

The server reads each setting once, when it starts. To change one, stop the server and start it again. Write a setting that has a list value as JSON. A value the server can't accept stops it from starting. The server prints one line for each problem, with the name of the environment variable and what's wrong, and it doesn't repeat the value, so an API key doesn't end up in a log:

```sh
BXAPI_SESSION_CACHE_SIZE=0 BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

```text
bxapi: BXAPI_SESSION_CACHE_SIZE: Input should be greater than or equal to 1
```

For a problem in one entry of `BXAPI_API_KEYS`, the line contains the entry's number, counting from 1, and the field's name:

```text
bxapi: BXAPI_API_KEYS, entry 1, key: the key must be at least 16 characters long
```

The server exits with status 1.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `BXAPI_DATABASE` | `bxapi.db` | The path of the SQLite database file, or `:memory:` to keep everything in memory and lose it when the server stops. |
| `BXAPI_API_KEYS` | none | A JSON list of API keys. See [Authentication](authentication.md#configure-api-keys). |
| `BXAPI_ALLOW_ANONYMOUS` | `false` | Whether a server with no API keys may start. Such a server answers only a request that calls it as `localhost`, `127.0.0.1`, or `[::1]`. |
| `BXAPI_CORS_ORIGINS` | none | A JSON list of the web origins whose pages may call the API from a browser. |
| `BXAPI_SESSION_CACHE_SIZE` | `256` | How many sessions (games in progress) the server keeps loaded in memory, at least 1. |
| `BXAPI_MAX_BODY_BYTES` | `8388608` | The largest request body, in bytes, at least 1,024. A request that takes no document, like a command, is also limited to 65,536 bytes. |
| `BXAPI_BODY_TIMEOUT_SECONDS` | `30` | How long the server waits for a request's whole body to arrive, in seconds. |
| `BXAPI_DOCS_ENABLED` | `true` | Whether the server serves `/docs`, `/redoc`, and `/openapi.json`. |
| `BXAPI_HOST` | `127.0.0.1` | The address the `bxapi` command listens on. |
| `BXAPI_PORT` | `8640` | The port the `bxapi` command listens on, from 0 to 65535. |

Examples:

- `BXAPI_DATABASE=/var/lib/bxapi/game.db` stores the data in that file. The server creates the file if it doesn't exist, readable and writable only by the user that runs the server.
- `BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]'` accepts one player key. With no keys, the server starts only when `BXAPI_ALLOW_ANONYMOUS` is `true`, and then asks for no key and treats every caller as a referee. Use that only on your own machine.
- `BXAPI_CORS_ORIGINS='["https://game.example"]'` lets pages from that origin call the API. An origin is a scheme, a host, and an optional port, like `http://localhost:3000`, with no path and no trailing slash. `*` isn't allowed.
- `BXAPI_SESSION_CACHE_SIZE=32` keeps 32 sessions loaded. When the cache is full, the server drops the session used longest ago and loads it from the database the next time a request needs it, so a smaller cache saves memory and makes some requests slower.
- `BXAPI_MAX_BODY_BYTES=1048576` refuses a request body of more than 1 MiB with 413 `content_too_large`. A save document grows with every command, so a server that imports saves needs a limit larger than its biggest save. The setting is the limit for the five requests that take a document: an adventure upload, an adventure check, a character import, a save import, and a new session, which can contain a party document. Every other request body is also limited to 65,536 bytes, and no setting raises that limit.
- `BXAPI_BODY_TIMEOUT_SECONDS=10` gives a client 10 seconds to send a body. A slower one gets 408 `request_timeout`.
- `BXAPI_DOCS_ENABLED=false` turns the documentation pages off.
- `BXAPI_HOST=0.0.0.0` listens on every network interface of the machine, instead of only the machine itself.
- `BXAPI_PORT=8700` listens on port 8700.

## Run it for other people

### Require API keys

Never run a shared server with `BXAPI_ALLOW_ANONYMOUS=true`. A server with no keys answers only a request that calls it as `localhost`, `127.0.0.1`, or `[::1]`, and gives every other request 421 `misdirected_request`, but a program on your network can send one of those names. Give each application a separate client name and separate keys, and give a referee key only to software you trust. See [Authentication](authentication.md).

### Allow browser clients

A page in a browser can call the API only from an origin that the server lists in `BXAPI_CORS_ORIGINS`. Without the setting, the server sends no cross-origin headers, and a browser blocks every call from another origin. The setting affects only calls from pages in a browser. Start a server that allows one origin:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' BXAPI_CORS_ORIGINS='["https://game.example"]' uv run bxapi
```

The server logs the origins at startup:

```text
INFO:     bxapi requires an API key. Keys: 1. Clients: my-game. CORS origins: https://game.example.
```

A browser first sends a *preflight* request, an `OPTIONS` request that asks whether the real request is allowed. A preflight request has no key, and the server answers it without one:

```sh
curl -si -X OPTIONS http://127.0.0.1:8640/v1/sessions -H "Origin: https://game.example" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: authorization,content-type"
```

```text
HTTP/1.1 200 OK
...
access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY
access-control-max-age: 600
access-control-allow-origin: https://game.example
access-control-allow-headers: authorization,content-type
...
```

The server lists `WWW-Authenticate`, `Retry-After`, `ETag`, `Idempotent-Replayed`, and `X-Request-Id` in `access-control-expose-headers`, so a page's code can read them. A request from an origin that isn't in the list gets no `access-control-allow-origin` header, so the browser stops the page from reading the response. A key in a web page's code is readable by every player, so use a player key there and never a referee key. For more on that, see [An application with many users](authentication.md#an-application-with-many-users).

### Choose where the database lives and back it up

The server stores everything in one SQLite file, `bxapi.db` in its working directory by default. Set `BXAPI_DATABASE` to a path on a disk that's backed up. The server creates the file readable and writable only by its user, because it contains the *master seed* of every session, the number that determines the session's dice rolls. While the server runs, SQLite also keeps `-wal` and `-shm` files beside it, with the same permissions.

Don't copy the file with `cp` while the server runs, because some committed data can still be in the `-wal` file. Back it up with the `.backup` command of the `sqlite3` command-line tool, which makes a consistent copy while the server runs. If you set `BXAPI_DATABASE`, use that path in place of `bxapi.db`:

```sh
sqlite3 bxapi.db ".backup bxapi-backup.db"
```

The backup is a normal SQLite file. The `.backup` command creates it with your user's default permissions, which can let other users on the machine read it, so restrict the file's permissions:

```sh
chmod 600 bxapi-backup.db
```

To restore, stop the server, put the backup where `BXAPI_DATABASE` points, and start the server. The server refuses to start with a file whose table layout it doesn't know, and its message states the path and the versions.

### Put a reverse proxy in front

The server limits how long a request's body takes to arrive, with `BXAPI_BODY_TIMEOUT_SECONDS`, but it can't limit how long a client takes to send a request's headers or how long a connection stays open between requests. Run a deployed server behind a *reverse proxy*, a server like nginx or Caddy that accepts the connections from the internet and forwards the requests to bxapi. Set the proxy to enforce timeouts for headers and idle connections, to end TLS, and to limit the body size. bxapi has no rate limits, so set the proxy to limit how many requests and connections one address may make. Keep bxapi on the default `BXAPI_HOST` of `127.0.0.1`, so only the proxy on the same machine can reach it, or on a private network address.

### Find a request in the log

The `bxapi` command logs one line for each request, when it sends the response:

```text
INFO:     request_id=req_Zk3uY0mWq1x8Jr5cTt2aLw method=POST path=/v1/sessions/ses_BVmUCi5oRTQ0qSr1NadnJw/commands status=200 duration_ms=2.3 client=my-game
```

`request_id` is the id in the response's `X-Request-Id` header and in a problem document's `request_id`, so a client developer who reports an error can give you the id to search for. `duration_ms` is the time the server took. `client` is the client name of the request's API key, or `-` for a request with no valid key. The line never contains a key, a query string, or a body. When a request fails with 500 `internal_error`, the server also logs the traceback, in a line that states the same id.

A client can send its own `X-Request-Id`, so don't treat an id as unique or as proof of who sent a request.

The line goes to the Python logger `bxapi.access`, at the level `INFO`. If you serve the application with another ASGI server instead of the `bxapi` command, configure the `bxapi` logger to see it.

### Run more than one process

The `bxapi` command runs one process. You can start several against one database file, on different ports behind your proxy. They coordinate through the database: each process checks the session's *revision*, the count of commands the session has accepted, in the database before it uses a loaded session, and writes a command only if the revision hasn't changed since it read it. Two processes can serve the same session without losing or repeating a command.

When another process writes the session after a request reads it and before the request writes it, the command has no effect, and the client gets 409 `session_busy` with the session's current `revision`. A client handles it as [Errors](errors.md#session_busy) describes: it reads the session if it sent `expected_revision`, and sends the command again if it didn't. A client that sends `expected_revision` with every command never repeats a command by mistake.

Processes also take turns writing to the database. A process that waits more than 5 seconds for the write lock answers with 503 `database_busy` and the header `Retry-After: 1`. Have your client wait the number of seconds in `Retry-After` and send the request again.

All processes need the same keys, so give them the same `BXAPI_API_KEYS`. Run them from the same version of bxapi. If a process with a different release of the rules engine wrote a session, a process that can't load that session answers 500 `session_unreadable`.

### Turn off the documentation pages

The pages at `/docs` and `/redoc` and the document at `/openapi.json` need no key, so anyone who can reach the server can read the API's description. To turn them off:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' BXAPI_DOCS_ENABLED=false uv run bxapi
```

The three paths then return 404:

```sh
curl -si http://127.0.0.1:8640/docs
```

```text
HTTP/1.1 404 Not Found
...
```

You can still get the OpenAPI document without a server: [the README](../README.md#read-the-api-reference) shows how.

## Next steps

- [Authentication](authentication.md): create keys and choose roles.
- [Errors](errors.md): what a client does about 408, 409, 413, and 503.
- [Play a game](play-a-game.md): use the API.
