# bxapi

bxapi is an HTTP API for the 1981 B/X (Basic/Expert) fantasy adventure game rules, built with [FastAPI](https://fastapi.tiangolo.com/) on [osrlib](https://github.com/mmacy/osrlib-python). Your application renders the interface and reads player input, while bxapi rolls dice, enforces B/X rules, stores characters, adventures (the towns, dungeons, and encounters a party plays), sessions (active games in progress), and saved snapshots in SQLite, and serializes requests to each active game. You can query rules catalogs, roll dice, and generate characters, treasure, rooms, or monsters without storing state, or run full dungeon-crawl sessions from character creation through combat.

## Prerequisites

- [git](https://git-scm.com/) to clone the repository.
- [uv](https://docs.astral.sh/uv/) to install dependencies and run the server. If Python 3.14 isn't installed, uv downloads it automatically.
- [curl](https://curl.se/) to send test requests from a terminal.

## Get it running

1. Clone the repository and start the server in local anonymous mode:

   ```sh
   git clone https://github.com/mmacy/bxapi.git
   cd bxapi
   BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
   ```

   The first run takes longer while uv downloads Python 3.14 and installs dependencies. When startup finishes, the server listens on <http://127.0.0.1:8640>.

   Without configured API keys, bxapi starts only when `BXAPI_ALLOW_ANONYMOUS` is `true`. In that mode, the server requires no key, assigns callers to the `local` client with the `referee` role (which exposes hidden game state and referee commands), and accepts `/v1` requests only when the `Host` header is `localhost`, `127.0.0.1`, or `[::1]`. Use `BXAPI_ALLOW_ANONYMOUS=true` only on your workstation. Before exposing a server on a network, configure API keys as described in [Configure and deploy](docs/configuration.md#deploy-for-other-callers).

2. In a second terminal, check that the server is listening:

   ```sh
   curl -s http://127.0.0.1:8640/health
   ```

   ```json
   {"status":"ok"}
   ```

3. Send a seeded roll to [`POST /v1/dice/roll`](https://mmacy.github.io/bxapi/reference/endpoint/dice#operation-roll_dice):

   ```sh
   curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Content-Type: application/json" -d '{"expression": "3d6", "seed": 7}'
   ```

   ```json
   {"expression":"3d6","seed":7,"rolls":[{"rolls":[3,2,4],"modifier":0,"multiplier":1,"total":9}]}
   ```

   Passing `seed` reproduces the same roll on every run, whereas omitting `seed` lets the server pick and return a random one.

To create a party, enter the built-in dungeon, and fight a battle step by step, follow [Play a game](docs/play-a-game.md), or run [`examples/play_barrow.py`](examples/README.md) to watch a script play the first battle automatically.

## API reference and OpenAPI

The [API reference](https://mmacy.github.io/bxapi/reference/) documents every endpoint, parameter, request and response schema, convention, and error code, with a tested `curl` command for each operation.

A running server also serves interactive Swagger UI documentation at <http://127.0.0.1:8640/docs>, ReDoc at <http://127.0.0.1:8640/redoc>, and the raw OpenAPI 3.1 specification at <http://127.0.0.1:8640/openapi.json>:

```sh
curl -s http://127.0.0.1:8640/openapi.json > openapi.json
```

To export the same OpenAPI document without starting a server, run `bxapi.openapi`. It builds the application with fixed in-memory settings, ignores `BXAPI_` environment variables and `.env`, and writes the JSON specification to standard output:

```sh
uv run python -m bxapi.openapi > openapi.json
```

## Guides

Read the guides and reference on GitHub or at <https://mmacy.github.io/bxapi/>:

- [Play a game](docs/play-a-game.md): step-by-step `curl` walkthrough from rolling ability scores through a saved battle.
- [Build a client](docs/client-guide.md): key topologies, game-loop state, retries and concurrency, custom adventures, and referee workflows.
- [Configure and deploy](docs/configuration.md): `BXAPI_` environment variables, API key generation, CORS, SQLite backups, reverse proxies, and multi-process setups.
- [Example client](examples/README.md): standard-library Python script that plays the first battle of `builtin-barrow`.
- [API reference](https://mmacy.github.io/bxapi/reference/): every endpoint, request and response schema, HTTP convention, and error code.

## Contribute

Every pull request must pass these checks:

```sh
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

If you change documentation, routes, models, or OpenAPI examples, build the Docusaurus site in `website/` as well. Building the site requires [Node.js](https://nodejs.org/) 22 or later:

```sh
cd website
npm ci
npm run build
```

Read [AGENTS.md](AGENTS.md) for contributor rules and [docs/spec.md](docs/spec.md) for the API contract.

## License

The code is licensed under the [MIT license](LICENSE).

The rules content served by the API comes from osrlib under the Open Game License 1.0a. [`GET /v1/rules/license`](https://mmacy.github.io/bxapi/reference/endpoint/rules#operation-get_license) returns the license text and Section 15 copyright notice.

bxapi is an independent project and is not affiliated with or endorsed by Necrotic Gnome. "Old-School Essentials" is a trademark of Necrotic Gnome, used here only to identify the source document of the rules content. bxapi makes no claim of compatibility.
