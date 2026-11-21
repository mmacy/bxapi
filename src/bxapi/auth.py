"""Who is calling the API, and what their role allows.

A client sends its API key as a bearer token:

```text
Authorization: Bearer <key>
```

The server looks the key up in [`Settings.api_keys`][bxapi.settings.Settings.api_keys] and gets
a [`Principal`][bxapi.auth.Principal]: the client's name and its
[`Role`][bxapi.settings.Role]. A server started with no keys and with
[`allow_anonymous`][bxapi.settings.Settings.allow_anonymous] set asks for no key and treats
every caller as a referee for the client named `local`.

[`AuthMiddleware`][bxapi.auth.AuthMiddleware] checks the key of every request under `/v1`
before anything reads the request's body, so a caller without a valid key gets a 401 and the
server parses nothing it sent. An endpoint declares what it needs with one of two parameter
types. [`CurrentPrincipal`][bxapi.auth.CurrentPrincipal] accepts any caller with a valid key.
[`RefereePrincipal`][bxapi.auth.RefereePrincipal] accepts only a referee.
"""

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from bxapi.errors import ApiError, forbidden, problem_response
from bxapi.settings import Role, Settings

__all__ = [
    "LOCAL_CLIENT",
    "AuthMiddleware",
    "CurrentPrincipal",
    "Principal",
    "RefereePrincipal",
    "authenticate",
    "find_principal",
    "require_referee",
]

LOCAL_CLIENT = "local"
"""The client name every caller gets when the server allows anonymous callers."""

_PROTECTED_PREFIX = "/v1"

_bearer = HTTPBearer(
    auto_error=False,
    description="An API key from the server's `BXAPI_API_KEYS` setting.",
)


class Principal(BaseModel):
    """The caller of a request."""

    model_config = ConfigDict(frozen=True)

    client: str
    """The name of the client application. Stored resources belong to a client, and a caller
    reads and changes only its client's resources."""
    role: Role
    """What the caller may do."""

    @property
    def is_referee(self) -> bool:
        """Whether the caller has the referee role."""
        return self.role is Role.REFEREE


def _digest(secret: bytes) -> bytes:
    return hashlib.sha256(secret).digest()


def find_principal(settings: Settings, authorization: str | None) -> Principal | None:
    """Return the caller that an `Authorization` header identifies, or `None`.

    The function compares the SHA-256 digest of the token with the digest of every key, with no
    early exit, so the time it takes doesn't depend on which key matched or on how much of a key
    the token repeats. The scheme name `Bearer` is case-insensitive.

    Args:
        settings: The server's settings.
        authorization: The value of the request's `Authorization` header, decoded as Latin-1
            the way Starlette decodes every header, or `None` if the request has none.

    Returns:
        The caller, or `None` if the header is missing, isn't a bearer token, or matches no key.
        When the server has no API keys, the caller is a referee for the client `local`.

    Examples:
        ```python
        from bxapi.auth import find_principal
        from bxapi.settings import ApiKey, Settings

        key = ApiKey.model_validate({"client": "my-game", "key": "s3cret-0123456789"})
        settings = Settings(database=":memory:", api_keys=(key,))
        assert find_principal(settings, "Bearer s3cret-0123456789").client == "my-game"
        assert find_principal(settings, "Bearer s3cret-0123456789x") is None
        ```
    """
    if not settings.auth_enabled:
        return Principal(client=LOCAL_CLIENT, role=Role.REFEREE)
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    sent = _digest(token.encode("latin-1"))
    match = None
    for entry in settings.api_keys:
        if hmac.compare_digest(sent, _digest(entry.key.get_secret_value().encode("ascii"))):
            match = entry
    return None if match is None else Principal(client=match.client, role=match.role)


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "Send a valid API key as a bearer token in the Authorization header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthMiddleware:
    """Refuse a request under `/v1` that has no valid API key, before anything reads its body.

    The middleware puts the caller it finds at `request.state.principal`, where
    [`authenticate`][bxapi.auth.authenticate] reads it. A request without a valid key gets the
    401 problem document with the code `unauthorized`. A path outside `/v1`, like `/health` or
    `/docs`, needs no key.

    Args:
        app: The ASGI application to wrap.
        settings: The server's settings, with the keys to accept.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application if it needs no key or has a valid one."""
        path: str = scope.get("path", "")
        if scope["type"] != "http" or not (path == _PROTECTED_PREFIX or path.startswith(_PROTECTED_PREFIX + "/")):
            await self.app(scope, receive, send)
            return
        principal = find_principal(self.settings, Headers(scope=scope).get("authorization"))
        if principal is None:
            error = _unauthorized()
            await problem_response(error.problem(), error.headers)(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def authenticate(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    """Return the caller of a request, or refuse the request.

    FastAPI calls it for every endpoint that declares a
    [`CurrentPrincipal`][bxapi.auth.CurrentPrincipal] or a
    [`RefereePrincipal`][bxapi.auth.RefereePrincipal] parameter. Under `/v1`, it returns the
    caller that [`AuthMiddleware`][bxapi.auth.AuthMiddleware] found. Elsewhere, it checks the
    key itself.

    Args:
        request: The request, which gives access to the server's settings.
        credentials: The bearer token from the `Authorization` header, if the request has one.
            The parameter makes the OpenAPI document describe the bearer scheme.

    Returns:
        The caller. When the server allows anonymous callers, it's a referee for the client
        `local`.

    Raises:
        ApiError: With status 401 and the code `unauthorized`, if the server has API keys and
            the request has no bearer token or one that matches no key.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        principal = find_principal(request.app.state.settings, request.headers.get("authorization"))
    if principal is None:
        raise _unauthorized()
    return principal


CurrentPrincipal = Annotated[Principal, Depends(authenticate)]
"""Annotate an endpoint parameter with this type to require a valid API key of either role."""


def require_referee(principal: CurrentPrincipal) -> Principal:
    """Return the caller if it's a referee, or refuse the request.

    Args:
        principal: The authenticated caller.

    Returns:
        The same caller.

    Raises:
        ApiError: With status 403 and the code `referee_required`, if the caller is a player.
    """
    if not principal.is_referee:
        raise forbidden("referee_required", "This request needs an API key with the referee role.")
    return principal


RefereePrincipal = Annotated[Principal, Depends(require_referee)]
"""Annotate an endpoint parameter with this type to require an API key with the referee role."""
