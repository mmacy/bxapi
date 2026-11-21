"""Idempotency keys: how a client sends a `POST` again without the server running it twice.

A client that gets no response to a `POST`, because the connection or the server failed, can't
tell whether the server ran the request. To send it again safely, the client puts a value it
chose, like a UUID, in the `Idempotency-Key` header of the first request and of every retry:

```text
POST /v1/sessions
Idempotency-Key: 0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11
```

The server runs the first request with a key and stores its response. A later request from the
same client with the same key gets the stored response, with the header `Idempotent-Replayed: true`,
and the server runs nothing.

Every route under `/v1` is an [`IdempotentRoute`][bxapi.idempotency.IdempotentRoute], which
does this for a `POST` that has the header. The keys are rows of the `idempotency_keys` table, so
the processes that share a database file share them.
"""

import hashlib
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from bxapi.db import Database, IdempotencyRow
from bxapi.errors import ApiError, invalid, is_reported_error, validation_error

__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "IN_PROGRESS_SECONDS",
    "REPLAYED_HEADER",
    "RETENTION_SECONDS",
    "IdempotentRoute",
    "key_in_progress",
    "key_interrupted",
    "key_reused",
    "request_fingerprint",
]

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
"""The request header a client sends its key in."""

REPLAYED_HEADER = "Idempotent-Replayed"
"""The response header the server sets to `true` on a stored response."""

RETENTION_SECONDS = 24 * 60 * 60
"""How long the server remembers a key, counted from the first request with it."""

IN_PROGRESS_SECONDS = 60
"""How long the server treats a key with no recorded outcome as a request that's still running.

A request takes that long only on a server too overloaded to give it a worker thread, so an
older key with no outcome almost always belongs to a request that the server stopped in the
middle of. A retry of a request that's still running after this time gets
`idempotency_key_interrupted`, and never runs the request itself."""

_KEY = re.compile(r"[\x21-\x7e]{1,255}")

_INTERRUPTED = 0

_log = logging.getLogger("bxapi")


