"""The application factory.

[`create_app`][bxapi.app.create_app] builds the FastAPI application: the database, the error
handlers, the API key check, the request body limits, the cross-origin headers, and every
router. Serve it with the `bxapi` command, or point an ASGI server at the factory:

```sh
uvicorn bxapi.app:create_app --factory --port 8640
```

The `bxapi` command sends the server's own log lines, like the one about API keys at startup,
to the same place as uvicorn's. Another ASGI server shows them only if you configure the
`bxapi` logger.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bxapi import __version__
from bxapi.adventures import builtin_adventures
from bxapi.auth import AuthMiddleware, authenticate
from bxapi.db import Database
from bxapi.errors import PROBLEM_MEDIA_TYPE, ApiError, install_error_handlers, problem_response, problem_responses
from bxapi.routers import V1_ROUTERS, meta
from bxapi.session_manager import SessionManager
from bxapi.settings import Settings, SettingsError

__all__ = ["BodyLimitMiddleware", "create_app"]

_log = logging.getLogger("bxapi")

_DESCRIPTION = """
The 1981 B/X (Basic/Expert) fantasy adventure game rules over HTTP, run by
[osrlib](https://github.com/mmacy/osrlib-python).

Every error is a problem document (RFC 9457) with a fixed `code`. A command the rules refuse
isn't an error: the command endpoint returns it with status 200 and `accepted` set to `false`.
"""

_TAGS = [
    {"name": "meta", "description": "The server's versions, and who the caller is."},
    {"name": "rules", "description": "The rules content: classes, monsters, spells, items, and the printed tables."},
    {"name": "dice", "description": "Roll dice expressions."},
    {"name": "generate", "description": "Roll characters, treasure, and encounters without starting a game."},
    {"name": "characters", "description": "Create characters and store them so a session can start from them."},
    {"name": "adventures", "description": "Upload, validate, and list the adventures a session can play."},
    {"name": "sessions", "description": "Start a game, send it commands, and read what the players see."},
    {"name": "saves", "description": "Save a game, and start a new session from a save."},
]

_NO_KEYS = (
    "bxapi has no API keys, so it won't start. To require a key, set BXAPI_API_KEYS to a JSON list like "
    '\'[{"client": "my-game", "key": "<at least 16 characters>", "role": "player"}]\'. To run on your own machine '
    "with no key, where every caller is a referee for the client 'local', set BXAPI_ALLOW_ANONYMOUS=true."
)

_SCHEMAS = "#/components/schemas/"
_PROBLEM_REF = _SCHEMAS + "Problem"
_FASTAPI_VALIDATION_REF = _SCHEMAS + "HTTPValidationError"


class BodyLimitMiddleware:
    """Refuse a request whose body is larger than the server allows or arrives too slowly.

    The middleware answers a request with a `Content-Length` over the size limit before it
    reads the body. For a request with no `Content-Length`, it counts the bytes as they arrive
    and stops reading when the count passes the limit. Either way the caller gets a 413 problem
    document with the code `content_too_large`.

    The whole body must arrive within the time limit, counted from when the application starts
    to read it. A body that takes longer gets a 408 problem document with the code
    `request_timeout`. The limit doesn't cover the request's headers or an idle connection,
    which a reverse proxy in front of the server has to limit.

    Args:
        app: The ASGI application to wrap.
        max_bytes: The largest body to accept, in bytes.
        timeout_seconds: The most time the body may take to arrive, in seconds.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, timeout_seconds: float) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application, and stop reading a body that breaks a limit."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = f"The request body is larger than the {self.max_bytes} bytes this server accepts."
        too_slow = f"The request body didn't arrive within the {self.timeout_seconds:g} seconds this server waits."
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = problem_response(ApiError(413, "content_too_large", too_large).problem())
            await response(scope, receive, send)
            return
        received = 0
        complete = False
        deadline: float | None = None

        async def limited_receive() -> Message:
            nonlocal received, complete, deadline
            if complete:
                # After the body, the application waits here for the client to disconnect, with no time limit.
                return await receive()
            if deadline is None:
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            try:
                async with asyncio.timeout_at(deadline):
                    message = await receive()
            except TimeoutError:
                # FastAPI re-raises an HTTPException raised while it reads the body, and turns
                # any other exception into a 400.
                raise StarletteHTTPException(status_code=408, detail=too_slow) from None
            if message["type"] != "http.request" or not message.get("more_body", False):
                complete = True
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise StarletteHTTPException(status_code=413, detail=too_large)
            return message

        await self.app(scope, limited_receive, send)


