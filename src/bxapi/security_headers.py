"""The response headers that tell a browser how to treat the server's responses.

A client application reads the API's responses with its own code. A browser that loads one as a
page, because a person followed a link to the API or another site put the URL in a frame, must
not run or show anything in it.
[`SecurityHeadersMiddleware`][bxapi.security_headers.SecurityHeadersMiddleware] adds these headers
to every response:

- `X-Content-Type-Options: nosniff`. The browser uses the response's `Content-Type`, and never
  guesses that a JSON body with text from another user is HTML or a script.
- `X-Frame-Options: DENY`. No page can show the response in a frame, so no page can put its own
  buttons over the interactive documentation at `/docs`.
- `Referrer-Policy: no-referrer`. A link from the documentation pages to another site doesn't tell
  that site the server's address.
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, on every response that
  sets no policy of its own. A browser that shows the response as a page loads and runs nothing
  from it. The documentation pages at `/docs` and `/redoc` load scripts, so each sets a policy
  that allows its own files, as [`bxapi.docs_pages`][bxapi.docs_pages] describes.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["API_HEADERS", "CONTENT_SECURITY_POLICY", "PAGE_HEADERS", "SecurityHeadersMiddleware"]

CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"
"""The `Content-Security-Policy` of every response that sets none of its own."""

PAGE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
"""The headers of every response, including the documentation pages."""

API_HEADERS = PAGE_HEADERS | {"Content-Security-Policy": CONTENT_SECURITY_POLICY}
"""The headers of every response that sets no `Content-Security-Policy` of its own."""


class SecurityHeadersMiddleware:
    """Add the browser security headers to every response.

    Every response gets [`PAGE_HEADERS`][bxapi.security_headers.PAGE_HEADERS], which replace a
    header of the same name that the application set. A response that sets no
    `Content-Security-Policy` gets
    [`CONTENT_SECURITY_POLICY`][bxapi.security_headers.CONTENT_SECURITY_POLICY].

    Args:
        app: The ASGI application to wrap.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:", allow_anonymous=True)))
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
        assert "script-src" in client.get("/docs").headers["Content-Security-Policy"]
        ```
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application, and add the headers to its response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.update(PAGE_HEADERS)
                headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)
