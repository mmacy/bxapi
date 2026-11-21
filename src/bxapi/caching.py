"""Conditional requests and `HEAD`: how a client avoids downloading a response it already has.

[`ConditionalGetMiddleware`][bxapi.caching.ConditionalGetMiddleware] gives every successful `GET`
response an `ETag`, a short name for the response's content. A client that has a response sends
its `ETag` back in `If-None-Match`, and gets 304 with no body while the content is the same:

```text
GET /v1/sessions/ses_Zk3uY0mWq1x8Jr5cTt2aLw/view
If-None-Match: W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"

304 Not Modified
ETag: W/"5f0c1b6de0a94d7f8f3f0f5b7a1c2d3e"
```

[`HeadMiddleware`][bxapi.caching.HeadMiddleware] answers a `HEAD` request with the status and
headers of the `GET` response, and no body.
"""

import hashlib

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["REVALIDATE", "ConditionalGetMiddleware", "HeadMiddleware", "etag_matches", "etag_of"]

REVALIDATE = "private, no-cache"
"""The `Cache-Control` value of a successful `GET` response under `/v1` that sets none itself.

A browser may store the response, and it asks the server whether the response is still current
before it uses its copy. A cache that serves more than one user may not store it."""

_API_PREFIX = "/v1/"


def etag_of(body: bytes) -> str:
    """Return the `ETag` for a response body.

    The tag is weak, as the `W/` prefix states, because the server sends the same content
    compressed and uncompressed with the same tag.

    Args:
        body: The response body, before compression.

    Returns:
        `W/"` and 32 hexadecimal digits of the body's BLAKE2b hash, and `"`.

    Examples:
        ```python
        from bxapi.caching import etag_of

        assert etag_of(b'{"status":"ok"}') == etag_of(b'{"status":"ok"}')
        assert etag_of(b'{"status":"ok"}') != etag_of(b"{}")
        assert etag_of(b"{}").startswith('W/"')
        ```
    """
    return f'W/"{hashlib.blake2b(body, digest_size=16).hexdigest()}"'


def _opaque(tag: str) -> str:
    return tag.strip().removeprefix("W/")


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Return whether an `If-None-Match` header lists an `ETag`.

    The comparison is the weak one that RFC 9110 defines for `If-None-Match`: two tags match when
    they're the same after their `W/` prefixes are removed.

    Args:
        if_none_match: The header's value, or `None` if the request has no such header.
        etag: The `ETag` of the response the server would send.

    Returns:
        `True` if the header is `*` or lists the tag.

    Examples:
        ```python
        from bxapi.caching import etag_matches

        assert etag_matches('W/"abc"', 'W/"abc"')
        assert etag_matches('"xyz", "abc"', 'W/"abc"')
        assert etag_matches("*", 'W/"abc"')
        assert not etag_matches('"xyz"', 'W/"abc"')
        assert not etag_matches(None, 'W/"abc"')
        ```
    """
    if if_none_match is None:
        return False
    if if_none_match.strip() == "*":
        return True
    wanted = _opaque(etag)
    return any(_opaque(tag) == wanted for tag in if_none_match.split(","))


class ConditionalGetMiddleware:
    """Give each 200 response to a `GET` an `ETag`, and answer 304 when the client already has that response.

    The middleware reads the whole response body, which the application has in memory already,
    and hashes it. When the request's `If-None-Match` lists the tag, the middleware sends 304 with
    the response's headers and no body. The application still does the work of building the
    response, so a 304 saves the transfer and not the work.

    A 200 response under `/v1` that has no `Cache-Control` header gets
    [`REVALIDATE`][bxapi.caching.REVALIDATE], so a browser checks with the server before it uses a
    stored copy. A response with another status, or to another method, passes through unchanged.

    Args:
        app: The ASGI application to wrap.
        vary: A request header to list in the `Vary` header of each 304, or `None` to add none.
            Pass `"Accept-Encoding"` when a compression middleware wraps this one: that middleware
            adds the header to a 200 it compresses, and a 304 has no body for it to compress.
    """

    def __init__(self, app: ASGIApp, vary: str | None = None) -> None:
        self.app = app
        self.vary = vary

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application, and send 304 in place of a response the client has."""
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []

        async def send_conditionally(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start" and message["status"] == 200:
                start = message
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            headers = MutableHeaders(scope=start)
            etag = etag_of(body)
            headers["ETag"] = etag
            if scope["path"].startswith(_API_PREFIX) and "cache-control" not in headers:
                headers["Cache-Control"] = REVALIDATE
            if etag_matches(Headers(scope=scope).get("if-none-match"), etag):
                for name in ("content-length", "content-type"):
                    del headers[name]
                if self.vary is not None and self.vary.lower() not in headers.get("vary", "").lower():
                    headers.add_vary_header(self.vary)
                start["status"] = 304
                body = b""
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_conditionally)


class HeadMiddleware:
    """Answer a `HEAD` request with the status and headers of the `GET` response, and no body.

    The middleware passes the request on as a `GET` and drops the body of the response. The
    `Content-Length` header still states the length of the body that a `GET` returns.

    Args:
        app: The ASGI application to wrap.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass a `HEAD` request to the wrapped application as a `GET`, and send the response without its body."""
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)
