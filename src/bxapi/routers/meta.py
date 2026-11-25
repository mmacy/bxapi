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
    """Liveness response body returned by `GET /health`."""

    status: str = "ok"
    """Always `"ok"`."""


class Storage(ApiModel):
    """Stored document byte usage and quota for your client application.

    Counts the stored JSON bytes of your client's ability rolls, characters, adventures, sessions,
    saves, and 24-hour idempotency replay responses.
    """

    used_bytes: int
    """Bytes currently stored for your client. Deleting sessions, saves, adventures, or characters
    reduces this count."""
    max_bytes: int | None
    """Maximum bytes the server stores for one client (`BXAPI_MAX_CLIENT_BYTES`, 1,073,741,824 by
    default), or `null` when storage quotas are disabled. When `used_bytes` reaches `max_bytes`,
    requests that store new documents fail with 507 `storage_full`."""


class Meta(ApiModel):
    """Server versions, authentication mode, caller identity, and client storage usage."""

    api_version: str
    """Installed bxapi version string, such as `"0.1.0"`."""
    engine_version: str
    """Installed osrlib rules engine version string, such as `"2.6.0"`."""
    schema_version: int
    """osrlib document format version. The server refuses adventure, character, party, and save
    documents stamped with a higher `schema_version` with 409 `version_unsupported`."""
    auth_enabled: bool
    """Whether the server requires a bearer API key under `/v1`."""
    client: str
    """Client application name bound to your API key, or `"local"` when `auth_enabled` is `false`."""
    role: Role
    """Role granted to your API key (`"player"` or `"referee"`)."""
    storage: Storage
    """Current document storage usage and quota for `client`."""


@health_router.get("/health", summary="Check that the server is up", response_description='`{"status": "ok"}`.')
def get_health() -> Health:
    """Return `{"status": "ok"}` while the HTTP server is accepting requests.

    Requires no API key, touches no database tables, and has no rate limit, so load balancers
    and container health probes can poll it frequently. Does not verify database connectivity or API
    keys. To verify an API key and inspect server versions, call `GET /v1/meta`.
    """
    return Health()


@router.get(
    "/meta",
    summary="Get the server's versions and the caller's role",
    response_description="Server versions, authentication mode, caller client and role, and storage usage.",
)
def get_meta(request: Request, principal: CurrentPrincipal) -> Meta:
    """Verify your API key and return the server's versions, your client and role, and your storage usage.

    Call this operation at application startup. A 200 response confirms that your bearer token is
    valid, and `role` indicates whether your key can call referee-only operations (`"referee"`) or
    player operations (`"player"`). Compare `schema_version` against any adventure, character, or
    save documents your client stores before uploading or importing them, because documents with a
    higher `schema_version` fail with 409 `version_unsupported`.

    Inspect `storage.used_bytes` and `storage.max_bytes` to monitor your client's document quota
    before hitting 507 `storage_full` or to confirm space reclaimed after deleting resources.
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
