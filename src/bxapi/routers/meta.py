"""The health check, and the endpoint that reports the server's versions and the caller."""

from fastapi import APIRouter, Request
from osrlib.versioning import SCHEMA_VERSION, engine_version

from bxapi import __version__
from bxapi.auth import CurrentPrincipal
from bxapi.models import ApiModel
from bxapi.settings import Role

__all__ = ["Health", "Meta", "health_router", "router"]

health_router = APIRouter(tags=["meta"])
router = APIRouter(tags=["meta"])


class Health(ApiModel):
    """The body of the health check."""

    status: str = "ok"
    """Always `"ok"`. A server that can't answer returns no body at all."""


class Meta(ApiModel):
    """The server's versions, and the caller as the server sees it."""

    api_version: str
    """The version of bxapi, like `"0.1.0"`."""
    engine_version: str
    """The version of osrlib that applies the rules."""
    schema_version: int
    """The version of osrlib's document format. Adventure, character, and save documents are
    stamped with it, and the server refuses a document stamped with a higher one."""
    auth_enabled: bool
    """Whether the server asks for an API key."""
    client: str
    """The client application the caller's API key identifies, or `"local"` when the server
    asks for no key."""
    role: Role
    """The caller's role."""


@health_router.get("/health", summary="Check that the server is up")
def health() -> Health:
    """Return `{"status": "ok"}`. The endpoint needs no API key and reads no data."""
    return Health()


@router.get("/meta", summary="Get the server's versions and the caller's role")
def get_meta(request: Request, principal: CurrentPrincipal) -> Meta:
    """Return the versions of bxapi and osrlib, and the client and role of the API key you sent.

    Call it when your application starts, to check that the key works and that the server's
    `schema_version` isn't lower than the one your stored documents need.
    """
    return Meta(
        api_version=__version__,
        engine_version=engine_version(),
        schema_version=SCHEMA_VERSION,
        auth_enabled=request.app.state.settings.auth_enabled,
        client=principal.client,
        role=principal.role,
    )
