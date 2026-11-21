# bxapi

bxapi is an HTTP API for the rules of B/X (Basic/Expert), a fantasy adventure game published in 1981. A game or utility that you write sends it JSON requests and gets JSON back. You can look up monsters, spells, and equipment, roll dice, generate treasure, and create characters. You can also run a whole dungeon crawl, a game in which a party of characters explores a dungeon. For a dungeon crawl, bxapi starts the game, applies your players' commands under the B/X rules, stores each game in its database, and returns what the players see. It's built with [FastAPI](https://fastapi.tiangolo.com/) on [osrlib](https://github.com/mmacy/osrlib-python), a Python rules engine that applies every rule, so your application doesn't implement any.

## Prerequisites

- [git](https://git-scm.com/) to clone the repository.
- [uv](https://docs.astral.sh/uv/) to install and run bxapi. uv downloads Python 3.14 if you don't have it.
- [curl](https://curl.se/) to try the API from a terminal.

## Get it running

Clone the repository and start the server:

```sh
git clone https://github.com/mmacy/bxapi.git
cd bxapi
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server listens on <http://127.0.0.1:8640>. The first time you run `uv run bxapi`, it takes longer to start, because uv installs the dependencies, and Python 3.14 if you don't have it.

A server with no API keys refuses to start unless `BXAPI_ALLOW_ANONYMOUS` is `true`. With that setting, the server asks for no key and treats every caller as a *referee*, a caller who may see a game's hidden state and change the game directly. Use the setting only on your own machine. To run a server for other people, see [Run it for other people](docs/configuration.md#run-it-for-other-people).

In a second terminal, check that the server answers:

```sh
curl -s http://127.0.0.1:8640/health
```

The server returns `{"status":"ok"}`.

## Make a first request

Roll three six-sided dice. The dice endpoint accepts an optional `seed`, a number that determines the roll. The same seed gives the same roll, so you get the response shown here. If you leave `seed` out, the server picks a random one and returns it.

```sh
curl -s -X POST http://127.0.0.1:8640/v1/dice/roll -H "Content-Type: application/json" -d '{"expression": "3d6", "seed": 7}'
```

```json
{"expression":"3d6","seed":7,"rolls":[{"rolls":[3,2,4],"modifier":0,"multiplier":1,"total":9}]}
```

Look up a monster. The `q` parameter matches part of a monster's id or name:

```sh
curl -s "http://127.0.0.1:8640/v1/rules/monsters?q=goblin&limit=1"
```

```json
{"items":[{"id":"goblin","name":"Goblin","hit_dice":{"count":1,"die":8,"modifier":-1,"asterisks":0,"average_hp":3,"fixed_hp":null},"ac":6,"ac_ascending":13,"xp":5,"alignment":{"options":["chaotic"],"usual":null}}],"total":2,"limit":1,"offset":0}
```

## Play a game

With four requests, you create a character, start a game, enter a dungeon, and move one step inside it. The game uses the *adventure* that comes with the server. An adventure is the town, dungeons, monsters, and treasure that a game is played in. The server returns an id for each thing it creates, and you pass that id in the next request. Your ids differ from the ones shown here, so replace each id in a `curl` command with the one your server returned. The server returns compact JSON, and we've shortened the responses here with `...`.

Create a character. The server rolls the character's *ability scores*, the six numbers like strength and dexterity that the rules use for the character, so your response differs from the one shown here:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/characters -H "Content-Type: application/json" -d '{"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}'
```

```json
{"id":"chr_YT8cPTCkC64HI8WyN-Vq6A","name":"Brenna","class_id":"fighter","level":1,"created_at":"2026-11-21T05:25:55Z","character":{...},"derived":{...}}
```

Start a *session*, which is one game in progress. Its *party*, the group of characters that plays, is the character you created, and its adventure is `builtin-barrow`:

```sh
curl -s -X POST http://127.0.0.1:8640/v1/sessions -H "Content-Type: application/json" -d '{"adventure_id": "builtin-barrow", "character_ids": ["chr_YT8cPTCkC64HI8WyN-Vq6A"]}'
```

```json
{"id":"ses_BVmUCi5oRTQ0qSr1NadnJw","name":"The Barrow of the Forgotten King","adventure_id":"builtin-barrow","adventure_name":"The Barrow of the Forgotten King","mode":"town","ended":false,"revision":0,...}
```

The session starts in town. Send it the command to enter the dungeon:

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

On a server that allows anonymous callers, you act as a referee. A game client normally sends a key with the *player* role, and the server shows that client only what a player may see. [Play a game](docs/play-a-game.md) repeats these steps with a player key and goes on through a battle, a save, and a restore.

## Read the API reference

The running server documents every endpoint at <http://127.0.0.1:8640/docs>, with a form to try each one, and at <http://127.0.0.1:8640/redoc>. Both pages are built from the server's OpenAPI document, a machine-readable description of the API that a tool can use to generate a client:

```sh
curl -s http://127.0.0.1:8640/openapi.json
```

To get the same document without a running server, run the `bxapi.openapi` module. It builds the bxapi application with fixed settings, reads no environment variable and no file, and prints the document to standard output:

```sh
uv run python -m bxapi.openapi > openapi.json
```

## Documentation

- [Play a game](docs/play-a-game.md): a walkthrough of one game, from an ability roll to a saved battle.
- [Authentication](docs/authentication.md): API keys, roles, and whose data a key can reach.
- [Errors](docs/errors.md): the error format, every error code, and what to do about each.
- [Rules reference and generators](docs/reference-and-generators.md): look up rules content, roll dice, and generate treasure and monsters.
- [Adventures](docs/adventures.md): upload and validate your own adventure.
- [Run games as a referee](docs/referee.md): what a referee key adds.
- [Configuration](docs/configuration.md): every setting, and how to run a server for other people.
- [Example client](examples/README.md): a Python script that plays the first battle of the built-in adventure.

## Contribute

A pull request must pass these four commands:

```sh
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

[AGENTS.md](AGENTS.md) has the rules for working in the repository, and [docs/spec.md](docs/spec.md) is the API's contract, which every endpoint must match.

## License

The code is licensed under the [MIT license](LICENSE).

The rules content that the API serves comes from osrlib. It's Open Game Content under the Open Game License 1.0a, which `GET /v1/rules/license` returns with its Section 15 copyright notice.

bxapi is an independent project, not affiliated with or endorsed by Necrotic Gnome. "Old-School Essentials" is a trademark of Necrotic Gnome, used here only to identify the source document of the rules content. bxapi makes no claim of compatibility.
