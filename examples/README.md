# Example client

[`play_barrow.py`](play_barrow.py) is a Python script that plays the built-in adventure, `builtin-barrow`, against a running bxapi server, up to the end of the first battle, the party's first fight with monsters. The script uses only the Python standard library, so once you've set up bxapi as the [README](../README.md#start-the-server) shows, you have everything it needs.

The script plays the game in these steps:

1. It creates a *party* of three characters: a fighter, a cleric, and a thief.
2. It starts a *session*, a single game in progress, of `builtin-barrow` and enters the dungeon.
3. It sends commands until the first battle ends or it has sent 40 commands, and it prints each command and each message that the server returns.

The script picks each command from the server's responses, not from a fixed list of moves. Before every command, it reads the legal commands from `GET /v1/sessions/{session_id}/commands` and sends only a command from that list. It sends each command with `include=view`, so the response includes the *player view*, everything the players may know about the game. The script reads the view to pick a direction to walk and an action for each party member in battle.

The script also retries safely. It sends every `POST` with an `Idempotency-Key` header. When a connection fails, or the server returns an error that a retry can resolve, the script sends the request again with the same key, for up to four attempts in total. Because every attempt has the same key, the server runs the request once and returns the first response to a retry. When the script gives up, it prints the error's code, its detail, and the request's ID, which the server operator can find in the log.

## Run the script

In one terminal, from the repository's root, start a server:

```sh
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server listens on `http://127.0.0.1:8640` by default. In a second terminal, from the repository's root, run the script with the server's URL:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640
```

The URL is optional and defaults to `http://127.0.0.1:8640`.

To set up API keys, see [Authentication](../docs/authentication.md). If the server requires an API key, pass a key that has the player role:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --api-key player-key-0123456789
```

To change the limit of 40 commands, pass `--max-commands`:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --max-commands 10
```

The dice are random, so each run prints different messages. The output ends with a line such as `The battle is over. The session is in mode exploring.` When the party loses, the line ends with the mode `game_over` instead.

## Next steps

- [Play a game](../docs/play-a-game.md): the same game, one request at a time.
