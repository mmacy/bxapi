"""Who is calling the API, and what their role allows.

A client sends its API key as a bearer token:

```text
Authorization: Bearer <key>
```

The server looks the key up in [`Settings.api_keys`][bxapi.settings.Settings.api_keys] and gets
a [`Principal`][bxapi.auth.Principal]: the client's name and its
[`Role`][bxapi.settings.Role]. When the server has no keys configured, it asks for none and
treats every caller as a referee for the client named `local`.

An endpoint declares what it needs with one of two parameter types.
[`CurrentPrincipal`][bxapi.auth.CurrentPrincipal] accepts any caller with a valid key.
[`RefereePrincipal`][bxapi.auth.RefereePrincipal] accepts only a referee.
"""

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from bxapi.errors import ApiError, forbidden
from bxapi.settings import Role, Settings

__all__ = ["LOCAL_CLIENT", "CurrentPrincipal", "Principal", "RefereePrincipal", "authenticate", "require_referee"]

LOCAL_CLIENT = "local"
"""The client name every caller gets when the server has no API keys configured."""

_bearer = HTTPBearer(
    auto_error=False,
    description="An API key from the server's `BXAPI_API_KEYS` setting. Not needed when the server has no keys.",
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


def authenticate(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    """Return the caller of a request, or refuse the request.

    FastAPI calls it for every endpoint that declares a
    [`CurrentPrincipal`][bxapi.auth.CurrentPrincipal] or a
    [`RefereePrincipal`][bxapi.auth.RefereePrincipal] parameter.

    Args:
        request: The request, which gives access to the server's settings.
        credentials: The bearer token from the `Authorization` header, if the request has one.

    Returns:
        The caller. When the server has no API keys, it's a referee for the client `local`.

    Raises:
        ApiError: With status 401 and the code `unauthorized`, if the server has API keys and
            the request has no bearer token or one that matches no key.
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return Principal(client=LOCAL_CLIENT, role=Role.REFEREE)
    if credentials is not None:
        sent = credentials.credentials.encode()
        match = None
        # Compare against every key, with no early exit, so the time taken doesn't depend on which key matched.
        for entry in settings.api_keys:
            if hmac.compare_digest(sent, entry.key.get_secret_value().encode()):
                match = entry
        if match is not None:
            return Principal(client=match.client, role=match.role)
    raise ApiError(
        401,
        "unauthorized",
        "Send a valid API key as a bearer token in the Authorization header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


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
