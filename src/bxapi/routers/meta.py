"""The health check, and the endpoint that reports the server's versions and the caller."""

from fastapi import APIRouter, Request
from osrlib.versioning import SCHEMA_VERSION, engine_version

from bxapi import __version__
from bxapi.auth import CurrentPrincipal
from bxapi.models import ApiModel
from bxapi.settings import Role

__all__ = ["Health", "Meta", "Storage", "health_router", "router"]

health_router = APIRouter(tags=["meta"])
router = APIRouter(tags=["meta"])


class Health(ApiModel):
    """The body of the health check."""

    status: str = "ok"
    """Always `"ok"`."""


class Storage(ApiModel):
    """The number of bytes of documents the server stores for a client, and the storage limit.

    The count covers the client's characters, ability rolls, adventures, sessions, and saves, plus the responses the
    server stores for the client's idempotency keys.
    """

    used_bytes: int
    """The number of bytes the server currently stores for the client. Deleting a session, a save, an adventure, or a
    character lowers it."""
    max_bytes: int | None
    """The maximum number of bytes the server stores for one client, or `null` when the server has no limit. After
    `used_bytes` reaches the maximum, each request that would store more receives `507 storage_full`."""


class Meta(ApiModel):
    """The server's versions, and the caller's identity as the server sees it."""

    api_version: str
    """The version of bxapi, such as `"0.1.0"`."""
    engine_version: str
    """The version of osrlib that applies the rules."""
    schema_version: int
    """The version of osrlib's document format. osrlib stamps adventure, character, and save documents with it, and the
    server rejects a document stamped with a higher version."""
    auth_enabled: bool
    """Whether the server requires an API key."""
    client: str
    """The client application that the caller's API key identifies, or `"local"` when the server runs in anonymous
    mode."""
    role: Role
    """The caller's role."""
    storage: Storage
    """The number of bytes the server stores for the caller's client, and the limit."""


@health_router.get("/health", summary="Check that the server is up", response_description='`{"status": "ok"}`.')
def get_health() -> Health:
    """Return `{"status": "ok"}` while the server can answer requests.

    The endpoint requires no API key and reads no data, so a load balancer or a container health check can call it
    frequently. It doesn't check the database or the API key. To verify an API key, call `GET /v1/meta`.
    """
    return Health()


@router.get(
    "/meta",
    summary="Get the server's versions and the caller's role",
    response_description="The versions, whether the server requires a key, and the caller's client and role.",
)
def get_meta(request: Request, principal: CurrentPrincipal) -> Meta:
    """Return the versions of bxapi and osrlib, and the client and role of the API key you sent.

    Call it when a client starts. A `200` means the key works, and `role` tells you whether the client can call the
    referee-only operations. If the client keeps documents, such as save documents, compare `schema_version` with the
    `schema_version` of each document. The server rejects a document with a higher version and returns
    `409 version_unsupported`.

    `storage` reports how many bytes of documents the server stores for your client, and the limit. Read it to see how
    close the client is to `507 storage_full`, and how many bytes a delete freed.
    """
    return Meta(
        api_version=__version__,
        engine_version=engine_version(),
        schema_version=SCHEMA_VERSION,
        auth_enabled=request.app.state.settings.auth_enabled,
        client=principal.client,
        role=principal.role,
        storage=Storage(
            used_bytes=request.app.state.db.get_usage(principal.client),
            max_bytes=request.app.state.settings.max_client_bytes or None,
        ),
    )
