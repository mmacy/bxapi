# Example client

[`play_barrow.py`](play_barrow.py) is a command-line Python client that plays `builtin-barrow`, the built-in adventure, through its first monster battle. It uses only the Python standard library, so you need no extra packages once you have completed [Get it running](../README.md#get-it-running).

The script illustrates the core request loop of a bxapi client:

- Creates a three-character party (`Brenna` the fighter, `Tomas` the cleric, and `Wren` the thief) with [`POST /v1/characters`](https://mmacy.github.io/bxapi/reference/endpoint/characters#operation-create_character), then starts a game session with [`POST /v1/sessions`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-create_session).
- Before each action, fetches legal commands from [`GET /v1/sessions/{session_id}/commands`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-list_session_commands) and selects a command using the current `mode` and player view.
- Sends each command to [`POST /v1/sessions/{session_id}/commands`](https://mmacy.github.io/bxapi/reference/endpoint/sessions#operation-send_command) with `include=view` so the response returns the updated player view in a single round-trip, then prints any rule `rejections` or event `messages`.
- Attaches a UUID `Idempotency-Key` header to every `POST` and retries up to four times on dropped connections or `session_busy`, `database_busy`, `request_timeout`, and `idempotency_key_in_progress`, waiting the seconds in `Retry-After` (or 1 second when absent) between attempts. On any other [problem document](https://mmacy.github.io/bxapi/reference/#errors), it prints `code`, `detail`, and `request_id` and exits.

## Run the script

1. From the repository root, start a local server in one terminal:

   ```sh
   BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
   ```

2. In a second terminal, run the script from the repository root:

   ```sh
   uv run python examples/play_barrow.py
   ```

   The server URL argument is optional and defaults to `http://127.0.0.1:8640`.

To run against a server configured with API keys (see [Configure and deploy](../docs/configuration.md#generate-and-configure-api-keys)), pass the server URL and `--api-key`:

```sh
uv run python examples/play_barrow.py http://127.0.0.1:8640 --api-key player-key-0123456789
```

By default, the script stops after 40 commands if the first battle hasn't ended. Pass `--max-commands` to change that cap:

```sh
uv run python examples/play_barrow.py --max-commands 10
```

Because dice rolls are random, each run prints different combat messages. The script stops when the first battle ends (`mode` returns to `exploring`) or monsters defeat the party (`mode` becomes `game_over`). To run the same game loop manually one request at a time, follow [Play a game](../docs/play-a-game.md).
