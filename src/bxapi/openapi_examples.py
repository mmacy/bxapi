"""The request and response examples in the OpenAPI document.

`openapi_examples.json` in this package lists example exchanges with the server, in the order a
client makes them. Each one has a request, written as a template that can refer to the responses
of earlier exchanges, and the request and response that a server returned for it.
[`add_examples`][bxapi.openapi_examples.add_examples] copies the recorded requests and responses
into the document.

Some errors read the same on every operation, like 401 `unauthorized`, or can't be caused by a
request alone, like 503 `database_busy`. [`shared_problems`][bxapi.openapi_examples.shared_problems]
lists one example of each, built by the function the server calls to report the error.
`add_examples` shows it on each operation that lists the error's code and has no recorded example
of it.

`tests/test_openapi_examples.py` sends every request to a new server and fails when a response
differs from the recorded one, so an example can't go stale. To add or change an example, edit
its template in the file and record the responses again:

```sh
uv run python -m tests.test_openapi_examples
```
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from bxapi.auth import unauthorized
from bxapi.db import BUSY_TIMEOUT_SECONDS
from bxapi.errors import (
    ApiError,
    Problem,
    content_too_large,
    database_busy,
    internal_error,
    request_timeout,
    unsupported_media_type,
    validation_error,
)
from bxapi.idempotency import key_in_progress, key_interrupted, key_reused
from bxapi.session_manager import LOCK_TIMEOUT_SECONDS, session_busy, session_unreadable
from bxapi.settings import Settings

__all__ = [
    "EXAMPLES_FILE",
    "EXAMPLE_REQUEST_ID",
    "SharedProblem",
    "add_examples",
    "error_codes",
    "load_examples",
    "shared_problems",
    "trim",
    "trim_note",
]

EXAMPLES_FILE = "openapi_examples.json"
"""The name of the file in the `bxapi` package that lists the examples."""

EXAMPLE_REQUEST_ID = "req_Zk3uY0mWq1x8Jr5cTt2aLw"
"""The `request_id` in each example from [`shared_problems`][bxapi.openapi_examples.shared_problems]."""


@cache
def load_examples() -> tuple[dict[str, Any], ...]:
    """Return the example exchanges, in the order a client makes them.

    Returns:
        One dict for each exchange, as the file lists it.
    """
    text = resources.files("bxapi").joinpath(EXAMPLES_FILE).read_text(encoding="utf-8")
    return tuple(json.loads(text))


@dataclass(frozen=True)
class SharedProblem:
    """An example of an error response that the OpenAPI document shows on more than one operation."""

    name: str
    """The example's name in the document, like `unauthorized`."""
    summary: str
    """A short title for the example."""
    description: str
    """When the server sends the response, as Markdown."""
    problem: Problem
    """The problem document the server sends."""
    operations: frozenset[str] = frozenset()
    """The `operationId` of each operation to show the example on. When it's empty, the example
    goes on every operation that lists its code, except an operation that another shared example
    of the same code lists."""


