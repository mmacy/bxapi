r"""How the server reads a request's JSON body: which values it refuses, and how large a body may be.

Python's `json` module reads three kinds of value that the server can't store or can't send back
as JSON, so [`parse_json`][bxapi.json_body.parse_json] refuses each one with a 400:

- A number that JSON doesn't have: `NaN`, `Infinity`, `-Infinity`, and a number too large for a
  64-bit float, like `1e400`, which Python reads as infinity. The server would send it back as
  `NaN` or `Infinity`, which a JSON parser refuses.
- A string or a key with half of a UTF-16 surrogate pair, like `"\ud800"`. Python can't encode
  it as UTF-8, so the server could neither store it nor send it back.
- Arrays and objects nested more than [`MAX_JSON_DEPTH`][bxapi.json_body.MAX_JSON_DEPTH] levels
  deep. osrlib and pydantic check a document with recursive functions, which fail on a document
  nested hundreds of levels deep.

A body is also limited in size. [`BodyLimitMiddleware`][bxapi.app.BodyLimitMiddleware] refuses a
body larger than the server's `max_body_bytes` setting on every path. That setting has to fit an
adventure or a save document, which is megabytes. Most operations take a body of a few hundred
bytes, so [`JsonRequest`][bxapi.json_body.JsonRequest] refuses a body larger than
[`SMALL_BODY_BYTES`][bxapi.json_body.SMALL_BODY_BYTES] on every operation that isn't marked with
[`accepts_document`][bxapi.json_body.accepts_document].
"""

import json
from collections.abc import Callable
from contextlib import aclosing
from typing import Any, NoReturn

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import Receive, Scope

from bxapi.errors import content_too_large

__all__ = ["MAX_JSON_DEPTH", "SMALL_BODY_BYTES", "JsonRequest", "accepts_document", "body_limit", "parse_json"]

MAX_JSON_DEPTH = 100
"""The most levels of arrays and objects a request body may nest. `[]` is one level, and `[[]]` is two.

It's a limit of the server. A save document from osrlib nests 12 levels, and the adventure that
comes with the server nests 11."""

SMALL_BODY_BYTES = 64 * 1024
"""The largest body, in bytes, of an operation that takes no document.

A command, a set of character choices, or a new name is a few hundred bytes. The limit bounds how
long the server spends on one request to these operations, and how much one accepted command adds
to a session's stored command log."""

_DOCUMENT_MARK = "__bxapi_accepts_document__"


def accepts_document[**P, R](endpoint: Callable[P, R]) -> Callable[P, R]:
    """Mark an endpoint whose request body can be an osrlib document, like an adventure or a save.

    The server reads a body of up to its `max_body_bytes` setting for a marked endpoint, and of up
    to [`SMALL_BODY_BYTES`][bxapi.json_body.SMALL_BODY_BYTES] for any other. Put the decorator
    under the route decorator, so the route registers the marked function.

    Args:
        endpoint: The endpoint function.

    Returns:
        The same function.

    Examples:
        ```python
        from bxapi.json_body import SMALL_BODY_BYTES, accepts_document, body_limit


        @accepts_document
        def upload_adventure() -> None: ...


        def roll_dice() -> None: ...


        assert body_limit(upload_adventure) is None
        assert body_limit(roll_dice) == SMALL_BODY_BYTES
        ```
    """
    setattr(endpoint, _DOCUMENT_MARK, True)
    return endpoint


def body_limit(endpoint: Callable[..., Any]) -> int | None:
    """Return the largest body an endpoint reads, apart from the server's `max_body_bytes` setting.

    Args:
        endpoint: The endpoint function.

    Returns:
        [`SMALL_BODY_BYTES`][bxapi.json_body.SMALL_BODY_BYTES], or `None` for an endpoint marked
        with [`accepts_document`][bxapi.json_body.accepts_document], whose only limit is the
        setting.
    """
    return None if getattr(endpoint, _DOCUMENT_MARK, False) else SMALL_BODY_BYTES


def _refuse(detail: str) -> NoReturn:
    # FastAPI re-raises an HTTPException raised while it reads the body, and turns any other
    # exception into a 400 whose detail says nothing about the cause.
    raise StarletteHTTPException(status_code=400, detail=detail)


def _refuse_constant(name: str) -> NoReturn:
    _refuse(f"The body has the number {name}, which JSON doesn't have.")


