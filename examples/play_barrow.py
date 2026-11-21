"""Play the first battle of the built-in adventure against a bxapi server.

The script creates a party of three, starts `builtin-barrow`, enters the dungeon, and sends
commands until the first battle ends or it has sent `--max-commands` commands. It prints the
messages of each response. It sends each `POST` with an `Idempotency-Key`, so it can send a
request again after a failed connection without the server running the request twice. It uses
only the standard library.

    python examples/play_barrow.py http://127.0.0.1:8640 --api-key YOUR_KEY
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

# How many times to send one request, and the problem codes that the same request can succeed after.
ATTEMPTS = 4
RETRYABLE = {"session_busy", "database_busy", "request_timeout", "idempotency_key_in_progress"}

# The party starts on its first cell facing north, and the rules refuse a move into a wall.
DIRECTIONS = ("north", "east", "south", "west")

# Each kit costs at most 30 gp, and a first-level character starts with at least 30 gp.
PARTY = (
    {"name": "Brenna", "class_id": "fighter", "alignment": "lawful", "kit": ["leather", "sword"]},
    {"name": "Tomas", "class_id": "cleric", "alignment": "lawful", "kit": ["leather", "mace"]},
    {"name": "Wren", "class_id": "thief", "alignment": "neutral", "kit": ["leather", "dagger"]},
)


class Api:
    """A minimal JSON client for bxapi."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Send a request and return the decoded response, or exit with the problem's code and detail.

        The method sends the request again, up to `ATTEMPTS` times, when the connection fails or the server
        answers with a code in `RETRYABLE`.
        """
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers)
        if method == "POST":
            # Every attempt has the same key, so the server runs the request once and returns the first
            # response to a retry.
            headers["Idempotency-Key"] = str(uuid.uuid4())
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                # The body of an error response is a problem document with a `code`. A refused command isn't an
                # error. Report `request_id` to whoever runs the server, who can find the request in the log.
                problem = json.load(error)
                if problem["code"] not in RETRYABLE or attempt == ATTEMPTS:
                    raise SystemExit(
                        f"{method} {path} failed: {problem['code']}: {problem['detail']} "
                        f"(request {problem['request_id']})"
                    ) from None
                time.sleep(float(error.headers.get("Retry-After", 1)))
            except urllib.error.URLError as error:
                if attempt == ATTEMPTS:
                    raise SystemExit(f"{method} {path} failed: {error.reason}") from None
                time.sleep(1)
        raise AssertionError("unreachable")


def create_party(api: Api) -> list[str]:
    """Create each character in `PARTY` and return their ids in marching order."""
    ids = []
    for member in PARTY:
        purchases = [{"item_id": item} for item in member["kit"]]
        character = api.call(
            "POST",
            "/v1/characters",
            {
                "name": member["name"],
                "class_id": member["class_id"],
                "alignment": member["alignment"],
                "purchases": purchases,
                "equip_item_ids": member["kit"],
            },
        )
        print(f"Created {character['name']}, a level {character['level']} {character['class_id']}.")
        ids.append(character["id"])
    return ids


def battle_declarations(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Declare an action for every member who has to declare this round."""
    encounter = view["encounter"]
    rates = {member["id"]: member["encounter_rate_feet"] for member in view["party"]}
    target = next((group for group in encounter["groups"] if not group["fled"]), None)
    declarations: list[dict[str, Any]] = []
    for member_id in encounter["declarers"]:
        distance = target["member_distance_feet"].get(member_id, target["distance_feet"]) if target else 0
        # Only the front rank can reach the monsters in melee, and only if a move of up to the member's
        # encounter rate, plus 5 feet, closes the distance. The rules refuse any other attack.
        if target is None or member_id not in encounter["front_rank"] or distance > rates[member_id] + 5:
            declarations.append({"character_id": member_id, "action": "hold"})
            continue
        declaration = {"character_id": member_id, "action": "attack", "target_group_id": target["id"]}
        if member_id not in target["front_rank"]:
            declaration["move"] = "close"
        declarations.append(declaration)
    return declarations


def choose_move(view: dict[str, Any], tried: set[tuple[Any, ...]]) -> dict[str, Any] | None:
    """Pick a direction that the party hasn't tried from where it stands."""
    position = tuple(view["location"]["position"])
    for direction in DIRECTIONS:
        if (position, direction) not in tried:
            tried.add((position, direction))
            return {"command_type": "move_party", "direction": direction}
    return None


def choose_command(
    mode: str, legal: set[str], view: dict[str, Any], dungeon_id: str, tried: set[tuple[Any, ...]]
) -> dict[str, Any] | None:
    """Pick the next command from the session's mode, the legal commands, and the player view."""
    if mode == "town" and "enter_dungeon" in legal:
        return {"command_type": "enter_dungeon", "dungeon_id": dungeon_id}
    if mode == "exploring" and "move_party" in legal:
        return choose_move(view, tried)
    if mode == "encounter" and "engage_battle" in legal:
        return {"command_type": "engage_battle"}
    if mode == "battle" and "resolve_battle_round" in legal:
        return {"command_type": "resolve_battle_round", "declarations": battle_declarations(view)}
    return None


def play(api: Api, max_commands: int) -> None:
    """Start the adventure and send commands until the first battle ends or `max_commands` are sent."""
    adventure = api.call("GET", "/v1/adventures/builtin-barrow")
    session = api.call("POST", "/v1/sessions", {"adventure_id": adventure["id"], "character_ids": create_party(api)})
    base = f"/v1/sessions/{session['id']}"
    print(f"Started {session['name']} as {session['id']}.")
    tried: set[tuple[Any, ...]] = set()
    in_battle = False
    view = api.call("GET", f"{base}/view")
    for _ in range(max_commands):
        legal = api.call("GET", f"{base}/commands")
        command = choose_command(
            legal["mode"],
            {entry["command_type"] for entry in legal["commands"]},
            view,
            adventure["dungeons"][0]["id"],
            tried,
        )
        if command is None:
            print(f"No command to send in mode {legal['mode']}. Stopping.")
            return
        # `include=view` returns the player view after the command in the same response.
        outcome = api.call("POST", f"{base}/commands?include=view", command)
        view = outcome["view"]
        print(f"> {command['command_type']}" + (f" {command['direction']}" if "direction" in command else ""))
        if not outcome["accepted"]:
            print("  refused:", ", ".join(rejection["code"] for rejection in outcome["rejections"]))
        for message in outcome["messages"]:
            print("  " + message)
        in_battle = in_battle or outcome["mode"] == "battle"
        if outcome["ended"] or (in_battle and outcome["mode"] != "battle"):
            print(f"The battle is over. The session is in mode {outcome['mode']}.")
            return
    print(f"Sent {max_commands} commands. Stopping.")


def main() -> None:
    """Read the command line and play."""
    parser = argparse.ArgumentParser(description="Play the first battle of the built-in adventure.")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8640", help="the server's URL")
    parser.add_argument("--api-key", help="an API key, when the server asks for one")
    parser.add_argument("--max-commands", type=int, default=40, help="the most commands to send")
    arguments = parser.parse_args()
    play(Api(arguments.url, arguments.api_key), arguments.max_commands)


if __name__ == "__main__":
    sys.exit(main())
