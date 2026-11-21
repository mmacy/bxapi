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
  "request_id": "req_Zk3uY0mWq1x8Jr5cTt2aLw",
  "resource": "session",
  "id": "ses_x"
}
```

`request_id` is the id the server gave the request, which the `X-Request-Id` response header has
too. The server logs it with the request, so whoever runs the server can find the request.

An endpoint raises [`ApiError`][bxapi.errors.ApiError], or calls one of the functions here
that build one, and [`install_error_handlers`][bxapi.errors.install_error_handlers] turns the
exception into the response. The same handlers convert a request that fails validation, an
osrlib exception, a busy database, and any other exception.

A command that osrlib refuses isn't an error. The command endpoint responds with status 200
and `accepted` set to `false`.
"""

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from osrlib.core.validation import Rejection
from osrlib.errors import ContentValidationError, ReplayVersionError, SaveVersionError
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.json_schema import SkipJsonSchema
from starlette.exceptions import HTTPException as StarletteHTTPException

from bxapi.db import StorageFull
from bxapi.models import left_out_when_none
from bxapi.request_ids import REQUEST_ID_HEADER, request_id_of
from bxapi.security_headers import API_HEADERS

__all__ = [
    "INTERNAL_ERROR",
    "MAX_DETAIL_LENGTH",
    "MAX_FIELD_ERRORS",
    "PROBLEM_MEDIA_TYPE",
    "VALIDATION_ERROR",
    "ApiError",
    "FieldError",
    "Problem",
    "conflict",
    "content_too_large",
    "database_busy",
    "forbidden",
    "install_error_handlers",
    "internal_error",
    "invalid",
    "is_reported_error",
    "not_found",
    "problem_response",
    "problem_responses",
    "request_timeout",
    "shorten",
    "storage_full",
    "unsupported_media_type",
    "validation_error",
]

PROBLEM_MEDIA_TYPE = "application/problem+json"
"""The media type of every error response."""

MAX_DETAIL_LENGTH = 1000
"""The most characters a problem document's `detail` has."""

MAX_FIELD_ERRORS = 20
"""The most entries the `errors` member of a `validation_error` lists."""

VALIDATION_ERROR = (
    "`validation_error`: The request doesn't match the operation's schema. A required field is missing, a value has "
    "the wrong type or is out of range, or the body isn't valid JSON. `errors` lists the first 20 problems, and the "
    "`loc` of each is the path to the value. Fix the request before you send it again."
)
"""The OpenAPI entry for 422 `validation_error`, which every route under `/v1` that takes a body can return.

[`create_app`][bxapi.app.create_app] replaces it with an entry about query parameters on a route
that takes no body, and leaves it out on a route where every request matches the schema.
"""

INTERNAL_ERROR = (
    "`internal_error`: The server failed because of a bug. The response says nothing about the cause, and the server "
    "logs it. Report the request to the server's operator."
)
"""The OpenAPI entry for 500 `internal_error`, which every route under `/v1` can return."""

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

    Every problem document has `type`, `title`, `status`, `detail`, `code`, and `request_id`. The
    server leaves out the other members, except the ones that the document's code adds:

    - `validation_error` adds `errors` and `error_count`.
    - `not_found` adds `resource` and `id`.
    - `revision_conflict` and `session_busy` add `revision`.
    - `referee_command` adds `command_type`.
    - `character_invalid` adds `rejections` when osrlib reports them. Otherwise it adds `field`
      and `id`, and `index` when `field` is a list.
    - `storage_full` adds `used_bytes` and `max_bytes`.

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
    request_id: str | SkipJsonSchema[None] = left_out_when_none()
    """The id of the request, which the `X-Request-Id` response header has too. The server logs
    it with the request. Include it when you report the error to whoever runs the server."""
    errors: list[FieldError] | SkipJsonSchema[None] = left_out_when_none()
    """`validation_error` only: the first 20 parts of the request that failed validation."""
    error_count: int | SkipJsonSchema[None] = left_out_when_none()
    """`validation_error` only: how many parts of the request failed validation, including any
    that `errors` leaves out."""
    resource: str | SkipJsonSchema[None] = left_out_when_none()
    """`not_found` only: the kind of resource the request asked for, like `"session"`."""
    id: str | SkipJsonSchema[None] = left_out_when_none()
    """For `not_found`, the id in the request, or its first 128 characters followed by `...`.
    For `character_invalid`, the value of the entry that's wrong, like an item id."""
    revision: int | SkipJsonSchema[None] = left_out_when_none()
    """`revision_conflict` and `session_busy` only: the session's current revision."""
    command_type: str | SkipJsonSchema[None] = left_out_when_none()
    """`referee_command` only: the `command_type` of the referee command a player sent."""
    field: str | SkipJsonSchema[None] = left_out_when_none()
    """`character_invalid` only, when osrlib reports no rejections: the request field that's
    wrong, like `"equip_item_ids"`."""
    index: int | SkipJsonSchema[None] = left_out_when_none()
    """`character_invalid` only, when `field` is a list: the position of the entry that's
    wrong, counted from 0."""
    used_bytes: int | SkipJsonSchema[None] = left_out_when_none()
    """`storage_full` only: the bytes of documents the server stores for your client."""
    max_bytes: int | SkipJsonSchema[None] = left_out_when_none()
    """`storage_full` only: the most bytes the server stores for one client."""
    rejections: list[Rejection] | SkipJsonSchema[None] = left_out_when_none()
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


def validation_error(entries: Sequence[Any]) -> ApiError:
    """Build the 422 for a request that doesn't match the operation's schema.

    Args:
        entries: pydantic's errors for the request, as `RequestValidationError.errors()` lists
            them. Each is a mapping with `type`, `loc`, and `msg`, and optionally `ctx`.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `validation_error`. Its `errors`
        member lists the first 20 entries, and `error_count` states how many there are.

    Examples:
        ```python
        from bxapi.errors import validation_error

        entry = {"type": "missing", "loc": ("body", "expression"), "msg": "Field required"}
        problem = validation_error([entry]).problem()
        assert problem.errors is not None and problem.errors[0].loc == ["body", "expression"]
        ```
    """
    return ApiError(
        422,
        "validation_error",
        "The request failed validation. See `errors` for each field.",
        errors=[_field_error(entry) for entry in entries[:MAX_FIELD_ERRORS]],
        error_count=len(entries),
    )


def internal_error() -> ApiError:
    """Build the 500 for an exception the server doesn't expect.

    The detail says nothing about the cause, because an exception's message can contain anything.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `internal_error`.
    """
    return ApiError(500, "internal_error", "The server failed to handle the request.")


def database_busy() -> ApiError:
    """Build the 503 for a request that waited too long for the database's write lock.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `database_busy` and the header
        `Retry-After: 1`.
    """
    return ApiError(
        503,
        "database_busy",
        "Another process is writing to the database. Send the request again.",
        headers={"Retry-After": "1"},
    )


def storage_full(used_bytes: int, max_bytes: int) -> ApiError:
    """Build the 507 for a request that would store more for a client that has reached its limit.

    Args:
        used_bytes: The bytes of documents the server stores for the client.
        max_bytes: The most bytes the server stores for one client.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `storage_full`, and `used_bytes` and
        `max_bytes` as extra members.
    """
    return ApiError(
        507,
        "storage_full",
        f"The server stores {used_bytes} bytes for your client, and the limit is {max_bytes}. The request stored "
        "nothing. Delete sessions, saves, adventures, or characters you no longer need, and send the request again.",
        used_bytes=used_bytes,
        max_bytes=max_bytes,
    )


def content_too_large(max_bytes: int, *, for_document: bool = True) -> ApiError:
    """Build the 413 for a request body over a size limit.

    Args:
        max_bytes: The largest body the server accepts for the request, in bytes.
        for_document: Whether `max_bytes` is the server's `max_body_bytes` setting, which limits
            every body. `False` when it's the smaller limit of an operation that takes no
            document, [`SMALL_BODY_BYTES`][bxapi.json_body.SMALL_BODY_BYTES].

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `content_too_large`.
    """
    scope = "" if for_document else " for an operation that takes no document"
    return ApiError(
        413, "content_too_large", f"The request body is larger than the {max_bytes} bytes this server accepts{scope}."
    )


def request_timeout(timeout_seconds: float) -> ApiError:
    """Build the 408 for a request body that didn't arrive in time.

    Args:
        timeout_seconds: The most time the server waits for a body, in seconds.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `request_timeout`.
    """
    return ApiError(
        408,
        "request_timeout",
        f"The request body didn't arrive within the {timeout_seconds:g} seconds this server waits.",
    )


def unsupported_media_type() -> ApiError:
    """Build the 415 for a request body that the request doesn't declare as JSON.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `unsupported_media_type`.
    """
    return ApiError(
        415,
        "unsupported_media_type",
        "The request has a body, and its Content-Type header doesn't say that the body is JSON. Send the body as "
        "JSON with the header Content-Type: application/json.",
    )


def problem_response(
    problem: Problem, headers: dict[str, str] | None = None, *, request_id: str | None = None
) -> JSONResponse:
    """Return the HTTP response for a problem document.

    Args:
        problem: The problem to send.
        headers: Extra response headers.
        request_id: The request's id, from [`request_id_of`][bxapi.request_ids.request_id_of].
            The response has it as the problem's `request_id` and in the `X-Request-Id` header.

    Returns:
        A response with the problem's status and the `application/problem+json` media type.
    """
    if request_id is not None:
        problem = problem.model_copy(update={"request_id": request_id})
        headers = {**(headers or {}), REQUEST_ID_HEADER: request_id}
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def problem_responses(errors: Mapping[int, Sequence[str]]) -> dict[int | str, dict[str, Any]]:
    """Return the `responses` argument that documents an endpoint's error statuses in OpenAPI.

    Pass the result to a route decorator. For each status, list one entry for each error code the
    endpoint returns with that status. Start the entry with the code, then say what causes it and
    what the caller does about it. Every route under `/v1` can also return 401, 500, and 503, and
    most can return 422, which [`create_app`][bxapi.app.create_app] documents once for all of them.
    A route that lists
    422 or 500 replaces that shared entry, so it starts its list with
    [`VALIDATION_ERROR`][bxapi.errors.VALIDATION_ERROR] or
    [`INTERNAL_ERROR`][bxapi.errors.INTERNAL_ERROR]. `create_app` changes the media type of each of
    these responses to `application/problem+json` when it builds the OpenAPI document.

    Args:
        errors: A mapping from each error status to its entries, each a line of Markdown.

    Returns:
        A mapping from each status to its OpenAPI response, with the entries as a Markdown list.

    Examples:
        ```python
        from bxapi.errors import problem_responses

        responses = problem_responses({404: ["`not_found`: No session has that id."]})
        assert responses[404]["description"] == "- `not_found`: No session has that id."
        ```
    """
    return {
        status: {"description": "\n".join(f"- {entry}" for entry in entries), "model": Problem}
        for status, entries in errors.items()
    }


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


def _is_busy(error: BaseException) -> bool:
    # The extended result codes, like SQLITE_BUSY_SNAPSHOT, have the primary code in the low byte.
    # An OperationalError that Python code raises, rather than SQLite, has no code.
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.OperationalError) and code is not None and code & 0xFF in _BUSY_CODES


def is_reported_error(error: BaseException) -> bool:
    """Return whether the server reports an exception as a problem other than 500 `internal_error`.

    The server raises each of these exceptions on purpose, before the request changes anything or
    in place of the change: an [`ApiError`][bxapi.errors.ApiError], a request that fails
    validation, an osrlib content or version error, a busy database, a client at its storage
    limit, and Starlette's `HTTPException`. Any other exception is a bug, and the request may have changed something
    before it.

    Args:
        error: The exception an endpoint raised.

    Returns:
        `True` if one of the handlers that
        [`install_error_handlers`][bxapi.errors.install_error_handlers] registers turns the
        exception into a problem document with a code other than `internal_error`.

    Examples:
        ```python
        from bxapi.errors import is_reported_error, not_found

        assert is_reported_error(not_found("session", "ses_x"))
        assert not is_reported_error(KeyError("name"))
        ```
    """
    reported = (
        ApiError,
        RequestValidationError,
        ContentValidationError,
        SaveVersionError,
        ReplayVersionError,
        StarletteHTTPException,
        StorageFull,
    )
    return isinstance(error, reported) or _is_busy(error)


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
    - [`StorageFull`][bxapi.db.StorageFull], raised when a client has reached the limit on its
      stored bytes, sent as 507 `storage_full`.
    - SQLite's busy and locked errors, raised when another process holds the database's write
      lock for longer than [`BUSY_TIMEOUT_SECONDS`][bxapi.db.BUSY_TIMEOUT_SECONDS], sent as
      503 `database_busy` with the header `Retry-After: 1`.
    - Starlette's `HTTPException`, like the 404 for a path with no route.
    - Any other exception, logged with its traceback and sent as 500 `internal_error` with no
      detail about the cause.

    Every `detail` is at most [`MAX_DETAIL_LENGTH`][bxapi.errors.MAX_DETAIL_LENGTH] characters,
    because an osrlib message can repeat a large part of the document the caller sent. Every
    response has the request's id, as the problem's `request_id` and in the `X-Request-Id` header.

    Args:
        app: The application to register the handlers on.
    """

    def respond(request: Request, error: ApiError) -> JSONResponse:
        return problem_response(error.problem(), error.headers, request_id=request_id_of(request.scope))

    @app.exception_handler(ApiError)
    def _api_error(request: Request, error: ApiError) -> JSONResponse:
        return respond(request, error)

    @app.exception_handler(RequestValidationError)
    def _request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return respond(request, validation_error(error.errors()))

    @app.exception_handler(ContentValidationError)
    def _content_validation_error(request: Request, error: ContentValidationError) -> JSONResponse:
        return respond(request, ApiError(422, "content_invalid", str(error)))

    @app.exception_handler(SaveVersionError)
    @app.exception_handler(ReplayVersionError)
    def _version_error(request: Request, error: Exception) -> JSONResponse:
        return respond(request, ApiError(409, "version_unsupported", str(error)))

    @app.exception_handler(StorageFull)
    def _storage_full(request: Request, error: StorageFull) -> JSONResponse:
        return respond(request, storage_full(error.used_bytes, error.max_bytes))

    @app.exception_handler(sqlite3.OperationalError)
    def _database_error(request: Request, error: sqlite3.OperationalError) -> JSONResponse:
        if not _is_busy(error):
            raise error
        return respond(request, database_busy())

    @app.exception_handler(StarletteHTTPException)
    def _http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        phrase = HTTPStatus(error.status_code).phrase
        code = phrase.lower().replace(" ", "_").replace("-", "_")
        detail = error.detail if isinstance(error.detail, str) else phrase
        headers = dict(error.headers) if error.headers else None
        return respond(request, ApiError(error.status_code, code, detail, headers=headers))

    @app.exception_handler(Exception)
    def _unexpected(request: Request, error: Exception) -> JSONResponse:
        request_id = request_id_of(request.scope)
        _log.exception(
            "unhandled error on %s %s, request %s", request.method, request.url.path, request_id, exc_info=error
        )
        response = respond(request, internal_error())
        # Starlette sends this response from outside every middleware, so SecurityHeadersMiddleware can't add its
        # headers and HeadMiddleware can't drop the body.
        response.headers.update(API_HEADERS)
        if request.method == "HEAD":
            response.body = b""
        return response