def _nests_deeper_than(value: object, limit: int) -> bool:
    # Visit one level of containers at a time, so the check itself can't run out of stack.
    level: list[object] = [value]
    for _ in range(limit):
        children: list[object] = []
        for node in level:
            if isinstance(node, dict):
                children.extend(node.values())
            elif isinstance(node, list):
                children.extend(node)
        level = [child for child in children if isinstance(child, (dict, list))]
        if not level:
            return False
    return True


def parse_json(body: bytes) -> Any:
    r"""Parse a request body as JSON, and refuse a value the server can't store or send back.

    Args:
        body: The body's bytes, in UTF-8, UTF-16, or UTF-32.

    Returns:
        The parsed value.

    Raises:
        json.JSONDecodeError: If the body decodes as text but isn't valid JSON.
        ValueError: If the body's bytes don't decode as text, or the body has an integer with more
            digits than Python converts. `UnicodeDecodeError` is a `ValueError`.
        starlette.exceptions.HTTPException: With status 400, if the body has `NaN`, `Infinity`,
            or a number too large for a 64-bit float, a string or a key with an unpaired
            surrogate, or more than [`MAX_JSON_DEPTH`][bxapi.json_body.MAX_JSON_DEPTH] nested
            levels.

    Examples:
        ```python
        import pytest
        from starlette.exceptions import HTTPException

        from bxapi.json_body import parse_json

        assert parse_json(b'{"expression": "3d6"}') == {"expression": "3d6"}
        for refused in (b'{"times": NaN}', b'{"times": 1e400}', b'{"name": "\\ud800"}', b"[" * 101 + b"]" * 101):
            with pytest.raises(HTTPException):
                parse_json(refused)
        ```
    """
    value = json.loads(body, parse_constant=_refuse_constant)
    # A body with at most MAX_JSON_DEPTH opening brackets can't nest deeper, whatever its encoding.
    if body.count(b"[") + body.count(b"{") > MAX_JSON_DEPTH and _nests_deeper_than(value, MAX_JSON_DEPTH):
        _refuse(f"The body nests arrays and objects more than {MAX_JSON_DEPTH} levels deep.")
    try:
        # Writing the value back out finds a float that isn't finite and a surrogate with no pair,
        # anywhere in the value, at the speed of the parser.
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        _refuse("The body has a string with half of a UTF-16 surrogate pair, like \\ud800 on its own.")
    except ValueError:
        _refuse("The body has a number that's too large for a 64-bit float, like 1e400.")
    return value


class JsonRequest(Request):
    """A request whose body the server reads with [`parse_json`][bxapi.json_body.parse_json], up to a size limit.

    [`IdempotentRoute`][bxapi.idempotency.IdempotentRoute] gives one to every endpoint under
    `/v1` that takes a body, in place of Starlette's `Request`.

    Args:
        scope: The request's ASGI scope.
        receive: The request's ASGI receive function.
        max_body_bytes: The largest body to read, in bytes, from
            [`body_limit`][bxapi.json_body.body_limit]. `None` reads a body of any size that
            [`BodyLimitMiddleware`][bxapi.app.BodyLimitMiddleware] lets through.
    """

    def __init__(self, scope: Scope, receive: Receive, *, max_body_bytes: int | None = None) -> None:
        super().__init__(scope, receive)
        self._max_body_bytes = max_body_bytes

    def _too_large(self, limit: int) -> StarletteHTTPException:
        error = content_too_large(limit, for_document=False)
        return StarletteHTTPException(status_code=error.status, detail=error.detail)

    async def body(self) -> bytes:
        """Return the request's body, and refuse one larger than the limit before reading all of it.

        Raises:
            starlette.exceptions.HTTPException: With status 413, if the `Content-Length` header
                states more bytes than the limit, or more bytes than the limit arrive.
        """
        limit = self._max_body_bytes
        if limit is None or hasattr(self, "_body"):
            return await super().body()
        declared = self.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise self._too_large(limit)
        chunks: list[bytes] = []
        received = 0
        async with aclosing(self.stream()) as stream:
            async for chunk in stream:
                received += len(chunk)
                if received > limit:
                    raise self._too_large(limit)
                chunks.append(chunk)
        self._body = b"".join(chunks)
        return self._body

    async def json(self) -> Any:
        """Return the body parsed by [`parse_json`][bxapi.json_body.parse_json]."""
        if not hasattr(self, "_json"):
            self._json = parse_json(await self.body())
        return self._json
