# Example client

`play_barrow.py` plays the first battle of the built-in adventure against a bxapi server. It uses only the Python standard library, so it needs no installation beyond Python 3.14, which `uv` provides.

The script creates a party of a fighter, a cleric, and a thief, starts `builtin-barrow`, enters the dungeon, and sends commands until the first battle ends or it has sent 40 commands. It prints each command and each message that the server returns. It reads `GET /v1/sessions/{session_id}/commands` before every command and sends only a command from that list, and it reads the player view to pick a direction to walk and an action for each party member in battle.

## Run it

Start a server, as the [README](../README.md#get-it-running) shows, then run the script from the repository's root with the server's URL:

```sh
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640
```

The URL is optional and defaults to `http://127.0.0.1:8640`. For a server that asks for an API key, pass a player key:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --api-key player-key-0123456789
```

`--max-commands` changes the limit of 40 commands:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --max-commands 10
```

The dice decide the battle, so each run prints different messages. The output ends with a line like `The battle is over. The session is in mode exploring.`, or `game_over` when the party loses.

## Next steps

- [Play a game](../docs/play-a-game.md): the same game, one request at a time.
