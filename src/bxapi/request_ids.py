"""The id the server gives each request, and the line it logs for each one.

[`RequestIdMiddleware`][bxapi.request_ids.RequestIdMiddleware] gives every request an id, like
`req_Zk3uY0mWq1x8Jr5cTt2aLw`, and returns it in the `X-Request-Id` response header. A problem
document repeats it as `request_id`. When a request fails, the client developer sends the id to
whoever runs the server, who finds the request's line in the log:

```text
request_id=req_Zk3uY0mWq1x8Jr5cTt2aLw method=GET path=/v1/meta status=200 duration_ms=0.4 client=my-game
```

A client can choose the id itself by sending `X-Request-Id`, to follow one request through its
own systems and the server's log.
"""

import logging
import re
import time
from urllib.parse import quote

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bxapi.ids import new_id

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "request_id_of"]

REQUEST_ID_HEADER = "X-Request-Id"
"""The request header a client can send an id in, and the response header the server returns the id in."""

_ACCEPTED = re.compile(r"[A-Za-z0-9._~-]{1,128}")

_access_log = logging.getLogger("bxapi.access")


def request_id_of(scope: Scope) -> str | None:
    """Return the id that [`RequestIdMiddleware`][bxapi.request_ids.RequestIdMiddleware] gave a request.

    Args:
        scope: The request's ASGI scope.

    Returns:
        The id, or `None` if the request didn't pass through the middleware.
    """
    return scope.get("state", {}).get("request_id")


class RequestIdMiddleware:
    """Give each request an id, return it in `X-Request-Id`, and log one line when the response is sent.

    The middleware uses the `X-Request-Id` the client sent when it's 1 to 128 letters, digits, and
    the characters `.`, `_`, `~`, and `-`. Otherwise it draws a new id that starts with `req_`. It
    puts the id at `request.state.request_id`, where the error handlers read it.

    The log line goes to the `bxapi.access` logger at the level `INFO`. It has the id, the method,
    the path without the query string, the status, the time the server took in milliseconds, and
    the client name of the API key, or `-` when the request had no valid key. The line never
    contains a key, a query string, or a body.

    Args:
        app: The ASGI application to wrap.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:", allow_anonymous=True)))
        assert client.get("/health").headers["X-Request-Id"].startswith("req_")
        assert client.get("/health", headers={"X-Request-Id": "trace-42"}).headers["X-Request-Id"] == "trace-42"
        ```
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application with an id, and log the request when it's done."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = sent if sent is not None and _ACCEPTED.fullmatch(sent) else new_id("req")
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        # A request that raises an exception gets its 500 from Starlette's outermost error
        # middleware, which this function's `send` never sees.
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            principal = state.get("principal")
            _access_log.info(
                "request_id=%s method=%s path=%s status=%d duration_ms=%.1f client=%s",
                request_id,
                scope["method"],
                quote(scope["path"]),
                status,
                (time.perf_counter() - started) * 1000,
                "-" if principal is None else principal.client,
            )
