"""The error format the API returns, and the exceptions that produce it.

Every error response is a problem document as RFC 9457 defines it, sent as
`application/problem+json`. A client reads `code` to decide what to do and shows `detail` to
a person:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No session has the id 'ses_x'.",
  "code": "not_found",
  "resource": "session",
  "id": "ses_x"
}
```

An endpoint raises [`ApiError`][bxapi.errors.ApiError], or calls one of the functions here
that build one, and [`install_error_handlers`][bxapi.errors.install_error_handlers] turns the
exception into the response. The same handlers convert a request that fails validation, an
osrlib exception, a busy database, and any other exception.

A command that osrlib refuses isn't an error. The command endpoint responds with status 200
and `accepted` set to `false`.
"""

import logging
import sqlite3
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from osrlib.core.validation import Rejection
from osrlib.errors import ContentValidationError, ReplayVersionError, SaveVersionError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.config import JsonDict
from pydantic.json_schema import SkipJsonSchema
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "MAX_DETAIL_LENGTH",
    "MAX_FIELD_ERRORS",
    "PROBLEM_MEDIA_TYPE",
    "ApiError",
    "FieldError",
    "Problem",
    "conflict",
    "forbidden",
    "install_error_handlers",
    "invalid",
    "not_found",
    "problem_response",
    "problem_responses",
    "shorten",
]

PROBLEM_MEDIA_TYPE = "application/problem+json"
"""The media type of every error response."""

MAX_DETAIL_LENGTH = 1000
"""The most characters a problem document's `detail` has."""

MAX_FIELD_ERRORS = 20
"""The most entries the `errors` member of a `validation_error` lists."""

_MAX_TEXT_SHOWN = 128

_MAX_TAG_SHOWN = 64

_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})

_log = logging.getLogger("bxapi")


def shorten(text: str, limit: int) -> str:
    """Return text cut to at most `limit` characters, ending in `...` when it was cut.

    Use it on any text that can repeat what a caller sent, before it goes into a response.

    Args:
        text: The text to shorten.
        limit: The most characters to return. At least 3.

    Returns:
        `text` unchanged when it fits, and otherwise its start followed by `...`.

    Examples:
        ```python
        from bxapi.errors import shorten

        assert shorten("goblin", 10) == "goblin"
        assert shorten("x" * 50, 10) == "xxxxxxx..."
        ```
    """
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _leave_out_default(schema: JsonDict) -> None:
    # The server leaves an unset member out of the document instead of sending null.
    schema.pop("default", None)


def _is_none(value: object) -> bool:
    return value is None


def _optional() -> Any:
    return Field(default=None, exclude_if=_is_none, json_schema_extra=_leave_out_default)


class FieldError(BaseModel):
    """One part of a request that failed validation."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    loc: list[str | int]
    """Where the error is: `body`, `query`, `path`, or `header`, followed by the field names
    and list positions that lead to the value. The server cuts each name to 128 characters."""
    msg: str
    """What's wrong with the value, at most 128 characters."""
    type: str
    """pydantic's name for the kind of error, like `missing` or `extra_forbidden`."""


class Problem(BaseModel):
    """The body of an error response.

    Every problem document has `type`, `title`, `status`, `detail`, and `code`. The server
    leaves out the other members, except the ones that the document's code adds:

    - `validation_error` adds `errors` and `error_count`.
    - `not_found` adds `resource` and `id`.
    - `revision_conflict` adds `revision`.
    - `referee_command` adds `command_type`.
    - `character_invalid` adds `rejections` when osrlib reports them. Otherwise it adds `field`
      and `id`, and `index` when `field` is a list.

    A later release can add members, so ignore any member you don't know.
    """

    model_config = ConfigDict(extra="allow", use_attribute_docstrings=True)

    type: str = "about:blank"
    """Always `"about:blank"`. Read `code` to identify the error."""
    title: str
    """The standard phrase for the HTTP status, like `"Not Found"`."""
    status: int
    """The HTTP status code, repeated from the response."""
    detail: str
    """A short description of what went wrong, written for a person, at most 1,000 characters."""
    code: str
    """A fixed snake_case name for the error, like `"not_found"`. It doesn't change between
    releases, so a client can branch on it."""
    errors: list[FieldError] | SkipJsonSchema[None] = _optional()
    """`validation_error` only: the first 20 parts of the request that failed validation."""
    error_count: int | SkipJsonSchema[None] = _optional()
    """`validation_error` only: how many parts of the request failed validation, including any
    that `errors` leaves out."""
    resource: str | SkipJsonSchema[None] = _optional()
    """`not_found` only: the kind of resource the request asked for, like `"session"`."""
    id: str | SkipJsonSchema[None] = _optional()
    """For `not_found`, the id in the request, or its first 128 characters followed by `...`.
    For `character_invalid`, the value of the entry that's wrong, like an item id."""
    revision: int | SkipJsonSchema[None] = _optional()
    """`revision_conflict` only: the session's current revision."""
    command_type: str | SkipJsonSchema[None] = _optional()
    """`referee_command` only: the `command_type` of the referee command a player sent."""
    field: str | SkipJsonSchema[None] = _optional()
    """`character_invalid` only, when osrlib reports no rejections: the request field that's
    wrong, like `"equip_item_ids"`."""
    index: int | SkipJsonSchema[None] = _optional()
    """`character_invalid` only, when `field` is a list: the position of the entry that's
    wrong, counted from 0."""
    rejections: list[Rejection] | SkipJsonSchema[None] = _optional()
    """`character_invalid` only, and only when osrlib reports them: osrlib's reasons for
    refusing the character, each a `code` and `params`."""

    @field_validator("detail")
    @classmethod
    def _detail_is_short(cls, detail: str) -> str:
        return shorten(detail, MAX_DETAIL_LENGTH)


