# bxapi

bxapi is an HTTP API for the rules of B/X (Basic/Expert), a fantasy adventure game published in 1981. Use it as the JSON backend for a game or utility that you write: look up monsters, spells, and equipment, roll dice, generate treasure, create characters, or run a whole dungeon crawl.

A *dungeon crawl* is a game in which a party of characters explores a dungeon. bxapi starts the game, applies your players' commands under the B/X rules, stores each game in its database, and returns what the players see. bxapi is built with [FastAPI](https://fastapi.tiangolo.com/) on [osrlib](https://github.com/mmacy/osrlib-python), a Python rules engine that applies every rule, so your application doesn't implement any of them.

## Prerequisites

Install these three tools before you start:

- [git](https://git-scm.com/) to clone the repository.
- [uv](https://docs.astral.sh/uv/) to install and run bxapi. uv downloads Python 3.14 if you don't have it.
- [curl](https://curl.se/) to send requests to the API from a terminal.

## Start the server

Clone the repository and start the server:

```sh
git clone https://github.com/mmacy/bxapi.git
cd bxapi
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server listens on <http://127.0.0.1:8640>. The first `uv run bxapi` takes longer to start, because uv installs the dependencies, and Python 3.14 if you don't have it.

`BXAPI_ALLOW_ANONYMOUS=true` starts the server in *anonymous mode*, and a server with no API keys won't start without it. A server in anonymous mode doesn't require an API key and treats every caller as a *referee*, a caller who may see a game's hidden state and change the game directly. Use anonymous mode only on your own machine. A server in anonymous mode answers only requests addressed to `localhost`, `127.0.0.1`, or `[::1]`. To run a server for other people, see [Deploy a shared server](docs/configuration.md#deploy-a-shared-server).

In a second terminal, confirm that the server responds:

```sh
curl -s http://127.0.0.1:8640/health
```

The server returns `{"status":"ok"}`.

## Make your first request

Roll three six-sided dice with the dice endpoint. The endpoint accepts an optional `seed`, a number that determines the roll. The same seed always produces the same roll, so this request returns the response shown here. If you omit `seed`, the server picks a random seed and returns it.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Content-Type: application/json" -d '{"expression": "3d6", "seed": 7}'
```

```json
{"expression":"3d6","seed":7,"rolls":[{"rolls":[3,2,4],"modifier":0,"multiplier":1,"total":9}]}
```

Next, look up a monster. The `q` parameter matches part of a monster's ID or name:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/monsters?q=goblin&limit=1"
```

```json
{"items":[{"id":"goblin","name":"Goblin","hit_dice":{"count":1,"die":8,"modifier":-1,"asterisks":0,"average_hp":3,"fixed_hp":null},"ac":6,"ac_ascending":13,"xp":5,"alignment":{"options":["chaotic"],"usual":null}}],"total":2,"limit":1,"offset":0}
```

## Play a game

Four requests create a character, start a game, enter a dungeon, and move one step inside it. The game uses the built-in *adventure*. An adventure is the town, dungeons, monsters, and treasure that a game is played in.

The server returns an ID for each resource it creates, and you pass that ID in the next request. Your IDs differ from the ones shown here, so replace each ID in a `curl` command with the one your server returned. The server returns compact JSON, and the responses here are shortened with `...`.

Create a character. The server rolls the character's *ability scores*, the six numbers such as strength and dexterity that the rules use for the character, so your response differs from the one shown here:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}'
```

```json
{"id":"chr_YT8cPTCkC64HI8WyN-Vq6A","name":"Brenna","class_id":"fighter","level":1,"created_at":"2026-11-21T05:25:55Z","character":{...},"derived":{...}}
```

Start a *session*, which is a single game in progress. This request makes the character you created the session's *party*, the group of characters that plays, and sets the session's adventure to `builtin-barrow`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_YT8cPTCkC64HI8WyN-Vq6A"]}'
```

```json
{"id":"ses_BVmUCi5oRTQ0qSr1NadnJw","name":"The Barrow of the Forgotten King","adventure_id":"builtin-barrow","adventure_name":"The Barrow of the Forgotten King","mode":"town","ended":false,"revision":0,...}
```

The session starts in town. Send the command that enters the dungeon:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_BVmUCi5oRTQ0qSr1NadnJw/commands -H "Content-Type: application/json" -d '{"command_type": "enter_dungeon", "dungeon_id": "barrow"}'
```

```json
{"accepted":true,"rejections":[],"events":[...],"messages":["The party enters dungeon barrow (level 1).","A new quest: The Jade Idol. ..."],"mode":"exploring","ended":false,"revision":1}
```

Move the party one cell east:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions/ses_BVmUCi5oRTQ0qSr1NadnJw/commands -H "Content-Type: application/json" -d '{"command_type": "move_party", "direction": "east"}'
```

```json
{"accepted":true,"rejections":[],"events":[...],"messages":["The party moves to (1, 0), facing east."],"mode":"exploring","ended":false,"revision":2}
```

You act as a referee in these requests, because the server runs in anonymous mode. A game client normally sends a key with the *player* role, and the server shows that client only what a player may see. [Play a game](docs/play-a-game.md) repeats these steps with a player key and continues through a battle, a save, and a restore.

## API reference

The documentation site has a reference page for every endpoint at <https://mmacy.github.io/bxapi/reference/>, each with a tested `curl` command.

A running server also documents every endpoint. The page at <http://127.0.0.1:8640/docs> has a form for trying each endpoint, and the page at <http://127.0.0.1:8640/redoc> shows the same reference in a second layout. Both pages are built from the server's OpenAPI document, a machine-readable description of the API that a tool can use to generate a client. Fetch the document from a running server:

```sh
curl -s http://127.0.0.1:8640/openapi.json
```

To get the same document without a running server, run the `bxapi.openapi` module. The module builds the bxapi application with fixed settings, reads no environment variables or files, and prints the document to standard output:

```sh
uv run python -m bxapi.openapi > openapi.json
```

## Documentation

The documentation site at <https://mmacy.github.io/bxapi/> hosts the API reference and these guides:

- [Play a game](docs/play-a-game.md): a walkthrough of one game, from an ability roll to a saved battle.
- [Authentication](docs/authentication.md): API keys, roles, and which client's data a key can reach.
- [Errors](docs/errors.md): the error format, every error code, and how to handle each one.
- [Rules reference and generators](docs/reference-and-generators.md): look up rules content, roll dice, and generate treasure and monsters.
- [Adventures](docs/adventures.md): upload and validate your own adventure.
- [Run games as a referee](docs/referee.md): the capabilities that a referee key adds.
- [Configuration](docs/configuration.md): every setting, and how to deploy a server for other people.
- [Example client](examples/README.md): a Python script that plays the first battle of the built-in adventure.

## Contribute

Every pull request must pass these four checks:

```sh
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

A pull request must also build the documentation site in `website/`, which combines these guides with an API reference generated from the OpenAPI document. The build requires [Node.js](https://nodejs.org/) 22 or later:

```sh
cd website
npm ci
npm run build
```

[AGENTS.md](AGENTS.md) contains the rules for working in the repository, and [docs/spec.md](docs/spec.md) is the API's contract, which every endpoint must match.

## License

The code is licensed under the [MIT license](LICENSE).

The rules content that the API serves comes from osrlib. That content is Open Game Content under the Open Game License 1.0a, and `GET /v1/rules/license` returns the license with its Section 15 copyright notice.

bxapi is an independent project, not affiliated with or endorsed by Necrotic Gnome. "Old-School Essentials" is a trademark of Necrotic Gnome, used here only to identify the source document of the rules content. bxapi makes no claim of compatibility.