@cache
def shared_problems() -> tuple[SharedProblem, ...]:
    """Return the examples of the errors that the recorded exchanges don't cover.

    Each example's problem document comes from the function that the server calls to build that
    error, with the default settings where the detail states a limit. Its `request_id` is
    [`EXAMPLE_REQUEST_ID`][bxapi.openapi_examples.EXAMPLE_REQUEST_ID].

    Returns:
        One example for each error code, except `session_unreadable`, which has one for a
        session and one for a save.

    Examples:
        ```python
        from bxapi.openapi_examples import shared_problems

        names = [example.name for example in shared_problems()]
        assert "unauthorized" in names
        ```
    """
    max_bytes: int = Settings.model_fields["max_body_bytes"].default
    timeout_seconds: float = Settings.model_fields["body_timeout_seconds"].default
    missing = {"type": "missing", "loc": ("body", "expression"), "msg": "Field required"}

    def shared(name: str, summary: str, description: str, error: ApiError, *operations: str) -> SharedProblem:
        problem = error.problem().model_copy(update={"request_id": EXAMPLE_REQUEST_ID})
        return SharedProblem(name, summary, description, problem, frozenset(operations))

    return (
        shared(
            "unauthorized",
            "Send no API key",
            "The response to a request under `/v1` with no API key, or with a key the server doesn't have.",
            unauthorized(),
        ),
        shared(
            "bad_request",
            "Send a body that isn't UTF-8",
            "The response to a request with a body the server can't decode. FastAPI writes the `detail`.",
            # FastAPI raises this error itself, and the server's handler only converts it.
            ApiError(400, "bad_request", "There was an error parsing the body"),
        ),
        shared(
            "request_timeout",
            "Send a body too slowly",
            f"The response when a body takes more than {timeout_seconds:g} seconds to arrive, the server's default "
            "limit.",
            request_timeout(timeout_seconds),
        ),
        shared(
            "content_too_large",
            "Send a body that's too large",
            f"The response to a body larger than {max_bytes} bytes, the server's default limit.",
            content_too_large(max_bytes),
        ),
        shared(
            "unsupported_media_type",
            "Send a body that isn't declared as JSON",
            "The response to a request with a body and the header `Content-Type: text/plain`.",
            unsupported_media_type(),
        ),
        shared(
            "validation_error",
            "Leave out a required field",
            "The response to `POST /v1/dice/roll` with the body `{}`. Each entry in `errors` is one part of the "
            "request that failed validation, and `loc` is the path to it.",
            validation_error([missing]),
        ),
        shared(
            "idempotency_key_in_progress",
            "Retry while the first request runs",
            "The response to a second request with an `Idempotency-Key` while the first request with that key is "
            "still running.",
            key_in_progress(),
        ),
        shared(
            "idempotency_key_interrupted",
            "Retry a request the server didn't finish",
            "The response to a request with an `Idempotency-Key` whose first request the server failed or stopped "
            "in the middle of.",
            key_interrupted(),
        ),
        shared(
            "idempotency_key_reused",
            "Send a key again with another request",
            "The response to a request with an `Idempotency-Key` that your client already sent with a different body.",
            key_reused(),
        ),
        shared(
            "session_busy",
            "Wait too long for the session",
            f"The response when another request uses the session for more than {LOCK_TIMEOUT_SECONDS:g} seconds. "
            "`revision` is the session's revision.",
            session_busy(3, waited_seconds=LOCK_TIMEOUT_SECONDS),
        ),
        shared(
            "internal_error",
            "Hit a bug in the server",
            "The response to a request that raises an exception the server doesn't expect. Every such request gets "
            "the same document.",
            internal_error(),
        ),
        shared(
            "session_unreadable",
            "Load a session osrlib can't read",
            "The response when the server's osrlib can't load the session's stored state.",
            session_unreadable("session"),
        ),
        shared(
            "save_unreadable",
            "Restore a save osrlib can't read",
            "The response when the server's osrlib can't load the save's stored document.",
            session_unreadable("save"),
            "restore_save",
        ),
        shared(
            "database_busy",
            "Wait too long for the database",
            f"The response when another process holds the database's write lock for more than "
            f"{BUSY_TIMEOUT_SECONDS:g} seconds.",
            database_busy(),
        ),
    )


_LISTED_CODE = re.compile(r"^- `([a-z_]+)`", re.MULTILINE)


def error_codes(response: dict[str, Any]) -> list[str]:
    """Return the error codes that an error response in the OpenAPI document lists.

    Args:
        response: A response object from the document, with the description that
            [`problem_responses`][bxapi.errors.problem_responses] writes.

    Returns:
        The codes, in the order the description lists them.

    Examples:
        ```python
        from bxapi.errors import problem_responses
        from bxapi.openapi_examples import error_codes

        response = problem_responses({404: ["`not_found`: No session has that id."]})[404]
        assert error_codes(response) == ["not_found"]
        ```
    """
    return _LISTED_CODE.findall(response.get("description", ""))


def _shared_example(code: str, operation_id: str) -> SharedProblem | None:
    matches = [example for example in shared_problems() if example.problem.code == code]
    specific = [example for example in matches if operation_id in example.operations]
    general = [example for example in matches if not example.operations]
    return next(iter(specific + general), None)


def _add_shared_examples(operation_id: str, operation: dict[str, Any]) -> None:
    for status, response in operation["responses"].items():
        if not status.startswith(("4", "5")) or "content" not in response:
            continue
        media = next(iter(response["content"].values()))
        shown = {example["value"].get("code") for example in media.get("examples", {}).values()}
        for code in error_codes(response):
            example = None if code in shown else _shared_example(code, operation_id)
            if example is not None:
                media.setdefault("examples", {})[example.name] = {
                    "summary": example.summary,
                    "description": example.description,
                    "value": example.problem.model_dump(mode="json"),
                }


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
    """Add the recorded examples, and then the shared error examples, to an OpenAPI document.

    Each request body becomes an example of its operation's request body, and each response body
    an example of the response with its status. A request and its response have the same example
    name, so a tool that pairs them can. Then each error code that an operation lists, and that no
    recorded example of the operation shows, gets its example from
    [`shared_problems`][bxapi.openapi_examples.shared_problems].

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
    for operation_id, operation in operations.items():
        _add_shared_examples(operation_id, operation)
