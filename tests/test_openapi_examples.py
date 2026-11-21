"""The examples in the OpenAPI document are what a server returns for their requests.

`src/bxapi/openapi_examples.json` lists example exchanges with request templates. A template can refer to a value
from an earlier exchange's response as `{name.field}`, like `{create_session.id}`, or to a whole response as `{name}`.
The tests send each request to a new server, in order, and compare each response with the recorded one. Ids and
timestamps differ from run to run, so the comparison replaces them with placeholders. The server draws every seed
from `SEED`, so the dice are the same on every run. Each request has the API key of the client `my-game` with the
role the example names in `role`, or the player role when it names none.

Run this module to send the requests and record the responses again:

```sh
uv run python -m tests.test_openapi_examples
```
"""

import json
import re
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.openapi import _FixedSettings, build_openapi
from bxapi.openapi_examples import EXAMPLES_FILE, load_examples, trim
from bxapi.settings import ApiKey, Role

SEED = 4127730658136791
"""The seed the server draws whenever a request has none."""

_KEYS = {Role.PLAYER: "my-game-player-key-0123", Role.REFEREE: "my-game-referee-key-0123"}

_TEMPLATE = re.compile(r"\{([a-z0-9_]+)((?:\.[A-Za-z0-9_]+)*)\}")
_ID = re.compile(r"\b(rol|chr|adv|ses|sav)_[A-Za-z0-9_-]{22}\b")
_TIME = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\b")


def _lookup(responses: dict[str, Any], name: str, fields: str) -> Any:
    value = responses[name]
    for field in fields.split(".")[1:]:
        value = value[int(field)] if isinstance(value, list) else value[field]
    return value


def _resolve(template: Any, responses: dict[str, Any]) -> Any:
    """Return a template with each reference to an earlier response replaced by its value."""
    if isinstance(template, dict):
        return {key: _resolve(value, responses) for key, value in template.items()}
    if isinstance(template, list):
        return [_resolve(value, responses) for value in template]
    if isinstance(template, str):
        whole = _TEMPLATE.fullmatch(template)
        if whole:
            return _lookup(responses, whole.group(1), whole.group(2))
        return _TEMPLATE.sub(lambda match: str(_lookup(responses, match.group(1), match.group(2))), template)
    return template


def _normalized(value: Any) -> Any:
    """Return a JSON value with every id and timestamp replaced by a placeholder."""
    text = _TIME.sub("<time>", _ID.sub(lambda match: f"<{match.group(1)}>", json.dumps(value, sort_keys=True)))
    return json.loads(text)


def _replay() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Send every example request to a new server, and yield each example with what the server returned."""
    # The settings ignore the `BXAPI_` environment variables, so a key in your shell can't change the examples.
    keys = tuple(ApiKey.model_validate({"client": "my-game", "key": key, "role": role}) for role, key in _KEYS.items())
    settings = _FixedSettings(database=":memory:", api_keys=keys)
    responses: dict[str, Any] = {}
    with mock.patch("bxapi.seeds.draw_seed", return_value=SEED), TestClient(create_app(settings)) as client:
        for exchange in load_examples():
            request = exchange["request"]
            sent: dict[str, Any] = {"method": request["method"], "path": _resolve(request["path"], responses)}
            if "query" in request:
                sent["query"] = _resolve(request["query"], responses)
            if "body" in request:
                sent["body"] = _resolve(request["body"], responses)
            key = _KEYS[Role(exchange.get("role", Role.PLAYER))]
            response = client.request(
                sent["method"],
                sent["path"],
                params=sent.get("query"),
                json=sent.get("body"),
                headers={"Authorization": f"Bearer {key}"},
            )
            body = response.json() if response.content else None
            responses[exchange["name"]] = body
            shown = trim(body, exchange["trim"]) if "trim" in exchange else body
            yield exchange, {**sent, "status": response.status_code, "response": shown}


def test_every_example_is_what_the_server_returns() -> None:
    checked = 0
    for exchange, live in _replay():
        recorded = exchange["recorded"]
        assert _normalized(live) == _normalized(recorded), (
            f"{exchange['name']}: the server's response differs from the recorded one. Record the examples again "
            "with `uv run python -m tests.test_openapi_examples`, and check the difference."
        )
        checked += 1
    assert checked == len(load_examples())


def test_every_example_belongs_to_its_operation() -> None:
    operations = {
        operation["operationId"]: (method.upper(), path)
        for path, item in build_openapi()["paths"].items()
        for method, operation in item.items()
    }
    names = [exchange["name"] for exchange in load_examples()]
    assert len(names) == len(set(names))
    assert {exchange["operation"] for exchange in load_examples()} == set(operations), "every operation has an example"
    for exchange in load_examples():
        method, template = operations[exchange["operation"]]
        pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
        assert exchange["request"]["method"] == method, exchange["name"]
        assert re.fullmatch(pattern, exchange["recorded"]["path"]), exchange["name"]


def test_the_document_shows_each_example() -> None:
    document = build_openapi()
    paths = document["paths"]
    for exchange in load_examples():
        recorded = exchange["recorded"]
        operation = next(
            operation
            for item in paths.values()
            for operation in item.values()
            if operation["operationId"] == exchange["operation"]
        )
        if "body" in recorded:
            media = next(iter(operation["requestBody"]["content"].values()))
            assert media["examples"][exchange["name"]]["value"] == recorded["body"]
        if recorded["response"] is not None:
            media = next(iter(operation["responses"][str(recorded["status"])]["content"].values()))
            assert media["examples"][exchange["name"]]["value"] == recorded["response"]


def _record() -> None:
    path = Path(str(resources.files("bxapi").joinpath(EXAMPLES_FILE)))
    load_examples.cache_clear()
    exchanges = [{**exchange, "recorded": live} for exchange, live in _replay()]
    path.write_text(json.dumps(exchanges, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Recorded {len(exchanges)} examples in {path}.")


if __name__ == "__main__":
    _record()
