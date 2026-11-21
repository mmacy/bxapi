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
    """Always `"ok"`."""


class Meta(ApiModel):
    """The server's versions, and the caller as the server sees it."""

    api_version: str
    """The version of bxapi, like `"0.1.0"`."""
    engine_version: str
    """The version of osrlib that applies the rules."""
    schema_version: int
    """The version of osrlib's document format. osrlib stamps adventure, character, and save
    documents with it, and the server refuses a document stamped with a higher one."""
    auth_enabled: bool
    """Whether the server asks for an API key."""
    client: str
    """The client application the caller's API key identifies, or `"local"` when the server
    allows anonymous callers."""
    role: Role
    """The caller's role."""


@health_router.get("/health", summary="Check that the server is up", response_description='`{"status": "ok"}`.')
def get_health() -> Health:
    """Return `{"status": "ok"}` while the server can answer requests.

    The endpoint needs no API key and reads no data, so a load balancer or a container's health check can call it
    often. It doesn't check the database or the API key. To check an API key, call `GET /v1/meta`.
    """
    return Health()


@router.get(
    "/meta",
    summary="Get the server's versions and the caller's role",
    response_description="The versions, whether the server asks for a key, and the caller's client and role.",
)
def get_meta(request: Request, principal: CurrentPrincipal) -> Meta:
    """Return the versions of bxapi and osrlib, and the client and role of the API key you sent.

    Call it when a client starts. A 200 means the key works, and `role` says whether the client can use the
    operations only a referee may call. If the client keeps documents, like save documents, compare
    `schema_version` with the `schema_version` of each document. The server refuses a document with a higher one,
    with 409 `version_unsupported`.
    """
    return Meta(
        api_version=__version__,
        engine_version=engine_version(),
        schema_version=SCHEMA_VERSION,
        auth_enabled=request.app.state.settings.auth_enabled,
        client=principal.client,
        role=principal.role,
    )