def request_fingerprint(method: str, path: str, query: bytes, role: str, body: bytes) -> str:
    """Return a hash of what a request asks for, to tell a retry from another request with the same key.

    Args:
        method: The request's method.
        path: The request's path.
        query: The request's query string, as sent.
        role: The role of the caller's API key. A player and a referee can get different responses to
            the same request.
        body: The request's body, as sent.

    Returns:
        32 hexadecimal digits.

    Examples:
        ```python
        from bxapi.idempotency import request_fingerprint

        first = request_fingerprint("POST", "/v1/dice/roll", b"", "player", b'{"expression": "3d6"}')
        again = request_fingerprint("POST", "/v1/dice/roll", b"", "player", b'{"expression": "3d6"}')
        other = request_fingerprint("POST", "/v1/dice/roll", b"", "player", b'{"expression": "4d6"}')
        assert first == again != other
        ```
    """
    digest = hashlib.blake2b(digest_size=16)
    for part in (method.encode(), path.encode(), query, role.encode(), body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def key_reused() -> ApiError:
    """Build the 422 for a key that the client already sent with another request.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `idempotency_key_reused`.
    """
    return invalid(
        "idempotency_key_reused",
        "Your client already sent this Idempotency-Key with another request: a different method, path, query, body, "
        "or role. Send each new request with a new key, and each retry with the key and the request unchanged.",
    )


def key_in_progress() -> ApiError:
    """Build the 409 for a key whose first request is still running.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `idempotency_key_in_progress` and the
        header `Retry-After: 1`.
    """
    return ApiError(
        409,
        "idempotency_key_in_progress",
        "The first request with this Idempotency-Key is still running. Wait a second and send the request again.",
        headers={"Retry-After": "1"},
    )


def key_interrupted() -> ApiError:
    """Build the 409 for a key whose first request ended with no recorded outcome.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `idempotency_key_interrupted`.
    """
    return ApiError(
        409,
        "idempotency_key_interrupted",
        "The server failed or stopped while it handled the first request with this Idempotency-Key, and it has no "
        "record of whether that request took effect. Read the resource the request would have changed. If the "
        "request didn't take effect, send it again with a new key.",
    )


def _stored_response(row: IdempotencyRow, fingerprint: str, now: int) -> Response:
    if row.fingerprint != fingerprint:
        raise key_reused()
    if row.status is None and now - row.claimed_at < IN_PROGRESS_SECONDS:
        raise key_in_progress()
    if row.status is None or row.status == _INTERRUPTED:
        raise key_interrupted()
    return Response(
        content=row.body or b"",
        status_code=row.status,
        media_type="application/json",
        headers={REPLAYED_HEADER: "true"},
    )


async def _record(write: Callable[..., None], client: str, key: str, **outcome: Any) -> None:
    # The endpoint has finished, so its response or its error goes to the caller whether or not this
    # write succeeds. A key whose write failed has no recorded outcome, and a retry with it gets
    # `idempotency_key_in_progress` and then `idempotency_key_interrupted`.
    try:
        await run_in_threadpool(write, client, key, **outcome)
    except sqlite3.Error:
        _log.exception("couldn't record the outcome of a request with an idempotency key for the client %s", client)


class IdempotentRoute(APIRoute):
    """A route that runs a `POST` with an `Idempotency-Key` once, and answers each retry with the stored response.

    Pass the class as `route_class` to an `APIRouter`. A request with another method, or without the
    header, runs as on any route.

    For a `POST` with the header, the route claims the key for the caller's client before it runs
    the endpoint, in one database transaction. Then:

    - A 2xx response is stored with the key and sent. A later request with the same key, method,
      path, query string, body, and role gets the stored status and body, with the header
      `Idempotent-Replayed: true`. The endpoint doesn't run again.
    - An error that the server reports on purpose, which is every problem document except 500
      `internal_error`, means that the request changed nothing. The route forgets the key, so the
      same request with the same key runs again.
    - Any other exception leaves the request's effect unknown. A later request with the key gets
      409 `idempotency_key_interrupted`, and so does one that arrives after a server stopped in the
      middle of the first request.
    - While the first request runs, a second one with the key gets 409
      `idempotency_key_in_progress`.
    - If the database refuses the write that records the outcome, the caller still gets the
      endpoint's response or error, and the server logs the failure. The key has no recorded
      outcome, so a retry never runs the request again.
    - The same key with another request gets 422 `idempotency_key_reused`.

    A key is 1 to 255 printable ASCII characters with no spaces, and another value gets 422
    `validation_error`. The server forgets a key
    [`RETENTION_SECONDS`][bxapi.idempotency.RETENTION_SECONDS] after its first request. A key
    belongs to the client that sent it, so two clients can use the same value.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Return the route's handler, which checks the key before it runs the endpoint."""
        handler = super().get_route_handler()

        async def idempotent(request: Request) -> Response:
            key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
            if key is None or request.method != "POST":
                return await handler(request)
            if not _KEY.fullmatch(key):
                raise validation_error(
                    [
                        {
                            "type": "string_pattern_mismatch",
                            "loc": ("header", IDEMPOTENCY_KEY_HEADER),
                            "msg": "An idempotency key is 1 to 255 printable ASCII characters, with no spaces.",
                        }
                    ]
                )
            db: Database = request.app.state.db
            principal = request.state.principal
            client: str = principal.client
            try:
                body = await request.body()
            except ClientDisconnect:
                # FastAPI answers a client that disconnects in the middle of the body the same way.
                raise StarletteHTTPException(status_code=400, detail="There was an error parsing the body") from None
            fingerprint = request_fingerprint(
                request.method, request.url.path, request.scope["query_string"], principal.role, body
            )
            now = int(time.time())
            earlier = await run_in_threadpool(
                db.claim_idempotency_key, client, key, fingerprint, now=now, retention_seconds=RETENTION_SECONDS
            )
            if earlier is not None:
                return _stored_response(earlier, fingerprint, now)
            try:
                response = await handler(request)
            except Exception as error:
                if is_reported_error(error):
                    await _record(db.release_idempotency_key, client, key)
                else:
                    await _record(db.complete_idempotency_key, client, key, status=_INTERRUPTED, body=None)
                raise
            if 200 <= response.status_code < 300:
                stored = bytes(getattr(response, "body", b""))
                await _record(db.complete_idempotency_key, client, key, status=response.status_code, body=stored)
            else:
                await _record(db.release_idempotency_key, client, key)
            return response

        return idempotent