def _log_startup(settings: Settings) -> None:
    origins = ", ".join(settings.cors_origins) or "none"
    if settings.auth_enabled:
        clients = ", ".join(sorted({entry.client for entry in settings.api_keys}))
        _log.info(
            "bxapi requires an API key. Keys: %d. Clients: %s. CORS origins: %s.",
            len(settings.api_keys),
            clients,
            origins,
        )
    else:
        _log.warning(
            "bxapi asks for no API key, because BXAPI_API_KEYS is empty and BXAPI_ALLOW_ANONYMOUS is set. Every "
            "caller is a referee for the client 'local'. Keys: 0. CORS origins: %s.",
            origins,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _log_startup(app.state.settings)
    yield
    app.state.db.close()


def _operation_id(route: APIRoute) -> str:
    # The endpoint function's name, like "roll_dice", so a generated client gets short method names.
    return route.name


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": _PROBLEM_REF}}}


def _describe_problems(schema: dict[str, Any]) -> None:
    for path_name, path in schema.get("paths", {}).items():
        for operation in path.values():
            responses = operation.setdefault("responses", {})
            if path_name.startswith("/v1/") and "requestBody" in operation:
                for status in (408, 413):
                    responses.setdefault(
                        str(status), {"description": HTTPStatus(status).phrase, "content": _problem_content()}
                    )
            for response in responses.values():
                json_schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                if json_schema.get("$ref") in (_PROBLEM_REF, _FASTAPI_VALIDATION_REF):
                    # FastAPI documents every response model as application/json. The server
                    # sends every error as a problem document.
                    response["content"] = _problem_content()
    schemas = schema.get("components", {}).get("schemas", {})
    # HTTPValidationError refers to ValidationError, so check it first.
    for name in ("HTTPValidationError", "ValidationError"):
        rest = {
            "paths": schema.get("paths", {}),
            "schemas": {key: value for key, value in schemas.items() if key != name},
        }
        if name in schemas and _SCHEMAS + name not in set(_refs(rest)):
            del schemas[name]


def _discriminators(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("discriminator"), dict):
            yield value
        for item in value.values():
            yield from _discriminators(item)
    elif isinstance(value, list):
        for item in value:
            yield from _discriminators(item)


def _require_discriminators(schema: dict[str, Any]) -> None:
    # osrlib gives each tag field, like `command_type`, a default, so pydantic leaves it out of
    # `required`. A union can't be narrowed on an optional property, and the server needs the tag
    # to pick the member anyway, so each member a discriminator refers to requires it.
    schemas = schema.get("components", {}).get("schemas", {})
    for union in list(_discriminators(schema)):
        tag = union["discriminator"]["propertyName"]
        members = set(union["discriminator"].get("mapping", {}).values())
        members.update(option.get("$ref") for option in union.get("oneOf", []) if isinstance(option, dict))
        for ref in members:
            if not isinstance(ref, str) or not ref.startswith(_SCHEMAS):
                continue
            member = schemas.get(ref.removeprefix(_SCHEMAS))
            if member is not None and tag in member.get("properties", {}):
                required = member.setdefault("required", [])
                if tag not in required:
                    required.append(tag)


def _install_openapi(app: FastAPI) -> None:
    """Make the application's OpenAPI document describe error responses as problem documents."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags,
                license_info=app.license_info,
            )
            _describe_problems(schema)
            _require_discriminators(schema)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the bxapi application.

    Call it once per process. A test builds a separate application for each test, with settings
    that point at a temporary database.

    Every route under `/v1` requires a valid API key. `GET /health`, and the documentation at
    `/docs`, `/redoc`, and `/openapi.json`, need none. A server with no API keys starts only
    when [`allow_anonymous`][bxapi.settings.Settings.allow_anonymous] is set, and then asks
    for no key.

    The function loads the adventures that come with the server, so a broken adventure file
    stops the server before it serves a request. It opens the database in the settings and puts
    it at `app.state.db`, and puts the
    [`SessionManager`][bxapi.session_manager.SessionManager] that runs the sessions at
    `app.state.sessions`. It opens the database here rather than at startup, so a test client that
    never runs the application's startup can still use it. When the server starts the
    application, the application logs one line about its API keys and CORS origins: a warning
    when it asks for no key. When the server shuts the application down, the application closes
    the database.

    Args:
        settings: The settings to run with. When it's `None`, the function reads them from the
            `BXAPI_` environment variables.

    Returns:
        The application, ready to serve.

    Raises:
        SettingsError: If the settings have no API keys and don't set `allow_anonymous`.
        DatabaseOpenError: If the database file can't be opened or has another table layout.
        osrlib.errors.ContentValidationError: If an adventure that comes with the server isn't
            valid for the installed osrlib.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:", allow_anonymous=True)))
        assert client.get("/health").json() == {"status": "ok"}
        ```
    """
    settings = settings if settings is not None else Settings()
    if not settings.auth_enabled and not settings.allow_anonymous:
        raise SettingsError(_NO_KEYS)
    builtin_adventures()
    app = FastAPI(
        title="bxapi",
        version=__version__,
        description=_DESCRIPTION,
        openapi_tags=_TAGS,
        license_info={"name": "MIT", "identifier": "MIT"},
        lifespan=_lifespan,
        generate_unique_id_function=_operation_id,
        # A path with a trailing slash is a 404, not a redirect to a path that may not exist either.
        redirect_slashes=False,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    _install_openapi(app)
    app.state.settings = settings
    app.state.db = Database(settings.database)
    app.state.sessions = SessionManager(app.state.db, cache_size=settings.session_cache_size)
    install_error_handlers(app)
    # Each middleware added wraps the ones before it. A request passes the CORS headers first,
    # then the API key check, then the body limits, so the server reads no body from a caller
    # without a key.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.max_body_bytes, timeout_seconds=settings.body_timeout_seconds
    )
    app.add_middleware(AuthMiddleware, settings=settings)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "Retry-After"],
        )
    app.include_router(meta.health_router)
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)], responses=problem_responses(401, 422, 500, 503))
    for router in V1_ROUTERS:
        v1.include_router(router)
    app.include_router(v1)
    return app