class ApiError(Exception):
    """An error the API reports to the caller as a problem document.

    Raise it from an endpoint, or from code an endpoint calls, when the server can't carry out
    the request. The handler that
    [`install_error_handlers`][bxapi.errors.install_error_handlers] registers sends it as a
    [`Problem`][bxapi.errors.Problem].

    Args:
        status: The HTTP status code of the response.
        code: The fixed snake_case name of the error.
        detail: What went wrong, written for a person.
        headers: Extra response headers, like `WWW-Authenticate` on a 401.
        **extras: Extra members of the problem document, like `resource="session"`.

    Examples:
        ```python
        from bxapi.errors import ApiError

        error = ApiError(409, "revision_conflict", "The session has moved on.", revision=12)
        assert error.problem().model_dump()["revision"] == 12
        ```
    """

    def __init__(
        self, status: int, code: str, detail: str, *, headers: dict[str, str] | None = None, **extras: Any
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers
        self.extras = extras

    def problem(self) -> Problem:
        """Return the problem document for this error."""
        return Problem(
            title=HTTPStatus(self.status).phrase, status=self.status, detail=self.detail, code=self.code, **self.extras
        )


def not_found(resource: str, resource_id: str) -> ApiError:
    """Build the 404 for an identifier that matches nothing the caller can read.

    Use it for a resource that belongs to another client too, so a caller can't learn that the
    identifier exists.

    Args:
        resource: The kind of resource, like `"session"`.
        resource_id: The identifier the caller sent. The response repeats at most its first 128
            characters.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `not_found`.
    """
    shown = resource_id if len(resource_id) <= _MAX_TEXT_SHOWN else resource_id[:_MAX_TEXT_SHOWN] + "..."
    return ApiError(404, "not_found", f"No {resource} has the id {shown!r}.", resource=resource, id=shown)


def forbidden(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 403 for a caller whose role doesn't allow the request.

    Args:
        code: The error code, like `"referee_required"`.
        detail: What the caller may not do.
        **extras: Extra members of the problem document.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 403.
    """
    return ApiError(403, code, detail, **extras)


def conflict(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 409 for a request that conflicts with the state of a resource.

    Args:
        code: The error code, like `"revision_conflict"`.
        detail: What the request conflicts with.
        **extras: Extra members of the problem document.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 409.
    """
    return ApiError(409, code, detail, **extras)


def invalid(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 422 for a request that's well formed but that the server can't accept.

    Args:
        code: The error code, like `"character_invalid"`.
        detail: What's wrong with the request.
        **extras: Extra members of the problem document, like a list of rejections.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 422.
    """
    return ApiError(422, code, detail, **extras)


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    """Return the HTTP response for a problem document.

    Args:
        problem: The problem to send.
        headers: Extra response headers.

    Returns:
        A response with the problem's status and the `application/problem+json` media type.
    """
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Return the `responses` argument that documents an endpoint's error statuses in OpenAPI.

    Pass the result to a route decorator. Every route under `/v1` can also return 401, 422,
    500, and 503, which the router documents once for all of them.
    [`create_app`][bxapi.app.create_app] changes the media type of each of these responses to
    `application/problem+json` when it builds the OpenAPI document.

    Args:
        *statuses: The error statuses the endpoint can return, like `404` and `409`.

    Returns:
        A mapping from each status to its OpenAPI response description.

    Examples:
        ```python
        from bxapi.errors import problem_responses

        responses = problem_responses(404)
        assert responses[404]["description"] == "Not Found"
        ```
    """
    return {status: {"description": HTTPStatus(status).phrase, "model": Problem} for status in statuses}


def _quoted(name: str) -> str:
    # Pydantic puts quotes around a discriminator's name in `ctx` but not around the tag.
    return "'" + name.strip("'") + "'"


def _message(entry: Any) -> str:
    # Pydantic's message for an unknown tag lists every valid tag, and the response would cut it off at
    # _MAX_TEXT_SHOWN characters in the middle of the list.
    context = entry.get("ctx") or {}
    match entry["type"]:
        case "union_tag_invalid" if "tag" in context and "discriminator" in context:
            tag = shorten(str(context["tag"]), _MAX_TAG_SHOWN)
            return f"'{tag}' isn't a value that {_quoted(str(context['discriminator']))} accepts."
        case "union_tag_not_found" if "discriminator" in context:
            return f"The body needs {_quoted(str(context['discriminator']))}."
        case _:
            return str(entry["msg"])


def _field_error(entry: Any) -> FieldError:
    location = [part if isinstance(part, int) else shorten(str(part), _MAX_TEXT_SHOWN) for part in entry["loc"]]
    return FieldError(loc=location, msg=shorten(_message(entry), _MAX_TEXT_SHOWN), type=str(entry["type"]))


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that turn exceptions into problem documents.

    [`create_app`][bxapi.app.create_app] calls it once. The handlers cover:

    - [`ApiError`][bxapi.errors.ApiError], sent as raised.
    - A request FastAPI can't validate, sent as 422 `validation_error`. `errors` lists the
      first 20 of pydantic's errors, without the values the caller sent, and `error_count`
      states how many there are. For a `command_type` that's missing or unknown, the `msg` is a
      short sentence instead of pydantic's message, which lists every valid value.
    - osrlib's `ContentValidationError`, sent as 422 `content_invalid`.
    - osrlib's `SaveVersionError` and `ReplayVersionError`, sent as 409
      `version_unsupported`.
    - SQLite's busy and locked errors, raised when another process holds the database's write
      lock for longer than [`BUSY_TIMEOUT_SECONDS`][bxapi.db.BUSY_TIMEOUT_SECONDS], sent as
      503 `database_busy` with the header `Retry-After: 1`.
    - Starlette's `HTTPException`, like the 404 for a path with no route.
    - Any other exception, logged with its traceback and sent as 500 `internal_error` with no
      detail about the cause.

    Every `detail` is at most [`MAX_DETAIL_LENGTH`][bxapi.errors.MAX_DETAIL_LENGTH] characters,
    because an osrlib message can repeat a large part of the document the caller sent.

    Args:
        app: The application to register the handlers on.
    """

    @app.exception_handler(ApiError)
    def _api_error(request: Request, error: ApiError) -> JSONResponse:
        return problem_response(error.problem(), error.headers)

    @app.exception_handler(RequestValidationError)
    def _request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        entries = error.errors()
        problem = ApiError(
            422,
            "validation_error",
            "The request failed validation. See `errors` for each field.",
            errors=[_field_error(entry) for entry in entries[:MAX_FIELD_ERRORS]],
            error_count=len(entries),
        ).problem()
        return problem_response(problem)

    @app.exception_handler(ContentValidationError)
    def _content_validation_error(request: Request, error: ContentValidationError) -> JSONResponse:
        return problem_response(ApiError(422, "content_invalid", str(error)).problem())

    @app.exception_handler(SaveVersionError)
    @app.exception_handler(ReplayVersionError)
    def _version_error(request: Request, error: Exception) -> JSONResponse:
        return problem_response(ApiError(409, "version_unsupported", str(error)).problem())

    @app.exception_handler(sqlite3.OperationalError)
    def _database_error(request: Request, error: sqlite3.OperationalError) -> JSONResponse:
        # The extended result codes, like SQLITE_BUSY_SNAPSHOT, have the primary code in the low byte.
        # An OperationalError that Python code raises, rather than SQLite, has no code.
        code = getattr(error, "sqlite_errorcode", None)
        if code is None or code & 0xFF not in _BUSY_CODES:
            raise error
        problem = ApiError(
            503, "database_busy", "Another process is writing to the database. Send the request again."
        ).problem()
        return problem_response(problem, {"Retry-After": "1"})

    @app.exception_handler(StarletteHTTPException)
    def _http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        phrase = HTTPStatus(error.status_code).phrase
        code = phrase.lower().replace(" ", "_").replace("-", "_")
        detail = error.detail if isinstance(error.detail, str) else phrase
        problem = Problem(title=phrase, status=error.status_code, detail=detail, code=code)
        return problem_response(problem, dict(error.headers) if error.headers else None)

    @app.exception_handler(Exception)
    def _unexpected(request: Request, error: Exception) -> JSONResponse:
        _log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=error)
        return problem_response(ApiError(500, "internal_error", "The server failed to handle the request.").problem())
