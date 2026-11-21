"""The request and response examples in the OpenAPI document.

`openapi_examples.json` in this package lists example exchanges with the server, in the order a
client makes them. Each one has a request, written as a template that can refer to the responses
of earlier exchanges, and the request and response that a server returned for it.
[`add_examples`][bxapi.openapi_examples.add_examples] copies the recorded requests and responses
into the document.

`tests/test_openapi_examples.py` sends every request to a new server and fails when a response
differs from the recorded one, so an example can't go stale. To add or change an example, edit
its template in the file and record the responses again:

```sh
uv run python -m tests.test_openapi_examples
```
"""

import json
from functools import cache
from importlib import resources
from typing import Any

__all__ = ["EXAMPLES_FILE", "add_examples", "load_examples", "trim", "trim_note"]

EXAMPLES_FILE = "openapi_examples.json"
"""The name of the file in the `bxapi` package that lists the examples."""


@cache
def load_examples() -> tuple[dict[str, Any], ...]:
    """Return the example exchanges, in the order a client makes them.

    Returns:
        One dict for each exchange, as the file lists it.
    """
    text = resources.files("bxapi").joinpath(EXAMPLES_FILE).read_text(encoding="utf-8")
    return tuple(json.loads(text))


def _parent(document: Any, pointer: str) -> tuple[Any, str | int]:
    parts = pointer.removeprefix("/").split("/")
    for part in parts[:-1]:
        document = document[int(part)] if isinstance(document, list) else document[part]
    last = parts[-1]
    return document, int(last) if isinstance(document, list) else last


def trim(response: Any, cuts: dict[str, int]) -> Any:
    """Return a copy of a response with long values cut short, so the example stays readable.

    Args:
        response: A response body, as decoded JSON.
        cuts: A mapping from a JSON pointer, like `/text`, to the length to cut that value to.
            A list keeps that many items, and a string keeps that many characters followed by
            `...`.

    Returns:
        The shortened copy.

    Examples:
        ```python
        from bxapi.openapi_examples import trim

        assert trim({"text": "abcdef", "items": [1, 2, 3]}, {"/text": 3, "/items": 1}) == {
            "text": "abc...",
            "items": [1],
        }
        ```
    """
    shortened = json.loads(json.dumps(response))
    for pointer, length in cuts.items():
        parent, key = _parent(shortened, pointer)
        value = parent[key]
        parent[key] = value[:length] + "..." if isinstance(value, str) else value[:length]
    return shortened


def trim_note(cuts: dict[str, int], response: Any) -> str:
    """Return the sentence that tells the reader which values an example shortens.

    Args:
        cuts: The same mapping [`trim`][bxapi.openapi_examples.trim] takes.
        response: The shortened response.

    Returns:
        A sentence, or an empty string when `cuts` is empty.
    """
    notes = []
    for pointer, length in cuts.items():
        parent, key = _parent(response, pointer)
        unit = "characters" if isinstance(parent[key], str) else "items"
        notes.append(f"`{pointer.removeprefix('/').replace('/', '.')}` is cut to its first {length} {unit}")
    return f"This example shows a shortened response: {' and '.join(notes)}." if notes else ""


def _request_line(recorded: dict[str, Any]) -> str:
    query = "&".join(f"{name}={value}" for name, value in recorded.get("query", {}).items())
    return f"`{recorded['method']} {recorded['path']}{'?' + query if query else ''}`"


def add_examples(schema: dict[str, Any]) -> None:
    """Add the recorded examples to an OpenAPI document.

    Each request body becomes an example of its operation's request body, and each response body
    an example of the response with its status. A request and its response have the same example
    name, so a tool that pairs them can.

    Args:
        schema: The OpenAPI document, which the function changes.
    """
    operations = {
        operation["operationId"]: operation
        for path in schema.get("paths", {}).values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    for exchange in load_examples():
        operation = operations[exchange["operation"]]
        recorded = exchange["recorded"]
        line = _request_line(recorded)
        if exchange.get("role") == "referee":
            line += ", with a referee key"
        if "body" in recorded:
            content = operation["requestBody"]["content"]
            media = next(iter(content.values()))
            media.setdefault("examples", {})[exchange["name"]] = {
                "summary": exchange["summary"],
                "description": line,
                "value": recorded["body"],
            }
        if recorded.get("response") is None:
            continue
        response = operation["responses"][str(recorded["status"])]
        media = next(iter(response["content"].values()))
        description = " ".join(
            part
            for part in (f"The response to {line}.", trim_note(exchange.get("trim", {}), recorded["response"]))
            if part
        )
        media.setdefault("examples", {})[exchange["name"]] = {
            "summary": exchange["summary"],
            "description": description,
            "value": recorded["response"],
        }
