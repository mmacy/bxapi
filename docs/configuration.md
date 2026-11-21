# Configuration

bxapi is a server that you run yourself. Configure it with environment variables, then prepare it for a deployment that other people, such as the players of your game, can reach over a network.

## Set configuration values

Every setting is an environment variable that starts with `BXAPI_`. Set a variable in the same command that starts the server:

```sh
BXAPI_PORT=8700 BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server also reads a `.env` file in its working directory, with one `NAME=value` pair per line. An environment variable overrides the same setting in the file:

```text
BXAPI_PORT=8700
BXAPI_DATABASE=/var/lib/bxapi/game.db
BXAPI_API_KEYS=[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]
```

The server reads each setting once, at startup, so restart the server to change a setting. Write a setting that has a list value as JSON.

The server doesn't start with a value it can't accept. It prints one line for each problem, with the name of the environment variable and what's wrong, and then exits with status 1. The line never repeats the value, so an API key doesn't end up in a log:

```sh
BXAPI_SESSION_CACHE_SIZE=0 BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

```text
bxapi: BXAPI_SESSION_CACHE_SIZE: Input should be greater than or equal to 1
```

For a problem in one entry of `BXAPI_API_KEYS`, the line gives the entry's number, counting from 1, and the field's name:

```text
bxapi: BXAPI_API_KEYS, entry 1, key: the key must be at least 16 characters long
```

## Settings reference

The table summarizes the server's twelve settings and their defaults. The sections after the table give an example value for each setting and describe the setting's behavior in detail.

