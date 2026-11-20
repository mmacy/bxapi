"""The application factory.

[`create_app`][bxapi.app.create_app] builds the FastAPI application: the database, the error
handlers, the request size limit, the cross-origin headers, and every router. Serve it with the
`bxapi` command, or point an ASGI server at the factory:

```sh
uvicorn bxapi.app:create_app --factory --port 8640
```
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bxapi import __version__
from bxapi.auth import authenticate
from bxapi.db import Database
from bxapi.errors import PROBLEM_MEDIA_TYPE, ApiError, install_error_handlers, problem_response, problem_responses
from bxapi.routers import V1_ROUTERS, meta
from bxapi.settings import Settings

__all__ = ["BodyLimitMiddleware", "create_app"]

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
    {"name": "characters", "description": "Create characters and store them for the games that play them."},
    {"name": "adventures", "description": "Upload, validate, and list the adventures a session can play."},
    {"name": "sessions", "description": "Start a game, send it commands, and read what the players see."},
    {"name": "saves", "description": "Save a game, and start a new session from a save."},
]


class BodyLimitMiddleware:
    """Refuse a request whose body is larger than the server allows.

    The middleware answers a request with a `Content-Length` over the limit before it reads
    the body. For a request with no `Content-Length`, it counts the bytes as they arrive and
    stops reading when the count passes the limit. Either way the caller gets a 413 problem
    document with the code `content_too_large`.

    Args:
        app: The ASGI application to wrap.
        max_bytes: The largest body to accept, in bytes.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request to the wrapped application unless its body is too large."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        detail = f"The request body is larger than the {self.max_bytes} bytes this server accepts."
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = problem_response(ApiError(413, "content_too_large", detail).problem())
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises an HTTPException raised while it reads the body, and turns
                    # any other exception into a 400.
                    raise StarletteHTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    app.state.db.close()


def _operation_id(route: APIRoute) -> str:
    # The endpoint function's name, like "roll_dice", so a generated client gets short method names.
    return route.name


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
            )
            for path in schema.get("paths", {}).values():
                for operation in path.values():
                    for status, response in operation.get("responses", {}).items():
                        content = response.get("content", {})
                        if status.startswith(("4", "5")) and "application/json" in content:
                            # FastAPI documents every response model as application/json. The server
                            # sends every error as a problem document.
                            problem = {"schema": {"$ref": "#/components/schemas/Problem"}}
                            response["content"] = {PROBLEM_MEDIA_TYPE: problem}
            schemas = schema.get("components", {}).get("schemas", {})
            for unused in ("HTTPValidationError", "ValidationError"):
                schemas.pop(unused, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the bxapi application.

    Call it once per process. A test builds a separate application for each test, with settings
    that point at a temporary database.

    Every route under `/v1` requires a valid API key when the settings contain any keys.
    `GET /health` never does.

    The function opens the database in the settings and puts it at `app.state.db`. It opens the
    database here rather than at startup, so a test client that never runs the application's
    startup can still use it. When the server shuts the application down, the application closes
    the database.

    Args:
        settings: The settings to run with. When it's `None`, the function reads them from the
            `BXAPI_` environment variables.

    Returns:
        The application, ready to serve.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:")))
        assert client.get("/health").json() == {"status": "ok"}
        ```
    """
    settings = settings if settings is not None else Settings()
    app = FastAPI(
        title="bxapi",
        version=__version__,
        description=_DESCRIPTION,
        openapi_tags=_TAGS,
        lifespan=_lifespan,
        generate_unique_id_function=_operation_id,
        # A path with a trailing slash is a 404, not a redirect to a path that may not exist either.
        redirect_slashes=False,
    )
    _install_openapi(app)
    app.state.settings = settings
    app.state.db = Database(settings.database)
    install_error_handlers(app)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    if settings.cors_origins:
        # Added last so it wraps everything, and a 413 from the size limit gets the headers too.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(meta.health_router)
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)], responses=problem_responses(401, 422))
    for router in V1_ROUTERS:
        v1.include_router(router)
    app.include_router(v1)
    return app
