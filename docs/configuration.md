# Configure and deploy

Configure a bxapi server with `BXAPI_` environment variables, issue API keys for your client applications, and harden the server before exposing it over a network.

## Configure settings

bxapi reads every setting once at startup from `BXAPI_` environment variables, falling back to a `.env` file in the working directory (`NAME=value`, one per line) for any variable not set in the environment. Restart the server after changing a setting. Write list-valued settings (`BXAPI_API_KEYS` and `BXAPI_CORS_ORIGINS`) as JSON:

```text
BXAPI_PORT=8700
BXAPI_DATABASE=/var/lib/bxapi/game.db
BXAPI_API_KEYS=[{"client": "my-game", "key": "player-key-0123456789", "role": "player"}]
```

If any setting fails validation, the server exits with status `1` and prints the variable name and error to standard error without echoing secret values:

```sh
BXAPI_SESSION_CACHE_SIZE=0 BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

```text
bxapi: BXAPI_SESSION_CACHE_SIZE: Input should be greater than or equal to 1
```

For an invalid entry in `BXAPI_API_KEYS`, the error identifies the 1-based entry index and field name (`bxapi: BXAPI_API_KEYS, entry 1, key: the key must be at least 16 characters long`).

| Variable | Default | Description |
| --- | --- | --- |
| `BXAPI_DATABASE` | `bxapi.db` | Path to the SQLite database file, or `:memory:` for an ephemeral in-memory database lost on shutdown. When creating a file, the server sets permissions to `0600` (owner read/write only). |
| `BXAPI_API_KEYS` | none | JSON array of API key objects (`client`, `key`, and optional `role`). Required unless `BXAPI_ALLOW_ANONYMOUS` is `true`. |
| `BXAPI_ALLOW_ANONYMOUS` | `false` | Allows startup when `BXAPI_API_KEYS` is empty, treating every caller as `referee` for client `local` and restricting `/v1` requests to `Host` values `localhost`, `127.0.0.1`, or `[::1]`. Ignored when `BXAPI_API_KEYS` contains any key. |
| `BXAPI_CORS_ORIGINS` | none | JSON array of allowed browser origins (`scheme://host[:port]`, with no path or trailing slash. The server rejects `*`.) When unset, the server sends no CORS headers. |
| `BXAPI_RATE_LIMIT_PER_MINUTE` | `600` | Per-process token-bucket request limit per minute for each `client` name under `/v1` (unauthenticated requests share one bucket of the same size). Set to `0` to disable. |
| `BXAPI_SESSION_CACHE_SIZE` | `256` | Maximum active sessions kept in memory per process (minimum `1`). Every accepted command writes through to SQLite immediately. Eviction drops the least-recently-used in-memory copy and reloads it from SQLite on demand. |
| `BXAPI_MAX_CLIENT_BYTES` | `1073741824` | Maximum stored document bytes per `client` across ability rolls, characters, adventures (info and document), sessions, saves, and 24-hour idempotency replay bodies (`1` GiB by default, shared across processes via SQLite). Set to `0` to disable. |
| `BXAPI_MAX_BODY_BYTES` | `8388608` | Maximum request body size in bytes (`8` MiB by default, minimum `1024`) for the five operations that accept osrlib documents: [`POST /v1/adventures`](https://mmacy.github.io/bxapi/reference/endpoint/adventures#operation-upload_adventure), [`POST /v1/adventures/validate`](https://mmacy.github.io/bxapi/reference/endpoint/adventures#operation-validate_adventure), [`POST /v1/characters/import`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-import_character), [`POST /v1/saves/import`](https://mmacy.github.io/bxapi/reference/endpoint/saves#operation-import_save), and [`POST /v1/sessions`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-create_session). The server caps all other request bodies at the smaller of `65536` bytes or `BXAPI_MAX_BODY_BYTES`. Save documents grow as sessions accumulate events, so size this setting above your largest imported save. |
| `BXAPI_BODY_TIMEOUT_SECONDS` | `30` | Maximum seconds allowed for a request body to arrive once reading starts (greater than `0`). Slower uploads fail with 408 `request_timeout`. |
| `BXAPI_DOCS_ENABLED` | `true` | Serves unauthenticated API docs at `/docs`, `/redoc`, and `/openapi.json` when `true`, or returns 404 on all three paths when `false`. |
| `BXAPI_HOST` | `127.0.0.1` | Bind address for the `bxapi` CLI command (`0.0.0.0` listens on all interfaces). |
| `BXAPI_PORT` | `8640` | Bind port for the `bxapi` CLI command (`0` to `65535`). |

## Generate and configure API keys

Each object in `BXAPI_API_KEYS` configures one bearer token:

- `client`: application identifier (`1` to `64` characters matching `^[A-Za-z0-9][A-Za-z0-9._-]*$`). Keys with the same `client` share stored resources, rate limits, and storage quotas (see [Build a client](client-guide.md#choose-a-key-and-client-topology)).
- `key`: secret string of at least `16` printable ASCII characters (`0x21` to `0x7E`) with no spaces. Every entry in `BXAPI_API_KEYS` must have a distinct `key`.
- `role`: `"player"` (default) or `"referee"`.

1. Generate a random secret and store it in a shell variable:

   ```sh
   PLAYER_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
   echo "$PLAYER_KEY"
   ```

2. Pass the key in `BXAPI_API_KEYS` (or write it into `.env`) and start the server:

   ```sh
   BXAPI_API_KEYS="[{\"client\": \"my-game\", \"key\": \"$PLAYER_KEY\", \"role\": \"player\"}]" uv run bxapi
   ```

   At startup, bxapi logs the key count, client names, and allowed CORS origins without logging any secret:

   ```text
   INFO:     bxapi requires an API key. Keys: 1. Clients: my-game. CORS origins: none.
   ```

3. From a second terminal (with `PLAYER_KEY` exported to the same secret), verify that `/health` answers without a key, `/v1/meta` rejects unauthenticated requests with `401`, and your bearer token resolves to `my-game` with the `player` role:

   ```sh
   curl -sS -w '\n' http://127.0.0.1:8640/health
   curl -sSi http://127.0.0.1:8640/v1/meta | head -n 1
   curl -sS -w '\n' http://127.0.0.1:8640/v1/meta -H "Authorization: Bearer $PLAYER_KEY"
   ```

When you run locally with `BXAPI_ALLOW_ANONYMOUS=true` and no `BXAPI_API_KEYS`, the server checks the `Host` header on `/v1` requests and rejects anything other than `localhost`, `127.0.0.1`, or `[::1]` with 421 `misdirected_request`. That check blocks DNS-rebinding attacks from untrusted websites open in your local browser, which send their domain in `Host`. When you configure `BXAPI_API_KEYS`, bxapi accepts any `Host` header because unauthenticated requests fail with 401 `unauthorized`.

## Deploy for other callers

### Put a reverse proxy in front

Never run a shared server with `BXAPI_ALLOW_ANONYMOUS=true` (any local process or network peer can forge `Host: localhost` if the port is reachable). Keep `BXAPI_HOST` set to `127.0.0.1` (or a private interface) and place a reverse proxy such as nginx or Caddy in front of bxapi to handle:

- **TLS termination** so clients don't send bearer tokens in cleartext.
- **Header and idle-connection timeouts**, because `BXAPI_BODY_TIMEOUT_SECONDS` bounds only request-body reads after headers arrive.
- **Per-IP connection and request rate limits**, because `BXAPI_RATE_LIMIT_PER_MINUTE` throttles by authenticated `client` name (with one shared bucket for invalid keys) rather than by client IP address.

### Allow browser origins

If a browser single-page application calls bxapi directly, list its exact origin (`scheme://host[:port]`) in `BXAPI_CORS_ORIGINS`:

```sh
BXAPI_API_KEYS="[{\"client\": \"my-game\", \"key\": \"$PLAYER_KEY\", \"role\": \"player\"}]" BXAPI_CORS_ORIGINS='["https://game.example"]' uv run bxapi
```

bxapi answers unauthenticated `OPTIONS` preflight requests automatically and exposes `WWW-Authenticate`, `Retry-After`, `ETag`, `Idempotent-Replayed`, and `X-Request-Id` to browser scripts via `Access-Control-Expose-Headers`. Embed only a `player` key in browser code, never a `referee` key.

### Store and back up the SQLite database

Create dedicated database and backup directories owned by the service account with `0700` permissions, and set `BXAPI_DATABASE` to a file inside the database directory. Because the database stores each session's master seed, bxapi creates new database files (along with SQLite's `-wal` and `-shm` write-ahead log files while running) with `0600` permissions, whereas existing or restored files keep their current permissions. Plan disk capacity for `BXAPI_MAX_CLIENT_BYTES` per configured `client`, plus one extra document per client and SQLite table and index overhead.

Don't copy a live database with `cp`, which can miss committed frames still in `-wal`. Run SQLite's online `.backup` command inside your `0700` backup directory under a restrictive `umask` so the backup file is never world-readable while being written:

```sh
(umask 077 && sqlite3 /var/lib/bxapi/game.db ".backup /var/backups/bxapi/game-backup.db")
```

To restore from a backup, stop all bxapi processes, copy the backup to `BXAPI_DATABASE` with `0600` permissions owned by the service account, and start the server again. bxapi performs no automatic schema migrations: if a database file's `PRAGMA user_version` doesn't match the installed release's table layout version, startup fails with an error naming the path and versions.

### Read access logs and trace errors

When started with the `bxapi` command, the server emits one `INFO` line per completed request to the `bxapi.access` logger:

```text
INFO:     request_id=req_Zk3uY0mWq1x8Jr5cTt2aLw method=POST path=/v1/sessions/ses_BVmUCi5oRTQ0qSr1NadnJw/commands status=200 duration_ms=2.3 client=my-game
```

Access logs omit API keys, query strings, and bodies, and print `client=-` when authentication fails. On a 500 `internal_error`, the server logs the Python traceback with the same `request_id`. Because callers can supply a custom `X-Request-Id` header, treat `request_id` as a correlation handle rather than a unique or authenticated identifier.

If you run the ASGI application factory directly (for example, `uvicorn bxapi.app:create_app --factory --port 8640`) instead of `uv run bxapi`, configure Python's `bxapi` logger at level `INFO` in your server logging configuration so uvicorn emits startup and access lines.

### Run multiple server processes

The `bxapi` command runs a single process. To scale on one machine, run multiple processes on separate ports behind your reverse proxy against the same local SQLite database file (never place a SQLite WAL database on a network filesystem such as NFS or SMB, and independent hosts don't share state):

- **Session concurrency:** Before executing a command, each process verifies the session's `revision` in SQLite and commits only if no other process updated the row in the meantime. If another process wrote first, the losing request makes no changes and returns 409 `session_busy` with the current `revision`.
- **SQLite write locks:** Processes serialize database writes and wait up to 5 seconds for the write lock before returning 503 `database_busy` with `Retry-After: 1`.
- **Quotas and rate limits:** SQLite tracks `BXAPI_MAX_CLIENT_BYTES` across all processes, whereas each process tracks `BXAPI_RATE_LIMIT_PER_MINUTE` in memory (so *N* processes allow up to *N* times `BXAPI_RATE_LIMIT_PER_MINUTE` requests per minute).
- **Version parity:** Configure every process with the exact same `BXAPI_API_KEYS`, bxapi version, and osrlib version. If one process writes a session with an incompatible osrlib version, another process that can't deserialize it returns 500 `session_unreadable`.

### Disable public API docs

`/docs`, `/redoc`, and `/openapi.json` require no API key (the HTML pages load version-pinned, Subresource-Integrity-hashed bundles from `cdn.jsdelivr.net` under a strict `Content-Security-Policy`). Set `BXAPI_DOCS_ENABLED=false` if you don't want a public deployment to serve them. You can still export `openapi.json` offline with `uv run python -m bxapi.openapi > openapi.json`.