| Variable | Default | Description |
| --- | --- | --- |
| [`BXAPI_DATABASE`](#bxapi_database) | `bxapi.db` | The path of the SQLite database file, or `:memory:` to keep everything in memory and lose it when the server stops. |
| [`BXAPI_API_KEYS`](#bxapi_api_keys) | none | A JSON list of API keys. See [Authentication](authentication.md#configure-api-keys). |
| [`BXAPI_ALLOW_ANONYMOUS`](#bxapi_allow_anonymous) | `false` | Whether a server with no API keys may start. Such a server answers only requests that address it as `localhost`, `127.0.0.1`, or `[::1]`. |
| [`BXAPI_CORS_ORIGINS`](#bxapi_cors_origins) | none | A JSON list of the origins whose pages may call the API from a browser. |
| [`BXAPI_RATE_LIMIT_PER_MINUTE`](#bxapi_rate_limit_per_minute) | `600` | The number of requests each client may send per minute. Set it to `0` to disable the limit. |
| [`BXAPI_SESSION_CACHE_SIZE`](#bxapi_session_cache_size) | `256` | The number of *sessions* (games in progress) the server keeps loaded in memory. The minimum is 1. |
| [`BXAPI_MAX_CLIENT_BYTES`](#bxapi_max_client_bytes) | `1073741824` | The maximum number of bytes of documents the server stores for one client. Set it to `0` to disable the limit. |
| [`BXAPI_MAX_BODY_BYTES`](#bxapi_max_body_bytes) | `8388608` | The maximum size of a request body, in bytes. The minimum is 1,024. A request that takes no document, such as a command, is also limited to 65,536 bytes. |
| [`BXAPI_BODY_TIMEOUT_SECONDS`](#bxapi_body_timeout_seconds) | `30` | The number of seconds the server waits for a request's whole body to arrive. |
| [`BXAPI_DOCS_ENABLED`](#bxapi_docs_enabled) | `true` | Whether the server serves `/docs`, `/redoc`, and `/openapi.json`. |
| [`BXAPI_HOST`](#bxapi_host) | `127.0.0.1` | The address the `bxapi` command listens on. |
| [`BXAPI_PORT`](#bxapi_port) | `8640` | The port the `bxapi` command listens on, from 0 to 65535. |

### `BXAPI_DATABASE`

Set `BXAPI_DATABASE=/var/lib/bxapi/game.db` to store the data in that file. If the file doesn't exist, the server creates it, readable and writable only by the user that runs the server.

### `BXAPI_API_KEYS`

Set `BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]'` to accept one player key. With no keys, the server starts only when `BXAPI_ALLOW_ANONYMOUS` is `true`.

### `BXAPI_ALLOW_ANONYMOUS`

Set `BXAPI_ALLOW_ANONYMOUS=true` to let a server with no API keys start. That server doesn't require an API key and treats every caller as a referee, so use the setting only on your own machine. The setting has no effect when the server has any key. [Anonymous mode for local development](authentication.md#anonymous-mode-for-local-development) shows a server that runs this way.

### `BXAPI_CORS_ORIGINS`

Set `BXAPI_CORS_ORIGINS='["https://game.example"]'` to let pages from that origin call the API. An origin is a scheme, a host, and an optional port, such as `http://localhost:3000`, with no path and no trailing slash. The server doesn't accept `*`.

### `BXAPI_RATE_LIMIT_PER_MINUTE`

Set `BXAPI_RATE_LIMIT_PER_MINUTE=6000` to let each client send 6,000 requests per minute, which is 100 per second. A client can send a minute's requests at once, and it regains them at a steady rate. A request that exceeds the limit returns `429 rate_limited` with a `Retry-After` header.

The limit applies to each client name, so every key of one application shares it. Raise the limit for an application whose own server sends the requests of many players with one key.

### `BXAPI_SESSION_CACHE_SIZE`

Set `BXAPI_SESSION_CACHE_SIZE=32` to keep 32 sessions loaded. When the cache is full, the server evicts the least recently used session and loads it from the database the next time a request needs it. A smaller cache therefore saves memory but makes some requests slower.

### `BXAPI_MAX_CLIENT_BYTES`

Set `BXAPI_MAX_CLIENT_BYTES=104857600` to store at most 100 MiB of documents for each client. The count covers the client's characters, adventures, sessions, and saves, and the responses the server stores for idempotency keys. A client at the limit receives `507 storage_full` for each request that would store more, until it deletes something. `GET /v1/meta` shows a client its count and the limit.

The server counts the bytes of the documents, so the database file is larger than the sum of the counts.

### `BXAPI_MAX_BODY_BYTES`

Set `BXAPI_MAX_BODY_BYTES=1048576` to reject a request body of more than 1 MiB with `413 content_too_large`. The setting is the limit for the five requests that take a document: an adventure upload, an adventure validation, a character import, a save import, and a new session, which can contain a party document. Every other request body is also limited to 65,536 bytes, and no setting raises that limit.

A save document grows with every command, so a server that imports saves needs a limit larger than its largest save.

### `BXAPI_BODY_TIMEOUT_SECONDS`

Set `BXAPI_BODY_TIMEOUT_SECONDS=10` to give a client 10 seconds to send a request body. A client that takes longer receives `408 request_timeout`.

### `BXAPI_DOCS_ENABLED`

Set `BXAPI_DOCS_ENABLED=false` to disable the built-in documentation pages, as [Disable the built-in documentation pages](#disable-the-built-in-documentation-pages) describes.

### `BXAPI_HOST`

Set `BXAPI_HOST=0.0.0.0` to listen on every network interface of the machine, instead of only the loopback interface.

### `BXAPI_PORT`

Set `BXAPI_PORT=8700` to listen on port 8700.

## Deploy a shared server

A server that other people can reach needs API keys, a database on a backed-up disk, and a reverse proxy in front of it.

### Require API keys

Never run a shared server with `BXAPI_ALLOW_ANONYMOUS=true`. A server with no keys answers only requests that address it as `localhost`, `127.0.0.1`, or `[::1]`, and it returns `421 misdirected_request` for every other request, but a program on your network can send one of those names. Give each application its own client name and its own keys, and give a referee key only to software you trust. [Authentication](authentication.md) describes keys and roles.

### Allow browser clients

A page in a browser can call the API only from an origin that the server lists in `BXAPI_CORS_ORIGINS`. Without the setting, the server sends no cross-origin headers, and a browser blocks every call from another origin. The setting affects only calls from pages in a browser. Start a server that allows one origin:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' BXAPI_CORS_ORIGINS='["https://game.example"]' uv run bxapi
```

The server logs the allowed origins at startup:

```text
INFO:     bxapi requires an API key. Keys: 1. Clients: my-game. CORS origins: https://game.example.
```

Before the real request, a browser sends a *preflight* request, an `OPTIONS` request that asks whether the real request is allowed. A preflight request has no key, and the server answers it without one:

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

The server lists `WWW-Authenticate`, `Retry-After`, `ETag`, `Idempotent-Replayed`, and `X-Request-Id` in `access-control-expose-headers`, so a page's code can read them. A request from an origin that isn't in the list receives no `access-control-allow-origin` header, so the browser stops the page from reading the response.

Every player can read a key in a web page's code, so use a player key there and never a referee key. [Multi-user applications](authentication.md#multi-user-applications) covers the trade-offs of calling bxapi directly from a client.

### Database location and backups

The server stores everything in one SQLite file, which defaults to `bxapi.db` in its working directory. Set `BXAPI_DATABASE` to a path on a disk that's backed up. The server creates the file readable and writable only by its user, because the file contains the *master seed* of every session, the number that determines the session's dice rolls. While the server runs, SQLite also keeps `-wal` and `-shm` files beside the database file, with the same permissions.

Don't copy the file with `cp` while the server runs, because some committed data might still be in the `-wal` file. Back it up with the `.backup` command of the `sqlite3` command-line tool instead, which makes a consistent copy while the server runs. If you set `BXAPI_DATABASE`, use that path in place of `bxapi.db`:

```sh
sqlite3 bxapi.db ".backup bxapi-backup.db"
```

The backup is a normal SQLite file. The `.backup` command creates it with your user's default permissions, which might let other users on the machine read it, so restrict the file's permissions:

```sh
chmod 600 bxapi-backup.db
```

Plan disk space for the database file. The file grows by at most `BXAPI_MAX_CLIENT_BYTES` of documents for each client name in `BXAPI_API_KEYS`, plus one document for each client and the space SQLite uses for rows and indexes. Give the disk room for that total, or lower the setting.

To restore a backup, stop the server, put the backup where `BXAPI_DATABASE` points, and start the server. The server exits at startup if it doesn't recognize the file's table layout, and its message states the path and the versions.

### Reverse proxy

Run a deployed server behind a *reverse proxy*, a server such as nginx or Caddy that accepts the connections from the internet and forwards the requests to bxapi. Configure the proxy to cover what bxapi doesn't enforce:

- **Header and idle timeouts:** bxapi limits how long a request's body takes to arrive, with `BXAPI_BODY_TIMEOUT_SECONDS`, but it can't limit how long a client takes to send a request's headers or how long a connection stays open between requests. Set the proxy to enforce timeouts for headers and idle connections.
- **TLS and body size:** Set the proxy to terminate TLS and to limit the body size.
- **Per-IP rate limits:** bxapi limits requests per client name, with `BXAPI_RATE_LIMIT_PER_MINUTE`, not per IP address. Set the proxy to limit how many requests and connections one IP address may make.
- **Network exposure:** Keep bxapi on the default `BXAPI_HOST` of `127.0.0.1`, so that only the proxy on the same machine can reach it, or bind it to an address on a private network.

### Request logging

The `bxapi` command logs one line for each request when it sends the response:

```text
INFO:     request_id=req_Zk3uY0mWq1x8Jr5cTt2aLw method=POST path=/v1/sessions/ses_BVmUCi5oRTQ0qSr1NadnJw/commands status=200 duration_ms=2.3 client=my-game
```

The `request_id`, `duration_ms`, and `client` fields have these meanings:

- `request_id`: The ID in the response's `X-Request-Id` header and in a problem document's `request_id`, so a client developer who reports an error can give you the ID to search for.
- `duration_ms`: The time the server took.
- `client`: The client name of the request's API key, or `-` for a request with no valid key.

The line never contains a key, a query string, or a body. When a request fails with `500 internal_error`, the server also logs the traceback, in a line that states the same ID.

A client can send its own `X-Request-Id`, so don't treat an ID as unique or as proof of who sent a request.

The server writes the line to the Python logger `bxapi.access` at the `INFO` level. If you serve the application with another ASGI server instead of the `bxapi` command, configure the `bxapi` logger to see the line.

### Multiple server processes

The `bxapi` command runs one process. You can start several processes against one database file, on different ports behind your proxy. The processes coordinate through the database: each process checks the session's *revision*, the count of commands the session has accepted, in the database before it uses a loaded session, and it writes a command only if the revision hasn't changed since the process read it. Two processes can therefore serve the same session without losing or repeating a command.

Running more than one process changes how the server behaves in these ways:

- **Session write conflicts:** When another process writes the session after a request reads it and before the request writes it, the command has no effect, and the client receives `409 session_busy` with the session's current `revision`. A client handles the error as [Errors](errors.md#session_busy) describes: it reads the session if it sent `expected_revision`, and otherwise sends the command again. A client that sends `expected_revision` with every command never repeats a command by mistake.
- **Database write lock:** Processes take turns writing to the database. A process that waits more than 5 seconds for the write lock returns `503 database_busy` with the header `Retry-After: 1`. Have your client wait the number of seconds in `Retry-After` and send the request again.
- **Rate limits:** Each process counts a client's requests in memory, so with two processes a client can send twice `BXAPI_RATE_LIMIT_PER_MINUTE`.
- **Storage limit:** The count of a client's stored bytes is in the database, so the processes share it, and `BXAPI_MAX_CLIENT_BYTES` is the limit for all of them together.
- **API keys:** All processes need the same keys, so give them the same `BXAPI_API_KEYS`.
- **Versions:** Run every process from the same version of bxapi. If a process with a different release of osrlib, the rules engine, wrote a session, a process that can't load that session returns `500 session_unreadable`.

### Disable the built-in documentation pages

The pages at `/docs` and `/redoc` and the OpenAPI document at `/openapi.json` don't require a key, so anyone who can reach the server can read the API's description. Each page loads its script from `cdn.jsdelivr.net`, so a browser that opens a page contacts that host. The server pins the script's version and hash, and the browser rejects any other file. Set `BXAPI_DOCS_ENABLED=false` to disable the pages and the document:

```sh
BXAPI_API_KEYS='[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]' BXAPI_DOCS_ENABLED=false uv run bxapi
```

The three paths then return a `404`:

```sh
curl -si http://127.0.0.1:8640/docs
```

```text
HTTP/1.1 404 Not Found
...
```

You can still get the OpenAPI document without a running server, as [the README](../README.md#api-reference) shows.

## Next steps

- [Authentication](authentication.md): create keys and choose roles.
- [Errors](errors.md): how a client handles `408`, `409`, `413`, and `503` responses.
- [Play a game](play-a-game.md): walk through one game, one request at a time.
