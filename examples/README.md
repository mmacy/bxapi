# Example client

[`play_barrow.py`](play_barrow.py) is a Python script that sends requests to a running bxapi server. It plays the adventure that comes with bxapi, `builtin-barrow`, up to the end of the first battle, the party's first fight with monsters. It uses only the Python standard library, so once you've set up bxapi as the [README](../README.md#get-it-running) shows, you have everything the script needs.

The script creates a *party* of three characters: a fighter, a cleric, and a thief. It starts a *session*, one game in progress, of `builtin-barrow`, enters the dungeon, and sends commands until the first battle ends or it has sent 40 commands. It prints each command and each message that the server returns. Before every command, it reads the legal commands from `GET /v1/sessions/{session_id}/commands` and sends only a command from that list. It also reads the *player view*, everything the players may know about the game, to pick a direction to walk and an action for each party member in battle.

## Run it

In one terminal, from the repository's root, start a server:

```sh
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server listens on `http://127.0.0.1:8640` by default. In a second terminal, from the repository's root, run the script with the server's URL:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640
```

The URL is optional and defaults to `http://127.0.0.1:8640`. To set up API keys, see [Authentication](../docs/authentication.md). For a server that asks for a key, pass a key with the player role:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --api-key player-key-0123456789
```

To change the limit of 40 commands, pass `--max-commands`:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --max-commands 10
```

The dice are random, so each run prints different messages. The output ends with a line like `The battle is over. The session is in mode exploring.` When the party loses, the line ends with the mode `game_over` instead.

## Next steps

- [Play a game](../docs/play-a-game.md): the same game, one request at a time.
